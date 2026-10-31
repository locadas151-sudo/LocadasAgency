"""Strukturierte Felder aus Angebot/Rechnung: Kunde, Nummer, Betrag, Fälligkeit.

Erst Claude (wenn ANTHROPIC_API_KEY gesetzt), sonst bzw. bei Fehler Heuristik per Regex.
Beides liefert dasselbe `Extracted`-Objekt, damit der Rest der Pipeline nichts merkt.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from ..config import settings
from ..db import DocKind

log = logging.getLogger(__name__)


@dataclass
class Extracted:
    kind: str                 # "offer" | "invoice"
    customer_name: str = ""
    customer_email: str = ""
    document_number: str = ""
    title: str = ""
    amount_gross: float | None = None
    currency: str = "EUR"
    document_date: str | None = None   # ISO
    due_date: str | None = None        # ISO, Rechnungen
    valid_until: str | None = None     # ISO, Angebote
    confidence: float = 0.0
    source: str = "heuristic"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


_PROMPT = """Du bekommst den Text eines Dokuments, das ein deutscher Handwerksbetrieb per E-Mail an einen Kunden geschickt hat, plus Betreff und Empfänger der Mail.
Zieh die Felder heraus und antworte NUR mit einem JSON-Objekt, ohne Erklärung, ohne Markdown:

{
  "kind": "offer" | "invoice",
  "customer_name": "Name des Kunden/Empfängers (Person oder Firma), wie er im Dokument oder in der Mail steht",
  "customer_email": "E-Mail des Kunden, falls im Dokument; sonst leer",
  "document_number": "Angebots- bzw. Rechnungsnummer, sonst leer",
  "title": "kurze Bezeichnung der Leistung (max. 8 Wörter), z. B. 'Badsanierung EG' oder 'Austausch Gastherme'",
  "amount_gross": Bruttobetrag als Zahl ohne Tausenderpunkte, Punkt als Dezimaltrenner, oder null,
  "currency": "EUR",
  "document_date": "YYYY-MM-DD oder null",
  "due_date": "Zahlungsziel YYYY-MM-DD bei Rechnungen (aus 'zahlbar bis', 'Zahlungsziel', 'innerhalb von X Tagen' berechnet), sonst null",
  "valid_until": "Gültigkeit YYYY-MM-DD bei Angeboten, sonst null",
  "confidence": 0.0 bis 1.0, wie sicher du bei Art, Betrag und Kunde bist
}

Hinweise: 'kind' ist "offer" bei Angebot/Kostenvoranschlag, "invoice" bei Rechnung/Abschlagsrechnung/Schlussrechnung.
Der Bruttobetrag ist der Endbetrag inkl. MwSt. (Gesamtbetrag, Endbetrag, Rechnungsbetrag, zu zahlen). Bei Rechnungen mit Abzügen zählt der zu zahlende Betrag.
Wenn im Text nichts Belastbares steht, lass das Feld leer bzw. null, rate nicht.

Mail-Betreff: {subject}
Mail-Empfänger: {recipients}
Datum der Mail: {mail_date}

Dokumenttext:
<<<
{text}
>>>"""


def _claude_extract(kind_hint: DocKind | None, subject: str, recipients: str, mail_date: datetime, text: str) -> Extracted | None:
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return None
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    prompt = (
        _PROMPT.replace("{subject}", subject).replace("{recipients}", recipients)
        .replace("{mail_date}", mail_date.date().isoformat()).replace("{text}", text[:12000])
    )
    try:
        resp = client.messages.create(
            model=settings.claude_model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.warning("Claude-Extraktion fehlgeschlagen: %s", exc)
        return None
    raw = "".join(getattr(b, "text", "") for b in resp.content).strip()
    return parse_claude_json(raw, kind_hint)


def parse_claude_json(raw: str, kind_hint: DocKind | None) -> Extracted | None:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    kind = data.get("kind") if data.get("kind") in ("offer", "invoice") else (kind_hint.value if kind_hint else "offer")
    amount = data.get("amount_gross")
    try:
        amount = float(str(amount).replace(".", "").replace(",", ".")) if isinstance(amount, str) else (float(amount) if amount is not None else None)
    except ValueError:
        amount = None
    return Extracted(
        kind=kind,
        customer_name=(data.get("customer_name") or "")[:200],
        customer_email=(data.get("customer_email") or "")[:320].lower(),
        document_number=(data.get("document_number") or "")[:100],
        title=(data.get("title") or "")[:300],
        amount_gross=amount,
        currency=(data.get("currency") or "EUR")[:8],
        document_date=_iso(data.get("document_date")),
        due_date=_iso(data.get("due_date")),
        valid_until=_iso(data.get("valid_until")),
        confidence=float(data.get("confidence") or 0.5),
        source="claude",
    )


def _iso(value) -> str | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


# --- Heuristik ---------------------------------------------------------------

_AMOUNT_LABELS = (
    r"zu zahlen(?:der betrag)?", r"zahlbetrag", r"noch zu zahlen", r"rechnungsbetrag", r"endbetrag", r"gesamtbetrag",
    r"gesamt brutto", r"bruttobetrag", r"summe brutto", r"brutto", r"angebotssumme", r"gesamtsumme", r"gesamtpreis",
    r"summe", r"gesamt\b", r"total",
)
_MONEY_CH = r"(?:chf|fr\.)\s*(?P<amt>\d{1,3}(?:'\d{3})*(?:\.\d{2})|\d+\.\d{2})"
_MONEY = r"(?P<amt>\d{1,3}(?:[.\s]\d{3})*(?:,\d{2}|,-{1,2})|\d+(?:,\d{2}|,-{1,2}))\s*(?:€|eur)?"
_NUMBER_RE = re.compile(r"(?:angebots?|rechnungs?|kostenvoranschlags?|offerte|abschlagsrechnung|auftrags?)[\s-]*(?:nr\.?|nummer|no\.?|#)?\s*[:.]?\s*([A-Z0-9][A-Z0-9\-/]*\d[\w\-/]*)", re.I)
_DATE_RE = r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})"
_DUE_RE = re.compile(r"(?:zahlbar\s+(?:bis|zum)|zahlungsziel|fällig(?:keit)?(?:\s+am)?|bitte\s+(?:überweisen|zahlen)\s+(?:sie\s+)?bis(?:\s+zum)?)\s*:?\s*(?:am\s+)?" + _DATE_RE, re.I)
_DUE_DAYS_RE = re.compile(r"(?:innerhalb\s+von|zahlbar\s+innerhalb|zahlungsziel:?)\s*(\d{1,2})\s*tage", re.I)
_VALID_RE = re.compile(r"(?:gültig\s+bis|gueltig\s+bis|bindefrist(?:\s+bis)?|angebot\s+gilt\s+bis)\s*:?\s*(?:zum\s+)?" + _DATE_RE, re.I)
_VALID_DAYS_RE = re.compile(r"(?:gültig|gueltig|bindefrist)\D{0,20}?(\d{1,3})\s*tage", re.I)
_DOCDATE_RE = re.compile(r"(?:datum|vom|rechnungsdatum|angebotsdatum)\s*:?\s*" + _DATE_RE, re.I)
_DATE_RE = r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})"
_DUE_RE = re.compile(r"(?:zahlbar\s+(?:bis|zum)|zahlungsziel|fällig(?:keit)?(?:\s+am)?|bitte\s+(?:überweisen|zahlen)\s+(?:sie\s+)?bis(?:\s+zum)?)\s*:?\s*(?:am\s+)?" + _DATE_RE, re.I)
_DUE_DAYS_RE = re.compile(r"(?:innerhalb\s+von|zahlbar\s+innerhalb|zahlungsziel:?)\s*(\d{1,2})\s*tage", re.I)
_VALID_RE = re.compile(r"(?:gültig\s+bis|gueltig\s+bis|bindefrist(?:\s+bis)?|angebot\s+gilt\s+bis)\s*:?\s*(?:zum\s+)?" + _DATE_RE, re.I)
_VALID_DAYS_RE = re.compile(r"(?:gültig|gueltig|bindefrist)\D{0,20}?(\d{1,3})\s*tage", re.I)
_DOCDATE_RE = re.compile(r"(?:datum|vom|rechnungsdatum|angebotsdatum)\s*:?\s*" + _DATE_RE, re.I)


def _to_float(s: str) -> float | None:
    s = s.replace(" ", "").replace(".", "").replace(",--", ",00").replace(",-", ",00").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _to_iso(d: str, m: str, y: str) -> str | None:
    try:
        year = int(y)
        if year < 100:
            year += 2000
        return date(year, int(m), int(d)).isoformat()
    except ValueError:
        return None


def heuristic_extract(kind: DocKind, subject: str, recipients: list[tuple[str, str]], mail_date: datetime, text: str) -> Extracted:
    t = text or ""
    amount: float | None = None
    # Betrag: Zeilen mit Gesamt-Label, Netto/MwSt-Zeilen auslassen; sonst größter Betrag
    for label in _AMOUNT_LABELS:
        for m in re.finditer(r"(?im)^.*?" + label + r"[^\d\n]{0,40}" + _MONEY, t):
            line = m.group(0).lower()
            if "netto" in line or "mwst" in line or "ust" in line.split(label)[0][-12:] or "zzgl" in line:
                continue
            amount = _to_float(m.group("amt"))
            if amount:
                break
        if amount:
            break
    currency = "EUR"
    if amount is None and re.search(r"(?i)\bchf\b", t):
        currency = "CHF"
        hits = [m for m in re.finditer(r"(?im)^.*?(total inkl|total|gesamt|endbetrag|betrag)[^\n]*?" + _MONEY_CH, t)]
        if hits:
            amount = float(hits[-1].group("amt").replace("'", ""))
    if amount is None:
        candidates = [_to_float(m.group("amt")) for m in re.finditer(_MONEY, t)]
        candidates = [c for c in candidates if c]
        amount = max(candidates) if candidates else None

    number = ""
    m = _NUMBER_RE.search(subject + "\n" + t)
    if m:
        number = m.group(1).strip(".:")

    doc_date = None
    m = _DOCDATE_RE.search(t)
    if m:
        doc_date = _to_iso(*m.groups())
    base_date = date.fromisoformat(doc_date) if doc_date else mail_date.date()

    due = valid = None
    if kind == DocKind.INVOICE:
        m = _DUE_RE.search(t)
        if m:
            due = _to_iso(*m.groups())
        else:
            m = _DUE_DAYS_RE.search(t)
            days = int(m.group(1)) if m else settings.default_invoice_due_days
            due = (base_date + timedelta(days=days)).isoformat()
    else:
        m = _VALID_RE.search(t)
        if m:
            valid = _to_iso(*m.groups())
        else:
            m = _VALID_DAYS_RE.search(t)
            if m:
                valid = (base_date + timedelta(days=int(m.group(1)))).isoformat()

    name, email_addr = "", ""
    if recipients:
        name, email_addr = recipients[0]
        if not name:
            local = email_addr.split("@")[0]
            name = re.sub(r"[._-]+", " ", local).title()

    # Titel: Betreff ohne "Angebot"/"Rechnung" und Nummer
    title = re.sub(r"(?i)\b(angebot|rechnung|kostenvoranschlag|ihr|ihre|unser|unsere|nr\.?|nummer|zu|vom|für|fuer)\b", " ", subject)
    title = " ".join(w for w in title.split() if not re.search(r"\d", w) and len(w) > 1)
    title = re.sub(r"\s+", " ", title).strip(" :-–")[:300]

    return Extracted(
        kind=kind.value,
        customer_name=name[:200],
        customer_email=email_addr.lower(),
        document_number=number[:100],
        title=title,
        amount_gross=amount,
        currency=currency,
        document_date=doc_date,
        due_date=due,
        valid_until=valid,
        confidence=0.4 if amount else 0.25,
        source="heuristic",
    )


def extract_fields(kind: DocKind, subject: str, recipients: list[tuple[str, str]], mail_date: datetime, text: str) -> Extracted:
    """Claude zuerst, Heuristik als Netz. Kunden-Mail kommt notfalls aus dem Mail-Empfänger."""
    heuristic = heuristic_extract(kind, subject, recipients, mail_date, text)
    rec_str = ", ".join(f"{n} <{a}>" if n else a for n, a in recipients)
    result = _claude_extract(kind, subject, rec_str, mail_date, text) if text.strip() else None
    if result is None:
        return heuristic
    # Lücken aus der Heuristik füllen
    if not result.customer_email:
        result.customer_email = heuristic.customer_email
    if not result.customer_name:
        result.customer_name = heuristic.customer_name
    if result.amount_gross is None:
        result.amount_gross = heuristic.amount_gross
    if result.kind == "invoice" and not result.due_date:
        result.due_date = heuristic.due_date
    if not result.title:
        result.title = heuristic.title
    return result


def utc_from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)
