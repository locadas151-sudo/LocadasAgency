"""Personalisierte Nachfass-Texte ohne Netz: Stilprofil, Prüfregeln, Fallback auf die Vorlage."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from locadas.db import DocKind
from locadas.engine import personalize as pz
from locadas.engine import style, texts

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

SIE_PROFILE = {"tone": "sie", "greeting": "Guten Tag", "sign_off": "Beste Grüße", "signature": "", "formality": "neutral", "avg_words": 60, "notes": ""}
DU_PROFILE = {"tone": "du", "greeting": "Moin", "sign_off": "LG", "signature": "", "formality": "locker", "avg_words": 40, "notes": "kurz und direkt"}


def _business(**kw):
    base = dict(id=1, name="Müller Haustechnik GmbH", owner_name="Thomas Müller", phone="0171 2345678", signature="",
                include_view_link=False, personalize=True, style_json="", style_updated_at=None, style_samples_json="")
    base.update(kw)
    return SimpleNamespace(**base)


def _doc(kind=DocKind.OFFER, customer="Herr Weber", **kw):
    b = kw.pop("business", None) or _business()
    base = dict(id=7, kind=kind, kind_label="Angebot" if kind == DocKind.OFFER else "Rechnung", customer_name=customer,
                document_number="A-2026-118", title="Austausch Gastherme", subject="Angebot Gastherme", sent_at=NOW,
                amount_gross=3724.70, business=b, valid_until=None, due_date=None, view_token="tok123", pdf_path="/x.pdf")
    base.update(kw)
    return SimpleNamespace(**base)


class FakeAnthropic:
    """Ersetzt anthropic.Anthropic; gibt der Reihe nach die vorbereiteten Antworten zurück."""

    replies: list = []
    calls: list = []

    def __init__(self, **kw):
        self.messages = self

    def create(self, **kw):
        FakeAnthropic.calls.append(kw)
        reply = FakeAnthropic.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=[SimpleNamespace(text=reply)])


@pytest.fixture()
def fake_claude(monkeypatch):
    import anthropic

    monkeypatch.setattr(pz.settings, "anthropic_api_key", "test")
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    FakeAnthropic.replies = []
    FakeAnthropic.calls = []
    return FakeAnthropic


GOOD_OFFER_1 = (
    "Guten Tag Herr Weber,\n\n"
    "vor ein paar Tagen habe ich Ihnen unser Angebot A-2026-118 für den Austausch der Gastherme geschickt. "
    "Ist alles gut angekommen?\n\n"
    "Wenn Fragen offen sind oder Sie etwas anpassen möchten, melden Sie sich gern bei mir.\n\n"
    "Viele Grüße\nThomas Müller"
)


# --- Anrede ------------------------------------------------------------------------

@pytest.mark.parametrize("name,profile,expected", [
    ("Herr Weber", SIE_PROFILE, "Guten Tag Herr Weber,"),
    ("Hr. Weber", {**SIE_PROFILE, "greeting": "Sehr geehrte"}, "Sehr geehrter Herr Weber,"),
    ("Frau Kaya", {**SIE_PROFILE, "greeting": "Hallo"}, "Hallo Frau Kaya,"),
    ("Fam. Schmidt", SIE_PROFILE, "Guten Tag Familie Schmidt,"),
    ("Familie Schmidt", DU_PROFILE, "Moin Familie Schmidt,"),
    ("Bau GmbH & Co. KG", DU_PROFILE, "Moin,"),
    ("Bau GmbH & Co. KG", {**SIE_PROFILE, "greeting": "Hallo"}, "Guten Tag,"),
    ("", {**SIE_PROFILE, "greeting": "Sehr geehrte"}, "Sehr geehrte Damen und Herren,"),
    ("Peter Weber", DU_PROFILE, "Moin Peter,"),
    ("Peter Weber", SIE_PROFILE, "Guten Tag Peter Weber,"),
    ("Herr Weber", DU_PROFILE, "Moin Herr Weber,"),          # Herr/Frau: nie duzen
    ("Dr. Peter Weber", DU_PROFILE, "Moin Dr. Peter Weber,"),
    ("Peter Weber", None, "Guten Tag Peter Weber,"),
])
def test_anrede(name, profile, expected):
    assert pz.anrede(_doc(customer=name), profile) == expected


def test_du_only_for_private_customers_when_business_clearly_duzt():
    assert pz.build_context(_doc(customer="Peter Weber"), 1, DU_PROFILE).du
    assert not pz.build_context(_doc(customer="Peter Weber"), 1, {**DU_PROFILE, "tone": "gemischt"}).du
    assert not pz.build_context(_doc(customer="Familie Schmidt"), 1, DU_PROFILE).du
    assert not pz.build_context(_doc(customer="Schmidt Bau GmbH"), 1, DU_PROFILE).du


# --- Stilprofil -------------------------------------------------------------------------

def test_collect_sample_strips_quotes_and_skips_colleagues():
    b = _business()
    account = SimpleNamespace(email="info@mueller-shk.de", business=b)
    session = SimpleNamespace(scalar=lambda *a, **k: None)
    body = "Hallo Herr Weber,\n\nanbei wie besprochen die Unterlagen für das Bad.\n\nViele Grüße\nThomas Müller\n\nAm 12.10.2026 schrieb Peter Weber:\n> alte Mail"
    mail = SimpleNamespace(to=[("Peter Weber", "p.weber@web.de")], message_id="<1@x>", text=body)
    style.collect_sample(session, account, mail)
    samples = json.loads(b.style_samples_json)
    assert len(samples) == 1 and "alte Mail" not in samples[0] and samples[0].endswith("Thomas Müller")
    style.collect_sample(session, account, SimpleNamespace(to=[("", "kollege@mueller-shk.de")], message_id="<2@x>", text=body))
    style.collect_sample(session, account, SimpleNamespace(to=[("", "a@b.de")], message_id="<3@x>", text="Danke!"))
    assert len(json.loads(b.style_samples_json)) == 1


def test_samples_are_capped():
    b = _business()
    account = SimpleNamespace(email="info@mueller-shk.de", business=b)
    session = SimpleNamespace(scalar=lambda *a, **k: None)
    for i in range(20):
        style.collect_sample(session, account, SimpleNamespace(to=[("", "k@web.de")], message_id=f"<{i}@x>", text=f"Hallo, hier die Unterlagen Nummer {i} wie besprochen für Sie anbei."))
    assert len(json.loads(b.style_samples_json)) == style.MAX_SAMPLES


def test_profile_derivation_call_path(fake_claude):
    samples = [
        "Moin Peter,\n\nanbei das Angebot fürs Bad. Meld dich einfach.\n\nLG\nThomas\nMüller Haustechnik",
        "Moin Sabine,\n\nwie besprochen komme ich Dienstag.\n\nLG\nThomas\nMüller Haustechnik",
        "Moin Jens,\n\nhier die Rechnung. Danke dir!\n\nLG\nThomas\nMüller Haustechnik",
    ]
    b = _business(style_samples_json=json.dumps(samples[:2]))
    assert not style.needs_update(b)  # unter 3 Proben: kein Aufruf
    b.style_samples_json = json.dumps(samples)
    assert style.needs_update(b)
    fake_claude.replies = ['```json\n{"anrede":"du","greeting":"Moin","sign_off":"LG","signature":"Thomas\\nMüller Haustechnik\\nErfundene Str. 1","formality":"locker","avg_words":25,"notes":"kurz"}\n```']
    profile = style.ensure_profile(b)
    assert len(fake_claude.calls) == 1
    call = fake_claude.calls[0]
    assert "temperature" not in call and call["model"] == pz.settings.claude_model
    assert "Moin Sabine" in call["messages"][0]["content"]
    assert profile["tone"] == "du" and profile["greeting"] == "Moin" and profile["sign_off"] == "LG"
    assert profile["signature"] == ""  # eine Zeile stand in keiner Mail: ganze Signatur verworfen
    assert b.style_samples_json == "" and b.style_updated_at is not None
    # frisch: kein weiterer Aufruf
    style.ensure_profile(b)
    assert len(fake_claude.calls) == 1


def test_profile_parse_rejects_junk():
    p = style.parse_profile('{"anrede":"ihr","greeting":"Yo","sign_off":"Ruf an: 0171 123","signature":"Thomas","formality":"x"}', ["Gruß Thomas"])
    assert p["tone"] == "sie" and p["greeting"] == "Guten Tag" and p["sign_off"] == "Viele Grüße" and p["signature"] == "Thomas"
    assert style.parse_profile("kein json", []) is None


def test_profile_failure_keeps_samples(fake_claude):
    b = _business(style_samples_json=json.dumps(["Hallo Herr A, anbei das Angebot wie besprochen."] * 1 + ["Guten Tag Frau B, hier die Rechnung wie besprochen."] + ["Hallo Herr C, Termin passt so wie besprochen."]))
    fake_claude.replies = [RuntimeError("overloaded")]
    assert style.ensure_profile(b) is None
    assert len(json.loads(b.style_samples_json)) == 3


# --- Generierung und Prüfung --------------------------------------------------------------

def test_happy_path_offer(fake_claude):
    doc = _doc(business=_business(include_view_link=True))
    fake_claude.replies = [GOOD_OFFER_1]
    body = pz.generate(doc, 1, SIE_PROFILE)
    assert body is not None
    assert body.startswith("Guten Tag Herr Weber,\n\nvor ein paar Tagen")
    assert "Viele Grüße\nThomas Müller\n\n" not in body  # Grußformel von Claude abgeschnitten
    assert texts._link(doc).strip() in body
    assert body.endswith("Beste Grüße\nThomas Müller\nMüller Haustechnik GmbH\n0171 2345678")
    assert "temperature" not in fake_claude.calls[0]
    prompt = fake_claude.calls[0]["messages"][0]["content"]
    assert "A-2026-118" in prompt and "3.724,70 €" in prompt and "Guten Tag Herr Weber," in prompt


def test_du_link_and_own_signature(fake_claude):
    b = _business(include_view_link=True, signature="Thomas\nMüller Haustechnik")
    doc = _doc(customer="Peter Weber", business=b)
    fake_claude.replies = ["Moin Peter,\n\nhast du dir unser Angebot A-2026-118 schon anschauen können? Wenn du Fragen hast, meld dich einfach bei mir."]
    body = pz.generate(doc, 1, DU_PROFILE)
    assert body is not None and "Hier kannst du das Angebot" in body
    assert body.endswith("LG\nThomas\nMüller Haustechnik")


@pytest.mark.parametrize("bad,reason", [
    ("Guten Tag Herr Weber,\n\nich wollte kurz nachhören, ob unser Angebot angekommen ist und ob noch Fragen offen sind.", "fehlt"),
    ("Guten Tag Herr Weber,\n\n" + "ich melde mich wegen Angebot A-2026-118 und wollte fragen ob alles passt. " * 8, "zu lang"),
    ("Guten Tag Herr Weber,\n\nunser Angebot A-2026-118 – Sie erinnern sich – liegt noch bei Ihnen. Haben Sie noch Fragen?", "Gedankenstrich"),
    ("Guten Tag Herr Weber,\n\nzu unserem Angebot A-2026-118: Bei Auftrag bis Freitag gebe ich Ihnen 250,00 € Nachlass. Haben Sie Fragen?", "erfundene Zahl"),
    ("Guten Tag Herr Weber,\n\nhaben Sie unser Angebot A-2026-118 erhalten, und ist alles klar? Ich freue mich auf Ihre Rückmeldung.", "Komma vor"),
    ("Guten Tag Herr Weber,\n\nunser Angebot A-2026-118 wartet! Jetzt zugreifen!! Melden Sie sich bei Fragen.", "Ausrufezeichen"),
    ("Guten Tag Herr Weber,\n\nunser Angebot A-2026-118 ist da 😊 Melden Sie sich bei Fragen gern bei mir.", "Emoji"),
    ("Hallo Peter,\n\nhast du unser Angebot A-2026-118 bekommen? Melde dich bei Fragen gern bei mir.", "erste Zeile"),
    ("Guten Tag Herr Weber,\n\nhast du unser Angebot A-2026-118 bekommen? Melde dich bei Fragen gern bei mir.", "duzt"),
    ("Guten Tag Herr Weber,\n\nunser Angebot A-2026-118 ist das beste Angebot, mit Preisgarantie für Sie. Melden Sie sich bei Fragen.", "Werbung"),
    ("Guten Tag Herr Weber,\n\nich melde mich nächste Woche noch einmal wegen unseres Angebots A-2026-118 bei Ihnen.", "Datum"),
    ("Guten Tag Herr Weber,\n\nI wanted to follow up on offer A-2026-118 and ask whether you have any questions, please let me know.", "deutsch"),
])
def test_validate_rejects(bad, reason):
    ctx = pz.build_context(_doc(), 1, SIE_PROFILE)
    problems = pz.validate(pz.clean_output(bad, ctx), ctx)
    assert any(reason in p for p in problems), problems


def test_validate_accepts_good_text():
    ctx = pz.build_context(_doc(), 1, SIE_PROFILE)
    assert pz.validate(pz.clean_output(GOOD_OFFER_1, ctx), ctx) == []


def test_invoice_step_rules():
    inv = _doc(kind=DocKind.INVOICE, document_number="R-2026-301", amount_gross=224.91, due_date=NOW)
    ctx3 = pz.build_context(inv, 3, SIE_PROFILE)
    no_mahn = "Guten Tag Herr Weber,\n\nunsere Rechnung R-2026-301 über 224,91 € ist weiter offen. Bitte überweisen Sie den Betrag innerhalb von 7 Tagen."
    assert any("Mahnverfahren" in p for p in pz.validate(no_mahn, ctx3))
    ok3 = no_mahn + " Danach muss ich das Mahnverfahren einleiten."
    assert pz.validate(ok3, ctx3) == []
    ctx1 = pz.build_context(inv, 1, SIE_PROFILE)
    early = "Guten Tag Herr Weber,\n\nunsere Rechnung R-2026-301 ist offen. Sonst folgt das Mahnverfahren."
    assert any("zu früh" in p for p in pz.validate(early, ctx1))
    # Frist "7" ist erst ab Schritt 2 ein bekannter Fakt
    assert any("erfundene Zahl" in p for p in pz.validate("Guten Tag Herr Weber,\n\nunsere Rechnung R-2026-301 ist offen. Bitte zahlen Sie in 7 Tagen.", ctx1))


def test_rejected_twice_falls_back_to_template(fake_claude):
    doc = _doc()
    fake_claude.replies = [
        "Guten Tag Herr Weber,\n\nunser Angebot – wie besprochen – liegt bei Ihnen.",
        "Guten Tag Herr Weber,\n\nunser Angebot liegt bei Ihnen. Melden Sie sich bis Freitag.",
    ]
    doc.business.style_json = json.dumps(SIE_PROFILE)
    doc.business.style_updated_at = NOW
    body = pz.body_for(doc, 1)
    assert body == texts.body_for(doc, 1)
    assert len(fake_claude.calls) == 2
    assert "abgelehnt" in fake_claude.calls[1]["messages"][0]["content"]


def test_retry_succeeds(fake_claude):
    doc = _doc()
    fake_claude.replies = ["Guten Tag Herr Weber,\n\nunser Angebot – wie besprochen – liegt bei Ihnen.", GOOD_OFFER_1]
    body = pz.body_for(doc, 1)
    assert body != texts.body_for(doc, 1) and "A-2026-118" in body


def test_claude_exception_falls_back(fake_claude):
    doc = _doc()
    fake_claude.replies = [RuntimeError("API down")]
    assert pz.body_for(doc, 2) == texts.body_for(doc, 2)


def test_no_key_or_toggle_off_uses_template(fake_claude, monkeypatch):
    doc = _doc(business=_business(personalize=False))
    assert pz.body_for(doc, 1) == texts.body_for(doc, 1)
    monkeypatch.setattr(pz.settings, "anthropic_api_key", None)
    doc.business.personalize = True
    assert pz.body_for(doc, 1) == texts.body_for(doc, 1)
    assert fake_claude.calls == []


def test_prepare_uses_personalized_body(fake_claude):
    from locadas.engine import followups

    doc = _doc()
    fake_claude.replies = [GOOD_OFFER_1]
    fu = SimpleNamespace(document=doc, step=1, subject="", body="")
    followups._prepare(fu)
    assert fu.subject == "AW: Angebot Gastherme"
    assert fu.body.startswith("Guten Tag Herr Weber,\n\nvor ein paar Tagen")


def test_init_db_adds_new_columns_to_existing_sqlite(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from locadas import db as dbmod

    url = f"sqlite:///{tmp_path / 'old.db'}"
    dbmod.reset_engine_for_tests(url)
    with dbmod.get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO businesses (name, owner_name, email, signature, phone, followup_offers, followup_invoices, paused, notify_opened, "
            "include_view_link, approvals_done, auto_mode, personalize, style_json, style_samples_json, created_at) "
            "VALUES ('A', '', 'a@b.de', '', '', 1, 1, 0, 1, 1, 0, 0, 1, '', '', '2026-01-01 00:00:00')"
        ))
        for col in ("personalize", "style_json", "style_updated_at", "style_samples_json"):
            conn.execute(text(f"ALTER TABLE businesses DROP COLUMN {col}"))
    dbmod.reset_engine_for_tests(url)
    cols = {c["name"] for c in inspect(create_engine(url)).get_columns("businesses")}
    assert {"personalize", "style_json", "style_updated_at", "style_samples_json"} <= cols
    with dbmod.new_session() as s:
        b = s.query(dbmod.Business).one()
        assert b.personalize is True and b.style_json == ""


def test_case_fix_and_unfinished_sentence():
    ctx = pz.build_context(_doc(), 2, SIE_PROFILE)
    t = pz.clean_output("Guten Tag Herr Weber,\n\nIch melde mich wegen Angebot A-2026-118.\n\nich freue mich über eine kurze Rückmeldung.", ctx)
    assert t == "Guten Tag Herr Weber,\n\nich melde mich wegen Angebot A-2026-118.\n\nIch freue mich über eine kurze Rückmeldung."
    assert pz.validate(t, ctx) == []
    t2 = "Guten Tag Herr Weber,\n\nich melde mich wegen Angebot A-2026-118.\n\nEine kurze Rückmeldung reicht mir"
    assert any("Satzzeichen" in p for p in pz.validate(t2, ctx))
