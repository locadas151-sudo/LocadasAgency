"""WhatsApp-Einwilligung (Opt-in, STOP, START), Mail-Tipp als Seite statt mailto-Weiterleitung, Rechtstexte. Ohne Netzwerk."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from locadas.db import Business, DocKind, DocStatus, Document, FollowUp, new_session, utcnow
from locadas.engine import followups as fu_engine
from locadas.engine import trial
from locadas.notify import Notification, send
from locadas.web import auth
from tests.test_whatsapp import NUM, _client, _say, graph  # noqa: F401  (Fixture)


def _trial(s) -> Business:
    b = trial.create(s, "Thomas Müller", "Müller Haustechnik GmbH", NUM)
    b.wa_optin_at = utcnow()
    b.whatsapp_last_inbound_at = utcnow() - timedelta(minutes=5)
    s.commit()
    return b


def test_testen_form_has_required_unchecked_optin():
    r = _client().get("/testen")
    assert 'name="wa_optin"' in r.text and "required" in r.text.split('name="wa_optin"')[1][:60]
    assert "Ich bin einverstanden, dass mir Locadas zu meinen Angeboten per WhatsApp schreibt. Abbestellen jederzeit mit STOP." in r.text
    assert 'name="wa_optin" value="1" required checked' not in r.text


def test_stop_and_start(fresh_db, graph):
    with new_session() as s:
        bid = _trial(s).id
    c = _client()
    _say(c, "STOP")
    with new_session() as s:
        b = s.get(Business, bid)
        assert not b.whatsapp_opt_in and b.wa_optout_at is not None
    assert "START" in graph.texts()[-1]
    n_calls = len(graph.calls)
    with new_session() as s:  # abgemeldet: nichts mehr per WhatsApp, Mail als Rückfall (hier ohne Postfach: keine Zustellung)
        b = s.get(Business, bid)
        assert send(s, Notification(business=b, kind="info", title="x", text="y")) is False
    assert len(graph.calls) == n_calls
    _say(c, "Stopp")  # zweites Mal schadet nicht
    _say(c, "Start")
    with new_session() as s:
        b = s.get(Business, bid)
        assert b.whatsapp_opt_in and b.wa_optout_at is None and b.wa_optin_text.endswith(":start")
    assert "Abbestellen jederzeit mit STOP" in graph.texts()[-1]
    _say(c, "stopp 2")  # „Stopp 2“ bleibt der Listen-Befehl, keine Abmeldung
    with new_session() as s:
        assert s.get(Business, bid).whatsapp_opt_in


def test_settings_store_optin_proof(fresh_db, graph):
    with new_session() as s:
        b = Business(name="Betrieb", email="info@betrieb.de")
        s.add(b)
        s.commit()
        bid = b.id
    c = _client()
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    r = c.get("/einstellungen")
    assert "Abbestellen jederzeit mit STOP" in r.text
    c.post("/whatsapp", data={"whatsapp_number": "0171 2345678", "whatsapp_opt_in": ""})
    with new_session() as s:
        assert s.get(Business, bid).wa_optin_at is None
    c.post("/whatsapp", data={"whatsapp_number": "0171 2345678", "whatsapp_opt_in": "1"})
    with new_session() as s:
        b = s.get(Business, bid)
        assert b.whatsapp_opt_in and b.wa_optin_at is not None and b.wa_optin_text.startswith("wa-optin-")


def test_mailto_tap_page_tracks_humans_only(fresh_db, graph):
    now = utcnow()
    with new_session() as s:
        b = _trial(s)
        d = Document(business_id=b.id, account_id=None, kind=DocKind.OFFER, status=DocStatus.OPEN, subject="Angebot", sent_at=now - timedelta(days=4),
                     customer_name="Familie Brandt", customer_email="brandt@web.de", document_number="A-1", amount_gross=1000.0, source="forward")
        s.add(d)
        s.flush()
        fu_engine.schedule_followups(s, d, now - timedelta(days=4))
        f = d.followups[0]
        trial.start_tap(s, f, now)
        s.commit()
        token, fid = f.tap_token, f.id
        assert f.tap_kind == "mailto"
    c = _client()
    r = c.get(f"/t/{token}", headers={"user-agent": "WhatsApp/2.23 iOS"})  # Link-Vorschau
    assert r.status_code == 200 and r.headers["x-robots-tag"].startswith("noindex")
    with new_session() as s:
        assert s.get(FollowUp, fid).tapped_at is None
    r = c.get(f"/t/{token}", headers={"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Mobile/15E148"})
    assert 'id="open-mail" href="mailto:brandt@web.de?subject=' in r.text and "window.location.href=a.href" in r.text
    assert "brandt@web.de" in r.text and "Text kopieren" in r.text and "Gesendet" in r.text
    with new_session() as s:
        assert s.get(FollowUp, fid).tapped_at is not None


def test_legal_pages():
    c = _client()
    p = c.get("/datenschutz").text
    for needle in ("Anthropic PBC", "Unterauftragsverarbeiter", "USA", "Standardvertragsklauseln", "Meta Platforms Ireland", "STOP",
                   "14 Tagen", "STARTTLS", "Fotos", "https://locadas.de/impressum.html"):
        assert needle in p, needle
    a = c.get("/avv").text
    for needle in ("Amazon Web Services", "Frankfurt", "Anthropic PBC", "Meta Platforms Ireland", "Google Ireland", "Microsoft Ireland", "14 Tagen"):
        assert needle in a, needle
    assert "—" not in p and "—" not in a  # keine Gedankenstriche als Einschub
    assert "Impressum" in c.get("/").text
