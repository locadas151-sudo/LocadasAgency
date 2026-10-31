"""Nach Review und Simulation: Rechnungen nie peinlich, sichere Links, kein Doppelversand, ehrliche Texte, Timing, Kosten."""
from __future__ import annotations

import email
import email.policy
import json
import ssl
import struct
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from locadas import holidays
from locadas.config import DEFAULT_SECRET_KEY, Settings, check_startup, settings
from locadas.db import Business, DocKind, DocStatus, Document, Event, FollowUp, FollowUpStatus, MailAccount, SeenMessage, new_session
from locadas.engine import followups as fu
from locadas.engine import matcher, personalize as pz, texts
from locadas.engine.absence import parse_return_date
from locadas.engine.scanner import scan_account
from locadas.extract import fields as fx
from locadas.mail import autoconfig, connect as conn
from locadas.mail.imap import ImapClient, MailConnectError, tls_context
from locadas.mail.smtp import SmtpClient
from locadas.web import auth
from tests.conftest import PASSWORD, USER, click
from tests.fakemail import make_mail, make_pdf
from tests.test_e2e import WEEKDAY_10, _seed_sent, _setup

BERLIN = ZoneInfo("Europe/Berlin")


def _berlin(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=BERLIN).astimezone(timezone.utc)


def _msgs(smtp, to=None):
    out = []
    for _, rcpts, raw in smtp.messages:
        if to is None or rcpts == to or to in rcpts:
            out.append((rcpts, email.message_from_bytes(raw, policy=email.policy.default)))
    return out


def _scanned(s, imap, smtp):
    _seed_sent(imap)
    b, account = _setup(s, imap, smtp)
    scan_account(s, account)
    offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
    inv = s.scalars(select(Document).where(Document.document_number == "R-2026-301")).one()
    return b, account, offer, inv


def _app():
    from locadas.main import app
    return app


# --- 1. Rechnungen nie automatisch -------------------------------------------------------

def test_needs_approval_rules():
    b = SimpleNamespace(needs_approval=False, threshold=10000.0)
    offer = SimpleNamespace(needs_review=False, kind=DocKind.OFFER, amount_gross=3000.0)
    assert not fu.needs_approval(b, offer)
    assert fu.needs_approval(b, SimpleNamespace(needs_review=False, kind=DocKind.OFFER, amount_gross=12000.0))
    assert fu.needs_approval(b, SimpleNamespace(needs_review=False, kind=DocKind.INVOICE, amount_gross=50.0))
    assert fu.needs_approval(SimpleNamespace(needs_approval=True, threshold=10000.0), offer)
    assert fu.needs_approval(b, SimpleNamespace(needs_review=True, kind=DocKind.OFFER, amount_gross=10.0))


def test_invoice_question_paid_sends_nothing(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        inv.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        stats = fu.process_due(s, now=WEEKDAY_10)
        assert stats["approval"] == 1 and stats["sent"] == 0
        assert s.get(Document, inv.id).followups[0].status == FollowUpStatus.AWAITING_APPROVAL
        f = s.get(Document, inv.id).followups[0]
        token, doc_id = f.approval_token, inv.id
    r = click(TestClient(_app()), f"/f/{token}/done")
    assert r.status_code == 200 and "Bezahlt" in r.text
    with new_session() as s:
        d = s.get(Document, doc_id)
        assert d.status == DocStatus.PAID and d.followups_sent == 0
        fu.process_due(s, now=WEEKDAY_10 + timedelta(days=30))
    assert _msgs(smtp, "p.weber@web.de") == []  # nie eine Mail an den Rechnungskunden


# --- 2. „Schon bezahlt“ im Dokument -----------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Rechnungsbetrag 224,91 EUR\nBetrag dankend erhalten.",
    "Gesamt 99,00 EUR. Bar bezahlt am 30.10.2026",
    "Zahlungsart: EC-Karte",
    "Der Betrag wurde bereits bezahlt.",
    "Der Rechnungsbetrag wird per SEPA-Lastschrift von Ihrem Konto eingezogen. Mandatsreferenz M-77",
    "Der Betrag wird per Lastschrift eingezogen.",
    "Per PayPal bezahlt am 29.10.",
])
def test_paid_markers(text):
    assert fx.paid_marker(text)


@pytest.mark.parametrize("text", [
    "Bitte überweisen Sie den Betrag innerhalb von 14 Tagen.",
    "Sie können per Überweisung, PayPal oder EC-Karte zahlen.",
    "Barrierefreier Umbau Bad, Rechnungsbetrag 4.200,00 EUR",
])
def test_no_paid_marker(text):
    assert fx.paid_marker(text) == ""


def test_paid_invoice_is_created_paid_without_followups(fresh_db, imap, smtp):
    pdf = make_pdf(["Mueller Haustechnik GmbH", "Rechnung Nr. R-2026-400", "Herr Kurt Bar", "Notdienst 1 St 150,00 EUR",
                    "Rechnungsbetrag 178,50 EUR", "Betrag in bar dankend erhalten."])
    imap.store.append("Gesendet", make_mail(USER, "k.bar@web.de", "Ihre Rechnung R-2026-400", "Anbei.", ("Rechnung_R-2026-400.pdf", pdf)))
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        scan_account(s, account)
        d = s.scalars(select(Document).where(Document.document_number == "R-2026-400")).one()
        assert d.status == DocStatus.PAID and d.followups == [] and "bezahlt" in d.close_reason.lower()
        ev = s.scalars(select(Event).where(Event.document_id == d.id, Event.kind == "paid")).one()
        assert "erinnere hier nicht" in ev.text


# --- 3. IBAN nur aus dem eigenen Bankblock ----------------------------------------------------

OWN_IBAN = "DE89 3704 0044 0532 0130 00"
CUSTOMER_IBAN = "DE02 1203 0000 0000 2020 51"


def test_iban_from_own_footer_only():
    assert fx.iban_valid(OWN_IBAN) and fx.iban_valid(CUSTOMER_IBAN)
    own = f"Rechnung Nr. 5\nSumme 100,00 EUR\n\nMüller Haustechnik GmbH · Bankverbindung: Sparkasse Aachen · IBAN: {OWN_IBAN} · BIC COBADEFFXXX"
    assert fx.find_iban(own, ("Müller Haustechnik GmbH",)) == OWN_IBAN
    holder_ok = f"Kontoinhaber: Thomas Müller\nIBAN {OWN_IBAN}"
    assert fx.find_iban(holder_ok, ("Müller Haustechnik GmbH", "Thomas Müller")) == OWN_IBAN


def test_iban_never_from_customer_mandate():
    text = f"Ihre Bankverbindung für den Einzug:\nIBAN {CUSTOMER_IBAN}\n\nUnsere Bankverbindung: IBAN {OWN_IBAN}"
    # Lastschrift-Hinweis fehlt, Kunden-Block wird übersprungen, eigener genommen
    assert fx.find_iban(text, ("Müller",)) == OWN_IBAN
    mandate = f"SEPA-Lastschriftmandat\nKontoinhaber: Peter Weber\nIBAN {CUSTOMER_IBAN}\nMandatsreferenz 4711\n\nBankverbindung IBAN {OWN_IBAN}"
    assert fx.find_iban(mandate, ("Müller",)) == ""  # Lastschrift: gar keine IBAN, der Kunde überweist nicht
    other_holder = f"Kontoinhaber: Peter Weber\nIBAN {CUSTOMER_IBAN}"
    assert fx.find_iban(other_holder, ("Müller Haustechnik GmbH", "Thomas Müller")) == ""
    unlabeled = f"Referenz {CUSTOMER_IBAN}"
    assert fx.find_iban(unlabeled) == ""
    d = fx.bank_details(mandate, ("Müller",))
    assert d["iban"] == "" and d["account_holder"] == ""


# --- 4. Keine Behauptung über Zahlungseingang -------------------------------------------------

def _inv(tone="freundlich", **kw):
    biz = SimpleNamespace(name="Rohrblitz", owner_name="Kevin Lorenz", phone="", signature="", tone=tone,
                          include_view_link=False, doc_delivery="link", personalize=False)
    base = dict(id=1, kind=DocKind.INVOICE, kind_label="Rechnung", doc_term="", customer_name="Peter Weber", customer_email="p@web.de",
                document_number="R-1", title="Wartung", subject="Rechnung R-1", amount_gross=224.91, currency="EUR",
                sent_at=WEEKDAY_10 - timedelta(days=20), due_date=WEEKDAY_10 - timedelta(days=6), valid_until=None,
                view_token="t", pdf_path="", business=biz, iban="", bic="", bank_name="", account_holder="", ack_at=None, first_opened_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_templates_never_claim_missing_payment():
    for tone in texts.TONES:
        for step in (1, 2, 3):
            body = texts.body_for(_inv(tone), step, WEEKDAY_10)
            low = body.lower()
            assert "zahlungseingang" not in low and "feststellen" not in low and "konnte ich" not in low, (tone, step, body)
            assert "laut meinen unterlagen" in low


def test_personalized_invoice_rejects_payment_claims():
    profile = {"tone": "sie", "greeting": "Guten Tag", "sign_off": "Viele Grüße"}
    ctx = pz.build_context(_inv(), 1, profile, now=WEEKDAY_10)
    assert "Behaupte nie" in pz.build_prompt(ctx) and "Laut meinen Unterlagen" in pz.build_prompt(ctx)
    bad = "Guten Tag Peter Weber,\n\nzu unserer Rechnung R-1 konnte ich bisher keinen Zahlungseingang feststellen. Bei Fragen melden Sie sich gern."
    assert any("Zahlungseingang" in p for p in pz.validate(bad, ctx))
    good = "Guten Tag Peter Weber,\n\nlaut meinen Unterlagen ist die Rechnung R-1 noch offen. Bei Fragen zur Rechnung melden Sie sich gern."
    assert pz.validate(good, ctx) == []


# --- 5. Auftragsbestätigung / Rechnung schließt das Angebot -----------------------------------

def test_order_confirmation_closes_offer_as_won(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        doc_id = offer.id
    ab = make_pdf(["Mueller Haustechnik GmbH", "Auftragsbestätigung AB-12", "zu Angebot A-2026-118", "Gesamt 3.724,70 EUR"])
    imap.store.append("Gesendet", make_mail(USER, "familie.schmidt@gmx.de", "Auftragsbestätigung Gastherme", "Anbei.", ("AB-12.pdf", ab),
                                            date=datetime.now(timezone.utc)))
    with new_session() as s:
        scan_account(s, s.get(MailAccount, account.id))
        d = s.get(Document, doc_id)
        assert d.status == DocStatus.WON and all(f.status == FollowUpStatus.CANCELLED for f in d.followups)
        assert s.scalars(select(Event).where(Event.document_id == doc_id, Event.kind == "won")).first() is not None
        assert s.scalars(select(Document)).all().__len__() == 2  # AB ist kein neues Dokument


def test_invoice_to_same_company_closes_single_offer(fresh_db, imap, smtp):
    offer = make_pdf(["Mueller Haustechnik GmbH", "Angebot Nr. A-77", "Hausverwaltung Rhein GmbH", "Gesamtbetrag 5.000,00 EUR"])
    old = datetime.now(timezone.utc) - timedelta(days=5)
    imap.store.append("Gesendet", make_mail(USER, "m.becker@hv-rhein.de", "Angebot Strang", "Anbei.", ("Angebot_A-77.pdf", offer), date=old))
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        scan_account(s, account)
    invoice = make_pdf(["Mueller Haustechnik GmbH", "Rechnung Nr. R-88", "Hausverwaltung Rhein GmbH", "Rechnungsbetrag 5.000,00 EUR"])
    imap.store.append("Gesendet", make_mail(USER, "buchhaltung@hv-rhein.de", "Rechnung R-88", "Anbei.", ("Rechnung_R-88.pdf", invoice),
                                            date=datetime.now(timezone.utc)))
    with new_session() as s:
        scan_account(s, s.get(MailAccount, account.id))
        o = s.scalars(select(Document).where(Document.document_number == "A-77")).one()
        assert o.status == DocStatus.WON


def test_ambiguous_order_confirmation_closes_nothing(fresh_db, imap, smtp):
    old = datetime.now(timezone.utc) - timedelta(days=5)
    for nr in ("A-1", "A-2"):
        pdf = make_pdf(["Mueller Haustechnik GmbH", f"Angebot Nr. {nr}", "Gesamtbetrag 1.000,00 EUR"])
        imap.store.append("Gesendet", make_mail(USER, "kunde@web.de", f"Angebot {nr}", "Anbei.", (f"Angebot_{nr}.pdf", pdf), date=old))
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        scan_account(s, account)
    imap.store.append("Gesendet", make_mail(USER, "kunde@web.de", "Auftragsbestätigung", "Danke für den Auftrag.", date=datetime.now(timezone.utc)))
    with new_session() as s:
        scan_account(s, s.get(MailAccount, account.id))
        assert {d.status for d in s.scalars(select(Document)).all()} == {DocStatus.OPEN}


# --- 6. Freigabe ab Betrag, „ich gebe jede Mail frei“, ehrliche Texte --------------------------

def test_big_offer_needs_approval_with_full_text(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        b.approval_threshold = 3000  # Angebot hat 3.724,70 €
        offer.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        assert fu.process_due(s, now=WEEKDAY_10)["approval"] == 1
        f = s.get(Document, offer.id).followups[0]
        body = f.body
    [(rcpts, m)] = _msgs(smtp, [USER])
    text = m.get_content().replace("\r\n", "\n")
    assert body and body in text
    assert "Bei Angeboten über 3.000,00 € frage ich vorher" in text


def test_small_offer_runs_automatically_with_preview(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        assert b.review_mode == "auto" and b.threshold == 10000
        offer.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        st = fu.process_due(s, now=WEEKDAY_10)
        assert st["approval"] == 0 and st["announced"] == 1


def test_public_texts_match_reality():
    c = TestClient(_app())
    for path in ("/", "/verbinden"):
        html = c.get(path).text
        assert "ersten fünf" not in html and "ersten 5" not in html
    assert "Noch offen" in c.get("/").text


# --- 7. Links: GET zeigt nur, POST ändert; Ablauf; Abschlussfrage nur einmal -------------------

def test_get_link_changes_nothing_and_post_does(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        token = offer.followups[0].approval_token
        doc_id = offer.id
    c = TestClient(_app())
    r = c.get(f"/f/{token}/stop")
    assert r.status_code == 200 and 'method="post"' in r.text
    with new_session() as s:
        assert s.get(Document, doc_id).status == DocStatus.OPEN
    assert c.post(f"/f/{token}/stop").status_code == 200
    with new_session() as s:
        assert s.get(Document, doc_id).status == DocStatus.REPLIED
    assert c.get(f"/f/{token}/hack").status_code == 404


def test_links_expire_after_60_days(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        f = offer.followups[0]
        f.requested_at = datetime.now(timezone.utc) - timedelta(days=61)
        offer.outcome_asked_at = datetime.now(timezone.utc) - timedelta(days=61)
        s.commit()
        token, otoken, doc_id = f.approval_token, offer.outcome_token, offer.id
    c = TestClient(_app())
    assert c.get(f"/f/{token}/stop").status_code == 410
    assert c.post(f"/f/{token}/stop").status_code == 410
    assert c.post(f"/o/{otoken}/won").status_code == 410
    with new_session() as s:
        assert s.get(Document, doc_id).status == DocStatus.OPEN


def test_outcome_can_be_set_only_once_by_link(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        otoken, doc_id = offer.outcome_token, offer.id
    c = TestClient(_app())
    assert c.get(f"/o/{otoken}/won").status_code == 200
    with new_session() as s:
        assert s.get(Document, doc_id).status == DocStatus.OPEN  # GET allein ändert nichts
    assert "Danke" in c.post(f"/o/{otoken}/won").text
    r = c.post(f"/o/{otoken}/lost")
    assert "Schon notiert" in r.text and "Dashboard" in r.text
    with new_session() as s:
        d = s.get(Document, doc_id)
        assert d.status == DocStatus.WON and all(f.status == FollowUpStatus.CANCELLED for f in d.followups)


# --- 8. Kein Doppelversand, eindeutige gesehene Mails, „Jetzt prüfen“ ------------------------------

def test_atomic_claim_prevents_double_send(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        b.review_mode = "all"
        offer.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        fu.process_due(s, now=WEEKDAY_10)
        fid = offer.followups[0].id
    with new_session() as s1, new_session() as s2:
        f1, f2 = s1.get(FollowUp, fid), s2.get(FollowUp, fid)
        assert f1.status == f2.status == FollowUpStatus.AWAITING_APPROVAL
        assert fu.approve(s1, f1) is True
        s1.commit()
        assert fu.send_followup(s2, f2) is False  # zweiter Klick / zweiter Prozess
    assert len(_msgs(smtp, ["familie.schmidt@gmx.de"])) == 1


def test_stuck_sending_is_not_retried(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        f = offer.followups[0]
        f.status, f.sent_at = FollowUpStatus.SENDING, WEEKDAY_10 - timedelta(hours=2)
        s.commit()
        fu.process_due(s, now=WEEKDAY_10)
        assert s.get(FollowUp, f.id).status == FollowUpStatus.FAILED
    assert _msgs(smtp, ["familie.schmidt@gmx.de"]) == []


def test_seen_messages_unique(fresh_db, imap, smtp):
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        s.add(SeenMessage(account_id=account.id, message_id="<a@b>"))
        s.commit()
        s.add(SeenMessage(account_id=account.id, message_id="<a@b>"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        from locadas.engine.scanner import _mark_seen
        assert _mark_seen(s, s.get(MailAccount, account.id), "<a@b>") is False
        assert _mark_seen(s, s.get(MailAccount, account.id), "<c@d>") is True


def test_process_due_only_for_given_business_and_scan_now_respects_lock(fresh_db, imap, smtp):
    from locadas.engine.scheduler import cycle_lock

    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        offer.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        assert fu.process_due(s, now=WEEKDAY_10, business_id=b.id + 1).get("announced", 0) == 0
        assert s.get(FollowUp, offer.followups[0].id).announced_at is None
        assert fu.process_due(s, now=WEEKDAY_10, business_id=b.id)["announced"] == 1
        bid = b.id
    c = TestClient(_app())
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    cycle_lock.acquire()
    try:
        r = c.post("/jetzt-pruefen", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("msg=laeuft")
    finally:
        cycle_lock.release()
    r = c.post("/jetzt-pruefen", follow_redirects=False)
    assert r.headers["location"] == "/dashboard"


# --- 9. TLS -------------------------------------------------------------------------------------

def test_tls_context_verifies():
    ctx = tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_no_plaintext_login_without_starttls(imap, smtp, monkeypatch):
    import locadas.mail.imap as imap_mod
    import locadas.mail.smtp as smtp_mod

    monkeypatch.setattr(imap_mod, "is_local_host", lambda h: False)
    monkeypatch.setattr(smtp_mod, "is_local_host", lambda h: False)
    with pytest.raises(MailConnectError, match="Verschlüsselung"):
        ImapClient("127.0.0.1", imap.port, False, USER, PASSWORD).connect()
    assert not any(line.startswith("LOGIN") for line in imap.log)
    with pytest.raises(MailConnectError, match="Verschlüsselung"):
        SmtpClient("127.0.0.1", smtp.port, False, USER, PASSWORD).test()


def test_ssl_uses_verified_context(monkeypatch):
    import imaplib
    import smtplib

    seen = {}

    def fake_imap(host, port, **kw):
        seen["imap"] = kw.get("ssl_context")
        raise OSError("aus")

    def fake_smtp(host, port, **kw):
        seen["smtp"] = kw.get("context")
        raise OSError("aus")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_imap)
    monkeypatch.setattr(smtplib, "SMTP_SSL", fake_smtp)
    with pytest.raises(MailConnectError):
        ImapClient("imap.example.de", 993, True, "u", "p").connect()
    with pytest.raises(MailConnectError):
        SmtpClient("smtp.example.de", 465, True, "u", "p").test()
    assert seen["imap"].verify_mode == ssl.CERT_REQUIRED and seen["smtp"].verify_mode == ssl.CERT_REQUIRED


def test_manual_ports_default_to_encryption(monkeypatch):
    tried = []

    def fake_try(ih, ip, issl, sh, sp, sssl, user, pw, sent):
        tried.append((ih, ip, issl, sh, sp, sssl))
        return None

    monkeypatch.setattr(conn, "_try", fake_try)
    with pytest.raises(conn.ConnectFailed):
        conn.connect_mailbox("info@x-bau.de", "pw", imap_host="mail.x-bau.de", imap_port=993, smtp_port=587)
    assert tried == [("mail.x-bau.de", 993, True, "mail.x-bau.de", 587, False)]  # 587 = STARTTLS (Pflicht)
    tried.clear()
    with pytest.raises(conn.ConnectFailed):
        conn.connect_mailbox("info@x-bau.de", "pw", imap_host="mail.x-bau.de", imap_port=9993)
    assert [t[2] for t in tried] == [True, False] and all(t[4] == 465 and t[5] for t in tried)


# --- 10. Eigene Domain: Autoconfig / MX statt Passwort-Rundreise ----------------------------------

AUTOCONFIG = b"""<?xml version="1.0"?>
<clientConfig version="1.1"><emailProvider id="x-bau.de">
<incomingServer type="imap"><hostname>imap.plain.de</hostname><port>143</port><socketType>plain</socketType><username>%EMAILADDRESS%</username></incomingServer>
<incomingServer type="imap"><hostname>imap.x-host.de</hostname><port>993</port><socketType>SSL</socketType><username>%EMAILLOCALPART%</username></incomingServer>
<outgoingServer type="smtp"><hostname>smtp.x-host.de</hostname><port>587</port><socketType>STARTTLS</socketType><username>%EMAILADDRESS%</username></outgoingServer>
</emailProvider></clientConfig>"""


def test_parse_autoconfig_skips_plaintext():
    cfg = autoconfig.parse_autoconfig(AUTOCONFIG, "info@x-bau.de", "ispdb")
    assert (cfg.imap_host, cfg.imap_port, cfg.imap_ssl) == ("imap.x-host.de", 993, True)
    assert (cfg.smtp_host, cfg.smtp_port, cfg.smtp_ssl) == ("smtp.x-host.de", 587, False)
    assert cfg.username == "info"
    assert autoconfig.parse_autoconfig(b"<kaputt", "a@b.de", "x") is None


def _mx_response(qid: int, domain: str, records: list[tuple[int, str]]) -> bytes:
    q = autoconfig.build_mx_query(domain, qid)[12:]
    out = struct.pack(">HHHHHH", qid, 0x8180, 1, len(records), 0, 0) + q
    for pref, host in records:
        name = b"".join(struct.pack("B", len(p)) + p.encode() for p in host.split(".")) + b"\x00"
        rdata = struct.pack(">H", pref) + name
        out += b"\xc0\x0c" + struct.pack(">HHIH", 15, 1, 300, len(rdata)) + rdata
    return out


def test_mx_parse_and_provider_mapping():
    data = _mx_response(4711, "x-bau.de", [(20, "mx01.kundenserver.de"), (10, "mx00.kundenserver.de")])
    assert autoconfig.parse_mx_response(data, 4711) == [(10, "mx00.kundenserver.de"), (20, "mx01.kundenserver.de")]
    assert autoconfig.parse_mx_response(data, 1) == []  # falsche ID
    assert autoconfig.provider_for_mx(["mx00.kundenserver.de"]).key == "ionos"
    assert autoconfig.provider_for_mx(["smtpin.rzone.de"]).key == "strato"
    assert autoconfig.provider_for_mx(["x-bau-de.mail.protection.outlook.com"]).key == "outlook"
    assert autoconfig.provider_for_mx(["aspmx.l.google.com"]).key == "gmail"
    assert autoconfig.provider_for_mx(["mail.eigener-server.de"]) is None


def test_resolve_order_ispdb_then_mx(monkeypatch):
    monkeypatch.setattr(autoconfig, "_fetch", lambda url: AUTOCONFIG if "thunderbird" in url else None)
    assert autoconfig.resolve("info@x-bau.de").source == "ispdb"
    monkeypatch.setattr(autoconfig, "_fetch", lambda url: None)
    monkeypatch.setattr(autoconfig, "mx_lookup", lambda d: ["mx00.kundenserver.de"])
    cfg = autoconfig.resolve("info@x-bau.de")
    assert cfg.source == "mx" and cfg.imap_host == "imap.ionos.de"
    assert autoconfig._fetch("http://unsicher.de/config.xml") is None  # nur HTTPS


def test_custom_domain_tries_only_the_resolved_host(monkeypatch):
    tried = []

    def fake_try(ih, ip, issl, sh, sp, sssl, user, pw, sent):
        tried.append(ih)
        return None

    monkeypatch.setattr(conn, "_try", fake_try)
    monkeypatch.setattr(autoconfig, "resolve", lambda email: autoconfig.ServerConfig("imap.x-host.de", 993, True, "smtp.x-host.de", 465, True, "", "ispdb"))
    with pytest.raises(conn.ConnectFailed):
        conn.connect_mailbox("info@x-bau.de", "pw")
    assert tried == ["imap.x-host.de"]
    tried.clear()
    monkeypatch.setattr(autoconfig, "resolve", lambda email: None)
    with pytest.raises(conn.ConnectFailed) as exc:
        conn.connect_mailbox("info@x-bau.de", "pw")
    assert tried == [] and "nicht automatisch" in exc.value.message
    from locadas.mail import providers
    assert not hasattr(providers, "custom_host_candidates")


def test_custom_domain_at_microsoft_explains_oauth(monkeypatch):
    from locadas.mail.providers import OUTLOOK

    monkeypatch.setattr(autoconfig, "resolve", lambda email: autoconfig.ServerConfig(OUTLOOK.imap_host, 993, True, OUTLOOK.smtp_host, 587, False, "", "mx", OUTLOOK))
    monkeypatch.setattr(conn, "_try", lambda *a: pytest.fail("kein Login-Versuch"))
    with pytest.raises(conn.ConnectFailed, match="kein Passwort-Login"):
        conn.connect_mailbox("info@x-bau.de", "pw")


# --- 11. Start nur mit eigenen Schlüsseln ------------------------------------------------------

def test_startup_check():
    s = Settings()
    s.secret_key, s.encryption_key, s.debug, s.allow_insecure_dev = DEFAULT_SECRET_KEY, None, False, False
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        check_startup(s)
    s.secret_key = "x" * 40
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        check_startup(s)
    s.encryption_key = "k"
    check_startup(s)
    s.secret_key, s.encryption_key, s.allow_insecure_dev = DEFAULT_SECRET_KEY, None, True
    check_startup(s)  # Tests/Demo
    assert settings.allow_insecure_dev  # conftest


# --- 12. Rate-Limit, interne Hosts ------------------------------------------------------------

def test_login_rate_limit(fresh_db):
    c = TestClient(_app())
    codes = [c.post("/login", data={"email": "niemand@x.de"}).status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_check_rate_limit():
    c = TestClient(_app())
    codes = [c.post("/check", data={"text": "kurz"}).status_code for _ in range(11)]
    assert codes[-1] == 429


def test_connect_blocks_private_hosts(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "allow_insecure_dev", False)
    c = TestClient(_app())
    r = c.post("/verbinden", data={"business_name": "X", "email": "a@x-bau.de", "password": "p", "imap_host": "127.0.0.1",
                                   "imap_port": "993", "consent": "1", "avv": "1"})
    assert r.status_code == 200 and "interne Adresse" in r.text


# --- 13. Datenschutz, AVV ----------------------------------------------------------------------

def test_privacy_avv_and_consent(fresh_db):
    c = TestClient(_app())
    avv = c.get("/avv")
    assert avv.status_code == 200 and "Vorlage" in avv.text and "Art. 28" in avv.text and "Anthropic" in avv.text
    priv = c.get("/datenschutz").text
    for word in ("Anthropic", "Standardvertragsklauseln", "USA", "Stilprobe", "Posteingang", "Link", "/avv"):
        assert word in priv
    assert 'name="avv"' in c.get("/verbinden").text
    r = c.post("/verbinden", data={"business_name": "X", "email": "a@x-bau.de", "password": "p", "consent": "1"})
    assert "Auftragsverarbeitungsvertrag" in r.text


# --- 14. Vorschau nennt die echte Versandzeit ---------------------------------------------------

def test_preview_late_afternoon_says_tomorrow_and_sends_then(fresh_db, imap, smtp):
    t = _berlin(2026, 11, 3, 17, 0)
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        offer.followups[0].due_at = t - timedelta(hours=1)
        s.commit()
        assert fu.process_due(s, now=t)["announced"] == 1
        f = s.get(FollowUp, offer.followups[0].id)
        assert f.send_after == _berlin(2026, 11, 4, 8, 0)
        n = len(smtp.messages)
        assert fu.process_due(s, now=_berlin(2026, 11, 3, 17, 59))["sent"] == 0
        st = fu.process_due(s, now=_berlin(2026, 11, 4, 8, 0))
        assert st["sent"] == 1 and "announced" not in st
    [(_, preview)] = [x for x in _msgs(smtp, [USER]) if "nachfassen" in str(x[1]["Subject"])]
    assert "Morgen ab 8 Uhr fasse ich" in preview.get_content()
    assert str(preview["Subject"]).startswith("Morgen nachfassen")
    assert len(smtp.messages) == n + 1  # nur die Kundenmail, keine neue Vorschau


def test_preview_morning_says_today_exact_time(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        offer.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        fu.process_due(s, now=WEEKDAY_10)
        assert fu.process_due(s, now=WEEKDAY_10 + timedelta(hours=1, minutes=59))["sent"] == 0
        assert fu.process_due(s, now=WEEKDAY_10 + timedelta(hours=2))["sent"] == 1
    preview = [m for r, m in _msgs(smtp, [USER])][0]
    assert "Heute ab 12 Uhr fasse ich" in preview.get_content()


def test_next_send_time_and_phrase_over_holidays():
    t = _berlin(2026, 12, 24, 17, 0)  # Do vor Weihnachten
    nxt = fu.next_send_time(t + timedelta(hours=2))
    assert nxt == _berlin(2026, 12, 28, 8, 0)
    assert fu.when_phrase(nxt, t) == "am Montag, 28.12., ab 8 Uhr"
    assert fu.when_phrase(_berlin(2026, 12, 24, 12, 30), _berlin(2026, 12, 24, 10, 30)) == "heute ab 12:30 Uhr"


# --- 15. Abstände ---------------------------------------------------------------------------------

def test_invoice_next_step_after_deadline_plus_three_bank_days(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        f1 = inv.followups[0]
        f1.due_at = WEEKDAY_10 - timedelta(hours=1)
        inv.followups[1].due_at = WEEKDAY_10 + timedelta(days=2)
        s.commit()
        fu.process_due(s, now=WEEKDAY_10)
        assert fu.approve(s, s.get(FollowUp, f1.id))
        s.commit()
        f1 = s.get(FollowUp, f1.id)
        deadline = texts.payment_deadline(f1.sent_at)
        assert f"bis {deadline:%d.%m.%Y}" in f1.body  # Frist steht in der Erinnerung
        f2 = s.get(Document, inv.id).followups[1]
        # erst Frist + 3 Bankarbeitstage, 8 Uhr
        d3 = holidays.add_workdays(deadline.date(), 3)
        assert f2.due_at >= _berlin(d3.year, d3.month, d3.day, 8, 0) > f1.sent_at + timedelta(days=9)
        assert s.get(Document, inv.id).followups[2].due_at >= f2.due_at + timedelta(days=7)


def test_offer_steps_at_least_four_days_apart(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        b.review_mode = "all"
        offer.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        offer.followups[1].due_at = WEEKDAY_10 + timedelta(days=1)
        s.commit()
        fu.process_due(s, now=WEEKDAY_10)
        f1 = s.get(FollowUp, offer.followups[0].id)
        assert fu.approve(s, f1)
        s.commit()
        d = s.get(Document, offer.id)
        assert d.followups[1].due_at >= f1.sent_at + timedelta(days=4)
        assert d.followups[2].due_at >= d.followups[1].due_at + timedelta(days=4)


# --- 16. Abwesenheitsnotiz mit Rückkehrdatum --------------------------------------------------------

@pytest.mark.parametrize("text,named,back", [
    ("Ich bin bis 15.11. nicht im Büro.", date(2026, 11, 15), date(2026, 11, 16)),
    ("Ab dem 12.11. bin ich wieder für Sie erreichbar.", date(2026, 11, 12), date(2026, 11, 12)),
    ("I am out of office until November 10.", date(2026, 11, 10), date(2026, 11, 11)),
    ("Ich bin ab 03.11. im Urlaub und ab Montag, 17.11.2026 wieder da.", date(2026, 11, 17), date(2026, 11, 17)),
    ("vom 01.11. bis einschließlich Freitag, 14. November", date(2026, 11, 14), date(2026, 11, 15)),
])
def test_parse_return_date(text, named, back):
    assert parse_return_date(text, date(2026, 11, 3)) == (named, back)


def test_parse_return_date_rejects_nonsense():
    assert parse_return_date("Ich bin im Urlaub.", date(2026, 11, 3)) is None
    assert parse_return_date("bis 01.01.2020 nicht da", date(2026, 11, 3)) is None


def test_out_of_office_postpones_and_tells_owner(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        doc_id = offer.id
    imap.store.append("INBOX", make_mail("familie.schmidt@gmx.de", USER, "Automatische Antwort: Angebot Gastherme",
                                         "Wir sind bis 15.11. im Urlaub und lesen keine Mails.", headers={"Auto-Submitted": "auto-replied"}))
    with new_session() as s:
        scan_account(s, s.get(MailAccount, account.id))
        d = s.get(Document, doc_id)
        assert d.status == DocStatus.OPEN
        resume = _berlin(2026, 11, 17, 8, 0)  # 16.11. zurück, +1 Werktag
        assert all(f.due_at >= resume for f in d.followups if f.status == FollowUpStatus.SCHEDULED)
        assert d.away_until == resume
    subj = [str(m["Subject"]) for _, m in _msgs(smtp, [USER])]
    assert any("Familie Schmidt ist bis 15.11. weg, ich warte" in x for x in subj)
    # Zweite Abwesenheitsnotiz: keine zweite Nachricht
    imap.store.append("INBOX", make_mail("familie.schmidt@gmx.de", USER, "Abwesenheitsnotiz", "Bis 15.11. nicht da.", headers={"Auto-Submitted": "auto-replied"}))
    n = len(smtp.messages)
    with new_session() as s:
        scan_account(s, s.get(MailAccount, account.id))
    assert len(smtp.messages) == n


# --- 17. Eigene Mail an die Kundenadresse schiebt, Vorschau zeigt es -------------------------------------

def test_any_own_mail_to_customer_pushes_and_preview_shows(fresh_db, imap, smtp):
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
        doc_id = offer.id
        before = [f.due_at for f in offer.followups]
    imap.store.append("Gesendet", make_mail(USER, "familie.schmidt@gmx.de", "Termin nächste Woche", "Passt Dienstag?", date=datetime.now(timezone.utc)))
    with new_session() as s:
        scan_account(s, s.get(MailAccount, account.id))
        d = s.get(Document, doc_id)
        assert d.owner_contact_at is not None
        assert d.followups[0].due_at >= datetime.now(timezone.utc) + timedelta(days=3) and d.followups[0].due_at > before[0]
        fu.send_preview(s, d.business, [d.followups[0]], WEEKDAY_10)
    preview = [m for _, m in _msgs(smtp, [USER])][-1]
    assert "selbst geschrieben" in preview.get_content()


# --- 18. CC bleibt erhalten ------------------------------------------------------------------------------

def test_cc_of_original_mail_is_kept(fresh_db, imap, smtp):
    pdf = make_pdf(["Mueller Haustechnik GmbH", "Angebot Nr. A-500", "Familie Kern", "Gesamtbetrag 2.000,00 EUR"])
    imap.store.append("Gesendet", make_mail(USER, "kern@web.de", "Angebot Bad", "Anbei.", ("Angebot_A-500.pdf", pdf),
                                            cc=f"Architekturbüro Lang <lang@planung.de>, {USER}"))
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        scan_account(s, account)
        d = s.scalars(select(Document).where(Document.document_number == "A-500")).one()
        assert d.cc == [("Architekturbüro Lang", "lang@planung.de")]
        assert fu.send_followup(s, d.followups[0])
    rcpts, m = _msgs(smtp, "kern@web.de")[-1]
    assert set(rcpts) == {"kern@web.de", "lang@planung.de"} and "lang@planung.de" in str(m["Cc"]) and USER not in str(m["Cc"])


# --- 19. Feiertage ----------------------------------------------------------------------------------------

def test_federal_holidays():
    assert holidays.easter(2026) == date(2026, 4, 5) and holidays.easter(2027) == date(2027, 3, 28)
    for d in (date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 1), date(2026, 5, 14), date(2026, 5, 25), date(2026, 10, 3), date(2026, 12, 25)):
        assert holidays.is_holiday(d) and not holidays.is_workday(d)
    assert holidays.is_workday(date(2026, 11, 3))
    assert not fu.in_send_window(_berlin(2026, 12, 25, 10, 0))
    assert fu.in_send_window(_berlin(2026, 12, 23, 10, 0))
    # Zahlungsfrist fällt nie auf einen Feiertag: 18.12. + 7 = 25.12. → Mo 28.12.
    assert texts.payment_deadline(_berlin(2026, 12, 18, 10, 0)).date() == date(2026, 12, 28)


# --- 20. Kosten: schnelles Modell, Vorfilter --------------------------------------------------------------

def test_extraction_uses_fast_model(monkeypatch):
    calls = []

    class _M:
        def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"kind": "offer", "amount_gross": 10}))])

    import anthropic
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=_M()))
    fx.extract_fields(DocKind.OFFER, "Angebot", [("", "a@b.de")], WEEKDAY_10, "Angebot Nr. 1 Gesamt 10,00 EUR")
    assert calls and calls[0]["model"] == settings.claude_fast_model == "claude-haiku-5-5" and "temperature" not in calls[0]


def test_worth_asking_prefilter():
    d = SimpleNamespace(customer_email="thomas.wolff@gmx.de", document_number="A-2026-140", customer_name="Familie Wolff")
    h = SimpleNamespace(customer_email="m.becker@hv-rhein.de", document_number="A-9", customer_name="Hausverwaltung Rhein GmbH")
    assert matcher.worth_asking("thomas.wolff@gmx.de", "x", "", [d])
    assert matcher.worth_asking("petra@web.de", "Frage zu A-2026-140", "", [d])
    assert matcher.worth_asking("petra@web.de", "Küche", "Gruß, Petra Wolff", [d])
    assert matcher.worth_asking("k.schulz@hv-rhein.de", "Strang", "", [h])  # gleiche Firmendomain
    assert not matcher.worth_asking("angebot@grosshandel.de", "Rohre im Angebot", "Jetzt zugreifen", [d, h])
    assert not matcher.worth_asking("max@gmx.de", "Hallo", "Grillen am Samstag?", [d, h])  # gmx.de ist keine Firmendomain


def test_unrelated_mail_never_reaches_claude(fresh_db, imap, smtp, monkeypatch):
    calls = []
    import anthropic
    with new_session() as s:
        b, account, offer, inv = _scanned(s, imap, smtp)
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=SimpleNamespace(create=lambda **c: calls.append(c))))
    imap.store.append("INBOX", make_mail("vertrieb@grosshandel.de", USER, "Preisliste 2027", "Neue Preise für Kupferrohre."))
    with new_session() as s:
        assert scan_account(s, s.get(MailAccount, account.id))["replies"] == 0
    assert calls == []
