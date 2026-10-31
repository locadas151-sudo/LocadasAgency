"""Webhooks der WhatsApp-Konten unserer Betriebe (Stufe 2), über denselben Endpunkt /webhook/whatsapp.

route() nimmt aus einem Webhook alles heraus, was zu einer verbundenen Betriebsnummer gehört
(metadata.phone_number_id bzw. entry.id = waba_id), und gibt den Rest unverändert an den Sekretär weiter.

- messages: Kunde schreibt dem Betrieb → 24h-Fenster auf, Einwilligung „customer_initiated_chat“,
  Abmeldewörter (STOP, abmelden …) → abgemeldet, sonst als Antwort werten: Nachfassen stoppen, Betrieb kurz informieren.
- smb_message_echoes: Betrieb hat selbst aus der App geschrieben → wie selbst nachgefasst, nächste Stufe rückt nach hinten.
- statuses: sent/delivered/read/failed → echte Lesebestätigung (FollowUp.wa_read_at), Abrechnungskategorie, Fehler.
- history / smb_app_state_sync: einmaliger Abgleich nach dem Verbinden (nur Hinweise, keine Einwilligung).
- account_update, message_template_status_update, template_category_update, phone_number_quality_update.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import (
    Business, DocStatus, Document, FollowUp, FollowUpStatus, WaContact, WaMessage, WaTemplate, WhatsAppConnection,
    WhatsAppInbound, log_event, utcnow,
)
from . import contacts, usage
from . import wa_business as wab
from .config import wab_settings

log = logging.getLogger(__name__)


def valid_signature(body: bytes, header: str | None) -> bool:
    secret = wab_settings.app_secret
    if not wab_settings.enabled or not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:].strip())


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _ts(v, fallback: datetime) -> datetime:
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return fallback


def _conn_by_phone(session: Session, pnid: str) -> WhatsAppConnection | None:
    if not pnid:
        return None
    return session.scalar(select(WhatsAppConnection).where(
        WhatsAppConnection.phone_number_id == pnid, WhatsAppConnection.status.in_(["active", "error", "pending"]),
    ))


def _conn_by_waba(session: Session, waba_id: str) -> WhatsAppConnection | None:
    if not waba_id:
        return None
    return session.scalar(select(WhatsAppConnection).where(
        WhatsAppConnection.waba_id == waba_id, WhatsAppConnection.status != "disconnected",
    ))


def route(session: Session, data: dict, now: datetime | None = None) -> tuple[dict, int]:
    """Betriebs-Teile verarbeiten. Gibt (Rest für den Sekretär, Zahl verarbeiteter Einträge) zurück."""
    now = now or utcnow()
    if not isinstance(data, dict):
        return data, 0
    # Partner-Hülle (history / smb_app_state_sync direkt an die Partner-URL)
    if data.get("event") in ("history", "smb_app_state_sync") and isinstance(data.get("data"), dict):
        inner = data["data"]
        conn = _conn_by_phone(session, str((inner.get("metadata") or {}).get("phone_number_id") or "")) or _conn_by_waba(session, str(inner.get("id") or ""))
        if conn is None:
            return {}, 0
        _safe(session, _sync_event, session, conn, data["event"], inner, now)
        return {}, 1
    handled = 0
    rest_entries = []
    for entry in data.get("entry") or []:
        rest_changes = []
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            field = change.get("field") or ""
            pnid = str((value.get("metadata") or {}).get("phone_number_id") or "")
            conn = _conn_by_phone(session, pnid) if pnid else None
            if conn is None and not pnid and field in _WABA_FIELDS:
                conn = _conn_by_waba(session, str(entry.get("id") or ""))
            if conn is None:
                rest_changes.append(change)
                continue
            handled += _safe(session, _change, session, conn, field, value, now) or 0
        if rest_changes:
            rest_entries.append({**entry, "changes": rest_changes})
    rest = {**data, "entry": rest_entries} if rest_entries else {}
    return rest, handled


def _safe(session: Session, fn, *args):
    try:
        out = fn(*args)
        session.commit()
        return out
    except Exception:  # ein kaputter Eintrag darf den Rest nicht blockieren
        session.rollback()
        log.exception("WhatsApp-Webhook (Betriebsnummer) nicht verarbeitet")
        return 0


_WABA_FIELDS = {"account_update", "message_template_status_update", "template_category_update", "phone_number_quality_update"}


def _change(session: Session, conn: WhatsAppConnection, field: str, value: dict, now: datetime) -> int:
    n = 0
    if field in ("history", "smb_app_state_sync"):
        _sync_event(session, conn, field, value, now)
        return 1
    if field == "smb_message_echoes" or value.get("message_echoes"):
        for echo in value.get("message_echoes") or []:
            n += _echo(session, conn, echo, now)
        return n
    if field == "account_update":
        return _account_update(session, conn, value)
    if field == "message_template_status_update":
        return _template_status(session, conn, value)
    if field == "template_category_update":
        return _template_category(session, conn, value)
    if field == "phone_number_quality_update":
        conn.messaging_limit_tier = str(value.get("current_limit") or conn.messaging_limit_tier)[:40]
        if value.get("event"):
            conn.quality_rating = str(value.get("event"))[:20]
        return 1
    for msg in value.get("messages") or []:
        n += _inbound(session, conn, msg, value, now)
    for st in value.get("statuses") or []:
        n += _status(session, conn, st, now)
    return n


# --- Kunde schreibt -----------------------------------------------------------------------

def _first_time(session: Session, wamid: str, business_id: int) -> bool:
    if not wamid:
        return True
    if session.scalar(select(WhatsAppInbound.id).where(WhatsAppInbound.wamid == wamid)) is not None:
        return False
    session.add(WhatsAppInbound(wamid=wamid, business_id=business_id))
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return False
    return True


def _text_of(msg: dict) -> str:
    kind = msg.get("type")
    if kind == "text":
        return ((msg.get("text") or {}).get("body") or "").strip()
    if kind == "button":
        return ((msg.get("button") or {}).get("text") or "").strip()
    if kind == "interactive":
        i = msg.get("interactive") or {}
        return ((i.get("button_reply") or i.get("list_reply") or {}).get("title") or "").strip()
    if kind in ("image", "document", "video", "audio", "sticker"):
        cap = ((msg.get(kind) or {}).get("caption") or "").strip()
        return cap or {"image": "[Bild]", "document": "[Dokument]", "video": "[Video]", "audio": "[Sprachnachricht]", "sticker": "[Sticker]"}[kind]
    return f"[{kind or 'Nachricht'}]"


def _pick_doc(session: Session, business_id: int, phone: str, docs: list[Document]) -> Document | None:
    """Zu welchem Vorgang gehört die Nachricht? Zuletzt per WhatsApp nachgefasst, sonst das einzige offene Dokument."""
    if not docs:
        return None
    ids = {d.id for d in docs}
    last = session.scalar(
        select(WaMessage).where(WaMessage.business_id == business_id, WaMessage.to_phone == phone, WaMessage.document_id.in_(ids))
        .order_by(WaMessage.created_at.desc(), WaMessage.id.desc())
    )
    if last is not None:
        return next(d for d in docs if d.id == last.document_id)
    return docs[0] if len(docs) == 1 else None


def _inbound(session: Session, conn: WhatsAppConnection, msg: dict, value: dict, now: datetime) -> int:
    phone = _digits(msg.get("from"))
    if not phone or not _first_time(session, str(msg.get("id") or ""), conn.business_id):
        return 0
    business = session.get(Business, conn.business_id)
    profile = next((c.get("profile", {}).get("name", "") for c in value.get("contacts") or [] if _digits(c.get("wa_id")) == phone), "")
    contact = contacts.get_or_create(session, business.id, phone, name=profile or "")
    when = _ts(msg.get("timestamp"), now)
    contact.last_inbound_at = max(when, contact.last_inbound_at) if contact.last_inbound_at else when
    text = _text_of(msg)

    if contacts.is_opt_out(text):
        contacts.opt_out(contact, now)
        log_event(session, business.id, "whatsapp", f"+{phone} möchte keine WhatsApp-Nachrichten mehr. Ich fasse dort nur noch per Mail nach.")
        try:
            wab.send_text(conn, phone, "Alles klar, Sie bekommen von uns hier keine automatischen Erinnerungen mehr.")
            usage.count(session, business.id, "service", now)
        except wab.WaSendError as exc:
            log.info("Bestätigung der Abmeldung nicht gesendet: %s", exc)
        return 1

    if not contact.wa_opt_in_source and contact.opted_out_at is None:
        contact.wa_opt_in_source, contact.opt_in_at = "customer_initiated_chat", now

    docs = contacts.open_docs_for(session, business.id, phone, contact.customer_email)
    doc = _pick_doc(session, business.id, phone, docs)
    if doc is None:
        if docs:
            names = ", ".join(sorted({d.customer_name or d.customer_email for d in docs}))
            log_event(session, business.id, "reply", f"{contact.name or '+' + phone} hat dir auf WhatsApp geschrieben. Offene Vorgänge: {names}. Nachfassen läuft weiter, bis du es stoppst.")
        return 1
    _close_on_reply(session, doc, phone, contact, text, now)
    return 1


def _close_on_reply(session: Session, doc: Document, phone: str, contact: WaContact, text: str, now: datetime) -> None:
    from ..engine import followups as fe
    from ..engine import matcher

    result = None
    if matcher.enabled() and text and not text.startswith("["):
        try:
            from ..mail.imap import ParsedMail

            mail = ParsedMail(uid=0, message_id="", subject=f"WhatsApp von +{phone}", from_addr=doc.customer_email or "",
                              from_name=contact.name or "", to=[], date=now, in_reply_to="", references=[], text=text)
            result = matcher.classify_mail(mail, text, [doc], known=doc)
        except Exception as exc:  # nie das Stoppen verhindern
            log.info("Einordnung der WhatsApp-Antwort fehlgeschlagen: %s", exc)
    status = DocStatus.REPLIED
    doc.reply_intent, doc.reply_summary = "", ""
    if result is not None:
        doc.reply_intent, doc.reply_summary = result.intent, result.summary[:300]
        status = matcher.status_for(doc, result.intent)
    doc.last_customer_reply_at = now
    fe.close_document(session, doc, status, f"Antwort per WhatsApp von +{phone}")
    if doc.reply_summary:
        log_event(session, doc.business_id, "reply", f"{doc.reply_intent_label}: {doc.reply_summary}", doc.id)
    _notify_owner_reply(session, doc, phone, text, status)


def _notify_owner_reply(session: Session, doc: Document, phone: str, text: str, status: DocStatus) -> None:
    b = doc.business
    if not b.notify_opened:
        return
    from ..config import settings
    from ..notify import send
    from ..notify import messages as m

    notes = {
        DocStatus.WON: "Ich habe das Angebot als „Auftrag bekommen“ markiert und fasse nicht mehr nach.",
        DocStatus.LOST: "Ich habe das Angebot als Absage markiert und fasse nicht mehr nach.",
        DocStatus.PAID: "Ich habe die Rechnung als bezahlt markiert und fasse nicht mehr nach. Prüf bitte trotzdem den Zahlungseingang.",
    }
    note = notes.get(status, "Ich fasse dort nicht mehr nach.")
    who = doc.customer_name or f"+{phone}"
    ref = f"{doc.kind_label} {doc.document_number or doc.title}".strip()
    quote = text if len(text) <= 300 else text[:299] + "…"
    lines = [f"Hallo {b.owner_name or b.name},", "", f"{who} hat dir auf WhatsApp zu {ref} geantwortet. Die Nachricht steht auch in deiner WhatsApp Business App."]
    if doc.reply_summary:
        lines += ["", f"» {doc.reply_summary}"]
    elif quote:
        lines += ["", f"» {quote}"]
    lines += ["", note, "", f"Wenn das nicht stimmt, kannst du das hier ändern: {settings.base_url}/dokument/{doc.id}", "", "Locadas Nachfasser"]
    try:
        send(session, m.replied(doc, note, f"{who} hat auf WhatsApp geantwortet ({ref})", "\n".join(lines)))
    except Exception as exc:
        log.info("Hinweis zur WhatsApp-Antwort nicht zugestellt: %s", exc)


# --- Betrieb schreibt selbst aus der App -----------------------------------------------------

def _echo(session: Session, conn: WhatsAppConnection, echo: dict, now: datetime) -> int:
    from ..engine.scanner import _push_back

    wamid = str(echo.get("id") or "")
    if wamid and session.scalar(select(WaMessage.id).where(WaMessage.wamid == wamid)) is not None:
        return 0  # unsere eigene API-Nachricht
    if not _first_time(session, wamid, conn.business_id):
        return 0
    phone = _digits(echo.get("to"))
    if not phone:
        return 0
    when = _ts(echo.get("timestamp"), now)
    contact = contacts.get(session, conn.business_id, phone)
    docs = contacts.open_docs_for(session, conn.business_id, phone, contact.customer_email if contact else "")
    for doc in docs:
        doc.owner_contact_at = when
        _push_back(doc, when)
        log_event(session, conn.business_id, "owner", f"Du hast {doc.customer_name or '+' + phone} selbst auf WhatsApp geschrieben. Die nächste Nachfassung zu {doc.kind_label} {doc.document_number or doc.title} rückt nach hinten.", doc.id)
    return 1 if docs else 0


# --- Zustellstatus -------------------------------------------------------------------------

def _status(session: Session, conn: WhatsAppConnection, st: dict, now: datetime) -> int:
    wamid = str(st.get("id") or "")
    rec = session.scalar(select(WaMessage).where(WaMessage.wamid == wamid)) if wamid else None
    if rec is None:
        return 0
    state = str(st.get("status") or "")
    when = _ts(st.get("timestamp"), now)
    pricing = st.get("pricing") or {}
    cat = str(pricing.get("category") or "").lower()
    if cat and cat in usage.CATEGORIES and cat != rec.category:
        usage.recategorize(session, rec.business_id, rec.category, cat, rec.created_at)
        rec.category = cat
    fu = session.get(FollowUp, rec.followup_id) if rec.followup_id else None
    if state == "delivered":
        rec.delivered_at = rec.delivered_at or when
        rec.status = rec.status if rec.status == "read" else "delivered"
    elif state == "read":
        rec.read_at = rec.read_at or when
        rec.delivered_at = rec.delivered_at or when
        rec.status = "read"
        if fu is not None and fu.wa_read_at is None:
            fu.wa_read_at = when
    elif state == "sent":
        if rec.status == "accepted":
            rec.status = "sent"
    elif state == "failed":
        _failed(session, conn, rec, fu, st, now)
    return 1


def _failed(session: Session, conn: WhatsAppConnection, rec: WaMessage, fu: FollowUp | None, st: dict, now: datetime) -> None:
    from ..channels import graph, handle_send_error

    errs = st.get("errors") or [{}]
    exc = graph.GraphError.from_payload(errs[0] if isinstance(errs[0], dict) else {})
    err = wab.map_error(exc, rec.to_phone)
    rec.status, rec.failed_at, rec.error = "failed", now, f"{err.code}: {exc}"[:1000]
    usage.count(session, rec.business_id, rec.category, rec.created_at, -1)  # nicht zugestellt kostet nichts
    business = session.get(Business, rec.business_id)
    contact = contacts.get(session, rec.business_id, rec.to_phone)
    tmpl = None
    if rec.template_key:
        tmpl = session.scalar(select(WaTemplate).where(WaTemplate.connection_id == conn.id, WaTemplate.key == rec.template_key))
    handle_send_error(session, business, conn, contact, err, now, doc_id=rec.document_id, template=tmpl)
    # Nachfassung kam nicht an: Schritt wieder einplanen, dann geht er per Mail (WhatsApp ist für den Kontakt gesperrt)
    if fu is not None and fu.status == FollowUpStatus.SENT and err.fallback_mail:
        doc = fu.document
        if doc.status == DocStatus.OPEN:
            fu.status = FollowUpStatus.SCHEDULED
            fu.sent_at = None
            fu.subject, fu.body = "", ""
            fu.due_at = now
            fu.announced_at = fu.send_after = None
            doc.followups_sent = max(0, doc.followups_sent - 1)
            doc.next_followup_at = min(f.due_at for f in doc.followups if f.status == FollowUpStatus.SCHEDULED)
            if contact is not None and contact.undeliverable_at is None and contact.opted_out_at is None:
                contact.undeliverable_at = now  # sonst würde derselbe Weg wieder gewählt


# --- Konto, Vorlagen, Abgleich -----------------------------------------------------------------

def _account_update(session: Session, conn: WhatsAppConnection, value: dict) -> int:
    event = str(value.get("event") or "")
    if event in ("PARTNER_REMOVED", "ACCOUNT_DELETED", "DISABLED_UPDATE") or (event == "ACCOUNT_RESTRICTION"):
        if event == "PARTNER_REMOVED":
            conn.status, conn.business_token_enc = "disconnected", ""
            text = "Du hast Locadas in WhatsApp getrennt. Nachfassen läuft wieder nur per Mail."
        else:
            conn.status = "error"
            text = f"Meta meldet eine Einschränkung deines WhatsApp-Kontos ({event}). Bis das geklärt ist, fasse ich per Mail nach."
        conn.last_error = text
        log_event(session, conn.business_id, "whatsapp", text)
    return 1


def _template_status(session: Session, conn: WhatsAppConnection, value: dict) -> int:
    name = str(value.get("message_template_name") or "")
    lang = str(value.get("message_template_language") or "")
    q = select(WaTemplate).where(WaTemplate.connection_id == conn.id, WaTemplate.name == name)
    if lang:
        q = q.where(WaTemplate.language == lang)
    rec = session.scalar(q)
    if rec is None:
        return 0
    rec.status = str(value.get("event") or rec.status).upper()[:30]
    rec.note = str(value.get("reason") or "")[:500]
    return 1


def _template_category(session: Session, conn: WhatsAppConnection, value: dict) -> int:
    name = str(value.get("message_template_name") or "")
    new = str(value.get("new_category") or value.get("correct_category") or "").upper()
    rec = session.scalar(select(WaTemplate).where(WaTemplate.connection_id == conn.id, WaTemplate.name == name))
    if rec is None or not new:
        return 0
    if new != rec.category:
        old = rec.category
        rec.category = new[:20]
        if new == "MARKETING":
            log_event(session, conn.business_id, "whatsapp", f"Meta stuft die Vorlage {name} als Werbung ein (vorher {old or 'unbekannt'}). Jede Nachricht damit kostet mehr und Meta begrenzt Werbung pro Kunde.")
    return 1


def _sync_event(session: Session, conn: WhatsAppConnection, kind: str, value: dict, now: datetime) -> None:
    if kind == "smb_app_state_sync":
        n = 0
        for item in value.get("state_sync") or []:
            c = item.get("contact") or {}
            phone = _digits(c.get("phone_number"))
            if item.get("type") != "contact" or not phone:
                continue
            rec = contacts.get_or_create(session, conn.business_id, phone, name=c.get("full_name") or c.get("first_name") or "")
            n += 1 if rec else 0
        log.info("Kontakte aus der WhatsApp Business App übernommen: %s", n)
        return
    if value.get("errors") or any(h.get("errors") for h in value.get("history") or [] if isinstance(h, dict)):
        log_event(session, conn.business_id, "whatsapp", "Du hast den Chatverlauf nicht geteilt. Das ist in Ordnung, Locadas schreibt trotzdem in deinem Namen.")
        return
    biz_phone = _digits((value.get("metadata") or {}).get("display_phone_number"))
    for chunk in value.get("history") or []:
        for thread in chunk.get("threads") or []:
            phone = _digits(thread.get("id"))
            if not phone:
                continue
            wrote = any(_digits(m.get("from")) == phone for m in thread.get("messages") or [])
            if wrote and phone != biz_phone:
                rec = contacts.get_or_create(session, conn.business_id, phone)
                rec.history_seen_at = rec.history_seen_at or now
