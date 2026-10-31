"""Versand über die eigene WhatsApp-Nummer des Betriebs (Cloud API, Coexistence mit der WhatsApp Business App).

- Vorlage (Template) an die Handynummer des Kunden: außerhalb des 24h-Fensters, nur mit Einwilligung.
- Freier Text: nur innerhalb von 24 Stunden nach der letzten Nachricht des Kunden an den Betrieb.
- Die Nachricht erscheint im Chat der WhatsApp Business App des Betriebs, Antworten kommen dort und per Webhook an.
- Fehler von Meta werden in klare Hinweise für den Betrieb übersetzt (map_error).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..crypto import decrypt
from ..db import WhatsAppConnection
from . import graph
from .config import wab_settings

log = logging.getLogger(__name__)

TEXT_MAX = 4096
PARAM_MAX = 700


class WaSendError(Exception):
    def __init__(self, err: "MappedError", raw: graph.GraphError | None = None):
        super().__init__(err.owner_text)
        self.err = err
        self.raw = raw


@dataclass(frozen=True)
class MappedError:
    code: int
    scope: str        # recipient | window | template | account | rate | transient | unknown
    owner_text: str   # klarer Hinweis für den Betrieb (Dashboard-Verlauf, ggf. WhatsApp/Mail)
    fallback_mail: bool = True  # Nachfassen stattdessen per Mail


# Meta-Fehlercodes (Cloud API). Quellen in WHATSAPP_COEXISTENCE.md.
_ERRORS: dict[int, tuple[str, str]] = {
    131047: ("window", "Der Kunde hat in den letzten 24 Stunden nicht geschrieben, deshalb ging nur eine freigegebene Vorlage. Ich fasse diesmal per Mail nach."),
    470: ("window", "Der Kunde hat in den letzten 24 Stunden nicht geschrieben, deshalb ging nur eine freigegebene Vorlage. Ich fasse diesmal per Mail nach."),
    131026: ("recipient", "Die WhatsApp-Nachricht an {phone} kam nicht an. Vielleicht hat die Nummer kein WhatsApp oder der Kunde hat unsere Nummer blockiert. Ich fasse per Mail nach."),
    131049: ("recipient", "WhatsApp hat die Nachricht an {phone} nicht zugestellt, weil Meta die Zahl der Werbenachrichten pro Kunde begrenzt und die Vorlage als Marketing einstuft. Ich fasse diesmal per Mail nach."),
    131050: ("recipient", "{phone} möchte keine Werbenachrichten mehr von dir. Ich schreibe dort nicht mehr per WhatsApp und fasse per Mail nach."),
    131021: ("recipient", "Die Kundennummer ist deine eigene WhatsApp-Nummer. Bitte trag die Handynummer des Kunden ein."),
    131051: ("unknown", "Diese Art Nachricht unterstützt WhatsApp nicht. Ich fasse per Mail nach."),
    131060: ("recipient", "WhatsApp konnte die Nachricht in deiner WhatsApp Business App nicht anzeigen. Schau bitte kurz in die App. Ich fasse per Mail nach."),
    132000: ("template", "Die Nachfass-Vorlage passt nicht mehr zu Meta (falsche Anzahl Werte). Ich fasse per Mail nach und gleiche die Vorlagen neu ab."),
    132001: ("template", "Die Nachfass-Vorlage gibt es in deinem WhatsApp-Konto nicht oder sie ist noch nicht freigegeben. Ich fasse per Mail nach."),
    132005: ("template", "Der Text ist für die Vorlage zu lang. Ich fasse per Mail nach."),
    132007: ("template", "Meta hat die Vorlage wegen ihres Inhalts nicht angenommen. Ich fasse per Mail nach."),
    132012: ("template", "Die Werte passen nicht zur Vorlage. Ich fasse per Mail nach und gleiche die Vorlagen neu ab."),
    132015: ("template", "Meta hat die Vorlage wegen schlechter Bewertungen pausiert. Ich fasse per Mail nach."),
    132016: ("template", "Meta hat die Vorlage dauerhaft gesperrt. Ich fasse per Mail nach."),
    131042: ("account", "Bei Meta ist für dein WhatsApp-Konto keine gültige Zahlungsmethode hinterlegt. Bitte im WhatsApp Manager eine Zahlungsmethode eintragen. Bis dahin fasse ich per Mail nach."),
    131031: ("account", "Meta hat dein WhatsApp-Konto gesperrt. Bitte prüfe den WhatsApp Manager. Bis dahin fasse ich per Mail nach."),
    368: ("account", "Meta hat das Senden über deine Nummer vorübergehend eingeschränkt. Bis dahin fasse ich per Mail nach."),
    190: ("account", "Die Verbindung zu deinem WhatsApp-Konto ist abgelaufen. Bitte verbinde WhatsApp in den Einstellungen neu. Bis dahin fasse ich per Mail nach."),
    200: ("account", "Locadas hat keine Berechtigung mehr für dein WhatsApp-Konto. Bitte verbinde WhatsApp in den Einstellungen neu. Bis dahin fasse ich per Mail nach."),
    10: ("account", "Locadas hat keine Berechtigung mehr für dein WhatsApp-Konto. Bitte verbinde WhatsApp in den Einstellungen neu. Bis dahin fasse ich per Mail nach."),
    133010: ("account", "Deine Nummer ist bei Meta nicht für die Cloud API registriert. Bitte verbinde WhatsApp in den Einstellungen neu. Bis dahin fasse ich per Mail nach."),
    131048: ("rate", "Meta bremst deine Nummer gerade, weil Kunden Nachrichten blockiert oder gemeldet haben. Ich fasse per Mail nach."),
    131056: ("rate", "An diesen Kunden gingen gerade zu viele Nachrichten kurz hintereinander. Ich fasse per Mail nach."),
    130429: ("rate", "Meta hat gerade zu viele Nachrichten auf einmal gebremst. Ich fasse per Mail nach."),
    131000: ("transient", "WhatsApp hatte einen unbekannten Fehler. Ich fasse diesmal per Mail nach."),
    131016: ("transient", "WhatsApp ist gerade überlastet. Ich fasse diesmal per Mail nach."),
}


def map_error(exc: graph.GraphError | None, phone: str = "") -> MappedError:
    code = exc.code if exc is not None else 0
    scope, text = _ERRORS.get(code, ("unknown", "Die WhatsApp-Nachricht ging nicht raus ({detail}). Ich fasse diesmal per Mail nach."))
    pretty = f"+{phone}" if phone else "den Kunden"
    detail = (str(exc) if exc is not None else "unbekannt")[:120]
    return MappedError(code=code, scope=scope, owner_text=text.format(phone=pretty, detail=detail))


def token(conn: WhatsAppConnection) -> str:
    return decrypt(conn.business_token_enc) if conn.business_token_enc else ""


def active_connection(session: Session, business_id: int) -> WhatsAppConnection | None:
    return session.scalar(select(WhatsAppConnection).where(
        WhatsAppConnection.business_id == business_id, WhatsAppConnection.status == "active",
    ))


def param(text: str) -> str:
    """Meta lehnt Werte mit Zeilenumbrüchen, Tabs oder mehr als 4 Leerzeichen am Stück ab."""
    flat = re.sub(r"\s*\n\s*", " ", (text or "").strip())
    flat = re.sub(r"[\t ]{2,}", " ", flat)
    if len(flat) > PARAM_MAX:
        flat = flat[: PARAM_MAX - 1].rstrip() + "…"
    return flat or "-"


def _send(conn: WhatsAppConnection, payload: dict, phone: str) -> str:
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", **payload}
    try:
        data = graph.call("POST", f"{conn.phone_number_id}/messages", token(conn), body)
    except graph.GraphError as exc:
        raise WaSendError(map_error(exc, phone), exc) from exc
    msgs = data.get("messages") or []
    wamid = msgs[0].get("id", "") if msgs and isinstance(msgs[0], dict) else ""
    if not wamid:
        raise WaSendError(map_error(None, phone))
    return wamid


def send_template(conn: WhatsAppConnection, to: str, name: str, params: list[str], button_suffix: str | None = None,
                  language: str | None = None) -> str:
    components: list[dict] = [{"type": "body", "parameters": [{"type": "text", "text": param(p)} for p in params]}]
    if button_suffix:
        components.append({"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": button_suffix}]})
    return _send(conn, {
        "to": to, "type": "template",
        "template": {"name": name, "language": {"code": language or wab_settings.template_lang}, "components": components},
    }, to)


def send_text(conn: WhatsAppConnection, to: str, text: str) -> str:
    text = (text or "").strip()
    if len(text) > TEXT_MAX:
        text = text[: TEXT_MAX - 1].rstrip() + "…"
    return _send(conn, {"to": to, "type": "text", "text": {"body": text, "preview_url": True}}, to)
