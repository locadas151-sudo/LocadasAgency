"""Telefonnummern für WhatsApp: deutsche Schreibweisen nach E.164 ohne „+“ (0171 2345678 → 491712345678)."""
from __future__ import annotations

import re


def normalize_phone(raw: str, default_cc: str = "49") -> str | None:
    """'' bei leerer Eingabe, None wenn die Nummer nicht passt, sonst nur Ziffern mit Ländervorwahl."""
    s = (raw or "").strip()
    if not s:
        return ""
    if re.search(r"[^\d\s+()/.\-]", s):
        return None
    plus = s.lstrip().startswith("+")
    digits = re.sub(r"\D", "", s)
    if plus:
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = default_cc + digits[1:]
    elif not digits.startswith(default_cc):
        return None
    # +49 (0)171 … → die 0 nach der Ländervorwahl fällt weg
    if digits.startswith(default_cc + "0"):
        digits = default_cc + digits[len(default_cc) + 1:]
    if not 10 <= len(digits) <= 15:
        return None
    return digits


def pretty(number: str) -> str:
    return f"+{number}" if number else ""
