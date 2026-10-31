"""Telefonnummer des Kunden aus dem PDF-Text bzw. aus Mail-Signaturen, nie die eigenen Nummern des Betriebs.

Vorgehen im PDF:
1. Alle Nummern sammeln (deutsche, österreichische, Schweizer Schreibweisen), nach E.164 ohne „+“ normalisieren.
2. Eigene Nummern des Betriebs bestimmen: Telefon und WhatsApp aus den Einstellungen, Signatur, Nummern im
   Briefkopf (vor der Anschrift des Kunden) und in der Fußzeile. Diese fallen überall raus, auch wenn sie
   durch zusammengezogene Spalten neben der Kundenanschrift stehen.
3. Zeilen mit Bankverbindung, Steuernummer, Register, „Ihr Ansprechpartner“, „Bearbeiter“ oder dem Namen des
   Betriebs zählen nicht, Fax auch nicht.
4. Kunde: Nummer im Anschriftenblock (ab der Zeile mit dem Kundennamen) oder mit Beschriftung wie „Kunde“,
   „vor Ort“, „Bauherr“, „Mieter“. Ohne einen dieser Bezüge nehmen wir keine Nummer, lieber gar keine als die falsche.

Mobilnummern (DE 015x/016x/017x, AT 06xx, CH 07x) sind WhatsApp-fähig, Festnetz bleibt nur als Telefon gespeichert.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..notify.phone import normalize_phone

# Nummer: „+“/„00“/„0“ am Anfang, dann Zifferngruppen mit einzelnen Trennern (Leerzeichen, / . - und „(0)“)
_CAND = re.compile(r"(?<![\w+])(?:\+|00|\(?0)\d[\d()]*(?:(?:[ \t]?[/.\-][ \t]?|[ \t])\(?\d[\d()]*)*")
_DATE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{2,4}$")
_LABEL = re.compile(
    r"(?i)\b(tel(?:efon)?|fon|phone|mobil(?:telefon|nummer|funk)?|mob|handy(?:nummer)?|natel|whats\s?app|fax|telefax)\b\.?\s*(?:nr\.?|nummer)?\s*:?\s*$"
)
_MOBILE_LABEL = re.compile(r"(?i)mobil|mob\b|handy|natel|whats\s?app")
_FAX_LABEL = re.compile(r"(?i)fax")
# Ganze Zeile zählt nicht: Bank, Steuer, Register, Inhaber/Geschäftsführung (Fußzeile des Betriebs)
_SKIP_LINE = re.compile(
    r"(?i)\biban\b|\bbic\b|\bblz\b|\bkonto|\bust\b|ust-?id|ust\.?-?id|umsatzsteuer|steuer-?n|st\.?-?nr|\bhrb\b|\bhra\b|"
    r"amtsgericht|registergericht|handelsregister|geschäftsführ|gesch\.?f\.|\binhaber|sparkasse|volksbank|raiffeisen|"
    r"\bbank\b|bankverbindung|che-\d|\batu\d|mwst-?nr|\buid\b"
)
# Nummer gehört zum Betrieb
_BUSINESS_CTX = re.compile(
    r"(?i)\bihr(?:e)?\s+ansprechpartner|\bbearbeiter|sachbearbeiter|\bunser(?:e)?\s+(?:büro|zentrale|telefon|nummer|hotline)|"
    r"\bzentrale\b|\bhotline\b|\bnotdienst|\bservice-?telefon|\bbüro\b|\bwww\.|\bweb\b"
)
# Nummer gehört zum Kunden
_CUSTOMER_CTX = re.compile(
    r"(?i)\bkunde|\bkunden|\bkd\.|vor\s+ort|\bbauherr|\bauftraggeber|\bmieter|\beigentümer|\bobjekt|\bbaustelle|"
    r"rechnungsempfänger|\bansprechpartner(?:in)?\s+(?:vor\s+ort|kunde|beim\s+kunden)"
)
_NUMBER_CTX = re.compile(r"(?i)(?:nr\.?|nummer|no\.?|#|auftrag|rechnung|angebot|kundennr|kd-?nr|best(?:ell)?|ref)\s*[:.]?\s*$")
_SALUTATION = re.compile(r"(?i)^\s*(?:herrn?|frau|familie|fam\.|firma|eheleute|z\.\s?hd\.?)\b")
_GENERIC = {"gmbh", "mbh", "und", "co", "kg", "ohg", "inh", "inhaber", "der", "die", "das", "von", "firma", "familie", "herr", "herrn", "frau", "service", "ug", "ag", "gbr", "e.k."}


def is_mobile(number: str) -> bool:
    """E.164 ohne „+“: Mobilfunk in DE, AT, CH (WhatsApp-fähig)."""
    n = re.sub(r"\D", "", number or "")
    if n.startswith("49"):
        return n[2:4] in ("15", "16", "17") and 12 <= len(n) <= 13
    if n.startswith("43"):
        return n[2:3] == "6" and 11 <= len(n) <= 14
    if n.startswith("41"):
        return n[2:4] in ("75", "76", "77", "78", "79") and len(n) == 11
    return False


def country_code(text: str = "", currency: str | None = None) -> str:
    """Ländervorwahl für Nummern ohne „+“: aus Währung bzw. IBAN/Land im Dokument."""
    t = text or ""
    if (currency or "").upper() == "CHF" or re.search(r"\bCHF\b|\bSchweiz\b|\bCH\s?\d{2}\s?\d{4}", t):
        return "41"
    if re.search(r"\bAT\s?\d{2}\s?\d{4}\s?\d{4}|\bÖsterreich\b|\bATU\s?\d{8}\b|\bWien\b|\bGraz\b|\bLinz\b|\bSalzburg\b|\bInnsbruck\b", t):
        return "43"
    return "49"


def normalize(raw: str, cc: str = "49") -> str:
    """Eine Nummer nach E.164 ohne „+“ oder leer."""
    s = re.sub(r"\(\s*0\s*\)", " ", raw or "")  # +49 (0) 171 …
    s = s.replace("(", " ").replace(")", " ")
    out = normalize_phone(s, default_cc=cc)
    return out or ""


@dataclass
class Found:
    number: str           # E.164 ohne „+“
    raw: str
    line: int
    mobile: bool
    label: str = ""
    score: int = 0


def _candidates(line: str, idx: int, cc: str) -> list[Found]:
    out: list[Found] = []
    for m in _CAND.finditer(line):
        raw = m.group(0).strip(" .-/")
        digits = re.sub(r"\D", "", raw)
        if not 9 <= len(digits) <= 16 or _DATE.match(raw):
            continue
        if re.search(r"\d{1,2}\.\d{1,2}\.\d{2,4}", raw):
            continue
        before = line[: m.start()]
        if _NUMBER_CTX.search(before[-20:]):
            continue
        number = normalize(raw, cc)
        if not number:
            continue
        lm = _LABEL.search(before[-30:])
        out.append(Found(number=number, raw=raw, line=idx, mobile=is_mobile(number), label=(lm.group(1).lower() if lm else "")))
    return out


def numbers_in(text: str, cc: str = "49") -> list[Found]:
    """Alle Nummern im Text (ohne Bank-/Steuerzeilen)."""
    out: list[Found] = []
    for i, line in enumerate((text or "").splitlines()):
        if _SKIP_LINE.search(line):
            continue
        out += _candidates(line, i, cc)
    return out


def own_numbers(*sources: str, cc: str = "49") -> set[str]:
    """Nummern des Betriebs aus Einstellungen/Signatur (normalisiert)."""
    out: set[str] = set()
    for s in sources:
        if not s:
            continue
        n = normalize(s, cc) if re.fullmatch(r"[\d\s+()/.\-]+", s.strip()) else ""
        if n:
            out.add(n)
        for f in numbers_in(s, cc):
            out.add(f.number)
        # Signatur ohne Bank-Filter: auch „Tel. … · IBAN …“ in einer Zeile zählt
        for i, line in enumerate(s.splitlines()):
            for f in _candidates(line, i, cc):
                out.add(f.number)
    return out


def _tokens(names) -> set[str]:
    out: set[str] = set()
    for n in names or ():
        for w in re.findall(r"[a-zäöüß]{3,}", (n or "").lower()):
            if w not in _GENERIC:
                out.add(w)
    return out


def _has_token(line: str, tokens: set[str]) -> bool:
    low = line.lower()
    return any(re.search(r"(?<![a-zäöüß])" + re.escape(t) + r"(?![a-zäöüß])", low) for t in tokens)


def _anchor(lines: list[str], customer_name: str, footer_from: int) -> int | None:
    toks = [t for t in _tokens([customer_name]) if t not in ("kunde",)]
    # Nachname (letztes Wort) zuerst, dann jedes Namenswort, dann eine Anrede-Zeile
    order = []
    words = re.findall(r"[A-Za-zÄÖÜäöüß-]{3,}", customer_name or "")
    if words and words[-1].lower() in toks:
        order.append(words[-1].lower())
    order += [t for t in toks if t not in order]
    for t in order:
        for i, line in enumerate(lines[:footer_from]):
            if _has_token(line, {t}):
                return i
    for i, line in enumerate(lines[: min(footer_from, 25)]):
        if _SALUTATION.match(line):
            return i
    return None


@dataclass
class CustomerPhones:
    phone: str = ""    # beste Nummer (Festnetz oder Handy)
    mobile: str = ""   # beste Mobilnummer (WhatsApp)


HEADER_MAX = 8
FOOTER_LINES = 5
WINDOW = 7


def from_document(text: str, customer_name: str = "", own: set[str] | None = None, business_names=(), cc: str | None = None,
                  currency: str | None = None) -> CustomerPhones:
    """Telefon/Handy des Kunden aus dem Dokumenttext (siehe Moduldoku)."""
    t = text or ""
    cc = cc or country_code(t, currency)
    lines = t.splitlines()
    if not lines:
        return CustomerPhones()
    own = set(own or ())
    anchor = _anchor(lines, customer_name, max(1, len(lines) - 2))
    # Fußzeile: die letzten Zeilen, aber nie in den Anschriftenblock hinein (kurze Dokumente)
    footer_from = max(0, len(lines) - FOOTER_LINES)
    if anchor is not None:
        footer_from = min(len(lines), max(footer_from, anchor + WINDOW + 1))
    header_to = anchor if anchor is not None and anchor <= HEADER_MAX else min(3, len(lines))
    biz_tokens = _tokens(business_names)
    cust_tokens = _tokens([customer_name])
    found = numbers_in(t, cc)
    # Briefkopf und Fußzeile gehören dem Betrieb (außer ausdrücklich als Kunde beschriftet)
    for f in found:
        line = lines[f.line]
        if (f.line < header_to or f.line >= footer_from) and not _CUSTOMER_CTX.search(line):
            own.add(f.number)
    best: list[Found] = []
    for f in found:
        line = lines[f.line]
        if f.number in own or _FAX_LABEL.search(f.label or ""):
            continue
        if _BUSINESS_CTX.search(line):
            continue
        cust_label = bool(_CUSTOMER_CTX.search(line))
        if biz_tokens and _has_token(line, biz_tokens) and not cust_label and not (cust_tokens and _has_token(line, cust_tokens)):
            continue
        score = 0
        if cust_label:
            score += 5
        if anchor is not None and anchor <= f.line <= anchor + WINDOW and f.line < footer_from:
            score += 3
        if score <= 0:
            continue
        if f.mobile and (f.label and _MOBILE_LABEL.search(f.label)):
            score += 1
        f.score = score
        best.append(f)
    best.sort(key=lambda f: (-f.score, f.line))
    out = CustomerPhones()
    if best:
        land = next((f for f in best if not f.mobile), None)
        out.phone = (land or best[0]).number
        mob = next((f for f in best if f.mobile), None)
        out.mobile = mob.number if mob else ""
    return out


def letterhead_numbers(text: str, cc: str = "49") -> set[str]:
    """Nummern aus den ersten Zeilen (Briefkopf) und der Fußzeile, außer ausdrücklich als Kunde beschriftet."""
    lines = (text or "").splitlines()
    if not lines:
        return set()
    head = min(3, len(lines))
    foot = max(head, len(lines) - FOOTER_LINES)
    return {f.number for f in numbers_in(text, cc) if (f.line < head or f.line >= foot) and not _CUSTOMER_CTX.search(lines[f.line])}


# --- Mails ---------------------------------------------------------------------------

_QUOTE_HEAD = re.compile(
    r"(?im)^(?:>|(?:am|on)\s.{4,200}(?:schrieb|wrote)|-{2,}\s*(?:original|ursprüngliche|weitergeleitete)|(?:von|from)\s*:\s.+$)"
)


def split_quote(text: str) -> tuple[str, str]:
    """(eigener Text, zitierter Verlauf)."""
    t = (text or "").replace("\r\n", "\n")
    m = _QUOTE_HEAD.search(t)
    if not m:
        return t, ""
    return t[: m.start()], t[m.start():]


def from_mail_text(text: str, own: set[str] | None = None, cc: str = "49", signature_lines: int = 15) -> CustomerPhones:
    """Nummer aus der Signatur einer Mail (nur der nicht zitierte Teil, letzte Zeilen)."""
    own = set(own or ())
    lines = [ln.lstrip("> ").rstrip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln.strip()][-signature_lines:]
    cands = [f for f in numbers_in("\n".join(lines), cc) if f.number not in own and not _FAX_LABEL.search(f.label or "")]
    cands = [f for f in cands if not _BUSINESS_CTX.search(lines[f.line])]
    out = CustomerPhones()
    if cands:
        cands.sort(key=lambda f: (not f.mobile, -f.line))
        out.phone = next((f.number for f in cands if not f.mobile), cands[0].number)
        mob = next((f for f in cands if f.mobile), None)
        out.mobile = mob.number if mob else ""
        if not out.phone:
            out.phone = out.mobile
    return out


def from_reply(text: str, own: set[str] | None = None, cc: str = "49") -> CustomerPhones:
    """Antwort des Kunden: Signatur im eigenen Teil (Zitat mit unserer Signatur fällt weg)."""
    fresh, _ = split_quote(text)
    return from_mail_text(fresh, own, cc)


def from_thread(text: str, own: set[str] | None = None, cc: str = "49") -> CustomerPhones:
    """Eigene Mail mit Angebot: im zitierten Verlauf steht oft die Anfrage des Kunden mit seiner Signatur."""
    _, quoted = split_quote(text)
    if not quoted:
        return CustomerPhones()
    # Mehrere zitierte Mails: jede einzeln, die erste mit Nummer zählt
    parts = re.split(r"(?im)^>*\s*(?:am|on)\s.{4,200}(?:schrieb|wrote)\s*:?\s*$|^>*\s*-{2,}\s*(?:original|ursprüngliche).*$", quoted)
    for part in parts:
        res = from_mail_text(part, own, cc)
        if res.phone or res.mobile:
            return res
    return CustomerPhones()


def pretty(number: str) -> str:
    """491712345678 → +49 171 2345678 (nur fürs Auge)."""
    n = re.sub(r"\D", "", number or "")
    if not n:
        return ""
    for cc, area in (("49", 3), ("43", 3), ("41", 2)):
        if n.startswith(cc):
            rest = n[len(cc):]
            return f"+{cc} {rest[:area]} {rest[area:]}"
    return f"+{n}"
