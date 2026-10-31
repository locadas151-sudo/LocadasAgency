"""Testen ohne Postfach („Freebie-Tester“).

Ein Handwerker meldet sich mit Name, Betrieb und Handynummer an und bekommt eine persönliche Weiterleitungsadresse
<slug>-<6 Zeichen>@INBOUND_DOMAIN. Angebote kommen per Weiterleitung, CC/BCC oder als PDF bzw. Foto an den
WhatsApp-Sekretär (engine/inbound_mail.py). Weil wir nicht in seinem Namen senden können, wird jede fällige
Nachfass-Stufe ein Tipp (wie engine/taps.py):

- Handy des Kunden bekannt: WhatsApp-Tipp (wa.me), wie bei verbundenen Betrieben.
- nur Mailadresse bekannt: mailto-Tipp, /t/{token} öffnet die fertige Mail im eigenen Mailprogramm.
- nichts bekannt: Text zum Kopieren, und der Betrieb wird nach Nummer oder Mailadresse gefragt.

Danach der Weg zum Postfach: nach dem ersten bestätigten „Gesendet“ oder dem dritten Dokument einmal
„Soll das künftig automatisch laufen?“ mit Termin (Calendly) oder „Selbst verbinden“ (/verbinden?b=<Token>).
Grenzen: höchstens TRIAL_MAX_DOCS Dokumente je TRIAL_WINDOW_DAYS, nach TRIAL_DELETE_AFTER_DAYS ohne Aktivität gelöscht.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import fmt
from ..config import settings
from ..db import Business, DocKind, DocStatus, Document, FollowUp, FollowUpStatus, MailAccount, log_event, new_session, utcnow
from ..extract import phones
from ..notify import Notification
from ..notify import send as notify
from ..notify.tokens import button, payload

log = logging.getLogger(__name__)

MAILTO_BODY_MAX = 1800
UPGRADE_AFTER_DOCS = 3
_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # ohne 0/o, 1/l/i


# --- Anmelden ---------------------------------------------------------------------------------

def slugify(name: str, limit: int = 20) -> str:
    s = (name or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"\b(gmbh|ug|gbr|ohg|kg|ek|e\.k\.|inh\.?|haftungsbeschraenkt)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = s[:limit].strip("-")
    return s or "betrieb"


def new_inbound_address(session: Session, name: str) -> str:
    base = slugify(name)
    for _ in range(20):
        rnd = "".join(secrets.choice(_ALPHABET) for _ in range(6))
        addr = f"{base}-{rnd}@{settings.inbound_domain}"
        if session.scalar(select(Business.id).where(Business.inbound_address == addr)) is None:
            return addr
    raise RuntimeError("Keine freie Weiterleitungsadresse gefunden")


def placeholder_email(inbound_address: str) -> str:
    """Business.email ist eindeutig und Pflicht. Ohne echte Adresse ein Platzhalter, der nie zugestellt wird."""
    return f"{inbound_address.split('@')[0]}@trial.invalid"


def create(session: Session, owner_name: str, business_name: str, whatsapp: str, email: str = "") -> Business:
    """Tester anlegen. whatsapp ist schon normalisiert (E.164 ohne +), email schon geprüft und frei."""
    b = Business(name=business_name.strip()[:200], owner_name=owner_name.strip()[:200], mode="trial",
                 followup_channel="mail", whatsapp_number=whatsapp, whatsapp_opt_in=bool(whatsapp), email="pending", signup_source="testen")
    b.inbound_address = new_inbound_address(session, business_name or owner_name)
    b.email = (email or "").strip().lower()[:320] or placeholder_email(b.inbound_address)
    b.trial_active_at = utcnow()
    session.add(b)
    session.flush()
    log_event(session, b.id, "trial", f"Testzugang angelegt. Weiterleitungsadresse {b.inbound_address}.")
    return b


def touch(b: Business, now: datetime | None = None) -> None:
    b.trial_active_at = now or utcnow()


# --- Grenzen ----------------------------------------------------------------------------------

def docs_in_window(session: Session, b: Business, now: datetime | None = None) -> int:
    now = now or utcnow()
    since = now - timedelta(days=settings.trial_window_days)
    return session.scalar(select(func.count(Document.id)).where(Document.business_id == b.id, Document.created_at >= since)) or 0


def at_limit(session: Session, b: Business, now: datetime | None = None) -> bool:
    return b.is_trial and docs_in_window(session, b, now) >= settings.trial_max_docs


def limit_notice(session: Session, b: Business, now: datetime | None = None) -> bool:
    """Limit erreicht: höchstens alle 7 Tage einmal Bescheid geben, mit dem Weg zum Postfach."""
    now = now or utcnow()
    if b.trial_limit_at is not None and now - b.trial_limit_at < timedelta(days=7):
        return False
    text = (f"Du hast in den letzten {settings.trial_window_days} Tagen {settings.trial_max_docs} Dokumente geschickt, mehr geht im Test nicht. "
            f"Mit verbundenem Postfach läuft es ohne Grenze und ohne Weiterleiten.")
    n = _upgrade_notification(b, "Testgrenze erreicht", text)
    if notify(session, n, now):
        b.trial_limit_at = now
        return True
    return False


# --- Nach dem Erkennen: Aha-Moment, Hinweis bei neuen Dokumenten ---------------------------------

def _day(dt: datetime) -> str:
    """Tag, an dem der Tipp wirklich kommt: Tipps eines Werktags kommen gebündelt am Morgen, am Wochenende/Feiertag nie."""
    from ..holidays import add_workdays, is_workday

    day = dt.astimezone(ZoneInfo(settings.timezone)).date()
    if not is_workday(day):
        day = add_workdays(day, 1)
    return f"{_WEEKDAYS[day.weekday()]}, {day.strftime('%d.%m.')}"


def _who(d: Document) -> str:
    return (d.customer_name or d.customer_email or "").split(" <")[0]


def _ref(d: Document) -> str:
    return f"{d.term} {d.document_number}".strip() if d.document_number else d.term


def recognized_line(d: Document) -> str:
    """„Erkannt: Angebot A-123 an Familie Brandt über 3.724,70 €.“"""
    who = _who(d)
    return f"Erkannt: {_ref(d)}{f' an {who}' if who else ''}{fmt.over(d.amount_gross, d.currency)}."


def aha_text(d: Document, first_tap_now: bool, first: bool = True) -> str:
    lines = [recognized_line(d)]
    if first_tap_now:
        lines.append("Das ist schon ein paar Tage her, deshalb kommt die erste Nachfass-Nachricht gleich fertig zu dir.")
    elif d.next_followup_at:
        tail = " Du musst nichts tun." if first else ""
        lines.append(f"Ich melde mich am {_day(d.next_followup_at)} mit der fertigen Nachfass-Nachricht.{tail}")
    if not (d.customer_mobile or d.customer_email):
        lines.append(f"Mir fehlt noch die Handynummer oder Mailadresse{f' von {_who(d)}' if _who(d) else ''}. Schick sie mir einfach hier.")
    return "\n".join(lines)


def after_ingest(session: Session, d: Document, first_tap_now: bool, now: datetime | None = None) -> None:
    """Nach jedem neuen Dokument eines Testers: Bestätigung (beim ersten Mal der Aha-Moment), ggf. erster Tipp, Upgrade."""
    now = now or utcnow()
    b = d.business
    touch(b, now)
    first = b.trial_aha_at is None
    text = aha_text(d, first_tap_now, first)
    title = "Angebot erkannt" if d.kind == DocKind.OFFER else "Rechnung erkannt"
    mail_body = f"Hallo {b.owner_name or b.name},\n\n{text}\n\nAlles im Überblick: {settings.base_url}/dashboard\n\nLocadas Nachfasser"
    n = Notification(
        business=b, kind="trial", title=title if first else "", text=text,
        buttons=[button("Passt", "ok"), button("Falsch erkannt", "wrong", d.id)],
        email_subject=recognized_line(d).rstrip("."), email_body=mail_body, doc_ids=[d.id],
        template="event", template_params=[(b.owner_name or "").split(" ")[0] or b.name, text], template_buttons=[payload("ok")],
    )
    if notify(session, n, now) and first:
        b.trial_aha_at = now
    if first_tap_now:
        start_first_tap(session, d, now)
    session.commit()
    if docs_in_window(session, b, now) >= UPGRADE_AFTER_DOCS:
        maybe_upgrade(session, b, now)


def start_first_tap(session: Session, d: Document, now: datetime) -> None:
    """Älteres Angebot: erste Stufe sofort als Tipp, Benachrichtigung gleich hinterher."""
    from . import taps

    nxt = next((f for f in d.followups if f.status == FollowUpStatus.SCHEDULED), None)
    if nxt is None:
        return
    nxt.due_at = min(nxt.due_at, now)
    start_tap(session, nxt, now)
    session.commit()
    taps.notify_pending(session, now, d.business_id)


# --- Tipps ohne Postfach -----------------------------------------------------------------------

def tap_kind_for(doc: Document) -> str:
    if doc.customer_mobile:
        return "wa"
    if doc.customer_email:
        return "mailto"
    return "manual"


def mailto_link(to: str, subject: str, body: str) -> str:
    """RFC 6068: Adresse, Betreff und Text prozentkodiert (UTF-8), Zeilenumbrüche als %0D%0A, Text unter 1800 Zeichen."""
    body = clip_body(body)
    body = re.sub(r"\r?\n", "\r\n", body)
    addr = quote((to or "").strip(), safe="@.+-_")
    return f"mailto:{addr}?subject={quote(subject or '', safe='')}&body={quote(body, safe='')}"


def clip_body(body: str, limit: int = MAILTO_BODY_MAX) -> str:
    body = (body or "").strip()
    if len(body) < limit:
        return body
    cut = body[: limit - 2]
    para = cut.rfind("\n\n")
    return (cut[:para] if para > limit // 2 else cut.rsplit(" ", 1)[0]).rstrip() + " …"


def prepare_texts(fu: FollowUp, now: datetime | None = None) -> None:
    """Texte je nach Art: WhatsApp kurz, Mail mit Betreff (mailto), ohne Kontakt die kurze Fassung zum Kopieren."""
    from . import personalize, texts, wa_texts

    doc = fu.document
    kind = tap_kind_for(doc)
    fu.tap_kind = kind
    if kind == "mailto":
        fu.subject = texts.subject_for(doc, fu.step)
        fu.body = clip_body(personalize.body_for(doc, fu.step, now))
        fu.wa_text = fu.body
    else:
        fu.wa_text = wa_texts.text_for(doc, fu.step, now)
        fu.subject, fu.body = ("WhatsApp" if kind == "wa" else "Nachricht"), fu.wa_text


def start_tap(session: Session, fu: FollowUp, now: datetime | None = None) -> None:
    """Fällige Stufe eines Testers als Tipp vorbereiten. Per Mail geht nie etwas automatisch raus."""
    now = now or utcnow()
    doc = fu.document
    prepare_texts(fu, now)
    fu.tap_token = fu.tap_token or secrets.token_urlsafe(24)
    fu.status = FollowUpStatus.AWAITING_TAP
    fu.tap_state = "open"
    fu.requested_at = now
    fu.tap_notified_at = fu.tapped_at = fu.tap_reminded_at = None
    fu.tap_notice = ""
    how = {"wa": "per WhatsApp", "mailto": "per Mail aus deinem Mailprogramm", "manual": "zum Kopieren"}[fu.tap_kind]
    log_event(session, doc.business_id, "tap", f"Nachfass-Nachricht {fu.step} für {_ref(doc)} an {_who(doc) or 'den Kunden'} liegt bereit, {how}.", doc.id)


def kind_label(fu: FollowUp) -> str:
    """Beschriftung des Tipp-Knopfs (höchstens 20 Zeichen)."""
    return {"mailto": "Mail öffnen", "manual": "Text kopieren"}.get(fu.tap_kind or "", "In WhatsApp öffnen")


def is_trial_tap(fu: FollowUp) -> bool:
    return (fu.tap_kind or "") in ("mailto", "manual") or bool(fu.document.business.is_trial)


# --- Kontakt nachreichen (Antwort per WhatsApp oder Formular) ------------------------------------

_EMAIL_RE = re.compile(r"^[\w.+'-]+@[\w-]+(?:\.[\w-]+)+$")


def parse_contact(text: str, currency: str = "EUR") -> tuple[str, str] | None:
    """(mobile_or_phone, email) aus einer kurzen Antwort, sonst None."""
    t = (text or "").strip().strip(".")
    if _EMAIL_RE.match(t):
        return "", t.lower()
    if re.fullmatch(r"[+\d][\d\s()/.\-]{6,22}", t):
        cc = "41" if (currency or "").upper() == "CHF" else "49"
        n = phones.normalize(t, cc)
        if n:
            return n, ""
    return None


def doc_waiting_for_contact(session: Session, b: Business) -> Document | None:
    """Jüngstes offenes Dokument ohne Kontakt (bevorzugt mit offenem Text-Tipp), sonst das jüngste offene."""
    docs = list(session.scalars(
        select(Document).where(Document.business_id == b.id, Document.status == DocStatus.OPEN).order_by(Document.created_at.desc(), Document.id.desc())
    ).all())
    for d in docs:
        if not (d.customer_mobile or d.customer_email):
            return d
    return None


def set_contact(session: Session, doc: Document, number: str = "", email: str = "") -> str:
    """Nummer oder Mailadresse eintragen, offene Text-Tipps auf WhatsApp bzw. Mail umstellen. Gibt den neuen Tipp-Link zurück."""
    from . import customers, taps

    if email:
        doc.customer_email = email[:320]
        customers.for_document(session, doc)
    if number:
        if phones.is_mobile(number):
            taps.set_phones(session, doc, "", number, overwrite=True)
        else:
            taps.set_phones(session, doc, number, "", overwrite=True)
    link = ""
    for f in doc.followups:
        if f.status == FollowUpStatus.AWAITING_TAP and f.tap_state == "open":
            prepare_texts(f)
            link = f"{settings.base_url}/t/{f.tap_token}"
    what = f"Mailadresse {email}" if email else f"Nummer {phones.pretty(number)}"
    log_event(session, doc.business_id, "edit", f"{what} für {_who(doc) or _ref(doc)} eingetragen.", doc.id)
    return link


def reply_contact(session: Session, b: Business, text: str) -> str | None:
    """Freitext im WhatsApp-Chat: Nummer oder Mailadresse für das jüngste Dokument ohne Kontakt. None = nicht zuständig."""
    if not b.is_trial:
        return None
    parsed = parse_contact(text)
    if parsed is None:
        return None
    doc = doc_waiting_for_contact(session, b)
    if doc is None:
        return None
    number, email = parsed
    if number and not phones.is_mobile(number) and not email:
        # Festnetz: kein WhatsApp, trotzdem speichern (zum Anrufen)
        set_contact(session, doc, number=number)
        return f"Danke, die Festnetznummer steht jetzt bei {_who(doc) or _ref(doc)}. Per WhatsApp geht das nicht, schick mir gern noch eine Handynummer oder Mailadresse."
    link = set_contact(session, doc, number=number, email=email)
    touch(b)
    how = "per WhatsApp" if number else "als fertige Mail"
    tail = f" Die Nachricht ist fertig, ein Tipp öffnet sie {how}: {link}" if link else " Ich melde mich, wenn die nächste Nachricht dran ist."
    return f"Danke, eingetragen bei {_who(doc) or _ref(doc)}.{tail}"


# --- Upgrade ------------------------------------------------------------------------------------

def _connect_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="locadas-trial-connect")


def connect_token(b: Business) -> str:
    return _connect_serializer().dumps({"b": b.id})


def business_from_connect_token(session: Session, token: str) -> Business | None:
    if not token or len(token) > 300:
        return None
    try:
        data = _connect_serializer().loads(token, max_age=60 * 60 * 24 * 60)
    except (BadSignature, SignatureExpired):
        return None
    b = session.get(Business, int(data.get("b", 0) or 0))
    return b if b is not None and b.is_trial else None


def connect_url(b: Business) -> str:
    return f"{settings.base_url}/verbinden?b={connect_token(b)}"


UPGRADE_TEXT = "Soll das künftig automatisch laufen, ohne Weiterleiten? 15 Minuten Einrichtung mit Julius, kostenlos."


def _upgrade_notification(b: Business, title: str, text: str) -> Notification:
    self_link = connect_url(b)
    wa_text = f"{text}\n\nLieber selbst verbinden? Zwei Minuten: {self_link}"
    mail = (f"Hallo {b.owner_name or b.name},\n\n{text}\n\nTermin buchen: {settings.calendly_url}\nSelbst verbinden: {self_link}\n\n"
            "Deine bisherigen Angebote bleiben erhalten.\n\nLocadas Nachfasser")
    return Notification(
        business=b, kind="trial", title=title, text=wa_text, cta=("Termin buchen", settings.calendly_url),
        email_subject=title, email_body=mail,
        template="event", template_params=[(b.owner_name or "").split(" ")[0] or b.name,
                                           f"{text} Termin buchen: {settings.calendly_url} Selbst verbinden: {self_link}"],
        template_buttons=[payload("ok")],
    )


def upgrade_notification(b: Business) -> Notification:
    return _upgrade_notification(b, "Automatisch nachfassen?", UPGRADE_TEXT)


def upgrade_due(b: Business) -> bool:
    return b.is_trial and b.upgrade_prompt_at is None


def maybe_upgrade(session: Session, b: Business, now: datetime | None = None) -> bool:
    """Einmal: „Soll das künftig automatisch laufen?“ Gibt True zurück, wenn gesendet."""
    if not upgrade_due(b):
        return False
    now = now or utcnow()
    if notify(session, upgrade_notification(b), now):
        b.upgrade_prompt_at = now
        log_event(session, b.id, "trial", "Angebot geschickt: Postfach verbinden (Termin oder selbst).")
        return True
    return False


def claim_upgrade(b: Business, now: datetime | None = None) -> Notification | None:
    """Für den WhatsApp-Chat: Nachricht zurückgeben (wird nach der Antwort verschickt) und als gesendet merken."""
    if not upgrade_due(b):
        return None
    b.upgrade_prompt_at = now or utcnow()
    return upgrade_notification(b)


# --- Umwandeln beim Verbinden des Postfachs ---------------------------------------------------------

def convert(session: Session, b: Business, account: MailAccount, now: datetime | None = None) -> int:
    """Tester verbindet sein Postfach: Dokumente bleiben, ab jetzt per Mail automatisch, WhatsApp-Sekretär bleibt.

    Offene Tipps ohne Postfach werden wieder normale geplante Stufen (gehen dann wie gewohnt per Mail raus)."""
    now = now or utcnow()
    b.mode = "connected"
    b.followup_channel = "mail"
    n = 0
    for d in b.documents:
        if d.account_id is None:
            d.account_id = account.id
            n += 1
        for f in d.followups:
            if f.status == FollowUpStatus.AWAITING_TAP and f.tap_state == "open":
                f.tap_state = "cancelled"
                f.status = FollowUpStatus.SCHEDULED
                f.subject = f.body = ""
                f.due_at = max(f.due_at, now + timedelta(hours=1))
                f.announced_at = f.send_after = None
        pending = [f.due_at for f in d.followups if f.status == FollowUpStatus.SCHEDULED]
        if d.status == DocStatus.OPEN:
            d.next_followup_at = min(pending) if pending else None
    log_event(session, b.id, "trial", f"Postfach verbunden. {n} Dokumente aus dem Test übernommen, ab jetzt läuft das Nachfassen automatisch.")
    return n


# --- Aufräumen ---------------------------------------------------------------------------------

def last_activity(b: Business) -> datetime:
    return max(x for x in (b.created_at, b.last_login_at, b.trial_active_at) if x is not None)


def cleanup(session: Session, now: datetime | None = None) -> int:
    """Tester ohne Aktivität seit TRIAL_DELETE_AFTER_DAYS Tagen löschen, mit PDFs."""
    now = now or utcnow()
    limit = now - timedelta(days=settings.trial_delete_after_days)
    gone = 0
    for b in session.scalars(select(Business).where(Business.mode == "trial")).all():
        if last_activity(b) >= limit:
            continue
        for d in b.documents:
            if d.pdf_path:
                try:
                    Path(d.pdf_path).unlink(missing_ok=True)
                except OSError:
                    pass
        from ..db import InboundMail

        for row in session.scalars(select(InboundMail).where(InboundMail.business_id == b.id)).all():
            session.delete(row)
        session.delete(b)
        gone += 1
    if gone:
        session.commit()
        log.info("Testzugänge gelöscht: %s", gone)
    return gone


def cleanup_job() -> int:
    with new_session() as s:
        return cleanup(s)
