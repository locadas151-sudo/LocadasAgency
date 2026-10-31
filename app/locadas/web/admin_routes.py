"""Admin-Cockpit /admin für den Betrieb von Locadas (Kennzahlen, Funnel, Betriebe, Fehler) und GET /healthz.

Zugang nur für Adressen aus ADMIN_EMAILS: angemeldeter Betrieb mit dieser Adresse (Login-Cookie) oder Magic-Link
an eine dieser Adressen (/admin/login). Alles andere unter /admin antwortet 404. Formulare tragen ein signiertes
CSRF-Feld, das Admin-Cookie ist SameSite=Strict. Seiten sind noindex und werden nicht gecacht.
Eingebunden aus main.py über install(), damit main.py schlank bleibt.
"""
from __future__ import annotations

import hmac
import logging
import threading
from datetime import timedelta
from typing import Callable
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import AccountStatus, Business, Document, Event, MailAccount, NotificationLog, log_event, new_session, utcnow
from ..ops import alerts, backup, health, logstore, stats
from . import auth, ratelimit

log = logging.getLogger(__name__)

COOKIE = "locadas_admin"
COOKIE_MAX_AGE = 60 * 60 * 12   # 12 Stunden
LINK_MAX_AGE = 60 * 60 * 2      # Magic-Link 2 Stunden
CSRF_MAX_AGE = 60 * 60 * 12


def _is_admin_email(email: str | None) -> bool:
    return bool(email) and email.strip().lower() in settings.admin_emails


def link_token(email: str) -> str:
    return auth._serializer("admin-link").dumps({"e": email.strip().lower()})


def _from_link(token: str) -> str | None:
    try:
        data = auth._serializer("admin-link").loads(token, max_age=LINK_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    e = str(data.get("e", ""))
    return e if _is_admin_email(e) else None


def _cookie_value(email: str) -> str:
    return auth._serializer("admin").dumps({"e": email})


def csrf_token(email: str) -> str:
    return auth._serializer("admin-csrf").dumps({"e": email})


def _csrf_ok(token: str, email: str) -> bool:
    try:
        data = auth._serializer("admin-csrf").loads(token or "", max_age=CSRF_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return hmac.compare_digest(str(data.get("e", "")), email)


def admin_email(request: Request, session: Session) -> str | None:
    """Adresse des Admins oder None. Ohne ADMIN_EMAILS gibt es kein Cockpit."""
    if not settings.admin_emails:
        return None
    raw = request.cookies.get(COOKIE)
    if raw:
        try:
            e = str(auth._serializer("admin").loads(raw, max_age=COOKIE_MAX_AGE).get("e", ""))
            if _is_admin_email(e):
                return e
        except (BadSignature, SignatureExpired):
            pass
    b = auth.business_from_token(session, request.cookies.get(auth.COOKIE))
    if b is None:
        return None
    for e in [b.owner_email] + [a.email for a in b.accounts if a.status != AccountStatus.DISCONNECTED]:
        if _is_admin_email(e):
            return e.strip().lower()
    return None


def _same_origin(request: Request) -> bool:
    """POST nur von der eigenen Seite (zusätzlich zum CSRF-Feld)."""
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not origin:
        return True  # manche Browser schicken keins, das CSRF-Feld reicht dann
    host = urlparse(origin).netloc
    return host in {request.headers.get("host", ""), urlparse(settings.base_url).netloc}


def _private(resp: Response) -> Response:
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def install(app: FastAPI, render: Callable, get_db: Callable, current_business: Callable, client_ip: Callable) -> None:

    def require_admin(request: Request, session: Session = Depends(get_db)) -> str:
        email = admin_email(request, session)
        if email is None:
            raise HTTPException(404)
        return email

    async def require_admin_post(request: Request, session: Session = Depends(get_db)) -> str:
        email = admin_email(request, session)
        if email is None:
            raise HTTPException(404)
        form = await request.form()
        if not _same_origin(request) or not _csrf_ok(str(form.get("csrf", "")), email):
            raise HTTPException(403, "Formular abgelaufen. Bitte Seite neu laden.")
        return email

    def page(request: Request, name: str, email: str, **ctx) -> Response:
        ctx.setdefault("business", None)
        return _private(render(request, name, admin=email, csrf=csrf_token(email), **ctx))

    # --- Health ------------------------------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        ok, data = health.check()
        return JSONResponse(data, status_code=200 if ok else 503, headers={"Cache-Control": "no-store"})

    # --- Login per Magic-Link ------------------------------------------------------------------------

    @app.get("/admin/login")
    def admin_login_form(request: Request, gesendet: str = ""):
        if not settings.admin_emails:
            raise HTTPException(404)
        return _private(render(request, "admin_login.html", business=None, sent=bool(gesendet)))

    @app.post("/admin/login")
    def admin_login_submit(request: Request, session: Session = Depends(get_db), email: str = Form("")):
        if not settings.admin_emails:
            raise HTTPException(404)
        if not ratelimit.allow("admin_login", client_ip(request)):
            return _private(render(request, "error.html", business=None, title="Kurz durchatmen", message="Zu viele Versuche. Bitte warte ein paar Minuten.", status_code=429))
        e = email.strip().lower()
        if _is_admin_email(e):
            link = f"{settings.base_url}/admin/login/{link_token(e)}"
            ok = alerts.send_admin(session, "Admin-Login", f"Dein Link ins Cockpit (2 Stunden gültig): {link}", kind="login")
            session.commit()
            log.info("Admin-Link angefordert (%s)", "zugestellt" if ok else "nicht zugestellt")
        return RedirectResponse("/admin/login?gesendet=1", status_code=303)  # gleiche Antwort für jede Adresse

    @app.get("/admin/login/{token}")
    def admin_login_link(token: str):
        e = _from_link(token)
        if e is None:
            raise HTTPException(404)
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(COOKIE, _cookie_value(e), max_age=COOKIE_MAX_AGE, httponly=True, samesite="strict",
                        secure=settings.base_url.startswith("https"), path="/admin")
        return _private(resp)

    @app.get("/admin/logout")
    def admin_logout(email: str = Depends(require_admin)):
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(COOKIE, path="/admin")
        return _private(resp)

    # --- Übersicht -----------------------------------------------------------------------------------

    @app.get("/admin")
    def admin_home(request: Request, email: str = Depends(require_admin), session: Session = Depends(get_db)):
        from ..engine import scheduler as sched

        alerts.flush_buffers(session)
        now = utcnow()
        k = stats.overview(session, now)
        recent = list(session.scalars(select(Business).order_by(Business.created_at.desc()).limit(10)).all())
        ok, hz = health.check()
        return page(
            request, "admin.html", email, k=k, funnel=stats.funnel(session), funnel_7d=stats.funnel(session, now - timedelta(days=7)),
            recent=recent, backups=backup.list_backups()[:5], last_backup=_last_backup(session), health_ok=ok, hz=hz,
            cycle=sched.last_cycle_stats, alerts_on=alerts.enabled(),
        )

    def _last_backup(session: Session) -> dict | None:
        from ..ops import state

        return state.get(session, "last_backup")

    # --- Betriebe ------------------------------------------------------------------------------------

    @app.get("/admin/betriebe")
    def admin_list(request: Request, email: str = Depends(require_admin), session: Session = Depends(get_db), q: str = "", f: str = "alle"):
        alerts.flush_buffers(session)
        flt = f if f in stats.FILTERS else "alle"
        return page(request, "admin_list.html", email, rows=stats.businesses(session, q, flt), q=q, f=flt, filters=stats.FILTERS)

    def _business(session: Session, business_id: int) -> Business:
        b = session.get(Business, business_id)
        if b is None:
            raise HTTPException(404)
        return b

    @app.get("/admin/betrieb/{business_id}")
    def admin_detail(request: Request, business_id: int, email: str = Depends(require_admin), session: Session = Depends(get_db), msg: str = ""):
        alerts.flush_buffers(session)
        b = _business(session, business_id)
        docs = list(session.scalars(select(Document).where(Document.business_id == b.id).order_by(Document.created_at.desc()).limit(100)).all())
        events = list(session.scalars(select(Event).where(Event.business_id == b.id).order_by(Event.created_at.desc()).limit(60)).all())
        notes = list(session.scalars(select(NotificationLog).where(NotificationLog.business_id == b.id).order_by(NotificationLog.id.desc()).limit(60)).all())
        accounts = list(session.scalars(select(MailAccount).where(MailAccount.business_id == b.id).order_by(MailAccount.id.desc())).all())
        return page(request, "admin_detail.html", email, b=b, docs=docs, events=events, notes=notes, accounts=accounts,
                    errors=logstore.recent(session, 50, b.id), msg=msg, phone=stats._phone(b))

    @app.post("/admin/betrieb/{business_id}/pause")
    def admin_pause(business_id: int, email: str = Depends(require_admin_post), session: Session = Depends(get_db)):
        b = _business(session, business_id)
        b.paused = not b.paused
        log_event(session, b.id, "admin", "Nachfassen pausiert (Locadas-Support)." if b.paused else "Nachfassen wieder aktiv (Locadas-Support).")
        session.commit()
        log.info("Admin %s: Betrieb %s %s", email, b.id, "pausiert" if b.paused else "fortgesetzt")
        return _private(RedirectResponse(f"/admin/betrieb/{b.id}?msg={'pausiert' if b.paused else 'aktiv'}", status_code=303))

    @app.post("/admin/betrieb/{business_id}/scan")
    def admin_scan(business_id: int, email: str = Depends(require_admin_post), session: Session = Depends(get_db)):
        from ..engine import scheduler as sched

        b = _business(session, business_id)
        ids = [a.id for a in b.accounts if a.status != AccountStatus.DISCONNECTED]
        if not ids:
            return _private(RedirectResponse(f"/admin/betrieb/{b.id}?msg=kein_postfach", status_code=303))

        def run() -> None:
            for i in ids:
                sched.scan_one(i, force=True)

        threading.Thread(target=run, daemon=True, name=f"admin-scan-{b.id}").start()
        return _private(RedirectResponse(f"/admin/betrieb/{b.id}?msg=scan", status_code=303))

    @app.post("/admin/betrieb/{business_id}/login-link")
    def admin_login_link_resend(business_id: int, email: str = Depends(require_admin_post), session: Session = Depends(get_db)):
        from ..notify import send as notify
        from .trial_routes import login_notification

        b = _business(session, business_id)
        ok = notify(session, login_notification(b))
        session.commit()
        return _private(RedirectResponse(f"/admin/betrieb/{b.id}?msg={'link_ok' if ok else 'link_fehler'}", status_code=303))

    @app.post("/admin/betrieb/{business_id}/notiz")
    def admin_note(business_id: int, email: str = Depends(require_admin_post), session: Session = Depends(get_db),
                   admin_note: str = Form(""), onboarded: str = Form("")):
        b = _business(session, business_id)
        b.admin_note = admin_note.strip()[:5000]
        if onboarded and b.onboarded_at is None:
            b.onboarded_at = utcnow()
        elif not onboarded:
            b.onboarded_at = None
        session.commit()
        return _private(RedirectResponse(f"/admin/betrieb/{b.id}?msg=notiz", status_code=303))

    # --- Fehler --------------------------------------------------------------------------------------

    @app.get("/admin/fehler")
    def admin_errors(request: Request, email: str = Depends(require_admin), session: Session = Depends(get_db), level: str = ""):
        alerts.flush_buffers(session)
        rows = logstore.recent(session, 200)
        if level in ("ERROR", "WARNING"):
            rows = [r for r in rows if (r.level in ("ERROR", "CRITICAL")) == (level == "ERROR")]
        names = {b.id: b.name for b in session.scalars(select(Business).where(Business.id.in_({r.business_id for r in rows if r.business_id}))).all()}
        return page(request, "admin_errors.html", email, rows=rows, names=names, level=level)

    @app.post("/admin/aktion/{what}")
    def admin_action(what: str, email: str = Depends(require_admin_post)):
        """Werkzeuge: Sicherung jetzt, Tagesbericht jetzt, Wächter jetzt."""
        if what == "sicherung":
            info = backup.run_backup()
            return _private(RedirectResponse(f"/admin?msg={'sicherung_ok' if info.get('ok') else 'sicherung_fehler'}", status_code=303))
        if what == "bericht":
            with new_session() as s:
                ok = alerts.digest(s)
            return _private(RedirectResponse(f"/admin?msg={'bericht_ok' if ok else 'bericht_fehler'}", status_code=303))
        raise HTTPException(404)
