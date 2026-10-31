"""Einfaches Token-Bucket pro IP, im Speicher. Reicht für einen einzelnen App-Prozess."""
from __future__ import annotations

import threading
import time

# Name → (Kapazität, Sekunden pro neuem Token)
LIMITS: dict[str, tuple[int, float]] = {
    "login": (5, 60.0),       # 5 sofort, dann einer pro Minute
    "verbinden": (6, 120.0),  # Passwort-Versuche gegen fremde Mailserver bremsen
    "check": (10, 300.0),     # Claude-Aufrufe für den Angebots-Check
}

_lock = threading.Lock()
_buckets: dict[tuple[str, str], tuple[float, float]] = {}  # (name, ip) → (tokens, zuletzt)


def allow(name: str, ip: str, now: float | None = None) -> bool:
    cap, per = LIMITS[name]
    now = time.monotonic() if now is None else now
    key = (name, ip or "?")
    with _lock:
        tokens, last = _buckets.get(key, (float(cap), now))
        tokens = min(float(cap), tokens + (now - last) / per)
        if tokens < 1.0:
            _buckets[key] = (tokens, now)
            return False
        _buckets[key] = (tokens - 1.0, now)
        if len(_buckets) > 50_000:  # nicht unbegrenzt wachsen
            _buckets.clear()
        return True


def reset() -> None:
    with _lock:
        _buckets.clear()
