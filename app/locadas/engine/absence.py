"""Rückkehrdatum aus Abwesenheitsnotizen lesen („bis 15.11.“, „ab dem 12.11. wieder“, „until November 10“)."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

MONTHS = {
    "januar": 1, "jänner": 1, "jan": 1, "january": 1,
    "februar": 2, "feb": 2, "february": 2,
    "märz": 3, "maerz": 3, "mär": 3, "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juni": 6, "jun": 6, "june": 6,
    "juli": 7, "jul": 7, "july": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "oktober": 10, "okt": 10, "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "dez": 12, "december": 12, "dec": 12,
}
_MONTH_RX = "|".join(sorted(MONTHS, key=len, reverse=True))
_FILLER = re.compile(
    r"^[\s,:]*(?:(?:dem|den|der|the|am|on|zum|zur|einschließlich|einschliesslich|einschl\.|inkl\.|inklusive|including|and including|"
    r"montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mo\.|di\.|mi\.|do\.|fr\.)[\s,]+)*",
    re.I,
)
_DATE_PATTERNS = (
    ("num", re.compile(r"^(\d{1,2})\.\s?(\d{1,2})\.?(?:\s?(\d{4}|\d{2})(?![\d.]))?", re.I)),
    ("dm", re.compile(r"^(\d{1,2})(?:\.|st|nd|rd|th)?\s+(?:of\s+)?(" + _MONTH_RX + r")\b\.?(?:,?\s+(\d{4}))?", re.I)),
    ("md", re.compile(r"^(" + _MONTH_RX + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?", re.I)),
)
# „bis X“: X ist der letzte Abwesenheitstag. „ab X wieder“: X ist der erste Tag zurück.
_UNTIL = re.compile(r"\b(?:bis|until|till|through|thru)\b", re.I)
_BACK = re.compile(
    r"\b(?:ab|zurück(?:\s+(?:am|ab))?|wieder\s+(?:am|ab)|back(?:\s+in\s+the\s+office)?(?:\s+(?:on|from))?|return(?:ing)?(?:\s+(?:on|to\s+the\s+office\s+on))?)\b",
    re.I,
)
_BACK_CONFIRM = re.compile(r"wieder|zurück|erreichbar|im büro|im haus|back|return|available", re.I)


def _parse_date_at(text: str, ref: date) -> tuple[date, int] | None:
    """Datum am Anfang von `text` (nach Füllwörtern). Gibt (Datum, Länge) zurück."""
    m0 = _FILLER.match(text)
    off = m0.end() if m0 else 0
    rest = text[off:]
    for kind, rx in _DATE_PATTERNS:
        m = rx.match(rest)
        if not m:
            continue
        try:
            if kind == "num":
                d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
            elif kind == "dm":
                d, mo, y = int(m.group(1)), MONTHS[m.group(2).lower()], m.group(3)
            else:
                mo, d, y = MONTHS[m.group(1).lower()], int(m.group(2)), m.group(3)
            if y:
                year = int(y) + (2000 if len(y) == 2 else 0)
                result = date(year, mo, d)
            else:
                result = date(ref.year, mo, d)
                if result < ref - timedelta(days=30):
                    result = date(ref.year + 1, mo, d)
        except (ValueError, KeyError):
            continue
        return result, off + m.end()
    return None


def parse_return_date(text: str, ref: date | datetime) -> tuple[date, date] | None:
    """(genanntes Datum, erster Tag zurück) aus einer Abwesenheitsnotiz, oder None.

    Nur plausible Daten (heute bis 90 Tage). „ab X“ zählt nur, wenn danach „wieder“, „zurück“, „erreichbar“ o. Ä. steht,
    damit „ab 03.11. im Urlaub“ nicht als Rückkehr gilt.
    """
    if isinstance(ref, datetime):
        ref = ref.date()
    t = text or ""

    def ok(d: date) -> bool:
        return ref - timedelta(days=1) <= d <= ref + timedelta(days=90)

    for m in _BACK.finditer(t):
        hit = _parse_date_at(t[m.end():m.end() + 60], ref)
        if not hit:
            continue
        d, length = hit
        trigger = m.group(0).lower()
        after = re.split(r"\bund\b|\band\b|[.;!]\s", t[m.end() + length: m.end() + length + 60], maxsplit=1)[0]
        if trigger == "ab" and not _BACK_CONFIRM.search(after):
            continue
        if ok(d):
            return d, d
    for m in _UNTIL.finditer(t):
        hit = _parse_date_at(t[m.end():m.end() + 60], ref)
        if hit and ok(hit[0]):
            return hit[0], hit[0] + timedelta(days=1)
    return None
