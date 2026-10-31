"""Kundenmails ohne Thread-Bezug zuordnen und Antworten einordnen (Claude gemockt, kein Netz)."""
from __future__ import annotations

import email
import email.policy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from locadas.config import settings
from locadas.db import Business, DocKind, DocStatus, Document, Event, new_session
from locadas.engine import accounts as acc
from locadas.engine import matcher
from locadas.engine.scanner import scan_account
from locadas.mail.connect import connect_mailbox
from tests.conftest import PASSWORD, USER
from tests.fakemail import make_mail


class FakeClaude:
    """Ersetzt anthropic.Anthropic. `reply` ist ein dict (wird JSON) oder eine Exception."""

    def __init__(self):
        self.calls: list[dict] = []
        self.reply: dict | Exception = {}

    def factory(self, **kw):
        outer = self

        class _Messages:
            def create(self, **call):
                outer.calls.append(call)
                if isinstance(outer.reply, Exception):
                    raise outer.reply
                return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(outer.reply, ensure_ascii=False))])

        return SimpleNamespace(messages=_Messages())

    @property
    def prompts(self) -> list[str]:
        return [c["messages"][0]["content"] for c in self.calls]


@pytest.fixture()
def claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", fake.factory)
    return fake


def _setup(s, imap, smtp):
    cfg = connect_mailbox(USER, PASSWORD, imap_host="127.0.0.1", imap_port=imap.port, smtp_host="127.0.0.1", smtp_port=smtp.port, imap_ssl=False, smtp_ssl=False)
    b = Business(name="Müller Haustechnik GmbH", owner_name="Thomas Müller", email=USER)
    s.add(b)
    s.flush()
    account = acc.create_account(s, b, cfg, PASSWORD)
    s.commit()
    scan_account(s, account)  # Erstlauf: Posteingang ab jetzt
    return b, account


def _doc(s, b, account, kind, name, addr, title, number, days_ago, amount=1000.0) -> Document:
    d = Document(
        business_id=b.id, account_id=account.id, kind=kind, status=DocStatus.OPEN,
        message_id=f"<{number}@mueller-shk.de>", subject=f"{'Angebot' if kind == DocKind.OFFER else 'Rechnung'} {title}",
        sent_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        customer_name=name, customer_email=addr, document_number=number, title=title, amount_gross=amount,
    )
    s.add(d)
    s.commit()
    return d


def _notifications(smtp):
    out = []
    for _, rcpts, raw in smtp.messages:
        if rcpts == [USER]:
            m = email.message_from_bytes(raw, policy=email.policy.default)
            out.append((str(m["Subject"]), m.get_content()))
    return out


def _with_header(raw: bytes, header: str) -> bytes:
    return header.encode() + b"\n" + raw


def test_context_free_mail_from_wife_is_matched(fresh_db, imap, smtp, claude):
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        kueche = _doc(s, b, account, DocKind.OFFER, "Familie Wolff", "thomas.wolff@gmx.de", "Einbauküche Wolff", "A-2026-140", 10, 18450.0)
        bad = _doc(s, b, account, DocKind.OFFER, "Hausverwaltung Rhein GmbH", "m.becker@hv-rhein.de", "Strangsanierung Königsallee 44", "A-2026-141", 5)
        claude.reply = {"doc_id": kueche.id, "confidence": 0.9, "intent": "question", "summary": "Familie Wolff fragt, wann es mit der Küche losgeht."}
        imap.store.append("INBOX", make_mail("petra.wolff@web.de", USER, "Küche", "Ey, was ist denn jetzt mit der Küche?\n\nLG Petra", from_name="Petra Wolff"))
        assert scan_account(s, account)["replies"] == 1
        s.expire_all()
        kueche, bad = s.get(Document, kueche.id), s.get(Document, bad.id)
        assert kueche.status == DocStatus.REPLIED and bad.status == DocStatus.OPEN
        assert kueche.reply_intent == "question" and "Küche" in kueche.reply_summary
        # Ein Aufruf, aktuelles Modell, ohne temperature, mit Mailtext und beiden Dokumenten
        assert len(claude.calls) == 1
        call = claude.calls[0]
        assert call["model"] == settings.claude_fast_model and "temperature" not in call
        assert "was ist denn jetzt mit der Küche" in claude.prompts[0]
        assert f"id={kueche.id}" in claude.prompts[0] and f"id={bad.id}" in claude.prompts[0]
        assert imap.text_fetches  # Text nur bei Bedarf geholt
        ev = s.scalars(select(Event).where(Event.document_id == kueche.id, Event.kind == "reply")).one()
        assert "wann es mit der Küche losgeht" in ev.text
        [(subject, body)] = _notifications(smtp)
        assert "hat geantwortet" in subject
        assert "» Familie Wolff fragt, wann es mit der Küche losgeht." in body
        assert "petra.wolff@web.de" in body


def test_low_confidence_does_not_close(fresh_db, imap, smtp, claude):
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        kueche = _doc(s, b, account, DocKind.OFFER, "Familie Wolff", "thomas.wolff@gmx.de", "Einbauküche", "A-1", 10)
        claude.reply = {"doc_id": kueche.id, "confidence": 0.5, "intent": "question", "summary": "Jemand fragt nach einem Termin."}
        imap.store.append("INBOX", make_mail("someone@web.de", USER, "Termin", "Wann hätten Sie Zeit für Wolff?"))
        assert scan_account(s, account)["replies"] == 0
        s.expire_all()
        assert s.get(Document, kueche.id).status == DocStatus.OPEN
        assert len(claude.calls) == 1 and _notifications(smtp) == []


def test_two_open_docs_same_customer_goes_to_right_one(fresh_db, imap, smtp, claude):
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        bad = _doc(s, b, account, DocKind.OFFER, "Karin Meier", "k.meier@web.de", "Badsanierung OG", "A-7", 20, 23800.0)
        wartung = _doc(s, b, account, DocKind.INVOICE, "Karin Meier", "k.meier@web.de", "Wartung Heizung", "R-9", 2, 224.91)
        claude.reply = {"doc_id": bad.id, "confidence": 0.85, "intent": "question", "summary": "Frau Meier fragt, ob die Dusche bodengleich geht."}
        imap.store.append("INBOX", make_mail("k.meier@web.de", USER, "Rückfrage zum Angebot Bad", "Geht die Dusche auch bodengleich?"))
        assert scan_account(s, account)["replies"] == 1
        s.expire_all()
        # Ohne Claude hätte die neuere Rechnung gegriffen
        assert s.get(Document, bad.id).status == DocStatus.REPLIED
        assert s.get(Document, wartung.id).status == DocStatus.OPEN


@pytest.mark.parametrize("kind,intent,expected", [
    (DocKind.OFFER, "accept", DocStatus.WON),
    (DocKind.OFFER, "decline", DocStatus.LOST),
    (DocKind.INVOICE, "payment_done", DocStatus.PAID),
    (DocKind.OFFER, "payment_done", DocStatus.REPLIED),
    (DocKind.INVOICE, "accept", DocStatus.REPLIED),
    (DocKind.OFFER, "later", DocStatus.REPLIED),
])
def test_intent_mapping(fresh_db, imap, smtp, claude, kind, intent, expected):
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        d = _doc(s, b, account, kind, "Peter Weber", "p.weber@web.de", "Gastherme", "X-1", 5)
        claude.reply = {"doc_id": d.id, "confidence": 0.95, "intent": intent, "summary": "Herr Weber meldet sich."}
        imap.store.append("INBOX", make_mail("weber.privat@t-online.de", USER, "Gastherme", "Text"))
        scan_account(s, account)
        s.expire_all()
        assert s.get(Document, d.id).status == expected
        assert s.get(Document, d.id).reply_intent == intent


def test_thread_reply_gets_intent_without_matching(fresh_db, imap, smtp, claude):
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        d = _doc(s, b, account, DocKind.OFFER, "Familie Schmidt", "familie.schmidt@gmx.de", "Gastherme", "A-118", 5)
        other = _doc(s, b, account, DocKind.OFFER, "Hans Kurz", "h.kurz@web.de", "Dach", "A-119", 5)
        claude.reply = {"doc_id": None, "confidence": 0.2, "intent": "accept", "summary": "Familie Schmidt erteilt den Auftrag."}
        imap.store.append("INBOX", make_mail("familie.schmidt@gmx.de", USER, "AW: Angebot Gastherme", "Passt, machen wir!\n\n> Anbei unser Angebot", in_reply_to=d.message_id))
        assert scan_account(s, account)["replies"] == 1
        s.expire_all()
        assert s.get(Document, d.id).status == DocStatus.WON
        assert len(claude.calls) == 1
        assert f"id={d.id}" in claude.prompts[0] and f"id={other.id}" not in claude.prompts[0]
        assert "Anbei unser Angebot" not in claude.prompts[0]  # Zitat abgeschnitten
        [(subject, body)] = _notifications(smtp)
        assert "hat zugesagt" in subject and "Auftrag bekommen" in body


def test_claude_failure_falls_back_to_old_behavior(fresh_db, imap, smtp, claude):
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        a = _doc(s, b, account, DocKind.OFFER, "Familie Schmidt", "familie.schmidt@gmx.de", "Gastherme", "A-1", 5)
        c = _doc(s, b, account, DocKind.INVOICE, "Peter Weber", "p.weber@web.de", "Wartung", "R-1", 5)
        e = _doc(s, b, account, DocKind.OFFER, "Hans Kurz", "h.kurz@web.de", "Dach", "A-2", 5)
        claude.reply = RuntimeError("overloaded")
        imap.store.append("INBOX", make_mail("familie.schmidt@gmx.de", USER, "AW: Angebot", "Ja bitte!", in_reply_to=a.message_id))  # Thread
        imap.store.append("INBOX", make_mail("p.weber@web.de", USER, "Zahlung", "Ist überwiesen."))  # exakte Kundenadresse
        imap.store.append("INBOX", make_mail("frau.kurz@gmx.de", USER, "Dach", "Wann kommen Sie?"))  # unbekannt
        assert scan_account(s, account)["replies"] == 2
        s.expire_all()
        assert s.get(Document, a.id).status == DocStatus.REPLIED and s.get(Document, a.id).reply_summary == ""
        assert s.get(Document, c.id).status == DocStatus.REPLIED
        assert s.get(Document, e.id).status == DocStatus.OPEN
        assert len(claude.calls) == 3
        assert all("hat geantwortet" in subj for subj, _ in _notifications(smtp))


def test_known_customer_unrelated_per_claude_still_stops_but_keeps_status(fresh_db, imap, smtp, claude):
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        d = _doc(s, b, account, DocKind.OFFER, "Peter Weber", "p.weber@web.de", "Gastherme", "A-1", 5)
        claude.reply = {"doc_id": None, "confidence": 0.8, "intent": "accept", "summary": "Herr Weber bedankt sich für die Weihnachtskarte."}
        imap.store.append("INBOX", make_mail("p.weber@web.de", USER, "Danke", "Danke für die Karte!"))
        assert scan_account(s, account)["replies"] == 1
        s.expire_all()
        d = s.get(Document, d.id)
        assert d.status == DocStatus.REPLIED and "Weihnachtskarte" in d.reply_summary


def test_newsletter_and_own_mails_skip_claude(fresh_db, imap, smtp, claude):
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        d = _doc(s, b, account, DocKind.OFFER, "Familie Wolff", "thomas.wolff@gmx.de", "Küche", "A-1", 5)
        claude.reply = {"doc_id": d.id, "confidence": 0.99, "intent": "accept", "summary": "x"}
        imap.store.append("INBOX", _with_header(make_mail("angebote@grosshandel.de", USER, "Küchen-Aktion", "Jetzt zugreifen"), "List-Unsubscribe: <mailto:off@grosshandel.de>"))
        imap.store.append("INBOX", _with_header(make_mail("info@baumarkt.de", USER, "Wochenangebote", "Rabatt"), "Precedence: bulk"))
        imap.store.append("INBOX", make_mail("noreply@paypal.de", USER, "Zahlung erhalten", "Sie haben Geld erhalten"))
        imap.store.append("INBOX", make_mail("kollege@mueller-shk.de", USER, "Küche Wolff", "Wann fahren wir hin?"))
        imap.store.append("INBOX", make_mail("thomas.wolff@gmx.de", USER, "Abwesenheitsnotiz", "Bin im Urlaub"))
        assert scan_account(s, account)["replies"] == 0
        s.expire_all()
        assert s.get(Document, d.id).status == DocStatus.OPEN
        assert claude.calls == []


def test_no_key_behaves_like_before(fresh_db, imap, smtp, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        d = _doc(s, b, account, DocKind.OFFER, "Familie Wolff", "thomas.wolff@gmx.de", "Küche", "A-1", 5)
        imap.store.append("INBOX", make_mail("petra.wolff@web.de", USER, "Küche", "Was ist mit der Küche?"))
        assert scan_account(s, account)["replies"] == 0
        assert imap.text_fetches == []
        imap.store.append("INBOX", make_mail("thomas.wolff@gmx.de", USER, "Küche", "Was ist mit der Küche?"))
        assert scan_account(s, account)["replies"] == 1
        s.expire_all()
        assert s.get(Document, d.id).status == DocStatus.REPLIED


def test_no_open_docs_no_call(fresh_db, imap, smtp, claude):
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        imap.store.append("INBOX", make_mail("petra.wolff@web.de", USER, "Küche", "Was ist mit der Küche?"))
        assert scan_account(s, account)["replies"] == 0
        assert claude.calls == []


def test_clean_body_and_parse():
    body = "Passt so.\n\nGesendet von meinem iPhone\n\nAm 3.11.2026 um 10:00 schrieb Müller <info@x.de>:\n> alt"
    assert matcher.clean_body(body) == "Passt so."
    html = "<html><body><p>Hallo,</p><p>wann geht&#39;s los?</p><style>p{}</style></body></html>"
    assert "wann geht's los?" in matcher.clean_body(html)
    assert len(matcher.clean_body("x" * 10_000)) == matcher.MAX_BODY_CHARS
    r = matcher.parse_result('```json\n{"doc_id": "7", "confidence": 1.4, "intent": "kaufen", "summary": "  a  b "}\n```', {7})
    assert r.doc_id == 7 and r.confidence == 1.0 and r.intent == "other" and r.summary == "a b"
    assert matcher.parse_result('{"doc_id": 99, "confidence": 0.9, "intent": "accept", "summary": ""}', {7}).doc_id is None
    assert matcher.parse_result("kein json", {7}) is None


def test_init_db_adds_new_columns_to_existing_sqlite(tmp_path):
    import sqlite3

    from locadas import db as dbmod

    path = tmp_path / "old.db"
    dbmod.reset_engine_for_tests(f"sqlite:///{path}")
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE documents DROP COLUMN reply_intent")
    con.execute("ALTER TABLE documents DROP COLUMN reply_summary")
    con.commit()
    con.close()
    dbmod.reset_engine_for_tests(f"sqlite:///{path}")
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
    con.close()
    assert {"reply_intent", "reply_summary"} <= cols
