"""WhatsApp-Sekretär: Routing, 24h-Fenster, Webhook, Knöpfe, Idempotenz, Nummern. Ohne Netzwerk (Graph API gemockt)."""
from __future__ import annotations

import email
import email.policy
import hashlib
import hmac
import itertools
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from locadas.config import settings
from locadas.db import Business, DocStatus, Document, Event, FollowUp, FollowUpStatus, WhatsAppInbound, new_session, utcnow
from locadas.engine import followups as fu
from locadas.engine.scanner import scan_account
from locadas.notify import Notification, messages, send, tokens
from locadas.notify import whatsapp as wa
from locadas.notify.phone import normalize_phone
from tests.test_e2e import WEEKDAY_10, _seed_sent, _setup

NUM = "491712345678"
SECRET = "app-secret-test"
_ids = itertools.count(1)


class Graph:
    """Fängt alle Aufrufe an die Graph API ab."""

    def __init__(self):
        self.calls: list[dict] = []
        self.fail = False

    def __call__(self, url, payload, token):
        if self.fail:
            raise wa.WhatsAppError("kaputt")
        assert url == f"https://graph.facebook.com/v21.0/PNID/messages"
        assert token == "tok"
        self.calls.append(payload)
        return {"messages": [{"id": f"wamid.out{len(self.calls)}"}]}

    @property
    def last(self) -> dict:
        return self.calls[-1]

    def texts(self) -> list[str]:
        out = []
        for c in self.calls:
            if c["type"] == "text":
                out.append(c["text"]["body"])
            elif c["type"] == "interactive":
                out.append(c["interactive"]["body"]["text"])
        return out


@pytest.fixture()
def graph(monkeypatch):
    g = Graph()
    monkeypatch.setattr(wa, "http_post", g)
    monkeypatch.setattr(settings, "whatsapp_token", "tok")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "PNID")
    monkeypatch.setattr(settings, "whatsapp_app_secret", SECRET)
    monkeypatch.setattr(settings, "whatsapp_verify_token", "verify-me")
    return g


def _business(s, imap, smtp, window: bool = True):
    _seed_sent(imap)
    b, account = _setup(s, imap, smtp)
    scan_account(s, account)
    b.whatsapp_number = NUM
    b.whatsapp_opt_in = True
    b.whatsapp_last_inbound_at = utcnow() - timedelta(hours=1) if window else None
    s.commit()
    offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
    invoice = s.scalars(select(Document).where(Document.document_number == "R-2026-301")).one()
    return b, offer, invoice


def _hook(client, msg: dict, secret: str = SECRET, sig: str | None = None):
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "contacts": [{"wa_id": msg.get("from", NUM)}], "messages": [msg],
        }}]}],
    }).encode()
    if sig is None:
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def _press(client, pl: str, template: bool = False, wamid: str | None = None):
    msg = {"from": NUM, "id": wamid or f"wamid.in{next(_ids)}", "timestamp": "1"}
    if template:
        msg.update(type="button", button={"payload": pl, "text": "x"})
    else:
        msg.update(type="interactive", interactive={"type": "button_reply", "button_reply": {"id": pl, "title": "x"}})
    return _hook(client, msg)


def _say(client, text: str):
    return _hook(client, {"from": NUM, "id": f"wamid.in{next(_ids)}", "type": "text", "text": {"body": text}})


def _client():
    from locadas.main import app

    return TestClient(app)


# --- Nummern ------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("0171 2345678", "491712345678"),
    ("0171/234 56 78", "491712345678"),
    ("+49 171 2345678", "491712345678"),
    ("+49 (0)171 2345678", "491712345678"),
    ("0049 171-2345678", "491712345678"),
    ("491712345678", "491712345678"),
    ("+43 664 1234567", "436641234567"),
    ("", ""),
    ("171 2345678", None),
    ("0171", None),
    ("ruf mich an", None),
])
def test_normalize_german_numbers(raw, expected):
    assert normalize_phone(raw) == expected


# --- Routing ----------------------------------------------------------------------------

def test_routing_whatsapp_vs_email(fresh_db, imap, smtp, graph, monkeypatch):
    with new_session() as s:
        b, offer, _ = _business(s, imap, smtp)
        n_mail = len(smtp.messages)

        n = messages.opened(offer, "Betreff", "Mailtext")
        assert send(s, n) and n.channel == "whatsapp"
        assert len(graph.calls) == 1 and len(smtp.messages) == n_mail

        b.whatsapp_opt_in = False  # ohne Opt-in: Mail
        n = messages.opened(offer, "Betreff", "Mailtext")
        assert send(s, n) and n.channel == "email"
        assert len(graph.calls) == 1 and len(smtp.messages) == n_mail + 1

        b.whatsapp_opt_in = True
        monkeypatch.setattr(settings, "whatsapp_token", None)  # Server ohne WhatsApp: Mail
        n = messages.opened(offer, "Betreff", "Mailtext")
        assert send(s, n) and n.channel == "email"
        monkeypatch.setattr(settings, "whatsapp_token", "tok")

        graph.fail = True  # Graph API kaputt: Mail als Rückfall
        n = messages.opened(offer, "Betreff", "Mailtext")
        assert send(s, n) and n.channel == "email"
        assert len(smtp.messages) == n_mail + 3
        m = email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default)
        assert m["Subject"] == "Betreff" and m.get_content().strip() == "Mailtext"


def test_interactive_inside_window_template_outside(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, offer, _ = _business(s, imap, smtp, window=True)
        send(s, messages.opened(offer, "S", "B"))
        p = graph.last
        assert p["type"] == "interactive" and p["to"] == NUM
        btns = p["interactive"]["action"]["buttons"]
        assert [x["reply"]["title"] for x in btns] == ["Jetzt anrufen", "Erledigt"]
        assert tokens.parse(btns[0]["reply"]["id"]) == ("call", [offer.id])
        assert len(p["interactive"]["body"]["text"]) <= 1024

        b.whatsapp_last_inbound_at = utcnow() - timedelta(hours=25)
        send(s, messages.opened(offer, "S", "B"))
        p = graph.last
        assert p["type"] == "template"
        t = p["template"]
        assert t["name"] == "locadas_opened" and t["language"]["code"] == "de"
        params = [x["text"] for x in t["components"][0]["parameters"]]
        assert params == ["Thomas", "Familie Schmidt", "Angebot A-2026-118"]
        qr = [c for c in t["components"] if c["type"] == "button"]
        assert [c["index"] for c in qr] == ["0", "1"] and tokens.parse(qr[1]["parameters"][0]["payload"]) == ("done", [offer.id])

        # Login hat keine Vorlage: außerhalb des Fensters per Mail
        n = messages.login(b, "http://x/login/abc", "Login", "Link")
        n_calls = len(graph.calls)
        assert send(s, n) and n.channel == "email" and len(graph.calls) == n_calls


def test_template_params_are_flattened(graph):
    assert wa.template_param("Zeile 1\n\nZeile 2\t  mit    Lücken") == "Zeile 1 · Zeile 2 mit Lücken"
    assert len(wa.template_param("x" * 5000)) <= wa.PARAM_MAX


def test_notification_limits():
    with pytest.raises(ValueError):
        Notification(business=None, kind="quatsch", title="", text="")
    with pytest.raises(ValueError):
        Notification(business=None, kind="info", title="", text="", buttons=[("a", "1")] * 4)
    assert tokens.button("Ein sehr langer Knopftext hier", "ok")[0] == "Ein sehr langer Knop"
    assert tokens.parse("ok:1.falsch.sig") is None


# --- Webhook ------------------------------------------------------------------------------

def test_verify_challenge(graph):
    c = _client()
    r = c.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"})
    assert r.status_code == 200 and r.text == "12345"
    assert c.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "falsch", "hub.challenge": "1"}).status_code == 403


def test_webhook_signature(fresh_db, imap, smtp, graph):
    with new_session() as s:
        _business(s, imap, smtp, window=False)
    c = _client()
    msg = {"from": NUM, "id": "wamid.sig", "type": "text", "text": {"body": "Hallo"}}
    assert _hook(c, msg, secret="falsch").status_code == 403
    assert _hook(c, msg, sig="").status_code == 403
    assert graph.calls == []
    r = _hook(c, msg)
    assert r.status_code == 200 and r.json()["handled"] == 1
    assert "Locadas-Sekretär" in graph.texts()[-1]  # Freitext → Hilfe
    with new_session() as s:
        b = s.scalars(select(Business)).one()
        assert b.whatsapp_last_inbound_at is not None and utcnow() - b.whatsapp_last_inbound_at < timedelta(minutes=1)
    # Gleiche Nachricht noch einmal (Meta wiederholt): wird nicht doppelt verarbeitet
    n = len(graph.calls)
    assert _hook(c, msg).json()["handled"] == 0 and len(graph.calls) == n
    # Unbekannte Nummer: ignoriert
    assert _hook(c, {"from": "4915000000000", "id": "wamid.x", "type": "text", "text": {"body": "?"}}).json()["handled"] == 0


# --- Knöpfe -----------------------------------------------------------------------------------

def test_stop_button_is_idempotent(fresh_db, imap, smtp, graph):
    with new_session() as s:
        _, offer, _ = _business(s, imap, smtp)
        doc_id = offer.id
    c = _client()
    pl = tokens.payload("stop", doc_id)
    assert _press(c, pl, template=True).status_code == 200
    assert "nicht mehr nach" in graph.texts()[-1]
    with new_session() as s:
        d = s.get(Document, doc_id)
        assert d.status == DocStatus.REPLIED and d.close_reason == "Vom Betrieb gestoppt"
        assert all(f.status == FollowUpStatus.CANCELLED for f in d.followups)
        n_events = len(s.scalars(select(Event)).all())
    _press(c, pl)
    assert "schon nicht mehr" in graph.texts()[-1]
    with new_session() as s:
        assert len(s.scalars(select(Event)).all()) == n_events


@pytest.mark.parametrize("action,status,label", [
    ("won", DocStatus.WON, "Auftrag bekommen"),
    ("lost", DocStatus.LOST, "Nicht bekommen"),
    ("paid", DocStatus.PAID, "Bezahlt"),
    ("unpaid", DocStatus.DONE, "Nicht bezahlt, Betrieb kümmert sich"),
])
def test_outcome_buttons(fresh_db, imap, smtp, graph, action, status, label):
    with new_session() as s:
        _, offer, _ = _business(s, imap, smtp)
        doc_id = offer.id
    c = _client()
    _press(c, tokens.payload(action, doc_id))
    with new_session() as s:
        d = s.get(Document, doc_id)
        assert d.status == status and d.close_reason == label and d.next_followup_at is None
        assert not any(f.status == FollowUpStatus.SCHEDULED for f in d.followups)
    _press(c, tokens.payload(action, doc_id))  # zweimal gedrückt
    assert "schon notiert" in graph.texts()[-1]
    with new_session() as s:
        assert len(s.scalars(select(Event).where(Event.kind == "outcome")).all()) == 1


def test_call_ok_done_and_owner_pdf_link(fresh_db, imap, smtp, graph):
    with new_session() as s:
        _, offer, _ = _business(s, imap, smtp)
        doc_id = offer.id
    c = _client()
    _press(c, tokens.payload("call", doc_id))
    reply = graph.texts()[-1]
    assert "familie.schmidt@gmx.de" in reply and "/wa/d/" in reply
    link = reply.split("http://testserver")[1].split()[0]
    r = c.get(link)
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    assert c.get("/wa/d/kaputt").status_code == 404
    _press(c, tokens.payload("ok"))
    _press(c, tokens.payload("done", doc_id))
    assert graph.texts()[-2:] == ["Alles klar.", "Gut, abgehakt."]
    with new_session() as s:
        d = s.get(Document, doc_id)
        assert d.status == DocStatus.OPEN and d.open_count == 0  # eigener PDF-Link zählt nicht als Kunden-Öffnen


def test_reopen_after_reply(fresh_db, imap, smtp, graph):
    with new_session() as s:
        _, offer, _ = _business(s, imap, smtp)
        fu.close_document(s, offer, DocStatus.REPLIED, "Antwort")
        s.commit()
        doc_id = offer.id
    c = _client()
    _press(c, tokens.payload("reopen", doc_id))
    with new_session() as s:
        d = s.get(Document, doc_id)
        assert d.status == DocStatus.OPEN and any(f.status == FollowUpStatus.SCHEDULED for f in d.followups)
    _press(c, tokens.payload("reopen", doc_id))
    assert "schon weiter nach" in graph.texts()[-1]


def test_approval_queue_send_skip_gone(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, offer, invoice = _business(s, imap, smtp)
        for d in (offer, invoice):
            d.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        n_mail = len(smtp.messages)
        stats = fu.process_due(s, now=WEEKDAY_10)
        s.commit()
        assert stats["approval"] == 2
        assert len(smtp.messages) == n_mail  # kam per WhatsApp, nicht per Mail
        p = graph.last
        assert p["type"] == "interactive"
        assert [x["reply"]["title"] for x in p["interactive"]["action"]["buttons"]] == ["Senden", "Diesmal nicht", "Hat sich gemeldet"]
        assert p["interactive"]["footer"]["text"].startswith("1 von 2")
        first = tokens.parse(p["interactive"]["action"]["buttons"][0]["reply"]["id"])
        assert first[0] == "send"
        first_fu = s.get(FollowUp, first[1][0])
        first_doc, other_doc = first_fu.document_id, ({offer.id, invoice.id} - {first_fu.document_id}).pop()
    c = _client()
    _press(c, tokens.payload("send", first[1][0]))
    with new_session() as s:
        f = s.get(FollowUp, first[1][0])
        assert f.status == FollowUpStatus.SENT and s.get(Business, f.document.business_id).approvals_done == 1
    assert len(smtp.messages) == n_mail + 1  # die Nachfass-Mail an den Kunden
    # Danach kommt sofort die nächste Freigabe
    nxt = graph.last
    assert nxt["type"] == "interactive"
    action, (fu_id,) = tokens.parse(nxt["interactive"]["action"]["buttons"][1]["reply"]["id"])
    assert action == "skip"
    with new_session() as s:
        assert s.get(FollowUp, fu_id).document_id == other_doc
    _press(c, tokens.payload("send", first[1][0]))  # doppelt
    assert "schon raus" in graph.texts()[-2] or "schon raus" in graph.texts()[-1]
    assert len(smtp.messages) == n_mail + 1
    _press(c, tokens.payload("skip", fu_id))
    with new_session() as s:
        assert s.get(FollowUp, fu_id).status == FollowUpStatus.SKIPPED
    _press(c, tokens.payload("gone", fu_id))
    with new_session() as s:
        assert s.get(Document, other_doc).status == DocStatus.REPLIED
        assert s.get(Document, first_doc).status == DocStatus.OPEN


def test_preview_template_pick_and_stop_by_number(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, offer, invoice = _business(s, imap, smtp, window=False)
        b.auto_mode = True
        for d in (offer, invoice):
            d.needs_review = False
            d.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        n_mail = len(smtp.messages)
        assert fu.process_due(s, now=WEEKDAY_10)["announced"] == 2
        s.commit()
        assert len(smtp.messages) == n_mail
        p = graph.last
        assert p["type"] == "template" and p["template"]["name"] == "locadas_preview"
        params = [x["text"] for x in p["template"]["components"][0]["parameters"]]
        assert params[0] == "Thomas" and params[1] == "12:00" and "Familie Schmidt" in params[2]
        pick = [c for c in p["template"]["components"] if c["type"] == "button"][0]["parameters"][0]["payload"]
        assert tokens.parse(pick)[0] == "pick"
        assert all(f.announced_at is not None for d in (offer, invoice) for f in d.followups[:1])
        ids = json.loads(b.whatsapp_context)["list"]
    c = _client()
    _press(c, pick, template=True)
    p = graph.last
    assert p["type"] == "interactive"  # Fenster ist jetzt offen
    assert [x["reply"]["title"].startswith("Stopp: ") for x in p["interactive"]["action"]["buttons"]] == [True, True]
    _say(c, "Stopp 2")
    assert "nicht mehr nach" in graph.texts()[-1]
    with new_session() as s:
        assert s.get(Document, ids[1]).status == DocStatus.REPLIED
        assert s.get(Document, ids[0]).status == DocStatus.OPEN
        assert fu.process_due(s, now=WEEKDAY_10 + timedelta(hours=3))["sent"] == 1
    _say(c, "Stopp 7")
    assert "nur 2 Kunden" in graph.texts()[-1]


def test_outcome_queue(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, offer, invoice = _business(s, imap, smtp)
        for d in (offer, invoice):
            for f in d.followups:
                f.status, f.sent_at, f.message_id = FollowUpStatus.SENT, WEEKDAY_10 - timedelta(days=10), "<x@y>"
            d.next_followup_at = None
        b.whatsapp_last_inbound_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        n_mail = len(smtp.messages)
        assert fu.finish_exhausted(s, now=WEEKDAY_10) == 2
        s.commit()
        assert len(smtp.messages) == n_mail
        first = tokens.parse(graph.last["interactive"]["action"]["buttons"][0]["reply"]["id"])
    c = _client()
    _press(c, tokens.payload(first[0], *first[1]))
    second = tokens.parse(graph.last["interactive"]["action"]["buttons"][0]["reply"]["id"])
    assert second[1] != first[1]
    _press(c, tokens.payload(second[0], *second[1]))
    with new_session() as s:
        assert {s.get(Document, first[1][0]).status, s.get(Document, second[1][0]).status} == {DocStatus.WON, DocStatus.PAID}
    assert graph.last["type"] == "text"  # nichts mehr offen, keine weitere Frage


def test_bounce_new_address_by_text_and_opt_out(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, offer, _ = _business(s, imap, smtp)
        fu.close_document(s, offer, DocStatus.BOUNCED, "unzustellbar")
        send(s, messages.bounced(offer, "S", "B"))
        s.commit()
        doc_id = offer.id
    c = _client()
    _say(c, "schmidt.neu@gmx.de")
    assert "schmidt.neu@gmx.de" in graph.texts()[-1]
    with new_session() as s:
        d = s.get(Document, doc_id)
        assert d.customer_email == "schmidt.neu@gmx.de" and d.status == DocStatus.OPEN
    _say(c, "abmelden")
    with new_session() as s:
        b = s.scalars(select(Business)).one()
        assert b.whatsapp_opt_in is False
        n = messages.opened(s.get(Document, doc_id), "S", "B")
        assert send(s, n) and n.channel == "email"


def test_expired_or_forged_button(fresh_db, imap, smtp, graph):
    with new_session() as s:
        _, offer, _ = _business(s, imap, smtp)
        doc_id = offer.id
    c = _client()
    _press(c, f"stop:{doc_id}.AAAAAA.forged")
    assert "abgelaufen" in graph.texts()[-1]
    with new_session() as s:
        assert s.get(Document, doc_id).status == DocStatus.OPEN


# --- Ereignisse aus der App -------------------------------------------------------------------

def test_opened_notifies_once_via_whatsapp(fresh_db, imap, smtp, graph):
    with new_session() as s:
        _, offer, _ = _business(s, imap, smtp)
        token = offer.view_token
    c = _client()
    n_mail = len(smtp.messages)
    c.get(f"/a/{token}")
    c.get(f"/a/{token}")
    assert len(graph.calls) == 1 and "geöffnet" in graph.texts()[0]
    assert len(smtp.messages) == n_mail


def test_onboarding_saves_number_and_sends_welcome(fresh_db, imap, smtp, graph):
    from locadas.web import auth

    with new_session() as s:
        _seed_sent(imap)
        b, _ = _setup(s, imap, smtp)
        bid = b.id
    c = _client()
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    r = c.post("/whatsapp", data={"whatsapp_number": "0171 2345678", "whatsapp_opt_in": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/start?whatsapp=wa_ok"
    assert graph.last["type"] == "template" and graph.last["template"]["name"] == "locadas_welcome"
    with new_session() as s:
        b = s.get(Business, bid)
        assert b.whatsapp_number == NUM and b.whatsapp_opt_in
    # Gleiche Nummer noch mal speichern: keine zweite Begrüßung
    form = {"name": "Müller Haustechnik GmbH", "whatsapp_number": "+49 171 2345678", "whatsapp_opt_in": "1"}
    r = c.post("/einstellungen", data=form, follow_redirects=False)
    assert r.headers["location"] == "/einstellungen?msg=gespeichert" and len(graph.calls) == 1
    r = c.post("/einstellungen", data={**form, "whatsapp_number": "abc"}, follow_redirects=False)
    assert r.headers["location"] == "/einstellungen?msg=wa_ungueltig"
    assert "WhatsApp-Nummer für deinen Locadas-Sekretär" in c.get("/einstellungen").text
    assert "WhatsApp-Nummer für deinen Locadas-Sekretär" in c.get("/start").text


def test_dedupe_table_records_inbound(fresh_db, imap, smtp, graph):
    with new_session() as s:
        _business(s, imap, smtp)
    c = _client()
    _press(c, tokens.payload("ok"), wamid="wamid.same")
    _press(c, tokens.payload("ok"), wamid="wamid.same")
    with new_session() as s:
        assert len(s.scalars(select(WhatsAppInbound)).all()) == 1
    assert graph.texts().count("Alles klar.") == 1
