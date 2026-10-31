"""Routen und Template-Helfer für Stufe 2. main.py ruft nur install(...) auf (eine Zeile)."""
from __future__ import annotations

import json
import logging

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from ..db import Business, Document, WaTemplate, WhatsAppConnection
from ..notify.phone import normalize_phone
from . import contacts, onboarding, usage
from .config import wab_settings

log = logging.getLogger(__name__)

TEMPLATE_STATUS_LABEL = {
    "APPROVED": "freigegeben", "PENDING": "in Prüfung bei Meta", "REJECTED": "abgelehnt", "PAUSED": "pausiert",
    "DISABLED": "gesperrt", "ERROR": "Fehler", "IN_APPEAL": "Einspruch läuft",
}


def connection(session: Session, business_id: int) -> WhatsAppConnection | None:
    return session.scalar(select(WhatsAppConnection).where(WhatsAppConnection.business_id == business_id))


def state(business: Business) -> dict:
    """Alles, was die Einstellungs-Karte und das Dashboard brauchen."""
    out = {"enabled": wab_settings.enabled, "plan_ok": usage.plan_allows(business), "conn": None, "templates": [], "usage": None,
           "app_id": wab_settings.app_id, "config_id": wab_settings.config_id, "graph_version": wab_settings.graph_version,
           "feature_type": wab_settings.feature_type, "session_info_version": wab_settings.session_info_version}
    session = object_session(business)
    if session is None or not wab_settings.enabled:
        return out
    conn = connection(session, business.id)
    if conn is not None and conn.status != "disconnected":
        out["conn"] = conn
        out["templates"] = list(session.scalars(select(WaTemplate).where(WaTemplate.connection_id == conn.id).order_by(WaTemplate.key)).all())
        s = usage.month_summary(session, business.id)
        s["eur_text"] = usage.eur_text(s["eur"])
        out["usage"] = s
    return out


def doc_state(doc: Document) -> dict:
    out = {"show": False}
    session = object_session(doc)
    if session is None or not wab_settings.enabled:
        return out
    conn = connection(session, doc.business_id)
    if conn is None or conn.status != "active" or not usage.plan_allows(doc.business):
        return out
    phone = contacts.customer_mobile(session, doc)
    c = contacts.get(session, doc.business_id, phone) if phone else None
    return {
        "show": True, "phone": phone, "contact": c,
        "source_label": contacts.OPT_IN_SOURCES.get(c.wa_opt_in_source, "") if c else "",
        "read_at": wa_read_at(doc),
    }


def wa_read_at(doc: Document):
    reads = [f.wa_read_at for f in getattr(doc, "followups", []) or [] if getattr(f, "wa_read_at", None)]
    return max(reads) if reads else None


def install(app, templates, get_db, require_business) -> None:
    templates.env.globals.update(
        wab_state=state, wab_doc_state=doc_state, wa_read_at=wa_read_at, WAB_TEMPLATE_STATUS=TEMPLATE_STATUS_LABEL,
    )

    @app.post("/wa-business/connect")
    async def wab_connect(request: Request, business: Business = Depends(require_business), session: Session = Depends(get_db)):
        if not wab_settings.enabled:
            raise HTTPException(404)
        b = session.get(Business, business.id)
        if not usage.plan_allows(b):
            return JSONResponse({"ok": False, "message": "WhatsApp über deine eigene Nummer gibt es im Pro-Paket."}, status_code=402)
        try:
            data = await request.json()
        except (ValueError, json.JSONDecodeError):
            form = await request.form()
            data = dict(form)
        if not isinstance(data, dict):
            data = {}
        try:
            conn = onboarding.connect(session, b, str(data.get("code") or ""), str(data.get("waba_id") or ""),
                                      str(data.get("phone_number_id") or ""), str(data.get("event") or ""))
        except onboarding.ConnectError as exc:
            session.commit()
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        session.commit()
        return {"ok": True, "message": f"Verbunden mit {conn.display_phone or 'deiner Nummer'}."}

    @app.post("/wa-business/trennen")
    def wab_disconnect(business: Business = Depends(require_business), session: Session = Depends(get_db)):
        onboarding.disconnect(session, session.get(Business, business.id))
        session.commit()
        return RedirectResponse("/einstellungen?msg=wab_getrennt#whatsapp-nachfasser", status_code=303)

    @app.post("/wa-business/vorlagen")
    def wab_sync(business: Business = Depends(require_business), session: Session = Depends(get_db)):
        conn = connection(session, business.id)
        if conn is None or conn.status == "disconnected" or not wab_settings.enabled:
            raise HTTPException(404)
        onboarding.sync_templates(session, conn)
        session.commit()
        return RedirectResponse("/einstellungen?msg=wab_vorlagen#whatsapp-nachfasser", status_code=303)

    @app.post("/wa-business/kontakt/{doc_id}")
    def wab_contact(doc_id: int, business: Business = Depends(require_business), session: Session = Depends(get_db),
                    mobile: str = Form(""), consent: str = Form("")):
        doc = session.get(Document, doc_id)
        if doc is None or doc.business_id != business.id or not wab_settings.enabled:
            raise HTTPException(404)
        phone = normalize_phone(mobile)
        if not phone:
            return RedirectResponse(f"/dokument/{doc.id}?msg=wab_nummer#whatsapp-nachfasser", status_code=303)
        c = contacts.get_or_create(session, business.id, phone, doc.customer_email, doc.customer_name)
        email = (doc.customer_email or "").lower()
        if email:
            c.customer_email = email
            for other in session.scalars(select(contacts.WaContact).where(
                contacts.WaContact.business_id == business.id, contacts.WaContact.customer_email == email, contacts.WaContact.id != c.id,
            )).all():
                other.customer_email = ""  # neue Nummer für diesen Kunden gilt ab jetzt
        if consent:
            contacts.record_opt_in(session, business.id, phone, "manual", doc.customer_email, doc.customer_name)
        elif c.wa_opt_in_source == "manual":
            contacts.withdraw_opt_in(c)
        session.commit()
        return RedirectResponse(f"/dokument/{doc.id}?msg=wab_kontakt#whatsapp-nachfasser", status_code=303)
