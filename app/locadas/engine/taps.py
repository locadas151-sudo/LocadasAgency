"""WhatsApp-Nachfassen mit einem Tipp.

Viele Kunden schreiben mit dem Betrieb per WhatsApp, nicht per Mail. Von seiner Nummer können wir (noch) nicht
selbst senden. Deshalb bekommt der Betrieb die fertige Nachricht: ein Tipp öffnet seinen WhatsApp-Chat mit dem
Kunden (wa.me-Link mit vorausgefülltem Text), er drückt auf Senden. Kostenlos, ohne Meta-Freigabe, von seiner Nummer.

Kanäle (Business.followup_channel, je Dokument überschreibbar):
- "mail": wie bisher.
- "whatsapp_tap": WhatsApp mit einem Tipp, wenn eine Handynummer des Kunden bekannt ist, sonst Mail.
- "both": Mail automatisch wie bisher, dazu ein WhatsApp-Vorschlag.

Ablauf whatsapp_tap: fällige Stufe → Text (≤ 400 Zeichen) → FollowUp AWAITING_TAP → Betrieb bekommt die Nachricht
(WhatsApp „In WhatsApp öffnen“ + Knöpfe, sonst Mail, immer im Dashboard). „Gesendet“ → SENT (sent_via whatsapp_tap),
nächste Stufe wie gewohnt. Keine Bestätigung nach 24 h: Link geöffnet → als gesendet werten, sonst einmal erinnern.
Nach 72 h ohne alles: übersprungen. Per Mail geht bei whatsapp_tap nie automatisch etwas raus.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, time, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import fmt
from ..config import settings
from ..db import Business, Customer, DocKind, DocStatus, Document, FollowUp, FollowUpStatus, log_event, utcnow
from ..extract import phones
from . import wa_texts

log = logging.getLogger(__name__)

CHANNELS = ("mail", "whatsapp_tap", "both")
CHANNEL_LABEL = {
    "mail": "Per Mail",
    "whatsapp_tap": "Per WhatsApp mit einem Tipp, sonst Mail",
    "both": "Mail automatisch plus WhatsApp-Vorschlag",
}
MAX_CTA_PER_DAY = 5               # so viele „In WhatsApp öffnen“-Nachrichten pro Tag, der Rest steht im Dashboard
CONFIRM_AFTER = timedelta(hours=24)
EXPIRE_AFTER = timedelta(hours=72)
ACTIONS = ("sent", "replied", "paid", "skip")
# Tipp-Arten (FollowUp.tap_kind): "" / "wa" = wa.me, "mailto" = fertige Mail im eigenen Mailprogramm, "manual" = Text kopieren
TAP_SENT_VIA = {"mailto": "mail_tap", "manual": "manual_tap"}


def tap_noun(fu: FollowUp) -> str:
    return {"mailto": "Nachfass-Mail", "manual": "Nachfass-Nachricht"}.get(fu.tap_kind or "", "WhatsApp-Nachricht")


def open_label(fu: FollowUp) -> str:
    """Beschriftung des Tipp-Links (Knopf höchstens 20 Zeichen)."""
    return {"mailto": "Mail öffnen", "manual": "Text kopieren"}.get(fu.tap_kind or "", "In WhatsApp öffnen")


# --- Kanal --------------------------------------------------------------------------------

def configured_channel(doc: Document) -> str:
    ch = (doc.followup_channel or "").strip() or (getattr(doc.business, "followup_channel", "") or "mail")
    return ch if ch in CHANNELS else "mail"


def channel_for(doc: Document) -> str:
    """Wirksamer Kanal: WhatsApp nur mit bekannter Handynummer des Kunden."""
    ch = configured_channel(doc)
    if ch != "mail" and not doc.customer_mobile:
        return "mail"
    return ch


def wa_link(number: str, text: str) -> str:
    """https://wa.me/<E.164 ohne +>?text=<urlencodiert> (UTF-8, Zeilenumbrüche als %0A)."""
    digits = re.sub(r"\D", "", number or "")
    return f"https://wa.me/{digits}?text={quote(text or '', safe='')}"


def tap_url(fu: FollowUp) -> str:
    return f"{settings.base_url}/t/{fu.tap_token}"


def action_url(fu: FollowUp, action: str) -> str:
    return f"{settings.base_url}/w/{fu.tap_token}/{action}"


def find(session: Session, token: str) -> FollowUp | None:
    if not token or len(token) > 64:
        return None
    return session.scalar(select(FollowUp).where(FollowUp.tap_token == token))


# --- Telefonnummern am Dokument und Kunden --------------------------------------------------

def set_phones(session: Session, doc: Document, phone: str = "", mobile: str = "", overwrite: bool = False) -> None:
    """Nummern am Dokument und am Kunden speichern. Ohne overwrite nur leere Felder füllen."""
    if mobile and not phones.is_mobile(mobile):
        phone, mobile = phone or mobile, ""
    if phone and (overwrite or not doc.customer_phone):
        doc.customer_phone = phone
    if mobile and (overwrite or not doc.customer_mobile):
        doc.customer_mobile = mobile
    c = _customer(session, doc)
    if c is None:
        return
    if doc.customer_phone and (overwrite or not c.phone):
        c.phone = doc.customer_phone
    if doc.customer_mobile and (overwrite or not c.mobile):
        c.mobile = doc.customer_mobile
    # Umgekehrt: Kunde hat schon eine Nummer (frühere Dokumente, Mail-Signatur)
    if not doc.customer_mobile and c.mobile:
        doc.customer_mobile = c.mobile
    if not doc.customer_phone and c.phone:
        doc.customer_phone = c.phone


def _customer(session: Session, doc: Document) -> Customer | None:
    from . import customers

    return customers.for_document(session, doc)


def own_numbers(business: Business, *texts: str, cc: str = "49") -> set[str]:
    return phones.own_numbers(business.phone or "", business.whatsapp_number or "", business.signature or "", *texts, cc=cc)


def learn_from_reply(session: Session, doc: Document, text: str) -> bool:
    """Signatur einer Kundenantwort: Nummer übernehmen, wenn noch keine da ist."""
    if not text or (doc.customer_mobile and doc.customer_phone):
        return False
    cc = phones.country_code(text, doc.currency)
    found = phones.from_reply(text, own_numbers(doc.business, cc=cc), cc)
    if not (found.phone or found.mobile):
        return False
    had = doc.customer_mobile
    set_phones(session, doc, found.phone, found.mobile)
    if doc.customer_mobile and not had:
        log_event(session, doc.business_id, "phone", f"Handynummer von {doc.customer_name or doc.customer_email} aus der Mail-Signatur übernommen: {phones.pretty(doc.customer_mobile)}.", doc.id)
    return True


# --- Start, Vorschlag ------------------------------------------------------------------------

def _ref(doc: Document) -> str:
    return f"{doc.kind_label} {doc.document_number or doc.title}"


def start_tap(session: Session, fu: FollowUp, now: datetime | None = None) -> None:
    """Fällige Stufe als WhatsApp-Nachricht vorbereiten (Kanal whatsapp_tap). Geht nicht per Mail raus."""
    now = now or utcnow()
    doc = fu.document
    fu.wa_text = wa_texts.text_for(doc, fu.step, now)
    fu.tap_token = fu.tap_token or secrets.token_urlsafe(24)
    fu.status = FollowUpStatus.AWAITING_TAP
    fu.tap_state = "open"
    fu.requested_at = now
    fu.tap_notified_at = fu.tapped_at = fu.tap_reminded_at = None
    fu.tap_notice = ""
    log_event(session, doc.business_id, "tap", f"WhatsApp-Nachricht {fu.step} für {_ref(doc)} an {doc.customer_name or phones.pretty(doc.customer_mobile)} liegt bereit, du schickst sie mit einem Tipp.", doc.id)


def suggest(session: Session, fu: FollowUp, now: datetime | None = None) -> None:
    """Kanal both: Mail ist raus, zusätzlich derselbe Inhalt kurz für WhatsApp."""
    now = now or utcnow()
    doc = fu.document
    fu.wa_text = wa_texts.text_for(doc, fu.step, now)
    fu.tap_token = fu.tap_token or secrets.token_urlsafe(24)
    fu.tap_state = "open"
    fu.requested_at = now
    fu.tap_notified_at = fu.tapped_at = fu.tap_reminded_at = None
    fu.tap_notice = ""


# --- Aktionen des Betriebs ---------------------------------------------------------------------

def _recompute_next(doc: Document) -> None:
    remaining = [f.due_at for f in doc.followups if f.status == FollowUpStatus.SCHEDULED]
    doc.next_followup_at = min(remaining) if remaining else None


def confirm_sent(session: Session, fu: FollowUp, now: datetime | None = None, assumed: bool = False) -> bool:
    if fu.tap_state != "open":
        return False
    from .followups import space_following

    now = now or utcnow()
    doc = fu.document
    who = doc.customer_name or doc.customer_email
    if fu.status == FollowUpStatus.AWAITING_TAP:
        fu.status = FollowUpStatus.SENT
        fu.sent_at = (fu.tapped_at if assumed and fu.tapped_at else now)
        fu.sent_via = TAP_SENT_VIA.get(fu.tap_kind or "", "whatsapp_tap")
        fu.subject = fu.subject or "WhatsApp"
        fu.body = fu.body or fu.wa_text
        doc.followups_sent += 1
        doc.needs_review = False  # der Betrieb hat selbst geschickt, also ist der Vorgang noch offen
        space_following(doc, fu, fu.sent_at)
        _recompute_next(doc)
    else:
        fu.sent_via = "mail+whatsapp_tap"
    fu.tap_state = "sent"
    how = "Du hast den Link geöffnet, ich werte sie als gesendet" if assumed else "Du hast bestätigt"
    log_event(session, doc.business_id, "sent", f"{tap_noun(fu)} {fu.step} für {_ref(doc)} an {who}: {how}.", doc.id)
    return True


def mark_replied(session: Session, fu: FollowUp, paid: bool = False, now: datetime | None = None) -> str:
    from .followups import close_document

    doc = fu.document
    if fu.tap_state == "open":
        fu.tap_state = "replied"
    if doc.status != DocStatus.OPEN:
        return ""
    if paid and doc.kind == DocKind.INVOICE:
        close_document(session, doc, DocStatus.PAID, "Vom Betrieb als bezahlt markiert")
        return "Bezahlt"
    doc.last_customer_reply_at = now or utcnow()
    close_document(session, doc, DocStatus.REPLIED, "Kunde hat per WhatsApp geantwortet")
    return "Hat geantwortet"


def skip(session: Session, fu: FollowUp) -> bool:
    if fu.tap_state != "open":
        return False
    doc = fu.document
    fu.tap_state = "skipped"
    if fu.status == FollowUpStatus.AWAITING_TAP:
        fu.status = FollowUpStatus.SKIPPED
        _recompute_next(doc)
        log_event(session, doc.business_id, "skipped", f"{tap_noun(fu)} {fu.step} für {_ref(doc)} diesmal nicht geschickt. Die nächste Stufe bleibt geplant.", doc.id)
    return True


def state_text(fu: FollowUp) -> tuple[str, str] | None:
    """Schon erledigt? (Titel, Text), sonst None."""
    doc = fu.document
    who = doc.customer_name or doc.customer_email
    if fu.tap_state == "sent":
        return "Schon notiert", f"Die {tap_noun(fu)} an {fmt.dativ(who)} ist als gesendet vermerkt."
    if fu.tap_state in ("replied",) or (doc.status != DocStatus.OPEN and fu.tap_state != "open"):
        return "Schon erledigt", f"Bei {who} fasse ich nicht mehr nach."
    if fu.tap_state in ("skipped", "expired", "cancelled", ""):
        return "Nicht mehr offen", f"Diese {tap_noun(fu)} ist nicht mehr offen."
    return None


def handle(session: Session, fu: FollowUp, action: str, now: datetime | None = None) -> tuple[str, str]:
    """Eine Aktion aus WhatsApp-Knopf, Mail-Link oder Dashboard. Idempotent. (Titel, Text)."""
    now = now or utcnow()
    doc = fu.document
    who = doc.customer_name or doc.customer_email
    if action in ("replied", "paid"):
        if doc.status != DocStatus.OPEN:
            return "Schon erledigt", f"Bei {who} fasse ich nicht mehr nach."
        label = mark_replied(session, fu, paid=(action == "paid"), now=now)
        return "Notiert", f"{who}: {label}. Ich fasse dort nicht mehr nach."
    state = state_text(fu)
    if state:
        return state
    if action == "sent":
        confirm_sent(session, fu, now)
        nxt = doc.next_followup_at
        tail = f" Die nächste Stufe ist am {nxt.astimezone(ZoneInfo(settings.timezone)).strftime('%d.%m.')} dran." if nxt and doc.status == DocStatus.OPEN else ""
        return "Notiert", f"Die {tap_noun(fu)} an {fmt.dativ(who)} ist als gesendet vermerkt.{tail}"
    if action == "skip":
        skip(session, fu)
        return "Übersprungen", "Diese Nachricht wird nicht geschickt. Die nächste Stufe bleibt geplant."
    return "Unbekannt", "Diese Aktion kenne ich nicht."


def record_tap(fu: FollowUp, now: datetime | None = None) -> None:
    if fu.tapped_at is None:
        fu.tapped_at = now or utcnow()


# --- Offene Taps, Erinnern, Benachrichtigen -------------------------------------------------------

def open_taps(session: Session, business_id: int) -> list[FollowUp]:
    return list(session.scalars(
        select(FollowUp).join(Document).where(Document.business_id == business_id, FollowUp.tap_state == "open", Document.status == DocStatus.OPEN)
        .order_by(FollowUp.requested_at, FollowUp.id)
    ).all())


def _day_start(now: datetime) -> datetime:
    tz = ZoneInfo(settings.timezone)
    local = now.astimezone(tz)
    return datetime.combine(local.date(), time(0, 0), tzinfo=tz).astimezone(timezone.utc)


def cta_sent_today(session: Session, business_id: int, now: datetime) -> int:
    start = _day_start(now)
    rows = session.scalars(
        select(FollowUp).join(Document).where(
            Document.business_id == business_id, FollowUp.tap_notice == "whatsapp",
            or_(FollowUp.tap_notified_at >= start, FollowUp.tap_reminded_at >= start),
        )
    ).all()
    n = 0
    for f in rows:
        n += int(f.tap_notified_at is not None and f.tap_notified_at >= start)
        n += int(f.tap_reminded_at is not None and f.tap_reminded_at >= start)
    return n


def housekeeping(session: Session, now: datetime, business_id: int | None = None) -> dict[int, list[FollowUp]]:
    """Offene Taps älter als 24 h: Link geöffnet → gesendet; sonst einmal erinnern; nach 72 h übersprungen.

    Gibt die zu erinnernden Taps je Betrieb zurück."""
    q = select(FollowUp).join(Document).where(FollowUp.tap_state == "open", FollowUp.requested_at <= now - CONFIRM_AFTER)
    if business_id is not None:
        q = q.where(Document.business_id == business_id)
    reminders: dict[int, list[FollowUp]] = {}
    for fu in session.scalars(q).all():
        doc = fu.document
        if doc.status != DocStatus.OPEN:
            fu.tap_state = "cancelled"
            continue
        if fu.tapped_at is not None:
            confirm_sent(session, fu, now, assumed=True)
        elif fu.requested_at <= now - EXPIRE_AFTER:
            fu.tap_state = "expired"
            if fu.status == FollowUpStatus.AWAITING_TAP:
                fu.status = FollowUpStatus.SKIPPED
                fu.error = "Nicht per WhatsApp verschickt (keine Bestätigung)"
                _recompute_next(doc)
            log_event(session, doc.business_id, "skipped", f"{tap_noun(fu)} {fu.step} für {_ref(doc)} wurde nicht bestätigt, ich lasse sie aus.", doc.id)
        elif fu.tap_reminded_at is None and fu.tap_notified_at is not None:
            reminders.setdefault(doc.business_id, []).append(fu)
    return reminders


def notify_pending(session: Session, now: datetime, business_id: int | None = None) -> int:
    """Neue Taps und Erinnerungen gebündelt an den Betrieb. Gibt die Zahl der gemeldeten Taps zurück."""
    reminders = housekeeping(session, now, business_id)
    q = select(FollowUp).join(Document).where(FollowUp.tap_state == "open", FollowUp.tap_notified_at.is_(None), Document.status == DocStatus.OPEN)
    if business_id is not None:
        q = q.where(Document.business_id == business_id)
    new: dict[int, list[FollowUp]] = {}
    for fu in session.scalars(q.order_by(FollowUp.requested_at, FollowUp.id)).all():
        new.setdefault(fu.document.business_id, []).append(fu)
    total = 0
    for bid in set(new) | set(reminders):
        b = session.get(Business, bid)
        if b is None or b.paused:
            continue
        total += send_bundle(session, b, new.get(bid, []), reminders.get(bid, []), now)
        session.commit()
    return total


def send_bundle(session: Session, b: Business, new: list[FollowUp], reminders: list[FollowUp], now: datetime) -> int:
    """WhatsApp: je Tap eine „In WhatsApp öffnen“-Nachricht plus Knöpfe, höchstens 5 am Tag, der Rest im Dashboard.
    Sonst bzw. bei Fehler eine einzige Mail mit allen Links."""
    from ..notify import core as ncore
    from ..notify import messages as m

    items = [(f, False) for f in new] + [(f, True) for f in reminders]
    if not items:
        return 0
    failed: list[tuple[FollowUp, bool]] = []
    if ncore.whatsapp_ready(b):
        quota = max(0, MAX_CTA_PER_DAY - cta_sent_today(session, b.id, now))
        for fu, reminder in items[:quota]:
            n = m.tap(b, fu, reminder=reminder)
            if ncore.send(session, n, now) and n.channel == "whatsapp":
                _mark(fu, reminder, "whatsapp", now)
            else:
                failed.append((fu, reminder))
        rest = items[quota:]
        if rest:
            n = m.tap_overflow(b, [f for f, _ in rest])
            if ncore.send(session, n, now) and n.channel == "whatsapp":
                for fu, reminder in rest:
                    _mark(fu, reminder, "dashboard", now)
            else:
                failed += rest
    else:
        failed = items
    if failed:
        subject, body = bundle_mail(b, [f for f, r in failed if not r], [f for f, r in failed if r])
        if ncore.send_email(session, b, subject, body):
            for fu, reminder in failed:
                _mark(fu, reminder, "email", now)
    return sum(1 for f, _ in items if f.tap_notified_at is not None)


def _mark(fu: FollowUp, reminder: bool, how: str, now: datetime) -> None:
    if reminder:
        fu.tap_reminded_at = now
    else:
        fu.tap_notified_at = now
    fu.tap_notice = how


def _prev_wa(fu: FollowUp) -> FollowUp | None:
    prev = [f for f in fu.document.followups if f.step < fu.step and "whatsapp" in (f.sent_via or "")]
    return prev[-1] if prev else None


def bundle_mail(b: Business, new: list[FollowUp], reminders: list[FollowUp]) -> tuple[str, str]:
    from .. import fmt

    base = settings.base_url
    lines = [f"Hallo {b.owner_name or b.name},", ""]

    def block(fu: FollowUp) -> list[str]:
        d = fu.document
        who = d.customer_name or d.customer_email
        out = [f"• {who}: {d.term} {d.document_number or d.title}{fmt.over(d.amount_gross, d.currency)} (Nachricht {fu.step} von 3)"]
        if fu.status != FollowUpStatus.AWAITING_TAP:
            out.append("  Die Mail ist schon raus. Wenn du zusätzlich per WhatsApp nachhaken willst:")
        prev = _prev_wa(fu)
        if prev is not None and prev.sent_at:
            out.append(f"  Die letzte Nachricht ging am {prev.sent_at.astimezone(ZoneInfo(settings.timezone)).strftime('%d.%m.')} per WhatsApp raus. Hat {who} dort geantwortet? Dann unten auf „Kunde hat schon geantwortet“.")
        if fu.tap_kind == "manual":
            out.append(f"  Mir fehlt die Handynummer oder Mailadresse. Trag sie hier ein oder kopier den Text: {tap_url(fu)}")
        out += ["  ---", *[f"  {ln}" if ln else "" for ln in (fu.wa_text or "").splitlines()], "  ---",
                f"  {(open_label(fu) + ':').ljust(28)} {tap_url(fu)}",
                f"  Abgeschickt:                 {action_url(fu, 'sent')}"]
        if d.kind == DocKind.INVOICE:
            out.append(f"  Ist schon bezahlt:           {action_url(fu, 'paid')}")
        out += [f"  Kunde hat schon geantwortet: {action_url(fu, 'replied')}",
                f"  Diesmal nicht:               {action_url(fu, 'skip')}", ""]
        return out

    if new:
        lines += [
            (f"heute {'ist bei diesem Kunden' if len(new) == 1 else f'sind bei diesen {len(new)} Kunden'} die nächste Nachfass-Nachricht per WhatsApp dran. "
             "Der Link öffnet deinen WhatsApp-Chat mit dem Kunden, der Text steht schon drin. Abschicken und kurz auf „Abgeschickt“ klicken.")
            if not any(f.tap_kind for f in new) else
            (f"heute {'ist bei diesem Kunden' if len(new) == 1 else f'sind bei diesen {len(new)} Kunden'} die nächste Nachfass-Nachricht dran. "
             "Der Link öffnet die fertige Nachricht in WhatsApp oder deinem Mailprogramm. Abschicken und kurz auf „Abgeschickt“ klicken."),
            "",
        ]
        lines[2] = lines[2][0].upper() + lines[2][1:]
        for fu in new:
            lines += block(fu)
    if reminders:
        lines += ["Noch offen von gestern, falls du nicht dazu gekommen bist:", ""]
        for fu in reminders:
            lines += block(fu)
    lines += [f"Alles auf einer Seite, auch fürs Handy: {base}/dashboard#whatsapp", ""]
    if any(f.status == FollowUpStatus.AWAITING_TAP and not f.tap_kind for f in new + reminders):
        lines += ["Per Mail schicke ich diesen Kunden nichts, das machst du per WhatsApp.", ""]
    elif any(f.tap_kind for f in new + reminders):
        lines += ["Ich schicke selbst nichts an deine Kunden, das machst du mit einem Tipp von deinem Handy oder Mailprogramm.", ""]
    lines.append("Locadas Nachfasser")
    names = ", ".join((f.document.customer_name or f.document.customer_email) for f in (new or reminders)[:3])
    more = " …" if len(new or reminders) > 3 else ""
    subject = f"{'Heute per WhatsApp nachfassen' if new else 'Noch offen per WhatsApp'}: {names}{more}"
    return subject, "\n".join(lines)
