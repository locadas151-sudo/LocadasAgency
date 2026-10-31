"""FastAPI-App: Landing, Postfach verbinden, Dashboard, Freigabe-Links, Angebots-Check."""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import __version__, fmt
from .config import check_startup, settings
from .db import AccountStatus, Business, Customer, DocKind, DocStatus, Document, Event, FollowUp, FollowUpStatus, MailAccount, init_db, log_event, new_session, utcnow
from .engine import accounts as acc
from .engine import customers as cust
from .engine import followups as fu_engine
from .engine import summary, taps
from .extract import phones as phone_x
from .engine.scanner import scan_account
from .engine.scheduler import run_cycle, start_scheduler
from .extract.check import check_offer
from .extract.pdftext import pdf_to_text
from .mail import oauth
from .mail.connect import ConnectFailed, ConnectionConfig, connect_mailbox
from .mail.imap import ImapClient, MailAuthError, MailConnectError
from .mail.providers import PROVIDERS, detect_provider
from . import notify
from .notify import inbound as wa_inbound
from .notify import messages as wa_msgs
from .notify import tokens as wa_tokens
from .notify import whatsapp as wa
from .notify.phone import normalize_phone
from .web import auth, ratelimit
from .channels import web as wab_web
from .channels import webhook as wab_webhook
from .engine import trial
from .web import trial_routes

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE = Path(__file__).parent
app = FastAPI(title="Locadas Nachfasser", version=__version__, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "web" / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "web" / "templates")
_scheduler = None
_inbound_smtp = None


# --- Hilfen --------------------------------------------------------------------

def fmt_date(value: datetime | None, with_time: bool = False) -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(ZoneInfo(settings.timezone))
    return local.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


def fmt_money(value: float | None, currency: str | None = "EUR") -> str:
    return fmt.money(value, currency)


def rel_date(value: datetime | None) -> str:
    if not value:
        return ""
    now = utcnow()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = value - now
    days = round(delta.total_seconds() / 86400)
    if abs(delta) < timedelta(hours=1):
        return "jetzt"
    if days == 0:
        return "heute"
    if days == 1:
        return "morgen"
    if days == -1:
        return "gestern"
    if days > 0:
        return f"in {days} Tagen"
    return f"vor {-days} Tagen"


STATUS_LABEL = {
    DocStatus.OPEN: "Wird nachgefasst",
    DocStatus.REPLIED: "Kunde hat geantwortet",
    DocStatus.WON: "Auftrag bekommen",
    DocStatus.LOST: "Absage",
    DocStatus.PAID: "Bezahlt",
    DocStatus.DONE: "Keine Antwort",
    DocStatus.IGNORED: "Ausgeschlossen",
    DocStatus.BOUNCED: "Adresse unzustellbar",
}
STATUS_CLASS = {
    DocStatus.OPEN: "open", DocStatus.REPLIED: "good", DocStatus.WON: "good", DocStatus.PAID: "good",
    DocStatus.LOST: "bad", DocStatus.DONE: "muted", DocStatus.IGNORED: "muted", DocStatus.BOUNCED: "bad",
}
FU_LABEL = {
    FollowUpStatus.SCHEDULED: "geplant", FollowUpStatus.AWAITING_APPROVAL: "wartet auf deine Freigabe",
    FollowUpStatus.AWAITING_TAP: "wartet auf deinen WhatsApp-Tipp",
    FollowUpStatus.SENDING: "wird gesendet", FollowUpStatus.SENT: "gesendet", FollowUpStatus.SKIPPED: "übersprungen", FollowUpStatus.CANCELLED: "gestoppt",
    FollowUpStatus.FAILED: "fehlgeschlagen",
}

templates.env.filters["date"] = fmt_date
templates.env.filters["datetime"] = lambda v: fmt_date(v, True)
templates.env.filters["money"] = fmt_money
templates.env.filters["rel"] = rel_date
templates.env.globals.update(
    STATUS_LABEL=STATUS_LABEL, STATUS_CLASS=STATUS_CLASS, FU_LABEL=FU_LABEL, DocStatus=DocStatus, DocKind=DocKind,
    FollowUpStatus=FollowUpStatus, settings=settings, PROVIDERS=PROVIDERS, version=__version__,
    review_question=fu_engine.review_question, channel_for=taps.channel_for, CHANNEL_LABEL=taps.CHANNEL_LABEL,
    phone_pretty=phone_x.pretty, configured_channel=taps.configured_channel, tap_label=taps.open_label,
    reconnect_link=lambda account_id: wa_tokens.reconnect_link(account_id), euro=lambda v: summary.euro(v),
    oauth_providers=lambda: [p for p in oauth.providers().values() if p.enabled],
)


def get_db():
    session = new_session()
    try:
        yield session
    finally:
        session.close()


def current_business(request: Request, session: Session = Depends(get_db)) -> Business | None:
    return auth.business_from_token(session, request.cookies.get(auth.COOKIE))


def require_business(request: Request, session: Session = Depends(get_db)) -> Business:
    b = auth.business_from_token(session, request.cookies.get(auth.COOKIE))
    if b is None:
        raise HTTPException(status_code=303, headers={"Location": "/login?next=" + request.url.path})
    return b


wab_web.install(app, templates, get_db, require_business)  # Stufe 2: WhatsApp über die eigene Nummer (aus ohne Konfiguration)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    status_code = ctx.pop("status_code", 200)
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def rate_limited(request: Request, name: str) -> bool:
    return not ratelimit.allow(name, client_ip(request))


def _too_many(request: Request):
    return render(request, "error.html", title="Kurz durchatmen", message="Zu viele Versuche in kurzer Zeit. Bitte warte ein paar Minuten und versuch es dann noch einmal.", status_code=429)


def _private_host(host: str) -> bool:
    """Interne Adressen (localhost, 10.x, 192.168.x …) sind als Mailserver gesperrt, außer in Entwicklung/Tests."""
    import ipaddress
    import socket

    host = (host or "").strip().strip("[]")
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
        addrs = {i[4][0] for i in infos}
    except (OSError, UnicodeError):
        return False  # nicht auflösbar: scheitert gleich beim Verbinden
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a.split("%")[0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return True
    return False


def set_session(response: Response, business: Business) -> Response:
    response.set_cookie(
        auth.COOKIE, auth.session_token(business.id), max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax",
        secure=settings.base_url.startswith("https"),
    )
    return response


trial_routes.install(app, render, get_db, current_business, set_session, client_ip)  # Testen ohne Postfach


@app.exception_handler(StarletteHTTPException)
async def _redirect_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if exc.status_code == 404:
        return render(request, "error.html", title="Seite nicht gefunden", message="Diese Seite gibt es nicht.", status_code=404)
    return render(request, "error.html", title="Fehler", message=str(exc.detail), status_code=exc.status_code)


@app.on_event("startup")
def _startup():
    global _scheduler
    check_startup()  # ohne SECRET_KEY/ENCRYPTION_KEY kein Start (außer DEBUG bzw. ALLOW_INSECURE_DEV)
    init_db()
    _scheduler = start_scheduler()
    if settings.inbound_smtp_port:  # Weiterleitungsadressen per eigenem SMTP-Empfänger (sonst nur Webhook /inbound/raw)
        global _inbound_smtp
        from .mail import inbound_smtp

        _inbound_smtp = inbound_smtp.start()


@app.on_event("shutdown")
def _shutdown():
    if _scheduler:
        _scheduler.shutdown(wait=False)
    if _inbound_smtp is not None:
        _inbound_smtp.stop()


# --- Öffentlich -----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def landing(request: Request, business: Business | None = Depends(current_business)):
    return render(request, "landing.html", business=business)


@app.get("/health")
def health():
    return {"ok": True, "version": __version__}


def _trial_for_connect(session: Session, request: Request, token: str = "") -> Business | None:
    """Tester, der jetzt sein Postfach verbindet: per signiertem Link (?b=) oder angemeldet per Cookie."""
    b = trial.business_from_connect_token(session, token) if token else None
    if b is None:
        cur = auth.business_from_token(session, request.cookies.get(auth.COOKIE))
        b = cur if cur is not None and cur.is_trial else None
    return b


@app.get("/verbinden", response_class=HTMLResponse)
def connect_form(request: Request, business: Business | None = Depends(current_business), session: Session = Depends(get_db), email: str = "", avv: str = "", b: str = "", reconnect: str = ""):
    if reconnect:  # Verbindungs-Wächter: „Neu verbinden“ aus der Nachricht an den Betrieb
        return _reconnect_page(request, reconnect)
    if business and business.accounts:
        return RedirectResponse("/dashboard", status_code=303)
    tester = _trial_for_connect(session, request, b)
    values = {"business_name": tester.name, "owner_name": tester.owner_name, "phone": phone_x.pretty(tester.whatsapp_number)} if tester else {}
    email = email or (tester.owner_email if tester else "")
    provider = detect_provider(email) if email else None
    error = "Bitte akzeptiere zuerst den Auftragsverarbeitungsvertrag (AVV)." if avv == "fehlt" else None
    return render(request, "connect.html", business=business, email=email, provider=provider, error=error, hint=None, values=values,
                  trial_token=trial.connect_token(tester) if tester else "")


@app.post("/verbinden", response_class=HTMLResponse)
def connect_submit(
    request: Request,
    session: Session = Depends(get_db),
    business_name: str = Form(""),
    owner_name: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
    phone: str = Form(""),
    imap_host: str = Form(""),
    imap_port: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form(""),
    username: str = Form(""),
    consent: str = Form(""),
    avv: str = Form(""),
    trial_token: str = Form(""),
):
    email = email.strip().lower()
    values = dict(business_name=business_name, owner_name=owner_name, email=email, phone=phone, imap_host=imap_host, imap_port=imap_port, smtp_host=smtp_host, smtp_port=smtp_port, username=username, trial_token=trial_token)
    provider = detect_provider(email)
    if rate_limited(request, "verbinden"):
        return _too_many(request)
    if not consent:
        return render(request, "connect.html", business=None, email=email, provider=provider, values=values, error="Bitte bestätige, dass wir dein Postfach lesen und in deinem Namen nachfassen dürfen.", hint=None)
    if not avv:
        return render(request, "connect.html", business=None, email=email, provider=provider, values=values, error="Bitte akzeptiere den Auftragsverarbeitungsvertrag (AVV). Ohne ihn dürfen wir die Daten deiner Kunden nicht verarbeiten.", hint=None)
    if not settings.allow_insecure_dev and any(_private_host(h) for h in (imap_host.strip(), smtp_host.strip()) if h):
        return render(request, "connect.html", business=None, email=email, provider=provider, values=values, error="Dieser Server ist eine interne Adresse und kann nicht verbunden werden.", hint="Trag den öffentlichen Servernamen deines Mailanbieters ein, zum Beispiel imap.deinanbieter.de.", show_advanced=True)
    if not business_name.strip():
        return render(request, "connect.html", business=None, email=email, provider=provider, values=values, error="Wie heißt dein Betrieb? Der Name steht als Absender in den Nachfass-Mails.", hint=None)
    try:
        cfg = connect_mailbox(
            email, password,
            imap_host=imap_host.strip() or None, imap_port=int(imap_port) if imap_port.strip() else None,
            smtp_host=smtp_host.strip() or None, smtp_port=int(smtp_port) if smtp_port.strip() else None,
            username=username.strip() or None,
        )
    except ConnectFailed as exc:
        show_advanced = exc.provider is None or exc.provider.key == "custom"
        return render(request, "connect.html", business=None, email=email, provider=exc.provider or provider, values=values, error=exc.message, hint=exc.hint, show_advanced=show_advanced)
    except ValueError:
        return render(request, "connect.html", business=None, email=email, provider=provider, values=values, error="Port muss eine Zahl sein.", hint=None, show_advanced=True)

    business = auth.business_by_email(session, email)
    tester = _trial_for_connect(session, request, trial_token)
    if tester is not None and (business is None or business.id == tester.id):
        business = tester  # Test wird zum verbundenen Betrieb, Dokumente bleiben
        business.email = email
        business.phone = phone.strip() or business.phone
    if business is None:
        business = Business(name=business_name.strip(), owner_name=owner_name.strip(), email=email, phone=phone.strip())
        session.add(business)
        session.flush()
    else:
        business.name = business_name.strip() or business.name
        business.owner_name = owner_name.strip() or business.owner_name
        for a in business.accounts:
            a.status = AccountStatus.DISCONNECTED
    business.avv_accepted_at = utcnow()
    account = acc.create_account(session, business, cfg, password)
    if business.is_trial:
        trial.convert(session, business, account)
    business.last_login_at = utcnow()
    session.commit()
    # Erst-Scan im Hintergrund, damit die Seite sofort da ist
    threading.Thread(target=_background_scan, args=(account.id,), daemon=True).start()
    return set_session(RedirectResponse("/start", status_code=303), business)


# --- Neu verbinden (Verbindungs-Wächter) ------------------------------------------------

def _reconnect_account(session: Session, token: str) -> MailAccount | None:
    account_id = wa_tokens.account_id_from_reconnect(token)
    account = session.get(MailAccount, account_id) if account_id else None
    if account is None or account.status == AccountStatus.DISCONNECTED:
        return None
    return account


def _reconnect_page(request: Request, token: str, error: str | None = None, status_code: int = 200):
    with new_session() as session:
        account = _reconnect_account(session, token)
        if account is None:
            return render(request, "error.html", title="Link abgelaufen", message="Dieser Link ist abgelaufen oder ungültig. Melde dich an und verbinde dein Postfach in den Einstellungen neu.", status_code=400)
        provider = oauth.get(account.provider) if account.auth_type == "oauth" else None
        return render(request, "reconnect.html", token=token, email=account.email, account=account, provider=provider, error=error, status_code=status_code)


@app.post("/verbinden/neu", response_class=HTMLResponse)
def reconnect_submit(request: Request, session: Session = Depends(get_db), token: str = Form(...), password: str = Form("")):
    """Neues Passwort für dasselbe Postfach: Zugangsdaten werden ersetzt, Dokumente und Verlauf bleiben."""
    if rate_limited(request, "verbinden"):
        return _too_many(request)
    account = _reconnect_account(session, token)
    if account is None:
        return _reconnect_page(request, token)
    if not password:
        return _reconnect_page(request, token, error="Bitte gib das Passwort deines Postfachs ein.")
    try:
        cfg = connect_mailbox(
            account.email, password, imap_host=account.imap_host, imap_port=account.imap_port,
            smtp_host=account.smtp_host, smtp_port=account.smtp_port, username=account.username,
            imap_ssl=account.imap_ssl, smtp_ssl=account.smtp_ssl,
        )
    except ConnectFailed as exc:
        return _reconnect_page(request, token, error=f"{exc.message} {exc.hint}".strip())
    acc.reconnect(session, account, cfg, password)
    business = account.business
    business.last_login_at = utcnow()
    session.commit()
    return set_session(RedirectResponse("/dashboard?msg=neu_verbunden", status_code=303), business)


def _background_scan(account_id: int) -> None:
    from .engine.scheduler import cycle_lock

    # Nicht gleichzeitig mit dem Scheduler dasselbe Postfach lesen
    if not cycle_lock.acquire(timeout=600):
        return
    try:
        with new_session() as s:
            account = s.get(MailAccount, account_id)
            if account is not None:
                scan_account(s, account)
    finally:
        cycle_lock.release()


@app.get("/start", response_class=HTMLResponse)
def start(request: Request, business: Business = Depends(require_business), session: Session = Depends(get_db), whatsapp: str = ""):
    docs = session.scalars(select(Document).where(Document.business_id == business.id).order_by(Document.sent_at.desc())).all()
    account = business.accounts[-1] if business.accounts else None
    scanning = account is not None and account.last_scan_at is None
    return render(request, "start.html", business=business, docs=docs, account=account, scanning=scanning, wa_msg=whatsapp)


def _save_whatsapp(session: Session, b: Business, number: str, opt_in: bool) -> str:
    """Nummer und Opt-in speichern; bei neuer Anmeldung Begrüßung schicken. Gibt den Status für die Seite zurück."""
    normalized = normalize_phone(number)
    if normalized is None:
        return "wa_ungueltig"
    was_active = notify.whatsapp_ready(b)
    changed = normalized != b.whatsapp_number
    b.whatsapp_number = normalized
    b.whatsapp_opt_in = bool(opt_in and normalized)
    if changed:
        b.whatsapp_last_inbound_at = None  # neues Handy, neues 24h-Fenster
        b.whatsapp_context = ""
    if b.whatsapp_opt_in and (changed or not was_active):
        log_event(session, b.id, "whatsapp", f"WhatsApp-Sekretär an +{normalized} eingeschaltet.")
        if settings.whatsapp_enabled:
            return "wa_ok" if notify.send(session, wa_msgs.welcome(b)) else "wa_fehler"
        return "wa_bald"
    return "gespeichert"


@app.post("/whatsapp")
def whatsapp_signup(business: Business = Depends(require_business), session: Session = Depends(get_db), whatsapp_number: str = Form(""), whatsapp_opt_in: str = Form("")):
    b = session.get(Business, business.id)
    result = _save_whatsapp(session, b, whatsapp_number, bool(whatsapp_opt_in))
    session.commit()
    return RedirectResponse(f"/start?whatsapp={result}", status_code=303)


# --- Dashboard -------------------------------------------------------------------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, business: Business = Depends(require_business), session: Session = Depends(get_db), filter: str = "open", msg: str = ""):
    q = select(Document).where(Document.business_id == business.id)
    if filter == "open":
        q = q.where(Document.status == DocStatus.OPEN)
    elif filter == "offers":
        q = q.where(Document.kind == DocKind.OFFER)
    elif filter == "invoices":
        q = q.where(Document.kind == DocKind.INVOICE)
    elif filter == "closed":
        q = q.where(Document.status != DocStatus.OPEN)
    docs = session.scalars(q.order_by(Document.next_followup_at.is_(None), Document.next_followup_at, Document.sent_at.desc())).all()
    pending = session.scalars(
        select(FollowUp).join(Document).where(Document.business_id == business.id, FollowUp.status == FollowUpStatus.AWAITING_APPROVAL).order_by(FollowUp.due_at)
    ).all()
    events = session.scalars(select(Event).where(Event.business_id == business.id).order_by(Event.created_at.desc()).limit(12)).all()
    counts = {
        "open_offers": session.scalar(select(func.count(Document.id)).where(Document.business_id == business.id, Document.status == DocStatus.OPEN, Document.kind == DocKind.OFFER)) or 0,
        "open_invoices": session.scalar(select(func.count(Document.id)).where(Document.business_id == business.id, Document.status == DocStatus.OPEN, Document.kind == DocKind.INVOICE)) or 0,
        "won": session.scalar(select(func.count(Document.id)).where(Document.business_id == business.id, Document.status.in_([DocStatus.WON, DocStatus.PAID]))) or 0,
        "sent": session.scalar(select(func.count(FollowUp.id)).join(Document).where(Document.business_id == business.id, FollowUp.status == FollowUpStatus.SENT)) or 0,
    }
    open_amount = session.scalar(
        select(func.coalesce(func.sum(Document.amount_gross), 0)).where(Document.business_id == business.id, Document.status == DocStatus.OPEN, Document.kind == DocKind.INVOICE)
    ) or 0
    if cust.backfill(session, business.id):  # Kunden zu Dokumenten von vor dem Kundenportal
        session.commit()
    customers = {c.email: c for c in session.scalars(select(Customer).where(Customer.business_id == business.id)).all()}
    counts["review_clicks"] = sum(1 for c in customers.values() if c.review_clicked_at)
    account = next((a for a in business.accounts if a.status != AccountStatus.DISCONNECTED), None)
    wa_taps = taps.open_taps(session, business.id)
    month = summary.current_month(session, business.id)
    trial_ctx = {"connect_url": trial.connect_url(business), "docs_used": trial.docs_in_window(session, business), "wa_link": trial_routes.secretary_link()} if business.is_trial else None
    return render(request, "dashboard.html", business=business, docs=docs, pending=pending, events=events, counts=counts, open_amount=open_amount, account=account, filter=filter, msg=msg, customers=customers, wa_taps=wa_taps, month=month, trial_ctx=trial_ctx)


@app.get("/freigaben", response_class=HTMLResponse)
def approvals_page(request: Request, business: Business = Depends(require_business), session: Session = Depends(get_db)):
    pending = session.scalars(
        select(FollowUp).join(Document).where(Document.business_id == business.id, FollowUp.status == FollowUpStatus.AWAITING_APPROVAL).order_by(Document.sent_at)
    ).all()
    return render(request, "approvals.html", business=business, pending=pending)


@app.post("/freigaben")
async def approvals_submit(request: Request, business: Business = Depends(require_business), session: Session = Depends(get_db)):
    form = await request.form()
    for key, value in form.items():
        if not key.startswith("fu_"):
            continue
        f = session.get(FollowUp, int(key[3:]))
        if f is None or f.document.business_id != business.id or f.status != FollowUpStatus.AWAITING_APPROVAL:
            continue
        if value == "send":
            fu_engine.approve(session, f)
        elif value == "stop":
            fu_engine.close_document(session, f.document, DocStatus.REPLIED, "Vom Betrieb als erledigt markiert")
        elif value == "skip":
            fu_engine.skip(session, f)
        elif value == "settle":
            fu_engine.settle(session, f.document)
        session.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/dokument/{doc_id}", response_class=HTMLResponse)
def document_page(request: Request, doc_id: int, business: Business = Depends(require_business), session: Session = Depends(get_db), msg: str = ""):
    doc = session.get(Document, doc_id)
    if doc is None or doc.business_id != business.id:
        raise HTTPException(404)
    events = session.scalars(select(Event).where(Event.document_id == doc.id).order_by(Event.created_at.desc())).all()
    previews = {f.id: (fu_engine.texts.subject_for(doc, f.step), fu_engine.texts.body_for(doc, f.step)) for f in doc.followups if not f.body}
    customer = cust.for_document(session, doc)
    session.commit()  # Kunde ggf. eben erst angelegt (ältere Dokumente)
    return render(request, "document.html", business=business, doc=doc, events=events, previews=previews, msg=msg, customer=customer,
                  portal_url=cust.portal_url(customer) if customer else "")


FERTIG_MSG = {
    "sent": "fertig", "already": "fertig_schon", "failed": "fertig_fehler", "no_account": "fertig_fehler",
}


def _safe_next(value: str, fallback: str) -> str:
    """Nur eigene Seiten als Rücksprung (kein offener Redirect)."""
    if value.startswith(("/dashboard", "/dokument/")) and "//" not in value and "\\" not in value:
        return value
    return fallback


@app.post("/kunde/{customer_id}/fertig")
def customer_done(customer_id: int, business: Business = Depends(require_business), session: Session = Depends(get_db), next: str = Form("/dashboard")):
    c = session.get(Customer, customer_id)
    if c is None or c.business_id != business.id:
        raise HTTPException(404)
    result = cust.mark_complete(session, c)
    session.commit()
    target = _safe_next(next, "/dashboard")
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}msg={FERTIG_MSG[result]}", status_code=303)


@app.post("/dokument/{doc_id}/status")
def document_status(doc_id: int, action: str = Form(...), business: Business = Depends(require_business), session: Session = Depends(get_db)):
    doc = session.get(Document, doc_id)
    if doc is None or doc.business_id != business.id:
        raise HTTPException(404)
    mapping = {"won": DocStatus.WON, "lost": DocStatus.LOST, "paid": DocStatus.PAID, "ignore": DocStatus.IGNORED, "replied": DocStatus.REPLIED}
    if action in mapping:
        fu_engine.close_document(session, doc, mapping[action], "Vom Betrieb gesetzt")
    elif action == "reopen":
        fu_engine.reopen_document(session, doc)
    elif action == "send_now":
        nxt = next((f for f in doc.followups if f.status in (FollowUpStatus.SCHEDULED, FollowUpStatus.AWAITING_APPROVAL)), None)
        if nxt is not None:
            if nxt.status == FollowUpStatus.AWAITING_APPROVAL:
                fu_engine.approve(session, nxt)
            else:
                fu_engine.send_followup(session, nxt)
    session.commit()
    return RedirectResponse(f"/dokument/{doc.id}", status_code=303)


@app.post("/dokument/{doc_id}/bearbeiten")
def document_edit(
    doc_id: int, business: Business = Depends(require_business), session: Session = Depends(get_db),
    customer_name: str = Form(""), customer_email: str = Form(""), amount_gross: str = Form(""), document_number: str = Form(""), title: str = Form(""), due_date: str = Form(""),
    customer_mobile: str | None = Form(None), customer_phone: str | None = Form(None),
):
    doc = session.get(Document, doc_id)
    if doc is None or doc.business_id != business.id:
        raise HTTPException(404)
    msg = "gespeichert"
    if customer_mobile is not None or customer_phone is not None:  # Felder waren im Formular
        cc = "41" if (doc.currency or "").upper() == "CHF" else "49"
        mob = phone_x.normalize(customer_mobile or "", cc) if (customer_mobile or "").strip() else ""
        tel = phone_x.normalize(customer_phone or "", cc) if (customer_phone or "").strip() else ""
        bad = ((customer_mobile or "").strip() and not mob) or ((customer_phone or "").strip() and not tel)
        if mob and not phone_x.is_mobile(mob):
            bad = True  # Festnetz kann kein WhatsApp
            tel, mob = tel or mob, ""
        if bad:
            msg = "nummer_ungueltig"
        if (mob, tel) != (doc.customer_mobile, doc.customer_phone):
            doc.customer_mobile, doc.customer_phone = mob, tel
            c = cust.for_document(session, doc)
            if c is not None:
                c.mobile, c.phone = mob, tel
            for f in doc.followups:
                if f.status == FollowUpStatus.AWAITING_TAP:
                    f.wa_text = ""  # wird unten neu geschrieben, die Nummer kommt beim Öffnen frisch aus dem Dokument
    doc.customer_name = customer_name.strip()[:200]
    doc.customer_email = customer_email.strip().lower()[:320]
    doc.document_number = document_number.strip()[:100]
    doc.title = title.strip()[:300]
    if amount_gross.strip():
        try:
            doc.amount_gross = float(amount_gross.replace(".", "").replace(",", ".").replace("€", "").strip())
        except ValueError:
            pass
    if due_date.strip():
        try:
            d = datetime.strptime(due_date.strip(), "%Y-%m-%d")
            doc.due_date = d.replace(hour=12, tzinfo=timezone.utc)
        except ValueError:
            pass
    # Texte neu erzeugen, falls noch nicht gesendet
    for f in doc.followups:
        if f.status in (FollowUpStatus.SCHEDULED,):
            f.subject = ""
            f.body = ""
    for f in doc.followups:
        if f.status == FollowUpStatus.AWAITING_TAP and not f.wa_text:
            f.wa_text = taps.wa_texts.text_for(doc, f.step)
    log_event(session, business.id, "edit", f"{doc.kind_label} {doc.document_number or doc.title}: Angaben korrigiert.", doc.id)
    session.commit()
    return RedirectResponse(f"/dokument/{doc.id}?msg={msg}", status_code=303)


@app.post("/dokument/{doc_id}/kanal")
def document_channel(doc_id: int, whatsapp: str = Form(""), business: Business = Depends(require_business), session: Session = Depends(get_db)):
    """Schalter „Diesen Kunden per WhatsApp nachfassen“ (überschreibt die Einstellung des Betriebs nur für dieses Dokument)."""
    doc = session.get(Document, doc_id)
    if doc is None or doc.business_id != business.id:
        raise HTTPException(404)
    biz = business.followup_channel or "mail"
    if whatsapp:
        doc.followup_channel = "" if biz in ("whatsapp_tap", "both") else "whatsapp_tap"
    else:
        doc.followup_channel = "" if biz == "mail" else "mail"
    label = "per WhatsApp mit einem Tipp" if whatsapp else "per Mail"
    log_event(session, business.id, "edit", f"{doc.kind_label} {doc.document_number or doc.title}: Nachfassen ab jetzt {label}.", doc.id)
    session.commit()
    return RedirectResponse(f"/dokument/{doc.id}?msg=gespeichert", status_code=303)


@app.get("/pdf/{doc_id}")
def document_pdf(doc_id: int, business: Business = Depends(require_business), session: Session = Depends(get_db)):
    doc = session.get(Document, doc_id)
    if doc is None or doc.business_id != business.id or not doc.pdf_path or not Path(doc.pdf_path).exists():
        raise HTTPException(404)
    return FileResponse(doc.pdf_path, media_type="application/pdf", filename=doc.pdf_filename or "dokument.pdf")


# --- Dokument-Link für Kunden (Lese-Tracking) ---------------------------------------

@app.get("/a/{token}", response_class=HTMLResponse)
def customer_view(request: Request, token: str, session: Session = Depends(get_db)):
    doc = session.scalar(select(Document).where(Document.view_token == token)) if token else None
    if doc is None or not doc.pdf_path or not Path(doc.pdf_path).exists():
        raise HTTPException(404)
    # Eigene Vorschau-Aufrufe des Betriebs nicht zählen
    viewer = auth.business_from_token(session, request.cookies.get(auth.COOKIE))
    if viewer is None or viewer.id != doc.business_id:
        now = utcnow()
        first = doc.first_opened_at is None
        doc.open_count += 1
        doc.last_opened_at = now
        if first:
            doc.first_opened_at = now
            fu_engine.on_first_open(session, doc, now)  # Kunde schaut rein: ihm Zeit lassen
            log_event(session, doc.business_id, "opened", f"{doc.customer_name or doc.customer_email} hat {doc.kind_label} {doc.document_number or doc.title} geöffnet.", doc.id)
        session.commit()
        if first and doc.business.notify_opened:
            # Höchstens einmal pro Dokument (nur beim ersten Öffnen)
            notify.send(session, wa_msgs.opened(
                doc,
                f"{doc.customer_name or doc.customer_email} hat dein {doc.kind_label} geöffnet",
                f"Hallo {doc.business.owner_name or doc.business.name},\n\n"
                f"{doc.customer_name or doc.customer_email} hat gerade {doc.kind_label} {doc.document_number or doc.title} geöffnet. "
                f"Guter Moment für einen kurzen Anruf.\n\n{settings.base_url}/dokument/{doc.id}\n\nLocadas Nachfasser",
            ))
            session.commit()
    c = cust.for_document(session, doc)
    session.commit()
    return render(request, "customer_view.html", doc=doc, token=token, portal_token=c.portal_token if c else "")


@app.get("/a/{token}/pdf")
def customer_pdf(token: str, session: Session = Depends(get_db)):
    doc = session.scalar(select(Document).where(Document.view_token == token)) if token else None
    if doc is None or not doc.pdf_path or not Path(doc.pdf_path).exists():
        raise HTTPException(404)
    return FileResponse(doc.pdf_path, media_type="application/pdf", filename=doc.pdf_filename or "dokument.pdf", content_disposition_type="inline")


# --- Kundenportal (ohne Login, nur über den nicht erratbaren Link) -------------------------

def _portal_headers(resp: Response) -> Response:
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"  # Portal-Link nicht an Google & Co. weiterreichen
    return resp


def _portal_customer(session: Session, token: str) -> Customer:
    c = cust.by_token(session, token)
    if c is None:
        raise HTTPException(404)
    return c


@app.get("/k/{token}", response_class=HTMLResponse)
def portal(request: Request, token: str, session: Session = Depends(get_db), danke: str = ""):
    c = _portal_customer(session, token)
    b = c.business
    docs = cust.documents(session, c)
    ref = docs[-1] if docs else None
    account = ref.account if ref is not None and ref.account is not None else next((a for a in b.accounts), None)
    sender = account.email if account else b.owner_email
    title = cust.job_title(docs)
    subject = f"Frage zu meinem Auftrag{f' „{title}“' if title else ''}"
    resp = render(
        request, "portal.html", c=c, b=b, docs=docs, steps=cust.timeline(c, docs), sender=sender, title=title, mail_subject=subject,
        doc_state=cust.doc_state, wa_number=b.whatsapp_number, phone_link=re.sub(r"[^\d+]", "", b.phone or ""),
        danke=danke == "1", feedback_max=cust.FEEDBACK_MAX, has_offer=any(d.kind == DocKind.OFFER for d in docs),
    )
    return _portal_headers(resp)


@app.get("/k/{token}/bewerten")
def portal_review(request: Request, token: str, session: Session = Depends(get_db)):
    """Weiterleitung zu Google. Erster Klick wird notiert (eigene Vorschau des Betriebs zählt nicht)."""
    c = _portal_customer(session, token)
    url = cust.valid_review_url(c.business.google_review_url)
    if not url:
        return _portal_headers(RedirectResponse(f"/k/{token}", status_code=302))
    viewer = auth.business_from_token(session, request.cookies.get(auth.COOKIE))
    if viewer is None or viewer.id != c.business_id:
        if cust.record_click(session, c):
            session.commit()
    return _portal_headers(RedirectResponse(url, status_code=302))


@app.post("/k/{token}/rueckmeldung")
def portal_feedback(request: Request, token: str, session: Session = Depends(get_db), text: str = Form("")):
    c = _portal_customer(session, token)
    if rate_limited(request, "feedback"):
        return _too_many(request)
    cust.save_feedback(session, c, text)
    session.commit()
    return _portal_headers(RedirectResponse(f"/k/{token}?danke=1#rueckmeldung", status_code=303))


# --- Auftrag fertig per Link (Betrieb, ohne Login) ------------------------------------------

def _fertig_customer(session: Session, token: str) -> Customer:
    c = cust.customer_from_fertig_token(session, token)
    if c is None:
        raise HTTPException(404)
    return c


def _fertig_doc(session: Session, c: Customer) -> Document | None:
    docs = cust.documents(session, c)
    return docs[-1] if docs else None


@app.get("/fertig/{token}", response_class=HTMLResponse)
def fertig_link(request: Request, token: str, session: Session = Depends(get_db)):
    """Nur Bestätigungsseite. Erst der POST schickt die Abschluss-Mail."""
    c = _fertig_customer(session, token)
    doc = _fertig_doc(session, c)
    who = c.name or c.email
    if c.completed_at is not None:
        return render(request, "approval.html", title="Schon gemeldet", text=f"{who} hat die Abschluss-Mail am {fmt_date(c.completed_at)} bekommen.", doc=doc, fu=None)
    review = " und dem Link zu deiner Google-Bewertung" if c.business.google_review_url else ""
    return render(request, "confirm.html", title="Auftrag fertig?", text=f"Dann bekommt {who} jetzt eine kurze Abschluss-Mail in deinem Namen, mit allen Unterlagen{review}.", doc=doc, button="Ja, Auftrag ist fertig", action=f"/fertig/{token}", preview=None)


@app.post("/fertig/{token}", response_class=HTMLResponse)
def fertig_submit(request: Request, token: str, session: Session = Depends(get_db)):
    c = _fertig_customer(session, token)
    who = c.name or c.email
    result = cust.mark_complete(session, c)
    session.commit()
    doc = _fertig_doc(session, c)
    if result == "sent":
        title, text = "Abschluss-Mail ist raus", f"{who} hat die Mail mit allen Unterlagen{' und dem Bewertungslink' if c.review_requested_at else ''} bekommen, in deinem Namen."
    elif result == "already":
        title, text = "Schon gemeldet", f"{who} hat die Abschluss-Mail schon bekommen."
    else:
        title, text = "Nicht gesendet", "Die Mail ging nicht raus. Prüf bitte die Postfach-Verbindung im Dashboard."
    return render(request, "approval.html", title=title, text=text, doc=doc, fu=None)


OUTCOME_BUTTON = {"won": "Ja, Auftrag bekommen", "lost": "Nicht bekommen", "paid": "Ja, ist bezahlt", "unpaid": "Nicht bezahlt, ich kümmere mich"}


def _outcome_doc(session: Session, token: str, outcome: str) -> Document:
    if outcome not in OUTCOME_BUTTON:
        raise HTTPException(404)
    doc = session.scalar(select(Document).where(Document.outcome_token == token)) if token else None
    if doc is None:
        raise HTTPException(404)
    return doc


def _expired(request: Request):
    return render(request, "error.html", title="Link abgelaufen", message="Dieser Link ist älter als 60 Tage und gilt nicht mehr. Du kannst alles im Dashboard erledigen.", status_code=410)


def _outcome_done(request: Request, doc: Document):
    return render(request, "approval.html", title="Schon notiert", text=f"{doc.customer_name or doc.customer_email}: {doc.close_reason or STATUS_LABEL.get(doc.status, '')}. Ändern kannst du das im Dashboard beim Vorgang.", doc=doc, fu=None)


@app.get("/o/{token}/{outcome}", response_class=HTMLResponse)
def outcome_link(request: Request, token: str, outcome: str, session: Session = Depends(get_db)):
    """Nur Bestätigungsseite. Erst der POST ändert etwas (Link-Vorschauen in Mailprogrammen lösen nichts aus)."""
    doc = _outcome_doc(session, token, outcome)
    if fu_engine.outcome_link_expired(doc):
        return _expired(request)
    if doc.outcome_at is not None:
        return _outcome_done(request, doc)
    q = "Hast du den Auftrag bekommen?" if doc.kind == DocKind.OFFER else "Ist die Rechnung bezahlt?"
    return render(request, "confirm.html", title=q, text=f"{doc.customer_name or doc.customer_email}: bitte kurz bestätigen.", doc=doc, button=OUTCOME_BUTTON[outcome], action=f"/o/{token}/{outcome}", preview=None)


@app.post("/o/{token}/{outcome}", response_class=HTMLResponse)
def outcome_submit(request: Request, token: str, outcome: str, session: Session = Depends(get_db)):
    doc = _outcome_doc(session, token, outcome)
    if fu_engine.outcome_link_expired(doc):
        return _expired(request)
    if doc.outcome_at is not None:
        return _outcome_done(request, doc)  # nur einmal per Link, danach im Dashboard
    if doc.status == DocStatus.OPEN:
        fu_engine.cancel_open_followups(session, doc, "Vom Betrieb per Link gesetzt")
    label = fu_engine.set_outcome(session, doc, outcome)
    extra = None
    if outcome == "won":
        c = cust.for_document(session, doc)
        if c is not None and c.completed_at is None:
            extra = (f"/fertig/{cust.fertig_token(c)}", "Auftrag ist schon fertig? Fertig melden")
    session.commit()
    return render(request, "approval.html", title="Danke, ist notiert", text=f"{doc.customer_name or doc.customer_email}: {label}.", doc=doc, fu=None, extra=extra)


# --- Freigabe per Link (ohne Login) ------------------------------------------------

APPROVAL_ACTIONS = ("send", "done", "skip", "stop")


def _approval_fu(session: Session, token: str, action: str) -> FollowUp:
    if action not in APPROVAL_ACTIONS:
        raise HTTPException(404)
    fu = fu_engine.find_by_token(session, token)
    if fu is None:
        raise HTTPException(404)
    return fu


def _approval_state(fu: FollowUp, action: str) -> tuple[str, str] | None:
    """Schon erledigt? Dann (Titel, Text) statt Bestätigungsseite."""
    doc = fu.document
    who = doc.customer_name or doc.customer_email
    if action in ("stop", "done"):
        if doc.status != DocStatus.OPEN:
            return "Schon erledigt", f"Bei {who} wird nicht mehr nachgefasst."
        return None
    if fu.status == FollowUpStatus.SENT:
        return "Schon gesendet", "Diese Nachfass-Mail ist bereits raus."
    if fu.status == FollowUpStatus.SENDING:
        return "Wird gerade gesendet", "Diese Nachfass-Mail geht gerade raus."
    if fu.status != FollowUpStatus.AWAITING_APPROVAL or doc.status != DocStatus.OPEN:
        return "Nicht mehr offen", "Diese Frage ist schon beantwortet oder der Vorgang ist abgeschlossen."
    return None


def _approval_confirm_text(fu: FollowUp, action: str) -> tuple[str, str, str, str | None]:
    """(Titel, Text, Knopf, Vorschau)."""
    doc = fu.document
    who = doc.customer_name or doc.customer_email
    if action == "send":
        if doc.kind == DocKind.INVOICE:
            return "Noch offen?", fu_engine.invoice_question(doc), "Noch offen: Zahlungserinnerung schicken", None
        if doc.needs_review:
            return "Nachfassen?", fu_engine.review_question(doc), "Bitte nachfassen", None
        return f"An {who} senden?", "Diese Nachfass-Mail geht nach dem Klick in deinem Namen raus.", "Jetzt senden", fu.body or None
    if action == "done":
        label = "Ist bezahlt" if doc.kind == DocKind.INVOICE else "Ist erledigt"
        return f"{label}?", f"Dann fasse ich bei {who} nicht nach.", label, None
    if action == "skip":
        return "Diesmal nicht senden?", "Diese Nachfass-Mail wird nicht geschickt, die nächste Stufe bleibt geplant.", "Diesmal nicht senden", None
    return "Nicht mehr nachfassen?", f"Hat sich {who} schon gemeldet? Dann stoppe ich das Nachfassen.", "Stoppen, hat sich gemeldet", None


@app.get("/f/{token}/{action}", response_class=HTMLResponse)
def approval_link(request: Request, token: str, action: str, session: Session = Depends(get_db)):
    """Nur Bestätigungsseite mit Knopf. Ein GET (z. B. Link-Vorschau im Mailprogramm) ändert nie etwas."""
    fu = _approval_fu(session, token, action)
    if fu_engine.link_expired(fu):
        return _expired(request)
    state = _approval_state(fu, action)
    if state:
        return render(request, "approval.html", title=state[0], text=state[1], doc=fu.document, fu=fu)
    title, text, button, preview = _approval_confirm_text(fu, action)
    return render(request, "confirm.html", title=title, text=text, doc=fu.document, button=button, action=f"/f/{token}/{action}", preview=preview)


@app.post("/f/{token}/{action}", response_class=HTMLResponse)
def approval_submit(request: Request, token: str, action: str, session: Session = Depends(get_db)):
    fu = _approval_fu(session, token, action)
    if fu_engine.link_expired(fu):
        return _expired(request)
    doc = fu.document
    who = doc.customer_name or doc.customer_email
    state = _approval_state(fu, action)
    if state:
        return render(request, "approval.html", title=state[0], text=state[1], doc=doc, fu=fu)
    if action == "send" and doc.needs_review and doc.kind != DocKind.INVOICE:
        if fu_engine.release_review(session, fu):
            title, text = "Wird nachgefasst", f"Ich fasse bei {who} nach. Den Text bekommst du vorher noch zu sehen."
        else:
            title, text = "Nicht mehr offen", "Diese Frage ist schon beantwortet."
    elif action == "done":
        label = fu_engine.settle(session, doc)
        title, text = ("Notiert", f"{who}: {label}. Ich fasse dort nicht nach.") if label else ("Schon erledigt", f"Bei {who} wird nicht mehr nachgefasst.")
    elif action == "send":
        if fu_engine.approve(session, fu):
            what = "Die Zahlungserinnerung" if doc.kind == DocKind.INVOICE else "Die Nachfass-Mail"
            title, text = "Gesendet", f"{what} an {who} ist raus, in deinem Namen."
        else:
            title, text = "Nicht gesendet", f"Das hat nicht geklappt: {fu.error or 'Freigabe war nicht mehr offen.'}"
    elif action == "skip":
        fu_engine.skip(session, fu)
        title, text = "Übersprungen", "Diese Nachfass-Mail wird nicht geschickt. Die nächste Stufe bleibt geplant."
    else:
        fu_engine.close_document(session, doc, DocStatus.REPLIED, "Vom Betrieb gestoppt")
        title, text = "Gestoppt", f"Bei {who} wird nicht mehr nachgefasst."
    session.commit()
    return render(request, "approval.html", title=title, text=text, doc=doc, fu=fu)


# --- WhatsApp mit einem Tipp (ohne Login, Token je Nachfass-Stufe) ----------------------------

# Link-Vorschauen (Messenger, Mail-Scanner) öffnen Links automatisch: das zählt nicht als Tipp des Betriebs
_BOT_UA = re.compile(r"(?i)bot|crawler|spider|preview|facebookexternalhit|whatsapp|slack|telegram|skype|discord|safelinks|proofpoint|mimecast|barracuda|curl|wget|python-requests|headless")


@app.get("/t/{token}")
def tap_redirect(request: Request, token: str, session: Session = Depends(get_db)):
    """„In WhatsApp öffnen“: merkt den Tipp und leitet auf wa.me mit vorausgefülltem Text weiter."""
    fu = taps.find(session, token)
    if fu is None or fu_engine.link_expired(fu):
        raise HTTPException(404)
    doc = fu.document
    if fu.tap_kind == "manual" or (fu.tap_kind == "mailto" and not doc.customer_email):
        # Testen ohne Postfach, Kontakt fehlt: Text zum Kopieren plus Feld für Nummer oder Mailadresse
        return render(request, "trial_tap.html", fu=fu, doc=doc, ok=request.query_params.get("ok"), fehler=request.query_params.get("fehler"))
    human = not _BOT_UA.search(request.headers.get("user-agent", ""))
    if fu.tap_kind == "mailto":
        if fu.tap_state == "open" and human:
            taps.record_tap(fu, utcnow())
            session.commit()
        return RedirectResponse(trial.mailto_link(doc.customer_email, fu.subject, fu.body or fu.wa_text), status_code=302)
    if not doc.customer_mobile or not fu.wa_text:
        return render(request, "approval.html", title="Keine Handynummer", text="Für diesen Kunden ist keine Handynummer hinterlegt. Trag sie beim Dokument ein.", doc=doc, fu=fu)
    if fu.tap_state == "open" and human:
        taps.record_tap(fu, utcnow())
        session.commit()
    return RedirectResponse(taps.wa_link(doc.customer_mobile, fu.wa_text), status_code=302)


TAP_CONFIRM = {
    "sent": ("Abgeschickt?", "Hast du die WhatsApp-Nachricht an {who} abgeschickt? Dann vermerke ich sie als gesendet.", "Ja, ist abgeschickt"),
    "replied": ("Kunde hat geantwortet?", "Hat {who} schon geantwortet, zum Beispiel per WhatsApp oder am Telefon? Dann fasse ich nicht mehr nach.", "Ja, hat geantwortet"),
    "paid": ("Schon bezahlt?", "Hat {who} schon bezahlt? Dann erinnere ich nicht mehr.", "Ja, ist bezahlt"),
    "skip": ("Diesmal nicht?", "Diese Nachricht an {who} wird nicht geschickt, die nächste Stufe bleibt geplant.", "Diesmal nicht"),
}


def _tap_fu(session: Session, token: str, action: str) -> FollowUp:
    if action not in taps.ACTIONS:
        raise HTTPException(404)
    fu = taps.find(session, token)
    if fu is None:
        raise HTTPException(404)
    return fu


@app.get("/w/{token}/{action}", response_class=HTMLResponse)
def tap_action_link(request: Request, token: str, action: str, session: Session = Depends(get_db)):
    """Bestätigungsseite. Ein GET ändert nie etwas (Link-Vorschau im Mailprogramm)."""
    fu = _tap_fu(session, token, action)
    if fu_engine.link_expired(fu):
        return _expired(request)
    doc = fu.document
    state = None if action in ("replied", "paid") and doc.status == DocStatus.OPEN else taps.state_text(fu)
    if action in ("replied", "paid") and doc.status != DocStatus.OPEN:
        state = ("Schon erledigt", f"Bei {doc.customer_name or doc.customer_email} fasse ich nicht mehr nach.")
    if state:
        return render(request, "approval.html", title=state[0], text=state[1], doc=doc, fu=fu)
    title, text, button = TAP_CONFIRM[action]
    return render(request, "confirm.html", title=title, text=text.format(who=doc.customer_name or doc.customer_email), doc=doc, button=button,
                  action=f"/w/{token}/{action}", preview=fu.wa_text if action == "sent" else None)


@app.post("/w/{token}/{action}", response_class=HTMLResponse)
def tap_action_submit(request: Request, token: str, action: str, session: Session = Depends(get_db), next: str = Form("")):
    fu = _tap_fu(session, token, action)
    if fu_engine.link_expired(fu):
        return _expired(request)
    was_open = fu.tap_state == "open"
    title, text = taps.handle(session, fu, action, utcnow())
    session.commit()
    b = fu.document.business
    if b.is_trial:
        trial.touch(b)
        if action == "sent" and was_open and fu.tap_state == "sent":
            trial.maybe_upgrade(session, b)  # einmal: „Soll das künftig automatisch laufen?“
        session.commit()
    if next:
        return RedirectResponse(_safe_next(next, "/dashboard") + "#whatsapp", status_code=303)
    return render(request, "approval.html", title=title, text=text, doc=fu.document, fu=fu)


# --- WhatsApp-Sekretär -----------------------------------------------------------

@app.get("/webhook/whatsapp")
def whatsapp_verify(request: Request):
    """Einmalige Prüfung durch Meta beim Eintragen der Webhook-URL."""
    q = request.query_params
    token = settings.whatsapp_verify_token
    if q.get("hub.mode") == "subscribe" and token and q.get("hub.verify_token") == token:
        return Response(q.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(403)


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request, session: Session = Depends(get_db)):
    body = await request.body()
    sig = request.headers.get("x-hub-signature-256")
    if not (wa.valid_signature(body, sig) or wab_webhook.valid_signature(body, sig)):
        return JSONResponse({"ok": False}, status_code=403)
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    data, handled = wab_webhook.route(session, data)  # Stufe 2: Nummern der Betriebe (sonst unverändert)
    if data:
        handled += wa_inbound.process_webhook(session, data)
    session.commit()
    return {"ok": True, "handled": handled}


@app.get("/wa/d/{token}")
def whatsapp_doc(token: str, session: Session = Depends(get_db)):
    """PDF für den Betrieb aus dem WhatsApp-Chat, ohne Login und ohne als Kunden-Öffnen zu zählen."""
    doc_id = wa_tokens.doc_id_from_link(token)
    doc = session.get(Document, doc_id) if doc_id else None
    if doc is None or not doc.pdf_path or not Path(doc.pdf_path).exists():
        raise HTTPException(404)
    return FileResponse(doc.pdf_path, media_type="application/pdf", filename=doc.pdf_filename or "dokument.pdf", content_disposition_type="inline")


# --- OAuth (Gmail, Outlook) -----------------------------------------------------

def _oauth_state(data: dict) -> str:
    return auth._serializer("oauth").dumps(data)


def _oauth_state_load(token: str) -> dict | None:
    from itsdangerous import BadSignature, SignatureExpired
    try:
        return auth._serializer("oauth").loads(token, max_age=900)
    except (BadSignature, SignatureExpired):
        return None


@app.get("/oauth/{key}/start")
def oauth_start(request: Request, key: str, business_name: str = "", owner_name: str = "", phone: str = "", avv: str = "", trial_token: str = "", session: Session = Depends(get_db)):
    p = oauth.get(key)
    if p is None or not p.enabled:
        raise HTTPException(404)
    if avv != "1":
        return RedirectResponse("/verbinden?avv=fehlt", status_code=303)
    tester = _trial_for_connect(session, request, trial_token)
    state = _oauth_state({"k": key, "b": business_name.strip()[:200], "o": owner_name.strip()[:200], "p": phone.strip()[:60], "avv": 1, "t": tester.id if tester else 0})
    return RedirectResponse(oauth.authorize_url(p, state), status_code=303)


@app.get("/oauth/{key}/callback", response_class=HTMLResponse)
def oauth_callback(request: Request, key: str, session: Session = Depends(get_db), code: str = "", state: str = "", error: str = "", error_description: str = ""):
    p = oauth.get(key)
    data = _oauth_state_load(state) if state else None
    if p is None or not p.enabled or data is None or data.get("k") != key:
        raise HTTPException(status_code=400, detail="Die Anmeldung ist abgelaufen. Bitte noch einmal von vorn.")
    values = dict(business_name=data.get("b", ""), owner_name=data.get("o", ""), phone=data.get("p", ""))

    def fail(msg: str, hint: str = ""):
        return render(request, "connect.html", business=None, email="", provider=None, values=values, error=msg, hint=hint)

    if error or not code:
        return fail(f"{p.label} hat die Verbindung abgelehnt.", error_description or error)
    try:
        tok = oauth.exchange_code(p, code)
    except oauth.OAuthError as exc:
        return fail("Die Verbindung hat nicht geklappt.", str(exc))
    email = oauth.email_from_id_token(tok.get("id_token"))
    if not email:
        return fail("Wir konnten deine Mailadresse nicht lesen.", "Bitte erlaube beim Verbinden den Zugriff auf die E-Mail-Adresse.")
    try:
        with ImapClient(p.imap_host, 993, True, email, tok["access_token"], "oauth") as c:
            sent = c.find_sent_folder(p.sent_folders)
    except (MailAuthError, MailConnectError) as exc:
        return fail(f"Postfach {email} ist angebunden, aber IMAP antwortet nicht.", f"{exc}. Bei Microsoft 365 muss IMAP im Admin-Center für das Postfach erlaubt sein.")
    if not sent:
        return fail("Kein Gesendet-Ordner gefunden.")
    cfg = ConnectionConfig(
        provider_key=key, provider_label=p.label, email=email, username=email,
        imap_host=p.imap_host, imap_port=993, imap_ssl=True,
        smtp_host=p.smtp_host, smtp_port=p.smtp_port, smtp_ssl=p.smtp_ssl, sent_folder=sent,
    )
    business = auth.business_by_email(session, email)
    same = next((a for a in business.accounts if a.email == email and a.status != AccountStatus.DISCONNECTED), None) if business else None
    if same is not None and tok.get("refresh_token"):
        # Dasselbe Postfach noch einmal (z. B. nach „Neu verbinden“): Zugang ersetzen, Dokumente und Verlauf bleiben
        acc.reconnect(session, same, cfg, tok["refresh_token"], auth_type="oauth")
        oauth._cache[f"acc:{same.id}"] = (tok["access_token"], __import__("time").time() + int(tok.get("expires_in", 3600)))
        business.last_login_at = utcnow()
        session.commit()
        return set_session(RedirectResponse("/dashboard?msg=neu_verbunden", status_code=303), business)
    tester = session.get(Business, int(data.get("t") or 0)) if data.get("t") else None
    if tester is not None and tester.is_trial and (business is None or business.id == tester.id):
        business = tester  # Test wird zum verbundenen Betrieb
        business.email = email
    if business is None:
        business = Business(name=values["business_name"] or email.split("@")[0], owner_name=values["owner_name"], email=email, phone=values["phone"])
        session.add(business)
        session.flush()
    else:
        business.name = values["business_name"] or business.name
        business.owner_name = values["owner_name"] or business.owner_name
        for a in business.accounts:
            a.status = AccountStatus.DISCONNECTED
    business.avv_accepted_at = utcnow()
    account = acc.create_account(session, business, cfg, tok["refresh_token"], auth_type="oauth")
    if business.is_trial:
        trial.convert(session, business, account)
    oauth._cache[f"acc:{account.id}"] = (tok["access_token"], __import__("time").time() + int(tok.get("expires_in", 3600)))
    business.last_login_at = utcnow()
    session.commit()
    threading.Thread(target=_background_scan, args=(account.id,), daemon=True).start()
    return set_session(RedirectResponse("/start", status_code=303), business)


# --- Einstellungen -------------------------------------------------------------------

@app.get("/einstellungen", response_class=HTMLResponse)
def settings_page(request: Request, business: Business = Depends(require_business), msg: str = ""):
    account = next((a for a in business.accounts if a.status != AccountStatus.DISCONNECTED), None)
    return render(request, "settings.html", business=business, account=account, msg=msg)


@app.post("/einstellungen")
def settings_save(
    business: Business = Depends(require_business), session: Session = Depends(get_db),
    name: str = Form(""), owner_name: str = Form(""), phone: str = Form(""), signature: str = Form(""),
    followup_offers: str = Form(""), followup_invoices: str = Form(""), paused: str = Form(""), review_mode: str | None = Form(None),
    approval_threshold: str | None = Form(None), notify_opened: str = Form(""),
    include_view_link: str = Form(""), personalize: str = Form(""), doc_delivery: str | None = Form(None), tone: str | None = Form(None),
    whatsapp_number: str | None = Form(None), whatsapp_opt_in: str = Form(""), google_review_url: str | None = Form(None), review_field: str = Form(""),
    followup_channel: str | None = Form(None),
):
    b = session.get(Business, business.id)
    review_ok = True
    if google_review_url is not None or review_field:  # Feld war im Formular (leer kommt als None an)
        google_review_url = google_review_url or ""
        if not google_review_url.strip():
            b.google_review_url = ""
        elif (clean := cust.valid_review_url(google_review_url)) is not None:
            b.google_review_url = clean
        else:
            review_ok = False
    b.name = name.strip() or b.name
    b.owner_name = owner_name.strip()
    b.phone = phone.strip()
    b.signature = signature.strip()
    b.followup_offers = bool(followup_offers)
    b.followup_invoices = bool(followup_invoices)
    b.paused = bool(paused)
    if review_mode in ("auto", "all"):
        b.review_mode = review_mode
    if approval_threshold is not None and approval_threshold.strip():
        try:
            v = float(approval_threshold.replace(".", "").replace(",", ".").replace("€", "").strip())
            b.approval_threshold = max(0.0, v)
        except ValueError:
            pass
    b.notify_opened = bool(notify_opened)
    if doc_delivery is not None:  # Auswahl im Formular: Link / PDF anhängen / nichts
        b.include_view_link = doc_delivery in fu_engine.texts.DELIVERY
        if doc_delivery in fu_engine.texts.DELIVERY:
            b.doc_delivery = doc_delivery
    else:
        b.include_view_link = bool(include_view_link)
    if tone in fu_engine.texts.TONES:
        b.tone = tone
    if followup_channel in taps.CHANNELS:
        b.followup_channel = followup_channel
    b.personalize = bool(personalize)
    for d in b.documents:
        for f in d.followups:
            if f.status == FollowUpStatus.SCHEDULED:
                f.subject, f.body = "", ""
    for a in b.accounts:
        a.display_name = b.name
    msg = "gespeichert"
    if whatsapp_number is not None:  # Feld war im Formular
        msg = _save_whatsapp(session, b, whatsapp_number, bool(whatsapp_opt_in))
    if not review_ok:
        msg = "review_ungueltig"
    session.commit()
    return RedirectResponse(f"/einstellungen?msg={msg}", status_code=303)


@app.post("/einstellungen/trennen")
def disconnect(business: Business = Depends(require_business), session: Session = Depends(get_db), confirm: str = Form("")):
    b = session.get(Business, business.id)
    if confirm != "ja":
        return RedirectResponse("/einstellungen?msg=bestaetigen", status_code=303)
    for doc in b.documents:
        if doc.pdf_path and Path(doc.pdf_path).exists():
            try:
                Path(doc.pdf_path).unlink()
            except OSError:
                pass
    session.delete(b)
    session.commit()
    resp = RedirectResponse("/?getrennt=1", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# --- Login -------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/dashboard", sent: str = ""):
    return render(request, "login.html", next=next, sent=sent, error=None)


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, session: Session = Depends(get_db), email: str = Form(...), next: str = Form("/dashboard")):
    if rate_limited(request, "login"):
        return _too_many(request)
    business = auth.business_by_email(session, email)
    if business is None and "@" not in email:
        number = normalize_phone(email)  # Tester ohne Mail: Login-Link per WhatsApp an die Handynummer
        business = session.scalars(select(Business).where(Business.whatsapp_number == number).order_by(Business.id.desc())).first() if number else None
    if business is None:
        return render(request, "login.html", next=next, sent="", error="Zu dieser Adresse oder Nummer kennen wir keinen Zugang.")
    if not business.accounts:
        ok = notify.send(session, trial_routes.login_notification(business))
        session.commit()
        if ok:
            return render(request, "login.html", next=next, sent=email, channel="whatsapp" if notify.whatsapp_ready(business) else "email", error=None)
        return render(request, "login.html", next=next, sent="", error="Wir konnten dir gerade keinen Link schicken. Versuch es gleich noch einmal.")
    link = f"{settings.base_url}/login/{auth.login_link_token(business.id)}"
    n = wa_msgs.login(
        business, link, "Dein Login-Link für den Locadas Nachfasser",
        f"Hallo {business.owner_name or business.name},\n\nhier ist dein Link zum Einloggen (2 Stunden gültig):\n{link}\n\nLocadas Nachfasser",
    )
    ok = notify.send(session, n)
    session.commit()
    if not ok:
        return render(request, "login.html", next=next, sent="", error="Wir konnten keine Mail über dein Postfach schicken. Verbinde das Postfach bitte neu.")
    return render(request, "login.html", next=next, sent=email, channel=n.channel, error=None)


@app.get("/login/{token}")
def login_link(token: str, session: Session = Depends(get_db), next: str = "/dashboard"):
    business = auth.business_from_login_token(session, token)
    if business is None:
        raise HTTPException(status_code=400, detail="Der Login-Link ist abgelaufen oder ungültig. Fordere bitte einen neuen an.")
    business.last_login_at = utcnow()
    if business.is_trial:
        trial.touch(business)
    session.commit()
    return set_session(RedirectResponse(_safe_next(next, "/dashboard"), status_code=303), business)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# --- Angebots-Check für Kunden ------------------------------------------------------

@app.get("/check", response_class=HTMLResponse)
def check_form(request: Request):
    return render(request, "check.html", result=None, error=None, available=bool(settings.anthropic_api_key))


@app.post("/check", response_class=HTMLResponse)
async def check_submit(request: Request, file: UploadFile | None = None, text: str = Form("")):
    if rate_limited(request, "check"):
        return _too_many(request)
    content = text.strip()
    if file is not None and file.filename:
        data = await file.read()
        if len(data) > 15_000_000:
            return render(request, "check.html", result=None, error="Die Datei ist zu groß (max. 15 MB).", available=True)
        if file.filename.lower().endswith(".pdf") or data[:4] == b"%PDF":
            content = pdf_to_text(data)
        else:
            content = data.decode("utf-8", "replace")
    if len(content) < 80:
        return render(request, "check.html", result=None, error="Da ist zu wenig Text drin. Lad das Angebot als PDF hoch oder kopier den Text rein.", available=bool(settings.anthropic_api_key))
    result = check_offer(content)
    if result is None:
        return render(request, "check.html", result=None, error="Der Check ist gerade nicht erreichbar. Versuch es in ein paar Minuten noch einmal.", available=bool(settings.anthropic_api_key))
    return render(request, "check.html", result=result, error=None, available=True)


# --- Betrieb / Admin -------------------------------------------------------------------

@app.post("/admin/run")
def admin_run(request: Request):
    if request.headers.get("x-admin-key") != settings.secret_key:
        raise HTTPException(403)
    return JSONResponse(run_cycle())


@app.post("/jetzt-pruefen")
def scan_now(business: Business = Depends(require_business), session: Session = Depends(get_db)):
    """Nur das eigene Postfach, nie parallel zum Scheduler (sonst doppelte Scans und Mails)."""
    from .engine.scheduler import cycle_lock

    if not cycle_lock.acquire(blocking=False):
        return RedirectResponse("/dashboard?msg=laeuft", status_code=303)
    try:
        for a in business.accounts:
            if a.status != AccountStatus.DISCONNECTED:
                scan_account(session, a, force=True)
        fu_engine.process_due(session, business_id=business.id)
        session.commit()
    finally:
        cycle_lock.release()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/datenschutz", response_class=HTMLResponse)
def privacy(request: Request):
    return render(request, "privacy.html")


@app.get("/avv", response_class=HTMLResponse)
def avv_page(request: Request):
    return render(request, "avv.html")
