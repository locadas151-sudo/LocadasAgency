"""OAuth-Verbindung (Google/Microsoft) mit gemocktem Token-Austausch."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import select

from locadas import main as m
from locadas.db import Business, MailAccount, new_session
from locadas.mail import oauth


def test_oauth_hidden_without_client_id(fresh_db):
    client = TestClient(m.app)
    assert client.get("/oauth/google/start", follow_redirects=False).status_code == 404
    assert "Mit Google verbinden" not in client.get("/verbinden").text


def test_oauth_google_flow(fresh_db, monkeypatch):
    monkeypatch.setattr(oauth.settings, "google_client_id", "cid")
    monkeypatch.setattr(oauth.settings, "google_client_secret", "sec")
    client = TestClient(m.app)
    assert "Mit Google verbinden" in client.get("/verbinden").text
    r = client.get("/oauth/google/start", params={"business_name": "Koch Elektro", "owner_name": "Sabine Koch", "avv": "1"}, follow_redirects=False)
    assert r.status_code == 303
    url = urlparse(r.headers["location"])
    assert url.netloc == "accounts.google.com"
    q = parse_qs(url.query)
    assert q["client_id"] == ["cid"] and q["access_type"] == ["offline"] and "mail.google.com" in q["scope"][0]
    state = q["state"][0]

    # Token-Austausch und IMAP-Login mocken
    import base64, json
    payload = base64.urlsafe_b64encode(json.dumps({"email": "koch@gmail.com"}).encode()).decode().rstrip("=")
    monkeypatch.setattr(oauth, "exchange_code", lambda p, code: {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "id_token": f"h.{payload}.s"})

    class FakeImap:
        def __init__(self, host, port, ssl, user, secret, auth_type):
            assert host == "imap.gmail.com" and user == "koch@gmail.com" and secret == "at" and auth_type == "oauth"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def find_sent_folder(self, cands):
            return "[Gmail]/Gesendet"

    monkeypatch.setattr(m, "ImapClient", FakeImap)
    monkeypatch.setattr(m, "_background_scan", lambda account_id: None)
    r = client.get("/oauth/google/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/start"
    with new_session() as s:
        b = s.scalars(select(Business)).one()
        a = s.scalars(select(MailAccount)).one()
        assert b.name == "Koch Elektro" and b.email == "koch@gmail.com"
        assert a.auth_type == "oauth" and a.provider == "google" and a.sent_folder == "[Gmail]/Gesendet" and a.smtp_host == "smtp.gmail.com"
        from locadas.crypto import decrypt
        assert decrypt(a.secret_enc) == "rt"
    # Abgelaufener/kaputter State wird abgewiesen
    assert client.get("/oauth/google/callback", params={"code": "abc", "state": "kaputt"}, follow_redirects=False).status_code == 400
    # Abbruch beim Anbieter zeigt das Formular mit Fehler
    r = client.get("/oauth/google/callback", params={"error": "access_denied", "state": state})
    assert r.status_code == 200 and "abgelehnt" in r.text
