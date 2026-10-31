"""Fehler-Feed: Log-Einträge ab WARNING landen in der Tabelle ops_log (Ringpuffer, die neuesten KEEP Zeilen).

Der Handler schreibt nie selbst in die Datenbank (ein Log-Aufruf mitten in einer Schreib-Transaktion würde sich
über eine zweite SQLite-Verbindung selbst sperren). Er sammelt im Speicher, flush() schreibt regelmäßig.
Betrieb dazu: logging.warning(..., extra={"business_id": 7}) oder im Scan automatisch über business_ctx.
"""
from __future__ import annotations

import contextvars
import logging
import threading
from collections import deque
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..db import OpsLog

KEEP = 1000             # Zeilen in der Tabelle
BUFFER = 500            # im Speicher bis zum nächsten flush (ältere fallen raus)
MESSAGE_MAX = 2000
_SKIP = ("locadas.ops", "sqlalchemy", "uvicorn.access", "httpx")

business_ctx: contextvars.ContextVar[int | None] = contextvars.ContextVar("locadas_business_id", default=None)

_buffer: deque[dict] = deque(maxlen=BUFFER)
_lock = threading.Lock()
_handler: "DbLogHandler | None" = None


class DbLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(_SKIP):
                return
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                msg = f"{msg} | {exc.__class__.__name__}: {exc}"
            bid = getattr(record, "business_id", None)
            if bid is None:
                bid = business_ctx.get()
            with _lock:
                _buffer.append({
                    "created_at": datetime.fromtimestamp(record.created, tz=timezone.utc),
                    "level": record.levelname[:10], "logger": record.name[:120], "message": msg[:MESSAGE_MAX],
                    "business_id": int(bid) if bid else None,
                })
        except Exception:  # pragma: no cover
            pass


def install() -> DbLogHandler:
    """Einmal am Root-Logger einhängen (mehrfacher Aufruf schadet nicht)."""
    global _handler
    root = logging.getLogger()
    if _handler is None:
        _handler = DbLogHandler()
    if _handler not in root.handlers:
        root.addHandler(_handler)
    return _handler


def pending() -> list[dict]:
    with _lock:
        return list(_buffer)


def flush(session: Session) -> int:
    with _lock:
        rows = list(_buffer)
        _buffer.clear()
    if not rows:
        return 0
    try:
        session.add_all(OpsLog(**r) for r in rows)
        session.flush()
        top = session.scalar(select(func.max(OpsLog.id))) or 0
        if top > KEEP:
            session.execute(delete(OpsLog).where(OpsLog.id <= top - KEEP))
        session.commit()
    except Exception:
        session.rollback()
        with _lock:
            _buffer.extendleft(reversed(rows))
        raise
    return len(rows)


def recent(session: Session, limit: int = 200, business_id: int | None = None) -> list[OpsLog]:
    q = select(OpsLog)
    if business_id is not None:
        q = q.where(OpsLog.business_id == business_id)
    return list(session.scalars(q.order_by(OpsLog.id.desc()).limit(limit)).all())
