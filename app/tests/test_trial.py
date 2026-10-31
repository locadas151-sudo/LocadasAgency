"""Testen ohne Postfach: Anmeldung, Weiterleitungsadresse, Eingang per Webhook/SMTP/WhatsApp, Tipps ohne Postfach,
Aha-Nachricht, Upgrade, Umwandeln beim Verbinden, Grenzen, Aufräumen. Ohne Netzwerk (Graph API und Claude gemockt)."""
from __future__ import annotations

import io
import json
import re
import smtplib
import socket
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from locadas.config import settings
from locadas.db import Business, DocKind, DocStatus, Document, FollowUp, FollowUpStatus, InboundMail, new_session, utcnow
from locadas.engine import followups as fu_engine
from locadas.engine import inbound_mail, taps, trial
from locadas.extract import vision
from locadas.holidays import is_workday
from locadas.notify import inbound as wa_inbound
from locadas.notify import whatsapp as wa
from tests.conftest import PASSWORD, USER
from tests.fakemail import make_pdf
from tests.test_e2e import _offer_pdf, _setup
from tests.test_whatsapp import NUM, _client, _hook, _press, graph  # noqa: F401  (Fixture)

SECRET = "inbound-secret-test"
CUSTOMER = "brandt@web.de"


def _workday_10() -> datetime:
    d = datetime.now(timezone.utc) + timedelta(days=1)
    while not is_workday(d.date()):
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, 9, 0, tzinfo=timezone.utc)


NOW = _workday_10()


@pytest.fixture()
def inbound(monkeypatch):
    monkeypatch.setattr(settings, "inbound_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "inbound_domain", "angebot.locadas.de")
    monkeypatch.setattr(wa_inbound, "MEDIA_ASYNC", False)


def _trial(s, email: str = "", number: str = NUM, window: bool = True) -> Business:
    b = trial.create(s, "Thomas Müller", "Müller Haustechnik GmbH", number, email)
    if window:
        b.whatsapp_last_inbound_at = utcnow() - timedelta(minutes=5)
    s.commit()
    return b


def _mail(frm: str, to: str, subject: str, body: str, pdf: bytes | None = None, cc: str = "", date: datetime | None = None, extra: dict | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = format_datetime(date or (utcnow() - timedelta(hours=1)))
    msg["Message-ID"] = make_msgid(domain="test.local")
    for k, v in (extra or {}).items():
        msg[k] = v
    msg.set_content(body)
    if pdf is not None:
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="Angebot_A-2026-118.pdf")
    return msg.as_bytes()


def _outlook_forward(addr: str, sent: str = "Dienstag, 27. Oktober 2026 16:40", frm: str = "Thomas Müller <info@mueller-shk.de>") -> bytes:
    body = (
        "Hier das Angebot.\n\n"
        "-----Ursprüngliche Nachricht-----\n"
        f"Von: {frm}\n"
        f"Gesendet: {sent}\n"
        "An: Familie Brandt <brandt@web.de>\n"
        "Betreff: Angebot Gastherme\n\n"
        "Guten Tag Familie Brandt,\nanbei unser Angebot.\n"
    )
    return _mail(frm, addr, "WG: Angebot Gastherme", body, _offer_pdf())


def _post(client, raw: bytes, secret: str = SECRET, rcpt: str = ""):
    headers = {"X-Inbound-Secret": secret, "Content-Type": "message/rfc822"}
    if rcpt:
        headers["X-Envelope-To"] = rcpt
    return client.post("/inbound/raw", content=raw, headers=headers)


# --- Anmeldung ------------------------------------------------------------------------------------

def test_signup_creates_trial_with_address(fresh_db, inbound, graph):
    c = _client()
    r = c.post("/testen", data={"owner_name": "Thomas Müller", "business_name": "Müller Haustechnik GmbH", "whatsapp": "0171 2345678"}, follow_redirects=False)
    assert r.status_code == 200 and "per WhatsApp schreiben darf" in r.text  # Einwilligung fehlt
    r = c.post("/testen", data={"owner_name": "Thomas Müller", "business_name": "Müller Haustechnik GmbH", "whatsapp": "0171 2345678", "wa_optin": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/testen/los"
    with new_session() as s:
        b = s.scalars(select(Business)).one()
        assert b.mode == "trial" and b.is_trial and not b.accounts
        assert re.fullmatch(r"mueller-haustechnik-[a-z2-9]{6}@angebot\.locadas\.de", b.inbound_address)
        assert b.owner_email == "" and b.email.endswith("@trial.invalid")
        assert b.whatsapp_number == NUM and b.whatsapp_opt_in
        assert b.wa_optin_at is not None and b.wa_optin_text.startswith("wa-optin-") and b.signup_source == "testen"
        addr = b.inbound_address
    # Willkommen per Vorlage (kein 24h-Fenster), mit der Adresse
    assert graph.calls and graph.last["type"] == "template"
    assert addr in json.dumps(graph.last, ensure_ascii=False)
    page = c.get("/testen/los")
    assert page.status_code == 200 and addr in page.text and "Gleich ausprobieren" in page.text
    # Zweite Anmeldung mit derselben Nummer: kein zweiter Zugang, Login-Link statt Übernahme
    r2 = _client().post("/testen", data={"owner_name": "X", "business_name": "Y", "whatsapp": "+49 171 2345678"})
    assert r2.status_code == 200 and 'id="already"' in r2.text
    with new_session() as s:
        assert s.scalar(select(Business.id).where(Business.id > 0).order_by(Business.id.desc())) == 1


def test_signup_validates(fresh_db, inbound):
    c = _client()
    r = c.post("/testen", data={"owner_name": "A", "business_name": "B", "whatsapp": "02161 4455"})
    assert "Festnetznummer" in r.text
    r = c.post("/testen", data={"owner_name": "A", "business_name": "B", "whatsapp": "0171 2345678", "email": "kaputt"})
    assert "Mailadresse" in r.text
    with new_session() as s:
        assert s.scalars(select(Business)).first() is None


def test_slug_and_placeholder():
    assert trial.slugify("Müller & Söhne Haustechnik GmbH") == "mueller-soehne-haust"
    assert trial.slugify("Bäder Ölmann GmbH") == "baeder-oelmann"
    assert trial.slugify("!!!") == "betrieb"


# --- Weitergeleiteter Kopfblock ---------------------------------------------------------------------

@pytest.mark.parametrize("text,email,name,day", [
    ("-----Ursprüngliche Nachricht-----\nVon: Thomas <info@m.de>\nGesendet: Dienstag, 27. Oktober 2026 16:40\nAn: Familie Brandt <brandt@web.de>\nBetreff: Angebot\n\nText",
     "brandt@web.de", "Familie Brandt", 27),
    ("---------- Forwarded message ---------\nFrom: Thomas <info@m.de>\nDate: Tue, 27 Oct 2026 at 16:40\nSubject: Angebot\nTo: <brandt@web.de>\n\nHallo",
     "brandt@web.de", "", 27),
    ("Anfang der weitergeleiteten Nachricht:\n\nVon: Thomas <info@m.de>\nBetreff: Angebot\nDatum: 26. Oktober 2026 um 09:12:40 MEZ\nAn: \"Lea Brunner\" <lea@brunner.ch>\n\nText",
     "lea@brunner.ch", "Lea Brunner", 26),
    ("-------- Weitergeleitete Nachricht --------\nBetreff: Angebot\nDatum: Mon, 26 Oct 2026 09:12:40 +0100\nVon: Thomas <info@m.de>\nAn: kunde@firma.de\n\nText",
     "kunde@firma.de", "", 26),
    ("Gesendet: Montag, 26. Oktober 2026 um 09:12 Uhr\nVon: \"Thomas\" <info@m.de>\nGesendet an: \"Herr Lange\" <lange@gmx.de>\nBetreff: Angebot\n\nText",
     "lange@gmx.de", "Herr Lange", 26),
    ("-----Original Message-----\nFrom: Thomas [mailto:info@m.de]\nSent: 26.10.2026 09:12\nTo: Weber, Peter <p.weber@web.de<mailto:p.weber@web.de>>\nSubject: Angebot\n",
     "p.weber@web.de", "Weber", 26),
])
def test_forward_header_formats(text, email, name, day):
    fh = inbound_mail.parse_forward_header(text, {"info@m.de"})
    assert fh is not None
    assert fh.to_email == email
    if name:
        assert name in fh.to_name
    assert fh.date is not None and fh.date.astimezone().day in (day, day - 1, day + 1)
    assert fh.subject == "Angebot"


# --- Eingang per Webhook ------------------------------------------------------------------------------

def test_webhook_forward_with_pdf(fresh_db, inbound):
    with new_session() as s:
        b = _trial(s, window=False)
        addr = b.inbound_address
    r = _post(_client(), _outlook_forward(addr), rcpt=addr)
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "created"
    with new_session() as s:
        d = s.scalars(select(Document)).one()
        assert d.account_id is None and d.source == "forward"
        assert d.customer_email == CUSTOMER and "Brandt" in d.customer_name
        assert d.document_number == "A-2026-118" and d.amount_gross == 3724.70
        assert d.subject == "Angebot Gastherme"
        assert d.sent_at.astimezone(timezone.utc).date().isoformat() == "2026-10-27"  # aus dem Kopfblock, nicht der Weiterleitung
        assert len(d.followups) == 3
        assert s.scalars(select(InboundMail)).one().result == "created"
    # Dieselbe Weiterleitung noch einmal: keine Dublette
    r = _post(_client(), _outlook_forward(addr), rcpt=addr)
    assert r.json()["result"] == "duplicate"
    with new_session() as s:
        assert len(s.scalars(select(Document)).all()) == 1


def test_webhook_cc_case_customer_is_to(fresh_db, inbound):
    with new_session() as s:
        b = _trial(s, email="info@mueller-shk.de", window=False)
        addr = b.inbound_address
    raw = _mail("Thomas Müller <info@mueller-shk.de>", "Familie Brandt <brandt@web.de>", "Ihr Angebot Gastherme", "Anbei unser Angebot.", _offer_pdf(), cc=addr,
                extra={"Authentication-Results": "mx.example; dkim=pass; spf=pass"})
    r = _post(_client(), raw)
    assert r.json()["result"] == "created"
    with new_session() as s:
        d = s.scalars(select(Document)).one()
        assert d.source == "cc" and d.customer_email == CUSTOMER and d.message_id
        assert "dkim=pass" in d.inbound_auth
        assert d.cc_json == ""  # unsere Adresse ist kein CC des Kunden


def test_webhook_forward_from_stranger_is_unverified(fresh_db, inbound):
    with new_session() as s:
        b = _trial(s, email="info@mueller-shk.de", window=False)
        addr = b.inbound_address
    r = _post(_client(), _outlook_forward(addr, frm="Fremder <fremd@evil.example>"))
    assert r.json()["result"] == "created"
    with new_session() as s:
        d = s.scalars(select(Document)).one()
        assert d.source == "forward_unverified" and d.inbound_from == "fremd@evil.example"
    with new_session() as s:
        b = s.scalars(select(Business)).one()
    c = _client()
    from locadas.web import auth

    c.cookies.set(auth.COOKIE, auth.session_token(b.id))
    assert "Absender nicht bestätigt" in c.get("/dashboard?filter=all").text


def test_webhook_rejects_unknown_wrong_secret_oversized(fresh_db, inbound, monkeypatch):
    with new_session() as s:
        b = _trial(s, window=False)
        addr = b.inbound_address
    c = _client()
    assert _post(c, _outlook_forward("niemand-abc123@angebot.locadas.de")).status_code == 404
    assert _post(c, _outlook_forward("info@locadas.de")).status_code == 404
    assert _post(c, _outlook_forward(addr), secret="falsch").status_code == 403
    monkeypatch.setattr(settings, "inbound_webhook_secret", None)
    assert _post(c, _outlook_forward(addr), secret="").status_code == 403
    monkeypatch.setattr(settings, "inbound_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "inbound_max_bytes", 2000)
    assert _post(c, _outlook_forward(addr)).status_code == 413
    with new_session() as s:
        assert s.scalars(select(Document)).first() is None


def test_rate_limit_per_address(fresh_db, inbound, monkeypatch):
    monkeypatch.setattr(settings, "inbound_per_hour", 2)
    with new_session() as s:
        b = _trial(s, window=False)
        addr = b.inbound_address
    c = _client()
    results = [_post(c, _mail("x@y.de", addr, f"WG: Notiz {i}", "ohne PDF")).json()["result"] for i in range(3)]
    assert results == ["no_document", "no_document", "rate_limited"]


# --- SMTP-Empfänger ------------------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_smtp_listener_accepts_only_inbound_domain(fresh_db, inbound):
    from locadas.mail import inbound_smtp

    with new_session() as s:
        b = _trial(s, window=False)
        addr = b.inbound_address
    ctrl = inbound_smtp.start(port=_free_port(), host="127.0.0.1")
    try:
        with smtplib.SMTP(ctrl.hostname, ctrl.port, timeout=10) as smtp:
            # Kein Relay: fremde Domain und unbekannte Adresse werden schon bei RCPT abgelehnt
            with pytest.raises(smtplib.SMTPRecipientsRefused):
                smtp.sendmail("x@y.de", ["opfer@gmail.com"], b"Subject: spam\r\n\r\nspam")
            with pytest.raises(smtplib.SMTPRecipientsRefused):
                smtp.sendmail("x@y.de", ["gibtsnicht-aaaaaa@angebot.locadas.de"], b"Subject: x\r\n\r\nx")
            raw = _outlook_forward(addr).replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")  # SMTP spricht CRLF
            refused = smtp.sendmail("info@mueller-shk.de", [addr], raw)
            assert refused == {}
    finally:
        ctrl.stop()
    with new_session() as s:
        d = s.scalars(select(Document)).one()
        assert d.customer_email == CUSTOMER and d.business_id == b.id


# --- WhatsApp: PDF und Foto ------------------------------------------------------------------------------

class Media:
    def __init__(self, data: bytes, mime: str):
        self.data, self.mime, self.urls = data, mime, []

    def __call__(self, url, token, limit=None):
        assert token == "tok"
        self.urls.append(url)
        if url.endswith("/MEDIA1"):
            return json.dumps({"url": "https://lookaside.example/media1", "mime_type": self.mime, "file_size": len(self.data)}).encode()
        assert url == "https://lookaside.example/media1"
        return self.data


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_whatsapp_document_inbound(fresh_db, inbound, graph, monkeypatch):
    media = Media(_offer_pdf(), "application/pdf")
    monkeypatch.setattr(wa, "http_get", media)
    with new_session() as s:
        _trial(s)
    r = _hook(_client(), {"from": NUM, "id": "wamid.doc1", "type": "document",
                          "document": {"id": "MEDIA1", "mime_type": "application/pdf", "filename": "Angebot_A-2026-118.pdf", "caption": "brandt@web.de"}})
    assert r.status_code == 200
    assert media.urls[0] == "https://graph.facebook.com/v21.0/MEDIA1"
    with new_session() as s:
        d = s.scalars(select(Document)).one()
        assert d.source == "whatsapp" and d.amount_gross == 3724.70 and d.customer_email == CUSTOMER
        b = d.business
        assert b.trial_aha_at is not None
    texts = graph.texts()
    aha = [t for t in texts if "Erkannt: Angebot A-2026-118" in t]
    assert len(aha) == 1 and "Du musst nichts tun" in aha[0] and "3.724,70 €" in aha[0]
    btns = [c for c in graph.calls if c["type"] == "interactive" and c["interactive"]["type"] == "button"]
    assert [x["reply"]["title"] for x in btns[-1]["interactive"]["action"]["buttons"]] == ["Passt", "Falsch erkannt"]
    # Derselbe Webhook noch einmal (Meta wiederholt): nichts Neues
    _hook(_client(), {"from": NUM, "id": "wamid.doc1", "type": "document", "document": {"id": "MEDIA1", "mime_type": "application/pdf"}})
    with new_session() as s:
        assert len(s.scalars(select(Document)).all()) == 1


def test_whatsapp_image_inbound_uses_vision(fresh_db, inbound, graph, monkeypatch):
    monkeypatch.setattr(wa, "http_get", Media(_png(), "image/jpeg"))
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    seen = {}

    def fake_claude(content):
        seen["content"] = content
        return json.dumps({"is_document": True, "kind": "offer", "customer_name": "Herr Dieter Lange", "customer_phone": "0172 4455667",
                           "document_number": "17/2026", "amount_gross": 1784.0, "currency": "EUR", "doc_term": "Angebot", "confidence": 0.8})

    monkeypatch.setattr(vision, "_call_claude", fake_claude)
    with new_session() as s:
        _trial(s)
    _hook(_client(), {"from": NUM, "id": "wamid.img1", "type": "image", "image": {"id": "MEDIA1", "mime_type": "image/jpeg"}})
    assert seen["content"][0]["type"] == "image" and seen["content"][0]["source"]["media_type"] == "image/png"
    with new_session() as s:
        d = s.scalars(select(Document)).one()
        assert d.document_number == "17/2026" and d.amount_gross == 1784.0 and d.extraction_source == "claude_vision"
        assert d.customer_mobile == "491724455667" and d.customer_email == ""
        assert open(d.pdf_path, "rb").read(4) == b"%PDF"  # Foto als PDF abgelegt (Kunden-Link geht wie gewohnt)
    assert any("Erkannt: Angebot 17/2026 an Herr Dieter Lange über 1.784,00 €" in t for t in graph.texts())


def test_whatsapp_image_without_claude_asks_for_pdf(fresh_db, inbound, graph, monkeypatch):
    monkeypatch.setattr(wa, "http_get", Media(_png(), "image/jpeg"))
    with new_session() as s:
        _trial(s)
    _hook(_client(), {"from": NUM, "id": "wamid.img2", "type": "image", "image": {"id": "MEDIA1", "mime_type": "image/jpeg"}})
    with new_session() as s:
        assert s.scalars(select(Document)).first() is None
    assert any("als PDF" in t for t in graph.texts())


# --- Tipps ohne Postfach ------------------------------------------------------------------------------------

def test_mailto_encoding():
    body = "Guten Tag Frau Müller,\nwie geht's? 50% & mehr\n\nViele Grüße"
    link = trial.mailto_link("kunde+test@web.de", "Angebot Bad & Küche", body)
    assert link.startswith("mailto:kunde+test@web.de?subject=")
    q = link.split("?", 1)[1]
    assert "Angebot%20Bad%20%26%20K%C3%BCche" in q
    assert "%0D%0A" in q and "\n" not in link and " " not in link and "+" not in q
    assert "%26%20mehr" in q and "50%25" in q
    assert unquote(q.split("&body=", 1)[1]).replace("\r\n", "\n") == body
    long = trial.mailto_link("a@b.de", "x", ("Satz eins ist hier. " * 40 + "\n\n") * 5)
    assert len(unquote(long.split("&body=", 1)[1])) < 1800


def _doc(s, b, mobile="", email="", name="Familie Brandt", number="A-1", days_ago=4) -> Document:
    d = Document(business_id=b.id, account_id=None, kind=DocKind.OFFER, status=DocStatus.OPEN, subject="Angebot", sent_at=NOW - timedelta(days=days_ago),
                 customer_name=name, customer_email=email, customer_mobile=mobile, document_number=number, amount_gross=1000.0, source="forward")
    s.add(d)
    s.flush()
    fu_engine.schedule_followups(s, d, NOW - timedelta(days=days_ago))
    s.commit()
    return d


def test_trial_taps_mobile_email_neither(fresh_db, inbound):
    with new_session() as s:
        b = _trial(s, window=False)
        d1 = _doc(s, b, mobile="491724455667", number="A-1")
        d2 = _doc(s, b, email=CUSTOMER, number="A-2")
        d3 = _doc(s, b, name="Herr Lange", number="A-3")
        stats = fu_engine.process_due(s, NOW)
        assert stats["tap"] == 3 and stats["sent"] == 0
        f1, f2, f3 = (s.get(Document, d.id).followups[0] for d in (d1, d2, d3))
        assert (f1.status, f1.tap_kind) == (FollowUpStatus.AWAITING_TAP, "wa")
        assert (f2.status, f2.tap_kind) == (FollowUpStatus.AWAITING_TAP, "mailto") and f2.subject and f2.body
        assert (f3.status, f3.tap_kind) == (FollowUpStatus.AWAITING_TAP, "manual")
        tokens = f1.tap_token, f2.tap_token, f3.tap_token
    c = _client()
    r = c.get(f"/t/{tokens[0]}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://wa.me/491724455667?text=")
    r = c.get(f"/t/{tokens[1]}", headers={"user-agent": "Mozilla/5.0 (iPhone)"})  # Seite statt 302 (In-App-Browser blocken mailto:)
    assert r.status_code == 200 and f'href="mailto:{CUSTOMER}?subject=' in r.text and "Mail öffnen" in r.text and "Text kopieren" in r.text
    r = c.get(f"/t/{tokens[2]}", follow_redirects=False)
    assert r.status_code == 200 and "Wem sollen wir schreiben?" in r.text and "Text kopieren" in r.text
    # Kontakt nachtragen: aus dem Text-Tipp wird ein Mail-Tipp
    r = c.post(f"/t/{tokens[2]}/kontakt", data={"contact": "lange@gmx.de"}, follow_redirects=False)
    assert r.status_code == 303
    r = c.get(f"/t/{tokens[2]}")
    assert r.status_code == 200 and 'href="mailto:lange@gmx.de?' in r.text
    with new_session() as s:
        f2 = s.scalar(select(FollowUp).where(FollowUp.tap_token == tokens[1]))
        assert f2.tapped_at is not None  # Tipp gemerkt
    # Bestätigen wie bei WhatsApp: Gesendet → SENT, per Mail-Tipp
    r = c.post(f"/w/{tokens[1]}/sent")
    assert r.status_code == 200
    with new_session() as s:
        f2 = s.scalar(select(FollowUp).where(FollowUp.tap_token == tokens[1]))
        assert f2.status == FollowUpStatus.SENT and f2.sent_via == "mail_tap"


def test_trial_tap_notifications(fresh_db, inbound, graph):
    with new_session() as s:
        b = _trial(s)
        b.whatsapp_last_inbound_at = NOW - timedelta(minutes=5)  # 24h-Fenster offen zum Zeitpunkt des Durchlaufs
        _doc(s, b, email=CUSTOMER, number="A-2")
        _doc(s, b, name="Herr Lange", number="A-3")
        fu_engine.process_due(s, NOW)
    ctas = [c["interactive"] for c in graph.calls if c["type"] == "interactive" and c["interactive"]["type"] == "cta_url"]
    labels = sorted(i["action"]["parameters"]["display_text"] for i in ctas)
    assert labels == ["Mail öffnen", "Text kopieren"]
    assert any("Mir fehlt die Handynummer oder Mailadresse von Herr Lange" in i["body"]["text"] for i in ctas)


def test_owner_replies_contact_in_whatsapp(fresh_db, inbound, graph):
    with new_session() as s:
        b = _trial(s)
        d = _doc(s, b, name="Herr Lange", number="A-3")
        fu_engine.process_due(s, NOW)
        token = s.get(Document, d.id).followups[0].tap_token
    _hook(_client(), {"from": NUM, "id": "wamid.t1", "type": "text", "text": {"body": "0172 4455667"}})
    with new_session() as s:
        d = s.get(Document, d.id)
        assert d.customer_mobile == "491724455667"
        assert d.followups[0].tap_kind == "wa"
    assert any("Danke, eingetragen bei Herr Lange" in t and f"/t/{token}" in t for t in graph.texts())


# --- Aha, älteres Angebot, Upgrade -----------------------------------------------------------------------

def test_aha_once_and_older_offer_gets_first_tap(fresh_db, inbound, graph):
    with new_session() as s:
        b = _trial(s)
        addr = b.inbound_address
    c = _client()
    _post(c, _outlook_forward(addr, sent=(utcnow() - timedelta(hours=2)).strftime("%d.%m.%Y %H:%M")))
    second = _mail("Thomas <t@m.de>", addr, "WG: Angebot", "-----Ursprüngliche Nachricht-----\nVon: t@m.de\nGesendet: 01.10.2026 10:00\nAn: Herr Lange <lange@gmx.de>\nBetreff: Angebot 2\n",
                   make_pdf(["Mueller Haustechnik", "Angebot Nr. A-2026-200", "Herr Lange", "Gesamtbetrag 990,00 EUR"]))
    _post(c, second)
    texts = graph.texts()
    assert sum("Du musst nichts tun" in t for t in texts) == 1
    assert any("Erkannt: Angebot A-2026-200 an Herr Lange über 990,00 €" in t and "schon ein paar Tage her" in t for t in texts)
    with new_session() as s:
        old = s.scalars(select(Document).where(Document.document_number == "A-2026-200")).one()
        f = old.followups[0]
        assert f.status == FollowUpStatus.AWAITING_TAP and f.tap_kind == "mailto" and f.tap_notified_at is not None
    assert any(c_["type"] == "interactive" and c_["interactive"]["type"] == "cta_url" for c_ in graph.calls)


def test_upgrade_prompt_once_after_sent(fresh_db, inbound, graph):
    from locadas.notify import tokens

    with new_session() as s:
        b = _trial(s)
        _doc(s, b, email=CUSTOMER, number="A-2")
        _doc(s, b, mobile="491724455667", number="A-1")
        fu_engine.process_due(s, NOW)
        fus = s.scalars(select(FollowUp).where(FollowUp.status == FollowUpStatus.AWAITING_TAP)).all()
        ids = [f.id for f in fus]
    c = _client()
    _press(c, tokens.payload("tsent", ids[0]))
    _press(c, tokens.payload("tsent", ids[1]))
    upgrades = [x for x in graph.calls if x["type"] == "interactive" and x["interactive"]["type"] == "cta_url"
                and x["interactive"]["action"]["parameters"]["url"] == settings.calendly_url]
    assert len(upgrades) == 1
    body = upgrades[0]["interactive"]["body"]["text"]
    assert "Soll das künftig automatisch laufen, ohne Weiterleiten? 15 Minuten Einrichtung mit Julius, kostenlos." in body
    assert "/verbinden?b=" in body
    with new_session() as s:
        assert s.scalars(select(Business)).one().upgrade_prompt_at is not None


def test_upgrade_after_three_documents(fresh_db, inbound, graph):
    with new_session() as s:
        b = _trial(s)
        addr = b.inbound_address
    c = _client()
    for i in range(3):
        raw = _mail("t@m.de", addr, f"WG: Angebot {i}", f"-----Ursprüngliche Nachricht-----\nVon: t@m.de\nAn: k{i}@web.de\nBetreff: Angebot {i}\n",
                    make_pdf(["Mueller", f"Angebot Nr. A-{i}00", f"Kunde {i}", "Gesamtbetrag 500,00 EUR"]))
        assert _post(c, raw).json()["result"] == "created"
    ups = [x for x in graph.calls if x["type"] == "interactive" and x["interactive"]["type"] == "cta_url"]
    assert len(ups) == 1


def test_wrong_button_sends_login_link_to_document(fresh_db, inbound, graph):
    from locadas.notify import tokens

    with new_session() as s:
        b = _trial(s)
        d = _doc(s, b, email=CUSTOMER)
    _press(_client(), tokens.payload("wrong", d.id))
    link = re.search(r"http://testserver(/login/\S+)", graph.texts()[-1]).group(1)
    r = _client().get(link, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/dokument/{d.id}"


# --- Grenzen, Aufräumen ------------------------------------------------------------------------------------

def test_trial_limit(fresh_db, inbound, graph, monkeypatch):
    monkeypatch.setattr(settings, "trial_max_docs", 2)
    with new_session() as s:
        b = _trial(s)
        addr = b.inbound_address
    c = _client()
    res = []
    for i in range(3):
        raw = _mail("t@m.de", addr, f"WG: Angebot {i}", f"An: k{i}@web.de\n", make_pdf(["M", f"Angebot Nr. B-{i}00", "Gesamtbetrag 100,00 EUR"]))
        res.append(_post(c, raw).json()["result"])
    assert res == ["created", "created", "limit"]
    assert any("Testgrenze erreicht" in t for t in graph.texts())
    with new_session() as s:
        assert len(s.scalars(select(Document)).all()) == 2
        assert s.scalars(select(Business)).one().trial_limit_at is not None


def test_cleanup_job_deletes_inactive_trials(fresh_db, inbound):
    with new_session() as s:
        old = _trial(s, number="491711111111", window=False)
        d = _doc(s, old, email=CUSTOMER)
        pdf = settings.pdf_dir / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        d.pdf_path = str(pdf)
        long_ago = utcnow() - timedelta(days=61)
        old.created_at = old.trial_active_at = long_ago
        old.last_login_at = None
        fresh = _trial(s, number="491722222222", window=False)
        connected = Business(name="C", email="c@c.de", created_at=long_ago)
        s.add(connected)
        s.commit()
        old_id, fresh_id = old.id, fresh.id
        assert trial.cleanup(s) == 1
    with new_session() as s:
        assert s.get(Business, old_id) is None and s.get(Business, fresh_id) is not None
        assert s.scalars(select(Business).where(Business.email == "c@c.de")).one()
        assert s.scalars(select(Document)).first() is None and s.scalars(select(FollowUp)).first() is None
    assert not pdf.exists()


# --- Umwandeln beim Verbinden ------------------------------------------------------------------------------

def test_conversion_keeps_documents(fresh_db, inbound, imap, smtp):
    with new_session() as s:
        b = _trial(s, window=False)
        d = _doc(s, b, email=CUSTOMER)
        fu_engine.process_due(s, NOW)
        assert s.get(Document, d.id).followups[0].status == FollowUpStatus.AWAITING_TAP
        token = trial.connect_token(b)
        bid, did = b.id, d.id
    c = _client()
    page = c.get(f"/verbinden?b={token}")
    assert "Müller Haustechnik GmbH" in page.text and "Termin buchen" in page.text and 'name="trial_token"' in page.text
    r = c.post("/verbinden", data={
        "business_name": "Müller Haustechnik GmbH", "owner_name": "Thomas Müller", "email": USER, "password": PASSWORD,
        "imap_host": "127.0.0.1", "imap_port": str(imap.port), "smtp_host": "127.0.0.1", "smtp_port": str(smtp.port),
        "consent": "1", "avv": "1", "trial_token": token,
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    with new_session() as s:
        assert len(s.scalars(select(Business)).all()) == 1
        b = s.get(Business, bid)
        assert b.mode == "connected" and b.email == USER and b.followup_channel == "mail"
        assert b.whatsapp_number == NUM and b.inbound_address
        d = s.get(Document, did)
        assert d.account_id == b.accounts[0].id
        f = d.followups[0]
        assert f.status == FollowUpStatus.SCHEDULED and f.tap_state == "cancelled"


def test_connected_business_cc_dedup_with_mailbox(fresh_db, inbound, imap, smtp):
    from locadas.engine.scanner import process_sent_mail
    from locadas.mail.imap import parse_message

    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        b.inbound_address = trial.new_inbound_address(s, b.name)
        s.commit()
        raw = _mail(f"Müller <{USER}>", "Familie Brandt <brandt@web.de>", "Angebot Gastherme", "Anbei.", _offer_pdf(), cc=b.inbound_address)
        aid = account.id
    assert _post(_client(), raw).json()["result"] == "created"
    with new_session() as s:
        d = s.scalars(select(Document)).one()
        assert d.account_id == aid and d.source == "cc" and not d.needs_review
        account = s.get(type(d.account), aid)
        assert process_sent_mail(s, account, parse_message(1, raw)) is None
        assert len(s.scalars(select(Document)).all()) == 1


# --- Migration ---------------------------------------------------------------------------------------------

def test_migration_makes_account_id_nullable(tmp_path):
    from sqlalchemy import MetaData, create_engine, inspect, text

    from locadas import db as dbmod

    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = create_engine(url)
    md = MetaData()
    for t in dbmod.Base.metadata.sorted_tables:
        t.to_metadata(md)
    md.tables["documents"].c.account_id.nullable = False
    md.create_all(eng, tables=[t for t in md.sorted_tables if t.name != "documents"])
    # documents wie vor dieser Änderung: account_id NOT NULL, ohne die neuen Herkunfts-Spalten
    from sqlalchemy.schema import CreateTable

    ddl = str(CreateTable(md.tables["documents"]).compile(eng))
    ddl = "\n".join(ln for ln in ddl.splitlines() if not re.match(r"\s*(source|inbound_from|inbound_auth) ", ln))
    with eng.begin() as conn:
        conn.execute(text(ddl))
    md.tables["documents"]._columns.remove(md.tables["documents"].c.source)
    md.tables["documents"]._columns.remove(md.tables["documents"].c.inbound_from)
    md.tables["documents"]._columns.remove(md.tables["documents"].c.inbound_auth)
    def insert(conn, table, **vals):
        import enum as _enum

        from sqlalchemy import Boolean, Enum as SAEnum, Float, Integer

        t = md.tables[table]
        row = dict(vals)
        for col in t.c:
            if col.name in row or col.nullable or col.primary_key:
                continue
            d = col.default.arg if col.default is not None and col.default.is_scalar else None
            if d is not None:
                row[col.name] = d.name if isinstance(d, _enum.Enum) else d
            elif isinstance(col.type, SAEnum):
                row[col.name] = col.type.enums[0]
            elif isinstance(col.type, (Integer, Float, Boolean)):
                row[col.name] = 0
            elif "DateTime" in type(col.type).__name__ or "UTC" in type(col.type).__name__:
                row[col.name] = "2026-01-01 00:00:00"
            else:
                row[col.name] = ""
        names = ", ".join(row)
        conn.execute(text(f"INSERT INTO {table} ({names}) VALUES ({', '.join(':' + k for k in row)})"), row)

    with eng.begin() as conn:
        insert(conn, "businesses", id=1, name="B", email="b@b.de")
        insert(conn, "mail_accounts", id=1, business_id=1, provider="x", email="b@b.de", username="b", secret_enc="s", imap_host="i", smtp_host="s", status="ACTIVE")
        insert(conn, "documents", id=7, business_id=1, account_id=1, kind="OFFER", status="OPEN", customer_email="k@k.de", document_number="A-1", outcome_token="o", view_token="v")
        insert(conn, "followups", id=3, document_id=7, step=1, due_at="2026-01-04 00:00:00", status="SCHEDULED")
    eng.dispose()
    try:
        dbmod.reset_engine_for_tests(url)
        cols = {c["name"]: c for c in inspect(dbmod.get_engine()).get_columns("documents")}
        assert cols["account_id"]["nullable"] is True
        with new_session() as s:
            d = s.get(Document, 7)
            assert d.account_id == 1 and d.source == "mailbox" and d.followups[0].id == 3
            s.add(Document(business_id=1, account_id=None, kind=DocKind.OFFER, sent_at=utcnow()))
            s.commit()
        with dbmod.get_engine().connect() as conn:
            fks = conn.execute(text("PRAGMA foreign_key_list(followups)")).fetchall()
            assert any(r[2] == "documents" for r in fks)
            assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
            idx = {r[1] for r in conn.execute(text("PRAGMA index_list(documents)")).fetchall()}
            assert "ix_documents_account_id" in idx
    finally:
        dbmod.reset_engine_for_tests(f"sqlite:///{tmp_path / 'after.db'}")


# --- Landing --------------------------------------------------------------------------------------------------

def test_landing_has_trial_cta(fresh_db):
    r = _client().get("/")
    assert "Du schickst das Angebot." in r.text and "bis der Kunde antwortet." in r.text
    assert "Mehr Aufträge aus denselben Angeboten. Kostenlos." in r.text
    assert 'href="/testen"' in r.text and "Ohne Anmeldung testen" in r.text and 'href="/verbinden"' in r.text
    assert "Leite uns dein letztes Angebot weiter." in r.text
