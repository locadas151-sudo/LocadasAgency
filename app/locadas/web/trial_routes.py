"""Web-Routen fürs Testen ohne Postfach: /testen, /testen/los, Webhook /inbound/raw, Kontakt nachreichen bei Text-Tipps.

Eingebunden aus main.py über install(), damit main.py schlank bleibt.
"""
from __future__ import annotations

import hmac
import logging
import re
from typing import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import Business, log_event, utcnow
from ..engine import inbound_mail, taps, trial
from ..extract import phones
from ..notify import Notification
from ..notify import send as notify
from ..notify.phone import normalize_phone
from ..notify.tokens import payload
from . import auth, ratelimit

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[\w.+'-]+@[\w-]+(?:\.[\w-]+)+$")
WA_PREFILL = "Hier mein Angebot:"


def secretary_link() -> str:
    n = settings.whatsapp_display_number
    return f"https://wa.me/{n}?text={quote(WA_PREFILL, safe='')}" if n else ""


def welcome(b: Business) -> Notification:
    text = (f"Willkommen beim Test! Schick mir hier dein letztes Angebot als PDF oder Foto. Oder leite die Mail mit dem PDF an "
            f"{b.inbound_address} weiter. Ich sage dir sofort, was ich erkannt habe, und melde mich mit der fertigen Nachfass-Nachricht, wenn sie dran ist.")
    return Notification(
        business=b, kind="trial", title="Hallo, hier ist dein Locadas-Sekretär", text=text + "\n\nSchreib „abmelden“, wenn du keine Nachrichten mehr willst.",
        template="event", template_params=[(b.owner_name or "").split(" ")[0] or b.name, text], template_buttons=[payload("ok")],
        email_subject="Deine Adresse für den Locadas-Test",
        email_body=(f"Hallo {b.owner_name or b.name},\n\nleite dein letztes Angebot mit PDF an diese Adresse weiter:\n{b.inbound_address}\n\n"
                    f"Oder setz sie beim nächsten Angebot in CC oder BCC. Ich sage dir sofort, was ich erkannt habe.\n\n"
                    f"Dein Überblick: {settings.base_url}/testen/los\n\nLocadas Nachfasser"),
    )


def login_notification(b: Business) -> Notification:
    link = f"{settings.base_url}/login/{auth.login_link_token(b.id)}"
    return Notification(
        business=b, kind="login", title="Dein Login-Link", text=f"Zwei Stunden gültig:\n{link}",
        template="event", template_params=[(b.owner_name or "").split(" ")[0] or b.name, f"Dein Login-Link (2 Stunden gültig): {link}"],
        template_buttons=[payload("ok")],
        email_subject="Dein Login-Link für den Locadas Nachfasser",
        email_body=f"Hallo {b.owner_name or b.name},\n\nhier ist dein Link zum Einloggen (2 Stunden gültig):\n{link}\n\nLocadas Nachfasser",
    )


def install(app: FastAPI, render: Callable, get_db: Callable, current_business: Callable, set_session: Callable, client_ip: Callable) -> None:

    def _form(request: Request, **ctx):
        ctx.setdefault("values", {})
        ctx.setdefault("error", None)
        ctx.setdefault("business", None)
        return render(request, "testen.html", wa_link=secretary_link(), **ctx)

    @app.get("/testen", response_class=HTMLResponse)
    def trial_form(request: Request, business: Business | None = Depends(current_business)):
        if business is not None and business.is_trial:
            return RedirectResponse("/testen/los", status_code=303)
        return _form(request, business=business)

    @app.post("/testen", response_class=HTMLResponse)
    def trial_submit(
        request: Request, session: Session = Depends(get_db),
        owner_name: str = Form(""), business_name: str = Form(""), whatsapp: str = Form(""), email: str = Form(""), website: str = Form(""),
    ):
        values = dict(owner_name=owner_name, business_name=business_name, whatsapp=whatsapp, email=email)
        if website.strip():  # Honigtopf für Bots
            return RedirectResponse("/testen", status_code=303)
        if not ratelimit.allow("testen", client_ip(request)):
            return render(request, "error.html", title="Kurz durchatmen", message="Zu viele Anmeldungen in kurzer Zeit. Bitte versuch es in ein paar Minuten noch einmal.", status_code=429)
        if not owner_name.strip() or not business_name.strip():
            return _form(request, values=values, error="Bitte trag deinen Namen und deinen Betrieb ein.")
        number = normalize_phone(whatsapp)
        if not number:
            return _form(request, values=values, error="Die Handynummer konnten wir nicht lesen. Bitte so eingeben: 0171 2345678.")
        if not phones.is_mobile(number):
            return _form(request, values=values, error="Das ist eine Festnetznummer. Für WhatsApp brauchen wir deine Handynummer.")
        mail = email.strip().lower()
        if mail and not _EMAIL_RE.match(mail):
            return _form(request, values=values, error="Die Mailadresse sieht nicht richtig aus. Du kannst das Feld auch leer lassen.")
        existing = session.scalars(select(Business).where(Business.whatsapp_number == number).order_by(Business.id.desc())).first()
        if existing is None and mail:
            existing = auth.business_by_email(session, mail)
        if existing is not None:
            # Keine fremden Zugänge übernehmen: Login-Link geht an die hinterlegte Nummer bzw. Adresse
            notify(session, login_notification(existing))
            session.commit()
            return _form(request, values=values, already=True)
        b = trial.create(session, owner_name, business_name, number, mail)
        session.commit()
        if not notify(session, welcome(b)):
            log.info("Willkommensnachricht an Tester %s nicht zugestellt", b.id)
        b.last_login_at = utcnow()
        session.commit()
        return set_session(RedirectResponse("/testen/los", status_code=303), b)

    @app.get("/testen/los", response_class=HTMLResponse)
    def trial_ready(request: Request, business: Business | None = Depends(current_business), session: Session = Depends(get_db)):
        if business is None:
            return RedirectResponse("/testen", status_code=303)
        if not business.inbound_address:
            business = session.get(Business, business.id)
            business.inbound_address = trial.new_inbound_address(session, business.name)
            session.commit()
        docs = sorted(business.documents, key=lambda d: d.created_at, reverse=True)[:5]
        return render(request, "testen_los.html", business=business, docs=docs, wa_link=secretary_link(),
                      connect_url=trial.connect_url(business) if business.is_trial else "/verbinden")

    @app.post("/weiterleitung/adresse")
    def create_address(business: Business | None = Depends(current_business), session: Session = Depends(get_db)):
        """Verbundene Betriebe (sevDesk/lexoffice „Kopie an“, POP3): eigene Kopie-Adresse anlegen."""
        if business is None:
            return RedirectResponse("/login?next=/dashboard", status_code=303)
        b = session.get(Business, business.id)
        if not b.inbound_address:
            b.inbound_address = trial.new_inbound_address(session, b.name)
            log_event(session, b.id, "trial", f"Kopie-Adresse {b.inbound_address} angelegt.")
            session.commit()
        return RedirectResponse("/dashboard#kopie", status_code=303)

    @app.post("/inbound/raw")
    async def inbound_raw(request: Request):
        """Rohmail von einem Mail-Anbieter (Mailgun, SES, Postmark). Header X-Inbound-Secret, optional X-Envelope-To."""
        secret = settings.inbound_webhook_secret
        given = request.headers.get("x-inbound-secret", "")
        if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
            return JSONResponse({"ok": False, "reason": "forbidden"}, status_code=403)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.inbound_max_bytes:
            return JSONResponse({"ok": False, "reason": "too_large"}, status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > settings.inbound_max_bytes:
                return JSONResponse({"ok": False, "reason": "too_large"}, status_code=413)
        rcpts = [a.strip() for a in re.split(r"[,\s]+", request.headers.get("x-envelope-to", "") + " " + request.query_params.get("rcpt", "")) if a.strip()]
        from starlette.concurrency import run_in_threadpool

        result = await run_in_threadpool(inbound_mail.process_raw, bytes(body), rcpts, request.headers.get("x-authentication-results", ""))
        status = {"unknown": 404, "too_large": 413, "rate_limited": 429, "invalid": 400}.get(result.status, 200)
        return JSONResponse({"ok": status == 200, "result": result.status, "document_id": result.document_id}, status_code=status)

    @app.post("/t/{token}/kontakt")
    def tap_contact(token: str, session: Session = Depends(get_db), contact: str = Form("")):
        """Text-Tipp ohne Kontakt: Handynummer oder Mailadresse nachtragen, dann ist der Tipp fertig."""
        fu = taps.find(session, token)
        if fu is None:
            raise HTTPException(404)
        doc = fu.document
        parsed = trial.parse_contact(contact, doc.currency)
        if parsed is None:
            return RedirectResponse(f"/t/{token}?fehler=1", status_code=303)
        number, mail = parsed
        trial.set_contact(session, doc, number=number, email=mail)
        session.commit()
        return RedirectResponse(f"/t/{token}?ok=1", status_code=303)
