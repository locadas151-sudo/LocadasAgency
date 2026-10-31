"""Eingehende WhatsApp-Nachrichten: Knopfdruck ausführen, kurz bestätigen, nächste offene Frage stellen.

Jede Aktion ist idempotent: zweimal drücken ändert nichts mehr und bekommt nur „schon erledigt“ zurück.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import Business, DocKind, DocStatus, Document, FollowUp, FollowUpStatus, WhatsAppInbound, log_event, utcnow
from ..engine import followups as fe
from . import messages as m
from . import whatsapp as wa
from .core import Notification, send
from .tokens import doc_link, parse

log = logging.getLogger(__name__)

_EMAIL = re.compile(r"^[\w.+'-]+@[\w-]+(?:\.[\w-]+)+$")
_STOP_N = re.compile(r"^\s*stopp?\s*(\d{1,2})\s*[.!]?\s*$", re.I)
_OPT_OUT = {"abmelden", "stop whatsapp", "stopp whatsapp", "keine nachrichten"}

OUTCOME_LABEL = {"won": "Auftrag bekommen", "lost": "Nicht bekommen", "paid": "Bezahlt", "unpaid": "Nicht bezahlt, Betrieb kümmert sich"}
OUTCOME_STATUS = {"won": DocStatus.WON, "lost": DocStatus.LOST, "paid": DocStatus.PAID, "unpaid": DocStatus.DONE}


# --- Webhook ---------------------------------------------------------------------

def process_webhook(session: Session, data: dict, now: datetime | None = None) -> int:
    """Alle Nachrichten aus einem Webhook-Aufruf verarbeiten. Gibt die Zahl der verarbeiteten Nachrichten zurück."""
    now = now or utcnow()
    handled = 0
    for entry in data.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for msg in value.get("messages") or []:
                try:
                    if handle_message(session, msg, now):
                        handled += 1
                    session.commit()
                except Exception:  # ein kaputter Eintrag darf den Rest nicht blockieren
                    session.rollback()
                    log.exception("WhatsApp-Nachricht nicht verarbeitet")
    return handled


def _businesses_for(session: Session, number: str) -> list[Business]:
    return list(session.scalars(
        select(Business).where(Business.whatsapp_number == number).order_by(Business.id.desc())
    ).all())


def _first_time(session: Session, wamid: str, business_id: int | None) -> bool:
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


def handle_message(session: Session, msg: dict, now: datetime) -> bool:
    sender = re.sub(r"\D", "", str(msg.get("from") or ""))
    businesses = _businesses_for(session, sender) if sender else []
    if not businesses:
        log.info("WhatsApp von unbekannter Nummer ignoriert")
        return False
    if not _first_time(session, str(msg.get("id") or ""), businesses[0].id):
        return False  # Meta hat denselben Webhook noch mal geschickt
    for b in businesses:
        b.whatsapp_last_inbound_at = now  # 24h-Fenster geht auf
    kind = msg.get("type")
    pl, text = "", ""
    if kind == "interactive":
        pl = ((msg.get("interactive") or {}).get("button_reply") or {}).get("id", "")
    elif kind == "button":  # Knopf einer Vorlage
        pl = (msg.get("button") or {}).get("payload", "")
    elif kind == "text":
        text = ((msg.get("text") or {}).get("body") or "").strip()

    if pl:
        reply, follow = run_payload(session, businesses, pl, now)
        b = follow.business if follow is not None else businesses[0]
    else:
        b = businesses[0]
        reply, follow = run_text(session, b, text, now)
    session.flush()
    if reply and b.whatsapp_number:
        try:
            wa.send_text(b.whatsapp_number, reply)
        except wa.WhatsAppError as exc:
            log.warning("Antwort per WhatsApp fehlgeschlagen: %s", exc)
    if follow is not None and b.whatsapp_opt_in:
        send(session, follow, now)
    return True


# --- Knöpfe ------------------------------------------------------------------------

def run_payload(session: Session, businesses: list[Business], value: str, now: datetime) -> tuple[str, Notification | None]:
    parsed = parse(value)
    if parsed is None:
        return "Dieser Knopf ist abgelaufen. Schreib mir einfach, wenn du Hilfe brauchst.", None
    action, ids = parsed
    allowed = {b.id for b in businesses}

    def own_doc(i: int) -> Document | None:
        d = session.get(Document, i)
        return d if d is not None and d.business_id in allowed else None

    def own_fu(i: int) -> FollowUp | None:
        f = session.get(FollowUp, i)
        return f if f is not None and f.document.business_id in allowed else None

    if action == "ok":
        return "Alles klar.", None

    if action in ("fertig", "notyet"):
        return _completion(session, allowed, action, ids, now), None

    if action == "pick":
        docs = [d for d in (own_doc(i) for i in ids) if d is not None and d.status == DocStatus.OPEN]
        if not docs:
            return "Die sind alle schon erledigt.", None
        return "", m.pick(docs[0].business, docs)

    if action in ("send", "skip", "gone", "settle"):
        fu = own_fu(ids[0]) if ids else None
        if fu is None:
            return "Diesen Vorgang finde ich nicht mehr.", None
        doc = fu.document
        if action == "settle":
            label = fe.settle(session, doc)
            reply = f"Notiert: {m.who(doc)}, {label}. Ich fasse dort nicht nach." if label else f"Bei {m.who(doc)} fasse ich schon nicht mehr nach."
        elif action == "send":
            if fu.status == FollowUpStatus.SENT:
                reply = "Die ist schon raus."
            elif fu.status != FollowUpStatus.AWAITING_APPROVAL:
                reply = "Diese Freigabe ist nicht mehr offen."
            elif doc.needs_review and doc.kind != DocKind.INVOICE:
                fe.release_review(session, fu)
                reply = f"Alles klar, ich fasse bei {m.who(doc)} nach. Den Text bekommst du vorher noch zu sehen."
            elif fe.approve(session, fu):
                what = "Zahlungserinnerung" if doc.kind == DocKind.INVOICE else "Mail"
                reply = f"Gesendet. Die {what} an {m.who(doc)} ist in deinem Namen raus."
            else:
                reply = f"Das hat nicht geklappt: {fu.error or 'unbekannter Fehler'}"
        elif action == "skip":
            if fu.status == FollowUpStatus.AWAITING_APPROVAL:
                fe.skip(session, fu)
                reply = "Übersprungen. Die nächste Stufe bleibt geplant."
            else:
                reply = "Diese Freigabe ist nicht mehr offen."
        else:
            reply = _stop(session, doc)
        return reply, next_approval(session, doc.business)

    doc = own_doc(ids[0]) if ids else None
    if doc is None:
        return "Diesen Vorgang finde ich nicht mehr.", None

    if action == "stop":
        return _stop(session, doc), None

    if action in OUTCOME_LABEL:
        label = OUTCOME_LABEL[action]
        newly = False
        if doc.status == OUTCOME_STATUS[action] and doc.close_reason == label:
            reply = f"Ist schon notiert: {m.who(doc)}, {label}."
        else:
            newly = True
            if doc.status == DocStatus.OPEN:
                fe.cancel_open_followups(session, doc, "Vom Betrieb per WhatsApp gesetzt")
            fe.set_outcome(session, doc, action)
            reply = f"Notiert: {m.who(doc)}, {label}."
        follow = next_outcome(session, doc.business)
        if action == "won" and newly:
            from ..engine import customers

            c = customers.for_document(session, doc)
            if c is not None and c.completed_at is None:
                if follow is None:
                    follow = customers.won_followup(doc, c)
                else:
                    reply += " Ist der Auftrag später fertig, tipp im Dashboard auf „Fertig melden“."
        return reply, follow

    if action == "call":
        return _contact(doc), None

    if action == "done":
        return "Gut, abgehakt.", None

    if action == "reopen":
        if doc.status == DocStatus.OPEN:
            return f"Bei {m.who(doc)} fasse ich schon weiter nach.", None
        if doc.status == DocStatus.BOUNCED:
            return "Die Adresse ist unzustellbar. Schick mir die richtige Mailadresse, dann fasse ich dort weiter nach.", None
        fe.reopen_document(session, doc)
        return f"Alles klar, ich fasse bei {m.who(doc)} weiter nach.{_next_hint(doc)}", None

    return "", None


def _completion(session: Session, allowed: set[int], action: str, ids: list[int], now: datetime) -> str:
    from ..db import Customer
    from ..engine import customers

    c = session.get(Customer, ids[0]) if ids else None
    if c is None or c.business_id not in allowed:
        return "Diesen Kunden finde ich nicht mehr."
    who = c.name or c.email
    if action == "notyet":
        return f"Alles klar, ich frage nicht noch einmal. Ist der Auftrag bei {who} fertig, tipp im Dashboard auf „Fertig melden“."
    result = customers.mark_complete(session, c, now)
    if result == "sent":
        review = " und dem Link zu deiner Google-Bewertung" if c.review_requested_at else ""
        return f"Erledigt. {who} hat die Abschluss-Mail mit allen Unterlagen{review} bekommen, in deinem Namen."
    if result == "already":
        return f"{who} hat die Abschluss-Mail schon bekommen."
    if result == "no_account":
        return "Dein Postfach ist nicht verbunden, deshalb konnte ich nichts schicken."
    return "Die Abschluss-Mail ging nicht raus. Prüf bitte die Postfach-Verbindung im Dashboard und versuch es dort noch einmal."


def _stop(session: Session, doc: Document) -> str:
    if doc.status != DocStatus.OPEN:
        return f"Bei {m.who(doc)} fasse ich schon nicht mehr nach."
    fe.close_document(session, doc, DocStatus.REPLIED, "Vom Betrieb gestoppt")
    return f"Alles klar, bei {m.who(doc)} fasse ich nicht mehr nach."


def _next_hint(doc: Document) -> str:
    if not doc.next_followup_at:
        return ""
    from zoneinfo import ZoneInfo

    return f" Nächste Mail am {doc.next_followup_at.astimezone(ZoneInfo(settings.timezone)).strftime('%d.%m.')}"


def _contact(doc: Document) -> str:
    # Die Telefonnummer des Kunden kennen wir nicht (steht nur im PDF), also Mail und Dokument-Link
    lines = [f"*{m.who(doc)} anrufen*", "Die Nummer habe ich nicht gespeichert, sie steht meist im Dokument. Tipp im PDF auf die Nummer, dann wählt dein Handy direkt."]
    if doc.customer_email:
        lines.append(f"Mail: {doc.customer_email}")
    lines.append(f"{m.ref(doc)}{m.dmoney(doc)}")
    if doc.pdf_path:
        lines.append(doc_link(doc.id))
    return "\n".join(lines)


# --- Warteschlangen: eine Frage nach der anderen -------------------------------------

def next_approval(session: Session, business: Business) -> Notification | None:
    pending = list(session.scalars(
        select(FollowUp).join(Document).where(Document.business_id == business.id, FollowUp.status == FollowUpStatus.AWAITING_APPROVAL).order_by(FollowUp.due_at, FollowUp.id)
    ).all())
    if not pending:
        return None
    fu = pending[0]
    subject, body = fe._approval_mail(business, fu)
    n = m.approval(business, fu, 1, len(pending), subject, body)
    n.footer = f"Noch {len(pending)} offen" if len(pending) > 1 else n.footer
    return n


def next_outcome(session: Session, business: Business) -> Notification | None:
    docs = list(session.scalars(
        select(Document).where(
            Document.business_id == business.id, Document.status == DocStatus.DONE,
            Document.outcome_asked_at.is_not(None), Document.close_reason == fe.OUTCOME_PENDING_REASON,
        ).order_by(Document.sent_at, Document.id)
    ).all())
    if not docs:
        return None
    subject, body = fe.outcome_mail(business, docs[:1])
    n = m.outcome(docs[0], 1, len(docs), subject, body)
    n.footer = f"Noch {len(docs)} offen" if len(docs) > 1 else ""
    return n


# --- Freitext ------------------------------------------------------------------------

def _context(b: Business) -> dict:
    try:
        data = json.loads(b.whatsapp_context or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def run_text(session: Session, b: Business, text: str, now: datetime) -> tuple[str, Notification | None]:
    low = text.lower().strip(" .!")
    ctx = _context(b)
    if low in _OPT_OUT:
        b.whatsapp_opt_in = False
        log_event(session, b.id, "whatsapp", "WhatsApp-Sekretär abgemeldet. Benachrichtigungen kommen wieder per Mail.")
        return "Erledigt, ich schreibe dir hier nicht mehr. Alles Weitere kommt wieder per Mail. Du kannst das in den Einstellungen wieder einschalten.", None
    hit = _STOP_N.match(text)
    if hit and ctx.get("list"):
        idx = int(hit.group(1))
        ids = ctx["list"]
        if not 1 <= idx <= len(ids):
            return f"In meiner letzten Liste gibt es nur {len(ids)} Kunden.", None
        doc = session.get(Document, int(ids[idx - 1]))
        if doc is None or doc.business_id != b.id:
            return "Diesen Vorgang finde ich nicht mehr.", None
        return _stop(session, doc), None
    if _EMAIL.match(text.strip()) and ctx.get("bounced"):
        doc = session.get(Document, int(ctx["bounced"]))
        if doc is not None and doc.business_id == b.id:
            addr = text.strip().lower()
            doc.customer_email = addr[:320]
            for f in doc.followups:
                if f.status == FollowUpStatus.CANCELLED:
                    f.subject, f.body = "", ""
            if doc.status == DocStatus.BOUNCED:
                fe.reopen_document(session, doc)
            ctx.pop("bounced", None)
            b.whatsapp_context = json.dumps(ctx)
            log_event(session, b.id, "edit", f"Neue Mailadresse für {m.who(doc)} per WhatsApp: {addr}.", doc.id)
            return f"Danke, ich fasse bei {m.who(doc)} ab jetzt unter {addr} nach.{_next_hint(doc)}", None
    return m.HELP, None
