"""Postfach-Scan: neue Angebote/Rechnungen im Gesendet-Ordner, Kundenantworten im Posteingang."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import DocKind, DocStatus, Document, FollowUpStatus, MailAccount, SeenMessage, log_event, utcnow
from ..extract.classify import classify, looks_like_customer
from ..extract.fields import extract_fields, utc_from_iso
from ..extract.pdftext import pdf_to_text
from ..mail.imap import ImapClient, MailAuthError, MailConnectError, ParsedMail, since_days
from . import accounts as acc
from . import style
from .followups import close_document, notify_business, schedule_followups

log = logging.getLogger(__name__)

MIN_SIZE = 1_500          # darunter ist kein PDF drin
MAX_SIZE = 30_000_000     # Riesenmails überspringen
MAX_PER_RUN = 60          # pro Durchlauf, damit ein Erst-Scan nichts blockiert


def _seen(session: Session, account: MailAccount, message_id: str) -> bool:
    if not message_id:
        return False
    return session.scalar(select(SeenMessage.id).where(SeenMessage.account_id == account.id, SeenMessage.message_id == message_id)) is not None


def _mark_seen(session: Session, account: MailAccount, message_id: str) -> None:
    if message_id:
        session.add(SeenMessage(account_id=account.id, message_id=message_id))


def _store_pdf(account: MailAccount, filename: str, data: bytes) -> str:
    h = hashlib.sha256(data).hexdigest()[:16]
    safe = "".join(c for c in filename if c.isalnum() or c in "._-")[:80] or "dokument.pdf"
    d = settings.pdf_dir / str(account.business_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{h}_{safe}"
    if not path.exists():
        path.write_bytes(data)
    return str(path)


def process_sent_mail(session: Session, account: MailAccount, mail: ParsedMail) -> Document | None:
    """Eine gesendete Mail prüfen und ggf. als Dokument anlegen."""
    style.collect_sample(session, account, mail)  # Stilprobe für personalisierte Nachfass-Texte, wirft nie
    pdfs = mail.pdf_attachments
    if not pdfs:
        return None
    recipients = [(n, a) for n, a in mail.to if looks_like_customer(a, account.email)]
    if not recipients:
        return None
    filenames = [p.filename for p in pdfs]
    # Das größte PDF ist in der Regel das Dokument (AGB/Datenblätter sind meist kleiner oder gleich groß, egal)
    main_pdf = max(pdfs, key=lambda p: len(p.data))
    text = pdf_to_text(main_pdf.data)
    kind = classify(mail.subject, filenames, text)
    if kind is None:
        return None
    # Bereits ein offenes Dokument an diesen Kunden mit derselben Nummer? (Mail nochmal geschickt)
    fields = extract_fields(kind, mail.subject, recipients, mail.date, text)
    if fields.document_number:
        dup = session.scalar(
            select(Document).where(
                Document.business_id == account.business_id,
                Document.document_number == fields.document_number,
                Document.kind == DocKind(fields.kind),
            )
        )
        if dup is not None:
            return None
    doc = Document(
        business_id=account.business_id,
        account_id=account.id,
        kind=DocKind(fields.kind),
        status=DocStatus.OPEN,
        message_id=mail.message_id,
        imap_uid=mail.uid,
        subject=mail.subject[:500],
        sent_at=mail.date,
        customer_name=fields.customer_name or recipients[0][0],
        customer_email=fields.customer_email or recipients[0][1],
        document_number=fields.document_number,
        title=fields.title,
        amount_gross=fields.amount_gross,
        currency=fields.currency or "EUR",
        due_date=utc_from_iso(fields.due_date),
        valid_until=utc_from_iso(fields.valid_until),
        pdf_path=_store_pdf(account, main_pdf.filename, main_pdf.data),
        pdf_filename=main_pdf.filename,
        extraction_source=fields.source,
        confidence=fields.confidence,
    )
    if doc.kind == DocKind.INVOICE and doc.due_date is None:
        doc.due_date = doc.sent_at + timedelta(days=settings.default_invoice_due_days)
    session.add(doc)
    session.flush()
    schedule_followups(session, doc)
    betrag = f" über {doc.amount_gross:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".") if doc.amount_gross else ""
    log_event(session, doc.business_id, "new", f"{doc.kind_label} {doc.document_number or doc.title} an {doc.customer_name or doc.customer_email}{betrag} erkannt.", doc.id)
    return doc


def scan_sent(session: Session, account: MailAccount, client: ImapClient) -> int:
    validity, uidnext = client.select(account.sent_folder)
    first_run = account.sent_uidvalidity is None
    if account.sent_uidvalidity is not None and validity != account.sent_uidvalidity:
        # Ordner wurde neu aufgebaut, UIDs ungültig: nur ab jetzt weiterlesen
        account.sent_last_uid = 0
        first_run = True
    account.sent_uidvalidity = validity
    since = since_days(settings.initial_scan_days) if first_run else None
    uids = client.search_uids(account.sent_last_uid + 1, since)
    created = 0
    for uid in uids[:MAX_PER_RUN]:
        size = client.fetch_message_size(uid)
        if size and (size < MIN_SIZE or size > MAX_SIZE):
            account.sent_last_uid = max(account.sent_last_uid, uid)
            continue
        mail = client.fetch_message(uid)
        account.sent_last_uid = max(account.sent_last_uid, uid)
        if mail is None or _seen(session, account, mail.message_id):
            continue
        _mark_seen(session, account, mail.message_id)
        try:
            if process_sent_mail(session, account, mail) is not None:
                created += 1
        except Exception as exc:  # pragma: no cover
            log.exception("Fehler beim Verarbeiten von UID %s: %s", uid, exc)
        session.commit()
    if len(uids) <= MAX_PER_RUN:
        account.sent_last_uid = max(account.sent_last_uid, uidnext - 1)
    return created


def _match_reply(session: Session, account: MailAccount, mail: ParsedMail) -> Document | None:
    ids = set(mail.references) | ({mail.in_reply_to} if mail.in_reply_to else set())
    open_docs = session.scalars(
        select(Document).where(Document.account_id == account.id, Document.status == DocStatus.OPEN)
    ).all()
    for doc in open_docs:
        if doc.message_id and doc.message_id in ids:
            return doc
        if any(f.message_id and f.message_id in ids for f in doc.followups if f.status == FollowUpStatus.SENT):
            return doc
    # Kein Thread-Bezug: Absender ist Kunde eines offenen Dokuments
    for doc in open_docs:
        if doc.customer_email and doc.customer_email == mail.from_addr:
            return doc
    return None


def scan_inbox(session: Session, account: MailAccount, client: ImapClient) -> int:
    validity, uidnext = client.select(account.inbox_folder)
    if account.inbox_uidvalidity is None or validity != account.inbox_uidvalidity:
        # Erstlauf: nur ab jetzt, alte Antworten gelten nicht
        account.inbox_uidvalidity = validity
        account.inbox_last_uid = max(0, uidnext - 1)
        return 0
    uids = client.search_uids(account.inbox_last_uid + 1)
    replies = 0
    for uid in uids[:200]:
        mail = client.fetch_headers(uid)
        account.inbox_last_uid = max(account.inbox_last_uid, uid)
        if mail is None or not mail.from_addr or mail.from_addr == account.email:
            continue
        doc = _match_reply(session, account, mail)
        if doc is None:
            continue
        doc.last_customer_reply_at = mail.date
        close_document(session, doc, DocStatus.REPLIED, f"Antwort von {mail.from_addr}")
        replies += 1
        session.commit()
        if doc.business.notify_opened:
            notify_business(
                session, doc.business,
                f"{doc.customer_name or mail.from_addr} hat geantwortet ({doc.kind_label} {doc.document_number or doc.title})",
                f"Hallo {doc.business.owner_name or doc.business.name},\n\n"
                f"{doc.customer_name or mail.from_addr} hat auf {doc.kind_label} {doc.document_number or doc.title} geantwortet (Betreff: {mail.subject}). "
                f"Ich fasse dort nicht mehr nach.\n\n"
                f"Wenn du doch weiter nachfassen willst, kannst du das im Dashboard wieder einschalten: {settings.base_url}/dokument/{doc.id}\n\n"
                f"Locadas Nachfasser",
            )
    account.inbox_last_uid = max(account.inbox_last_uid, uidnext - 1) if len(uids) <= 200 else account.inbox_last_uid
    return replies


def scan_account(session: Session, account: MailAccount) -> dict[str, int]:
    stats = {"new": 0, "replies": 0}
    if account.status.value == "disconnected":
        return stats
    try:
        with acc.imap_for(account) as client:
            stats["new"] = scan_sent(session, account, client)
            stats["replies"] = scan_inbox(session, account, client)
        acc.mark_ok(account)
    except (MailAuthError, MailConnectError) as exc:
        acc.mark_error(session, account, str(exc))
        log.warning("Scan für %s fehlgeschlagen: %s", account.email, exc)
    account.last_scan_at = utcnow()
    session.commit()
    return stats


def first_scan_summary(session: Session, business_id: int) -> tuple[int, int]:
    docs = session.scalars(select(Document).where(Document.business_id == business_id)).all()
    offers = sum(1 for d in docs if d.kind == DocKind.OFFER)
    invoices = sum(1 for d in docs if d.kind == DocKind.INVOICE)
    return offers, invoices
