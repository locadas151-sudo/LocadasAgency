"""Angebote ohne Postfach-Zugang: Weiterleitung, CC/BCC an die persönliche Adresse, PDF oder Foto per WhatsApp.

Zwei gleichwertige Eingänge für Mails rufen process_raw(raw_bytes, envelope_rcpts) auf:
- eingebauter SMTP-Empfänger (mail/inbound_smtp.py, aiosmtpd), nur für *@INBOUND_DOMAIN
- Webhook POST /inbound/raw (Rohmail von Mailgun, SES, Postmark), Header X-Inbound-Secret

Der WhatsApp-Sekretär ruft ingest_file() für Dokumente und Fotos (notify/inbound.py).

Zuordnung:
- Betrieb über die Empfängeradresse (Envelope, To, Cc, Delivered-To). Unbekannt = abgelehnt.
- Kunde: CC/BCC auf der echten Mail an den Kunden → To-Empfänger. Weiterleitung (WG:/Fwd: oder weitergeleiteter
  Kopfblock im Text) → Empfänger aus „An:“/„To:“/„Gesendet an:“ im Kopfblock, sonst der Kunde aus dem PDF.
  Ohne Mailadresse geht es auch (dann Tipp zum Kopieren und Frage nach der Nummer).
- Sicherheit: nur bekannte Adressen, Rate-Limit je Adresse, Absender ≠ Betrieb → source=forward_unverified
  (wird trotzdem verarbeitet und im Dashboard gezeigt), Authentication-Results des Anbieters gespeichert.
- Auch für verbundene Betriebe (sevDesk/lexoffice „Kopie an“, POP3): Dubletten über Message-ID und Nummer + Kunde.
"""
from __future__ import annotations

import email
import email.policy
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import fmt
from ..config import settings
from ..db import AccountStatus, Business, DocKind, DocStatus, Document, InboundMail, log_event, utcnow
from ..extract import phones
from ..extract.classify import classify, is_forward, looks_like_customer
from ..extract.fields import extract_fields, paid_marker, utc_from_iso
from ..extract.pdftext import pdf_to_text
from ..mail.imap import Attachment, ParsedMail, decode_str, parse_message
from . import customers, taps, trial

log = logging.getLogger(__name__)

REVIEW_AFTER_DAYS = 3


@dataclass
class Result:
    status: str                 # created | duplicate | unknown | too_large | rate_limited | no_document | not_recognized | limit | invalid
    business_id: int | None = None
    document_id: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("created", "duplicate", "no_document", "not_recognized", "limit")


# --- Adressen ------------------------------------------------------------------------------------

def is_inbound(addr: str) -> bool:
    a = (addr or "").strip().lower()
    return bool(settings.inbound_domain) and a.endswith("@" + settings.inbound_domain)


def business_for_address(session: Session, addr: str) -> Business | None:
    a = (addr or "").strip().lower()
    if not is_inbound(a):
        return None
    return session.scalar(select(Business).where(Business.inbound_address == a))


def _header_addrs(msg: email.message.Message, *names: str) -> list[str]:
    vals = [decode_str(v) for n in names for v in (msg.get_all(n) or [])]
    return [a.lower() for _, a in getaddresses(vals) if a]


def recipient_addresses(raw_msg: email.message.Message, envelope_rcpts: list[str] | None) -> list[str]:
    out: list[str] = []
    for a in list(envelope_rcpts or []) + _header_addrs(raw_msg, "To", "Cc", "Delivered-To", "X-Original-To", "X-Envelope-To"):
        a = (a or "").strip().strip("<>").lower()
        if a and a not in out:
            out.append(a)
    return out


def owner_addresses(b: Business) -> set[str]:
    out = {a.email.lower() for a in b.accounts if a.email and a.status != AccountStatus.DISCONNECTED}
    if b.owner_email:
        out.add(b.owner_email)
    return out


# --- Weitergeleiteter Kopfblock ---------------------------------------------------------------------

_FWD_MARKER = re.compile(
    r"(?im)^\s*(?:-{2,}\s*)?(?:urspr(?:ü|ue)ngliche nachricht|original message|forwarded message|weitergeleitete nachricht|"
    r"anfang der weitergeleiteten nachricht|begin forwarded message|message transf(?:é|e)r(?:é|e))\b"
)
_HDR = re.compile(r"^\s*>?\s*\*?(von|from|an|to|gesendet an|empf(?:ä|ae)nger|cc|datum|date|gesendet|sent|betreff|subject)\*?\s*:\s*(.*)$", re.I)
_MONTHS = {
    "jan": 1, "januar": 1, "feb": 2, "februar": 2, "mär": 3, "märz": 3, "maerz": 3, "mrz": 3, "apr": 4, "april": 4, "mai": 5,
    "jun": 6, "juni": 6, "jul": 7, "juli": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "okt": 10,
    "oktober": 10, "nov": 11, "november": 11, "dez": 12, "dezember": 12,
    "january": 1, "february": 2, "mar": 3, "march": 3, "may": 5, "june": 6, "july": 7, "oct": 10, "october": 10, "dec": 12, "december": 12,
}
_EMAIL_IN = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


@dataclass
class ForwardHeader:
    to_name: str = ""
    to_email: str = ""
    subject: str = ""
    date: datetime | None = None
    from_email: str = ""


def _clean_value(v: str) -> str:
    return re.sub(r"\s+", " ", (v or "").replace("*", "")).strip()


def _parse_person(value: str, skip: set[str]) -> tuple[str, str]:
    """Erster Empfänger aus „Familie Brandt <brandt@web.de>“, „brandt@web.de [mailto:…]“, „'Brandt' <…>; Kollege <…>“."""
    for part in re.split(r"[;,](?=(?:[^<>]*<[^<>]*>)*[^<>]*$)", value):
        m = _EMAIL_IN.search(part)
        if not m:
            continue
        addr = m.group(0).lower().rstrip(".")
        if addr in skip or is_inbound(addr):
            continue
        name = part[: part.find("<")] if "<" in part else (part[: part.find("[")] if "[" in part else "")
        name = re.sub(r"(?i)mailto:\S+", "", name).strip(" \"'\t")
        if _EMAIL_IN.fullmatch(name or ""):
            name = ""
        return name[:200], addr
    return "", ""


def parse_date_de(value: str) -> datetime | None:
    v = _clean_value(value)
    if not v:
        return None
    v = re.sub(r"(?i)\s+(?:at|um)\s+", " ", v)
    v = re.sub(r"(?i)\s*uhr\b", "", v)
    try:
        d = parsedate_to_datetime(v)
        if d is not None:
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    tz = ZoneInfo(settings.timezone)
    hm = re.search(r"(\d{1,2}):(\d{2})", v)
    hh, mm = (int(hm.group(1)), int(hm.group(2))) if hm else (12, 0)
    m = re.search(r"(\d{1,2})\.?\s*([A-Za-zäÄöüÜ]+)\.?,?\s+(\d{4})", v)
    m_en = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", v)
    if m and m.group(2).lower() in _MONTHS:
        day, mon, year = int(m.group(1)), _MONTHS[m.group(2).lower()], int(m.group(3))
    elif m_en and m_en.group(1).lower() in _MONTHS:
        day, mon, year = int(m_en.group(2)), _MONTHS[m_en.group(1).lower()], int(m_en.group(3))
    else:
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", v)
        if not m:
            m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", v)
            if not m2:
                return None
            year, mon, day = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        else:
            day, mon, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            year += 2000 if year < 100 else 0
    try:
        return datetime(year, mon, day, hh, mm, tzinfo=tz).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_forward_header(text: str, skip: set[str] | None = None) -> ForwardHeader | None:
    """Kopfblock der weitergeleiteten Mail (Outlook, Gmail, Apple Mail, Thunderbird, GMX/WEB.DE, deutsch und englisch)."""
    skip = {s.lower() for s in (skip or set())}
    lines = (text or "").splitlines()
    start = 0
    for i, ln in enumerate(lines):
        if _FWD_MARKER.search(ln):
            start = i + 1
            break
    hdr: dict[str, str] = {}
    seen_block = False
    blank_run = 0
    for ln in lines[start:start + 40]:
        m = _HDR.match(ln)
        if m:
            key = m.group(1).lower()
            seen_block = True
            blank_run = 0
            hdr.setdefault(key, _clean_value(m.group(2)))
            continue
        if seen_block:
            if not ln.strip():
                blank_run += 1
                if blank_run >= 1 and any(k in hdr for k in ("betreff", "subject")) and any(k in hdr for k in ("an", "to", "gesendet an", "empfänger", "empfaenger")):
                    break
            elif len(hdr) >= 3:
                break
    if not hdr:
        return None
    fh = ForwardHeader()
    for k in ("an", "to", "gesendet an", "empfänger", "empfaenger"):
        if hdr.get(k):
            fh.to_name, fh.to_email = _parse_person(hdr[k], skip)
            if fh.to_email:
                break
    fh.subject = hdr.get("betreff") or hdr.get("subject") or ""
    for k in ("datum", "date", "gesendet", "sent"):
        if hdr.get(k):
            fh.date = parse_date_de(hdr[k])
            if fh.date:
                break
    for k in ("von", "from"):
        if hdr.get(k):
            m = _EMAIL_IN.search(hdr[k])
            fh.from_email = m.group(0).lower() if m else ""
            break
    if not (fh.to_email or fh.to_name or fh.subject or fh.date):
        return None
    return fh


def _attached_message(raw_msg: email.message.Message) -> email.message.Message | None:
    """Weiterleitung „als Anhang“ (message/rfc822)."""
    for part in raw_msg.walk():
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list) and payload:
                return payload[0]
    return None


def looks_forwarded(mail: ParsedMail) -> bool:
    return is_forward(mail.subject or "") or bool(_FWD_MARKER.search(mail.text or ""))


_FWD_PREFIX = re.compile(r"^(?:\s*(?:wg|fw|fwd|weitergeleitet|tr|aw|re)\s*:\s*)+", re.I)


def strip_prefix(subject: str) -> str:
    return _FWD_PREFIX.sub("", subject or "").strip()


# --- Rate-Limit, Dubletten ------------------------------------------------------------------------------

def rate_limited(session: Session, b: Business, now: datetime) -> bool:
    n = session.scalar(select(func.count(InboundMail.id)).where(InboundMail.business_id == b.id, InboundMail.created_at >= now - timedelta(hours=1))) or 0
    return n >= settings.inbound_per_hour


def _record(session: Session, b: Business, address: str, message_id: str, sender: str, result: str, doc_id: int | None, auth: str) -> None:
    session.add(InboundMail(business_id=b.id, address=address[:320], message_id=(message_id or "")[:500], sender=(sender or "")[:320],
                            result=result[:40], document_id=doc_id, auth_results=(auth or "")[:2000]))


def find_duplicate(session: Session, b: Business, number: str, kind: DocKind, customer_email: str, customer_name: str, message_id: str, pdf_path: str) -> Document | None:
    if message_id:
        d = session.scalar(select(Document).where(Document.business_id == b.id, Document.message_id == message_id))
        if d is not None:
            return d
    if number:
        for d in session.scalars(select(Document).where(Document.business_id == b.id, Document.document_number == number, Document.kind == kind)).all():
            same_mail = customer_email and d.customer_email and d.customer_email.lower() == customer_email.lower()
            same_name = customer_name and d.customer_name and d.customer_name.strip().lower() == customer_name.strip().lower()
            unknown = not (customer_email and d.customer_email) and not (customer_name and d.customer_name)
            if same_mail or same_name or unknown or not (customer_email or customer_name):
                return d
    if pdf_path:
        return session.scalar(select(Document).where(Document.business_id == b.id, Document.pdf_path == pdf_path))
    return None


# --- Kern: ein Dokument anlegen --------------------------------------------------------------------------

@dataclass
class Incoming:
    files: list[Attachment]
    subject: str = ""
    customer_name: str = ""
    customer_email: str = ""
    recipients: list[tuple[str, str]] | None = None   # CC-Fall: echte Empfänger der Mail
    sent_at: datetime | None = None
    message_id: str = ""
    source: str = "forward"
    sender: str = ""
    auth: str = ""
    thread_text: str = ""           # Mailtext (für Handynummer aus zitierter Kundenmail)
    cc: list[tuple[str, str]] | None = None


def _store(b: Business, filename: str, data: bytes) -> str:
    import hashlib

    h = hashlib.sha256(data).hexdigest()[:16]
    safe = "".join(c for c in filename if c.isalnum() or c in "._-")[:80] or "dokument.pdf"
    d = settings.pdf_dir / str(b.id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{h}_{safe}"
    if not path.exists():
        path.write_bytes(data)
    return str(path)


_NOISE = re.compile(r"(?i)\b(agb|datenblatt|datasheet|widerruf|datenschutz|produktinfo|prospekt|katalog|zertifikat|anleitung|merkblatt)\b")


def _pick(subject: str, pdfs: list[Attachment]) -> tuple[Attachment | None, str, DocKind | None]:
    best = None
    for p in pdfs:
        text = pdf_to_text(p.data)
        kind = classify(subject if len(pdfs) == 1 else "", [p.filename], text)
        score = (10 if kind is not None else 0) - (20 if _NOISE.search(p.filename) else 0) + min(len(text), 4000) // 1000
        if best is None or score > best[0]:
            best = (score, p, text)
    if best is None:
        return None, "", None
    _, pdf, text = best
    return pdf, text, classify(subject, [pdf.filename], text)


def ingest(session: Session, b: Business, inc: Incoming, now: datetime | None = None) -> Result:
    """Dateien prüfen, Felder ziehen, Dokument anlegen, Nachfassen planen, Betrieb Bescheid geben."""
    from ..extract import vision

    now = now or utcnow()
    if b.is_trial and trial.at_limit(session, b, now):
        trial.limit_notice(session, b, now)
        return Result("limit", b.id)
    own_names = tuple(n for n in (b.name, b.owner_name) if n)
    pdfs = [f for f in inc.files if f.filename.lower().endswith(".pdf") or "pdf" in f.content_type]
    images = [f for f in inc.files if f.content_type in vision.IMAGE_TYPES or f.filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".heic"))]
    subject = strip_prefix(inc.subject)
    phone_hint = ""
    fields = None
    stored_name, stored_data, text = "", b"", ""
    sent_at = inc.sent_at or now
    rec = [(n, a) for n, a in (inc.recipients or [])]
    if inc.customer_email:
        rec = [(inc.customer_name, inc.customer_email)] + [r for r in rec if r[1] != inc.customer_email]
    if pdfs:
        pdf, text, kind = _pick(subject, pdfs)
        stored_name, stored_data = pdf.filename or "dokument.pdf", pdf.data
        if kind is not None:
            fields = extract_fields(kind, subject, rec, sent_at, text, stored_name, own_names=own_names)
        elif not text.strip():
            fields, phone_hint = vision.extract(pdf.data, "application/pdf", own_names)  # Scan ohne Textebene
    elif images:
        img = images[0]
        fields, phone_hint = vision.extract(img.data, img.content_type or "image/jpeg", own_names)
        if fields is not None:
            try:
                stored_data = vision.image_to_pdf(img.data)
                stored_name = re.sub(r"\.\w+$", "", img.filename or "foto") + ".pdf"
            except Exception as exc:
                log.warning("Foto nicht als PDF speicherbar: %s", exc)
                stored_data, stored_name = b"", ""
    else:
        _tell(session, b, "Kein PDF gefunden", "Da war kein Angebot dabei. Schick mir das Angebot als PDF, als Foto oder leite die Mail mit dem PDF-Anhang weiter.", force=inc.source == "whatsapp")
        return Result("no_document", b.id)
    if fields is None:
        msg = ("Das sieht für mich nicht nach einem Angebot oder einer Rechnung aus. Wenn es doch eins ist, schick es mir bitte als PDF."
               if (pdfs or vision.available()) else "Fotos kann ich gerade nicht lesen. Schick mir das Angebot bitte als PDF.")
        _tell(session, b, "Nicht erkannt", msg, force=inc.source == "whatsapp")
        return Result("not_recognized", b.id)

    doc_kind = DocKind(fields.kind)
    pdf_path = _store(b, stored_name, stored_data) if stored_data else ""
    customer_email = (inc.customer_email or "").lower()
    if not customer_email and rec:
        rec_addrs = {a for _, a in rec}
        customer_email = (fields.customer_email if fields.customer_email in rec_addrs else rec[0][1]).lower()
    if not customer_email:
        customer_email = (fields.customer_email or "").lower()
        if customer_email and (customer_email in owner_addresses(b) or is_inbound(customer_email)):
            customer_email = ""
    customer_name = fields.customer_name or inc.customer_name or (rec[0][0] if rec else "")
    dup = find_duplicate(session, b, fields.document_number, doc_kind, customer_email, customer_name, inc.message_id, pdf_path)
    if dup is not None:
        changed = False
        if customer_email and not dup.customer_email:
            dup.customer_email, changed = customer_email, True
        if changed:
            log_event(session, b.id, "edit", f"{dup.kind_label} {dup.document_number or dup.title}: Mailadresse aus der Weiterleitung ergänzt.", dup.id)
        if b.is_trial:
            trial.touch(b, now)
        if b.is_trial or inc.source == "whatsapp":
            _tell(session, b, "Kenne ich schon", f"{trial._ref(dup)}{f' an {trial._who(dup)}' if trial._who(dup) else ''} habe ich schon. Ich fasse dort wie geplant nach.", force=True)
        return Result("duplicate", b.id, dup.id)

    account = next((a for a in b.accounts if a.status != AccountStatus.DISCONNECTED), None)
    owners = owner_addresses(b)
    source = inc.source
    if source in ("forward", "cc") and owners and inc.sender and inc.sender.lower() not in owners:
        source = "forward_unverified"
    doc = Document(
        business_id=b.id, account_id=account.id if account else None, kind=doc_kind, status=DocStatus.OPEN,
        message_id=(inc.message_id or "")[:500] if inc.source == "cc" else "", subject=(subject or "")[:500], sent_at=sent_at,
        customer_name=customer_name[:200], customer_email=customer_email[:320],
        cc_json=json.dumps(inc.cc, ensure_ascii=False) if inc.cc else "",
        document_number=fields.document_number, title=fields.title, amount_gross=fields.amount_gross,
        currency=fields.currency or "EUR", due_date=utc_from_iso(fields.due_date), valid_until=utc_from_iso(fields.valid_until),
        pdf_path=pdf_path, pdf_filename=stored_name, extraction_source=fields.source, confidence=fields.confidence,
        doc_term=fields.doc_term, iban=fields.iban, bic=fields.bic, bank_name=fields.bank_name, account_holder=fields.account_holder,
        source=source, inbound_from=(inc.sender or "")[:320], inbound_auth=(inc.auth or "")[:2000],
    )
    if doc.kind == DocKind.INVOICE and doc.due_date is None:
        doc.due_date = doc.sent_at + timedelta(days=settings.default_invoice_due_days)
    older = doc.sent_at < now - timedelta(days=REVIEW_AFTER_DAYS)
    if older and not b.is_trial:
        doc.needs_review = True  # wie beim Erst-Scan: vielleicht schon erledigt
    session.add(doc)
    session.flush()
    how = {"whatsapp": "per WhatsApp", "cc": "per Kopie (CC/BCC)", "forward_unverified": "per Weiterleitung von einem fremden Absender"}.get(source, "per Weiterleitung")
    log_event(session, b.id, "new", f"{doc.kind_label} {doc.document_number or doc.title} an {doc.customer_name or doc.customer_email or 'unbekannten Kunden'}{fmt.over(doc.amount_gross, doc.currency)} {how} erkannt.", doc.id)
    paid = paid_marker(text) if doc_kind == DocKind.INVOICE else ""
    if paid:
        doc.status, doc.closed_at, doc.close_reason = DocStatus.PAID, now, f"Laut Rechnung schon bezahlt: {paid}"[:200]
    else:
        from .followups import schedule_followups

        schedule_followups(session, doc, now)
    customers.for_document(session, doc)
    _learn_phones(session, b, doc, text, inc.thread_text, phone_hint)
    session.commit()
    if b.is_trial and not paid:
        trial.after_ingest(session, doc, first_tap_now=older and doc.kind == DocKind.OFFER, now=now)
    elif inc.source == "whatsapp":
        _tell(session, b, "Erkannt", trial.recognized_line(doc) + " Ich fasse wie gewohnt nach.", force=True)
    return Result("created", b.id, doc.id)


def split_forward(text: str) -> tuple[str, str]:
    """Weitergeleitete Mail: (Rest ab dem ersten Zitat in der eigenen Mail, eigene Mail des Betriebs).

    Ohne Weiterleitungs-Markierung bleibt der Text, wie er ist (eigene Mail = "")."""
    t = (text or "").replace("\r\n", "\n")
    m = _FWD_MARKER.search(t)
    if not m:
        return t, ""
    rest = t[m.end():].split("\n", 1)[1] if "\n" in t[m.end():] else ""
    lines = rest.split("\n")
    i = 0
    while i < len(lines) and (not lines[i].strip() or _HDR.match(lines[i])):
        i += 1  # Kopfblock (Von/An/Betreff/Datum) überspringen
    body = "\n".join(lines[i:])
    own_part, quoted = phones.split_quote(body)
    return quoted, own_part


def _learn_phones(session: Session, b: Business, doc: Document, pdf_text: str, thread: str, hint: str) -> None:
    try:
        cc = phones.country_code(pdf_text, doc.currency)
        own = taps.own_numbers(b, cc=cc)
        names = tuple(n for n in (b.name, b.owner_name) if n)
        found = phones.from_document(pdf_text, doc.customer_name, own, names, cc) if pdf_text else phones.CustomerPhones()
        if pdf_text:
            own |= phones.letterhead_numbers(pdf_text, cc)  # Briefkopf/Fußzeile gehören dem Betrieb
        thread, own_part = split_forward(thread)
        if own_part:
            # Weitergeleitet: die erste Mail ist die eigene Mail des Betriebs, ihre Signatur ist seine Nummer
            own |= {f.number for f in phones.numbers_in(own_part, cc)}
        if not found.mobile and thread:
            t = phones.from_thread(thread, own, cc)
            found.phone, found.mobile = found.phone or t.phone, t.mobile
        if not (found.mobile or found.phone) and hint:
            n = phones.normalize(hint, cc)
            if n and n not in own:
                found.mobile, found.phone = (n, "") if phones.is_mobile(n) else ("", n)
        taps.set_phones(session, doc, found.phone, found.mobile)
    except Exception as exc:  # nie daran scheitern
        log.warning("Telefonnummer für Dokument %s nicht ermittelt: %s", doc.id, exc)


def _tell(session: Session, b: Business, title: str, text: str, force: bool = False) -> None:
    """Kurzer Hinweis an den Betrieb (WhatsApp, sonst Mail). Verbundene Betriebe sehen es nur im Verlauf (außer force)."""
    log_event(session, b.id, "inbound", f"{title}: {text}")
    if not (b.is_trial or force):
        return
    from ..notify import messages as m
    from ..notify import send as notify

    notify(session, m.info(b, title, text, title, f"Hallo {b.owner_name or b.name},\n\n{text}\n\nLocadas Nachfasser"))


# --- Eingang Mail -------------------------------------------------------------------------------------

def process_raw(raw: bytes, envelope_rcpts: list[str] | None = None, auth_results: str = "", now: datetime | None = None) -> Result:
    """Eine Rohmail (SMTP oder Webhook). Öffnet eine eigene Session."""
    from ..db import new_session

    with new_session() as session:
        try:
            return process_raw_in(session, raw, envelope_rcpts, auth_results, now)
        except Exception:
            session.rollback()
            log.exception("Eingehende Mail nicht verarbeitet")
            return Result("invalid", detail="Fehler bei der Verarbeitung")


def process_raw_in(session: Session, raw: bytes, envelope_rcpts: list[str] | None = None, auth_results: str = "", now: datetime | None = None) -> Result:
    now = now or utcnow()
    if len(raw or b"") > settings.inbound_max_bytes:
        return Result("too_large")
    try:
        msg = email.message_from_bytes(raw or b"")
    except Exception:
        return Result("invalid")
    addrs = recipient_addresses(msg, envelope_rcpts)
    b, address = None, ""
    for a in addrs:
        b = business_for_address(session, a)
        if b is not None:
            address = a
            break
    if b is None:
        return Result("unknown")
    mail = parse_message(0, raw)
    auth = auth_results or decode_str(msg.get("Authentication-Results", "")) or decode_str(msg.get("ARC-Authentication-Results", ""))
    if rate_limited(session, b, now):
        _record(session, b, address, mail.message_id, mail.from_addr, "rate_limited", None, auth)
        session.commit()
        return Result("rate_limited", b.id)
    if mail.message_id and session.scalar(select(InboundMail.id).where(InboundMail.business_id == b.id, InboundMail.message_id == mail.message_id, InboundMail.result == "created")):
        return Result("duplicate", b.id)
    owners = owner_addresses(b)
    skip = owners | {address}
    inc = Incoming(files=mail.attachments, sender=mail.from_addr, auth=auth, thread_text=mail.text or "", message_id=mail.message_id)
    inner = _attached_message(msg)
    others = [(n, a) for n, a in mail.to if not is_inbound(a) and a not in owners and looks_like_customer(a, mail.from_addr)]
    if inner is not None:
        inc.source = "forward"
        to = [(decode_str(n), a.lower()) for n, a in getaddresses([decode_str(inner.get("To", ""))]) if a and a.lower() not in skip and not is_inbound(a)]
        if to:
            inc.customer_name, inc.customer_email = to[0]
        inc.subject = decode_str(inner.get("Subject", "")) or mail.subject
        inc.sent_at = parse_date_de(inner.get("Date", "")) or mail.date
    elif looks_forwarded(mail) or not others:
        inc.source = "forward"
        fh = parse_forward_header(mail.text or "", skip)
        inc.subject = (fh.subject if fh and fh.subject else mail.subject)
        if fh is not None:
            inc.customer_name, inc.customer_email = fh.to_name, fh.to_email
            if fh.date and fh.date <= now + timedelta(hours=1):
                inc.sent_at = fh.date
        inc.sent_at = inc.sent_at or mail.date
    else:
        inc.source = "cc"
        inc.subject = mail.subject
        inc.recipients = others
        inc.sent_at = mail.date
        inc.cc = [(n, a) for n, a in mail.cc if a not in owners and not is_inbound(a) and a != others[0][1]]
    if inc.sent_at and inc.sent_at > now:
        inc.sent_at = now
    result = ingest(session, b, inc, now)
    _record(session, b, address, mail.message_id, mail.from_addr, result.status, result.document_id, auth)
    session.commit()
    return result


# --- Eingang WhatsApp -------------------------------------------------------------------------------------

def ingest_file(session: Session, b: Business, data: bytes, filename: str, content_type: str, caption: str = "", message_id: str = "", now: datetime | None = None) -> Result:
    """PDF oder Foto vom WhatsApp-Sekretär. Bildunterschrift als Betreff bzw. Kundenhinweis."""
    now = now or utcnow()
    if len(data or b"") > settings.inbound_max_bytes:
        _tell(session, b, "Zu groß", "Die Datei ist zu groß (höchstens 25 MB). Schick mir bitte das PDF.", force=True)
        return Result("too_large", b.id)
    inc = Incoming(files=[Attachment(filename=filename or "dokument", content_type=content_type or "", data=data)],
                   subject=caption or "", source="whatsapp", sent_at=now, message_id="", sender=b.whatsapp_number)
    m = _EMAIL_IN.search(caption or "")
    if m:
        inc.customer_email = m.group(0).lower()
    result = ingest(session, b, inc, now)
    session.commit()
    return result
