"""Nachfass-Logik: planen, freigeben, verschicken, stoppen."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import fmt
from ..config import settings
from ..holidays import add_workdays, is_workday
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
    if not is_workday(local.date()):  # Sa/So und bundesweite Feiertage
        return False
    return settings.send_hour_from <= local.hour < settings.send_hour_to


def next_send_time(t: datetime) -> datetime:
    """Frühester Zeitpunkt ab t im Versandfenster (Werktag, SEND_HOUR_FROM bis SEND_HOUR_TO, lokale Zeit)."""
    tz = ZoneInfo(settings.timezone)
    local = t.astimezone(tz)
    if is_workday(local.date()) and settings.send_hour_from <= local.hour < settings.send_hour_to:
        return t
    day = local.date()
    if not (is_workday(day) and local.hour < settings.send_hour_from):
        day = add_workdays(day, 1)
    return datetime(day.year, day.month, day.day, settings.send_hour_from, 0, tzinfo=tz).astimezone(timezone.utc)


_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


def when_phrase(send_at: datetime, now: datetime) -> str:
    """„heute ab 12:30 Uhr“, „morgen ab 8 Uhr“, „am Montag, 03.11., ab 8 Uhr“."""
    tz = ZoneInfo(settings.timezone)
    s_local, n_local = send_at.astimezone(tz), now.astimezone(tz)
    clock = f"{s_local.hour} Uhr" if s_local.minute == 0 else s_local.strftime("%H:%M Uhr")
    days = (s_local.date() - n_local.date()).days
    if days <= 0:
        return f"heute ab {clock}"
    if days == 1:
        return f"morgen ab {clock}"
    return f"am {_WEEKDAYS[s_local.weekday()]}, {s_local.strftime('%d.%m.')}, ab {clock}"


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
            fu.announced_at = fu.send_after = None  # neu ankündigen
            if fu.due_at < utcnow():
                fu.due_at = utcnow() + timedelta(hours=1)
    nxt = [f.due_at for f in doc.followups if f.status == FollowUpStatus.SCHEDULED]
    doc.next_followup_at = min(nxt) if nxt else None
    log_event(session, doc.business_id, "reopened", f"{doc.kind_label} {doc.document_number or doc.title} wird wieder nachgefasst.", doc.id)


# --- Versand ------------------------------------------------------------------

def _prepare(fu: FollowUp, now: datetime | None = None) -> None:
    doc = fu.document
    if not fu.subject:
        fu.subject = texts.subject_for(doc, fu.step)
    if not fu.body:
        # Personalisiert (Claude + Prüfung), wenn Schlüssel da und im Betrieb an; sonst bzw. bei Fehler die Vorlage
        fu.body = personalize.body_for(doc, fu.step, now)


def _attach_pdf(msg, doc: Document) -> None:
    """Einstellung „PDF anhängen“: das Original noch einmal mitschicken statt Link."""
    from pathlib import Path

    if texts.delivery(doc) != "attachment":
        return
    path = Path(doc.pdf_path)
    if not path.exists():
        return
    try:
        msg.add_attachment(path.read_bytes(), maintype="application", subtype="pdf", filename=doc.pdf_filename or path.name.split("_", 1)[-1])
    except OSError as exc:
        log.info("PDF für Dokument %s nicht anhängbar: %s", doc.id, exc)


OFFER_MIN_GAP = timedelta(days=4)   # Angebote: höchstens alle 4 Tage eine Nachfass-Mail
INVOICE_BANK_DAYS = 3               # Rechnungen: nächste Stufe erst Frist der letzten Erinnerung + 3 Bankarbeitstage
_CLAIMABLE = (FollowUpStatus.SCHEDULED, FollowUpStatus.AWAITING_APPROVAL)


def _claim(session: Session, fu: FollowUp) -> FollowUpStatus | None:
    """Atomar „sending“ setzen. Nur wer die Zeile wirklich umstellt, darf senden. Gibt den alten Status zurück."""
    before = fu.status
    if before not in _CLAIMABLE:
        return None
    session.flush()
    res = session.execute(
        update(FollowUp)
        .where(FollowUp.id == fu.id, FollowUp.status.in_(list(_CLAIMABLE)))
        .values(status=FollowUpStatus.SENDING, sent_at=utcnow())  # sent_at = Beginn des Versands, bis er fertig ist
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        session.rollback()
        return None
    session.commit()  # andere Prozesse sehen sofort „sending“
    fu.status = FollowUpStatus.SENDING
    return before


def space_following(doc: Document, sent_fu: FollowUp, sent_at: datetime) -> None:
    """Nach dem Versand: Mindestabstand zur nächsten Stufe herstellen."""
    pending = sorted((f for f in doc.followups if f.status == FollowUpStatus.SCHEDULED and f.step > sent_fu.step), key=lambda f: f.step)
    if not pending:
        return
    if doc.kind == DocKind.INVOICE:
        deadline = texts.payment_deadline(sent_at)
        d = add_workdays(deadline.date(), INVOICE_BANK_DAYS)
        earliest = datetime(d.year, d.month, d.day, settings.send_hour_from, 0, tzinfo=ZoneInfo(settings.timezone)).astimezone(timezone.utc)
        gap = earliest - sent_at
    else:
        earliest = sent_at + OFFER_MIN_GAP
        gap = OFFER_MIN_GAP
    for f in pending:
        if f.due_at < earliest:
            f.due_at = earliest
            f.announced_at = None
            f.send_after = None
        earliest = f.due_at + gap


def send_followup(session: Session, fu: FollowUp) -> bool:
    """Eine Nachfass-Mail verschicken. Doppelversand ist ausgeschlossen: vorher atomar beanspruchen."""
    doc = fu.document
    account = doc.account
    business = doc.business
    if not doc.customer_email:
        fu.status = FollowUpStatus.FAILED
        fu.error = "Keine Kundenadresse"
        return False
    _prepare(fu)
    before = _claim(session, fu)
    if before is None:
        log.info("Nachfass-Mail %s wird schon verschickt oder ist erledigt, übersprungen", fu.id)
        return False
    own = (account.email or "").lower()
    cc = [(n, a) for n, a in doc.cc if a.lower() not in (own, (doc.customer_email or "").lower())]
    try:
        msg = build_message(
            from_name=account.display_name or business.name,
            from_addr=account.email,
            to_name=doc.customer_name,
            to_addr=doc.customer_email,
            subject=fu.subject,
            body=fu.body,
            in_reply_to=doc.message_id,
            references=[doc.message_id] if doc.message_id else None,
            cc=cc or None,
        )
        _attach_pdf(msg, doc)
        fu.message_id = acc.smtp_for(account).send(msg)
    except (MailAuthError, MailConnectError) as exc:
        fu.status = FollowUpStatus.FAILED
        fu.sent_at = None
        fu.error = str(exc)[:1000]
        acc.mark_error(session, account, str(exc))
        log.warning("Versand fehlgeschlagen für Dokument %s: %s", doc.id, exc)
        session.commit()
        return False
    except Exception:
        fu.status = before  # unerwarteter Fehler: Zustand zurück, nichts ging raus
        fu.sent_at = None
        session.commit()
        raise
    acc.mark_ok(account)
    _copy_to_sent(account, msg)
    fu.status = FollowUpStatus.SENT
    fu.sent_at = utcnow()
    doc.followups_sent += 1
    space_following(doc, fu, fu.sent_at)
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


def invoice_question(doc: Document) -> str:
    """„Rechnung RE-1 an Herrn Weber über 224,91 € ist seit 03.11.2026 fällig. Noch offen?“"""
    nr = doc.document_number or doc.title or doc.subject
    who = doc.customer_name or doc.customer_email
    betrag = fmt.money(doc.amount_gross, doc.currency)
    due = doc.due_date or doc.sent_at
    when = due.astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m.%Y")
    return f"{texts.term(doc)} {nr} an {who}{f' über {betrag}' if betrag else ''} ist seit {when} fällig. Noch offen?"


def review_question(doc: Document) -> str:
    """Vor dem Start verschickt bzw. Rechnung: erst fragen, nicht gleich eine Mail vorbereiten."""
    if doc.kind == DocKind.INVOICE:
        return invoice_question(doc)
    nr = doc.document_number or doc.title or doc.subject
    what = f"{texts.dies(doc, 'acc')} {texts.term(doc)} {nr}{_money(doc.amount_gross, doc.currency)}"
    when = doc.sent_at.astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m.%Y")
    who = f" an {doc.customer_name or doc.customer_email}" if (doc.customer_name or doc.customer_email) else ""
    return f"{what} hast du am {when}{who} verschickt, also vor deinem Start bei uns. Ist {texts.es(doc)} schon erledigt?"


def review_labels(doc: Document) -> tuple[str, str]:
    """(Erledigt-Knopf, Weiter-Knopf)."""
    if doc.kind == DocKind.INVOICE:
        return ("Bezahlt", "Noch offen")
    return ("Erledigt", "Bitte nachfassen")


def is_question(fu: FollowUp) -> bool:
    """Freigabe als Frage (Rechnung „Noch offen?“ bzw. Angebot von vor dem Start), ohne vorbereiteten Text."""
    return fu.document.kind == DocKind.INVOICE or fu.document.needs_review


def _question_mail(business: Business, fu: FollowUp) -> tuple[str, str]:
    doc = fu.document
    base = settings.base_url
    done, go = review_labels(doc)
    q = review_question(doc)
    if doc.kind == DocKind.INVOICE:
        go_line = f"Noch offen, Zahlungserinnerung schicken: {base}/f/{fu.approval_token}/send"
        done_line = f"Ist bezahlt, nicht erinnern:            {base}/f/{fu.approval_token}/done"
        after = "Erst wenn du „Noch offen“ klickst, schicke ich die Zahlungserinnerung in deinem Namen. Ich sehe dein Konto nicht, deshalb frage ich vor jeder Erinnerung."
        subject = f"Noch offen? {texts.term(doc)} {doc.document_number or doc.title} von {doc.customer_name or doc.customer_email}".strip()
    else:
        go_line = f"{go}: {base}/f/{fu.approval_token}/send"
        done_line = f"{done}, nicht nachfassen: {base}/f/{fu.approval_token}/done"
        after = f"Erst wenn du „{go}“ klickst, plane ich eine Nachfass-Mail. Du siehst sie vorher noch."
        subject = f"Schon erledigt? {doc.customer_name or doc.customer_email} ({texts.term(doc)} {doc.document_number or doc.title})".strip()
    body = (
        f"Hallo {business.owner_name or business.name},\n\n"
        f"{q}\n\n{go_line}\n{done_line}\n\n{after}\n\nLocadas Nachfasser"
    )
    return subject, body


def approval_reason(business: Business, doc: Document) -> str:
    if business.needs_approval:
        return "Du hast eingestellt, dass du jede Nachfass-Mail selbst freigibst."
    return f"Bei Angeboten über {fmt.money(business.threshold, doc.currency)} frage ich vorher (änderbar in den Einstellungen)."


def _approval_mail(business: Business, fu: FollowUp) -> tuple[str, str]:
    doc = fu.document
    if is_question(fu):
        return _question_mail(business, fu)
    base = settings.base_url
    ok = f"{base}/f/{fu.approval_token}/send"
    skip = f"{base}/f/{fu.approval_token}/skip"
    stop = f"{base}/f/{fu.approval_token}/stop"
    body = (
        f"Hallo {business.owner_name or business.name},\n\n"
        f"ich würde jetzt bei {doc.customer_name or doc.customer_email} nachfassen ({texts.term(doc)} {doc.document_number or doc.title}{_money(doc.amount_gross, doc.currency)}).\n\n"
        f"Das würde ich in deinem Namen schicken:\n\n"
        f"Betreff: {fu.subject}\n---\n{fu.body}\n---\n\n"
        f"Senden:        {ok}\n"
        f"Diesmal nicht: {skip}\n"
        f"Kunde hat sich schon gemeldet, nicht mehr nachfassen: {stop}\n\n"
        f"{approval_reason(business, doc)}\n\n"
        f"Locadas Nachfasser"
    )
    subject = f"Nachfassen bei {doc.customer_name or doc.customer_email}? ({texts.term(doc)} {doc.document_number or doc.title})".strip()
    return subject, body


def notify_business(session: Session, business: Business, subject: str, body: str) -> bool:
    """Nur Mail ins eigene Postfach. Neue Stellen bitte über notify.send(Notification) schicken."""
    return send_email(session, business, subject, body)


def approval_notification(business: Business, fus: list[FollowUp], subject: str, body: str) -> Notification:
    """WhatsApp zeigt die Freigaben einzeln nacheinander (Knöpfe pro Nachricht), die Mail bündelt sie."""
    return wa_msgs.approval(business, fus[0], 1, len(fus), subject, body)


def needs_approval(business: Business, doc: Document) -> bool:
    """Rechnungen nie automatisch, Vorgänge von vor dem Start erst fragen, große Angebote und „alles freigeben“ mit Text."""
    if doc.needs_review or doc.kind == DocKind.INVOICE or business.needs_approval:
        return True
    return doc.amount_gross is not None and doc.amount_gross > business.threshold


_needs_approval = needs_approval


def request_approvals(session: Session, business: Business, fus: list[FollowUp], now: datetime | None = None) -> None:
    """Eine einzige Mail mit allen anstehenden Fragen und Freigaben, statt eine pro Kunde.

    Rechnungen und Vorgänge von vor dem Start kommen als Frage („Noch offen?“ / „Schon erledigt?“), ohne vorbereiteten Text.
    Angebote, die freigegeben werden müssen, kommen mit vollem Text.
    """
    if not fus:
        return
    now = now or utcnow()
    fus = sorted(fus, key=lambda f: (not is_question(f), f.document.kind != DocKind.INVOICE))
    for fu in fus:
        if not is_question(fu):
            _prepare(fu)
        fu.status = FollowUpStatus.AWAITING_APPROVAL
        fu.requested_at = now
    base = settings.base_url
    invoices = [f for f in fus if f.document.kind == DocKind.INVOICE]
    reviews = [f for f in fus if f.document.kind != DocKind.INVOICE and f.document.needs_review]
    normal = [f for f in fus if not is_question(f)]
    if len(fus) == 1:
        subject, body = _approval_mail(business, fus[0])
    else:
        lines = [f"Hallo {business.owner_name or business.name},", ""]
        if invoices:
            lines += [
                "Bevor ich Zahlungserinnerungen schicke, frage ich dich, ob die Rechnungen noch offen sind. Dein Konto sehe ich nicht.",
                "",
            ]
            for fu in invoices:
                lines += [
                    f"• {invoice_question(fu.document)}",
                    f"  Noch offen, erinnern: {base}/f/{fu.approval_token}/send",
                    f"  Bezahlt:              {base}/f/{fu.approval_token}/done",
                    "",
                ]
        if reviews:
            lines += [
                f"{'Ein paar kurze Fragen' if len(reviews) > 1 else 'Eine kurze Frage'} zu älteren Angeboten. Ich fasse dort nur nach, wenn du es sagst:",
                "",
            ]
            for fu in reviews:
                d = fu.document
                done, go = review_labels(d)
                lines += [
                    f"• {review_question(d)}",
                    f"  {done}, nicht nachfassen: {base}/f/{fu.approval_token}/done",
                    f"  {go}: {base}/f/{fu.approval_token}/send",
                    "",
                ]
        if normal:
            lines += [
                f"{'Außerdem würde ich' if (invoices or reviews) else 'Ich würde'} bei {f'{len(normal)} Kunden' if len(normal) > 1 else 'einem Kunden'} nachfassen. Das ist der Text, ein Klick pro Kunde reicht:",
                "",
            ]
            for fu in normal:
                d = fu.document
                lines += [
                    f"• {d.customer_name or d.customer_email}: {texts.term(d)} {d.document_number or d.title}{_money(d.amount_gross, d.currency)}, gesendet {d.sent_at.strftime('%d.%m.')}",
                    f"  Betreff: {fu.subject}",
                    "  ---",
                    *[f"  {ln}" if ln else "" for ln in fu.body.splitlines()],
                    "  ---",
                    f"  Senden: {base}/f/{fu.approval_token}/send",
                    f"  Erledigt, nicht nachfassen: {base}/f/{fu.approval_token}/stop",
                    "",
                ]
            lines.append(approval_reason(business, normal[0].document))
            lines.append(f"Alles auf einer Seite: {base}/freigaben")
        lines += ["", "Locadas Nachfasser"]
        parts = []
        if invoices:
            parts.append(f"{len(invoices)} {'Rechnungen' if len(invoices) > 1 else 'Rechnung'}: noch offen?")
        if reviews:
            parts.append(f"{len(reviews)} ältere {'Angebote' if len(reviews) > 1 else 'Angebot'}")
        if normal:
            parts.append(f"{len(normal)} {'Freigaben' if len(normal) > 1 else 'Freigabe'}")
        subject = "Kurz gefragt: " + ", ".join(parts)
        body = "\n".join(lines)
    if notify(session, approval_notification(business, fus, subject, body)):
        for fu in fus:
            d = fu.document
            if d.kind == DocKind.INVOICE:
                log_event(session, d.business_id, "approval", f"Nachgefragt, ob {d.kind_label} {d.document_number or d.title} noch offen ist.", d.id)
            elif d.needs_review:
                log_event(session, d.business_id, "approval", f"Nachgefragt, ob {d.kind_label} {d.document_number or d.title} schon erledigt ist.", d.id)
            else:
                log_event(session, d.business_id, "approval", f"Freigabe angefragt: Nachfass-Mail {fu.step} für {d.kind_label} {d.document_number or d.title}.", d.id)


def request_approval(session: Session, fu: FollowUp) -> None:
    request_approvals(session, fu.document.business, [fu])


def _money(v, currency: str | None = "EUR") -> str:
    return fmt.over(v, currency)


def _sent_today(session: Session, business_id: int, now: datetime) -> int:
    start = now - timedelta(hours=24)
    return len(session.scalars(
        select(FollowUp.id).join(Document).where(Document.business_id == business_id, FollowUp.status == FollowUpStatus.SENT, FollowUp.sent_at >= start)
    ).all())


STUCK_AFTER = timedelta(minutes=30)


def recover_stuck_sending(session: Session, now: datetime) -> int:
    """Abgestürzt mitten im Versand: nicht noch einmal senden (könnte raus sein), sondern als Fehler zeigen."""
    stuck = session.scalars(select(FollowUp).where(FollowUp.status == FollowUpStatus.SENDING)).all()
    n = 0
    for f in stuck:
        if f.sent_at is None or f.sent_at < now - STUCK_AFTER:
            f.status = FollowUpStatus.FAILED
            f.error = "Versand wurde unterbrochen. Bitte im Gesendet-Ordner prüfen, ob die Mail raus ist."
            log_event(session, f.document.business_id, "error", f"Nachfass-Mail {f.step} für {f.document.kind_label} {f.document.document_number or f.document.title}: Versand unterbrochen, nicht wiederholt.", f.document_id)
            n += 1
    if n:
        session.commit()
    return n


def process_due(session: Session, now: datetime | None = None, business_id: int | None = None) -> dict[str, int]:
    """Alle fälligen Nachfass-Schritte: Fragen/Freigaben bündeln, Vorschau schicken oder senden, mit Tageslimit pro Betrieb."""
    now = now or utcnow()
    stats = {"sent": 0, "approval": 0, "skipped": 0, "capped": 0}
    recover_stuck_sending(session, now)
    if not in_send_window(now):
        return stats
    q = select(FollowUp).where(FollowUp.status == FollowUpStatus.SCHEDULED, FollowUp.due_at <= now)
    if business_id is not None:
        q = q.join(Document).where(Document.business_id == business_id)
    due = session.scalars(q.order_by(FollowUp.due_at)).all()
    approvals: dict[int, list[FollowUp]] = {}
    previews: dict[int, list[FollowUp]] = {}
    sent_count: dict[int, int] = {}
    for fu in due:
        if fu.status != FollowUpStatus.SCHEDULED:
            continue  # inzwischen woanders verschickt
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
        if any(f.status in (FollowUpStatus.AWAITING_APPROVAL, FollowUpStatus.SENDING) for f in doc.followups):
            continue
        if any(f.document_id == doc.id for f in approvals.get(business.id, [])):
            continue
        if needs_approval(business, doc):
            approvals.setdefault(business.id, []).append(fu)
            continue
        if settings.preview_hours > 0:
            if fu.announced_at is None:
                previews.setdefault(business.id, []).append(fu)
                continue
            send_after = fu.send_after or next_send_time(fu.announced_at + timedelta(hours=settings.preview_hours))
            if now < send_after:
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
    for business_id_, fus in approvals.items():
        request_approvals(session, fus[0].document.business, fus, now)
        stats["approval"] += len(fus)
        session.commit()
    for business_id_, fus in previews.items():
        send_preview(session, fus[0].document.business, fus, now)
        stats["announced"] = stats.get("announced", 0) + len(fus)
        session.commit()
    return stats


def _preview_line_extra(d: Document) -> str:
    if d.owner_contact_at:
        return f" · du hast am {d.owner_contact_at.astimezone(ZoneInfo(settings.timezone)).strftime('%d.%m.')} selbst geschrieben"
    return ""


def send_preview(session: Session, business: Business, fus: list[FollowUp], now: datetime) -> None:
    """Morgen-Vorschau: wer wann eine Nachfass-Mail bekommt, mit Stopp-Link pro Kunde (für Anrufe, die wir nicht sehen).

    Die genannte Zeit ist die echte Versandzeit: Schließt das Versandfenster vorher, heißt es „morgen ab 8 Uhr“ und
    genau dann wird gesendet, ohne neue Vorschau.
    """
    base = settings.base_url
    send_at = next_send_time(now + timedelta(hours=settings.preview_hours))
    when = when_phrase(send_at, now)
    lines = [
        f"Hallo {business.owner_name or business.name},",
        "",
        f"{when} fasse ich bei {'diesem Kunden' if len(fus) == 1 else f'diesen {len(fus)} Kunden'} nach.",
        "Hat sich jemand schon gemeldet, zum Beispiel am Telefon? Dann einfach auf Stopp klicken.",
        "",
    ]
    for fu in fus:
        d = fu.document
        lines += [
            f"• {d.customer_name or d.customer_email}: {texts.term(d)} {d.document_number or d.title}{_money(d.amount_gross, d.currency)} (Nachfass-Mail {fu.step} von 3){_preview_line_extra(d)}",
            f"  Stopp, hat sich gemeldet: {base}/f/{fu.approval_token}/stop",
            "",
        ]
    lines += ["Du musst sonst nichts tun.", "", "Locadas Nachfasser"]
    lines[2] = lines[2][0].upper() + lines[2][1:]
    names = ", ".join((f.document.customer_name or f.document.customer_email).split(" <")[0] for f in fus[:3]) + (" …" if len(fus) > 3 else "")
    subject = f"{'Heute' if when.startswith('heute') else 'Morgen' if when.startswith('morgen') else 'Demnächst'} nachfassen: {names}"
    if notify(session, wa_msgs.preview(business, fus, when, subject, "\n".join(lines)), now):
        for fu in fus:
            fu.announced_at = now
            fu.send_after = send_at
            fu.requested_at = now
    else:
        # Ohne Vorschau keine Sendung: lieber einen Tag später als ungefragt
        for fu in fus:
            fu.announced_at = None
            fu.send_after = None


# --- Freigabe per Link ---------------------------------------------------------

LINK_MAX_AGE = timedelta(days=60)


def find_by_token(session: Session, token: str) -> FollowUp | None:
    if not token:
        return None
    return session.scalar(select(FollowUp).where(FollowUp.approval_token == token))


def link_expired(fu: FollowUp, now: datetime | None = None) -> bool:
    """Links in Freigabe- und Vorschau-Mails gelten 60 Tage ab der Mail."""
    now = now or utcnow()
    base = fu.requested_at or fu.announced_at or fu.due_at
    return base is not None and now > base + LINK_MAX_AGE


def outcome_link_expired(doc: Document, now: datetime | None = None) -> bool:
    now = now or utcnow()
    base = doc.outcome_asked_at or doc.closed_at or doc.created_at
    return base is not None and now > base + LINK_MAX_AGE


def release_review(session: Session, fu: FollowUp, now: datetime | None = None) -> bool:
    """„Bitte nachfassen“ bei einem Vorgang von vor dem Start: ab jetzt normaler Ablauf (Freigabe bzw. Morgen-Vorschau)."""
    if fu.status != FollowUpStatus.AWAITING_APPROVAL or not fu.document.needs_review:
        return False
    now = now or utcnow()
    doc = fu.document
    doc.needs_review = False
    fu.status = FollowUpStatus.SCHEDULED
    fu.subject, fu.body = "", ""
    fu.due_at = min(fu.due_at, now)
    fu.announced_at = None
    fu.send_after = None
    remaining = [f.due_at for f in doc.followups if f.status == FollowUpStatus.SCHEDULED]
    doc.next_followup_at = min(remaining) if remaining else None
    log_event(session, doc.business_id, "review", f"{doc.kind_label} {doc.document_number or doc.title}: Du willst nachfassen lassen. Die Mail kommt im normalen Ablauf.", doc.id)
    return True


def settle(session: Session, doc: Document) -> str:
    """„Bezahlt“ / „Erledigt“ auf die Frage zu einem Vorgang von vor dem Start."""
    if doc.status != DocStatus.OPEN:
        return ""
    if doc.kind == DocKind.INVOICE:
        close_document(session, doc, DocStatus.PAID, "Vom Betrieb als bezahlt markiert")
        return "Bezahlt"
    close_document(session, doc, DocStatus.DONE, "Vom Betrieb als erledigt markiert")
    return "Erledigt"


OPEN_PUSH_DAYS = 3


def on_first_open(session: Session, doc: Document, now: datetime | None = None) -> None:
    """Kunde hat das Angebot gerade zum ersten Mal geöffnet: ihm Zeit lassen, geplante Schritte rücken 3 Tage nach hinten."""
    if doc.kind != DocKind.OFFER or doc.status != DocStatus.OPEN:
        return
    now = now or utcnow()
    pending = [f for f in doc.followups if f.status == FollowUpStatus.SCHEDULED]
    if not pending:
        return
    for f in pending:
        f.due_at = max(f.due_at, now) + timedelta(days=OPEN_PUSH_DAYS)
        f.announced_at = None
        f.subject, f.body = "", ""
    doc.next_followup_at = min(f.due_at for f in pending)


def approve(session: Session, fu: FollowUp) -> bool:
    """„Senden“ bzw. bei Rechnungen „Noch offen“. Bei älteren Angeboten heißt es: bitte nachfassen (normaler Ablauf)."""
    if fu.status != FollowUpStatus.AWAITING_APPROVAL:
        return False
    doc = fu.document
    if doc.needs_review and doc.kind != DocKind.INVOICE:
        return release_review(session, fu)
    if doc.kind == DocKind.INVOICE:
        # Betrieb hat bestätigt: noch offen. Text erst jetzt schreiben, damit die Frist ab heute gilt.
        doc.needs_review = False
        fu.subject, fu.body = "", ""
        log_event(session, doc.business_id, "confirmed", f"Du hast bestätigt: {doc.kind_label} {doc.document_number or doc.title} ist noch offen.", doc.id)
    ok = send_followup(session, fu)
    if ok:
        doc.business.approvals_done += 1
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
        head = f"• {d.customer_name or d.customer_email}: {texts.term(d)} {d.document_number or d.title}{_money(d.amount_gross, d.currency)}"
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
    doc.outcome_at = utcnow()
    doc.closed_at = doc.closed_at or utcnow()
    doc.close_reason = label
    log_event(session, doc.business_id, "outcome", f"{doc.kind_label} {doc.document_number or doc.title}: {label}.", doc.id)
    return label
