"""FastAPI-App: Landing, Postfach verbinden, Dashboard, Freigabe-Links, Angebots-Check."""
from __future__ import annotations

import json
import logging
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
from .config import settings
from .db import AccountStatus, Business, DocKind, DocStatus, Document, Event, FollowUp, FollowUpStatus, MailAccount, init_db, log_event, new_session, utcnow
from .engine import accounts as acc
from .engine import followups as fu_engine
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
from .web import auth

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE = Path(__file__).parent
app = FastAPI(title="Locadas Nachfasser", version=__version__, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "web" / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "web" / "templates")
_scheduler = None


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
    FollowUpStatus.SENT: "gesendet", FollowUpStatus.SKIPPED: "übersprungen", FollowUpStatus.CANCELLED: "gestoppt",
    FollowUpStatus.FAILED: "fehlgeschlagen",
}

templates.env.filters["date"] = fmt_date
templates.env.filters["datetime"] = lambda v: fmt_date(v, True)
templates.env.filters["money"] = fmt_money
templates.env.filters["rel"] = rel_date
templates.env.globals.update(
    STATUS_LABEL=STATUS_LABEL, STATUS_CLASS=STATUS_CLASS, FU_LABEL=FU_LABEL, DocStatus=DocStatus, DocKind=DocKind,
    FollowUpStatus=FollowUpStatus, settings=settings, PROVIDERS=PROVIDERS, version=__version__,
    review_question=fu_engine.review_question,
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


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    status_code = ctx.pop("status_code", 200)
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def set_session(response: Response, business: Business) -> Response:
    response.set_cookie(
        auth.COOKIE, auth.session_token(business.id), max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax",
        secure=settings.base_url.startswith("https"),
    )
    return response


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
    init_db()
    _scheduler = start_scheduler()


@app.on_event("shutdown")
def _shutdown():
    if _scheduler:
        _scheduler.shutdown(wait=False)


# --- Öffentlich -----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def landing(request: Request, business: Business | None = Depends(current_business)):
    return render(request, "landing.html", business=business)


@app.get("/health")
def health():
    return {"ok": True, "version": __version__}


@app.get("/verbinden", response_class=HTMLResponse)
def connect_form(request: Request, business: Business | None = Depends(current_business), email: str = ""):
    if business and business.accounts:
        return RedirectResponse("/dashboard", status_code=303)
    provider = detect_provider(email) if email else None
    return render(request, "connect.html", business=business, email=email, provider=provider, error=None, hint=None, values={})


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
):
    email = email.strip().lower()
    values = dict(business_name=business_name, owner_name=owner_name, email=email, phone=phone, imap_host=imap_host, imap_port=imap_port, smtp_host=smtp_host, smtp_port=smtp_port, username=username)
    provider = detect_provider(email)
    if not consent:
        return render(request, "connect.html", business=None, email=email, provider=provider, values=values, error="Bitte bestätige, dass wir dein Postfach lesen und in deinem Namen nachfassen dürfen.", hint=None)
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
    if business is None:
        business = Business(name=business_name.strip(), owner_name=owner_name.strip(), email=email, phone=phone.strip())
        session.add(business)
        session.flush()
    else:
        business.name = business_name.strip() or business.name
        business.owner_name = owner_name.strip() or business.owner_name
        for a in business.accounts:
            a.status = AccountStatus.DISCONNECTED
    account = acc.create_account(session, business, cfg, password)
    business.last_login_at = utcnow()
    session.commit()
    # Erst-Scan im Hintergrund, damit die Seite sofort da ist
    threading.Thread(target=_background_scan, args=(account.id,), daemon=True).start()
    return set_session(RedirectResponse("/start", status_code=303), business)


def _background_scan(account_id: int) -> None:
    with new_session() as s:
        account = s.get(MailAccount, account_id)
        if account is not None:
            scan_account(s, account)


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
def dashboard(request: Request, business: Business = Depends(require_business), session: Session = Depends(get_db), filter: str = "open"):
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
    account = next((a for a in business.accounts if a.status != AccountStatus.DISCONNECTED), None)
    return render(request, "dashboard.html", business=business, docs=docs, pending=pending, events=events, counts=counts, open_amount=open_amount, account=account, filter=filter)


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
    return render(request, "document.html", business=business, doc=doc, events=events, previews=previews, msg=msg)


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
):
    doc = session.get(Document, doc_id)
    if doc is None or doc.business_id != business.id:
        raise HTTPException(404)
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
    log_event(session, business.id, "edit", f"{doc.kind_label} {doc.document_number or doc.title}: Angaben korrigiert.", doc.id)
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
    return render(request, "customer_view.html", doc=doc, token=token)


@app.get("/a/{token}/pdf")
def customer_pdf(token: str, session: Session = Depends(get_db)):
    doc = session.scalar(select(Document).where(Document.view_token == token)) if token else None
    if doc is None or not doc.pdf_path or not Path(doc.pdf_path).exists():
        raise HTTPException(404)
    return FileResponse(doc.pdf_path, media_type="application/pdf", filename=doc.pdf_filename or "dokument.pdf", content_disposition_type="inline")


@app.get("/o/{token}/{outcome}", response_class=HTMLResponse)
def outcome_link(request: Request, token: str, outcome: str, session: Session = Depends(get_db)):
    if outcome not in ("won", "lost", "paid", "unpaid"):
        raise HTTPException(404)
    doc = session.scalar(select(Document).where(Document.outcome_token == token)) if token else None
    if doc is None:
        raise HTTPException(404)
    label = fu_engine.set_outcome(session, doc, outcome)
    session.commit()
    return render(request, "approval.html", title="Danke, ist notiert", text=f"{doc.customer_name or doc.customer_email}: {label}.", doc=doc, fu=None)


# --- Freigabe per Link (ohne Login) ------------------------------------------------

@app.get("/f/{token}/{action}", response_class=HTMLResponse)
def approval_link(request: Request, token: str, action: str, session: Session = Depends(get_db)):
    fu = fu_engine.find_by_token(session, token)
    if fu is None:
        raise HTTPException(404)
    doc = fu.document
    if action == "send" and doc.needs_review:
        if fu_engine.release_review(session, fu):
            title, text = "Wird nachgefasst", f"Ich fasse bei {doc.customer_name or doc.customer_email} nach. Den Text bekommst du vorher noch zu sehen."
        else:
            title, text = "Nicht mehr offen", "Diese Frage ist schon beantwortet."
    elif action == "done":
        label = fu_engine.settle(session, doc)
        title, text = ("Notiert", f"{doc.customer_name or doc.customer_email}: {label}. Ich fasse dort nicht nach.") if label else ("Schon erledigt", f"Bei {doc.customer_name or doc.customer_email} wird nicht mehr nachgefasst.")
    elif action == "send":
        if fu.status == FollowUpStatus.SENT:
            title, text = "Schon gesendet", "Diese Nachfass-Mail ist bereits raus."
        elif fu_engine.approve(session, fu):
            title, text = "Gesendet", f"Die Nachfass-Mail an {doc.customer_name or doc.customer_email} ist raus, in deinem Namen."
        else:
            title, text = "Nicht gesendet", f"Das hat nicht geklappt: {fu.error or 'Freigabe war nicht mehr offen.'}"
    elif action == "skip":
        fu_engine.skip(session, fu)
        title, text = "Übersprungen", "Diese Nachfass-Mail wird nicht geschickt. Die nächste Stufe bleibt geplant."
    elif action == "stop":
        if doc.status == DocStatus.OPEN:
            fu_engine.close_document(session, doc, DocStatus.REPLIED, "Vom Betrieb gestoppt")
        title, text = "Gestoppt", f"Bei {doc.customer_name or doc.customer_email} wird nicht mehr nachgefasst."
    else:
        raise HTTPException(404)
    session.commit()
    return render(request, "approval.html", title=title, text=text, doc=doc, fu=fu)


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
    if not wa.valid_signature(body, request.headers.get("x-hub-signature-256")):
        return JSONResponse({"ok": False}, status_code=403)
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    handled = wa_inbound.process_webhook(session, data)
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
def oauth_start(key: str, business_name: str = "", owner_name: str = "", phone: str = ""):
    p = oauth.get(key)
    if p is None or not p.enabled:
        raise HTTPException(404)
    state = _oauth_state({"k": key, "b": business_name.strip()[:200], "o": owner_name.strip()[:200], "p": phone.strip()[:60]})
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
    if business is None:
        business = Business(name=values["business_name"] or email.split("@")[0], owner_name=values["owner_name"], email=email, phone=values["phone"])
        session.add(business)
        session.flush()
    else:
        business.name = values["business_name"] or business.name
        business.owner_name = values["owner_name"] or business.owner_name
        for a in business.accounts:
            a.status = AccountStatus.DISCONNECTED
    account = acc.create_account(session, business, cfg, tok["refresh_token"], auth_type="oauth")
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
    followup_offers: str = Form(""), followup_invoices: str = Form(""), paused: str = Form(""), auto_mode: str = Form(""), notify_opened: str = Form(""),
    include_view_link: str = Form(""), personalize: str = Form(""), doc_delivery: str | None = Form(None), tone: str | None = Form(None),
    whatsapp_number: str | None = Form(None), whatsapp_opt_in: str = Form(""),
):
    b = session.get(Business, business.id)
    b.name = name.strip() or b.name
    b.owner_name = owner_name.strip()
    b.phone = phone.strip()
    b.signature = signature.strip()
    b.followup_offers = bool(followup_offers)
    b.followup_invoices = bool(followup_invoices)
    b.paused = bool(paused)
    b.auto_mode = bool(auto_mode)
    b.notify_opened = bool(notify_opened)
    if doc_delivery is not None:  # Auswahl im Formular: Link / PDF anhängen / nichts
        b.include_view_link = doc_delivery in fu_engine.texts.DELIVERY
        if doc_delivery in fu_engine.texts.DELIVERY:
            b.doc_delivery = doc_delivery
    else:
        b.include_view_link = bool(include_view_link)
    if tone in fu_engine.texts.TONES:
        b.tone = tone
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
    business = auth.business_by_email(session, email)
    if business is None:
        return render(request, "login.html", next=next, sent="", error="Zu dieser Adresse ist kein Postfach verbunden.")
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
def login_link(token: str, session: Session = Depends(get_db)):
    business = auth.business_from_login_token(session, token)
    if business is None:
        raise HTTPException(status_code=400, detail="Der Login-Link ist abgelaufen oder ungültig. Fordere bitte einen neuen an.")
    business.last_login_at = utcnow()
    session.commit()
    return set_session(RedirectResponse("/dashboard", status_code=303), business)


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
    for a in business.accounts:
        if a.status != AccountStatus.DISCONNECTED:
            scan_account(session, a)
    fu_engine.process_due(session)
    session.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/datenschutz", response_class=HTMLResponse)
def privacy(request: Request):
    return render(request, "privacy.html")
