"""Strukturierte Felder aus Angebot/Rechnung: Kunde, Nummer, Betrag, Fälligkeit.

Erst Claude (wenn ANTHROPIC_API_KEY gesetzt), sonst bzw. bei Fehler Heuristik per Regex.
Beides liefert dasselbe `Extracted`-Objekt, damit der Rest der Pipeline nichts merkt.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from types import SimpleNamespace
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
    doc_term: str = ""                 # Begriff aus dem Dokument: Angebot, Offerte, Kostenvoranschlag, Rechnung …
    iban: str = ""                     # nur mit gültiger Prüfsumme, in Vierergruppen
    bic: str = ""
    bank_name: str = ""
    account_holder: str = ""

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
  "currency": "EUR" oder "CHF" (Währung des Endbetrags, wie im Dokument),
  "doc_term": "der Begriff, mit dem das Dokument sich selbst überschreibt, genau eines von: Angebot, Offerte, Kostenvoranschlag, Rechnung, Abschlagsrechnung, Schlussrechnung, Teilrechnung",
  "account_holder": "Kontoinhaber laut Bankverbindung, falls ausdrücklich genannt, sonst leer",
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
            model=settings.claude_fast_model,
            max_tokens=2000,  # Platz für Denk-Blöcke, sonst kommt kein Text
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
        doc_term=_canon_term(str(data.get("doc_term") or ""), kind),
        account_holder=re.sub(r"\s+", " ", str(data.get("account_holder") or "")).strip()[:200],
    )


def _iso(value) -> str | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


# --- Begriff und Bankverbindung ----------------------------------------------------

OFFER_TERMS = ("Kostenvoranschlag", "Offerte", "Angebot")
INVOICE_TERMS = ("Abschlagsrechnung", "Schlussrechnung", "Teilrechnung", "Rechnung")


def _terms_for(kind) -> tuple[str, ...]:
    k = kind.value if isinstance(kind, DocKind) else str(kind)
    return OFFER_TERMS if k == "offer" else INVOICE_TERMS


def _canon_term(raw: str, kind) -> str:
    raw = (raw or "").strip().lower()
    return next((t for t in _terms_for(kind) if t.lower() == raw), "")


def detect_doc_term(kind, subject: str = "", filename: str = "", text: str = "") -> str:
    """Wie nennt das Dokument sich selbst? Überschrift im PDF zuerst, dann Betreff, dann Dateiname. Leer, wenn unklar."""
    terms = _terms_for(kind)
    lines = [ln for ln in (text or "").splitlines() if ln.strip()][:40]
    for ln in lines:
        for t in terms:
            if re.match(r"\s*(?:ihr|ihre|unser|unsere)?\s*" + t + r"\b", ln, re.I):
                return t
    for src in (subject or "", re.sub(r"[_\-.]+", " ", filename or "")):
        for t in terms:
            if re.search(r"\b" + t + r"\b", src, re.I):
                return t
    return ""


IBAN_LENGTH = {"DE": 22, "AT": 20, "CH": 21, "LI": 21}
_IBAN_CAND = re.compile(r"\b(DE|AT|CH|LI)\s?(\d{2})((?:[ \u00a0]?[0-9A-Z]){10,34})")
_BIC_RE = re.compile(r"\b(?:BIC|SWIFT)(?:[/ -]?(?:SWIFT|Code))?\s*:?\s*([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b")
_BANK_RE = re.compile(r"(?im)\b(?:bankverbindung|bank|kreditinstitut|geldinstitut)\s*:\s*([^\n·|,;:]{3,60}?)\s*(?=$|[·|,;]|\bIBAN\b|\bBIC\b)")
_HOLDER_RE = re.compile(r"(?im)\b(?:kontoinhaber(?:in)?|zahlungsempfänger|empfänger)\s*:\s*([^\n·|;:]{3,80}?)\s*(?=$|[·|;]|\bIBAN\b|\bBIC\b)")


def iban_valid(iban: str) -> bool:
    iban = re.sub(r"\s", "", iban or "").upper()
    if len(iban) != IBAN_LENGTH.get(iban[:2], -1) or not iban.isalnum():
        return False
    if iban[:2] in ("DE", "AT") and not iban[2:].isdigit():
        return False
    rearranged = iban[4:] + iban[:4]
    try:
        return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1
    except ValueError:
        return False


def format_iban(iban: str) -> str:
    compact = re.sub(r"\s", "", iban or "").upper()
    return " ".join(compact[i:i + 4] for i in range(0, len(compact), 4))


# Lastschrift: der Betrieb zieht den Betrag selbst ein, die IBAN im Dokument ist die des Kunden
_DEBIT_RE = re.compile(
    r"(?i)lastschrift|mandatsreferenz|mandats-?ref|gl(?:ä|ae)ubiger-?id|"
    r"wird\s+(?:[^\n]{0,40}\s)?(?:eingezogen|abgebucht)|abbuchung\s+(?:von|vom)\s+ihrem\s+konto"
)
# Bankblock des Kunden (Mandat, „Ihre IBAN“), nie unsere Zahlungsadresse
_CUSTOMER_BANK_CTX = re.compile(
    r"(?i)mandat|lastschrift|\bihre?\s+(?:iban|bankverbindung|kontoverbindung|bank)\b|\bihr(?:em|es)?\s+konto|"
    r"zahlungspflichtige|kunden-?iban|kunden-?konto|abbuchung|kontoinhaber(?:in)?\s*\(\s*kunde"
)
# Beschriftung unseres eigenen Bankblocks (meist in der Fußzeile)
_OWN_BANK_CTX = re.compile(
    r"(?i)bankverbindung|kontoverbindung|\biban\b|\bbank\b|kreditinstitut|geldinstitut|sparkasse|volksbank|raiffeisen|"
    r"kontoinhaber|zahlungsempf(?:ä|ae)nger|überweis|ueberweis"
)
_GENERIC_NAME_WORDS = {"gmbh", "mbh", "und", "co", "kg", "ohg", "inh", "inhaber", "e.k.", "ek", "der", "die", "das", "von", "firma"}


def _name_tokens(names) -> set[str]:
    out: set[str] = set()
    for n in names or ():
        for w in re.findall(r"[a-zäöüß]{3,}", (n or "").lower()):
            if w not in _GENERIC_NAME_WORDS:
                out.add(w)
    return out


def _holder_is_own(holder: str, own_names) -> bool:
    own = _name_tokens(own_names)
    if not own or not holder:
        return True  # nichts zum Vergleichen: nicht ablehnen
    return bool(_name_tokens([holder]) & own)


def find_iban(text: str, own_names=()) -> str:
    """IBAN (DE/AT/CH/LI) mit gültiger Prüfsumme aus dem eigenen Bankblock des Betriebs, in Vierergruppen; sonst leer.

    Nie aus einem Lastschrift-/Mandatsabschnitt des Kunden. Steht irgendwo ein Lastschrift-Hinweis, gibt es gar keine IBAN
    (der Betrieb zieht ein, der Kunde soll nichts überweisen). Ist ein Kontoinhaber genannt, muss er zum Betrieb passen.
    """
    t = text or ""
    if _DEBIT_RE.search(t):
        return ""
    for m in _IBAN_CAND.finditer(t):
        compact = re.sub(r"[ \u00a0]", "", m.group(0))
        cand = compact[: IBAN_LENGTH[m.group(1)]]
        if not iban_valid(cand):
            continue
        before = "\n".join(t[: m.start()].split("\n")[-3:])
        if _CUSTOMER_BANK_CTX.search(before):
            continue
        if not _OWN_BANK_CTX.search(before):
            continue
        around = t[max(0, m.start() - 200): m.end() + 200]
        hm = _HOLDER_RE.search(around)
        if hm and not _holder_is_own(hm.group(1), own_names):
            continue
        return format_iban(cand)
    return ""


def bank_details(text: str, own_names=()) -> dict[str, str]:
    t = text or ""
    iban = find_iban(t, own_names)
    out = {"iban": iban, "bic": "", "bank_name": "", "account_holder": ""}
    if not iban:
        return out
    m = _BIC_RE.search(t)
    if m:
        out["bic"] = m.group(1)
    m = _BANK_RE.search(t)
    if m and not re.match(r"(?i)iban|bic", m.group(1)):
        out["bank_name"] = m.group(1).strip()[:100]
    m = _HOLDER_RE.search(t)
    if m:
        out["account_holder"] = m.group(1).strip()[:200]
    return out


# --- Schon bezahlt? -----------------------------------------------------------------

_PAID_MARKERS: tuple[tuple[str, str], ...] = (
    ("Betrag dankend erhalten", r"dankend\s+erhalten|betrag\s+(?:dankend\s+)?erhalten|zahlung\s+dankend"),
    ("bereits bezahlt", r"bereits\s+(?:bezahlt|beglichen|gezahlt|überwiesen)|wurde\s+(?:bereits\s+)?(?:bar\s+)?beglichen"),
    ("bar bezahlt", r"\b(?:in\s+)?bar\s+(?:bezahlt|erhalten|kassiert|gezahlt|beglichen)|\bbarzahlung\b|zahlungsart\s*:?\s*bar\b|\bbetrag\s+bar\b"),
    ("mit EC-Karte bezahlt",
     r"(?:per|mit|über)\s+(?:ec-?\s?karte|girocard|karte|kartenzahlung)\s+(?:bezahlt|gezahlt|beglichen)|"
     r"(?:ec-?\s?karte|girocard)\s+(?:bezahlt|gezahlt)|bezahlt\s+(?:per|mit)\s+(?:ec|karte|girocard)|"
     r"zahlungsart\s*:?\s*(?:ec\b|ec-?\s?karte|girocard|kartenzahlung)|kartenzahlung\s+erfolgt"),
    ("Lastschrift", r"lastschrift|mandatsreferenz|wird\s+(?:[^\n]{0,40}\s)?(?:eingezogen|abgebucht)"),
    ("per PayPal bezahlt",
     r"(?:per|via|über|mit)\s+paypal\s+(?:bezahlt|gezahlt|beglichen|erhalten)|paypal[^\n]{0,30}(?:bezahlt|zahlung\s+erhalten)|"
     r"zahlungsart\s*:?\s*paypal"),
)
_PAID_RES = tuple((label, re.compile(rx, re.I)) for label, rx in _PAID_MARKERS)


def paid_marker(text: str) -> str:
    """Hinweis im Rechnungstext oder in der Mail, dass nichts mehr zu überweisen ist (bar, EC, Lastschrift …). Leer, wenn keiner."""
    for label, rx in _PAID_RES:
        m = rx.search(text or "")
        if m:
            return f"{label} („{re.sub(r'\s+', ' ', m.group(0)).strip()}“)"
    return ""


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


def heuristic_extract(kind: DocKind, subject: str, recipients: list[tuple[str, str]], mail_date: datetime, text: str, filename: str = "", own_names=()) -> Extracted:
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
    if re.search(r"\bCHF\b", t) and not re.search(r"€|\bEUR\b", t):
        currency = "CHF"  # Schweizer Dokument, auch wenn der Betrag oben schon gefunden wurde
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
    title = re.sub(r"(?i)\b(angebot|offerte|rechnung|abschlagsrechnung|schlussrechnung|kostenvoranschlag|ihr|ihre|unser|unsere|nr\.?|nummer|zu|vom|für|fuer)\b", " ", subject)
    title = " ".join(w for w in title.split() if not re.search(r"\d", w) and len(w) > 1)
    title = re.sub(r"\s+", " ", title).strip(" :-–")[:300]

    bank = bank_details(t, own_names) if kind == DocKind.INVOICE else {}
    return Extracted(
        kind=kind.value,
        doc_term=detect_doc_term(kind, subject, filename, t),
        iban=bank.get("iban", ""), bic=bank.get("bic", ""), bank_name=bank.get("bank_name", ""), account_holder=bank.get("account_holder", ""),
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


def extract_fields(kind: DocKind, subject: str, recipients: list[tuple[str, str]], mail_date: datetime, text: str, filename: str = "", own_names=()) -> Extracted:
    """Claude zuerst, Heuristik als Netz. Kunden-Mail kommt notfalls aus dem Mail-Empfänger."""
    heuristic = heuristic_extract(kind, subject, recipients, mail_date, text, filename, own_names)
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
    if heuristic.currency == "CHF":
        result.currency = "CHF"
    result.currency = (result.currency or "EUR").upper()
    if result.kind != heuristic.kind:
        # Claude sieht eine andere Art: Begriff passend zur Art neu bestimmen
        heuristic.doc_term = detect_doc_term(result.kind, subject, filename, text)
    # Begriff: Überschrift/Betreff schlägt Claude, Claude nur, wenn wir nichts finden
    result.doc_term = heuristic.doc_term or result.doc_term
    # Bankverbindung nur per Regex mit Prüfsumme, nie geraten
    if result.kind == "invoice":
        bank = heuristic if heuristic.iban else SimpleNamespace(**bank_details(text, own_names))
        result.iban, result.bic, result.bank_name = bank.iban, bank.bic, bank.bank_name
        result.account_holder = (bank.account_holder or result.account_holder) if result.iban else ""
    else:
        result.iban = result.bic = result.bank_name = result.account_holder = ""
    return result


def utc_from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)
