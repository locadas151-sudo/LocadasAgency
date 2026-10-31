"""Rückmeldungen aus der Simulation (20 kritische Inhaber): Begriff/Währung, Rechnungston, Betreff, Link/Anhang,
geöffnete Angebote, Eingangsbestätigung, ruhige dritte Mail, Rückfrage bei Vorgängen von vor dem Start."""
from __future__ import annotations

import email
import email.policy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from locadas import fmt
from locadas.db import Business, DocKind, DocStatus, Document, FollowUp, FollowUpStatus, new_session
from locadas.engine import followups as fu
from locadas.engine import personalize as pz
from locadas.engine import texts
from locadas.engine.scanner import is_receipt_ack, scan_account
from locadas.extract import fields as fx
from locadas.notify import messages as wa_msgs
from locadas.notify.tokens import parse
from tests.conftest import USER
from tests.fakemail import make_mail, make_pdf
from tests.test_e2e import WEEKDAY_10, _seed_sent, _setup
from tests.test_personalize import SIE_PROFILE, FakeAnthropic, fake_claude  # noqa: F401

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)  # Freitag
VALID_DE = "DE89 3704 0044 0532 0130 00"
VALID_CH = "CH93 0076 2011 6238 5295 7"

SWISS_TEXT = (
    "Pannasch Renovationen\nBahnhofstrasse 14\n8400 Winterthur\n\nFrau Lea Brunner\nRosenweg 5\n\nOfferte Nr. O-0007\nDatum: 26.10.2026\n\n"
    "1   Badezimmer Plattenarbeiten   18 m2  CHF 95.00  CHF 1'710.00\n2   Silikonfugen erneuern  1 Pauschal  CHF 280.00  CHF 280.00\n\n"
    "Total exkl. MWST CHF 1'990.00\nMWST 8.1 % CHF 161.19\nTotal inkl. MWST CHF 2'151.19\n\nGueltig 30 Tage.\n\nIBAN CH93 0076 2011 6238 5295 7"
)


def _biz(**kw):
    base = dict(id=1, name="Rohrblitz Notdienst UG", owner_name="Kevin Lorenz", phone="0800 112211", signature="", include_view_link=True,
                doc_delivery="link", tone="freundlich", personalize=True, style_json="", style_updated_at=None, style_samples_json="")
    base.update(kw)
    return SimpleNamespace(**base)


def _doc(kind=DocKind.INVOICE, **kw):
    b = kw.pop("business", None) or _biz()
    base = dict(id=3, kind=kind, kind_label="Rechnung" if kind == DocKind.INVOICE else "Angebot", doc_term="", customer_name="Sven Krüger",
                customer_email="sven.krueger@gmx.net", document_number="NB-55102", title="Notdiensteinsatz", subject="Ihre Rechnung NB-55102",
                sent_at=NOW - timedelta(days=5), amount_gross=318.92, currency="EUR", due_date=NOW - timedelta(days=4), valid_until=None,
                business=b, view_token="tok", pdf_path="/x.pdf", iban=VALID_DE, bic="COBADEFFXXX", bank_name="", account_holder="",
                ack_at=None, first_opened_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _swiss_offer(**kw):
    base = dict(kind=DocKind.OFFER, kind_label="Angebot", doc_term="Offerte", customer_name="Lea Brunner", customer_email="lea.brunner@bluewin.ch",
                document_number="O-0007", title="Badezimmer", subject="Offerte Badezimmer", amount_gross=2151.19, currency="CHF", due_date=None,
                iban="", bic="", business=_biz(name="Pannasch Renovationen", owner_name="Mykhaylo Pannasch", phone="+41 78 123 45 67"))
    base.update(kw)
    return _doc(**base)


# --- 1. Begriff und Währung --------------------------------------------------------------

def test_money_formats_per_currency():
    assert fmt.money(1234.56, "EUR") == "1.234,56 €"
    assert fmt.money(2151.19, "CHF") == "CHF 2'151.19"
    assert fmt.money(None) == ""
    assert fmt.over(318.92, "EUR") == " über 318,92 €"
    assert wa_msgs.money(2151.19, "CHF") == " über CHF 2'151.19"
    assert fu._money(2151.19, "CHF") == " über CHF 2'151.19"


def test_swiss_offerte_extraction():
    r = fx.heuristic_extract(DocKind.OFFER, "Offerte Badezimmer", [("Lea Brunner", "lea.brunner@bluewin.ch")], NOW, SWISS_TEXT, "Offerte_O-0007.pdf")
    assert r.currency == "CHF" and r.amount_gross == 2151.19 and r.doc_term == "Offerte"


@pytest.mark.parametrize("kind,subject,filename,text,expected", [
    (DocKind.OFFER, "Unterlagen", "doc.pdf", "Firma\n\nKOSTENVORANSCHLAG Nr. 5\n", "Kostenvoranschlag"),
    (DocKind.OFFER, "Ihr Angebot AN-1", "x.pdf", "", "Angebot"),
    (DocKind.OFFER, "Dusche Meier", "Angebot Meier.pdf", "", "Angebot"),
    (DocKind.INVOICE, "Rechnung zu Angebot 118", "R.pdf", "", "Rechnung"),
    (DocKind.INVOICE, "Unterlagen", "x.pdf", "Bau GmbH\nAbschlagsrechnung Nr. 2\n", "Abschlagsrechnung"),
    (DocKind.OFFER, "Unterlagen", "x.pdf", "nichts", ""),
])
def test_detect_doc_term(kind, subject, filename, text, expected):
    assert fx.detect_doc_term(kind, subject, filename, text) == expected


def test_claude_extraction_returns_doc_term_and_currency(monkeypatch):
    monkeypatch.setattr(fx.settings, "anthropic_api_key", "test")

    class FakeClient:
        def __init__(self, **kw):
            self.messages = self

        def create(self, **kw):
            assert "doc_term" in kw["messages"][0]["content"] and "temperature" not in kw
            return SimpleNamespace(content=[SimpleNamespace(type="thinking"), SimpleNamespace(text='{"kind":"offer","doc_term":"Offerte","currency":"EUR","amount_gross":2151.19,"customer_name":"Lea Brunner","confidence":0.9}')])

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    r = fx.extract_fields(DocKind.OFFER, "Offerte Badezimmer", [("", "lea.brunner@bluewin.ch")], NOW, SWISS_TEXT, "o.pdf")
    assert r.doc_term == "Offerte" and r.currency == "CHF"  # CHF im Dokument schlägt Claudes „EUR“
    assert fx.parse_claude_json('{"kind":"offer","doc_term":"Quatsch"}', None).doc_term == ""


def test_templates_use_doc_term_and_currency():
    doc = _swiss_offer()
    for step in (1, 2, 3):
        body = texts.body_for(doc, step, NOW)
        assert "Offerte" in body and "Angebot" not in body and "€" not in body
    assert "zu unserer Offerte" in texts.body_for(doc, 2, NOW)
    assert "Die Offerte als PDF: " in texts.body_for(doc, 1, NOW)
    kv = _doc(kind=DocKind.OFFER, doc_term="Kostenvoranschlag", kind_label="Angebot")
    assert "unseren Kostenvoranschlag" in texts.body_for(kv, 1, NOW)
    inv = _doc(currency="CHF", amount_gross=2151.19)
    assert "CHF 2'151.19" in texts.body_for(inv, 1, NOW)


def test_personalize_guards_term_and_currency():
    ctx = pz.build_context(_swiss_offer(), 2, SIE_PROFILE, now=NOW)
    assert ctx.facts["Betrag"] == "CHF 2'151.19" and ctx.term == "Offerte"
    prompt = pz.build_prompt(ctx)
    assert "Offerte" in prompt and "CHF 2'151.19" in prompt
    good = "Guten Tag Lea Brunner,\n\nich melde mich noch einmal zu meiner Offerte O-0007 über CHF 2'151.19. Haben Sie noch Fragen dazu?"
    assert pz.validate(good, ctx) == []
    wrong_term = "Guten Tag Lea Brunner,\n\nich melde mich noch einmal zu meinem Angebot O-0007. Haben Sie noch Fragen dazu?"
    assert any("Offerte" in p for p in pz.validate(wrong_term, ctx))
    wrong_cur = "Guten Tag Lea Brunner,\n\nzu meiner Offerte O-0007 über 2.151,19 € wollte ich nachfragen. Haben Sie noch Fragen?"
    assert any("Währung" in p for p in pz.validate(wrong_cur, ctx))


# --- 2. Rechnungen: Ton, Frist, Bankverbindung ---------------------------------------------

def test_iban_extraction_with_checksum():
    assert fx.iban_valid(VALID_DE.replace(" ", "")) and fx.iban_valid(VALID_CH) and fx.iban_valid("AT61 1904 3002 3457 3201")
    assert not fx.iban_valid("DE12 3456 7890 1234 5678 90")
    text = "Bankverbindung: Sparkasse · IBAN DE12 3456 7890 1234 5678 90\nKontoinhaber: Rohrblitz Notdienst UG\nIBAN: DE89370400440532013000 BIC: COBADEFFXXX"
    d = fx.bank_details(text)
    assert d["iban"] == VALID_DE and d["bic"] == "COBADEFFXXX" and d["account_holder"] == "Rohrblitz Notdienst UG"
    assert fx.bank_details("USt-IdNr. DE123456789")["iban"] == ""
    r = fx.heuristic_extract(DocKind.INVOICE, "Rechnung", [("", "a@gmx.de")], NOW, "Rechnung Nr. 1\nRechnungsbetrag 99,00 €\nIBAN " + VALID_CH)
    assert r.iban == VALID_CH


def test_payment_deadline_skips_weekend():
    # Freitag + 7 = Freitag; Dienstag 03.11. + 7 = Dienstag; Sonntag 01.11. + 7 = Sonntag -> Montag 09.11.
    assert texts.payment_deadline(NOW).strftime("%d.%m.%Y") == "06.11.2026"
    assert texts.payment_deadline(datetime(2026, 10, 31, 10, tzinfo=timezone.utc)).strftime("%d.%m.%Y %a") == "09.11.2026 Mon"
    assert texts.payment_deadline(datetime(2026, 11, 1, 10, tzinfo=timezone.utc)).strftime("%d.%m.%Y") == "09.11.2026"


@pytest.mark.parametrize("tone", ["freundlich", "sachlich", "bestimmt"])
@pytest.mark.parametrize("step", [1, 2, 3])
def test_every_invoice_reminder_has_date_iban_and_no_soft_phrases(tone, step):
    doc = _doc(business=_biz(tone=tone))
    body = texts.body_for(doc, step, NOW)
    assert "Bitte überweisen Sie 318,92 € bis 06.11.2026 auf IBAN DE89 3704 0044 0532 0130 00 (BIC COBADEFFXXX), Verwendungszweck: NB-55102." in body
    for bad in ("untergegangen", "ignorieren", "Ja, Nein", ", und "):
        assert bad not in body
    if step == 3:
        assert "Mahnverfahren" in body
    if tone == "bestimmt" and step == 2:
        assert "Verzug" in body and "Mahnkosten" in body
    if tone != "bestimmt" and step < 3:
        assert "Mahn" not in body


def test_invoice_without_iban_still_has_date():
    body = texts.body_for(_doc(iban="", amount_gross=None, document_number=""), 1, NOW)
    assert "Bitte überweisen Sie den offenen Betrag bis 06.11.2026." in body


def test_business_customers_get_at_least_sachlich():
    assert texts.invoice_tone(_doc()) == "freundlich"
    assert texts.invoice_tone(_doc(customer_name="Hochtief Hochbau Projekt GmbH")) == "sachlich"
    assert texts.invoice_tone(_doc(customer_name="", customer_email="rechnungseingang@gu-bauprojekt.de")) == "sachlich"
    assert texts.invoice_tone(_doc(customer_name="Hausverwaltung Kraus")) == "sachlich"
    assert texts.invoice_tone(_doc(customer_name="Hausverwaltung Kraus", business=_biz(tone="bestimmt"))) == "bestimmt"
    gu = _doc(customer_name="Hochtief Hochbau Projekt GmbH", customer_email="rechnungseingang@gu-bauprojekt.de")
    assert "liegt uns noch kein Zahlungseingang vor" in texts.body_for(gu, 1, NOW)


def test_personalize_invoice_closing_and_soft_phrase_guard(fake_claude):
    doc = _doc(business=_biz(tone="bestimmt"), ack_at=NOW - timedelta(days=3))
    fake_claude.replies = [
        "Guten Tag Sven Krüger,\n\nvielleicht ist unsere Rechnung NB-55102 untergegangen. Falls schon bezahlt, ignorieren Sie diese Mail.",
        "Guten Tag Sven Krüger,\n\nunsere Rechnung NB-55102 über 318,92 € ist seit dem 26.10.2026 fällig und noch nicht bezahlt.",
    ]
    body = pz.generate(doc, 1, SIE_PROFILE, now=NOW)
    assert body is not None and "untergegangen" not in body
    assert "zu weich" in fake_claude.calls[1]["messages"][0]["content"]
    assert "Sie hatten den Eingang am 27.10.2026 bestätigt. Bitte überweisen Sie 318,92 € bis 06.11.2026 auf IBAN" in body
    assert "bestimmt" in fake_claude.calls[0]["messages"][0]["content"]
    ctx2 = pz.build_context(doc, 2, SIE_PROFILE, now=NOW)
    no_cost = "Guten Tag Sven Krüger,\n\nunsere Rechnung NB-55102 ist weiter offen. Sie sind damit im Verzug."
    assert any("Mahnkosten" in p for p in pz.validate(no_cost, ctx2))
    assert pz.validate(no_cost + " Danach berechnen wir Mahnkosten.", ctx2) == []


# --- 3. Betreff -----------------------------------------------------------------------------

def test_subjects_are_honest():
    offer = _swiss_offer()
    assert [texts.subject_for(offer, s) for s in (1, 2, 3)] == ["Offerte Badezimmer"] * 3
    inv = _doc(subject="AW: Ihre Rechnung NB-55102")
    assert texts.subject_for(inv, 1) == "Zahlungserinnerung: Ihre Rechnung NB-55102"
    assert texts.subject_for(inv, 2) == "Zahlungserinnerung: Ihre Rechnung NB-55102"
    assert texts.subject_for(inv, 3) == "Letzte Zahlungserinnerung: Ihre Rechnung NB-55102"
    assert texts.subject_for(_doc(subject=""), 1) == "Zahlungserinnerung: Rechnung NB-55102"


# --- 4. Link oder Anhang --------------------------------------------------------------------

def test_view_base_url_and_attachment_mode(monkeypatch):
    from locadas.config import settings

    monkeypatch.setattr(settings, "view_base_url", "https://dok.example.de")
    doc = _doc(kind=DocKind.OFFER, kind_label="Angebot", doc_term="Angebot")
    assert "Das Angebot als PDF: https://dok.example.de/a/tok" in texts.body_for(doc, 1, NOW)
    doc.business.doc_delivery = "attachment"
    body = texts.body_for(doc, 1, NOW)
    assert "/a/" not in body and "Das Angebot hänge ich Ihnen noch einmal an." in body
    doc.business.include_view_link = False
    assert "hänge" not in texts.body_for(doc, 1, NOW) and "/a/" not in texts.body_for(doc, 1, NOW)


def test_attachment_mode_resends_pdf(fresh_db, imap, smtp):
    _seed_sent(imap)
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        scan_account(s, account)
        b.auto_mode = True
        b.doc_delivery = "attachment"
        offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
        f = offer.followups[0]
        assert fu.send_followup(s, f)
        s.commit()
    m = email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default)
    assert m["Subject"] == "Angebot Gastherme"
    pdfs = [p for p in m.iter_attachments() if p.get_content_type() == "application/pdf"]
    assert len(pdfs) == 1 and pdfs[0].get_content()[:4] == b"%PDF"
    assert "/a/" not in m.get_body(preferencelist=("plain",)).get_content()


def test_settings_save_tone_and_delivery(fresh_db, imap, smtp):
    from locadas.main import app
    from locadas.web import auth

    with new_session() as s:
        b, _ = _setup(s, imap, smtp)
        bid = b.id
    client = TestClient(app)
    client.cookies.set(auth.COOKIE, auth.session_token(bid))
    r = client.get("/einstellungen")
    assert r.status_code == 200 and 'name="tone"' in r.text and 'name="doc_delivery"' in r.text
    client.post("/einstellungen", data={"name": "Müller", "doc_delivery": "attachment", "tone": "bestimmt"}, follow_redirects=False)
    with new_session() as s:
        b = s.get(Business, bid)
        assert b.doc_delivery == "attachment" and b.tone == "bestimmt" and b.include_view_link
    client.post("/einstellungen", data={"name": "Müller", "doc_delivery": "none", "tone": "quatsch"}, follow_redirects=False)
    with new_session() as s:
        b = s.get(Business, bid)
        assert not b.include_view_link and b.tone == "bestimmt"


# --- 5. Kunde hat geöffnet ------------------------------------------------------------------

def test_opened_offer_texts_never_ask_for_decision():
    doc = _swiss_offer(first_opened_at=NOW)
    for step in (1, 2, 3):
        body = texts.body_for(doc, step, NOW).lower()
        assert "entscheid" not in body and "rückmeldung" not in body
        assert "fragen zur offerte" in body
    ctx = pz.build_context(doc, 2, SIE_PROFILE, now=NOW)
    assert ctx.opened and any("geöffnet" in k for k in ctx.facts) and "nicht nach einer Entscheidung" in pz.build_prompt(ctx)
    pushy = "Guten Tag Lea Brunner,\n\nzu meiner Offerte O-0007 bitte ich Sie um eine kurze Entscheidung. Haben Sie noch Fragen?"
    assert any("geöffnet" in p for p in pz.validate(pushy, ctx))


def test_first_open_pushes_next_step(fresh_db, imap, smtp):
    _seed_sent(imap)
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        scan_account(s, account)
        offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
        before = [f.due_at for f in offer.followups]
        token, doc_id = offer.view_token, offer.id
    client = TestClient(__import__("locadas.main", fromlist=["app"]).app)
    r = client.get(f"/a/{token}")
    assert r.status_code == 200 and "Müller Haustechnik GmbH" in r.text and "0171 1" in r.text
    client.get(f"/a/{token}")  # zweites Öffnen schiebt nicht noch einmal
    with new_session() as s:
        d = s.get(Document, doc_id)
        after = [f.due_at for f in d.followups]
        assert all(b_ > d.first_opened_at for b_ in before)
        assert after == [b_ + timedelta(days=3) for b_ in before]
        assert d.next_followup_at == after[0]


# --- 6. Eingangsbestätigung ---------------------------------------------------------------

def test_receipt_ack_detection():
    assert is_receipt_ack("Automatische Antwort: Eingangsbestätigung", "Ihre Rechnung ist eingegangen und wird geprüft.")
    assert is_receipt_ack("Re: Rechnung", "We have received your invoice.")
    assert not is_receipt_ack("Abwesenheitsnotiz", "Ihre Mail habe ich erhalten, ich bin bis 15.11. im Urlaub.")
    assert not is_receipt_ack("Automatic reply", "I am out of office until 10 November.")


def test_autoreply_ack_is_stored_and_used(fresh_db, imap, smtp):
    _seed_sent(imap)
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        scan_account(s, account)
        inv = s.scalars(select(Document).where(Document.document_number == "R-2026-301")).one()
        raw = make_mail("p.weber@web.de", USER, "Automatische Antwort: Eingangsbestätigung", "Ihre Rechnung ist eingegangen und wird geprüft.", in_reply_to=inv.message_id)
        raw = raw.replace(b"Subject:", b"Auto-Submitted: auto-replied\nSubject:", 1)
        imap.store.append("INBOX", raw)
        scan_account(s, account)
        s.expire_all()
        inv = s.get(Document, inv.id)
        assert inv.status == DocStatus.OPEN and inv.ack_at is not None
        body = texts.body_for(inv, 1, NOW)
        assert f"Sie hatten den Eingang am {inv.ack_at.strftime('%d.%m.%Y')} bestätigt." in body


# --- 7. Ruhige dritte Mail beim Angebot -------------------------------------------------------

def test_offer_step3_is_calm():
    body = texts.body_for(_swiss_offer(), 3, NOW)
    assert "Ich melde mich nicht weiter, Sie erreichen mich aber jederzeit unter +41 78 123 45 67." in body
    assert "letzte" not in body.lower() and "Ja, Nein" not in body
    no_phone = _swiss_offer(business=_biz(phone=""))
    assert "Ich melde mich nicht weiter. Wenn Sie später" in texts.body_for(no_phone, 3, NOW)
    ctx = pz.build_context(_swiss_offer(), 3, SIE_PROFILE, now=NOW)
    threat = "Guten Tag Lea Brunner,\n\ndies ist meine letzte Nachricht zur Offerte O-0007. Ja, Nein oder später?"
    assert any("Drohung" in p for p in pz.validate(threat, ctx))
    calm = "Guten Tag Lea Brunner,\n\nzu meiner Offerte O-0007 habe ich noch nichts gehört. Vielleicht passt es gerade nicht, das ist völlig in Ordnung."
    assert pz.validate(calm, ctx) == []
    assert pz.assemble(calm, ctx.doc, ctx).count("Ich melde mich nicht weiter") == 1


# --- 8. Vorgänge von vor dem Start: erst fragen -------------------------------------------------

def _review_setup(s, imap, smtp):
    b, account = _setup(s, imap, smtp)
    scan_account(s, account)
    inv = s.scalars(select(Document).where(Document.document_number == "R-2026-301")).one()
    inv.needs_review = True
    inv.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
    offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
    for f in offer.followups:
        f.due_at = WEEKDAY_10 + timedelta(days=5 * f.step)
    s.commit()
    return b, inv


def test_needs_review_asks_question_first(fresh_db, imap, smtp):
    _seed_sent(imap)
    with new_session() as s:
        b, inv = _review_setup(s, imap, smtp)
        stats = fu.process_due(s, now=WEEKDAY_10)
        assert stats["approval"] == 1
        f = inv.followups[0]
        assert f.status == FollowUpStatus.AWAITING_APPROVAL and f.body == ""  # keine Mail vorbereitet
        token, fid, doc_id = f.approval_token, f.id, inv.id
    m = email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default)
    text = m.get_content()
    assert "Hallo Thomas Müller,\r\n\r\ndiese Rechnung R-2026-301 über 224,91 € hast du am" in text and "vor deinem Start bei uns. Ist sie schon bezahlt?" in text
    assert f"/f/{token}/done" in text and f"/f/{token}/send" in text and "Guten Tag" not in text

    from locadas.main import app
    client = TestClient(app)
    r = client.get(f"/f/{token}/send")
    assert r.status_code == 200 and "Wird nachgefasst" in r.text
    with new_session() as s:
        f = s.get(FollowUp, fid)
        assert f.status == FollowUpStatus.SCHEDULED and not f.document.needs_review and f.document.status == DocStatus.OPEN
        assert s.get(Document, doc_id).followups_sent == 0  # nichts gesendet, normaler Ablauf
        # Ab jetzt normale Freigabe mit Text (Betrieb ist noch im Freigabe-Modus)
        assert fu.process_due(s, now=WEEKDAY_10)["approval"] == 1
        assert s.get(FollowUp, fid).body.startswith("Guten Tag Peter Weber,")


def test_needs_review_paid_button(fresh_db, imap, smtp):
    _seed_sent(imap)
    with new_session() as s:
        b, inv = _review_setup(s, imap, smtp)
        fu.process_due(s, now=WEEKDAY_10)
        token, doc_id = inv.followups[0].approval_token, inv.id
    from locadas.main import app
    r = TestClient(app).get(f"/f/{token}/done")
    assert r.status_code == 200 and "Bezahlt" in r.text
    with new_session() as s:
        d = s.get(Document, doc_id)
        assert d.status == DocStatus.PAID and all(f.status == FollowUpStatus.CANCELLED for f in d.followups)


def test_needs_review_whatsapp_question_and_buttons(fresh_db, imap, smtp):
    from locadas.notify.inbound import run_payload

    _seed_sent(imap)
    with new_session() as s:
        b, inv = _review_setup(s, imap, smtp)
        f = inv.followups[0]
        f.status = FollowUpStatus.AWAITING_APPROVAL
        n = wa_msgs.approval(b, f, 1, 1, "s", "b", None)
        assert "Ist sie schon bezahlt?" in n.text and [lbl for lbl, _ in n.buttons] == ["Bezahlt", "Bitte nachfassen"]
        assert n.template is None  # außerhalb des 24h-Fensters lieber per Mail als mit falschen Vorlagen-Knöpfen
        assert parse(n.buttons[0][1]) == ("settle", [f.id]) and parse(n.buttons[1][1]) == ("send", [f.id])
        reply, _ = run_payload(s, [b], n.buttons[0][1], WEEKDAY_10)
        assert "Bezahlt" in reply and inv.status == DocStatus.PAID
        offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
        offer.needs_review = True
        of = offer.followups[0]
        of.status = FollowUpStatus.AWAITING_APPROVAL
        n = wa_msgs.approval(b, of, 1, 1, "s", "b", None)
        assert "Dieses Angebot A-2026-118" in n.text and "Ist es schon erledigt?" in n.text and n.buttons[0][0] == "Erledigt"
        reply, _ = run_payload(s, [b], n.buttons[1][1], WEEKDAY_10)
        assert "ich fasse bei" in reply and of.status == FollowUpStatus.SCHEDULED and not offer.needs_review


def test_mixed_approval_mail_bundles_questions_and_texts(fresh_db, imap, smtp):
    _seed_sent(imap)
    with new_session() as s:
        b, inv = _review_setup(s, imap, smtp)
        offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
        offer.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        assert fu.process_due(s, now=WEEKDAY_10)["approval"] == 2
    m = email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default)
    text = m.get_content()
    assert "Frage und 1 Freigabe" in m["Subject"]
    assert text.index("Ist sie schon bezahlt?") < text.index("Außerdem würde ich bei einem Kunden")
    assert len(smtp.messages) == 1


def test_opened_offer_never_reveals_tracking():
    ctx = pz.build_context(_swiss_offer(first_opened_at=NOW), 2, SIE_PROFILE, now=NOW)
    creepy = "Guten Tag Lea Brunner,\n\nich sehe, dass Sie die Offerte O-0007 angesehen haben. Haben Sie noch Fragen dazu?"
    assert any("Tracking" in p for p in pz.validate(creepy, ctx))
    assert "Niemals erwähnen" in pz.build_prompt(ctx)
