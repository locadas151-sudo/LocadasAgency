"""Erkennung und Extraktion ohne Netz: Heuristik, Claude-JSON-Parsing, Angebots-Check-Parsing, Texte."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from locadas.db import DocKind
from locadas.extract import fields as fx
from locadas.extract.check import parse_check_json
from locadas.extract.classify import classify, looks_like_customer

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("subject,files,text,expected", [
    ("Angebot Badsanierung", ["Angebot_118.pdf"], "", DocKind.OFFER),
    ("Ihre Rechnung", ["R-2026-301.pdf"], "", DocKind.INVOICE),
    ("Unterlagen", ["doc.pdf"], "Kostenvoranschlag Nr. 5 ...", DocKind.OFFER),
    ("Unterlagen", ["doc.pdf"], "Schlussrechnung Nr. 5 ...", DocKind.INVOICE),
    ("Mahnung zur Rechnung 301", ["Mahnung.pdf"], "", None),
    ("Lieferschein", ["LS.pdf"], "Lieferschein", None),
    ("Auftragsbestätigung", ["AB.pdf"], "", None),
    ("Rechnung zu Angebot 118", ["Rechnung_301.pdf"], "", DocKind.INVOICE),
    ("Fotos", ["bad.pdf"], "Hier die Bilder von der Baustelle", None),
])
def test_classify(subject, files, text, expected):
    assert classify(subject, files, text) == expected


def test_looks_like_customer():
    assert looks_like_customer("kunde@gmx.de", "info@betrieb.de")
    assert not looks_like_customer("info@betrieb.de", "info@betrieb.de")
    assert not looks_like_customer("noreply@shop.de", "info@betrieb.de")


def test_heuristic_offer():
    text = "Angebot Nr. A-2026-118\nPos 1 ... 1.200,00 €\nNetto 3.130,00 €\nMwSt 594,70 €\nGesamtbetrag 3.724,70 €\nDieses Angebot ist gültig bis 26.11.2026"
    r = fx.heuristic_extract(DocKind.OFFER, "Angebot Gastherme", [("Familie Schmidt", "fs@gmx.de")], NOW, text)
    assert r.amount_gross == 3724.70
    assert r.document_number == "A-2026-118"
    assert r.valid_until == "2026-11-26"
    assert r.customer_name == "Familie Schmidt" and r.customer_email == "fs@gmx.de"
    assert r.title == "Gastherme"


def test_heuristic_invoice_due_from_days_and_name_from_email():
    text = "Rechnung Nr. 301\nRechnungsbetrag 224,91 €\nZahlbar innerhalb von 10 Tagen"
    r = fx.heuristic_extract(DocKind.INVOICE, "Rechnung", [("", "peter.weber@web.de")], NOW, text)
    assert r.due_date == "2026-11-09"
    assert r.customer_name == "Peter Weber"


def test_heuristic_invoice_default_due():
    r = fx.heuristic_extract(DocKind.INVOICE, "Rechnung", [("", "x@y.de")], NOW, "Rechnung 1 Summe 100,00")
    assert r.due_date == "2026-11-13"
    assert r.amount_gross == 100.0


def test_parse_claude_json_tolerates_markdown_and_strings():
    raw = '```json\n{"kind":"invoice","customer_name":"Peter Weber","amount_gross":"1.234,50","due_date":"2026-11-03","confidence":0.9}\n```'
    r = fx.parse_claude_json(raw, DocKind.OFFER)
    assert r.kind == "invoice" and r.amount_gross == 1234.5 and r.due_date == "2026-11-03" and r.source == "claude"
    assert fx.parse_claude_json("kein json", None) is None


def test_extract_fields_uses_claude_and_fills_gaps(monkeypatch):
    monkeypatch.setattr(fx.settings, "anthropic_api_key", "test")

    class FakeMessages:
        def create(self, **kw):
            assert "Angebot Gastherme" in kw["messages"][0]["content"]
            return SimpleNamespace(content=[SimpleNamespace(text='{"kind":"offer","customer_name":"Fam. Schmidt","document_number":"A-118","title":"Gastherme","amount_gross":3724.7,"confidence":0.95}')])

    class FakeClient:
        def __init__(self, **kw):
            self.messages = FakeMessages()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    r = fx.extract_fields(DocKind.OFFER, "Angebot Gastherme", [("", "fs@gmx.de")], NOW, "Angebot ... Gesamtbetrag 3.724,70 €")
    assert r.source == "claude" and r.customer_name == "Fam. Schmidt" and r.customer_email == "fs@gmx.de" and r.amount_gross == 3724.7


def test_extract_fields_falls_back_when_claude_fails(monkeypatch):
    monkeypatch.setattr(fx.settings, "anthropic_api_key", "test")

    class Boom:
        def __init__(self, **kw):
            self.messages = self

        def create(self, **kw):
            raise RuntimeError("down")

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", Boom)
    r = fx.extract_fields(DocKind.INVOICE, "Rechnung 7", [("", "a@b.de")], NOW, "Rechnungsbetrag 99,00 €")
    assert r.source == "heuristic" and r.amount_gross == 99.0


def test_parse_check_json():
    r = parse_check_json('{"summary":"S","score":"72","present":["a"],"missing":[],"risks":["r"],"questions":["q1","q2"],"amount_gross":1500}')
    assert r.score == 72 and r.present == ["a"] and r.amount_gross == 1500.0 and r.questions == ["q1", "q2"]


def test_texts_anrede_and_subject():
    from locadas.engine import texts

    b = SimpleNamespace(name="Müller GmbH", owner_name="Thomas Müller", phone="0171", signature="")
    doc = SimpleNamespace(kind=DocKind.OFFER, customer_name="Herr Weber", document_number="A-1", title="Bad", subject="Angebot Bad", sent_at=NOW, amount_gross=1000.0, business=b, valid_until=None, due_date=None, kind_label="Angebot")
    body = texts.body_for(doc, 1)
    assert body.startswith("Guten Tag Herr Weber,") and "A-1" in body and body.rstrip().endswith("0171")
    assert texts.subject_for(doc, 1) == "Angebot Bad"
    doc.customer_name = "Bau GmbH & Co. KG"
    assert texts.body_for(doc, 2).startswith("Guten Tag,")
    doc.kind = DocKind.INVOICE
    doc.due_date = NOW
    doc.kind_label = "Rechnung"
    assert "1.000,00 €" in texts.body_for(doc, 3) and "Mahnverfahren" in texts.body_for(doc, 3)
