"""Microsoft Graph als Mail-Zugang: Verbinden, Scan mit Paging, Antworten, Versand, Token, Drosselung, Admin-Freigabe, MX.

Ohne Netzwerk: Fake-Graph-Server auf localhost (tests/fakegraph.py), Token-Endpunkt und DNS gemockt."""
from __future__ import annotations

import base64
import email
import email.policy
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from locadas import main as m
from locadas.crypto import decrypt, encrypt
from locadas.db import AccountStatus, Business, DocKind, DocStatus, Document, FollowUp, MailAccount, new_session
from locadas.engine import accounts as acc
from locadas.engine import followups as fu
from locadas.engine.scanner import scan_account
from locadas.mail import autoconfig, graph, oauth
from locadas.mail.connect import ConnectionConfig
from locadas.mail.imap import ImapClient, MailAuthError
from locadas.mail.smtp import build_message
from tests.conftest import USER
from tests.fakegraph import FakeGraphServer
from tests.fakemail import make_mail, make_pdf
from tests.test_e2e import _invoice_pdf, _offer_pdf
from tests.test_watchdog import _connection_calls
from tests.test_whatsapp import NUM, graph as wa_graph  # noqa: F401  (Fixture WhatsApp)


@pytest.fixture()
def msgraph(monkeypatch):
    srv = FakeGraphServer(USER).start()
    monkeypatch.setattr(graph, "GRAPH_BASE", srv.base)
    sleeps: list[float] = []
    monkeypatch.setattr(graph, "_sleep", sleeps.append)
    srv.sleeps = sleeps
    monkeypatch.setattr(oauth, "_post", srv.store.token_post)
    monkeypatch.setattr(oauth.settings, "microsoft_client_id", "cid")
    monkeypatch.setattr(oauth.settings, "microsoft_client_secret", "sec")
    monkeypatch.setattr(m, "_background_scan", lambda account_id: None)
    oauth._cache.clear()
    autoconfig._MS_CACHE.clear()
    yield srv
    oauth._cache.clear()
    srv.stop()


def _offer_nr(nr: str) -> bytes:
    return make_pdf(["Mueller Haustechnik GmbH", f"Angebot Nr. {nr}", "Herr Neu, Weg 1, 41061 Moenchengladbach", "Gesamtbetrag 1.190,00 EUR"])


def _seed(store):
    day = datetime.now(timezone.utc) - timedelta(days=1)
    store.add("sentitems", make_mail(USER, "familie.schmidt@gmx.de", "Angebot Gastherme", "Anbei unser Angebot.", ("Angebot_A-2026-118.pdf", _offer_pdf()), from_name="Müller Haustechnik", to_name="Familie Schmidt", date=day))
    store.add("sentitems", make_mail(USER, "kollege@mueller-shk.de", "Urlaubsplan", "Ohne PDF.", date=day + timedelta(minutes=1)))
    store.add("sentitems", make_mail(USER, "p.weber@web.de", "Ihre Rechnung R-2026-301", "Anbei die Rechnung.", ("Rechnung_R-2026-301.pdf", _invoice_pdf()), to_name="Peter Weber", date=day + timedelta(minutes=2)))
    store.add("sentitems", make_mail(USER, "kunde@example.org", "Lieferschein 55", "Lieferschein.", ("Lieferschein.pdf", make_pdf(["Lieferschein 55"])), date=day + timedelta(minutes=3)))


def _graph_account(s, store) -> tuple[Business, MailAccount]:
    b = Business(name="Müller Haustechnik GmbH", owner_name="Thomas Müller", email=USER, phone="0171 1")
    s.add(b)
    s.flush()
    cfg = ConnectionConfig("microsoft", "Microsoft / Outlook", USER, USER, "graph.microsoft.com", 443, True, "graph.microsoft.com", 443, True, "sentitems", "inbox")
    account = acc.create_account(s, b, cfg, "rt-0", auth_type="graph")
    s.commit()
    return b, account


def _start(client, **extra) -> str:
    params = {"business_name": "Koch Elektro", "owner_name": "Sabine Koch", "avv": "1", **extra}
    r = client.get("/oauth/microsoft/start", params=params, follow_redirects=False)
    assert r.status_code == 303
    url = urlparse(r.headers["location"])
    q = parse_qs(url.query)
    assert url.netloc == "login.microsoftonline.com" and url.path == "/common/oauth2/v2.0/authorize"
    scopes = q["scope"][0].split()
    for sc in ("offline_access", "openid", "email", "profile", "https://graph.microsoft.com/Mail.ReadWrite", "https://graph.microsoft.com/Mail.Send", "https://graph.microsoft.com/User.Read"):
        assert sc in scopes
    assert not any("IMAP" in sc or "SMTP" in sc for sc in scopes)
    return q["state"][0]


def _tokens(store, monkeypatch, rt: str = "rt-first"):
    monkeypatch.setattr(oauth, "exchange_code", lambda p, code: {"access_token": store.issue_token(), "refresh_token": rt, "expires_in": 3600})


# --- Verbinden und Neu verbinden -------------------------------------------------------------

def test_connect_via_callback_creates_graph_account(fresh_db, msgraph, monkeypatch):
    client = TestClient(m.app)
    page = client.get("/verbinden").text
    assert "Mit Microsoft anmelden" in page
    state = _start(client, email="info@koch-elektro.de")
    _tokens(msgraph.store, monkeypatch)
    r = client.get("/oauth/microsoft/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/start", r.text
    with new_session() as s:
        a = s.scalars(select(MailAccount)).one()
        b = s.scalars(select(Business)).one()
        assert a.auth_type == "graph" and a.provider == "microsoft" and a.email == USER  # aus /me (userPrincipalName)
        assert a.sent_folder == "sentitems" and a.inbox_folder == "inbox"
        assert decrypt(a.secret_enc) == "rt-first" and b.name == "Koch Elektro"
        assert isinstance(acc.imap_for(a), graph.GraphClient) and isinstance(acc.smtp_for(a), graph.GraphClient)
    assert ("GET", "/v1.0/me/mailFolders/sentitems") in msgraph.store.log


def test_reconnect_graph_account_in_place(fresh_db, msgraph, monkeypatch, wa_graph):
    from locadas.notify import tokens

    with new_session() as s:
        b, account = _graph_account(s, msgraph.store)
        account.status = AccountStatus.ERROR
        account.last_error = "Microsoft hat den Zugriff abgelehnt"
        s.commit()
        aid = account.id
    client = TestClient(m.app)
    page = client.get("/verbinden", params={"reconnect": tokens.reconnect_token(aid)})
    assert page.status_code == 200 and "/oauth/microsoft/start?avv=1&amp;email=info%40mueller-shk.de" in page.text
    r = client.get("/oauth/microsoft/start", params={"avv": "1", "email": USER}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["login_hint"] == [USER]
    _tokens(msgraph.store, monkeypatch, rt="rt-neu")
    r = client.get("/oauth/microsoft/callback", params={"code": "x", "state": q["state"][0]}, follow_redirects=False)
    assert r.status_code == 303 and "neu_verbunden" in r.headers["location"]
    with new_session() as s:
        a = s.scalars(select(MailAccount)).one()
        assert a.id == aid and a.status == AccountStatus.ACTIVE and a.auth_type == "graph" and decrypt(a.secret_enc) == "rt-neu"
        assert a.last_error == ""


def test_legacy_imap_oauth_account_switches_to_graph_and_keeps_position(fresh_db, msgraph, monkeypatch):
    with new_session() as s:
        b = Business(name="Alt GmbH", owner_name="A", email=USER)
        s.add(b)
        s.flush()
        cfg = ConnectionConfig("microsoft", "Microsoft / Outlook", USER, USER, "outlook.office365.com", 993, True, "smtp.office365.com", 587, False, "Sent Items")
        a = acc.create_account(s, b, cfg, "rt-legacy", auth_type="oauth")
        a.last_scan_at = datetime.now(timezone.utc) - timedelta(hours=2)
        s.commit()
        # Alte Konten: weiter IMAP mit den alten Scopes, neuer Refresh-Token wird gespeichert
        assert isinstance(acc.imap_for(a), ImapClient)
        assert "IMAP.AccessAsUser.All" in msgraph.store.token_calls[-1]["scope"]
        assert decrypt(a.secret_enc) == "rt-rotated-1"
    client = TestClient(m.app)
    state = _start(client)
    _tokens(msgraph.store, monkeypatch, rt="rt-graph")
    r = client.get("/oauth/microsoft/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as s:
        a = s.scalars(select(MailAccount)).one()
        assert a.auth_type == "graph" and a.sent_folder == "sentitems" and a.inbox_folder == "inbox"
        st = json.loads(a.graph_sync)
        assert st["sent"]["t"] and st["inbox"]["t"]  # macht dort weiter, wo IMAP aufgehört hat


# --- Scan ------------------------------------------------------------------------------------

def test_scan_finds_offer_in_sentitems_with_paging(fresh_db, msgraph):
    store = msgraph.store
    _seed(store)
    with new_session() as s:
        b, account = _graph_account(s, store)
        stats = scan_account(s, account)
        assert stats["new"] == 2, store.log
        assert account.status == AccountStatus.ACTIVE
        docs = s.scalars(select(Document).order_by(Document.id)).all()
        offer = next(d for d in docs if d.kind == DocKind.OFFER)
        inv = next(d for d in docs if d.kind == DocKind.INVOICE)
        assert offer.document_number == "A-2026-118" and offer.amount_gross == 3724.70 and offer.customer_email == "familie.schmidt@gmx.de"
        assert inv.document_number == "R-2026-301" and inv.amount_gross == 224.91
        lists = [p for meth, p in store.log if p.endswith("/sentitems/messages")]
        assert len(lists) == 2  # 4 Mails, 2 je Seite: über @odata.nextLink
        assert store.values_fetched() == 3  # Mail ohne Anhang beim Erst-Scan nicht geladen
        # Zweiter Durchlauf: nichts doppelt, keine Mail erneut geladen
        assert scan_account(s, account)["new"] == 0
        assert store.values_fetched() == 3
        assert s.scalar(select(func.count(Document.id))) == 2
        # Neue Mail mit Angebot später: wird gefunden
        store.add("sentitems", make_mail(USER, "neu@kunde.de", "Angebot Bad", "Anbei.", ("Angebot_A-2026-200.pdf", _offer_nr("A-2026-200"))), datetime.now(timezone.utc))
        assert scan_account(s, account)["new"] == 1


def test_reply_in_inbox_stops_followups(fresh_db, msgraph):
    store = msgraph.store
    _seed(store)
    with new_session() as s:
        b, account = _graph_account(s, store)
        scan_account(s, account)  # Erstlauf: Posteingang ab jetzt
        offer = s.scalars(select(Document).where(Document.kind == DocKind.OFFER)).one()
        store.add("inbox", make_mail("old@kunde.de", USER, "Alt", "Vor dem Verbinden.", in_reply_to=offer.message_id), datetime.now(timezone.utc) - timedelta(days=2))
        store.add("inbox", make_mail("familie.schmidt@gmx.de", USER, "AW: Angebot Gastherme", "Danke, wir melden uns.", in_reply_to=offer.message_id), datetime.now(timezone.utc))
        loaded = store.values_fetched()
        assert scan_account(s, account)["replies"] == 1
        s.refresh(offer)
        assert offer.status == DocStatus.REPLIED
        # Header kamen aus internetMessageHeaders, die Mail selbst wurde dafür nicht geladen
        assert store.values_fetched() == loaded
        # Nochmal: dieselbe Antwort zählt nicht doppelt (Überlappungsfenster mit gemerkten IDs)
        assert scan_account(s, account)["replies"] == 0


def test_followup_send_posts_mime_with_in_reply_to(fresh_db, msgraph):
    store = msgraph.store
    _seed(store)
    with new_session() as s:
        b, account = _graph_account(s, store)
        scan_account(s, account)
        offer = s.scalars(select(Document).where(Document.kind == DocKind.OFFER)).one()
        first = offer.followups[0]
        assert fu.send_followup(s, first)
        s.commit()
        assert len(store.sent_mime) == 1 and store.send_content_types == ["text/plain"]
        sent = email.message_from_bytes(store.sent_mime[0], policy=email.policy.default)
        assert sent["In-Reply-To"] == offer.message_id and offer.message_id in sent["References"]
        assert sent["To"].addresses[0].addr_spec == "familie.schmidt@gmx.de" and sent[graph.OWN_HEADER] == "1"
        assert first.message_id == sent["Message-ID"]
        # Kein IMAP-APPEND: Graph legt die Mail selbst in Gesendet ab. Der nächste Scan erkennt sie als eigene.
        assert scan_account(s, account)["new"] == 0
        assert s.scalar(select(func.count(Document.id))) == 2
        s.refresh(offer)
        assert offer.status == DocStatus.OPEN


def test_large_mail_goes_via_draft_and_upload_session(fresh_db, msgraph, monkeypatch):
    monkeypatch.setattr(graph, "MIME_LIMIT", 100)
    monkeypatch.setattr(graph, "SMALL_ATTACHMENT", 50)
    monkeypatch.setattr(graph, "UPLOAD_CHUNK", 1000)
    store = msgraph.store
    tok = store.issue_token()
    client = graph.GraphClient(lambda force: tok)
    msg = build_message("Müller", USER, "Familie Schmidt", "familie.schmidt@gmx.de", "Angebot Gastherme", "Hallo", in_reply_to="<orig@test.local>")
    pdf = b"%PDF-1.4 " + b"x" * 2500
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="Angebot.pdf")
    client.send(msg)
    draft = store.drafts["draft1"]
    bare = email.message_from_bytes(draft["raw"], policy=email.policy.default)
    assert bare["In-Reply-To"] == "<orig@test.local>" and not list(bare.iter_attachments())
    assert draft["sent"] and b"".join(c for _, c in draft["chunks"]) == pdf and draft["upload_name"] == "Angebot.pdf"
    assert [r for r, _ in draft["chunks"]][0] == f"bytes 0-999/{len(pdf)}"
    assert store.auth_headers and all(h is None for h in store.auth_headers)  # vorab signierte URL ohne Token


# --- Token und Fehler ------------------------------------------------------------------------

def test_401_refreshes_token_once_then_succeeds(fresh_db, msgraph):
    store = msgraph.store
    _seed(store)
    with new_session() as s:
        b, account = _graph_account(s, store)
        scan_account(s, account)
        calls = len(store.token_calls)
        store.valid_tokens.clear()  # Token im Cache ist bei Microsoft abgelaufen
        store.add("sentitems", make_mail(USER, "neu@kunde.de", "Angebot Bad", "Anbei.", ("Angebot_A-2026-200.pdf", _offer_nr("A-2026-200"))), datetime.now(timezone.utc))
        assert scan_account(s, account)["new"] == 1
        assert len(store.token_calls) == calls + 1 and account.status == AccountStatus.ACTIVE
        assert "Mail.ReadWrite" in store.token_calls[-1]["scope"]
        assert decrypt(account.secret_enc).startswith("rt-rotated-")  # rotierter Refresh-Token gespeichert


def test_invalid_grant_is_auth_error_and_watchdog_notifies(fresh_db, msgraph, wa_graph):
    store = msgraph.store
    _seed(store)
    with new_session() as s:
        b, account = _graph_account(s, store)
        scan_account(s, account)
        b.whatsapp_number = NUM
        b.whatsapp_opt_in = True
        b.whatsapp_last_inbound_at = None
        s.commit()
        store.token_error = "invalid_grant"
        oauth._cache.clear()
        with pytest.raises(MailAuthError):
            acc.graph_for(account).test()
        scan_account(s, account)
        assert account.status == AccountStatus.ERROR and "invalid_grant" in account.last_error
        assert len(_connection_calls(wa_graph)) == 1
        # Versand wartet, solange das Postfach gestört ist
        offer = s.scalars(select(Document).where(Document.kind == DocKind.OFFER)).one()
        assert not fu.send_followup(s, offer.followups[0]) and store.sent_mime == []


def test_401_after_refresh_is_auth_error(fresh_db, msgraph, monkeypatch):
    store = msgraph.store
    client = graph.GraphClient(lambda force: "nie-gueltig")
    with pytest.raises(MailAuthError):
        client.test()
    # Postfach ohne Exchange Online: verständliche Meldung, gilt als Login-Problem
    store.mailbox_error = "MailboxNotEnabledForRESTAPI"
    tok = store.issue_token()
    with pytest.raises(MailAuthError, match="Exchange"):
        graph.GraphClient(lambda force: tok).test()


def test_429_respects_retry_after(fresh_db, msgraph):
    store = msgraph.store
    _seed(store)
    store.fail_once("GET", "/me/mailFolders/sentitems/messages", 429, {"Retry-After": "7"})
    store.fail_once("GET", "/me/messages/", 503, {})
    with new_session() as s:
        b, account = _graph_account(s, store)
        assert scan_account(s, account)["new"] == 2
        assert account.status == AccountStatus.ACTIVE
    assert msgraph.sleeps == [7.0, 2.0]


def test_throttling_too_long_or_network_down_is_connect_error(fresh_db, msgraph):
    from locadas.mail.imap import MailConnectError

    store = msgraph.store
    tok = store.issue_token()
    store.fail_once("GET", "/me/mailFolders/sentitems", 429, {"Retry-After": "3600"})
    with pytest.raises(MailConnectError):
        graph.GraphClient(lambda force: tok).test()
    with pytest.raises(MailConnectError):
        graph.GraphClient(lambda force: tok, base="http://127.0.0.1:9/v1.0").test()
    with pytest.raises(MailConnectError):
        graph.GraphClient(lambda force: tok, base="http://graph.example.com/v1.0")  # nur HTTPS
    with new_session() as s:
        b, account = _graph_account(s, store)
        store.fail.extend([("GET", "/me/mailFolders", 503, {})] * 4)
        scan_account(s, account)
        assert account.status == AccountStatus.ACTIVE and account.consecutive_errors == 1  # erst nach 3 Durchläufen gestört


# --- Admin-Freigabe ----------------------------------------------------------------------------

def test_admin_consent_error_page(fresh_db, msgraph, monkeypatch):
    client = TestClient(m.app)
    state = _start(client, email="info@mueller-shk.de")
    r = client.get("/oauth/microsoft/callback", params={
        "state": state, "error": "access_denied",
        "error_description": "AADSTS90094: The grant requires admin permission.",
    })
    assert r.status_code == 200
    assert "deine IT fragen" in r.text
    assert "https://login.microsoftonline.com/mueller-shk.de/adminconsent?client_id=cid&amp;redirect_uri=http%3A%2F%2Ftestserver%2Foauth%2Fmicrosoft%2Fcallback" in r.text
    assert "calendly.com/julius-locadas/julius-komorek" in r.text
    # Gleiche Lage beim Token-Austausch (AADSTS65001), ohne bekannte Domain: organizations
    def refuse(p, code):
        raise oauth.OAuthError('Token-Austausch abgelehnt: {"error":"invalid_grant","error_description":"AADSTS65001: The user or administrator has not consented"}')
    monkeypatch.setattr(oauth, "exchange_code", refuse)
    r = client.get("/oauth/microsoft/callback", params={"state": _start(client), "code": "x"})
    assert r.status_code == 200 and "/organizations/adminconsent?client_id=cid" in r.text
    # Rückkehr vom Admin: Freigabe erteilt bzw. abgelehnt
    r = client.get("/oauth/microsoft/callback", params={"admin_consent": "True", "tenant": "abc"})
    assert r.status_code == 200 and "Freigabe" in r.text and "erteilt" in r.text
    r = client.get("/oauth/microsoft/callback", params={"error": "access_denied", "error_description": "AADSTS65004"})
    assert r.status_code == 200 and "abgebrochen" in r.text
    # Normales Ablehnen durch den Nutzer bleibt das Formular mit Fehler
    r = client.get("/oauth/microsoft/callback", params={"state": _start(client), "error": "access_denied", "error_description": "AADSTS65004: User declined"})
    assert r.status_code == 200 and "abgelehnt" in r.text and "adminconsent" not in r.text


# --- Microsoft erkennen (MX) -------------------------------------------------------------------

def test_mx_detection_for_custom_domain(fresh_db, msgraph, monkeypatch):
    asked: list[str] = []

    def mx(domain):
        asked.append(domain)
        return {"mueller-shk.de": ["mueller-shk-de.mail.protection.outlook.com"], "x-bau.de": ["mx00.ionos.de"]}.get(domain, [])

    monkeypatch.setattr(autoconfig, "mx_lookup", mx)
    assert autoconfig.is_microsoft_domain("info@mueller-shk.de")
    assert autoconfig.is_microsoft_domain("info@mueller-shk.de")  # aus dem Cache
    assert asked == ["mueller-shk.de"]
    assert not autoconfig.is_microsoft_domain("info@x-bau.de")
    assert autoconfig.is_microsoft_domain("max@hotmail.de") and not autoconfig.is_microsoft_domain("max@gmx.de")
    assert asked == ["mueller-shk.de", "x-bau.de"]  # bekannte Anbieter ohne DNS
    client = TestClient(m.app)
    assert client.get("/verbinden/anbieter", params={"email": "info@mueller-shk.de"}).json() == {"microsoft": True}
    assert client.get("/verbinden/anbieter", params={"email": "info@x-bau.de"}).json() == {"microsoft": False}
    page = client.get("/verbinden", params={"email": "info@mueller-shk.de"}).text
    assert 'id="ms-box">' in page and 'id="pw-field" hidden' in page
    page = client.get("/verbinden", params={"email": "info@x-bau.de"}).text
    assert 'id="ms-box" hidden' in page and 'id="pw-field">' in page
    # Passwort-Versuch mit Microsoft-Domain: Hinweis plus Microsoft-Knopf
    monkeypatch.setattr(autoconfig, "resolve", lambda email: autoconfig.ServerConfig("outlook.office365.com", 993, True, "smtp.office365.com", 587, False, "", "mx", autoconfig.OUTLOOK))
    r = client.post("/verbinden", data={"business_name": "Müller", "email": "info@mueller-shk.de", "password": "x", "consent": "1", "avv": "1"})
    assert "kein Passwort-Login" in r.text and 'id="ms-box">' in r.text


def test_initial_scan_spans_several_runs(fresh_db, msgraph, monkeypatch):
    from locadas.engine import scanner

    monkeypatch.setattr(scanner, "MAX_PER_RUN", 1)
    store = msgraph.store
    _seed(store)
    with new_session() as s:
        b, account = _graph_account(s, store)
        assert scan_account(s, account)["new"] == 1
        assert json.loads(account.graph_sync)["sent"]["init"] is True
        assert scan_account(s, account)["new"] == 1
        scan_account(s, account)  # Lieferschein: kein Dokument
        scan_account(s, account)
        assert json.loads(account.graph_sync)["sent"]["init"] is False
        assert store.values_fetched() == 3  # die Mail ohne Anhang wurde auch in späteren Erst-Scan-Runden nicht geladen
        assert s.scalar(select(func.count(Document.id))) == 2


def test_graph_watermark_handles_unseen_overlap(fresh_db, msgraph):
    """Mail, die Microsoft erst nach dem letzten Durchlauf mit älterem Zeitstempel einsortiert, wird trotzdem gelesen."""
    store = msgraph.store
    _seed(store)
    with new_session() as s:
        b, account = _graph_account(s, store)
        scan_account(s, account)
        wm = json.loads(account.graph_sync)["sent"]["t"]
        late = datetime.fromisoformat(wm) - timedelta(minutes=3)
        store.add("sentitems", make_mail(USER, "spaet@kunde.de", "Angebot Dach", "Anbei.", ("Angebot_A-2026-300.pdf", _offer_nr("A-2026-300"))), late)
        assert scan_account(s, account)["new"] == 1
