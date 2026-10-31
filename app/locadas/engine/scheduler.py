"""Hintergrund-Takt: alle paar Minuten Postfächer lesen und fällige Nachfass-Mails verschicken.

Postfächer werden parallel gelesen (SCAN_WORKERS Threads, Standard 8). Jedes Postfach hat eine eigene Sperre,
damit es nie zweimal gleichzeitig läuft (Durchlauf, „Jetzt prüfen“, Nachzügler aus dem letzten Durchlauf).
Hängt ein Postfach länger als SCAN_ACCOUNT_TIMEOUT Sekunden, wartet der Durchlauf nicht weiter darauf: es behält
seine Sperre, bis es fertig ist, und wird bis dahin übersprungen. Versand (process_due) läuft danach in einem Rutsch.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from sqlalchemy import select

from ..config import settings
from ..db import AccountStatus, MailAccount, new_session
from .followups import finish_exhausted, process_due
from . import summary
from .scanner import scan_account
from .trial import cleanup_job as trial_cleanup_job

log = logging.getLogger(__name__)
cycle_lock = threading.Lock()  # ein Durchlauf zur Zeit: Scheduler, „Jetzt prüfen“ und Erst-Scan teilen ihn
_lock = cycle_lock

last_cycle_at: float | None = None  # time.time() des letzten fertigen Durchlaufs (Health-Check)
last_cycle_stats: dict = {}

_account_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_executor_guard = threading.Lock()


def account_lock(account_id: int) -> threading.Lock:
    """Sperre je Postfach (für Durchlauf, „Jetzt prüfen“ und Erst-Scan)."""
    with _locks_guard:
        lock = _account_locks.get(account_id)
        if lock is None:
            lock = _account_locks[account_id] = threading.Lock()
        return lock


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _executor_guard:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=max(1, settings.scan_workers), thread_name_prefix="scan")
        return _executor


def scan_one(account_id: int, started: dict[int, float] | None = None, force: bool = False) -> dict[str, int]:
    """Ein Postfach lesen, in eigener Session. Läuft das Postfach schon, wird es übersprungen ({"skipped": 1})."""
    from ..ops import logstore

    lock = account_lock(account_id)
    if not lock.acquire(blocking=False):
        return {"skipped": 1}
    if started is not None:
        started[account_id] = time.monotonic()
    token = None
    try:
        with new_session() as session:
            account = session.get(MailAccount, account_id)
            if account is None or account.status == AccountStatus.DISCONNECTED:
                return {}
            token = logstore.business_ctx.set(account.business_id)
            try:
                return scan_account(session, account, force=force)
            except Exception as exc:
                session.rollback()
                log.exception("Scan-Fehler %s: %s", account.email, exc)
                from ..ops import alerts

                alerts.job_error("scan", exc, "Scan")
                return {"failed": 1}
    finally:
        if token is not None:
            logstore.business_ctx.reset(token)
        lock.release()


def scan_accounts(account_ids: list[int], timeout: float | None = None) -> dict[str, int]:
    """Postfächer parallel lesen. Gibt Summen zurück, timed_out = Postfächer, auf die nicht mehr gewartet wurde."""
    timeout = settings.scan_account_timeout if timeout is None else timeout
    totals = {"accounts": 0, "new": 0, "replies": 0, "skipped": 0, "failed": 0, "timed_out": 0}
    if not account_ids:
        return totals
    started: dict[int, float] = {}
    pool = _pool()
    pending: dict[Future, int] = {pool.submit(scan_one, i, started): i for i in account_ids}
    while pending:
        done, _ = wait(list(pending), timeout=0.5, return_when=FIRST_COMPLETED)
        for f in done:
            pending.pop(f)
            try:
                s = f.result()
            except Exception as exc:  # pragma: no cover (scan_one fängt selbst)
                log.exception("Scan-Worker: %s", exc)
                totals["failed"] += 1
                continue
            totals["accounts"] += 0 if s.get("skipped") else 1
            for k in ("new", "replies", "skipped", "failed"):
                totals[k] += int(s.get(k, 0) or 0)
        now = time.monotonic()
        for f, aid in list(pending.items()):
            t0 = started.get(aid)
            if t0 is not None and now - t0 > timeout:
                pending.pop(f)
                totals["timed_out"] += 1
                log.warning("Postfach %s braucht länger als %ss, Durchlauf wartet nicht weiter", aid, int(timeout))
    return totals


def run_cycle() -> dict[str, int]:
    """Ein kompletter Durchlauf. Läuft nie doppelt gleichzeitig."""
    global last_cycle_at, last_cycle_stats
    if not _lock.acquire(blocking=False):
        return {"skipped": 1}
    totals = {"accounts": 0, "new": 0, "replies": 0, "sent": 0, "approval": 0, "finished": 0}
    try:
        with new_session() as session:
            ids = list(session.scalars(select(MailAccount.id).where(MailAccount.status != AccountStatus.DISCONNECTED)).all())
        s = scan_accounts(ids)
        totals["accounts"] = s["accounts"]
        totals["new"], totals["replies"] = s["new"], s["replies"]
        for k in ("skipped", "failed", "timed_out"):
            if s[k]:
                totals[k] = s[k]
        with new_session() as session:
            try:
                d = process_due(session)
                totals["sent"] += d["sent"]
                totals["approval"] += d["approval"]
                totals["finished"] += finish_exhausted(session)
                session.commit()
            except Exception as exc:  # pragma: no cover
                log.exception("Nachfass-Fehler: %s", exc)
                session.rollback()
                from ..ops import alerts

                alerts.job_error("followups", exc, "Nachfassen")
        last_cycle_at = time.time()
        last_cycle_stats = dict(totals)
    finally:
        _lock.release()
    log.info("Durchlauf: %s", totals)
    return totals


def start_scheduler():
    if not settings.scheduler_enabled:
        return None
    from apscheduler.schedulers.background import BackgroundScheduler

    from ..ops import alerts, backup

    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(trial_cleanup_job, "interval", hours=12, id="trial_cleanup", max_instances=1, coalesce=True)  # Tester nach 60 Tagen ohne Aktivität löschen
    sched.add_job(run_cycle, "interval", minutes=settings.scan_interval_minutes, id="cycle", max_instances=1, coalesce=True)
    sched.add_job(summary.run_monthly, "cron", day=1, hour=summary.SEND_HOUR, minute=0, timezone=settings.timezone, id="summary", coalesce=True, misfire_grace_time=6 * 3600)
    # Betrieb: Alarme an den Admin, Tagesbericht, nächtliche Sicherung
    for fn, trigger, kw in alerts.jobs():
        sched.add_job(fn, trigger, **kw)
    sched.add_job(backup.backup_job, "cron", hour=3, minute=15, timezone=settings.timezone, id="backup", coalesce=True, misfire_grace_time=6 * 3600)
    if hasattr(sched, "add_listener"):
        from apscheduler.events import EVENT_JOB_ERROR

        sched.add_listener(alerts.scheduler_listener, EVENT_JOB_ERROR)
    sched.start()
    return sched
