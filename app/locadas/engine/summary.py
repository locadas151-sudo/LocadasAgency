"""Monatsbilanz: am 1. um 9 Uhr (lokal) eine Nachricht je Betrieb, was Locadas im Vormonat gebracht hat.

Ehrlich gezählt: Eine Antwort, ein Auftrag oder eine Zahlung zählt nur, wenn vorher mindestens eine Nachfass-Nachricht
(Mail oder WhatsApp mit einem Tipp) zu diesem Vorgang rausging. Monate ohne jede Aktivität werden übersprungen.
Das Dashboard zeigt dieselben Zahlen für den laufenden Monat („Dein Monat“).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..db import Business, DocKind, DocStatus, Document, FollowUpStatus, log_event, new_session, utcnow
from ..notify import messages as wa_msgs
from ..notify import send as notify

log = logging.getLogger(__name__)

MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember")
SEND_HOUR = 9


@dataclass
class MonthStats:
    key: str                 # "2026-10"
    label: str               # "Oktober"
    followups: int = 0       # Nachfass-Stufen raus (Mail und WhatsApp mit einem Tipp)
    via_mail: int = 0
    via_whatsapp: int = 0
    replied: int = 0         # Kunden, die nach einer Nachfass-Nachricht geantwortet haben
    won_count: int = 0       # Angebote „Auftrag bekommen“ nach Nachfassen
    won_sum: float = 0.0
    paid_count: int = 0      # Rechnungen bezahlt nach Erinnerung
    paid_sum: float = 0.0
    open_offers: int = 0     # jetzt offen
    open_invoices: int = 0
    doc_ids: list[int] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.followups or self.replied or self.won_count or self.paid_count)


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=_tz()).astimezone(timezone.utc)


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return month_start(year, month), month_start(ny, nm)


def previous_month(now: datetime) -> tuple[int, int]:
    local = now.astimezone(_tz())
    return (local.year - 1, 12) if local.month == 1 else (local.year, local.month - 1)


def _in(t: datetime | None, start: datetime, end: datetime) -> bool:
    return t is not None and start <= t < end


def compute(session: Session, business_id: int, start: datetime, end: datetime) -> MonthStats:
    """Zahlen für [start, end). Offene Vorgänge zählen wie sie jetzt sind."""
    local = start.astimezone(_tz())
    stats = MonthStats(key=f"{local.year:04d}-{local.month:02d}", label=MONTHS[local.month - 1])
    docs = session.scalars(
        select(Document).where(Document.business_id == business_id).options(selectinload(Document.followups))
    ).all()
    customers: set[str] = set()
    for d in docs:
        if d.status == DocStatus.OPEN:
            if d.kind == DocKind.OFFER:
                stats.open_offers += 1
            else:
                stats.open_invoices += 1
        sent = [f for f in d.followups if f.status == FollowUpStatus.SENT and f.sent_at is not None]
        if not sent:
            continue  # ohne Nachfassen zählt hier nichts
        first = min(f.sent_at for f in sent)
        touched = False
        for f in sent:
            if _in(f.sent_at, start, end):
                stats.followups += 1
                via = f.sent_via or "mail"
                stats.via_mail += "mail" in via
                stats.via_whatsapp += "whatsapp" in via
                touched = True
        reply = d.last_customer_reply_at
        if _in(reply, start, end) and first < reply:
            customers.add((d.customer_email or f"doc:{d.id}").lower())
            touched = True
        outcome = d.outcome_at or d.closed_at
        if _in(outcome, start, end) and first < outcome:
            if d.kind == DocKind.OFFER and d.status == DocStatus.WON:
                stats.won_count += 1
                stats.won_sum += d.amount_gross or 0.0
                touched = True
            elif d.kind == DocKind.INVOICE and d.status == DocStatus.PAID:
                stats.paid_count += 1
                stats.paid_sum += d.amount_gross or 0.0
                touched = True
        if touched:
            stats.doc_ids.append(d.id)
    stats.replied = len(customers)
    return stats


def current_month(session: Session, business_id: int, now: datetime | None = None) -> MonthStats:
    """„Dein Monat“ im Dashboard: vom 1. bis jetzt."""
    now = now or utcnow()
    local = now.astimezone(_tz())
    return compute(session, business_id, month_start(local.year, local.month), now)


def euro(v: float) -> str:
    """Ganze Euro wie im Gespräch: „21.400 €“."""
    return f"{int(round(v)):,}".replace(",", ".") + " €"


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def parts(s: MonthStats) -> list[str]:
    out = []
    if s.followups:
        out.append(f"{s.followups}× nachgefasst")
    if s.replied:
        out.append("1 Kunde hat geantwortet" if s.replied == 1 else f"{s.replied} Kunden haben geantwortet")
    if s.won_count:
        out.append(_n(s.won_count, "Auftrag", "Aufträge") + (f" über {euro(s.won_sum)}" if s.won_sum else ""))
    if s.paid_count:
        out.append(_n(s.paid_count, "Rechnung", "Rechnungen") + (f" über {euro(s.paid_sum)}" if s.paid_sum else "") + " bezahlt")
    return out


def open_line(s: MonthStats) -> str:
    items = []
    if s.open_offers:
        items.append(_n(s.open_offers, "Angebot", "Angebote"))
    if s.open_invoices:
        items.append(_n(s.open_invoices, "Rechnung", "Rechnungen"))
    return f"Offen: {' und '.join(items)}." if items else ""


def summary_text(s: MonthStats) -> str:
    """„Dein Oktober mit Locadas: 14× nachgefasst, 6 Kunden haben geantwortet, 3 Aufträge über 21.400 €, 2 Rechnungen über 3.120 € bezahlt. Offen: 5 Angebote.“"""
    text = f"Dein {s.label} mit Locadas: {', '.join(parts(s))}."
    rest = open_line(s)
    return f"{text} {rest}" if rest else text


def summary_mail(b: Business, s: MonthStats) -> tuple[str, str]:
    lines = [f"Hallo {wa_msgs.hello(b)},", "", summary_text(s), ""]
    if s.followups:
        lines.append(f"Nachgefasst: {s.followups}× ({s.via_mail} per Mail, {s.via_whatsapp} per WhatsApp)")
    if s.won_count or s.paid_count:
        lines.append("Gezählt sind nur Aufträge und Zahlungen, bei denen vorher mindestens einmal nachgefasst wurde.")
    lines += ["", f"Alle Vorgänge: {settings.base_url}/dashboard", "", "Locadas Nachfasser"]
    return f"Dein {s.label} mit Locadas", "\n".join(lines)


def due_key(now: datetime) -> str | None:
    """Monat, dessen Bilanz jetzt fällig ist ("2026-10"), oder None am 1. vor 9 Uhr."""
    local = now.astimezone(_tz())
    if local.day == 1 and local.hour < SEND_HOUR:
        return None
    y, m = previous_month(now)
    return f"{y:04d}-{m:02d}"


def _claim(session: Session, b: Business, key: str) -> str | None:
    """Atomar als verschickt markieren, damit zwei Prozesse nie doppelt schicken. Gibt den alten Wert zurück."""
    before = b.summary_sent_for or ""
    if before == key:
        return None
    res = session.execute(
        update(Business).where(Business.id == b.id, Business.summary_sent_for == before).values(summary_sent_for=key)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        session.rollback()
        return None
    session.commit()
    b.summary_sent_for = key
    return before


def send_for_business(session: Session, b: Business, now: datetime | None = None) -> str:
    """"sent", "skipped" (keine Aktivität), "already", "failed" oder "not_due"."""
    now = now or utcnow()
    key = due_key(now)
    if key is None:
        return "not_due"
    if b.summary_sent_for == key:
        return "already"
    y, m = (int(x) for x in key.split("-"))
    stats = compute(session, b.id, *month_bounds(y, m))
    before = _claim(session, b, key)
    if before is None:
        return "already"
    if not stats.active:
        return "skipped"
    text = summary_text(stats)
    subject, body = summary_mail(b, stats)
    try:
        ok = notify(session, wa_msgs.summary(b, text, subject, body), now)
    except Exception as exc:  # noqa: BLE001
        log.warning("Monatsbilanz an Betrieb %s fehlgeschlagen: %s", b.id, exc)
        ok = False
    if not ok:
        b.summary_sent_for = before  # beim nächsten Lauf noch einmal
        session.commit()
        return "failed"
    log_event(session, b.id, "summary", f"Monatsbilanz {stats.label} geschickt: {text}")
    session.commit()
    return "sent"


def send_monthly(session: Session, now: datetime | None = None) -> dict[str, int]:
    now = now or utcnow()
    out: dict[str, int] = {}
    for b in session.scalars(select(Business).order_by(Business.id)).all():
        try:
            r = send_for_business(session, b, now)
        except Exception as exc:  # noqa: BLE001  ein Betrieb hält die anderen nicht auf
            log.exception("Monatsbilanz für Betrieb %s: %s", b.id, exc)
            session.rollback()
            r = "failed"
        out[r] = out.get(r, 0) + 1
    return out


def run_monthly() -> dict[str, int]:
    """Für den Scheduler (Cron am 1. um 9 Uhr lokal)."""
    with new_session() as session:
        result = send_monthly(session)
    log.info("Monatsbilanz: %s", result)
    return result
