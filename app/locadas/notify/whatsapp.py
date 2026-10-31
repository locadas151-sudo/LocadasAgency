"""WhatsApp Cloud API (Meta Graph API), nur mit der Standardbibliothek.

Innerhalb von 24 Stunden nach der letzten Nachricht des Betriebs dürfen wir frei schreiben
(interaktive Nachricht mit bis zu 3 Antwort-Knöpfen). Außerhalb nur mit einer bei Meta
freigegebenen Vorlage (siehe WHATSAPP.md), deren Knöpfe fest beschriftet sind.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from ..config import settings
from ..db import Business

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
WINDOW = timedelta(hours=24) - timedelta(minutes=15)  # etwas Puffer zum harten 24h-Limit
BODY_MAX = 1024
TEXT_MAX = 4096
BUTTON_TITLE_MAX = 20
MAX_BUTTONS = 3
PARAM_MAX = 700


class WhatsAppError(Exception):
    pass


def http_post(url: str, payload: dict, token: str) -> dict:
    """Der einzige Netzwerkaufruf. Tests ersetzen genau diese Funktion."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise WhatsAppError(f"Graph API {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WhatsAppError(f"Graph API nicht erreichbar: {exc}") from exc


def _send(payload: dict) -> str:
    if not settings.whatsapp_enabled:
        raise WhatsAppError("WhatsApp ist nicht eingerichtet")
    url = f"{GRAPH}/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", **payload}
    data = http_post(url, body, settings.whatsapp_token or "")
    if "error" in data:
        raise WhatsAppError(str(data["error"])[:500])
    msgs = data.get("messages") or []
    return msgs[0].get("id", "") if msgs else ""


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def in_window(business: Business, now: datetime) -> bool:
    last = business.whatsapp_last_inbound_at
    return last is not None and now - last < WINDOW


def send_text(to: str, text: str) -> str:
    return _send({"to": to, "type": "text", "text": {"body": _clip(text, TEXT_MAX), "preview_url": True}})


def send_buttons(to: str, text: str, buttons: list[tuple[str, str]], footer: str = "") -> str:
    """Interaktive Nachricht mit Antwort-Knöpfen (nur im 24h-Fenster)."""
    if not buttons:
        return send_text(to, text)
    interactive: dict = {
        "type": "button",
        "body": {"text": _clip(text, BODY_MAX)},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": pid[:256], "title": label[:BUTTON_TITLE_MAX]}}
            for label, pid in buttons[:MAX_BUTTONS]
        ]},
    }
    if footer:
        interactive["footer"] = {"text": _clip(footer, 60)}
    return _send({"to": to, "type": "interactive", "interactive": interactive})


def template_param(text: str) -> str:
    """Meta lehnt Parameter mit Zeilenumbrüchen, Tabs oder mehr als 4 Leerzeichen am Stück ab."""
    flat = re.sub(r"\s*\n\s*", " · ", (text or "").strip())
    flat = re.sub(r"[\t ]{2,}", " ", flat)
    return _clip(flat, PARAM_MAX) or "-"


def send_template(to: str, name: str, params: list[str], button_payloads: list[str]) -> str:
    components: list[dict] = []
    if params:
        components.append({"type": "body", "parameters": [{"type": "text", "text": template_param(p)} for p in params]})
    for i, p in enumerate(button_payloads[:MAX_BUTTONS]):
        components.append({"type": "button", "sub_type": "quick_reply", "index": str(i), "parameters": [{"type": "payload", "payload": p}]})
    return _send({
        "to": to,
        "type": "template",
        "template": {"name": name, "language": {"code": settings.whatsapp_template_lang}, "components": components},
    })


def valid_signature(body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: sha256=<HMAC des Rohtexts mit dem App-Secret>."""
    secret = settings.whatsapp_app_secret
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:].strip())
