"""Verbindungs-Wächter: Passwort geändert, Server weg, Pause, Neu verbinden, Entwarnung, Neu-Verteilen, Erinnerungen.

Ohne Netzwerk: Fake-IMAP/SMTP auf localhost, WhatsApp Graph API gemockt."""
from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from locadas.config import settings
from locadas.crypto import decrypt, encrypt
from locadas.db import AccountStatus, Business, DocStatus, Document, Event, FollowUp, FollowUpStatus, MailAccount, new_session
from locadas.engine import accounts as acc
from locadas.engine import followups as fu
from locadas.engine.scanner import scan_account
from locadas.mail.imap import MailConnectError
from locadas.notify import tokens
from tests.conftest import PASSWORD
from tests.test_e2e import WEEKDAY_10, _seed_sent, _setup
from tests.test_whatsapp import NUM, graph  # noqa: F401  (Fixture)

T0 = WEEKDAY_10


def _biz(s, imap, smtp):
    _seed_sent(imap)
    b, account = _setup(s, imap, smtp)
    scan_account(s, account, now=T0 - timedelta(days=1))
    b.whatsapp_number = NUM
    b.whatsapp_opt_in = True
    b.whatsapp_last_inbound_at = None  # außerhalb des 24h-Fensters: Vorlagen
    s.commit()
    return b, account


def _break_password(s, account):
    account.secret_enc = encrypt("altes-passwort")
    s.commit()


def _connection_calls(graph):
    return [c for c in graph.calls if c["type"] == "template" and c["template"]["name"] == "locadas_connection"]


def _event_texts(graph):
    out = []
    for c in graph.calls:
        if c["type"] == "template":
            out += [p["text"] for comp in c["template"]["components"] if comp["type"] == "body" for p in comp["parameters"]]
        elif c["type"] == "interactive":
            out.append(c["interactive"]["body"]["text"])
        elif c["type"] == "text":
            out.append(c["text"]["body"])
    return out


def test_auth_error_sets_error_and_notifies_once(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        _break_password(s, account)
        scan_account(s, account, now=T0)
        assert account.status == AccountStatus.ERROR
        assert account.error_since == T0 and account.error_notified_at == T0 and account.consecutive_errors == 1
        calls = _connection_calls(graph)
        assert len(calls) == 1
        comps = calls[0]["template"]["components"]
        assert [p["text"] for p in comps[0]["parameters"]] == ["Thomas", account.email]
        url_token = comps[1]["parameters"][0]["text"]
        assert comps[1]["sub_type"] == "url" and tokens.account_id_from_reconnect(url_token) == account.id
        # Nächste Durchläufe: kein zweiter Bescheid (auch nicht, wenn der Scan stündlich neu probiert)
        for h in (0.2, 1.5, 5, 30):
            scan_account(s, account, now=T0 + timedelta(hours=h))
        assert len(_connection_calls(graph)) == 1
        assert s.scalar(select(func.count(Event.id)).where(Event.kind == "connection")) == 1


def test_connect_error_needs_three_cycles(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        account.imap_port = 1  # Server weg
        s.commit()
        for i in range(2):
            scan_account(s, account, now=T0 + timedelta(minutes=10 * i))
            assert account.status == AccountStatus.ACTIVE and account.consecutive_errors == i + 1
        # Mehrere Fehler im selben Durchlauf (Scan plus Versand) zählen einmal
        acc.record_failure(s, account, MailConnectError("Versand: Server weg"), T0 + timedelta(minutes=10, seconds=30))
        assert account.consecutive_errors == 2 and account.status == AccountStatus.ACTIVE
        assert _connection_calls(graph) == []
        scan_account(s, account, now=T0 + timedelta(minutes=20))
        assert account.status == AccountStatus.ERROR and account.consecutive_errors == 3
        assert len(_connection_calls(graph)) == 1


def test_one_success_resets_the_counter(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        port = account.imap_port
        account.imap_port = 1
        s.commit()
        scan_account(s, account, now=T0)
        scan_account(s, account, now=T0 + timedelta(minutes=10))
        account.imap_port = port
        scan_account(s, account, now=T0 + timedelta(minutes=20))
        assert account.consecutive_errors == 0 and account.status == AccountStatus.ACTIVE
        account.imap_port = 1
        scan_account(s, account, now=T0 + timedelta(minutes=30))
        assert account.status == AccountStatus.ACTIVE and graph.calls == []


def test_sending_pauses_while_broken(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
        f = offer.followups[0]
        f.due_at = T0 - timedelta(hours=3)
        f.announced_at, f.send_after = T0 - timedelta(hours=3), T0 - timedelta(hours=1)
        s.commit()
        _break_password(s, account)
        scan_account(s, account, now=T0)
        before = len(smtp.messages)
        stats = fu.process_due(s, now=T0)
        assert stats["sent"] == 0 and len(smtp.messages) == before
        assert f.status == FollowUpStatus.SCHEDULED
        # auch direkt (z. B. Freigabe-Klick) kein SMTP-Versuch
        assert fu.send_followup(s, f) is False and f.status == FollowUpStatus.SCHEDULED
        assert len(smtp.messages) == before


def test_smtp_auth_error_on_send_keeps_step_and_alerts(fresh_db, imap, smtp, graph, monkeypatch):
    from locadas.mail.imap import MailAuthError

    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
        f = offer.followups[0]

        class Broken:
            def send(self, msg):
                raise MailAuthError("535 Login abgelehnt")

        monkeypatch.setattr(fu.acc, "smtp_for", lambda a: Broken())
        assert fu.send_followup(s, f) is False
        assert f.status == FollowUpStatus.SCHEDULED and f.sent_at is None and offer.followups_sent == 0
        assert account.status == AccountStatus.ERROR and len(_connection_calls(graph)) == 1


def test_reconnect_keeps_documents_and_resets_error(fresh_db, imap, smtp, graph):
    from locadas.main import app

    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        _break_password(s, account)
        scan_account(s, account, now=T0)
        assert account.status == AccountStatus.ERROR
        aid, bid = account.id, b.id
        docs = s.scalar(select(func.count(Document.id)).where(Document.business_id == bid))
        token = tokens.reconnect_token(aid)
        email = account.email
    c = TestClient(app)
    page = c.get(f"/verbinden?reconnect={token}")
    assert page.status_code == 200 and f'value="{email}" readonly' in page.text and 'name="token"' in page.text
    # falsches Passwort: Fehler, nichts geändert
    r = c.post("/verbinden/neu", data={"token": token, "password": "falsch"})
    assert r.status_code == 200 and "note err" in r.text
    with new_session() as s:
        assert s.get(MailAccount, aid).status == AccountStatus.ERROR
    r = c.post("/verbinden/neu", data={"token": token, "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard?msg=neu_verbunden"
    with new_session() as s:
        a = s.get(MailAccount, aid)
        assert a.status == AccountStatus.ACTIVE and decrypt(a.secret_enc) == PASSWORD
        assert a.error_since is None and a.error_notified_at is None and a.consecutive_errors == 0 and a.last_error == ""
        assert s.scalar(select(func.count(MailAccount.id)).where(MailAccount.business_id == bid)) == 1
        assert s.scalar(select(func.count(Document.id)).where(Document.business_id == bid)) == docs
    assert any("Wieder verbunden, alles läuft." in t for t in _event_texts(graph))
    assert "Wieder verbunden, alles läuft." in c.get("/dashboard?msg=neu_verbunden").text
    # kaputter Link
    assert c.get("/verbinden?reconnect=quatsch").status_code == 400


def test_recovery_by_itself_sends_message_once(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        _break_password(s, account)
        scan_account(s, account, now=T0)
        account.secret_enc = encrypt(PASSWORD)  # z. B. Server-Störung vorbei bzw. Anbieter hat Zugang wieder frei
        s.commit()
        scan_account(s, account, now=T0 + timedelta(minutes=20))
        assert account.status == AccountStatus.ERROR  # gestört: erst nach einer Stunde neu probieren
        scan_account(s, account, now=T0 + timedelta(hours=2))
        assert account.status == AccountStatus.ACTIVE
        back = [t for t in _event_texts(graph) if "Wieder verbunden" in t]
        assert len(back) == 1
        scan_account(s, account, now=T0 + timedelta(hours=3))
        assert len([t for t in _event_texts(graph) if "Wieder verbunden" in t]) == 1


def test_resume_respaces_backlog(fresh_db, imap, smtp, graph, monkeypatch):
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
        inv = s.scalars(select(Document).where(Document.document_number == "R-2026-301")).one()
        # Während der Störung ist alles fällig geworden
        for d in (offer, inv):
            for f in d.followups:
                f.due_at = T0 - timedelta(days=10 - f.step)
                f.announced_at = T0 - timedelta(days=9)
        s.commit()
        monkeypatch.setattr(settings, "max_sends_per_day", 1)
        account.status = AccountStatus.ERROR
        moved = acc.resume_followups(s, account, T0)
        assert moved == 2
        first = sorted((d for d in (offer, inv)), key=lambda d: d.followups[0].due_at)
        a, b2 = first
        assert a.followups[0].due_at == T0  # Dienstag 10 Uhr, im Versandfenster
        assert b2.followups[0].due_at == T0 + timedelta(days=1)  # höchstens einer je Tag
        for d in (offer, inv):
            dues = [f.due_at for f in sorted(d.followups, key=lambda f: f.step)]
            assert all(f.announced_at is None for f in d.followups)  # neu ankündigen, nichts geht ungefragt raus
            assert all(later - earlier >= timedelta(days=4) for earlier, later in zip(dues, dues[1:]))
            assert d.next_followup_at == dues[0]


def test_reminder_every_three_days(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        _break_password(s, account)
        scan_account(s, account, now=T0)
        assert len(_connection_calls(graph)) == 1
        for h in (2, 24, 48, 71):
            scan_account(s, account, now=T0 + timedelta(hours=h))
        assert len(_connection_calls(graph)) == 1
        scan_account(s, account, now=T0 + timedelta(hours=72, minutes=5))
        assert len(_connection_calls(graph)) == 2
        assert account.error_notified_at == T0 + timedelta(hours=72, minutes=5)
        # Erinnerung auch dann, wenn der Scan in diesem Durchlauf gar nicht probiert (stündliche Pause)
        account.last_failure_at = T0 + timedelta(days=6, minutes=50)
        scan_account(s, account, now=T0 + timedelta(days=6, hours=1, minutes=10))
        assert len(_connection_calls(graph)) == 3
        assert account.status == AccountStatus.ERROR


def test_connection_mail_skips_broken_smtp(fresh_db, imap, smtp, monkeypatch):
    """Ohne WhatsApp: kein Versuch über das kaputte Postfach, Systemversand nur wenn eingerichtet."""
    sent = []
    from locadas.notify import core

    monkeypatch.setattr(core, "send_system_email", lambda to, name, subj, body: sent.append((to, subj, body)) or True)
    with new_session() as s:
        _seed_sent(imap)
        b, account = _setup(s, imap, smtp)
        scan_account(s, account, now=T0 - timedelta(days=1))
        before = len(smtp.messages)
        _break_password(s, account)
        scan_account(s, account, now=T0)
        assert len(smtp.messages) == before
        assert len(sent) == 1 and sent[0][0] == account.email
        assert "nicht mehr verbunden (Passwort geändert?)" in sent[0][2] and "/verbinden?reconnect=" in sent[0][2]
        assert account.error_notified_at == T0


def test_settings_and_dashboard_show_reconnect(fresh_db, imap, smtp, graph):
    from locadas.main import app
    from locadas.web import auth

    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        _break_password(s, account)
        scan_account(s, account, now=T0)
        bid = b.id
    c = TestClient(app)
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    for path in ("/dashboard", "/einstellungen"):
        page = c.get(path).text
        assert "ist nicht mehr verbunden" in page and "/verbinden?reconnect=" in page
