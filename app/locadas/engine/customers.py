"""Kunden und Kundenportal: Überblick je Auftrag, Fertig-Meldung und Weg zur Google-Bewertung.

Google-Richtlinie (wichtig, nicht ändern ohne Rücksprache):
Google verbietet „Review Gating“, also zufriedene Kunden zu Google zu schicken und unzufriedene vorher
abzufangen, und auch, gezielt nur um positive Bewertungen zu bitten. Deshalb gilt hier:
- Jeder Kunde bekommt bei der Fertig-Meldung denselben Google-Link, ohne Vorfrage („Waren Sie zufrieden?“).
- Die Rückmeldung direkt an den Betrieb steht im Portal gleichrangig daneben, nie davor und nie als Filter.
- Höchstens eine Bewertungsbitte je Kunde, keine Erinnerung.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urlparse

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import Business, Customer, DocKind, DocStatus, Document, log_event, utcnow
from ..mail.imap import MailAuthError, MailConnectError
from ..mail.smtp import build_message
from ..notify import Notification
from ..notify import messages as wa_msgs
from ..notify import send as notify
from ..notify.tokens import button
from . import accounts as acc

log = logging.getLogger(__name__)


# --- Kunde finden / anlegen ----------------------------------------------------------

def get_or_create(session: Session, business_id: int, email: str, name: str = "") -> Customer | None:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return None
    c = session.scalar(select(Customer).where(Customer.business_id == business_id, Customer.email == email))
    if c is not None:
        if name and not c.name:
            c.name = name[:200]
        return c
    c = Customer(business_id=business_id, email=email[:320], name=(name or "")[:200])
    try:
        with session.begin_nested():
            session.add(c)
            session.flush()
    except IntegrityError:  # gleichzeitig angelegt
        c = session.scalar(select(Customer).where(Customer.business_id == business_id, Customer.email == email))
    return c


def for_document(session: Session, doc: Document) -> Customer | None:
    """Kunde zum Dokument; legt ihn bei älteren Dokumenten beim ersten Zugriff an."""
    return get_or_create(session, doc.business_id, doc.customer_email, doc.customer_name)


def backfill(session: Session, business_id: int) -> int:
    """Fehlende Kunden zu allen Dokumenten eines Betriebs anlegen (bestehende Daten vor dem Portal)."""
    have = set(session.scalars(select(Customer.email).where(Customer.business_id == business_id)).all())
    rows = session.execute(
        select(Document.customer_email, Document.customer_name).where(Document.business_id == business_id, Document.customer_email != "")
        .order_by(Document.sent_at)
    ).all()
    n = 0
    for email, name in rows:
        email = (email or "").lower()
        if email and email not in have:
            if get_or_create(session, business_id, email, name) is not None:
                have.add(email)
                n += 1
    return n


def documents(session: Session, c: Customer) -> list[Document]:
    """Nur die Dokumente dieses Kunden bei diesem Betrieb."""
    return list(session.scalars(
        select(Document).where(Document.business_id == c.business_id, func.lower(Document.customer_email) == c.email)
        .order_by(Document.sent_at, Document.id)
    ).all())


def by_token(session: Session, token: str) -> Customer | None:
    if not token or len(token) > 64:
        return None
    return session.scalar(select(Customer).where(Customer.portal_token == token))


def portal_url(c: Customer) -> str:
    return f"{settings.view_base_url}/k/{c.portal_token}"


def review_link(c: Customer) -> str:
    """Unser Umweg zur Google-Seite, damit der Betrieb sieht, wer geklickt hat."""
    return f"{portal_url(c)}/bewerten"


# --- Statusleiste im Portal ----------------------------------------------------------

def _won_offer(docs: list[Document]) -> Document | None:
    won = [d for d in docs if d.kind == DocKind.OFFER and d.status == DocStatus.WON]
    return won[-1] if won else None


def timeline(c: Customer, docs: list[Document]) -> list[dict]:
    """Angebot erhalten → Auftrag bestätigt → Fertig → Rechnung → Bezahlt, jeweils mit Datum oder ausgegraut."""
    offers = [d for d in docs if d.kind == DocKind.OFFER]
    invoices = [d for d in docs if d.kind == DocKind.INVOICE]
    won = _won_offer(docs)
    steps: list[dict] = []
    if offers:
        steps.append({"key": "offer", "label": f"{offers[0].term} erhalten", "date": offers[0].sent_at})
    confirmed = None
    if won is not None:
        confirmed = won.outcome_at or won.closed_at or won.sent_at
    elif invoices:
        confirmed = invoices[0].sent_at
    steps.append({"key": "confirmed", "label": "Auftrag bestätigt", "date": confirmed})
    steps.append({"key": "done", "label": "Fertig", "date": c.completed_at})
    steps.append({"key": "invoice", "label": invoices[0].term if invoices else "Rechnung", "date": invoices[0].sent_at if invoices else None})
    paid = None
    if invoices and all(d.status == DocStatus.PAID for d in invoices):
        paid = max((d.closed_at or d.sent_at) for d in invoices)
    steps.append({"key": "paid", "label": "Bezahlt", "date": paid})
    current = next((i for i, s in enumerate(steps) if s["date"] is None), len(steps))
    for i, s in enumerate(steps):
        s["done"] = s["date"] is not None
        s["current"] = i == current
    return steps


def doc_state(doc: Document) -> tuple[str, str]:
    """(Text, CSS-Klasse) für den Kunden, ohne interne Begriffe wie „wird nachgefasst“."""
    if doc.kind == DocKind.OFFER:
        if doc.status == DocStatus.WON:
            return "beauftragt", "good"
        if doc.status == DocStatus.LOST:
            return "nicht beauftragt", "muted"
        return "liegt Ihnen vor", "open"
    if doc.status == DocStatus.PAID:
        return "bezahlt", "good"
    return "offen", "open"


def job_title(docs: list[Document]) -> str:
    """Leistung für Betreff und Überschrift, z. B. „Gastherme tauschen“. Leer, wenn nichts Brauchbares da ist."""
    won = _won_offer(docs)
    for d in ([won] if won else []) + list(reversed(docs)):
        t = re.sub(r"\s+", " ", (d.title or "")).strip(" .,:;-")
        if 3 <= len(t) <= 60 and not re.search(r"\d{3,}", t):
            return t
    return ""


# --- Fertig melden -------------------------------------------------------------------

def _account(business: Business, doc: Document | None):
    if doc is not None and doc.account is not None and doc.account.status.value != "disconnected":
        return doc.account
    return next((a for a in business.accounts if a.status.value != "disconnected"), None)


def _ref_doc(docs: list[Document]) -> Document | None:
    return _won_offer(docs) or (docs[-1] if docs else None)


def completion_mail(c: Customer, docs: list[Document], with_review: bool) -> tuple[str, str]:
    """Kurz und herzlich, immer in Sie-Form. Gruß und Grußformel aus dem Stilprofil, wenn es eins gibt."""
    from . import personalize, style

    b = c.business
    ref = _ref_doc(docs)
    profile = style.load_profile(b) or {}
    profile = {**profile, "tone": "sie"}
    if ref is not None:
        hello = personalize.anrede(ref, profile)
        sig = personalize.signature(ref, profile, 1)
    else:
        hello = "Guten Tag,"
        sig = f"Viele Grüße\n{b.owner_name or b.name}"
    title = job_title(docs)
    subject = f"{title}: abgeschlossen" if title else "Ihr Auftrag ist abgeschlossen"
    lines = [
        hello,
        "",
        f"Ihr Auftrag{f' „{title}“' if title else ''} ist abgeschlossen. Vielen Dank für Ihr Vertrauen!",
        "",
        "Angebot, Rechnung und den Stand Ihres Auftrags finden Sie jederzeit hier:",
        portal_url(c),
    ]
    if with_review:
        # Für alle Kunden gleich, ohne Vorfrage nach Zufriedenheit (Google-Richtlinie, siehe Modulkopf)
        lines += [
            "",
            "Wir würden uns sehr über Ihre ehrliche Bewertung bei Google freuen. Das dauert nur eine Minute und hilft anderen bei der Suche nach einem guten Betrieb:",
            review_link(c),
        ]
    phone = (b.phone or "").strip()
    lines += ["", f"Wenn noch etwas ist, erreichen Sie uns jederzeit{f' unter {phone}' if phone else ''}.", "", sig]
    return subject, "\n".join(lines)


def _claim_completion(session: Session, c: Customer, now: datetime) -> bool:
    """Atomar „fertig“ setzen. Nur wer die Zeile umstellt, schickt die Mail (kein Doppelversand)."""
    session.flush()
    res = session.execute(
        update(Customer).where(Customer.id == c.id, Customer.completed_at.is_(None)).values(completed_at=now)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        session.rollback()
        return False
    session.commit()
    c.completed_at = now
    return True


def mark_complete(session: Session, c: Customer, now: datetime | None = None) -> str:
    """„Auftrag fertig“: Kunde bekommt genau eine Mail aus dem Postfach des Betriebs mit Portal-Link
    und, wenn hinterlegt, der Bitte um eine Google-Bewertung.

    Rückgabe: "sent", "already", "failed" oder "no_account".
    """
    now = now or utcnow()
    if c.completed_at is not None:
        return "already"
    b = c.business
    docs = documents(session, c)
    ref = _ref_doc(docs)
    account = _account(b, ref)
    if account is None:
        return "no_account"
    if not _claim_completion(session, c, now):
        return "already"
    with_review = bool(b.google_review_url) and c.review_requested_at is None
    subject, body = completion_mail(c, docs, with_review)
    msg = build_message(
        from_name=account.display_name or b.name, from_addr=account.email,
        to_name=c.name, to_addr=c.email, subject=subject, body=body,
    )
    try:
        acc.smtp_for(account).send(msg)
    except (MailAuthError, MailConnectError) as exc:
        c.completed_at = None  # nichts ging raus: später noch einmal möglich
        acc.mark_error(session, account, str(exc))
        log_event(session, b.id, "error", f"Fertig-Meldung an {c.name or c.email} ging nicht raus: {exc}"[:500], ref.id if ref else None)
        session.commit()
        return "failed"
    except Exception:
        c.completed_at = None
        session.commit()
        raise
    acc.mark_ok(account)
    from .followups import _copy_to_sent

    _copy_to_sent(account, msg)
    if with_review:
        c.review_requested_at = now
    log_event(
        session, b.id, "completed",
        f"Auftrag für {c.name or c.email} als fertig gemeldet. Der Kunde hat die Abschluss-Mail mit Link zum Kundenportal"
        f"{' und zur Google-Bewertung' if with_review else ''} bekommen.",
        ref.id if ref else None,
    )
    session.commit()
    if not b.google_review_url and b.review_hint_at is None:
        if notify(session, review_hint(b), now):
            b.review_hint_at = now
            session.commit()
    return "sent"


def review_hint(b: Business):
    text = (
        "Hinterleg deinen Google-Bewertungslink in den Einstellungen, dann leiten wir alle Kunden nach der Fertig-Meldung direkt dorthin. "
        "Jeder Kunde bekommt denselben Link, so wie Google es verlangt."
    )
    body = (
        f"Hallo {b.owner_name or b.name},\n\n"
        f"die Fertig-Meldung ist beim Kunden. {text}\n\n"
        f"Einstellungen: {settings.base_url}/einstellungen\n\nLocadas Nachfasser"
    )
    return wa_msgs.info(b, "Tipp: Google-Bewertungen", text, "Tipp: Google-Bewertungslink hinterlegen", body)


# --- Vorschlag: Rechnung erkannt, ist der Auftrag fertig? ----------------------------------

def _owner_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="locadas-fertig")


FERTIG_LINK_MAX_AGE = 60 * 60 * 24 * 60


def fertig_token(c: Customer) -> str:
    """Signierter Link für den Betrieb (nicht der Portal-Token: der Kunde soll sich nicht selbst fertig melden)."""
    return _owner_serializer().dumps(c.id)


def customer_from_fertig_token(session: Session, token: str) -> Customer | None:
    try:
        cid = int(_owner_serializer().loads(token, max_age=FERTIG_LINK_MAX_AGE))
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None
    return session.get(Customer, cid)


def fertig_url(c: Customer) -> str:
    return f"{settings.base_url}/fertig/{fertig_token(c)}"


def suggest_completion(session: Session, invoice: Document, now: datetime | None = None) -> bool:
    """Rechnung an einen Kunden mit gewonnenem Angebot: einmal fragen, ob der Auftrag fertig ist."""
    if invoice.kind != DocKind.INVOICE:
        return False
    c = for_document(session, invoice)
    if c is None or c.completed_at is not None or c.completion_asked_at is not None:
        return False
    if _won_offer(documents(session, c)) is None:
        return False
    now = now or utcnow()
    b = c.business
    who = c.name or c.email
    link = fertig_url(c)
    text = (
        f"{invoice.term} an {who} erkannt. Ist der Auftrag fertig?\n\n"
        f"Dann schicke ich {who} in deinem Namen eine kurze Abschluss-Mail mit allen Unterlagen"
        f"{' und dem Link zu deiner Google-Bewertung' if b.google_review_url else ''}."
    )
    body = (
        f"Hallo {b.owner_name or b.name},\n\n{text}\n\n"
        f"Fertig melden: {link}\n\n"
        f"Noch nicht fertig? Dann musst du nichts tun. Später geht es im Dashboard mit „Fertig melden“.\n\nLocadas Nachfasser"
    )
    n = Notification(
        business=b, kind="completion", title=f"{who}: Auftrag fertig?", text=text,
        buttons=[button("Fertig melden", "fertig", c.id), button("Noch nicht", "notyet", c.id)],
        email_subject=f"Rechnung an {who} erkannt. Ist der Auftrag fertig?", email_body=body, doc_ids=[invoice.id],
    )
    if notify(session, n, now):
        c.completion_asked_at = now
        log_event(session, b.id, "completion_asked", f"Rechnung an {who} erkannt, gefragt, ob der Auftrag fertig ist.", invoice.id)
        return True
    return False


def won_followup(doc: Document, c: Customer):
    """Nach „Auftrag bekommen“ per WhatsApp: Knopf für später, wenn der Auftrag fertig ist."""
    b = doc.business
    return Notification(
        business=b, kind="completion", title="Wenn der Auftrag fertig ist",
        text=f"Tipp dann auf „Auftrag fertig“. {c.name or c.email} bekommt eine kurze Abschluss-Mail mit allen Unterlagen.",
        buttons=[button("Auftrag fertig", "fertig", c.id), button("Später", "ok")], doc_ids=[doc.id],
    )


# --- Bewertung und Rückmeldung -------------------------------------------------------

_GOOGLE_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*google\.[a-z]{2,3}(?:\.[a-z]{2})?$")
_GPAGE_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*g\.page$")


def valid_review_url(url: str) -> str | None:
    """Bereinigter Link oder None. Erlaubt: http(s) auf google.* oder g.page."""
    url = (url or "").strip()
    if not url or len(url) > 500 or any(ch.isspace() for ch in url):
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host or p.username or p.password:
        return None
    if not (_GOOGLE_HOST.match(host) or _GPAGE_HOST.match(host)):
        return None
    return url


def record_click(session: Session, c: Customer, now: datetime | None = None) -> bool:
    """Erster Klick auf den Bewertungslink. Spätere Klicks ändern nichts."""
    if c.review_clicked_at is not None:
        return False
    c.review_clicked_at = now or utcnow()
    ref = _ref_doc(documents(session, c))
    log_event(session, c.business_id, "review_click", f"{c.name or c.email} hat den Link zur Google-Bewertung geöffnet.", ref.id if ref else None)
    return True


FEEDBACK_MAX = 2000


def save_feedback(session: Session, c: Customer, text: str, now: datetime | None = None) -> bool:
    text = re.sub(r"\r\n?", "\n", (text or "")).strip()[:FEEDBACK_MAX]
    if not text:
        return False
    now = now or utcnow()
    c.feedback_text = (f"{c.feedback_text}\n\n---\n{text}" if c.feedback_text else text)[-8000:]
    c.feedback_at = now
    b = c.business
    who = c.name or c.email
    ref = _ref_doc(documents(session, c))
    log_event(session, b.id, "feedback", f"Rückmeldung von {who} über das Kundenportal: {text[:200]}", ref.id if ref else None)
    session.commit()
    link = f"{settings.base_url}/dokument/{ref.id}" if ref else f"{settings.base_url}/dashboard"
    body = f"Hallo {b.owner_name or b.name},\n\n{who} hat dir über das Kundenportal geschrieben:\n\n» {text}\n\nAntworten kannst du direkt an {c.email}.\n{link}\n\nLocadas Nachfasser"
    short = text if len(text) <= 600 else text[:599].rstrip() + "…"
    notify(session, wa_msgs.info(b, f"Rückmeldung von {who}", f"» {short}\n\nAntworten kannst du direkt an {c.email}.", f"Rückmeldung von {who}", body), now)
    session.commit()
    return True
