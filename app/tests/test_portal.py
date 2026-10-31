"""Kundenportal: Kunde je Mailadresse, Überblick ohne Login, Fertig-Meldung und Weg zur Google-Bewertung."""
from __future__ import annotations

import email
import email.policy
import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from locadas.db import Business, Customer, DocKind, DocStatus, Document, Event, new_session, utcnow
from locadas.engine import customers as cust
from locadas.engine.scanner import scan_account
from locadas.web import auth
from tests.conftest import USER, click
from tests.fakemail import make_mail, make_pdf
from tests.test_e2e import _seed_sent, _setup

REVIEW = "https://g.page/r/CabcDEF123/review"


def _msg(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.default)


def _body(m) -> str:
    return m.get_body(preferencelist=("plain",)).get_content() if m.is_multipart() else m.get_content()


def _customer_mails(smtp, addr: str):
    return [_msg(raw) for _, rcpts, raw in smtp.messages if rcpts == [addr]]


def _owner_mails(smtp):
    return [_msg(raw) for _, rcpts, raw in smtp.messages if rcpts == [USER]]


def _scanned(s, imap, smtp, review_url: str = REVIEW):
    _seed_sent(imap)
    b, account = _setup(s, imap, smtp)
    scan_account(s, account)
    b.google_review_url = review_url
    s.commit()
    offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
    invoice = s.scalars(select(Document).where(Document.document_number == "R-2026-301")).one()
    return b, account, offer, invoice


def _app():
    from locadas.main import app

    return app


# --- Kunden anlegen ----------------------------------------------------------------------

def test_scanner_creates_customers_and_backfill_links_old_docs(fresh_db, imap, smtp):
    with new_session() as s:
        b, _, offer, invoice = _scanned(s, imap, smtp)
        cs = s.scalars(select(Customer).order_by(Customer.email)).all()
        assert [c.email for c in cs] == ["familie.schmidt@gmx.de", "p.weber@web.de"]
        assert all(len(c.portal_token) >= 32 for c in cs)  # 24 Byte = 192 Bit
        assert cs[0].portal_token != cs[1].portal_token
        # Altbestand ohne Kunden (vor dem Portal): wird beim ersten Zugriff angelegt
        for c in cs:
            s.delete(c)
        s.commit()
        assert s.scalar(select(Customer)) is None
        c = cust.for_document(s, offer)
        assert c is not None and c.email == "familie.schmidt@gmx.de" and c.name
        assert cust.backfill(s, b.id) == 1
        assert cust.for_document(s, offer).id == c.id  # kein Duplikat
        offer.customer_email = "Familie.Schmidt@GMX.de"
        assert cust.for_document(s, offer).id == c.id  # Großschreibung egal
        s.commit()


def test_dashboard_backfills_and_shows_review_kpi(fresh_db, imap, smtp):
    with new_session() as s:
        b, _, offer, _ = _scanned(s, imap, smtp)
        for c in s.scalars(select(Customer)).all():
            s.delete(c)
        s.commit()
        bid = b.id
    web = TestClient(_app())
    web.cookies.set(auth.COOKIE, auth.session_token(bid))
    r = web.get("/dashboard")
    assert r.status_code == 200 and "Bewertungs-Klicks" in r.text
    with new_session() as s:
        assert len(s.scalars(select(Customer)).all()) == 2


# --- Portal ----------------------------------------------------------------------------

def test_portal_shows_only_own_documents_and_open_offer_state(fresh_db, imap, smtp):
    with new_session() as s:
        b, _, offer, invoice = _scanned(s, imap, smtp)
        b.whatsapp_number = "491712345678"
        s.commit()
        schmidt = cust.for_document(s, offer)
        token, weber_nr = schmidt.portal_token, invoice.document_number
        view = offer.view_token
    web = TestClient(_app())
    r = web.get(f"/k/{token}")
    assert r.status_code == 200
    assert r.headers["x-robots-tag"].startswith("noindex") and r.headers["referrer-policy"] == "no-referrer"
    t = r.text
    assert 'name="robots" content="noindex' in t
    assert "Müller Haustechnik GmbH" in t and "bereitgestellt über" in t
    assert "A-2026-118" in t and "3.724,70 €" in t and f"/a/{view}" in t
    assert weber_nr not in t and "p.weber@web.de" not in t and "Weber" not in t  # kein anderer Kunde
    assert 'href="tel:01711"' in t and "mailto:" in t and "https://wa.me/491712345678" in t
    assert "Ihr Auftrag ist fertig" not in t and "Bei Google bewerten" not in t
    assert "liegt Ihnen vor" in t
    # Unbekannter / zu langer Token
    assert web.get("/k/gibtesnicht").status_code == 404
    assert web.get("/k/" + "x" * 80).status_code == 404


def test_timeline_states(fresh_db, imap, smtp):
    with new_session() as s:
        _, _, offer, invoice = _scanned(s, imap, smtp)
        c = cust.for_document(s, offer)
        steps = cust.timeline(c, cust.documents(s, c))
        assert [x["key"] for x in steps] == ["offer", "confirmed", "done", "invoice", "paid"]
        assert [x["done"] for x in steps] == [True, False, False, False, False]
        assert steps[1]["current"]
        offer.status, offer.outcome_at = DocStatus.WON, utcnow()
        c.completed_at = utcnow()
        steps = cust.timeline(c, cust.documents(s, c))
        assert [x["done"] for x in steps] == [True, True, True, False, False] and steps[3]["current"]
        # Kunde nur mit Rechnung: kein Angebots-Schritt, Rechnung zählt als Auftrag
        w = cust.for_document(s, invoice)
        steps = cust.timeline(w, cust.documents(s, w))
        assert [x["key"] for x in steps] == ["confirmed", "done", "invoice", "paid"]
        assert [x["done"] for x in steps] == [True, False, True, False]
        invoice.status, invoice.closed_at = DocStatus.PAID, utcnow()
        assert cust.timeline(w, cust.documents(s, w))[-1]["done"]


def test_customer_view_links_to_portal(fresh_db, imap, smtp):
    with new_session() as s:
        _, _, offer, _ = _scanned(s, imap, smtp)
        view, token = offer.view_token, cust.for_document(s, offer).portal_token
        s.commit()
    r = TestClient(_app()).get(f"/a/{view}")
    assert r.status_code == 200 and f"/k/{token}" in r.text and "Alle Unterlagen zu Ihrem Auftrag" in r.text


# --- Fertig melden --------------------------------------------------------------------

def test_fertig_sends_exactly_one_mail_with_portal_and_review_link(fresh_db, imap, smtp):
    with new_session() as s:
        b, _, offer, _ = _scanned(s, imap, smtp)
        offer.status = DocStatus.WON
        c = cust.for_document(s, offer)
        s.commit()
        cid, token, bid, doc_id = c.id, c.portal_token, b.id, offer.id
    n_before = len(smtp.messages)
    web = TestClient(_app())
    web.cookies.set(auth.COOKIE, auth.session_token(bid))
    r = web.get(f"/dokument/{doc_id}")
    assert f"/k/{token}" in r.text and "Fertig melden" in r.text
    r = web.post(f"/kunde/{cid}/fertig", data={"next": f"/dokument/{doc_id}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/dokument/{doc_id}?msg=fertig"
    assert web.post(f"/kunde/{cid}/fertig", data={"next": "https://evil.example"}, follow_redirects=False).headers["location"] == "/dashboard?msg=fertig_schon"
    mails = _customer_mails(smtp, "familie.schmidt@gmx.de")
    assert len(mails) == 1
    m = mails[0]
    assert USER in m["From"] and not m["Subject"].lower().startswith(("aw:", "re:")) and not m["In-Reply-To"]
    assert "abgeschlossen" in m["Subject"]
    body = _body(m)
    assert f"http://testserver/k/{token}" in body
    assert f"http://testserver/k/{token}/bewerten" in body
    assert "Bewertung bei Google" in body and "zufrieden" not in body.lower()
    assert body.startswith("Guten Tag Familie Schmidt,") and "Thomas Müller" in body
    assert len(smtp.messages) == n_before + 1  # keine weitere Mail, auch kein Tipp an den Betrieb
    with new_session() as s:
        c = s.get(Customer, cid)
        assert c.completed_at and c.review_requested_at
        assert s.scalar(select(Event).where(Event.kind == "completed")) is not None
    # Portal zeigt den Abschluss mit Google-Karte und Rückmeldung daneben
    t = TestClient(_app()).get(f"/k/{token}").text
    assert "Ihr Auftrag ist fertig" in t and "Bei Google bewerten" in t and "Direkt an Müller Haustechnik GmbH" in t
    assert t.index("Bei Google bewerten") < t.index("Rückmeldung senden")  # Google-Link nie hinter der Rückmeldung
    assert len(_customer_mails(smtp, "familie.schmidt@gmx.de")) == 1


def test_fertig_without_review_url_skips_review_and_hints_owner_once(fresh_db, imap, smtp):
    with new_session() as s:
        b, _, offer, invoice = _scanned(s, imap, smtp, review_url="")
        c1, c2 = cust.for_document(s, offer), cust.for_document(s, invoice)
        s.commit()
        assert cust.mark_complete(s, c1) == "sent"
        assert cust.mark_complete(s, c1) == "already"
        assert cust.mark_complete(s, c2) == "sent"
        token = c1.portal_token
        assert c1.review_requested_at is None
    body = _body(_customer_mails(smtp, "familie.schmidt@gmx.de")[0])
    assert f"/k/{token}" in body and "/bewerten" not in body and "Google" not in body
    hints = [m for m in _owner_mails(smtp) if "Bewertungslink" in m["Subject"]]
    assert len(hints) == 1
    assert "alle Kunden" in _body(hints[0]) and "zufriedene" not in _body(hints[0])
    t = TestClient(_app()).get(f"/k/{token}").text
    assert "Ihr Auftrag ist fertig" in t and "Bei Google bewerten" not in t and "Rückmeldung senden" in t
    # Ohne Link leitet /bewerten zurück ins Portal
    r = TestClient(_app()).get(f"/k/{token}/bewerten", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/k/{token}"


def test_review_redirect_records_first_click(fresh_db, imap, smtp):
    with new_session() as s:
        _, _, offer, _ = _scanned(s, imap, smtp)
        c = cust.for_document(s, offer)
        cust.mark_complete(s, c)
        token, cid, bid = c.portal_token, c.id, c.business_id
    web = TestClient(_app())
    # Vorschau des Betriebs zählt nicht
    owner = TestClient(_app())
    owner.cookies.set(auth.COOKIE, auth.session_token(bid))
    assert owner.get(f"/k/{token}/bewerten", follow_redirects=False).headers["location"] == REVIEW
    with new_session() as s:
        assert s.get(Customer, cid).review_clicked_at is None
    r = web.get(f"/k/{token}/bewerten", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == REVIEW and r.headers["referrer-policy"] == "no-referrer"
    web.get(f"/k/{token}/bewerten", follow_redirects=False)
    with new_session() as s:
        assert s.get(Customer, cid).review_clicked_at is not None
        assert len(s.scalars(select(Event).where(Event.kind == "review_click")).all()) == 1
    owner_page = owner.get("/dashboard").text
    assert re.search(r'kpi-num">1</span><span class="kpi-lbl">Bewertungs-Klicks', owner_page)


def test_feedback_stored_owner_notified_and_rate_limited(fresh_db, imap, smtp):
    with new_session() as s:
        _, _, offer, _ = _scanned(s, imap, smtp)
        c = cust.for_document(s, offer)
        cust.mark_complete(s, c)
        token, cid = c.portal_token, c.id
    web = TestClient(_app())
    r = web.post(f"/k/{token}/rueckmeldung", data={"text": "  Alles top, nur der Keller war staubig.  "}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/k/{token}?danke=1")
    assert "Ihre Rückmeldung ist angekommen" in web.get(f"/k/{token}?danke=1").text
    with new_session() as s:
        c = s.get(Customer, cid)
        assert c.feedback_text == "Alles top, nur der Keller war staubig." and c.feedback_at
    note = [m for m in _owner_mails(smtp) if m["Subject"].startswith("Rückmeldung von")]
    assert len(note) == 1 and "Keller war staubig" in _body(note[0])
    assert web.post(f"/k/{token}/rueckmeldung", data={"text": "   "}, follow_redirects=False).status_code == 303
    for _ in range(4):
        web.post(f"/k/{token}/rueckmeldung", data={"text": "noch was"}, follow_redirects=False)
    assert web.post(f"/k/{token}/rueckmeldung", data={"text": "spam"}).status_code == 429
    assert web.post("/k/falsch/rueckmeldung", data={"text": "x"}).status_code == 404


# --- Rechnung zum gewonnenen Angebot: einmal fragen ---------------------------------------

def _followup_invoice(nr: str) -> bytes:
    return make_mail(USER, "familie.schmidt@gmx.de", f"Rechnung {nr}", "Anbei die Rechnung.", (f"Rechnung_{nr}.pdf", make_pdf([
        "Mueller Haustechnik GmbH", f"Rechnung Nr. {nr}", "Familie Schmidt, Gartenweg 4, 41061 Moenchengladbach",
        "Gastherme Vaillant 1 St 2.950,00 EUR", "Rechnungsbetrag 3.724,70 EUR", "Zahlbar innerhalb von 14 Tagen",
    ])), to_name="Familie Schmidt", date=utcnow())


def test_invoice_for_won_offer_suggests_fertig_once_and_link_needs_post(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, _ = _scanned(s, imap, smtp)
        assert not [m for m in _owner_mails(smtp) if "Auftrag fertig" in m["Subject"]]  # Erst-Scan fragt nicht
        imap.store.append("Gesendet", _followup_invoice("R-2026-400"))
        scan_account(s, account)
        assert s.get(Document, offer.id).status == DocStatus.WON  # Rechnung schließt das Angebot
        imap.store.append("Gesendet", _followup_invoice("R-2026-401"))
        scan_account(s, account)
        cid = cust.for_document(s, offer).id
    asks = [m for m in _owner_mails(smtp) if "Ist der Auftrag fertig" in m["Subject"]]
    assert len(asks) == 1
    link = re.search(r"http://testserver(/fertig/\S+)", _body(asks[0])).group(1)
    web = TestClient(_app())
    n = len(smtp.messages)
    r = web.get(link)  # Link-Vorschau im Mailprogramm
    assert r.status_code == 200 and 'method="post"' in r.text and "Ja, Auftrag ist fertig" in r.text
    assert len(smtp.messages) == n
    with new_session() as s:
        assert s.get(Customer, cid).completed_at is None
    r = web.post(link)
    assert r.status_code == 200 and "Abschluss-Mail ist raus" in r.text
    assert len(_customer_mails(smtp, "familie.schmidt@gmx.de")) == 1
    assert "Schon gemeldet" in web.get(link).text and "Schon gemeldet" in web.post(link).text
    assert len(_customer_mails(smtp, "familie.schmidt@gmx.de")) == 1
    assert web.get("/fertig/kaputt").status_code == 404
    # Der Portal-Token taugt nicht als Fertig-Link (Kunde soll sich nicht selbst fertig melden)
    with new_session() as s:
        assert web.get(f"/fertig/{s.get(Customer, cid).portal_token}").status_code == 404


def test_outcome_won_link_offers_fertig(fresh_db, imap, smtp):
    with new_session() as s:
        _, _, offer, _ = _scanned(s, imap, smtp)
        token = offer.outcome_token
        offer.outcome_asked_at = utcnow()
        s.commit()
    r = click(TestClient(_app()), f"/o/{token}/won")
    assert "Danke, ist notiert" in r.text and "/fertig/" in r.text and "Fertig melden" in r.text


def test_settings_validate_review_url(fresh_db, imap, smtp):
    with new_session() as s:
        b, *_ = _scanned(s, imap, smtp, review_url="")
        bid = b.id
    web = TestClient(_app())
    web.cookies.set(auth.COOKIE, auth.session_token(bid))
    base = {"name": "Müller Haustechnik GmbH"}
    for bad in ("javascript:alert(1)", "https://evil.example/google.com", "https://google.com.evil.de/x", "ftp://g.page/x", "https://user@g.page/r/x"):
        r = web.post("/einstellungen", data={**base, "google_review_url": bad}, follow_redirects=False)
        assert r.headers["location"].endswith("review_ungueltig"), bad
    for good in ("https://search.google.com/local/writereview?placeid=ChIJ123", "https://g.page/r/Cabc/review", "https://www.google.de/maps/place/x"):
        web.post("/einstellungen", data={**base, "google_review_url": good})
        with new_session() as s:
            assert s.get(Business, bid).google_review_url == good
    web.post("/einstellungen", data={**base, "google_review_url": "", "review_field": "1"})
    with new_session() as s:
        assert s.get(Business, bid).google_review_url == ""
    assert "Google-Bewertungslink" in web.get("/einstellungen").text


def test_whatsapp_fertig_button(fresh_db, imap, smtp, monkeypatch):
    from tests.test_whatsapp import NUM, Graph, _press
    from locadas.config import settings
    from locadas.notify import tokens
    from locadas.notify import whatsapp as wa

    g = Graph()
    monkeypatch.setattr(wa, "http_post", g)
    monkeypatch.setattr(settings, "whatsapp_token", "tok")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "PNID")
    from tests.test_whatsapp import SECRET
    monkeypatch.setattr(settings, "whatsapp_app_secret", SECRET)
    with new_session() as s:
        b, _, offer, _ = _scanned(s, imap, smtp)
        b.whatsapp_number, b.whatsapp_opt_in = NUM, True
        b.whatsapp_last_inbound_at = utcnow() - timedelta(hours=1)
        s.commit()
        doc_id = offer.id
        cid = cust.for_document(s, offer).id
        s.commit()
    web = TestClient(_app())
    _press(web, tokens.payload("won", doc_id))
    assert "Auftrag fertig" in str(g.calls[-1])  # Knopf für später
    _press(web, tokens.payload("fertig", cid))
    assert "Abschluss-Mail" in g.texts()[-1]
    _press(web, tokens.payload("fertig", cid))
    assert "schon bekommen" in g.texts()[-1]
    assert len(_customer_mails(smtp, "familie.schmidt@gmx.de")) == 1
    _press(web, tokens.payload("notyet", cid))
    assert "frage nicht noch einmal" in g.texts()[-1]


def test_business_delete_removes_customers(fresh_db, imap, smtp):
    with new_session() as s:
        b, *_ = _scanned(s, imap, smtp)
        bid = b.id
    web = TestClient(_app())
    web.cookies.set(auth.COOKIE, auth.session_token(bid))
    web.post("/einstellungen/trennen", data={"confirm": "ja"})
    with new_session() as s:
        assert s.scalar(select(Customer)) is None and s.get(Business, bid) is None
        assert DocKind.OFFER  # Import genutzt


def test_init_db_migrates_existing_db_to_portal(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from locadas import db as dbmod

    url = f"sqlite:///{tmp_path / 'old.db'}"
    dbmod.reset_engine_for_tests(url)
    with dbmod.get_engine().begin() as conn:
        conn.execute(text("DROP TABLE customers"))
        for col in ("google_review_url", "review_hint_at"):
            conn.execute(text(f"ALTER TABLE businesses DROP COLUMN {col}"))
        conn.execute(text(
            "INSERT INTO businesses (name, owner_name, email, signature, phone, followup_offers, followup_invoices, paused, notify_opened, "
            "include_view_link, approvals_done, auto_mode, personalize, style_json, style_samples_json, created_at) "
            "VALUES ('A', '', 'a@b.de', '', '', 1, 1, 0, 1, 1, 0, 0, 1, '', '', '2026-01-01 00:00:00')"
        ))
    dbmod.reset_engine_for_tests(url)
    insp = inspect(create_engine(url))
    assert insp.has_table("customers")
    assert {"google_review_url", "review_hint_at"} <= {c["name"] for c in insp.get_columns("businesses")}
    with dbmod.new_session() as s:
        b = s.query(dbmod.Business).one()
        assert b.google_review_url == ""
        c = cust.get_or_create(s, b.id, "Kunde@Example.org", "Kunde")
        s.commit()
        assert c.email == "kunde@example.org" and c.portal_token
