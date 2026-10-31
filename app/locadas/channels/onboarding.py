"""Embedded Signup (Coexistence): Code tauschen, App abonnieren, Vorlagen anlegen, Verbindung speichern.

Ablauf nach Meta (Quellen in WHATSAPP_COEXISTENCE.md):
1. Browser: FB.login mit config_id, response_type=code, extras.featureType=whatsapp_business_app_onboarding.
   Das Popup meldet per postMessage WA_EMBEDDED_SIGNUP mit waba_id (und meist phone_number_id), FB.login liefert `code` (30 s gültig).
2. Server: GET oauth/access_token?client_id&client_secret&code → Business Integration System User Token.
3. POST /{waba_id}/subscribed_apps → unsere App bekommt die Webhooks dieses Kontos.
4. Coexistence: Nummer NICHT registrieren (läuft schon in der App). Ohne Coexistence: POST /{phone_number_id}/register mit PIN.
5. Coexistence: innerhalb von 24 Stunden Kontakte und Verlauf anfordern (POST /{phone_number_id}/smb_app_data).
6. Vorlagen im Konto des Betriebs anlegen bzw. abgleichen (GET/POST /{waba_id}/message_templates).
"""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..crypto import encrypt
from ..db import Business, WaTemplate, WhatsAppConnection, log_event, utcnow
from . import graph
from . import wa_templates as tpl
from .config import wab_settings
from .wa_business import token

log = logging.getLogger(__name__)

COEX_EVENTS = {"FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"}
FINISH_EVENTS = {"FINISH", "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"}


class ConnectError(Exception):
    """Klarer deutscher Text für den Betrieb."""


def exchange_code(code: str) -> str:
    data = graph.call("GET", "oauth/access_token", params={
        "client_id": wab_settings.app_id or "", "client_secret": wab_settings.app_secret or "", "code": code,
    })
    tok = data.get("access_token") or data.get("_text") or ""
    if not tok:
        raise ConnectError("Meta hat keinen Zugang geliefert. Bitte noch einmal verbinden.")
    return str(tok)


def _phone_info(conn: WhatsAppConnection, tok: str) -> None:
    """Anzeigenummer, Qualität, Limit. Nicht kritisch: Fehler hier stoppen die Verbindung nicht."""
    try:
        if not conn.phone_number_id:
            data = graph.call("GET", f"{conn.waba_id}/phone_numbers", tok, params={"fields": "id,display_phone_number,verified_name,quality_rating"})
            rows = data.get("data") or []
            if not rows:
                return
            conn.phone_number_id = str(rows[0].get("id") or "")
        info = graph.call("GET", conn.phone_number_id, tok, params={
            "fields": "display_phone_number,verified_name,quality_rating,messaging_limit_tier,is_on_biz_app,platform_type",
        })
    except graph.GraphError as exc:
        log.info("Telefonnummer-Infos nicht lesbar: %s", exc)
        return
    conn.display_phone = str(info.get("display_phone_number") or conn.display_phone)[:40]
    conn.verified_name = str(info.get("verified_name") or conn.verified_name)[:200]
    conn.quality_rating = str(info.get("quality_rating") or "")[:20]
    conn.messaging_limit_tier = str(info.get("messaging_limit_tier") or "")[:40]
    if info.get("is_on_biz_app") is True:
        conn.coexistence = True


def sync_templates(session: Session, conn: WhatsAppConnection) -> dict[str, str]:
    """Unsere Vorlagen im Konto des Betriebs anlegen, vorhandene Status/Kategorien übernehmen. Gibt key → Status zurück."""
    tok = token(conn)
    lang = wab_settings.template_lang
    existing: dict[tuple[str, str], dict] = {}
    try:
        data = graph.call("GET", f"{conn.waba_id}/message_templates", tok, params={"fields": "id,name,status,category,language", "limit": "200"})
        for row in data.get("data") or []:
            existing[(str(row.get("name")), str(row.get("language")))] = row
    except graph.GraphError as exc:
        log.warning("Vorlagen nicht lesbar: %s", exc)
    out: dict[str, str] = {}
    for d in tpl.DEFS.values():
        row = existing.get((d.name, lang))
        note = ""
        if row is None:
            try:
                row = graph.call("POST", f"{conn.waba_id}/message_templates", tok, tpl.create_payload(d, lang))
            except graph.GraphError as exc:
                row = {"status": "ERROR"}
                note = f"{exc.code}: {exc}"[:500]
        rec = session.scalar(select(WaTemplate).where(WaTemplate.connection_id == conn.id, WaTemplate.name == d.name, WaTemplate.language == lang))
        if rec is None:
            rec = WaTemplate(connection_id=conn.id, key=d.key, name=d.name, language=lang)
            session.add(rec)
        rec.status = str(row.get("status") or rec.status or "PENDING").upper()[:30]
        rec.category = str(row.get("category") or rec.category or d.category).upper()[:20]
        rec.meta_id = str(row.get("id") or rec.meta_id)[:40]
        rec.note = note
        out[d.key] = rec.status
    conn.templates_synced_at = utcnow()
    return out


def template_for(session: Session, conn: WhatsAppConnection, key: str) -> WaTemplate | None:
    return session.scalar(select(WaTemplate).where(
        WaTemplate.connection_id == conn.id, WaTemplate.key == key, WaTemplate.language == wab_settings.template_lang,
    ))


def connect(session: Session, business: Business, code: str, waba_id: str, phone_number_id: str = "", event: str = "") -> WhatsAppConnection:
    """Alles nach dem Popup. Wirft ConnectError mit Text für den Betrieb."""
    waba_id = "".join(ch for ch in (waba_id or "") if ch.isdigit())
    phone_number_id = "".join(ch for ch in (phone_number_id or "") if ch.isdigit())
    if not code or not waba_id:
        raise ConnectError("Die Verbindung wurde nicht abgeschlossen. Bitte noch einmal auf „Mit WhatsApp verbinden“ tippen.")
    try:
        tok = exchange_code(code)
    except graph.GraphError as exc:
        raise ConnectError(f"Meta hat die Anmeldung abgelehnt ({exc.code or exc}). Bitte noch einmal verbinden, der Code gilt nur 30 Sekunden.") from exc

    conn = session.scalar(select(WhatsAppConnection).where(WhatsAppConnection.business_id == business.id))
    if conn is None:
        conn = WhatsAppConnection(business_id=business.id)
        session.add(conn)
    conn.waba_id, conn.phone_number_id = waba_id, phone_number_id
    conn.business_token_enc = encrypt(tok)
    conn.coexistence = (event in COEX_EVENTS) or not event
    conn.status = "pending"
    conn.last_error = ""
    session.flush()

    try:
        graph.call("POST", f"{waba_id}/subscribed_apps", tok)
    except graph.GraphError as exc:
        conn.status, conn.last_error = "error", f"Webhook-Abo fehlgeschlagen: {exc}"[:1000]
        raise ConnectError("Locadas konnte dein WhatsApp-Konto nicht abonnieren. Bitte noch einmal verbinden.") from exc

    _phone_info(conn, tok)
    if not conn.phone_number_id:
        conn.status, conn.last_error = "error", "Keine Telefonnummer im WhatsApp-Konto gefunden"
        raise ConnectError("In deinem WhatsApp-Konto ist keine Nummer. Bitte beim Verbinden deine WhatsApp-Business-Nummer auswählen.")

    if conn.coexistence:
        for sync_type in ("smb_app_state_sync", "history"):
            try:
                res = graph.call("POST", f"{conn.phone_number_id}/smb_app_data", tok, {"messaging_product": "whatsapp", "sync_type": sync_type})
                rid = str(res.get("request_id") or "")[:100]
                if sync_type == "history":
                    conn.history_request_id = rid
                else:
                    conn.contacts_request_id = rid
            except graph.GraphError as exc:  # kein Abbruch: Versand klappt auch ohne Verlauf
                log.info("Sync %s nicht angefordert: %s", sync_type, exc)
    else:
        pin = f"{secrets.randbelow(10**6):06d}"
        try:
            graph.call("POST", f"{conn.phone_number_id}/register", tok, {"messaging_product": "whatsapp", "pin": pin})
        except graph.GraphError as exc:
            conn.status, conn.last_error = "error", f"Registrierung fehlgeschlagen: {exc}"[:1000]
            raise ConnectError("Die Nummer konnte bei Meta nicht registriert werden. Bitte noch einmal verbinden.") from exc
        conn.register_pin_enc = encrypt(pin)

    sync_templates(session, conn)
    conn.status = "active"
    conn.connected_at = utcnow()
    log_event(session, business.id, "whatsapp", f"WhatsApp über deine Nummer {conn.display_phone or ''} verbunden. Die Nachfass-Vorlagen liegen bei Meta zur Prüfung.".replace("  ", " "))
    return conn


def disconnect(session: Session, business: Business) -> bool:
    conn = session.scalar(select(WhatsAppConnection).where(WhatsAppConnection.business_id == business.id))
    if conn is None or conn.status == "disconnected":
        return False
    tok = token(conn)
    if tok and conn.waba_id:
        try:
            graph.call("DELETE", f"{conn.waba_id}/subscribed_apps", tok)
        except graph.GraphError as exc:  # Trennen klappt lokal trotzdem
            log.info("Abo nicht gelöscht: %s", exc)
    conn.status = "disconnected"
    conn.business_token_enc = ""
    conn.register_pin_enc = ""
    log_event(session, business.id, "whatsapp", "WhatsApp über deine Nummer getrennt. Nachfassen läuft wieder nur per Mail.")
    return True
