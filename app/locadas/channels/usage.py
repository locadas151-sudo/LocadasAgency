"""Zählen für die Abrechnung (Stufe 2) und Paket-Sperre.

Meta rechnet pro zugestellter Vorlage nach Kategorie ab (seit 01.07.2025 pro Nachricht). Seit 01.10.2026 kosten
auch freie Antworten im 24h-Fenster (Service), mit 1.000 freien Service-Nachrichten pro Nummer und Monat.
Wir zählen beim Versand mit der Kategorie, die Meta der Vorlage gegeben hat, und korrigieren, wenn der
Status-Webhook eine andere Kategorie meldet (pricing.category). Meta rechnet selbst ab, das hier ist eine Schätzung.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import Business, WaUsage, utcnow
from .config import wab_settings

CATEGORIES = ("utility", "marketing", "service")


def month_key(now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m")


def plan_allows(business: Business) -> bool:
    if not wab_settings.requires_pro:
        return True
    return (getattr(business, "plan", "") or "free") == "pro"


def _row(session: Session, business_id: int, month: str, category: str) -> WaUsage:
    row = session.scalar(select(WaUsage).where(WaUsage.business_id == business_id, WaUsage.month == month, WaUsage.category == category))
    if row is None:
        row = WaUsage(business_id=business_id, month=month, category=category, count=0)
        session.add(row)
        session.flush()
    return row


def count(session: Session, business_id: int, category: str, now: datetime | None = None, delta: int = 1) -> None:
    category = (category or "utility").lower()
    if category not in CATEGORIES:
        category = "utility"
    row = _row(session, business_id, month_key(now), category)
    row.count = max(0, (row.count or 0) + delta)


def recategorize(session: Session, business_id: int, old: str, new: str, when: datetime | None = None) -> None:
    old, new = (old or "").lower(), (new or "").lower()
    if not new or new == old or new not in CATEGORIES:
        return
    if old in CATEGORIES:
        count(session, business_id, old, when, -1)
    count(session, business_id, new, when, 1)


def month_summary(session: Session, business_id: int, now: datetime | None = None) -> dict:
    month = month_key(now)
    rows = session.scalars(select(WaUsage).where(WaUsage.business_id == business_id, WaUsage.month == month)).all()
    counts = {c: 0 for c in CATEGORIES}
    for r in rows:
        counts[r.category] = counts.get(r.category, 0) + (r.count or 0)
    paid_service = max(0, counts["service"] - wab_settings.service_free_per_month)
    eur = (
        counts["utility"] * wab_settings.price_utility_eur
        + counts["marketing"] * wab_settings.price_marketing_eur
        + paid_service * wab_settings.price_utility_eur
    )
    return {"month": month, "counts": counts, "total": sum(counts.values()), "eur": round(eur, 2)}


def eur_text(v: float) -> str:
    return f"{v:.2f}".replace(".", ",") + " €"
