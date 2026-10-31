"""Schlüssel-Wert-Speicher in der Tabelle ops_state (JSON-Werte)."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..db import OpsState, utcnow


def get(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(OpsState, key)
    if row is None or not row.value:
        return default
    try:
        return json.loads(row.value)
    except ValueError:
        return default


def put(session: Session, key: str, value: Any) -> None:
    row = session.get(OpsState, key)
    raw = json.dumps(value, ensure_ascii=False, default=str)
    if row is None:
        session.add(OpsState(key=key, value=raw, updated_at=utcnow()))
    else:
        row.value = raw
        row.updated_at = utcnow()
