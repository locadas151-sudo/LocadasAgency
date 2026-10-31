"""Admin-Cockpit /admin, Kennzahlen, Funnel, Fehler-Feed, Claude-Zähler und Alarme an den Admin. Ohne Netzwerk."""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from locadas.config import settings
from locadas.db import (
    AccountStatus, Business, ClaudeUsage, DocKind, DocStatus, Document, FollowUp, FollowUpStatus, MailAccount, NotificationLog,
    OpsLog, new_session, utcnow,
)
from locadas.engine import trial
from locadas.notify import Notification, send
from locadas.ops import alerts, logstore, state, stats, usage
from locadas.web import admin_routes, auth
from tests.test_whatsapp import NUM, graph  # noqa: F401  (Fixture)

ADMIN = "julius@locadas.de"
ADMIN_WA = "491709999999"


def _client():
    from locadas.main import app

    return TestClient(app)


@pytest.fixture()
def admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", (ADMIN,))
    monkeypatch.setattr(settings, "admin_whatsapp", "")
    alerts._rate.clear()
    yield


def _biz(s, name="Müller Haustechnik", email="info@mueller.de", mode="connected", **kw) -> Business:
    b = Business(name=name, owner_name="Thomas Müller", email=email, mode=mode, **kw)
    s.add(b)
    s.flush()
    return b


def _account(s, b, status=AccountStatus.ACTIVE, email=None) -> MailAccount:
    a = MailAccount(business_id=b.id, provider="gmx", email=email or b.email, username=email or b.email, secret_enc="x",
                    imap_host="imap.example.org", smtp_host="smtp.example.org", status=status)
    s.add(a)
    s.flush()
    return a


def _login(c, business_id: int) -> None:
    c.cookies.set(auth.COOKIE, auth.session_token(business_id))


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


# --- Zugang ---------------------------------------------------------------------------------------------

def test_admin_is_404_for_everyone_else(fresh_db, admin, monkeypatch):
    with new_session() as s:
        other = _biz(s, email="info@fremd.de")
        s.commit()
        oid = other.id
    c = _client()
    for path in ("/admin", "/admin/betriebe", "/admin/fehler", f"/admin/betrieb/{oid}"):
        assert c.get(path).status_code == 404
    _login(c, oid)  # angemeldeter Betrieb, aber nicht in ADMIN_EMAILS
    assert c.get("/admin").status_code == 404
    assert c.post(f"/admin/betrieb/{oid}/pause", data={"csrf": "x"}).status_code == 404
    monkeypatch.setattr(settings, "admin_emails", ())
    assert c.get("/admin/login").status_code == 404  # ohne ADMIN_EMAILS gibt es kein Cockpit


def test_owner_with_admin_email_sees_all_pages(fresh_db, admin):
    with new_session() as s:
        me = _biz(s, name="Locadas", email=ADMIN)
        _account(s, me)
        t = trial.create(s, "Anna Test", "Test Fliesen", "491711111111")
        s.commit()
        mid, tid = me.id, t.id
    c = _client()
    _login(c, mid)
    r = c.get("/admin")
    assert r.status_code == 200 and "Cockpit" in r.text and "Funnel /testen" in r.text
    assert r.headers["x-robots-tag"].startswith("noindex") and "no-store" in r.headers["cache-control"]
    assert 'name="robots" content="noindex' in r.text
    r = c.get("/admin/betriebe?f=trial")
    assert r.status_code == 200 and "Test Fliesen" in r.text and "https://wa.me/491711111111" in r.text and "Locadas</b>" not in r.text
    assert "Test Fliesen" in c.get("/admin/betriebe?q=fliesen").text
    assert "Test Fliesen" not in c.get("/admin/betriebe?q=gibtsnicht").text
    r = c.get(f"/admin/betrieb/{tid}")
    assert r.status_code == 200 and "Onboarding-Call erledigt" in r.text and "Login-Link schicken" in r.text
    assert c.get("/admin/fehler").status_code == 200


def test_magic_link_login(fresh_db, admin, monkeypatch):
    sent = []
    monkeypatch.setattr("locadas.notify.core.send_system_email", lambda to, name, subject, body: sent.append((to, body)) or True)
    c = _client()
    assert c.get("/admin/login").status_code == 200
    r = c.post("/admin/login", data={"email": "fremd@example.org"}, follow_redirects=False)
    assert r.status_code == 303 and not sent  # gleiche Antwort, aber kein Link
    c.post("/admin/login", data={"email": ADMIN.upper()}, follow_redirects=False)
    assert len(sent) == 1 and sent[0][0] == ADMIN
    link = re.search(r"http://testserver(/admin/login/\S+)", sent[0][1]).group(1)
    r = c.get(link, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert c.get("/admin").status_code == 200
    assert c.get("/admin/login/kaputt").status_code == 404
    with new_session() as s:
        log = s.scalars(select(NotificationLog).where(NotificationLog.business_id.is_(None))).all()
        assert [n.kind for n in log] == ["admin:login"] and log[0].channel == "email"


def test_actions_need_csrf_and_work(fresh_db, admin, monkeypatch, graph):
    with new_session() as s:
        me = _biz(s, name="Locadas", email=ADMIN)
        b = _biz(s, name="Kunde GmbH", email="info@kunde.de", whatsapp_number=NUM, whatsapp_opt_in=True)
        acc = _account(s, b)
        s.commit()
        mid, bid, aid = me.id, b.id, acc.id
    c = _client()
    _login(c, mid)
    token = _csrf(c.get(f"/admin/betrieb/{bid}").text)
    assert c.post(f"/admin/betrieb/{bid}/pause", data={"csrf": "falsch"}).status_code == 403
    assert c.post(f"/admin/betrieb/{bid}/pause", data={"csrf": token}, headers={"origin": "https://evil.example"}).status_code == 403
    r = c.post(f"/admin/betrieb/{bid}/pause", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("msg=pausiert")
    with new_session() as s:
        assert s.get(Business, bid).paused is True
    c.post(f"/admin/betrieb/{bid}/pause", data={"csrf": token})
    with new_session() as s:
        assert s.get(Business, bid).paused is False
    # Notiz und Onboarding-Call
    c.post(f"/admin/betrieb/{bid}/notiz", data={"csrf": token, "admin_note": "Call 10 Uhr, nutzt Lexoffice", "onboarded": "1"})
    with new_session() as s:
        b = s.get(Business, bid)
        assert b.admin_note == "Call 10 Uhr, nutzt Lexoffice" and b.onboarded_at is not None
    c.post(f"/admin/betrieb/{bid}/notiz", data={"csrf": token, "admin_note": ""})
    with new_session() as s:
        assert s.get(Business, bid).onboarded_at is None
    # Scan jetzt: im Hintergrund über die Sperre je Postfach
    from locadas.engine import scheduler

    scanned = []
    monkeypatch.setattr(scheduler, "scan_one", lambda i, started=None, force=False: scanned.append((i, force)) or {})
    r = c.post(f"/admin/betrieb/{bid}/scan", data={"csrf": token}, follow_redirects=False)
    assert r.headers["location"].endswith("msg=scan")
    import time

    for _ in range(50):
        if scanned:
            break
        time.sleep(0.02)
    assert scanned == [(aid, True)]
    # Login-Link per WhatsApp (Vorlage, kein Fenster offen)
    r = c.post(f"/admin/betrieb/{bid}/login-link", data={"csrf": token}, follow_redirects=False)
    assert r.headers["location"].endswith("msg=link_ok")
    assert graph.last["type"] == "template" and "/login/" in str(graph.last)
    r = c.get(f"/admin/betrieb/{bid}")
    assert "Login-Link" in r.text and "template" in r.text  # Benachrichtigungs-Log


# --- Kennzahlen und Funnel -----------------------------------------------------------------------------

def _doc(s, b, status=DocStatus.OPEN, reply=False) -> Document:
    d = Document(business_id=b.id, kind=DocKind.OFFER, status=status, customer_email="k@x.de", subject="Angebot",
                 last_customer_reply_at=utcnow() if reply else None)
    s.add(d)
    s.flush()
    return d


def test_overview_and_funnel(fresh_db, admin):
    now = utcnow()
    with new_session() as s:
        t1 = trial.create(s, "A", "Test Eins", "491711111111")
        t2 = trial.create(s, "B", "Test Zwei", "491712222222")
        trial.create(s, "C", "Test Drei", "491713333333")
        d = _doc(s, t1, reply=True)
        _doc(s, t2)
        t1.trial_aha_at = t2.trial_aha_at = now
        f = FollowUp(document_id=d.id, step=1, due_at=now, status=FollowUpStatus.SENT, sent_at=now, tap_state="sent")
        s.add(f)
        t1.upgrade_prompt_at = now
        t1.mode = "connected"
        _account(s, t1, email="info@eins.de")
        broken = _biz(s, name="Kaputt", email="info@kaputt.de")
        _account(s, broken, status=AccountStatus.ERROR)
        _doc(s, broken, status=DocStatus.WON)
        s.add(NotificationLog(business_id=broken.id, kind="info", ok=False, error="x"))
        s.add(NotificationLog(business_id=t2.id, kind="trial", channel="whatsapp", wa_mode="template", ok=True))
        s.add(NotificationLog(business_id=t2.id, kind="trial", channel="whatsapp", wa_mode="session", ok=True))
        s.commit()
        k = stats.overview(s, now + timedelta(seconds=1))
        assert (k["businesses"], k["trial"], k["connected"]) == (4, 2, 2)
        assert k["connected_24h"] == 2 and k["signups_24h"] == 4
        assert k["docs"] == 3 and k["fu_today"] == 1 and k["taps_7d"] == 1
        assert k["replies"] == 1 and k["wins"] == 1 and k["accounts_error"] == 1 and k["notify_failed_24h"] == 1
        assert k["wa_today"]["template"] == 1 and k["wa_today"]["session"] == 1 and k["wa_today"]["cost_eur"] == 0.05
        fn = {x["key"]: x["n"] for x in stats.funnel(s)}
        assert fn == {"signup": 3, "doc": 2, "aha": 2, "tap": 1, "upgrade": 1, "connected": 1}
        rows = stats.businesses(s, flt="error")
        assert [r.b.name for r in rows] == ["Kaputt"]
        assert [r.b.name for r in stats.businesses(s, flt="trial")] == ["Test Drei", "Test Zwei"]


def test_claude_usage_counted_and_costed(fresh_db):
    resp = SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=200))
    usage.record("extract", "claude-haiku-5-5", resp)
    usage.record("extract", "claude-haiku-5-5", resp)
    usage.record("text", "claude-sonnet-5-5", SimpleNamespace(usage=SimpleNamespace(input_tokens=2000, output_tokens=500)))
    usage.record("match", "claude-haiku-5-5", error=True)
    with new_session() as s:
        assert usage.flush(s) == 3
        assert usage.flush(s) == 0
        day = stats.local_day(utcnow())
        u = usage.summary(s, day)
        assert u["calls"] == 4 and u["errors"] == 1 and u["by_purpose"]["extract"]["calls"] == 2
        # Haiku 2x(1000*1 + 200*5)/1e6 = 0.004, Sonnet (2000*3 + 500*15)/1e6 = 0.0135
        assert abs(u["cost_usd"] - 0.0175) < 1e-9
        usage.record("extract", "claude-haiku-5-5", resp)
        usage.flush(s)
        assert s.scalar(select(ClaudeUsage).where(ClaudeUsage.purpose == "extract")).calls == 3


def test_claude_call_sites_are_instrumented(fresh_db, monkeypatch):
    """Die echte Erkennung (fields) meldet den Aufruf, auch wenn Claude scheitert."""
    import anthropic

    from locadas.extract import fields

    class Fake:
        def __init__(self, **kw):
            self.messages = self

        def create(self, **kw):
            assert "temperature" not in kw
            return SimpleNamespace(content=[SimpleNamespace(text="{}")], usage=SimpleNamespace(input_tokens=10, output_tokens=5))

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    usage._pending.clear()
    fields._claude_extract(None, "Angebot", "k@x.de", utcnow(), "Angebot Nr. 1")
    assert sum(v[0] for v in usage._pending.values()) == 1
    usage._pending.clear()


# --- Fehler-Feed ----------------------------------------------------------------------------------------

def test_log_handler_stores_warnings_with_business(fresh_db, monkeypatch):
    h = logstore.install()
    logstore.install()
    assert logging.getLogger().handlers.count(h) == 1
    logstore._buffer.clear()
    lg = logging.getLogger("locadas.engine.test")
    lg.info("nur info")
    lg.warning("Postfach kaputt", extra={"business_id": 7})
    tok = logstore.business_ctx.set(9)
    try:
        try:
            raise ValueError("böse")
        except ValueError:
            lg.exception("Scan-Fehler")
    finally:
        logstore.business_ctx.reset(tok)
    logging.getLogger("locadas.ops.x").error("nicht speichern (Schleife)")
    with new_session() as s:
        assert logstore.flush(s) == 2
        rows = logstore.recent(s)
        assert [(r.level, r.business_id) for r in rows] == [("ERROR", 9), ("WARNING", 7)]
        assert "ValueError: böse" in rows[0].message
        monkeypatch.setattr(logstore, "KEEP", 3)
        for i in range(5):
            lg.warning("w%s", i)
        logstore.flush(s)
        assert s.scalar(select(OpsLog.id).order_by(OpsLog.id)) is not None
        assert len(s.scalars(select(OpsLog)).all()) == 3


# --- Alarme ---------------------------------------------------------------------------------------------

@pytest.fixture()
def admin_wa(monkeypatch, graph):
    monkeypatch.setattr(settings, "admin_emails", (ADMIN,))
    monkeypatch.setattr(settings, "admin_whatsapp", ADMIN_WA)
    alerts._rate.clear()
    return graph


def _admin_texts(graph) -> list[str]:
    out = []
    for c in graph.calls:
        if c.get("to") != ADMIN_WA:
            continue
        if c["type"] == "template":
            out += [p["text"] for comp in c["template"]["components"] if comp["type"] == "body" for p in comp["parameters"]]
        elif c["type"] == "text":
            out.append(c["text"]["body"])
    return out


def test_watch_alerts_signups_connections_and_errors(fresh_db, admin_wa):
    graph = admin_wa
    with new_session() as s:
        _biz(s, name="Alt GmbH", email="alt@x.de")
        s.commit()
        assert alerts.watch(s) == {"first": 1}  # erster Lauf: nur Stand merken
        assert graph.calls == []
        t = trial.create(s, "Anna Test", "Test Fliesen", "491711111111")
        s.commit()
        r = alerts.watch(s, utcnow() + timedelta(seconds=1))
        assert r["signups"] == 1 and r["sent"] == 1
        texts = _admin_texts(graph)
        assert any("Neu (Test): Test Fliesen, Anna Test, wa.me/491711111111" in x for x in texts)
        assert graph.last["type"] == "template"  # kein 24h-Fenster
        # Admin schreibt dem Sekretär: ab jetzt kostenlose Sitzungsnachrichten
        alerts.note_admin_inbound(s, ADMIN_WA)
        a = _account(s, t, email="info@fliesen.de")
        a.created_at = utcnow() + timedelta(seconds=2)
        s.commit()
        r = alerts.watch(s, utcnow() + timedelta(seconds=3))
        assert r["connections"] == 1 and graph.last["type"] == "text" and "Verbunden: Test Fliesen" in graph.last["text"]["body"]
        a.status, a.last_error, a.error_since = AccountStatus.ERROR, "Login abgelehnt", utcnow()
        s.commit()
        r = alerts.watch(s, utcnow() + timedelta(seconds=4))
        assert r["errors"] == 1 and "Postfach gestört: Test Fliesen" in graph.last["text"]["body"]
        n = len(graph.calls)
        assert alerts.watch(s, utcnow() + timedelta(seconds=5))["errors"] == 0  # nur einmal je Störung
        assert len(graph.calls) == n
        modes = [x.wa_mode for x in s.scalars(select(NotificationLog).where(NotificationLog.business_id.is_(None))).all()]
        assert modes == ["template", "session", "session"]


def test_job_error_rate_limited(fresh_db, admin_wa):
    graph = admin_wa
    assert alerts.job_error("scan", RuntimeError("kaputt"), "Scan") is True
    assert alerts.job_error("scan", RuntimeError("noch mal"), "Scan") is False
    assert alerts.job_error("followups", RuntimeError("anders"), "Nachfassen") is True
    assert len(_admin_texts(graph)) == 4  # je Vorlage zwei Parameter
    assert alerts.rate_ok("x", 0.0) and not alerts.rate_ok("x", 60.0) and alerts.rate_ok("x", 16 * 60.0)


def test_scheduler_listener_and_email_fallback(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", (ADMIN,))
    monkeypatch.setattr(settings, "admin_whatsapp", ADMIN_WA)  # WhatsApp nicht eingerichtet: Mail
    alerts._rate.clear()
    mails = []
    monkeypatch.setattr("locadas.notify.core.send_system_email", lambda to, name, subject, body: mails.append((to, subject, body)) or True)
    alerts.scheduler_listener(SimpleNamespace(job_id="backup", exception=OSError("Platte voll")))
    assert mails and mails[0][0] == ADMIN and "Platte voll" in mails[0][2]


def test_digest(fresh_db, admin_wa):
    graph = admin_wa
    with new_session() as s:
        t = trial.create(s, "Anna", "Test Fliesen", "491711111111")
        _doc(s, t)
        state.put(s, "last_backup", {"ok": True, "file": "locadas-20261101.sqlite.gz", "at": "2026-11-01T02:15:00"})
        s.commit()
        assert alerts.digest(s) is True
    text = " ".join(_admin_texts(graph))
    assert "Tagesbericht" in text and "Anmeldungen: 1 (davon Test 1)" in text and "Dokumente erkannt: 1" in text
    assert "locadas-20261101.sqlite.gz" in text


def test_scheduler_registers_ops_jobs(monkeypatch):
    from locadas.engine import scheduler

    class Sched:
        jobs, listeners = [], []

        def __init__(self, **kw):
            pass

        def add_job(self, fn, trigger, **kw):
            Sched.jobs.append((fn, trigger, kw))

        def add_listener(self, fn, mask):
            Sched.listeners.append(fn)

        def start(self):
            pass

    import apscheduler.schedulers.background as bg

    monkeypatch.setattr(bg, "BackgroundScheduler", Sched)
    monkeypatch.setattr(scheduler.settings, "scheduler_enabled", True)
    scheduler.start_scheduler()
    jobs = {kw["id"]: (trigger, kw) for _, trigger, kw in Sched.jobs}
    assert jobs["backup"][0] == "cron" and (jobs["backup"][1]["hour"], jobs["backup"][1]["minute"]) == (3, 15)
    assert jobs["ops_digest"][1]["hour"] == 19 and jobs["ops_digest"][1]["timezone"] == "Europe/Berlin"
    assert jobs["ops_watch"][0] == "interval"
    assert Sched.listeners == [alerts.scheduler_listener]


def test_notification_log_records_channel(fresh_db, graph):
    with new_session() as s:
        b = _biz(s, whatsapp_number=NUM, whatsapp_opt_in=True)
        b.whatsapp_last_inbound_at = utcnow()
        ok = send(s, Notification(business=b, kind="info", title="Hallo", text="Test"))
        b2 = _biz(s, name="Ohne", email="ohne@x.de")
        bad = send(s, Notification(business=b2, kind="info", title="Hallo", text="Test", email_subject="x", email_body="y"))
        s.commit()
        rows = s.scalars(select(NotificationLog).order_by(NotificationLog.id)).all()
        assert ok and not bad
        assert [(r.channel, r.wa_mode, r.ok) for r in rows] == [("whatsapp", "session", True), ("", "", False)]
