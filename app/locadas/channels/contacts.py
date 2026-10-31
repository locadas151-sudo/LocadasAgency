"""Endkunden als WhatsApp-Kontakte: Handynummer finden, Einwilligung, Abmeldung, 24h-Fenster.

Die Handynummer des Kunden kann aus mehreren Quellen kommen. Stufe 1 (anderer Entwickler) liest Telefonnummern
aus den Dokumenten aus und legt dafür eigene Spalten an. Wir lesen deshalb tolerant per getattr und fallen auf
unsere eigene Tabelle wa_contacts zurück (dort trägt der Betrieb die Nummer auf der Dokumentseite ein).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import Customer, Document, WaContact, utcnow
from ..notify.phone import normalize_phone

WINDOW = timedelta(hours=24) - timedelta(minutes=15)  # Puffer zum harten 24h-Limit von Meta

OPT_IN_SOURCES = {
    "customer_initiated_chat": "Kunde hat auf WhatsApp geschrieben",
    "offer_form": "Einwilligung im Anfrageformular",
    "manual": "Vom Betrieb bestätigt",
}

# Abmeldung durch den Endkunden (ganze Nachricht, Groß/Klein egal)
OPT_OUT_WORDS = {
    "stop", "stopp", "abmelden", "abbestellen", "keine nachrichten", "keine nachrichten mehr",
    "bitte keine nachrichten", "bitte keine nachrichten mehr", "unsubscribe", "austragen",
}

_DOC_ATTRS = ("customer_mobile", "customer_whatsapp", "customer_phone", "phone_mobile", "phone")
_CUSTOMER_ATTRS = ("mobile", "customer_mobile", "whatsapp_number", "phone")


def is_opt_out(text: str) -> bool:
    low = re.sub(r"[\s.!]+$", "", (text or "").strip().lower())
    low = re.sub(r"\s+", " ", low)
    return low in OPT_OUT_WORDS


def _norm(raw) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    return normalize_phone(raw) or ""


def customer_for(session: Session, doc: Document) -> Customer | None:
    email = (doc.customer_email or "").strip().lower()
    if not email:
        return None
    return session.scalar(select(Customer).where(Customer.business_id == doc.business_id, Customer.email == email))


def customer_mobile(session: Session, doc: Document) -> str:
    """Handynummer des Kunden in E.164 ohne „+“, oder "" wenn unbekannt."""
    for attr in _DOC_ATTRS:
        n = _norm(getattr(doc, attr, None))
        if n:
            return n
    c = customer_for(session, doc)
    if c is not None:
        for attr in _CUSTOMER_ATTRS:
            n = _norm(getattr(c, attr, None))
            if n:
                return n
    email = (doc.customer_email or "").strip().lower()
    if email:
        contact = session.scalar(
            select(WaContact).where(WaContact.business_id == doc.business_id, WaContact.customer_email == email)
            .order_by(WaContact.opt_in_at.is_(None), WaContact.id.desc())
        )
        if contact is not None:
            return contact.phone
    return ""


def get(session: Session, business_id: int, phone: str) -> WaContact | None:
    return session.scalar(select(WaContact).where(WaContact.business_id == business_id, WaContact.phone == phone))


def get_or_create(session: Session, business_id: int, phone: str, email: str = "", name: str = "") -> WaContact:
    c = get(session, business_id, phone)
    if c is None:
        c = WaContact(business_id=business_id, phone=phone, customer_email=(email or "").lower()[:320], name=(name or "")[:200])
        session.add(c)
        session.flush()
    else:
        if email and not c.customer_email:
            c.customer_email = email.lower()[:320]
        if name and not c.name:
            c.name = name[:200]
    return c


def record_opt_in(session: Session, business_id: int, phone: str, source: str, email: str = "", name: str = "",
                  now: datetime | None = None) -> WaContact:
    if source not in OPT_IN_SOURCES:
        raise ValueError(f"Unbekannte Opt-in-Quelle {source}")
    c = get_or_create(session, business_id, phone, email, name)
    c.wa_opt_in_source = source
    c.opt_in_at = now or utcnow()
    c.opted_out_at = None
    return c


def withdraw_opt_in(c: WaContact) -> None:
    c.wa_opt_in_source = ""
    c.opt_in_at = None


def opt_out(c: WaContact, now: datetime | None = None) -> None:
    c.opted_out_at = now or utcnow()
    c.wa_opt_in_source = ""


def in_window(c: WaContact | None, now: datetime) -> bool:
    return c is not None and c.last_inbound_at is not None and now - c.last_inbound_at < WINDOW


def may_send_template(c: WaContact | None) -> bool:
    return c is not None and bool(c.wa_opt_in_source) and c.opted_out_at is None


def open_docs_for(session: Session, business_id: int, phone: str, email: str = "") -> list[Document]:
    """Offene Dokumente zu diesem Kunden: über die Mailadresse des Kontakts oder eine Nummer am Dokument."""
    from ..db import DocStatus

    docs = list(session.scalars(
        select(Document).where(Document.business_id == business_id, Document.status == DocStatus.OPEN)
        .order_by(Document.sent_at.desc())
    ).all())
    email = (email or "").lower()
    out = []
    for d in docs:
        if email and (d.customer_email or "").lower() == email:
            out.append(d)
            continue
        if any(_norm(getattr(d, a, None)) == phone for a in _DOC_ATTRS):
            out.append(d)
    return out
