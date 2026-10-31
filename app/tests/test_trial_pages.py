"""Seiten für Tester rendern ohne Fehler (kein Postfach, Dokumente ohne account_id)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from locadas.db import DocKind, DocStatus, Document, new_session, utcnow
from locadas.engine import followups as fu_engine
from locadas.engine import trial
from locadas.web import auth
from tests.test_whatsapp import NUM, _client


def test_trial_pages_render(fresh_db):
    with new_session() as s:
        b = trial.create(s, "Thomas Müller", "Müller Haustechnik", NUM, "")
        d = Document(business_id=b.id, account_id=None, kind=DocKind.OFFER, status=DocStatus.OPEN, subject="Angebot", sent_at=utcnow() - timedelta(days=1),
                     customer_name="Familie Brandt", customer_email="brandt@web.de", document_number="A-1", amount_gross=100.0, source="forward_unverified",
                     inbound_from="x@y.de")
        s.add(d)
        s.flush()
        fu_engine.schedule_followups(s, d)
        s.commit()
        bid, did = b.id, d.id
    c = _client()
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    for path in ("/dashboard", "/dashboard?filter=all", f"/dokument/{did}", "/einstellungen", "/testen/los", "/start"):
        r = c.get(path)
        assert r.status_code == 200, (path, r.text[:500])
    dash = c.get("/dashboard").text
    assert "Dein Test" in dash and "Termin buchen" in dash and "Jetzt Postfach prüfen" not in dash
    # „Jetzt senden“ ohne Postfach: nichts geht raus, kein Absturz
    r = c.post(f"/dokument/{did}/status", data={"action": "send_now"}, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as s:
        d = s.get(Document, did)
        assert all(f.status.value != "sent" for f in d.followups)
    # Login per Handynummer (Tester ohne Mail)
    r = _client().post("/login", data={"email": "0171 2345678", "next": "/dashboard"})
    assert r.status_code == 200
    with new_session() as s:
        assert s.scalars(select(Document)).first() is not None


def test_connected_dashboard_offers_copy_address(fresh_db, imap, smtp):
    from tests.test_e2e import _setup

    with new_session() as s:
        b, _ = _setup(s, imap, smtp)
        bid = b.id
    c = _client()
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    assert "Kopie-Adresse anlegen" in c.get("/dashboard").text
    r = c.post("/weiterleitung/adresse", follow_redirects=False)
    assert r.status_code == 303
    with new_session() as s:
        from locadas.db import Business

        addr = s.get(Business, bid).inbound_address
        assert addr and addr.endswith("@angebot.locadas.de")
    assert addr in c.get("/dashboard").text
