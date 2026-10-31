"""Alarme und Tagesbericht an den Admin (ADMIN_WHATSAPP, sonst Mail an ADMIN_EMAILS).

- watch(): alle 2 Minuten aus dem Scheduler. Neue Anmeldungen (Test oder verbunden), frisch verbundene Postfächer,
  Postfächer, die neu gestört sind. Alles in einer kurzen Nachricht.
- job_error(): unbehandelte Ausnahme in einem Scheduler-Job, höchstens einmal je 15 Minuten und Art.
- digest(): Tagesbericht um ADMIN_DIGEST_HOUR (19 Uhr).

WhatsApp: im 24h-Fenster (der Admin hat dem Sekretär geschrieben) als normale Nachricht, sonst als Vorlage
„event“ (dieselbe wie für Tester, Kategorie Utility). Klappt das nicht, per Mail (Systemversand bzw. über das
verbundene Postfach eines Betriebs mit derselben Adresse).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import AccountStatus, Business, MailAccount, NotificationLog, new_session, utcnow
from . import logstore, state, stats, usage

log = logging.getLogger(__name__)

RATE_SECONDS = 15 * 60
WATCH_LIST_MAX = 6
_rate: dict[str, float] = {}
_rate_lock = threading.Lock()


def enabled() -> bool:
    return bool((settings.admin_whatsapp and settings.whatsapp_enabled) or settings.admin_emails)


# --- Zustellen ------------------------------------------------------------------------------------

def note_admin_inbound(session: Session, sender: str, now: datetime | None = None) -> bool:
    """Webhook: Nachricht vom Admin-Handy öffnet das 24h-Fenster für kostenlose Sitzungsnachrichten."""
    if not settings.admin_whatsapp or sender != settings.admin_whatsapp:
        return False
    state.put(session, "admin_wa_inbound", (now or utcnow()).isoformat())
    return True


def _in_window(session: Session, now: datetime) -> bool:
    from ..notify import whatsapp as wa

    raw = state.get(session, "admin_wa_inbound")
    if not raw:
        return False
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return False
    return now - last < wa.WINDOW


def _email(session: Session, to: str, subject: str, body: str) -> bool:
    from ..notify.core import send_email, send_system_email

    if send_system_email(to, settings.admin_name, subject, body):
        return True
    b = session.scalar(select(Business).where(Business.email == to))
    if b is not None and any(a.status != AccountStatus.DISCONNECTED for a in b.accounts):
        return send_email(session, b, subject, body)
    return False


def send_admin(session: Session, title: str, text: str, kind: str = "alert", now: datetime | None = None) -> bool:
    """Eine Nachricht an den Admin. Protokolliert in notification_log (business_id leer). Commit macht der Aufrufer."""
    from ..notify import whatsapp as wa
    from ..notify.tokens import payload

    now = now or utcnow()
    channel, mode, error = "", "", ""
    if settings.admin_whatsapp and settings.whatsapp_enabled:
        try:
            if _in_window(session, now):
                wa.send_text(settings.admin_whatsapp, f"*{title}*\n\n{text}")
                mode = "session"
            else:
                name = settings.whatsapp_templates.get("event")
                if not name:
                    raise wa.WhatsAppError("Vorlage event fehlt")
                wa.send_template(settings.admin_whatsapp, name, [settings.admin_name, f"{title}: {text}"], [payload("ok")])
                mode = "template"
            channel = "whatsapp"
        except wa.WhatsAppError as exc:
            error = str(exc)[:500]
            log.info("Admin-Nachricht per WhatsApp fehlgeschlagen, nehme Mail: %s", exc)
    if not channel:
        body = f"{title}\n\n{text}\n\n{settings.base_url}/admin"
        for to in settings.admin_emails:
            try:
                if _email(session, to, f"Locadas: {title}", body):
                    channel = "email"
            except Exception as exc:  # pragma: no cover
                error = str(exc)[:500]
    session.add(NotificationLog(business_id=None, kind=f"admin:{kind}"[:20], title=title[:200], channel=channel,
                                wa_mode=mode if channel == "whatsapp" else "", ok=bool(channel), error=error if not channel else ""))
    return bool(channel)


def rate_ok(key: str, now: float | None = None) -> bool:
    """Höchstens eine Nachricht je Art in RATE_SECONDS."""
    now = time.monotonic() if now is None else now
    with _rate_lock:
        last = _rate.get(key)
        if last is not None and now - last < RATE_SECONDS:
            return False
        _rate[key] = now
        return True


def job_error(key: str, exc: BaseException | str | None, where: str = "") -> bool:
    """Unbehandelter Fehler in einem Hintergrund-Job. Wirft nie."""
    try:
        if not enabled() or not rate_ok(key):
            return False
        detail = f"{exc.__class__.__name__}: {exc}" if isinstance(exc, BaseException) else str(exc or "")
        with new_session() as s:
            ok = send_admin(s, "Fehler im Hintergrund", f"{where or key}: {detail[:300]}. Weitere Fehler dieser Art 15 Minuten still. Details im Cockpit.", kind="job")
            s.commit()
            return ok
    except Exception:  # pragma: no cover
        log.debug("Admin-Alarm nicht gesendet", exc_info=True)
        return False


def scheduler_listener(event) -> None:
    """APScheduler EVENT_JOB_ERROR."""
    job_error(f"job:{getattr(event, 'job_id', '?')}", getattr(event, "exception", None), f"Job {getattr(event, 'job_id', '?')}")


# --- Wächter ----------------------------------------------------------------------------------------

def _who(b: Business) -> str:
    parts = [b.name]
    if b.owner_name:
        parts.append(b.owner_name)
    if b.whatsapp_number:
        parts.append(f"wa.me/{b.whatsapp_number}")
    elif b.phone:
        parts.append(b.phone)
    return ", ".join(parts)


def flush_buffers(session: Session) -> None:
    for fn in (logstore.flush, usage.flush):
        try:
            fn(session)
        except Exception:
            session.rollback()
            log.debug("Puffer nicht geschrieben", exc_info=True)


def watch(session: Session | None = None, now: datetime | None = None) -> dict:
    """Neue Anmeldungen, Verbindungen und Störungen seit dem letzten Lauf melden. Erster Lauf merkt nur den Stand."""
    own = session is None
    session = session or new_session()
    try:
        return _watch(session, now or utcnow())
    finally:
        if own:
            session.close()


def _watch(session: Session, now: datetime) -> dict:
    flush_buffers(session)
    raw = state.get(session, "watch_at")
    alerted: dict = state.get(session, "err_alerted", {}) or {}
    broken = list(session.scalars(select(MailAccount).where(MailAccount.status == AccountStatus.ERROR)).all())
    current = {str(a.id): (a.error_since or a.last_failure_at or now).isoformat() for a in broken}
    if not raw:
        state.put(session, "watch_at", now.isoformat())
        state.put(session, "err_alerted", current)
        session.commit()
        return {"first": 1}
    since = datetime.fromisoformat(raw)
    lines: list[str] = []
    new_biz = list(session.scalars(select(Business).where(Business.created_at > since, Business.created_at <= now).order_by(Business.id)).all())
    new_ids = {b.id for b in new_biz}
    for b in new_biz[:WATCH_LIST_MAX]:
        kind = "Test" if b.signup_source == "testen" or b.mode == "trial" else "Postfach verbunden"
        lines.append(f"Neu ({kind}): {_who(b)}")
    if len(new_biz) > WATCH_LIST_MAX:
        lines.append(f"und {len(new_biz) - WATCH_LIST_MAX} weitere Anmeldungen")
    new_acc = list(session.scalars(select(MailAccount).where(MailAccount.created_at > since, MailAccount.created_at <= now).order_by(MailAccount.id)).all())
    conns = [a for a in new_acc if a.business_id not in new_ids]
    for a in conns[:WATCH_LIST_MAX]:
        lines.append(f"Verbunden: {_who(a.business)} ({a.provider}, {a.email})")
    if len(conns) > WATCH_LIST_MAX:
        lines.append(f"und {len(conns) - WATCH_LIST_MAX} weitere Verbindungen")
    fresh = [a for a in broken if alerted.get(str(a.id)) != current[str(a.id)]]
    for a in fresh[:WATCH_LIST_MAX]:
        lines.append(f"Postfach gestört: {a.business.name} ({a.email}): {(a.last_error or '')[:120]}")
    if len(fresh) > WATCH_LIST_MAX:
        lines.append(f"und {len(fresh) - WATCH_LIST_MAX} weitere gestörte Postfächer")
    sent = False
    if lines and enabled():
        title = "Gestörtes Postfach" if fresh and not new_biz and not conns else "Neu bei Locadas"
        sent = send_admin(session, title, "\n".join(lines), kind="watch", now=now)
    state.put(session, "watch_at", now.isoformat())
    state.put(session, "err_alerted", current)
    session.commit()
    return {"signups": len(new_biz), "connections": len(conns), "errors": len(fresh), "sent": int(sent)}


def watch_job() -> None:
    watch()


# --- Tagesbericht ---------------------------------------------------------------------------------------

def digest_text(d: dict, backup: dict | None = None) -> str:
    lines = [
        f"Anmeldungen: {d['signups']} (davon Test {d['signups_trial']}), verbunden: {d['connected']}",
        f"Dokumente erkannt: {d['docs']}, nachgefasst: {d['followups']} (Tipps {d['taps']}), Antworten: {d['replies']}",
        f"Fehler: {d['errors']}, Warnungen: {d['warnings']}, gestörte Postfächer: {d['accounts_error']}, Nachrichten nicht zugestellt: {d['notify_failed']}",
        f"Claude: {d['claude']['calls']} Aufrufe, ca. {d['claude']['cost_usd']:.2f} USD. WhatsApp: {d['wa']['template']} Vorlagen, ca. {d['wa']['cost_eur']:.2f} €",
    ]
    if backup is not None:
        lines.append(f"Sicherung: {backup.get('file', '?')} ({backup.get('at', '')[:16]})" if backup.get("ok") else "Sicherung: fehlt oder fehlgeschlagen")
    return "\n".join(lines)


def digest(session: Session | None = None, now: datetime | None = None) -> bool:
    own = session is None
    session = session or new_session()
    try:
        now = now or utcnow()
        flush_buffers(session)
        d = stats.digest(session, now)
        day = now.astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m.")
        ok = send_admin(session, f"Tagesbericht {day}", digest_text(d, state.get(session, "last_backup")), kind="digest", now=now)
        session.commit()
        return ok
    finally:
        if own:
            session.close()


def digest_job() -> None:
    if enabled():
        digest()


def jobs() -> list[tuple]:
    """(Funktion, Trigger, kwargs) für den Scheduler."""
    return [
        (watch_job, "interval", {"minutes": 2, "id": "ops_watch", "max_instances": 1, "coalesce": True}),
        (digest_job, "cron", {"hour": settings.digest_hour, "minute": 0, "timezone": settings.timezone, "id": "ops_digest",
                              "coalesce": True, "misfire_grace_time": 3600}),
    ]

