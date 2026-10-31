"""Claude-Aufrufe zählen und Kosten schätzen.

Jede Stelle, die client.messages.create aufruft, meldet danach record(...). Gezählt wird erst im Speicher und
regelmäßig in die Tabelle claude_usage geschrieben (flush). So schreibt ein Aufruf mitten in einer offenen
Schreib-Transaktion (Scan) nie über eine zweite Verbindung in SQLite und kann sich nicht selbst sperren.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import ClaudeUsage, utcnow

# Listenpreise in USD je 1 Mio. Tokens (Eingabe, Ausgabe) nach Modellfamilie. Nur eine Schätzung fürs Cockpit.
PRICES_USD = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (5.0, 25.0)}
DEFAULT_PRICE = PRICES_USD["sonnet"]

_lock = threading.Lock()
# (Tag, Zweck, Modell) -> [Aufrufe, Fehler, Eingabe-Tokens, Ausgabe-Tokens]
_pending: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])


def _day(now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(ZoneInfo(settings.timezone)).date().isoformat()


def record(purpose: str, model: str, resp=None, error: bool = False, now: datetime | None = None) -> None:
    """Einen Aufruf merken. Wirft nie (darf die Erkennung nicht stören)."""
    try:
        usage = getattr(resp, "usage", None)
        tin = int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else 0
        tout = int(getattr(usage, "output_tokens", 0) or 0) if usage is not None else 0
        with _lock:
            row = _pending[(_day(now), purpose[:30], str(model or "")[:80])]
            row[0] += 1
            row[1] += 1 if error else 0
            row[2] += tin
            row[3] += tout
    except Exception:  # pragma: no cover
        pass


def flush(session: Session) -> int:
    """Gemerkte Aufrufe in die Tabelle schreiben (eigene, kurze Transaktion des Aufrufers). Gibt die Zahl der Zeilen zurück."""
    with _lock:
        items = list(_pending.items())
        _pending.clear()
    if not items:
        return 0
    try:
        for (day, purpose, model), (calls, errors, tin, tout) in items:
            row = session.scalar(select(ClaudeUsage).where(ClaudeUsage.day == day, ClaudeUsage.purpose == purpose, ClaudeUsage.model == model))
            if row is None:
                row = ClaudeUsage(day=day, purpose=purpose, model=model, calls=0, errors=0, input_tokens=0, output_tokens=0)
                session.add(row)
            row.calls += calls
            row.errors += errors
            row.input_tokens += tin
            row.output_tokens += tout
        session.commit()
    except Exception:
        session.rollback()
        with _lock:  # nichts verlieren, beim nächsten Mal noch einmal
            for key, vals in items:
                row = _pending[key]
                for i, v in enumerate(vals):
                    row[i] += v
        raise
    return len(items)


def price(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    return next((p for fam, p in PRICES_USD.items() if fam in m), DEFAULT_PRICE)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = price(model)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def summary(session: Session, since_day: str, until_day: str | None = None) -> dict:
    """Aufrufe, Fehler, Tokens und geschätzte Kosten (USD) ab einem Tag (inklusive), nach Zweck aufgeschlüsselt."""
    q = select(ClaudeUsage).where(ClaudeUsage.day >= since_day)
    if until_day:
        q = q.where(ClaudeUsage.day <= until_day)
    out = {"calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "by_purpose": {}}
    for r in session.scalars(q).all():
        c = cost_usd(r.model, r.input_tokens, r.output_tokens)
        out["calls"] += r.calls
        out["errors"] += r.errors
        out["input_tokens"] += r.input_tokens
        out["output_tokens"] += r.output_tokens
        out["cost_usd"] += c
        p = out["by_purpose"].setdefault(r.purpose, {"calls": 0, "cost_usd": 0.0})
        p["calls"] += r.calls
        p["cost_usd"] += c
    return out
