"""Monatsbilanz: Zahlen, ehrliche Zuordnung (nur nach Nachfassen), leere Monate, Idempotenz, Dashboard-Karte. Ohne Netzwerk."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from locadas.crypto import encrypt
from locadas.db import Business, DocKind, DocStatus, Document, FollowUp, FollowUpStatus, MailAccount, new_session, utcnow
from locadas.engine import summary
from locadas.web import auth
from tests.test_whatsapp import NUM, graph  # noqa: F401  (Fixture)

BERLIN = ZoneInfo("Europe/Berlin")
NOV_1_0930 = datetime(2026, 11, 1, 9, 30, tzinfo=BERLIN).astimezone(timezone.utc)


def _oct(day: int, hour: int = 10) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=BERLIN).astimezone(timezone.utc)


def _biz(s, whatsapp: bool = True) -> tuple[Business, MailAccount]:
    b = Business(name="Müller Haustechnik GmbH", owner_name="Thomas Müller", email="info@mueller-shk.de",
                 whatsapp_number=NUM if whatsapp else "", whatsapp_opt_in=whatsapp)
    s.add(b)
    s.flush()
    a = MailAccount(business_id=b.id, provider="custom", email=b.email, username=b.email, secret_enc=encrypt("x"),
                    imap_host="127.0.0.1", imap_port=1, imap_ssl=False, smtp_host="127.0.0.1", smtp_port=1, smtp_ssl=False)
    s.add(a)
    s.flush()
    return b, a


_n = [0]


def _doc(s, b, a, kind, status, amount=None, sent=None, fus=(), reply=None, outcome=None, email=None) -> Document:
    _n[0] += 1
    d = Document(business_id=b.id, account_id=a.id, kind=kind, status=status, amount_gross=amount,
                 sent_at=sent or _oct(1), customer_email=email or f"kunde{_n[0]}@example.org", customer_name=f"Kunde {_n[0]}",
                 document_number=f"N-{_n[0]}", last_customer_reply_at=reply, closed_at=outcome)
    s.add(d)
    s.flush()
    for step, (when, via) in enumerate(fus, start=1):
        s.add(FollowUp(document_id=d.id, step=step, due_at=when, status=FollowUpStatus.SENT, sent_at=when, sent_via=via))
    s.flush()
    return d


def _october(s):
    b, a = _biz(s)
    O, I = DocKind.OFFER, DocKind.INVOICE
    _doc(s, b, a, O, DocStatus.WON, 12000, fus=[(_oct(5), "mail")], reply=_oct(9), outcome=_oct(10))
    _doc(s, b, a, O, DocStatus.WON, 9400, fus=[(_oct(3), "whatsapp_tap"), (_oct(8), "mail")], outcome=_oct(20))
    _doc(s, b, a, O, DocStatus.WON, 5000, outcome=_oct(12))                                       # ohne Nachfassen: zählt nicht
    _doc(s, b, a, O, DocStatus.WON, 7000, fus=[(_oct(15), "mail")], outcome=_oct(14))              # Zusage vor dem Nachfassen
    _doc(s, b, a, I, DocStatus.PAID, 1120, fus=[(_oct(2), "mail")], outcome=_oct(6))
    _doc(s, b, a, I, DocStatus.PAID, 2000, fus=[(datetime(2026, 9, 25, 8, tzinfo=timezone.utc), "mail")], outcome=_oct(3))
    _doc(s, b, a, O, DocStatus.REPLIED, 800, fus=[(_oct(1), "mail")], reply=_oct(11), outcome=_oct(11))
    _doc(s, b, a, O, DocStatus.REPLIED, 600, reply=_oct(11), outcome=_oct(11))                     # Antwort ohne Nachfassen
    _doc(s, b, a, O, DocStatus.OPEN, 3000)
    _doc(s, b, a, O, DocStatus.OPEN, 4000)
    s.commit()
    return b


def test_numbers_and_honest_attribution(fresh_db):
    with new_session() as s:
        b = _october(s)
        st = summary.compute(s, b.id, *summary.month_bounds(2026, 10))
        assert (st.followups, st.via_mail, st.via_whatsapp) == (6, 5, 1)
        assert st.replied == 2
        assert (st.won_count, st.won_sum) == (2, 21400)
        assert (st.paid_count, st.paid_sum) == (2, 3120)
        assert (st.open_offers, st.open_invoices) == (2, 0)
        assert summary.summary_text(st) == (
            "Dein Oktober mit Locadas: 6× nachgefasst, 2 Kunden haben geantwortet, 2 Aufträge über 21.400 €, "
            "2 Rechnungen über 3.120 € bezahlt. Offen: 2 Angebote."
        )


def test_singular_and_open_invoices():
    st = summary.MonthStats(key="2026-03", label="März", followups=1, replied=1, won_count=1, won_sum=999.6, paid_count=1, paid_sum=50, open_offers=1, open_invoices=2)
    assert summary.summary_text(st) == (
        "Dein März mit Locadas: 1× nachgefasst, 1 Kunde hat geantwortet, 1 Auftrag über 1.000 €, 1 Rechnung über 50 € bezahlt. "
        "Offen: 1 Angebot und 2 Rechnungen."
    )


def test_send_once_on_the_first(fresh_db, graph):
    with new_session() as s:
        b = _october(s)
        assert summary.send_for_business(s, b, NOV_1_0930 - timedelta(hours=1)) == "not_due"  # 8:30 Uhr
        assert graph.calls == []
        assert summary.send_for_business(s, b, NOV_1_0930) == "sent"
        assert len(graph.calls) == 1
        tpl = graph.last["template"]
        assert tpl["name"] == "locadas_summary"
        params = [p["text"] for p in tpl["components"][0]["parameters"]]
        assert params[0] == "Thomas" and params[1].startswith("Dein Oktober mit Locadas: 6× nachgefasst")
        assert s.get(Business, b.id).summary_sent_for == "2026-10"
        # Zweiter Lauf (Neustart, zweiter Prozess, Cron doppelt): nichts
        assert summary.send_for_business(s, b, NOV_1_0930 + timedelta(minutes=5)) == "already"
        assert summary.send_monthly(s, NOV_1_0930 + timedelta(hours=2)) == {"already": 1}
        assert len(graph.calls) == 1


def test_zero_activity_month_is_skipped(fresh_db, graph):
    with new_session() as s:
        b, a = _biz(s)
        _doc(s, b, a, DocKind.OFFER, DocStatus.OPEN, 3000)
        _doc(s, b, a, DocKind.OFFER, DocStatus.WON, 5000, outcome=_oct(12))  # ohne Nachfassen gewonnen
        s.commit()
        assert summary.send_for_business(s, b, NOV_1_0930) == "skipped"
        assert graph.calls == []
        assert s.get(Business, b.id).summary_sent_for == "2026-10"
        assert summary.send_for_business(s, b, NOV_1_0930) == "already"


def test_failed_delivery_is_retried(fresh_db):
    """Kein WhatsApp, Postfach nicht erreichbar: nicht als verschickt merken."""
    with new_session() as s:
        b, a = _biz(s, whatsapp=False)
        _doc(s, b, a, DocKind.OFFER, DocStatus.OPEN, 3000, fus=[(_oct(5), "mail")])
        s.commit()
        assert summary.send_for_business(s, b, NOV_1_0930) == "failed"
        assert s.get(Business, b.id).summary_sent_for == ""


def test_due_key_and_december():
    assert summary.due_key(NOV_1_0930) == "2026-10"
    assert summary.due_key(datetime(2027, 1, 1, 9, 0, tzinfo=BERLIN)) == "2026-12"
    assert summary.due_key(datetime(2027, 1, 1, 8, 59, tzinfo=BERLIN)) is None
    start, end = summary.month_bounds(2026, 12)
    assert start.astimezone(BERLIN).day == 1 and end.astimezone(BERLIN) == datetime(2027, 1, 1, tzinfo=BERLIN)


def test_scheduler_registers_monthly_job(monkeypatch):
    from locadas.engine import scheduler

    class Sched:
        jobs = []

        def __init__(self, **kw):
            pass

        def add_job(self, fn, trigger, **kw):
            Sched.jobs.append((fn, trigger, kw))

        def start(self):
            pass

    import apscheduler.schedulers.background as bg

    monkeypatch.setattr(bg, "BackgroundScheduler", Sched)
    monkeypatch.setattr(scheduler.settings, "scheduler_enabled", True)
    scheduler.start_scheduler()
    job = next(j for j in Sched.jobs if j[2].get("id") == "summary")
    assert job[0] is summary.run_monthly and job[1] == "cron"
    assert job[2]["day"] == 1 and job[2]["hour"] == 9 and job[2]["timezone"] == "Europe/Berlin"


def test_dashboard_card_current_month(fresh_db):
    from locadas.main import app

    now = utcnow()
    with new_session() as s:
        b, a = _biz(s, whatsapp=False)
        _doc(s, b, a, DocKind.OFFER, DocStatus.WON, 4200, sent=now - timedelta(seconds=40),
             fus=[(now - timedelta(seconds=30), "mail")], reply=now - timedelta(seconds=20), outcome=now - timedelta(seconds=10))
        _doc(s, b, a, DocKind.OFFER, DocStatus.OPEN, 1000, sent=now - timedelta(seconds=40))
        s.commit()
        bid = b.id
    c = TestClient(app)
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    page = c.get("/dashboard").text
    card = page[page.index('id="monat"'):][:2500]
    assert "Dein Monat" in page
    assert "1×" in card and "1 Mail, 0 WhatsApp" in card
    assert "Kunde hat geantwortet" in card and "Auftrag über 4.200 €" in card
    assert "1 Angebot und 0 Rechnungen" in card


def test_dashboard_card_empty_month(fresh_db):
    from locadas.main import app

    with new_session() as s:
        b, a = _biz(s, whatsapp=False)
        s.commit()
        bid = b.id
    c = TestClient(app)
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    page = c.get("/dashboard").text
    assert "Dein Monat" in page and "noch nicht nachgefasst" in page
