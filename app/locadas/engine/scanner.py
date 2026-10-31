"""Postfach-Scan: neue Angebote/Rechnungen im Gesendet-Ordner, Kundenantworten im Posteingang.

Absichtlich vorsichtig:
- Weiterleitungen (WG:/Fwd:) und Mails an Steuerberater, Kollegen, No-Reply werden ignoriert.
- Dokumente aus dem ersten Scan, die älter als ein paar Tage sind, werden nur mit Freigabe nachgefasst
  (die Rechnung könnte längst bezahlt sein).
- Abwesenheitsnotizen stoppen nichts, Unzustellbar-Meldungen stoppen das Nachfassen und melden es.
- Schickt der Betrieb selbst eine Mail ohne PDF an den Kunden, zählt das als eigenes Nachfassen:
  die nächste Stufe rückt nach hinten.
- Ein überarbeitetes Angebot mit gleicher Nummer ersetzt das alte, statt ein zweites anzulegen.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import replace
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import DocKind, DocStatus, Document, FollowUp, FollowUpStatus, MailAccount, SeenMessage, log_event, utcnow
from ..extract.classify import classify, is_forward, looks_like_customer
from ..extract.fields import extract_fields, utc_from_iso
from ..extract.pdftext import pdf_to_text
from ..mail.imap import Attachment, ImapClient, MailAuthError, MailConnectError, ParsedMail, since_days
from . import accounts as acc
from . import matcher, style
from .followups import close_document, notify_business, schedule_followups
from .matcher import MatchResult

log = logging.getLogger(__name__)

MIN_SIZE = 1_500          # darunter ist kein PDF drin
MAX_SIZE = 30_000_000     # Riesenmails überspringen
MAX_PER_RUN = 60          # pro Durchlauf, damit ein Erst-Scan nichts blockiert
REVIEW_AFTER_DAYS = 3     # Erst-Scan: ältere Dokumente nur mit Freigabe


def _seen(session: Session, account: MailAccount, message_id: str) -> bool:
    if not message_id:
        return False
    return session.scalar(select(SeenMessage.id).where(SeenMessage.account_id == account.id, SeenMessage.message_id == message_id)) is not None


def _mark_seen(session: Session, account: MailAccount, message_id: str) -> None:
    if message_id:
        session.add(SeenMessage(account_id=account.id, message_id=message_id))


def _is_own_followup(session: Session, message_id: str) -> bool:
    if not message_id:
        return False
    return session.scalar(select(FollowUp.id).where(FollowUp.message_id == message_id)) is not None


def _store_pdf(account: MailAccount, filename: str, data: bytes) -> str:
    h = hashlib.sha256(data).hexdigest()[:16]
    safe = "".join(c for c in filename if c.isalnum() or c in "._-")[:80] or "dokument.pdf"
    d = settings.pdf_dir / str(account.business_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{h}_{safe}"
    if not path.exists():
        path.write_bytes(data)
    return str(path)


_NOISE_PDF = re.compile(r"(?i)\b(agb|datenblatt|datasheet|widerruf|datenschutz|produktinfo|prospekt|katalog|zertifikat|anleitung|merkblatt)\b")


def _pick_document_pdf(subject: str, pdfs: list[Attachment]) -> tuple[Attachment, str, DocKind | None]:
    """Unter mehreren PDFs das eigentliche Angebot / die Rechnung finden (nicht AGB oder Datenblatt)."""
    best: tuple[int, Attachment, str, DocKind | None] | None = None
    for p in pdfs:
        text = pdf_to_text(p.data)
        kind = classify(subject if len(pdfs) == 1 else "", [p.filename], text)
        score = 0
        if kind is not None:
            score += 10
        if _NOISE_PDF.search(p.filename):
            score -= 20
        score += min(len(text), 4000) // 1000
        if best is None or score > best[0]:
            best = (score, p, text, kind)
    assert best is not None
    _, pdf, text, kind = best
    # Bei nur einem PDF entscheidet auch der Betreff mit
    kind = classify(subject, [pdf.filename], text)
    return pdf, text, kind


def _fmt_amount(v: float | None) -> str:
    return f" über {v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".") if v else ""


def _thread_ids(mail: ParsedMail) -> set[str]:
    ids = set(mail.references)
    if mail.in_reply_to:
        ids.add(mail.in_reply_to)
    return ids


def _doc_in_thread(session: Session, account: MailAccount, ids: set[str], customer: str | None = None) -> Document | None:
    open_docs = session.scalars(select(Document).where(Document.account_id == account.id, Document.status == DocStatus.OPEN)).all()
    for doc in open_docs:
        if doc.message_id and doc.message_id in ids:
            return doc
        if any(f.message_id and f.message_id in ids for f in doc.followups if f.status == FollowUpStatus.SENT):
            return doc
    if customer:
        matches = [d for d in open_docs if d.customer_email and d.customer_email == customer]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return max(matches, key=lambda d: d.sent_at)
    return None


def _push_back(doc: Document, now) -> None:
    """Betrieb hat selbst nachgefasst: offene Stufen rücken ab jetzt nach hinten."""
    pending = sorted((f for f in doc.followups if f.status == FollowUpStatus.SCHEDULED), key=lambda f: f.step)
    if not pending:
        return
    gap = timedelta(days=4)
    for i, f in enumerate(pending):
        f.due_at = max(f.due_at, now + gap * (i + 1))
    doc.next_followup_at = pending[0].due_at


def _reschedule_from(doc: Document, new_base) -> None:
    """Überarbeitetes Angebot: Rhythmus neu ab dem neuen Versanddatum."""
    offsets = settings.offer_followup_days if doc.kind == DocKind.OFFER else settings.invoice_followup_days
    base = new_base if doc.kind == DocKind.OFFER else (doc.due_date or new_base)
    now = utcnow()
    for f in doc.followups:
        if f.status in (FollowUpStatus.SCHEDULED, FollowUpStatus.AWAITING_APPROVAL, FollowUpStatus.SKIPPED):
            idx = min(f.step - 1, len(offsets) - 1)
            f.status = FollowUpStatus.SCHEDULED
            f.due_at = max(base + timedelta(days=offsets[idx]), now + timedelta(hours=1))
            f.subject, f.body = "", ""
    pending = [f.due_at for f in doc.followups if f.status == FollowUpStatus.SCHEDULED]
    doc.next_followup_at = min(pending) if pending else None


def handle_manual_mail(session: Session, account: MailAccount, mail: ParsedMail) -> Document | None:
    """Mail ohne PDF aus dem Gesendet-Ordner: Hat der Betrieb selbst beim Kunden nachgefasst?"""
    recipients = [a for _, a in mail.to if looks_like_customer(a, account.email)]
    if not recipients:
        return None
    doc = _doc_in_thread(session, account, _thread_ids(mail))
    if doc is None:
        # Ohne Thread-Bezug nur, wenn der Betreff klar auf das Dokument zeigt
        for r in recipients:
            cand = _doc_in_thread(session, account, set(), customer=r)
            if cand and ((cand.document_number and cand.document_number in mail.subject) or (cand.subject and cand.subject.lower() in mail.subject.lower())):
                doc = cand
                break
    if doc is None:
        return None
    _push_back(doc, utcnow())
    log_event(session, doc.business_id, "manual", f"Du hast selbst bei {doc.customer_name or doc.customer_email} nachgefasst. Nächste Stufe rückt nach hinten.", doc.id)
    return doc


def process_sent_mail(session: Session, account: MailAccount, mail: ParsedMail, first_run: bool = False) -> Document | None:
    """Eine gesendete Mail prüfen und ggf. als Dokument anlegen oder aktualisieren."""
    if is_forward(mail.subject):
        return None
    style.collect_sample(session, account, mail)  # Stilprobe für personalisierte Nachfass-Texte, wirft nie
    pdfs = mail.pdf_attachments
    if not pdfs:
        handle_manual_mail(session, account, mail)
        return None
    recipients = [(n, a) for n, a in mail.to if looks_like_customer(a, account.email)]
    if not recipients:
        return None
    main_pdf, text, kind = _pick_document_pdf(mail.subject, pdfs)
    if kind is None:
        return None
    fields = extract_fields(kind, mail.subject, recipients, mail.date, text)
    doc_kind = DocKind(fields.kind)
    customer_email = (fields.customer_email or recipients[0][1]).lower()
    # Kunde laut PDF ist nicht unter den Empfängern (z. B. Mail ging an Hausverwaltung): Mail-Empfänger nehmen
    if customer_email not in {a for _, a in recipients}:
        customer_email = recipients[0][1]

    # Gleiche Nummer schon da? Dann ist das eine Korrektur / Neufassung oder ein erneuter Versand
    if fields.document_number:
        existing = session.scalar(
            select(Document).where(
                Document.business_id == account.business_id,
                Document.document_number == fields.document_number,
                Document.kind == doc_kind,
            )
        )
        if existing is not None:
            if existing.status == DocStatus.OPEN and mail.date > existing.sent_at:
                existing.sent_at = mail.date
                existing.message_id = mail.message_id or existing.message_id
                existing.subject = mail.subject[:500]
                existing.pdf_path = _store_pdf(account, main_pdf.filename, main_pdf.data)
                existing.pdf_filename = main_pdf.filename
                if fields.amount_gross is not None:
                    existing.amount_gross = fields.amount_gross
                if doc_kind == DocKind.INVOICE and fields.due_date:
                    existing.due_date = utc_from_iso(fields.due_date)
                _reschedule_from(existing, mail.date)
                log_event(session, existing.business_id, "updated", f"{existing.kind_label} {existing.document_number} wurde neu verschickt{_fmt_amount(existing.amount_gross)}. Nachfassen startet neu.", existing.id)
            return None

    doc = Document(
        business_id=account.business_id,
        account_id=account.id,
        kind=doc_kind,
        status=DocStatus.OPEN,
        message_id=mail.message_id,
        imap_uid=mail.uid,
        subject=mail.subject[:500],
        sent_at=mail.date,
        customer_name=fields.customer_name or recipients[0][0],
        customer_email=customer_email,
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
    if first_run and doc.sent_at < utcnow() - timedelta(days=REVIEW_AFTER_DAYS):
        doc.needs_review = True
    # Unsichere Erkennung: lieber fragen als falsch nachfassen
    if doc.confidence < 0.3 and doc.extraction_source == "heuristic" and doc.amount_gross is None:
        doc.needs_review = True
    session.add(doc)
    session.flush()
    schedule_followups(session, doc)
    log_event(session, doc.business_id, "new", f"{doc.kind_label} {doc.document_number or doc.title} an {doc.customer_name or doc.customer_email}{_fmt_amount(doc.amount_gross)} erkannt.", doc.id)
    return doc


def scan_sent(session: Session, account: MailAccount, client: ImapClient) -> int:
    validity, uidnext = client.select(account.sent_folder)
    first_run = account.sent_uidvalidity is None
    if account.sent_uidvalidity is not None and validity != account.sent_uidvalidity:
        # Ordner wurde neu aufgebaut, UIDs ungültig: neu lesen, Dubletten verhindert SeenMessage
        account.sent_last_uid = 0
    account.sent_uidvalidity = validity
    since = since_days(settings.initial_scan_days) if first_run else None
    uids = client.search_uids(account.sent_last_uid + 1, since)
    created = 0
    for uid in uids[:MAX_PER_RUN]:
        size = client.fetch_message_size(uid)
        if size and size > MAX_SIZE:
            account.sent_last_uid = max(account.sent_last_uid, uid)
            continue
        mail = client.fetch_message(uid)
        account.sent_last_uid = max(account.sent_last_uid, uid)
        if mail is None or _seen(session, account, mail.message_id):
            continue
        _mark_seen(session, account, mail.message_id)
        if _is_own_followup(session, mail.message_id):
            continue
        if first_run and not mail.pdf_attachments:
            continue  # alte Mails ohne PDF sind beim Erst-Scan uninteressant
        try:
            if process_sent_mail(session, account, mail, first_run=first_run) is not None:
                created += 1
        except Exception as exc:  # pragma: no cover
            log.exception("Fehler beim Verarbeiten von UID %s: %s", uid, exc)
        session.commit()
    if len(uids) <= MAX_PER_RUN:
        account.sent_last_uid = max(account.sent_last_uid, uidnext - 1)
    return created


_BOUNCE_ADDR = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def _handle_bounce(session: Session, account: MailAccount, mail: ParsedMail, client: ImapClient) -> bool:
    full = client.fetch_message(mail.uid) or mail
    body = full.text + "\n" + "\n".join(a.data.decode("utf-8", "replace") for a in full.attachments if a.content_type.startswith(("message/", "text/")))
    ids = set(re.findall(r"<[^<>\s]+@[^<>\s]+>", body)) | _thread_ids(full)
    doc = _doc_in_thread(session, account, ids)
    if doc is None:
        for addr in {a.lower() for a in _BOUNCE_ADDR.findall(body)}:
            if addr == account.email or "daemon" in addr or "postmaster" in addr:
                continue
            doc = _doc_in_thread(session, account, set(), customer=addr)
            if doc:
                break
    if doc is None:
        return False
    close_document(session, doc, DocStatus.BOUNCED, f"Adresse {doc.customer_email} unzustellbar")
    session.commit()
    notify_business(
        session, doc.business,
        f"Mail an {doc.customer_name or doc.customer_email} kam nicht an",
        f"Hallo {doc.business.owner_name or doc.business.name},\n\n"
        f"die Nachfass-Mail zu {doc.kind_label} {doc.document_number or doc.title} an {doc.customer_email} ist zurückgekommen, die Adresse ist unzustellbar. "
        f"Ich fasse dort nicht mehr nach.\n\nKorrigier die Adresse hier und schalte das Nachfassen wieder ein: {settings.base_url}/dokument/{doc.id}\n\nLocadas Nachfasser",
    )
    return True


MAX_CLASSIFY_PER_RUN = 40  # Claude-Aufrufe je Posteingangs-Durchlauf, danach wie ohne Claude


def _mail_body(client: ImapClient, mail: ParsedMail) -> str:
    """Nur Text, gekürzt, nur für den Claude-Aufruf (wird nicht gespeichert)."""
    try:
        return client.fetch_text(mail.uid, limit=20_000)
    except Exception as exc:
        log.warning("Text von UID %s nicht lesbar: %s", mail.uid, exc)
        return ""


def _match_free_mail(session: Session, account: MailAccount, client: ImapClient, mail: ParsedMail, budget: list[int]) -> tuple[Document | None, MatchResult | None]:
    """Mail ohne Thread-Bezug: exakte Kundenadresse wie bisher, sonst bzw. zusätzlich Claude entscheiden lassen."""
    fallback = _doc_in_thread(session, account, set(), customer=mail.from_addr)
    if not matcher.enabled() or budget[0] <= 0:
        return fallback, None
    if matcher.is_newsletter(mail) or not looks_like_customer(mail.from_addr, account.email):
        return fallback, None
    open_docs = list(session.scalars(
        select(Document).where(Document.business_id == account.business_id, Document.status == DocStatus.OPEN)
    ).all())
    if not open_docs:
        return fallback, None
    budget[0] -= 1
    result = matcher.classify_mail(mail, _mail_body(client, mail), open_docs)
    if result is None:
        return fallback, None
    if result.matched:
        return next(d for d in open_docs if d.id == result.doc_id), result
    if fallback is not None:
        # Bekannter Kunde, Claude unsicher: wie bisher stoppen, Status aber nicht aus der Absicht ableiten
        return fallback, replace(result, doc_id=fallback.id, confidence=0.0)
    return None, None


def _classify_thread_reply(client: ImapClient, mail: ParsedMail, doc: Document, budget: list[int]) -> MatchResult | None:
    if not matcher.enabled() or budget[0] <= 0:
        return None
    budget[0] -= 1
    try:
        return matcher.classify_mail(mail, _mail_body(client, mail), [doc], known=doc)
    except Exception as exc:  # nie das Schließen verhindern
        log.warning("Einordnung der Antwort fehlgeschlagen: %s", exc)
        return None


_STATUS_SUBJECT = {DocStatus.WON: "hat zugesagt", DocStatus.LOST: "hat abgesagt", DocStatus.PAID: "hat bezahlt"}
_STATUS_NOTE = {
    DocStatus.WON: "Ich habe das Angebot als „Auftrag bekommen“ markiert und fasse nicht mehr nach.",
    DocStatus.LOST: "Ich habe das Angebot als Absage markiert und fasse nicht mehr nach.",
    DocStatus.PAID: "Ich habe die Rechnung als bezahlt markiert und fasse nicht mehr nach. Prüf bitte trotzdem den Zahlungseingang.",
}


def _close_on_reply(session: Session, doc: Document, mail: ParsedMail, result: MatchResult | None) -> DocStatus:
    status = DocStatus.REPLIED
    doc.reply_intent, doc.reply_summary = "", ""
    if result is not None:
        doc.reply_intent = result.intent
        doc.reply_summary = result.summary[:300]
        if result.matched and result.doc_id == doc.id:
            status = matcher.status_for(doc, result.intent)
    doc.last_customer_reply_at = mail.date
    close_document(session, doc, status, f"Antwort von {mail.from_addr}")
    if doc.reply_summary:
        log_event(session, doc.business_id, "reply", f"{doc.reply_intent_label}: {doc.reply_summary}", doc.id)
    return status


def _notify_reply(session: Session, doc: Document, mail: ParsedMail, status: DocStatus) -> None:
    who = doc.customer_name or mail.from_addr
    ref = f"{doc.kind_label} {doc.document_number or doc.title}"
    lines = [f"Hallo {doc.business.owner_name or doc.business.name},", ""]
    lines.append(f"{who} hat auf {ref} geantwortet (Betreff: {mail.subject}).")
    if doc.reply_summary:
        lines += ["", f"» {doc.reply_summary}"]
    if doc.customer_email and mail.from_addr != doc.customer_email:
        lines += ["", f"Die Mail kam von {mail.from_addr}, nicht von {doc.customer_email}. Ich habe sie anhand des Inhalts zugeordnet."]
    lines += ["", _STATUS_NOTE.get(status, "Ich fasse dort nicht mehr nach."), ""]
    lines.append(f"Wenn das nicht stimmt oder du doch weiter nachfassen willst, kannst du das hier ändern: {settings.base_url}/dokument/{doc.id}")
    lines += ["", "Locadas Nachfasser"]
    notify_business(session, doc.business, f"{who} {_STATUS_SUBJECT.get(status, 'hat geantwortet')} ({ref})", "\n".join(lines))


def scan_inbox(session: Session, account: MailAccount, client: ImapClient) -> int:
    validity, uidnext = client.select(account.inbox_folder)
    if account.inbox_uidvalidity is None or validity != account.inbox_uidvalidity:
        # Erstlauf: nur ab jetzt, alte Antworten gelten nicht
        account.inbox_uidvalidity = validity
        account.inbox_last_uid = max(0, uidnext - 1)
        return 0
    uids = client.search_uids(account.inbox_last_uid + 1)
    replies = 0
    budget = [MAX_CLASSIFY_PER_RUN]
    for uid in uids[:200]:
        mail = client.fetch_headers(uid)
        account.inbox_last_uid = max(account.inbox_last_uid, uid)
        if mail is None or not mail.from_addr or mail.from_addr == account.email:
            continue
        if mail.is_bounce:
            _handle_bounce(session, account, mail, client)
            continue
        # Billig zuerst: Thread-Bezug über In-Reply-To/References
        doc = _doc_in_thread(session, account, _thread_ids(mail))
        if mail.is_auto_reply:
            doc = doc or _doc_in_thread(session, account, set(), customer=mail.from_addr)
            if doc is not None:
                log_event(session, doc.business_id, "autoreply", f"Abwesenheitsnotiz von {mail.from_addr}. Ich fasse trotzdem weiter nach.", doc.id)
                session.commit()
            continue
        if doc is not None:
            result = _classify_thread_reply(client, mail, doc, budget)
        else:
            doc, result = _match_free_mail(session, account, client, mail, budget)
        if doc is None:
            continue
        status = _close_on_reply(session, doc, mail, result)
        replies += 1
        session.commit()
        if doc.business.notify_opened:
            _notify_reply(session, doc, mail, status)
    if len(uids) <= 200:
        account.inbox_last_uid = max(account.inbox_last_uid, uidnext - 1)
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
