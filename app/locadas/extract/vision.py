"""Felder aus Foto, Screenshot oder gescanntem PDF per Claude (schnelles Modell, Bild- bzw. Dokument-Block).

Für Angebote, die per WhatsApp als Foto kommen oder als PDF ohne Textebene. Liefert dasselbe `Extracted` wie
extract/fields.py plus die Telefonnummer des Kunden, falls sie auf dem Dokument steht.
Fotos werden für die Ablage in ein PDF umgewandelt (Pillow), damit der Kunden-Link /a/{token} wie gewohnt geht.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from datetime import datetime

from ..config import settings
from ..ops import usage
from .fields import Extracted, parse_claude_json

log = logging.getLogger(__name__)

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 4_500_000  # Claude nimmt bis 5 MB je Bild (Base64), darüber verkleinern

_PROMPT = """Auf dem Bild bzw. im Dokument ist ein Angebot, ein Kostenvoranschlag oder eine Rechnung eines deutschen Handwerksbetriebs an einen Kunden (Foto, Screenshot oder Scan).
Antworte NUR mit einem JSON-Objekt, ohne Erklärung, ohne Markdown:

{
  "is_document": true oder false (false, wenn es kein Angebot und keine Rechnung ist),
  "kind": "offer" | "invoice",
  "customer_name": "Name des Kunden/Empfängers (Person oder Firma), nicht der Betrieb selbst",
  "customer_email": "E-Mail des Kunden, falls sichtbar, sonst leer",
  "customer_phone": "Telefon oder Handy des Kunden, falls sichtbar (nicht die Nummer des Betriebs), sonst leer",
  "document_number": "Angebots- bzw. Rechnungsnummer, sonst leer",
  "title": "kurze Bezeichnung der Leistung (max. 8 Wörter)",
  "amount_gross": Bruttobetrag als Zahl (Punkt als Dezimaltrenner) oder null,
  "currency": "EUR" oder "CHF",
  "doc_term": "genau eines von: Angebot, Offerte, Kostenvoranschlag, Rechnung, Abschlagsrechnung, Schlussrechnung, Teilrechnung",
  "document_date": "YYYY-MM-DD oder null",
  "due_date": "Zahlungsziel YYYY-MM-DD bei Rechnungen, sonst null",
  "valid_until": "Gültigkeit YYYY-MM-DD bei Angeboten, sonst null",
  "confidence": 0.0 bis 1.0
}

Wenn etwas nicht lesbar ist, lass das Feld leer bzw. null, rate nicht.
Absender (Betrieb), falls bekannt: {own}"""


def available() -> bool:
    return bool(settings.anthropic_api_key)


def _call_claude(content: list[dict]) -> str:
    """Der einzige Netzwerkaufruf. Tests ersetzen genau diese Funktion. Kein temperature-Parameter."""
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    try:
        resp = client.messages.create(
            model=settings.claude_fast_model,
            max_tokens=2000,
            messages=[{"role": "user", "content": content}],
        )
    except Exception:
        usage.record("vision", settings.claude_fast_model, error=True)
        raise
    usage.record("vision", settings.claude_fast_model, resp)
    return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "text") == "text").strip()


def sniff(data: bytes, fallback: str = "image/jpeg") -> str:
    """Echter Bildtyp aus den ersten Bytes (WhatsApp meldet z. B. image/jpeg auch für PNG-Screenshots)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def shrink_image(data: bytes, media_type: str) -> tuple[bytes, str]:
    media_type = sniff(data, media_type)
    if len(data) <= MAX_IMAGE_BYTES and media_type in IMAGE_TYPES:
        return data, media_type
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    quality = 85
    while True:
        img.thumbnail((2400, 2400))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        out = buf.getvalue()
        if len(out) <= MAX_IMAGE_BYTES or quality < 40:
            return out, "image/jpeg"
        quality -= 15
        img = img.resize((max(1, img.width * 3 // 4), max(1, img.height * 3 // 4)))


def image_to_pdf(data: bytes) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=150.0)
    return buf.getvalue()


def extract(data: bytes, media_type: str, own_names=(), now: datetime | None = None) -> tuple[Extracted | None, str]:
    """(Felder oder None, Telefon des Kunden roh). None, wenn kein Schlüssel, Fehler oder kein Angebot/keine Rechnung."""
    if not available() or not data:
        return None, ""
    if media_type == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode()}}
    else:
        try:
            data, media_type = shrink_image(data, media_type)
        except Exception as exc:
            log.warning("Bild nicht lesbar: %s", exc)
            return None, ""
        block = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()}}
    prompt = _PROMPT.replace("{own}", ", ".join(n for n in own_names if n) or "unbekannt")
    try:
        raw = _call_claude([block, {"type": "text", "text": prompt}])
    except Exception as exc:
        log.warning("Claude-Bilderkennung fehlgeschlagen: %s", exc)
        return None, ""
    m = re.search(r"\{.*\}", raw or "", flags=re.S)
    if not m:
        return None, ""
    try:
        data_json = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, ""
    if data_json.get("is_document") is False or data_json.get("kind") not in ("offer", "invoice"):
        return None, ""
    fields = parse_claude_json(m.group(0), None)
    if fields is not None:
        fields.source = "claude_vision"
    return fields, str(data_json.get("customer_phone") or "")[:40]
