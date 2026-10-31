"""GET /healthz: Datenbank, Scheduler, Alter des letzten Durchlaufs, SMTP-Empfänger. 200 oder 503 mit JSON."""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

from sqlalchemy import text

from ..config import settings
from ..db import get_engine


@dataclass
class _State:
    scheduler: object | None = None
    inbound: object | None = None
    started: float = field(default_factory=time.time)


state = _State()


def _db() -> tuple[bool, str]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:
        return False, str(exc)[:200]


def _smtp_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def check() -> tuple[bool, dict]:
    from ..engine import scheduler as sched_mod

    out: dict = {}
    ok = True
    db_ok, db_msg = _db()
    out["db"] = db_msg
    ok &= db_ok

    max_age = max(3 * settings.scan_interval_minutes * 60, 15 * 60)
    if settings.scheduler_enabled:
        running = bool(getattr(state.scheduler, "running", False))
        out["scheduler"] = "running" if running else "stopped"
        ok &= running
        last = sched_mod.last_cycle_at
        if last is None:
            age = time.time() - state.started
            out["last_cycle_age_s"] = None
            ok &= age < max_age  # nach dem Start kommt der erste Durchlauf erst nach SCAN_INTERVAL_MINUTES
        else:
            age = time.time() - last
            out["last_cycle_age_s"] = int(age)
            ok &= age < max_age
    else:
        out["scheduler"] = "disabled"

    if settings.inbound_smtp_port:
        port = getattr(state.inbound, "port", None) or settings.inbound_smtp_port
        listening = _smtp_listening("127.0.0.1", int(port))
        out["inbound_smtp"] = "listening" if listening else "down"
        ok &= listening
    else:
        out["inbound_smtp"] = "disabled"
    out["ok"] = bool(ok)
    return bool(ok), out
