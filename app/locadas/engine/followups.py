"""Nachfass-Logik: planen, freigeben, verschicken, stoppen."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import Business, DocKind, DocStatus, Document, FollowUp, FollowUpStatus, log_event, utcnow
from ..mail.imap import MailAuthError, MailConnectError
from ..mail.smtp import build_message
from . import accounts as acc
from . import personalize, texts

log = logging.getLogger(__name__)


# --- Planung -------------------------------------------------------------------

def schedule_followups(session: Session, doc: Document, now: datetime | None = None) -> list[FollowUp]:
    """Legt die Nachfass-Schritte für ein neues Dokument an."""
    now = now or utcnow()
    if doc.kind == DocKind.OFFER:
        base = doc.sent_at
        offsets = settings.offer_followup_days
    else:
        base = doc.due_date or (doc.sent_at + timedelta(days=settings.default_invoice_due_days))
        offsets = settings.invoice_followup_days
    created: list[FollowUp] = []
    for step, days in enumerate(offsets, start=1):
        due = base + timedelta(days=days)
        if due < now:
            # Altes Dokument (Erst-Scan): verpasste Schritte nicht nachholen, nur den nächsten in Kürze
            due = now + timedelta(hours=1)
        fu = FollowUp(document_id=doc.id, step=step, due_at=due, status=FollowUpStatus.SCHEDULED, approval_token=secrets.token_urlsafe(24))
        session.add(fu)
        created.append(fu)
    # verpasste Schritte zusammenziehen: nur der erste fällige bleibt "jetzt", die anderen rücken auf
    created.sort(key=lambda f: f.step)
    last_due = None
    for fu in created:
        if last_due is not None and fu.due_at <= last_due:
            fu.due_at = last_due + timedelta(days=max(1, offsets[fu.step - 1] - offsets[fu.step - 2]))
        last_due = fu.due_at
    doc.next_followup_at = created[0].due_at if created else None
    session.flush()
    return created


def in_send_window(now: datetime, business_tz: str | None = None) -> bool:
    tz = ZoneInfo(business_tz or settings.timezone)
    local = now.astimezone(tz)
    if local.weekday() >= 5:  # Sa/So
        return False
    return settings.send_hour_from <= local.hour < settings.send_hour_to


def cancel_open_followups(session: Session, doc: Document, reason: str) -> int:
    n = 0
    for fu in doc.followups:
        if fu.status in (FollowUpStatus.SCHEDULED, FollowUpStatus.AWAITING_APPROVAL):
            fu.status = FollowUpStatus.CANCELLED
            fu.error = reason
            n += 1
    doc.next_followup_at = None
    return n


def close_document(session: Session, doc: Document, status: DocStatus, reason: str) -> None:
    cancel_open_followups(session, doc, reason)
    doc.status = status
    doc.closed_at = utcnow()
    doc.close_reason = reason
    labels = {
        DocStatus.REPLIED: "Kunde hat geantwortet",
        DocStatus.WON: "Auftrag bekommen",
        DocStatus.LOST: "Absage",
        DocStatus.PAID: "Bezahlt",
        DocStatus.DONE: "Abgeschlossen",
        DocStatus.IGNORED: "Nicht nachfassen",
    }
    log_event(session, doc.business_id, "closed", f"{doc.kind_label} {doc.document_number or doc.title}: {labels.get(status, status.value)}.", doc.id)


def reopen_document(session: Session, doc: Document) -> None:
    doc.status = DocStatus.OPEN
    doc.closed_at = None
    doc.close_reason = ""
    for fu in doc.followups:
        if fu.status == FollowUpStatus.CANCELLED:
            fu.status = FollowUpStatus.SCHEDULED
            fu.error = ""
            if fu.due_at < utcnow():
                fu.due_at = utcnow() + timedelta(hours=1)
    nxt = [f.due_at for f in doc.followups if f.status == FollowUpStatus.SCHEDULED]
    doc.next_followup_at = min(nxt) if nxt else None
    log_event(session, doc.business_id, "reopened", f"{doc.kind_label} {doc.document_number or doc.title} wird wieder nachgefasst.", doc.id)


# --- Versand ------------------------------------------------------------------

def _prepare(fu: FollowUp) -> None:
    doc = fu.document
    if not fu.subject:
        fu.subject = texts.subject_for(doc, fu.step)
    if not fu.body:
        # Personalisiert (Claude + Prüfung), wenn Schlüssel da und im Betrieb an; sonst bzw. bei Fehler die Vorlage
        fu.body = personalize.body_for(doc, fu.step)


def send_followup(session: Session, fu: FollowUp) -> bool:
    doc = fu.document
    account = doc.account
    business = doc.business
    _prepare(fu)
    if not doc.customer_email:
        fu.status = FollowUpStatus.FAILED
        fu.error = "Keine Kundenadresse"
        return False
    msg = build_message(
        from_name=account.display_name or business.name,
        from_addr=account.email,
        to_name=doc.customer_name,
        to_addr=doc.customer_email,
        subject=fu.subject,
        body=fu.body,
        in_reply_to=doc.message_id,
        references=[doc.message_id] if doc.message_id else None,
    )
    try:
        fu.message_id = acc.smtp_for(account).send(msg)
    except (MailAuthError, MailConnectError) as exc:
        fu.status = FollowUpStatus.FAILED
        fu.error = str(exc)[:1000]
        acc.mark_error(session, account, str(exc))
        log.warning("Versand fehlgeschlagen für Dokument %s: %s", doc.id, exc)
        return False
    acc.mark_ok(account)
    fu.status = FollowUpStatus.SENT
    fu.sent_at = utcnow()
    doc.followups_sent += 1
    remaining = [f.due_at for f in doc.followups if f.status == FollowUpStatus.SCHEDULED]
    doc.next_followup_at = min(remaining) if remaining else None
    log_event(session, doc.business_id, "sent", f"Nachfass-Mail {fu.step} für {doc.kind_label} {doc.document_number or doc.title} an {doc.customer_email} geschickt.", doc.id)
    return True


def _approval_mail(business: Business, fu: FollowUp) -> tuple[str, str]:
    doc = fu.document
    base = settings.base_url
    ok = f"{base}/f/{fu.approval_token}/send"
    skip = f"{base}/f/{fu.approval_token}/skip"
    stop = f"{base}/f/{fu.approval_token}/stop"
    body = (
        f"Hallo {business.owner_name or business.name},\n\n"
        f"ich würde jetzt bei {doc.customer_name or doc.customer_email} nachfassen ({doc.kind_label} {doc.document_number or doc.title}).\n\n"
        f"Das würde ich in deinem Namen schicken:\n\n"
        f"---\n{fu.body}\n---\n\n"
        f"Senden:        {ok}\n"
        f"Diesmal nicht: {skip}\n"
        f"Kunde hat sich schon gemeldet, nicht mehr nachfassen: {stop}\n\n"
        f"Noch {max(0, settings.approvals_before_auto - business.approvals_done)} Freigaben, dann läuft es automatisch. "
        f"Du kannst das in den Einstellungen jederzeit ändern.\n\n"
        f"Locadas Nachfasser"
    )
    subject = f"Nachfassen bei {doc.customer_name or doc.customer_email}? ({doc.kind_label} {doc.document_number or doc.title})".strip()
    return subject, body


def notify_business(session: Session, business: Business, subject: str, body: str) -> bool:
    """Schickt dem Betrieb eine Mail in sein eigenes Postfach, über sein eigenes SMTP."""
    account = next((a for a in business.accounts if a.status.value != "disconnected"), None)
    if account is None:
        return False
    msg = build_message(
        from_name="Locadas Nachfasser",
        from_addr=account.email,
        to_name=business.owner_name or business.name,
        to_addr=account.email,
        subject=subject,
        body=body,
    )
    try:
        acc.smtp_for(account).send(msg)
        return True
    except (MailAuthError, MailConnectError) as exc:
        acc.mark_error(session, account, str(exc))
        return False


def request_approval(session: Session, fu: FollowUp) -> None:
    doc = fu.document
    _prepare(fu)
    fu.status = FollowUpStatus.AWAITING_APPROVAL
    subject, body = _approval_mail(doc.business, fu)
    if notify_business(session, doc.business, subject, body):
        log_event(session, doc.business_id, "approval", f"Freigabe angefragt: Nachfass-Mail {fu.step} für {doc.kind_label} {doc.document_number or doc.title}.", doc.id)


def process_due(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Alle fälligen Nachfass-Schritte: freigeben lassen oder senden."""
    now = now or utcnow()
    stats = {"sent": 0, "approval": 0, "skipped": 0}
    if not in_send_window(now):
        return stats
    due = session.scalars(
        select(FollowUp).where(FollowUp.status == FollowUpStatus.SCHEDULED, FollowUp.due_at <= now).order_by(FollowUp.due_at)
    ).all()
    for fu in due:
        doc = fu.document
        business = doc.business
        if doc.status != DocStatus.OPEN or business.paused:
            stats["skipped"] += 1
            continue
        if doc.kind == DocKind.OFFER and not business.followup_offers:
            fu.status = FollowUpStatus.SKIPPED
            continue
        if doc.kind == DocKind.INVOICE and not business.followup_invoices:
            fu.status = FollowUpStatus.SKIPPED
            continue
        # Nur ein Schritt pro Dokument gleichzeitig: wartet ein älterer auf Freigabe, nicht stapeln
        if any(f.status == FollowUpStatus.AWAITING_APPROVAL for f in doc.followups):
            continue
        if business.needs_approval:
            request_approval(session, fu)
            stats["approval"] += 1
        else:
            if send_followup(session, fu):
                stats["sent"] += 1
        session.commit()
    return stats


# --- Freigabe per Link ---------------------------------------------------------

def find_by_token(session: Session, token: str) -> FollowUp | None:
    if not token:
        return None
    return session.scalar(select(FollowUp).where(FollowUp.approval_token == token))


def approve(session: Session, fu: FollowUp) -> bool:
    if fu.status != FollowUpStatus.AWAITING_APPROVAL:
        return False
    ok = send_followup(session, fu)
    if ok:
        b = fu.document.business
        b.approvals_done += 1
        if b.approvals_done >= settings.approvals_before_auto:
            b.auto_mode = True
            log_event(session, b.id, "auto", "Ab jetzt läuft das Nachfassen automatisch. Du kannst das in den Einstellungen ändern.")
    return ok


def skip(session: Session, fu: FollowUp) -> None:
    if fu.status != FollowUpStatus.AWAITING_APPROVAL:
        return
    fu.status = FollowUpStatus.SKIPPED
    doc = fu.document
    remaining = [f.due_at for f in doc.followups if f.status == FollowUpStatus.SCHEDULED]
    doc.next_followup_at = min(remaining) if remaining else None
    log_event(session, doc.business_id, "skipped", f"Nachfass-Mail {fu.step} für {doc.kind_label} {doc.document_number or doc.title} übersprungen.", doc.id)


# --- Aufräumen ---------------------------------------------------------------

def finish_exhausted(session: Session, now: datetime | None = None, grace_days: int = 14) -> int:
    """Dokumente, bei denen alle Schritte raus sind und seit X Tagen nichts kam, auf 'Abgeschlossen' setzen."""
    now = now or utcnow()
    n = 0
    docs = session.scalars(select(Document).where(Document.status == DocStatus.OPEN, Document.next_followup_at.is_(None))).all()
    for doc in docs:
        sent = [f.sent_at for f in doc.followups if f.status == FollowUpStatus.SENT and f.sent_at]
        pending = any(f.status in (FollowUpStatus.SCHEDULED, FollowUpStatus.AWAITING_APPROVAL) for f in doc.followups)
        if pending or not sent:
            continue
        if max(sent) + timedelta(days=grace_days) <= now:
            close_document(session, doc, DocStatus.DONE, "Alle Nachfass-Schritte durch, keine Antwort")
            n += 1
    return n
