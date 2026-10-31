"""Kennzahlen fürs Admin-Cockpit und den Tagesbericht. Nur lesende Abfragen."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import (
    AccountStatus, Business, DocStatus, Document, Event, FollowUp, FollowUpStatus, MailAccount, NotificationLog, OpsLog,
    WaMessage, utcnow,
)
from . import usage

# Meta-Preise je Nachricht außerhalb des 24h-Fensters (Deutschland), Sitzungsnachrichten sind kostenlos
WA_PRICE_UTILITY_EUR = 0.0456
WA_PRICE_MARKETING_EUR = 0.1131


def day_start(now: datetime | None = None) -> datetime:
    """Beginn des heutigen Tages in lokaler Zeit, als UTC."""
    tz = ZoneInfo(settings.timezone)
    local = (now or utcnow()).astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))


def local_day(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(settings.timezone)).date().isoformat()


def _count(session: Session, q) -> int:
    return int(session.scalar(q) or 0)


def _connected_since(session: Session, since: datetime) -> int:
    return _count(session, select(func.count(func.distinct(MailAccount.business_id))).where(MailAccount.created_at >= since))


def _followups_sent(session: Session, since: datetime, until: datetime | None = None) -> int:
    q = select(func.count(FollowUp.id)).where(FollowUp.status == FollowUpStatus.SENT, FollowUp.sent_at >= since)
    if until is not None:
        q = q.where(FollowUp.sent_at < until)
    return _count(session, q)


def whatsapp_usage(session: Session, since: datetime) -> dict:
    """Gesendete WhatsApp-Nachrichten: Sekretär (notification_log) plus eigene Nummern der Betriebe (wa_messages)."""
    rows = session.execute(
        select(NotificationLog.wa_mode, func.count(NotificationLog.id))
        .where(NotificationLog.channel == "whatsapp", NotificationLog.created_at >= since).group_by(NotificationLog.wa_mode)
    ).all()
    by_mode = {m or "session": int(n) for m, n in rows}
    template_utility = by_mode.get("template", 0)
    session_n = by_mode.get("session", 0)
    template_marketing = 0
    for kind, cat, n in session.execute(
        select(WaMessage.kind, WaMessage.category, func.count(WaMessage.id))
        .where(WaMessage.created_at >= since).group_by(WaMessage.kind, WaMessage.category)
    ).all():
        if kind == "template" and cat == "marketing":
            template_marketing += int(n)
        elif kind == "template":
            template_utility += int(n)
        else:
            session_n += int(n)
    cost = template_utility * WA_PRICE_UTILITY_EUR + template_marketing * WA_PRICE_MARKETING_EUR
    return {"template": template_utility + template_marketing, "utility": template_utility, "marketing": template_marketing,
            "session": session_n, "cost_eur": round(cost, 2)}


def overview(session: Session, now: datetime | None = None) -> dict:
    now = now or utcnow()
    today = day_start(now)
    d1, d7 = now - timedelta(days=1), now - timedelta(days=7)
    k: dict = {}
    k["businesses"] = _count(session, select(func.count(Business.id)))
    k["trial"] = _count(session, select(func.count(Business.id)).where(Business.mode == "trial"))
    k["connected"] = _count(session, select(func.count(Business.id)).where(Business.mode == "connected"))
    k["paused"] = _count(session, select(func.count(Business.id)).where(Business.paused.is_(True)))
    k["signups_24h"] = _count(session, select(func.count(Business.id)).where(Business.created_at >= d1))
    k["signups_7d"] = _count(session, select(func.count(Business.id)).where(Business.created_at >= d7))
    k["connected_24h"] = _connected_since(session, d1)
    k["connected_7d"] = _connected_since(session, d7)
    k["docs"] = _count(session, select(func.count(Document.id)))
    k["docs_today"] = _count(session, select(func.count(Document.id)).where(Document.created_at >= today))
    k["docs_7d"] = _count(session, select(func.count(Document.id)).where(Document.created_at >= d7))
    k["fu_today"] = _followups_sent(session, today)
    k["fu_7d"] = _followups_sent(session, d7)
    k["taps_7d"] = _count(session, select(func.count(FollowUp.id)).where(FollowUp.tap_state == "sent", FollowUp.sent_at >= d7))
    k["replies"] = _count(session, select(func.count(Document.id)).where(Document.last_customer_reply_at.is_not(None)))
    k["replies_7d"] = _count(session, select(func.count(Document.id)).where(Document.last_customer_reply_at >= d7))
    k["wins"] = _count(session, select(func.count(Document.id)).where(Document.status.in_([DocStatus.WON, DocStatus.PAID])))
    k["accounts_error"] = _count(session, select(func.count(MailAccount.id)).where(MailAccount.status == AccountStatus.ERROR))
    k["notify_failed_24h"] = _count(session, select(func.count(NotificationLog.id)).where(NotificationLog.ok.is_(False), NotificationLog.created_at >= d1))
    k["notify_failed_7d"] = _count(session, select(func.count(NotificationLog.id)).where(NotificationLog.ok.is_(False), NotificationLog.created_at >= d7))
    k["errors_24h"] = _count(session, select(func.count(OpsLog.id)).where(OpsLog.level.in_(["ERROR", "CRITICAL"]), OpsLog.created_at >= d1))
    k["warnings_24h"] = _count(session, select(func.count(OpsLog.id)).where(OpsLog.level == "WARNING", OpsLog.created_at >= d1))
    k["claude_today"] = usage.summary(session, local_day(now))
    k["claude_7d"] = usage.summary(session, local_day(d7))
    k["wa_today"] = whatsapp_usage(session, today)
    k["wa_7d"] = whatsapp_usage(session, d7)
    return k


FUNNEL_STEPS = [
    ("signup", "Anmeldung /testen"),
    ("doc", "Erstes Dokument"),
    ("aha", "Aha-Nachricht raus"),
    ("tap", "Erster Tipp gesendet"),
    ("upgrade", "Angebot zum Verbinden"),
    ("connected", "Postfach verbunden"),
]


def funnel(session: Session, since: datetime | None = None) -> list[dict]:
    """Tester von /testen (Kohorte nach Anmeldedatum) und wie weit sie gekommen sind."""
    base = [Business.signup_source == "testen"]
    if since is not None:
        base.append(Business.created_at >= since)
    has_doc = exists().where(Document.business_id == Business.id)
    has_tap = exists().where(and_(FollowUp.document_id == Document.id, Document.business_id == Business.id, FollowUp.tap_state == "sent"))
    conds = {
        "signup": [],
        "doc": [has_doc],
        "aha": [Business.trial_aha_at.is_not(None)],
        "tap": [has_tap],
        "upgrade": [Business.upgrade_prompt_at.is_not(None)],
        "connected": [Business.mode == "connected"],
    }
    out = []
    first = None
    prev = None
    for key, label in FUNNEL_STEPS:
        n = _count(session, select(func.count(Business.id)).where(*base, *conds[key]))
        if first is None:
            first = n
        out.append({"key": key, "label": label, "n": n,
                    "pct_total": round(100 * n / first) if first else 0,
                    "pct_prev": round(100 * n / prev) if prev else (100 if n else 0)})
        prev = n
    return out


# --- Betriebsliste -------------------------------------------------------------------------------

FILTERS = ("alle", "trial", "connected", "error", "paused")


@dataclass
class Row:
    b: Business
    account: MailAccount | None
    docs: int
    followups: int
    last_activity: datetime | None
    last_error: str
    phone: str


def _phone(b: Business) -> str:
    if b.whatsapp_number:
        return b.whatsapp_number
    digits = "".join(c for c in (b.phone or "") if c.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "49" + digits[1:]
    return digits


def businesses(session: Session, q: str = "", flt: str = "alle", limit: int = 500) -> list[Row]:
    query = select(Business)
    if flt == "trial":
        query = query.where(Business.mode == "trial")
    elif flt == "connected":
        query = query.where(Business.mode == "connected")
    elif flt == "paused":
        query = query.where(Business.paused.is_(True))
    elif flt == "error":
        query = query.where(exists().where(MailAccount.business_id == Business.id, MailAccount.status == AccountStatus.ERROR))
    term = (q or "").strip().lower()
    if term:
        like = f"%{term}%"
        digits = "".join(c for c in term if c.isdigit())
        ors = [func.lower(Business.name).like(like), func.lower(Business.owner_name).like(like), func.lower(Business.email).like(like),
               func.lower(Business.inbound_address).like(like)]
        if len(digits) >= 4:
            ors += [Business.whatsapp_number.like(f"%{digits.lstrip('0')}%"), Business.phone.like(f"%{digits[-6:]}%")]
        query = query.where(or_(*ors))
    items = list(session.scalars(query.order_by(Business.created_at.desc()).limit(limit)).all())
    if not items:
        return []
    ids = [b.id for b in items]
    docs = dict(session.execute(select(Document.business_id, func.count(Document.id)).where(Document.business_id.in_(ids)).group_by(Document.business_id)).all())
    fus = dict(session.execute(
        select(Document.business_id, func.count(FollowUp.id)).join(FollowUp, FollowUp.document_id == Document.id)
        .where(Document.business_id.in_(ids), FollowUp.status == FollowUpStatus.SENT).group_by(Document.business_id)
    ).all())
    last_ev = dict(session.execute(select(Event.business_id, func.max(Event.created_at)).where(Event.business_id.in_(ids)).group_by(Event.business_id)).all())
    last_err = {}
    for bid, msg in session.execute(
        select(OpsLog.business_id, OpsLog.message).where(OpsLog.business_id.in_(ids)).order_by(OpsLog.id.desc()).limit(2000)
    ).all():
        last_err.setdefault(bid, msg)
    rows = []
    for b in items:
        account = next((a for a in b.accounts if a.status != AccountStatus.DISCONNECTED), None)
        times = [t for t in (b.last_login_at, b.trial_active_at, last_ev.get(b.id), account.last_scan_at if account else None) if t is not None]
        times = [t if t.tzinfo else t.replace(tzinfo=ZoneInfo("UTC")) for t in times]
        err = (account.last_error if account is not None and account.status == AccountStatus.ERROR else "") or last_err.get(b.id, "")
        rows.append(Row(b=b, account=account, docs=int(docs.get(b.id, 0)), followups=int(fus.get(b.id, 0)),
                        last_activity=max(times) if times else None, last_error=err, phone=_phone(b)))
    return rows


def digest(session: Session, now: datetime | None = None) -> dict:
    """Zahlen für den Tagesbericht (heute, lokale Zeit)."""
    now = now or utcnow()
    today = day_start(now)
    return {
        "signups_trial": _count(session, select(func.count(Business.id)).where(Business.created_at >= today, Business.signup_source == "testen")),
        "signups": _count(session, select(func.count(Business.id)).where(Business.created_at >= today)),
        "connected": _connected_since(session, today),
        "docs": _count(session, select(func.count(Document.id)).where(Document.created_at >= today)),
        "followups": _followups_sent(session, today),
        "taps": _count(session, select(func.count(FollowUp.id)).where(FollowUp.tap_state == "sent", FollowUp.sent_at >= today)),
        "replies": _count(session, select(func.count(Document.id)).where(Document.last_customer_reply_at >= today)),
        "errors": _count(session, select(func.count(OpsLog.id)).where(OpsLog.level.in_(["ERROR", "CRITICAL"]), OpsLog.created_at >= today)),
        "warnings": _count(session, select(func.count(OpsLog.id)).where(OpsLog.level == "WARNING", OpsLog.created_at >= today)),
        "accounts_error": _count(session, select(func.count(MailAccount.id)).where(MailAccount.status == AccountStatus.ERROR)),
        "notify_failed": _count(session, select(func.count(NotificationLog.id)).where(NotificationLog.ok.is_(False), NotificationLog.created_at >= today)),
        "claude": usage.summary(session, local_day(now), local_day(now)),
        "wa": whatsapp_usage(session, today),
    }
