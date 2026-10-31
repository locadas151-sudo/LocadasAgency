"""Regressionstests aus dem Launch-Check (Handy-Ansicht, Texte, Fehlerseiten, Auth auf allen Routen, Links)."""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from locadas import fmt
from locadas.db import Business, DocKind, DocStatus, Document, FollowUpStatus, new_session, utcnow
from locadas.engine import followups as fu_engine
from locadas.engine import inbound_mail, texts, trial, wa_texts
from locadas.notify import messages as wa_msgs
from locadas.web import auth
from tests.test_trial import SECRET, _mail, _post, _trial, inbound  # noqa: F401  (Fixture)
from tests.test_whatsapp import NUM, _client, graph  # noqa: F401  (Fixture)
from tests.fakemail import make_pdf


def _owner_forward(addr: str, quoted_inquiry: str = "") -> bytes:
    """Weitergeleitete eigene Mail mit eigener Signatur (Telefon des Betriebs), optional mit zitierter Kundenanfrage."""
    body = (
        "\n\n-------- Weitergeleitete Nachricht --------\n"
        "Betreff: \tAngebot Badsanierung\nDatum: \t26.10.2026 10:12\nVon: \tThomas Müller <thomas.mueller@gmx.de>\n"
        "An: \tFamilie Brandt <brandt@web.de>\n\n\n"
        "Guten Tag Frau Brandt,\n\nanbei wie besprochen unser Angebot.\n\nMit freundlichen Grüßen\nThomas Müller\n"
        "Müller Haustechnik\nTel. 02161 123456\nMobil 0171 5556667\n" + quoted_inquiry
    )
    pdf = make_pdf(["Müller Haustechnik GmbH", "Tel. 02161 123456", "Angebot Nr. A-2026-118", "Familie Brandt",
                    "Lindenweg 12, 41063 Mönchengladbach", "Gesamtbetrag 4.210,22 EUR"])
    return _mail("Thomas Müller <thomas.mueller@gmx.de>", addr, "WG: Angebot Badsanierung", body, pdf)


def test_forward_owner_signature_is_not_customer_phone(fresh_db, inbound, graph):
    """Bug: Telefon und Handy aus der Signatur der weitergeleiteten EIGENEN Mail landeten als Kundennummer."""
    with new_session() as s:
        b = _trial(s)
        addr = b.inbound_address
    r = _post(_client(), _owner_forward(addr), rcpt=addr)
    assert r.status_code == 200, r.text
    with new_session() as s:
        d = s.scalars(select(Document)).one()
        assert d.customer_email == "brandt@web.de"
        assert d.customer_phone == "" and d.customer_mobile == ""


def test_forward_keeps_customer_number_from_quoted_inquiry(fresh_db, inbound, graph):
    quoted = "\nAm 20.10.2026 schrieb Petra Brandt <brandt@web.de>:\n> Hallo, bitte um ein Angebot fürs Bad.\n> Gruß Petra Brandt\n> Handy: 0172 4455667\n"
    with new_session() as s:
        b = _trial(s)
        addr = b.inbound_address
    assert _post(_client(), _owner_forward(addr, quoted), rcpt=addr).status_code == 200
    with new_session() as s:
        d = s.scalars(select(Document)).one()
        assert d.customer_mobile == "491724455667"


def test_split_forward():
    rest, own = inbound_mail.split_forward("Hier.\n\n---- Weitergeleitete Nachricht ----\nVon: a@b.de\nAn: c@d.de\n\nMeine Mail\nTel 0211 1\n\nAm 1.1. schrieb X:\n> Kunde")
    assert "Meine Mail" in own and "Kunde" in rest and "Meine Mail" not in rest
    assert inbound_mail.split_forward("ohne Weiterleitung") == ("ohne Weiterleitung", "")


def test_testen_typos_do_not_use_up_rate_limit(fresh_db, inbound, graph):
    """Bug: jeder Tippfehler zählte gegen 5 Anmeldungen je 10 Minuten und IP (Mobilfunk teilt sich IPs)."""
    c = _client()
    for _ in range(12):
        r = c.post("/testen", data={"owner_name": "Thomas", "business_name": "Müller", "whatsapp": "123", "wa_optin": "1"})
        assert r.status_code == 200 and "Handynummer konnten wir nicht lesen" in r.text
    r = c.post("/testen", data={"owner_name": "Thomas", "business_name": "Müller", "whatsapp": "0171 2345678", "wa_optin": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/testen/los"


def test_trial_mail_tap_is_labelled_as_mail_on_document_and_dashboard(fresh_db, inbound, graph):
    """Bug: Mail-Tipp im Test hieß auf der Dokumentseite „WhatsApp-Nachricht … In WhatsApp öffnen“."""
    with new_session() as s:
        b = _trial(s)
        d = Document(business_id=b.id, kind=DocKind.OFFER, status=DocStatus.OPEN, subject="Angebot", sent_at=utcnow() - timedelta(days=5),
                     customer_name="Familie Brandt", customer_email="brandt@web.de", document_number="A-1", amount_gross=100.0, source="forward")
        s.add(d)
        s.flush()
        fu_engine.schedule_followups(s, d)
        f = d.followups[0]
        trial.start_tap(s, f)
        s.commit()
        bid, did, tok = b.id, d.id, f.tap_token
    c = _client()
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    page = c.get(f"/dokument/{did}").text
    assert "Mail öffnen" in page and "In WhatsApp öffnen" not in page and "WhatsApp-Nachricht" not in page
    assert "Nächste Stufe jetzt senden" not in page  # im Test geht nie etwas automatisch raus
    dash = c.get("/dashboard").text
    assert "Heute zum Abschicken" in dash and "Bewertungs-Klicks" not in dash
    confirm = c.get(f"/w/{tok}/sent").text
    assert "Nachfass-Mail an Familie Brandt" in confirm and "WhatsApp-Nachricht" not in confirm


def test_customer_texts_salutation_and_reference():
    b = Business(name="Müller Haustechnik GmbH", owner_name="Thomas Müller", phone="", signature="", tone="freundlich", include_view_link=False)
    d = Document(kind=DocKind.OFFER, customer_name="Herr Peter Weber", customer_email="p@web.de", document_number="", title="",
                 subject="Ihr Angebot", sent_at=utcnow() - timedelta(days=4), amount_gross=None)
    d.business = b
    body = texts.body_for(d, 1)
    assert body.startswith("Guten Tag Herr Weber,") and "()" not in body and "(Ihr Angebot)" not in body
    assert wa_texts.template_text(d, 1).startswith("Hallo Herr Weber,")
    d.document_number, d.title = "A-1", "Badsanierung"
    assert "unser Angebot (Badsanierung, Nr. A-1)" in texts.body_for(d, 1)
    assert fmt.surname_form("Dr. Anna Meier") == "Dr. Meier" and fmt.surname_form("Klaus von der Heide") == "von der Heide"
    assert fmt.dativ("Herr Peter Weber") == "Herrn Peter Weber" and fmt.dativ("Familie Brandt") == "Familie Brandt"


def test_owner_notification_grammar(fresh_db):
    with new_session() as s:
        b = Business(name="Müller", owner_name="Thomas Müller", email="t@m.de")
        s.add(b)
        s.flush()
        inv = Document(business_id=b.id, kind=DocKind.INVOICE, status=DocStatus.OPEN, subject="Rechnung", sent_at=utcnow() - timedelta(days=20),
                       due_date=utcnow() - timedelta(days=5), customer_name="Herr Peter Weber", customer_email="p@web.de", document_number="R-1", amount_gross=224.91)
        s.add(inv)
        s.flush()
        assert wa_msgs.opened(inv, "s", "b").title == "Herr Peter Weber hat deine Rechnung geöffnet"
        assert "an Herrn Peter Weber" in fu_engine.invoice_question(inv)
        _, body = fu_engine.outcome_mail(b, [inv])
        assert "Angebote zum Auftrag" not in body


def test_friendly_errors_instead_of_json(fresh_db):
    c = TestClient(__import__("locadas.main", fromlist=["app"]).app, raise_server_exceptions=False)
    r = c.post("/verbinden", data={})
    assert "text/html" in r.headers["content-type"] and "Field required" not in r.text
    r = c.post("/login", data={})
    assert "text/html" in r.headers["content-type"] and "Mailadresse oder Handynummer" in r.text
    assert c.get("/openapi.json").status_code == 404
    r = c.post("/dokument/abc/status", data={}, follow_redirects=False)
    assert r.status_code in (303, 404, 422) and "detail" not in r.text


def test_server_error_page_is_german(fresh_db, monkeypatch):
    from locadas import main

    with new_session() as s:
        b = Business(name="Müller", owner_name="Thomas Müller", email="t@m.de")
        s.add(b)
        s.commit()
        bid = b.id

    def boom(*a, **k):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(main.summary, "current_month", boom)
    c = TestClient(main.app, raise_server_exceptions=False)
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    r = c.get("/dashboard")
    assert r.status_code == 500 and "Da ist etwas schiefgelaufen" in r.text and "Traceback" not in r.text


def test_connect_outlook_without_password_explains_microsoft(fresh_db):
    r = _client().post("/verbinden", data={"business_name": "Müller", "email": "max@outlook.de", "consent": "1", "avv": "1"})
    assert r.status_code == 200 and "Mit Microsoft anmelden" in r.text


def test_connect_page_script_is_not_html_escaped(fresh_db):
    """Bug: autoescape machte aus dem JS-String &#34;…&#34;, das Skript brach ab (keine Microsoft-Erkennung beim Tippen)."""
    page = _client().get("/verbinden").text
    script = page[page.rfind("<script>"):]
    assert "&#34;" not in script and "&quot;" not in script


# --- Auth auf allen Routen, interne Links ------------------------------------------------------

PUBLIC = {"/", "/health", "/healthz", "/testen", "/check", "/datenschutz", "/avv", "/login", "/verbinden", "/verbinden/anbieter", "/admin/login"}


def test_every_route_needs_auth_or_token(fresh_db):
    from locadas.main import app

    c = TestClient(app, raise_server_exceptions=False)
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/static"):
            continue
        url = re.sub(r"\{(\w+)\}", lambda m: "1" if m.group(1).endswith("id") else "unguessable-token-xyz", path)
        for method in sorted((getattr(route, "methods", None) or set()) - {"HEAD"}):
            r = c.request(method, url, follow_redirects=False)
            assert r.status_code < 500, (method, path, r.status_code)
            if path not in PUBLIC and method == "GET":
                assert r.status_code != 200 or path == "/check", (method, path)
            if path not in PUBLIC and method == "POST" and path not in ("/testen", "/check"):
                assert r.status_code in (303, 400, 403, 404, 422, 429), (method, path, r.status_code)


def test_internal_links_resolve(fresh_db, imap, smtp):
    from tests.test_e2e import _seed_sent, _setup
    from locadas.engine.scanner import scan_account

    _seed_sent(imap)
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        scan_account(s, account)
        bid = b.id
        doc_ids = [d.id for d in s.scalars(select(Document)).all()]
    c = _client()
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    pages = ["/", "/dashboard", "/einstellungen", "/freigaben", "/start", "/testen", "/datenschutz", "/avv", "/check"] + [f"/dokument/{i}" for i in doc_ids]
    seen: set[str] = set()
    for p in pages:
        r = c.get(p)
        assert r.status_code == 200, (p, r.status_code)
        for href in re.findall(r'href="(/[^"#]*)', r.text):
            if href.startswith(("/static", "/logout")) or href in seen:
                continue
            seen.add(href)
            rr = c.get(href.replace("&amp;", "&"), follow_redirects=False)
            assert rr.status_code < 400 or href.startswith("/a/"), (p, href, rr.status_code)
    assert len(seen) > 15
