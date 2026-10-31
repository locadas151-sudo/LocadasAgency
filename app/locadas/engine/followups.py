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
from ..notify import Notification, send_email
from ..notify import messages as wa_msgs
from ..notify import send as notify
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
    _copy_to_sent(account, msg)
    fu.status = FollowUpStatus.SENT
    fu.sent_at = utcnow()
    doc.followups_sent += 1
    remaining = [f.due_at for f in doc.followups if f.status == FollowUpStatus.SCHEDULED]
    doc.next_followup_at = min(remaining) if remaining else None
    log_event(session, doc.business_id, "sent", f"Nachfass-Mail {fu.step} für {doc.kind_label} {doc.document_number or doc.title} an {doc.customer_email} geschickt.", doc.id)
    return True


# Diese Anbieter legen per SMTP verschickte Mails selbst im Gesendet-Ordner ab
_AUTO_SAVES_SENT = {"gmail", "google", "outlook", "microsoft"}


def _copy_to_sent(account, msg) -> None:
    """Damit der Betrieb seine Nachfass-Mails im eigenen Gesendet-Ordner sieht."""
    if account.provider in _AUTO_SAVES_SENT:
        return
    try:
        with acc.imap_for(account) as client:
            client.append(account.sent_folder, msg.as_bytes())
    except Exception as exc:  # nie den Versand daran scheitern lassen
        log.info("Kopie in Gesendet fehlgeschlagen (%s): %s", account.email, exc)


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
    """Nur Mail ins eigene Postfach. Neue Stellen bitte über notify.send(Notification) schicken."""
    return send_email(session, business, subject, body)


def approval_notification(business: Business, fus: list[FollowUp], subject: str, body: str) -> Notification:
    """WhatsApp zeigt die Freigaben einzeln nacheinander (3 Knöpfe pro Nachricht), die Mail bündelt sie."""
    left = max(0, settings.approvals_before_auto - business.approvals_done) if business.needs_approval else None
    return wa_msgs.approval(business, fus[0], 1, len(fus), subject, body, left)


def _needs_approval(business: Business, doc: Document) -> bool:
    return business.needs_approval or doc.needs_review


def request_approvals(session: Session, business: Business, fus: list[FollowUp]) -> None:
    """Eine einzige Mail mit allen anstehenden Freigaben, statt eine pro Kunde."""
    if not fus:
        return
    for fu in fus:
        _prepare(fu)
        fu.status = FollowUpStatus.AWAITING_APPROVAL
    base = settings.base_url
    if len(fus) == 1:
        subject, body = _approval_mail(business, fus[0])
    else:
        lines = [
            f"Hallo {business.owner_name or business.name},",
            "",
            f"bei {len(fus)} Kunden würde ich jetzt nachfassen. Ein Klick pro Kunde reicht.",
            f"Alle Texte in Ruhe ansehen und freigeben: {base}/freigaben",
            "",
        ]
        for fu in fus:
            d = fu.document
            hint = " (älter als dein Start bei uns, schon erledigt?)" if d.needs_review else ""
            lines += [
                f"• {d.customer_name or d.customer_email}: {d.kind_label} {d.document_number or d.title}{_money(d.amount_gross)}, gesendet {d.sent_at.strftime('%d.%m.')}{hint}",
                f"  Senden: {base}/f/{fu.approval_token}/send",
                f"  Erledigt, nicht nachfassen: {base}/f/{fu.approval_token}/stop",
                "",
            ]
        if business.needs_approval:
            lines.append(f"Noch {max(0, settings.approvals_before_auto - business.approvals_done)} Freigaben, dann läuft es automatisch.")
        lines += ["", "Locadas Nachfasser"]
        subject = f"{len(fus)} Kunden zum Nachfassen, bitte kurz freigeben"
        body = "\n".join(lines)
    if notify(session, approval_notification(business, fus, subject, body)):
        for fu in fus:
            d = fu.document
            log_event(session, d.business_id, "approval", f"Freigabe angefragt: Nachfass-Mail {fu.step} für {d.kind_label} {d.document_number or d.title}.", d.id)


def request_approval(session: Session, fu: FollowUp) -> None:
    request_approvals(session, fu.document.business, [fu])


def _money(v) -> str:
    return f" über {v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".") if v else ""


def _sent_today(session: Session, business_id: int, now: datetime) -> int:
    start = now - timedelta(hours=24)
    return len(session.scalars(
        select(FollowUp.id).join(Document).where(Document.business_id == business_id, FollowUp.status == FollowUpStatus.SENT, FollowUp.sent_at >= start)
    ).all())


def process_due(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Alle fälligen Nachfass-Schritte: Freigaben bündeln oder senden, mit Tageslimit pro Betrieb."""
    now = now or utcnow()
    stats = {"sent": 0, "approval": 0, "skipped": 0, "capped": 0}
    if not in_send_window(now):
        return stats
    due = session.scalars(
        select(FollowUp).where(FollowUp.status == FollowUpStatus.SCHEDULED, FollowUp.due_at <= now).order_by(FollowUp.due_at)
    ).all()
    approvals: dict[int, list[FollowUp]] = {}
    previews: dict[int, list[FollowUp]] = {}
    sent_count: dict[int, int] = {}
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
        # Nur ein Schritt pro Dokument gleichzeitig
        if any(f.status == FollowUpStatus.AWAITING_APPROVAL for f in doc.followups):
            continue
        if any(f.document_id == doc.id for f in approvals.get(business.id, [])):
            continue
        if _needs_approval(business, doc):
            approvals.setdefault(business.id, []).append(fu)
            continue
        if settings.preview_hours > 0:
            if fu.announced_at is None:
                previews.setdefault(business.id, []).append(fu)
                continue
            if fu.announced_at > now - timedelta(hours=settings.preview_hours):
                continue
        if business.id not in sent_count:
            sent_count[business.id] = _sent_today(session, business.id, now)
        if sent_count[business.id] >= settings.max_sends_per_day:
            stats["capped"] += 1
            continue
        if send_followup(session, fu):
            stats["sent"] += 1
            sent_count[business.id] += 1
        session.commit()
    for business_id, fus in approvals.items():
        request_approvals(session, fus[0].document.business, fus)
        stats["approval"] += len(fus)
        session.commit()
    for business_id, fus in previews.items():
        send_preview(session, fus[0].document.business, fus, now)
        stats["announced"] = stats.get("announced", 0) + len(fus)
        session.commit()
    return stats


def send_preview(session: Session, business: Business, fus: list[FollowUp], now: datetime) -> None:
    """Morgen-Vorschau: wer heute eine Nachfass-Mail bekommt, mit Stopp-Link pro Kunde (für Anrufe, die wir nicht sehen)."""
    base = settings.base_url
    send_time = (now + timedelta(hours=settings.preview_hours)).astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M")
    lines = [
        f"Hallo {business.owner_name or business.name},",
        "",
        f"heute ab {send_time} Uhr fasse ich bei {'diesem Kunden' if len(fus) == 1 else f'diesen {len(fus)} Kunden'} nach.",
        "Hat sich jemand schon gemeldet, zum Beispiel am Telefon? Dann einfach auf Stopp klicken.",
        "",
    ]
    for fu in fus:
        d = fu.document
        lines += [
            f"• {d.customer_name or d.customer_email}: {d.kind_label} {d.document_number or d.title}{_money(d.amount_gross)} (Nachfass-Mail {fu.step} von 3)",
            f"  Stopp, hat sich gemeldet: {base}/f/{fu.approval_token}/stop",
            "",
        ]
    lines += ["Du musst sonst nichts tun.", "", "Locadas Nachfasser"]
    names = ", ".join((f.document.customer_name or f.document.customer_email).split(" <")[0] for f in fus[:3]) + (" …" if len(fus) > 3 else "")
    if notify(session, wa_msgs.preview(business, fus, send_time, f"Heute nachfassen: {names}", "\n".join(lines)), now):
        for fu in fus:
            fu.announced_at = now
    else:
        # Ohne Vorschau keine Sendung: lieber einen Tag später als ungefragt
        for fu in fus:
            fu.announced_at = None


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
        if fu.document.needs_review:
            fu.document.needs_review = False
            return ok
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

def finish_exhausted(session: Session, now: datetime | None = None, grace_days: int | None = None) -> int:
    """Alle Stufen raus, eine Woche nichts gekommen: Vorgang abschließen und den Betrieb einmal fragen, wie es ausging."""
    now = now or utcnow()
    grace = settings.outcome_after_days if grace_days is None else grace_days
    asks: dict[int, list[Document]] = {}
    docs = session.scalars(select(Document).where(Document.status == DocStatus.OPEN, Document.next_followup_at.is_(None))).all()
    for doc in docs:
        sent = [f.sent_at for f in doc.followups if f.status == FollowUpStatus.SENT and f.sent_at]
        pending = any(f.status in (FollowUpStatus.SCHEDULED, FollowUpStatus.AWAITING_APPROVAL) for f in doc.followups)
        if pending or not sent:
            continue
        if max(sent) + timedelta(days=grace) <= now:
            close_document(session, doc, DocStatus.DONE, OUTCOME_PENDING_REASON)
            if doc.outcome_asked_at is None:
                asks.setdefault(doc.business_id, []).append(doc)
    for docs_ in asks.values():
        ask_outcome(session, docs_[0].business, docs_, now)
    return sum(len(v) for v in asks.values())


OUTCOME_PENDING_REASON = "Alle Nachfass-Schritte durch, keine Antwort per Mail"


def outcome_mail(business: Business, docs: list[Document]) -> tuple[str, str]:
    base = settings.base_url
    lines = [f"Hallo {business.owner_name or business.name},", "", "bei diesen Vorgängen habe ich alles nachgefasst und per Mail kam keine Antwort. Wie ist es ausgegangen? Ein Klick reicht.", ""]
    for d in docs:
        head = f"• {d.customer_name or d.customer_email}: {d.kind_label} {d.document_number or d.title}{_money(d.amount_gross)}"
        if d.kind == DocKind.OFFER:
            lines += [head, f"  Auftrag bekommen: {base}/o/{d.outcome_token}/won", f"  Nicht bekommen:   {base}/o/{d.outcome_token}/lost", ""]
        else:
            lines += [head, f"  Ist bezahlt:              {base}/o/{d.outcome_token}/paid", f"  Nicht bezahlt, ich kümmere mich: {base}/o/{d.outcome_token}/unpaid", ""]
    lines += ["Damit siehst du im Dashboard, wie viele deiner Angebote zum Auftrag werden.", "", "Locadas Nachfasser"]
    subject = f"Wie ist es ausgegangen? {len(docs)} {'Vorgang' if len(docs) == 1 else 'Vorgänge'}"
    return subject, "\n".join(lines)


def ask_outcome(session: Session, business: Business, docs: list[Document], now: datetime) -> None:
    """Eine Mail mit allen Vorgängen; per WhatsApp eine Frage nach der anderen (nächste nach jeder Antwort)."""
    subject, body = outcome_mail(business, docs)
    if notify(session, wa_msgs.outcome(docs[0], 1, len(docs), subject, body), now):
        for d in docs:
            d.outcome_asked_at = now
            log_event(session, d.business_id, "outcome_asked", f"Abschlussfrage zu {d.kind_label} {d.document_number or d.title} gestellt.", d.id)


def set_outcome(session: Session, doc: Document, outcome: str) -> str:
    mapping = {"won": (DocStatus.WON, "Auftrag bekommen"), "lost": (DocStatus.LOST, "Nicht bekommen"), "paid": (DocStatus.PAID, "Bezahlt"), "unpaid": (DocStatus.DONE, "Nicht bezahlt, Betrieb kümmert sich")}
    status, label = mapping[outcome]
    doc.status = status
    doc.closed_at = doc.closed_at or utcnow()
    doc.close_reason = label
    log_event(session, doc.business_id, "outcome", f"{doc.kind_label} {doc.document_number or doc.title}: {label}.", doc.id)
    return label
