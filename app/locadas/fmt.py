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


def over(amount: float | None, currency: str | None = "EUR") -> str:
    """„ über 1.234,56 €“ oder leer."""
    return f" über {money(amount, currency)}" if amount else ""
