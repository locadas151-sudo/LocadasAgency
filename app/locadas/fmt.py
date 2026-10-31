"""Beträge und Daten so formatieren, wie der Kunde sie aus seinem Dokument kennt."""
from __future__ import annotations


def money(amount: float | None, currency: str | None = "EUR") -> str:
    """EUR: „1.234,56 €“, CHF: „CHF 1'234.56“, sonst „1.234,56 USD“."""
    if amount is None:
        return ""
    cur = (currency or "EUR").upper().strip()
    if cur in ("CHF", "SFR", "FR."):
        return "CHF " + f"{amount:,.2f}".replace(",", "'")
    de = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if cur in ("EUR", "€", ""):
        return f"{de} €"
    return f"{de} {cur}"


def dativ(name: str) -> str:
    """„an Herrn Weber“ statt „an Herr Weber“ (nur die Anrede wird gebeugt)."""
    n = (name or "").strip()
    if n[:5].lower() == "herr ":
        return "Herrn " + n[5:]
    if n[:4].lower() == "hr. ":
        return "Herrn " + n[4:]
    return n


def possessive(kind_label: str) -> str:
    """„dein Angebot“, „deine Rechnung“, „deinen Kostenvoranschlag“ (Akkusativ)."""
    k = (kind_label or "").strip()
    low = k.lower()
    if low.endswith("rechnung") or low == "offerte":
        return f"deine {k}"
    if low == "kostenvoranschlag":
        return f"deinen {k}"
    return f"dein {k}"


def surname_form(rest: str) -> str:
    """Nach „Herr“/„Frau“: Vornamen weglassen, Titel und Namenszusätze behalten.

    „Peter Weber“ → „Weber“, „Dr. Anna Meier“ → „Dr. Meier“, „Klaus von der Heide“ → „von der Heide“."""
    import re

    toks = (rest or "").split()
    titles = [t for t in toks if re.match(r"(?i)^(dr|prof|dipl|ing)\b\.?", t)]
    names = [t for t in toks if t not in titles]
    if len(names) <= 1:
        return " ".join(toks)
    for i, t in enumerate(names[1:], 1):
        if t.lower() in ("von", "van", "de", "zu", "vom", "zur", "ter", "den", "der", "di", "da", "le", "la", "du"):
            return " ".join(titles + names[i:])
    return " ".join(titles + [names[-1]])


def over(amount: float | None, currency: str | None = "EUR") -> str:
    """„ über 1.234,56 €“ oder leer."""
    return f" über {money(amount, currency)}" if amount else ""
