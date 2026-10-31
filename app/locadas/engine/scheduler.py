"""Hintergrund-Takt: alle paar Minuten Postfächer lesen und fällige Nachfass-Mails verschicken."""
from __future__ import annotations

import logging
import threading

from sqlalchemy import select

from ..config import settings
from ..db import AccountStatus, MailAccount, new_session
from .followups import finish_exhausted, process_due
from .scanner import scan_account

log = logging.getLogger(__name__)
cycle_lock = threading.Lock()  # ein Durchlauf zur Zeit: Scheduler, „Jetzt prüfen“ und Erst-Scan teilen ihn
_lock = cycle_lock


def run_cycle() -> dict[str, int]:
    """Ein kompletter Durchlauf. Läuft nie doppelt gleichzeitig."""
    if not _lock.acquire(blocking=False):
        return {"skipped": 1}
    totals = {"accounts": 0, "new": 0, "replies": 0, "sent": 0, "approval": 0, "finished": 0}
    try:
        with new_session() as session:
            accounts = session.scalars(select(MailAccount).where(MailAccount.status != AccountStatus.DISCONNECTED)).all()
            for account in accounts:
                totals["accounts"] += 1
                try:
                    s = scan_account(session, account)
                    totals["new"] += s["new"]
                    totals["replies"] += s["replies"]
                except Exception as exc:  # pragma: no cover
                    log.exception("Scan-Fehler %s: %s", account.email, exc)
                    session.rollback()
            try:
                s = process_due(session)
                totals["sent"] += s["sent"]
                totals["approval"] += s["approval"]
                totals["finished"] += finish_exhausted(session)
                session.commit()
            except Exception as exc:  # pragma: no cover
                log.exception("Nachfass-Fehler: %s", exc)
                session.rollback()
    finally:
        _lock.release()
    log.info("Durchlauf: %s", totals)
    return totals


def start_scheduler():
    if not settings.scheduler_enabled:
        return None
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(run_cycle, "interval", minutes=settings.scan_interval_minutes, id="cycle", max_instances=1, coalesce=True)
    sched.start()
    return sched
