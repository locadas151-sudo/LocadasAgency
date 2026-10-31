"""Werktage: Mo–Fr ohne bundesweite Feiertage. An diesen Tagen geht keine Nachfass-Mail raus."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache


def easter(year: int) -> date:
    """Ostersonntag (gregorianisch, anonymer Algorithmus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=64)
def federal_holidays(year: int) -> dict[date, str]:
    e = easter(year)
    return {
        date(year, 1, 1): "Neujahr",
        e - timedelta(days=2): "Karfreitag",
        e + timedelta(days=1): "Ostermontag",
        date(year, 5, 1): "Tag der Arbeit",
        e + timedelta(days=39): "Christi Himmelfahrt",
        e + timedelta(days=50): "Pfingstmontag",
        date(year, 10, 3): "Tag der Deutschen Einheit",
        date(year, 12, 25): "1. Weihnachtstag",
        date(year, 12, 26): "2. Weihnachtstag",
    }


def _d(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def is_holiday(value: date | datetime) -> bool:
    d = _d(value)
    return d in federal_holidays(d.year)


def is_workday(value: date | datetime) -> bool:
    d = _d(value)
    return d.weekday() < 5 and not is_holiday(d)


def next_workday(value: date, include_today: bool = False) -> date:
    d = value if include_today else value + timedelta(days=1)
    while not is_workday(d):
        d += timedelta(days=1)
    return d


def add_workdays(value: date, n: int) -> date:
    d = value
    for _ in range(n):
        d = next_workday(d)
    return d
