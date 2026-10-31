"""Stufe 2: WhatsApp über die eigene Nummer des Betriebs (Coexistence). Ohne Netzwerk, Graph API gemockt."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from locadas.channels import contacts, graph, select_and_send, usage
from locadas.channels import wa_business as wab
from locadas.channels import wa_templates as tpl
from locadas.channels.config import wab_settings
from locadas.crypto import encrypt
from locadas.db import (
    Business, DocKind, DocStatus, Document, Event, FollowUp, FollowUpStatus, MailAccount, WaContact, WaMessage, WaTemplate,
    WaUsage, WhatsAppConnection, new_session, utcnow,
)
from locadas.engine import followups as fe
from locadas.web import auth

SECRET = "app-secret-stage2"
CUSTOMER = "491701112233"
PNID = "111222333"
WABA = "999888777"


class FakeGraph:
    """Fängt alle Aufrufe an graph.facebook.com ab. `errors` erzwingt Fehler je Pfad-Muster."""

    def __init__(self):
        self.calls: list[tuple[str, str, str | None, dict | None, dict | None]] = []
        self.errors: dict[str, dict] = {}
        self.existing_templates: list[dict] = []
        self.n = 0
        self.on_biz_app = True

    def __call__(self, method, url, token=None, payload=None, params=None):
        path = re.sub(r"^https://graph\.facebook\.com/v[\d.]+/", "", url)
        self.calls.append((method, path, token, payload, params))
        for pat, err in self.errors.items():
            if re.search(pat, f"{method} {path}"):
                raise graph.GraphError.from_payload(err, 400)
        if path == "oauth/access_token":
            return {"access_token": "BIZTOKEN", "token_type": "bearer"}
        if path.endswith("/subscribed_apps"):
            return {"success": True}
        if path == PNID and method == "GET":
            return {"display_phone_number": "+49 171 5550000", "verified_name": "Müller Haustechnik", "quality_rating": "GREEN",
                    "messaging_limit_tier": "TIER_250", "is_on_biz_app": self.on_biz_app, "platform_type": "CLOUD_API"}
        if path.endswith("/register"):
            return {"success": True}
        if path.endswith("/smb_app_data"):
            return {"request_id": f"req-{payload['sync_type']}"}
        if path.endswith("/message_templates") and method == "GET":
            return {"data": self.existing_templates}
        if path.endswith("/message_templates") and method == "POST":
            self.n += 1
            return {"id": f"tpl{self.n}", "status": "PENDING", "category": payload["category"]}
        if path.endswith("/messages"):
            self.n += 1
            return {"messaging_product": "whatsapp", "contacts": [{"input": payload["to"], "wa_id": payload["to"]}], "messages": [{"id": f"wamid.biz{self.n}"}]}
        raise AssertionError(f"Unerwarteter Aufruf {method} {path}")

    def of(self, method: str, suffix: str) -> list:
        return [c for c in self.calls if c[0] == method and c[1].endswith(suffix)]


@pytest.fixture()
def g(monkeypatch):
    fake = FakeGraph()
    monkeypatch.setattr(graph, "http", fake)
    monkeypatch.setattr(wab_settings, "enabled_flag", True)
    monkeypatch.setattr(wab_settings, "app_id", "APPID")
    monkeypatch.setattr(wab_settings, "app_secret", SECRET)
    monkeypatch.setattr(wab_settings, "config_id", "CFGID")
    monkeypatch.setattr(wab_settings, "requires_pro", True)
    return fake


def _seed(s, plan: str = "pro", kind: DocKind = DocKind.OFFER) -> tuple[Business, Document, FollowUp]:
    b = Business(name="Müller Haustechnik GmbH", owner_name="Thomas Müller", email=f"info{utcnow().timestamp()}@mueller-shk.de", plan=plan)
    s.add(b)
    s.flush()
    a = MailAccount(business_id=b.id, provider="other", email=b.email, username=b.email, secret_enc=encrypt("x"),
                    imap_host="127.0.0.1", smtp_host="127.0.0.1")
    s.add(a)
    s.flush()
    d = Document(business_id=b.id, account_id=a.id, kind=kind, customer_name="Familie Schmidt", customer_email="familie.schmidt@gmx.de",
                 document_number="A-2026-118" if kind == DocKind.OFFER else "R-2026-301", amount_gross=3724.70,
                 subject="Angebot Gastherme", sent_at=utcnow() - timedelta(days=4))
    s.add(d)
    s.flush()
    fe.schedule_followups(s, d)
    s.commit()
    return b, d, d.followups[0]


def _connect(s, b: Business, approved: bool = True) -> WhatsAppConnection:
    conn = WhatsAppConnection(business_id=b.id, waba_id=WABA, phone_number_id=PNID, display_phone="+49 171 5550000",
                              business_token_enc=encrypt("BIZTOKEN"), status="active", coexistence=True, connected_at=utcnow())
    s.add(conn)
    s.flush()
    for d in tpl.DEFS.values():
        s.add(WaTemplate(connection_id=conn.id, key=d.key, name=d.name, language="de", category="UTILITY",
                         status="APPROVED" if approved else "PENDING"))
    s.commit()
    return conn


def _client(bid: int) -> TestClient:
    from locadas.main import app

    c = TestClient(app)
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    return c


def _post_webhook(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook/whatsapp", content=body, headers={"x-hub-signature-256": sig, "content-type": "application/json"})


def _value(**kw) -> dict:
    return {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "491715550000", "phone_number_id": PNID}, **kw}


def _envelope(value: dict, field: str = "messages") -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{"field": field, "value": value}]}]}


# --- Verbinden ----------------------------------------------------------------------------------

def test_connect_flow_exchanges_code_subscribes_and_syncs_templates(fresh_db, g):
    g.existing_templates = [{"id": "old1", "name": tpl.DEFS["invoice_1"].name, "language": "de", "status": "APPROVED", "category": "UTILITY"}]
    with new_session() as s:
        b, _, _ = _seed(s)
        bid = b.id
    c = _client(bid)
    page = c.get("/einstellungen")
    assert "WhatsApp-Nachfasser (über deine eigene Nummer)" in page.text
    assert "whatsapp_business_app_onboarding" in page.text and "CFGID" in page.text
    r = c.post("/wa-business/connect", json={"code": "CODE123", "waba_id": WABA, "phone_number_id": PNID, "event": "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    tok = g.of("GET", "oauth/access_token")[0]
    assert tok[4] == {"client_id": "APPID", "client_secret": SECRET, "code": "CODE123"}
    sub = g.of("POST", f"{WABA}/subscribed_apps")
    assert sub and sub[0][2] == "BIZTOKEN"
    assert {c[3]["sync_type"] for c in g.of("POST", "/smb_app_data")} == {"smb_app_state_sync", "history"}
    assert not g.of("POST", "/register"), "Coexistence: Nummer darf nicht neu registriert werden"
    created = g.of("POST", "/message_templates")
    assert len(created) == 5  # invoice_1 gab es schon
    body = created[0][3]
    assert body["language"] == "de" and body["category"] == "UTILITY"
    assert any(comp["type"] == "BUTTONS" for comp in body["components"])
    assert "—" not in json.dumps(body, ensure_ascii=False)

    with new_session() as s:
        conn = s.scalar(select(WhatsAppConnection).where(WhatsAppConnection.business_id == bid))
        assert conn.status == "active" and conn.coexistence
        assert conn.display_phone == "+49 171 5550000" and conn.quality_rating == "GREEN"
        assert conn.business_token_enc and "BIZTOKEN" not in conn.business_token_enc
        assert wab.token(conn) == "BIZTOKEN"
        assert conn.history_request_id == "req-history" and conn.templates_synced_at is not None
        rows = {t.key: t for t in s.scalars(select(WaTemplate).where(WaTemplate.connection_id == conn.id))}
        assert rows["invoice_1"].status == "APPROVED" and rows["offer_1"].status == "PENDING"
    page = c.get("/einstellungen")
    assert "verbunden" in page.text and "in Prüfung bei Meta" in page.text

    # Trennen
    r = c.post("/wa-business/trennen", follow_redirects=False)
    assert r.status_code == 303
    assert g.of("DELETE", f"{WABA}/subscribed_apps")
    with new_session() as s:
        conn = s.scalar(select(WhatsAppConnection).where(WhatsAppConnection.business_id == bid))
        assert conn.status == "disconnected" and conn.business_token_enc == ""


def test_connect_without_coexistence_registers_number(fresh_db, g):
    from locadas.channels import onboarding

    with new_session() as s:
        b, _, _ = _seed(s)
        g.on_biz_app = False
        g_info = g.errors  # nichts erzwingen
        conn = onboarding.connect(s, b, "C", WABA, PNID, "FINISH")
        s.commit()
        assert conn.status == "active" and not conn.coexistence
        reg = g.of("POST", f"{PNID}/register")
        assert reg and re.fullmatch(r"\d{6}", reg[0][3]["pin"])
        assert not g.of("POST", "/smb_app_data")
        assert g_info == {}


def test_connect_errors_are_clear(fresh_db, g):
    g.errors["GET oauth/access_token"] = {"message": "Invalid code", "code": 100}
    with new_session() as s:
        b, _, _ = _seed(s)
        bid = b.id
    r = _client(bid).post("/wa-business/connect", json={"code": "alt", "waba_id": WABA})
    assert r.status_code == 400 and "30 Sekunden" in r.json()["message"]


def test_plan_gating(fresh_db, g):
    with new_session() as s:
        b, _, fu = _seed(s, plan="free")
        _connect(s, b)
        contacts.record_opt_in(s, b.id, CUSTOMER, "manual", "familie.schmidt@gmx.de")
        s.commit()
        bid = b.id
        assert select_and_send(s, fu) is None
        assert not g.of("POST", "/messages")
    c = _client(bid)
    assert "Pro-Paket" in c.get("/einstellungen").text
    assert c.post("/wa-business/connect", json={"code": "x", "waba_id": WABA}).status_code == 402
    with new_session() as s:
        wab_settings.requires_pro = False
        try:
            fu = s.get(FollowUp, fu.id)
            assert select_and_send(s, fu) is True
        finally:
            wab_settings.requires_pro = True


def test_disabled_without_credentials(fresh_db, g, monkeypatch):
    monkeypatch.setattr(wab_settings, "config_id", None)
    with new_session() as s:
        b, _, fu = _seed(s)
        _connect(s, b)
        contacts.record_opt_in(s, b.id, CUSTOMER, "manual", "familie.schmidt@gmx.de")
        s.commit()
        assert select_and_send(s, fu) is None
        bid = b.id
    assert "WhatsApp-Nachfasser" not in _client(bid).get("/einstellungen").text


# --- Versand ----------------------------------------------------------------------------------

def test_none_without_connection_mobile_or_opt_in(fresh_db, g):
    with new_session() as s:
        b, d, fu = _seed(s)
        assert select_and_send(s, fu) is None  # nicht verbunden
        _connect(s, b)
        assert select_and_send(s, fu) is None  # keine Handynummer
        contacts.get_or_create(s, b.id, CUSTOMER, "familie.schmidt@gmx.de")
        s.commit()
        assert contacts.customer_mobile(s, d) == CUSTOMER
        assert select_and_send(s, fu) is None  # Nummer ja, Einwilligung nein
        assert fu.status == FollowUpStatus.SCHEDULED
        assert not g.of("POST", "/messages")


def test_customer_mobile_reads_columns_of_stage1(fresh_db, g):
    with new_session() as s:
        b, d, _ = _seed(s)
        d.customer_mobile = "0170 1112233"  # Attribut wie es Stufe 1 anlegen könnte (hier nur am Objekt)
        assert contacts.customer_mobile(s, d) == CUSTOMER
        with pytest.raises(ValueError):
            contacts.record_opt_in(s, b.id, CUSTOMER, "irgendwas")


def test_template_outside_window(fresh_db, g):
    with new_session() as s:
        b, d, fu = _seed(s)
        _connect(s, b)
        contacts.record_opt_in(s, b.id, CUSTOMER, "manual", "familie.schmidt@gmx.de")
        s.commit()
        assert select_and_send(s, fu) is True
        call = g.of("POST", f"{PNID}/messages")[-1]
        assert call[2] == "BIZTOKEN"
        p = call[3]
        assert p["type"] == "template" and p["to"] == CUSTOMER
        assert p["template"]["name"] == tpl.DEFS["offer_1"].name and p["template"]["language"]["code"] == "de"
        body, button = p["template"]["components"]
        values = [x["text"] for x in body["parameters"]]
        assert values[0] == "Familie Schmidt" and values[1] == "Thomas Müller von Müller Haustechnik GmbH"
        assert values[3] == "unser Angebot Nr. A-2026-118"
        assert button["sub_type"] == "url" and button["parameters"][0]["text"] == d.view_token
        s.refresh(fu)
        assert fu.status == FollowUpStatus.SENT and "Familie Schmidt" in fu.body
        assert d.followups_sent == 1
        msg = s.scalar(select(WaMessage).where(WaMessage.followup_id == fu.id))
        assert msg.wamid.startswith("wamid.biz") and msg.category == "utility" and msg.kind == "template"
        assert usage.month_summary(s, b.id)["counts"]["utility"] == 1
        assert select_and_send(s, fu) is False  # schon verschickt: weder WhatsApp noch Mail


def test_invoice_template_params(fresh_db, g):
    with new_session() as s:
        b, d, fu = _seed(s, kind=DocKind.INVOICE)
        params = tpl.params_for(d, 1)
        assert params[2] == "unserer Rechnung Nr. R-2026-301" and "€" in params[3] and re.fullmatch(r"\d\d\.\d\d\.\d{4}", params[4])
        assert tpl.params_for(d, 2)[2] == "Unsere Rechnung Nr. R-2026-301"
        text = tpl.render("invoice_2", tpl.params_for(d, 2))
        assert "{{" not in text and "Unsere Rechnung Nr. R-2026-301 über" in text


def test_template_not_approved_falls_back(fresh_db, g):
    with new_session() as s:
        b, _, fu = _seed(s)
        _connect(s, b, approved=False)
        contacts.record_opt_in(s, b.id, CUSTOMER, "manual", "familie.schmidt@gmx.de")
        s.commit()
        assert select_and_send(s, fu) is None


def test_free_text_inside_window_without_opt_in(fresh_db, g):
    with new_session() as s:
        b, _, fu = _seed(s)
        _connect(s, b)
        c = contacts.get_or_create(s, b.id, CUSTOMER, "familie.schmidt@gmx.de")
        c.last_inbound_at = utcnow() - timedelta(hours=2)
        s.commit()
        assert select_and_send(s, fu) is True
        p = g.of("POST", "/messages")[-1][3]
        assert p["type"] == "text" and p["text"]["body"] == fu.body and fu.body
        assert usage.month_summary(s, b.id)["counts"]["service"] == 1
        # Fenster vorbei, ohne Einwilligung: keine Vorlage
        fu2 = fu.document.followups[1]
        c.last_inbound_at = utcnow() - timedelta(hours=25)
        s.commit()
        assert select_and_send(s, fu2) is None


def test_error_mapping_texts():
    e = lambda code: wab.map_error(graph.GraphError("x", code), "491701112233")  # noqa: E731
    assert e(131047).scope == "window" and "24 Stunden" in e(131047).owner_text
    assert e(470).scope == "window"
    assert e(131026).scope == "recipient" and "+491701112233" in e(131026).owner_text
    assert e(131049).scope == "recipient" and "Werbenachrichten" in e(131049).owner_text
    assert e(131051).fallback_mail
    assert e(190).scope == "account" and "neu" in e(190).owner_text
    assert e(132001).scope == "template"
    unknown = e(424242)
    assert unknown.scope == "unknown" and "per Mail" in unknown.owner_text
    for code in (131047, 131026, 131049, 131050, 190, 368, 132015, 131042):
        t = e(code).owner_text
        assert "—" not in t and ", und " not in t


def test_send_error_falls_back_to_mail_and_marks_contact(fresh_db, g):
    g.errors[r"POST .*/messages"] = {"message": "Message undeliverable", "code": 131026}
    with new_session() as s:
        b, d, fu = _seed(s)
        _connect(s, b)
        contacts.record_opt_in(s, b.id, CUSTOMER, "manual", "familie.schmidt@gmx.de")
        s.commit()
        assert select_and_send(s, fu) is None
        s.refresh(fu)
        assert fu.status == FollowUpStatus.SCHEDULED and fu.sent_at is None
        assert contacts.get(s, b.id, CUSTOMER).undeliverable_at is not None
        ev = s.scalars(select(Event).where(Event.business_id == b.id, Event.kind == "whatsapp")).all()
        assert any("kam nicht an" in e.text for e in ev)
        assert usage.month_summary(s, b.id)["total"] == 0


def test_account_error_marks_connection(fresh_db, g, monkeypatch):
    g.errors[r"POST .*/messages"] = {"message": "Token expired", "code": 190}
    told = []
    monkeypatch.setattr("locadas.notify.send", lambda session, n, now=None: told.append(n) or True)
    with new_session() as s:
        b, _, fu = _seed(s)
        conn = _connect(s, b)
        contacts.record_opt_in(s, b.id, CUSTOMER, "manual", "familie.schmidt@gmx.de")
        s.commit()
        assert select_and_send(s, fu) is None
        s.refresh(conn)
        assert conn.status == "error" and "neu" in conn.last_error
        assert told and "neu" in told[0].text
        assert select_and_send(s, fu) is None  # Verbindung nicht mehr aktiv


# --- Webhook ----------------------------------------------------------------------------------

def _sent_setup(s, g):
    b, d, fu = _seed(s)
    _connect(s, b)
    contacts.record_opt_in(s, b.id, CUSTOMER, "manual", "familie.schmidt@gmx.de")
    s.commit()
    assert select_and_send(s, fu) is True
    wamid = s.scalar(select(WaMessage.wamid).where(WaMessage.followup_id == fu.id))
    return b, d, fu, wamid


def test_webhook_inbound_reply_closes_document(fresh_db, g):
    with new_session() as s:
        b, d, fu, _ = _sent_setup(s, g)
        bid, did = b.id, d.id
    client = _client(bid)
    payload = _envelope(_value(
        contacts=[{"profile": {"name": "Anna Schmidt"}, "wa_id": CUSTOMER}],
        messages=[{"from": CUSTOMER, "id": "wamid.in1", "timestamp": str(int(utcnow().timestamp())), "type": "text", "text": {"body": "Danke, wir melden uns nächste Woche."}}],
    ))
    r = _post_webhook(client, payload)
    assert r.status_code == 200 and r.json()["handled"] == 1
    assert _post_webhook(client, payload).json()["handled"] == 0  # doppelt geschickt: ignoriert
    with new_session() as s:
        d = s.get(Document, did)
        assert d.status == DocStatus.REPLIED and "WhatsApp" in d.close_reason
        assert all(f.status != FollowUpStatus.SCHEDULED for f in d.followups)
        c = contacts.get(s, bid, CUSTOMER)
        assert c.last_inbound_at is not None and contacts.in_window(c, utcnow())


def test_webhook_unsigned_or_foreign_number_is_left_to_secretary(fresh_db, g):
    from locadas.channels import webhook

    with new_session() as s:
        b, _, _ = _seed(s)
        _connect(s, b)
        foreign = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": "LOCADAS"}, "messages": [{"from": "4912", "id": "x", "type": "text", "text": {"body": "hi"}}]}}]}]}
        rest, n = webhook.route(s, foreign)
        assert n == 0 and rest == foreign
        bid = b.id
    client = _client(bid)
    r = client.post("/webhook/whatsapp", content=b"{}", headers={"x-hub-signature-256": "sha256=falsch"})
    assert r.status_code == 403


def test_webhook_customer_first_message_is_opt_in_and_stop_opts_out(fresh_db, g):
    with new_session() as s:
        b, d, fu = _seed(s)
        _connect(s, b)
        bid, fid = b.id, fu.id
    client = _client(bid)
    _post_webhook(client, _envelope(_value(messages=[{"from": CUSTOMER, "id": "wamid.a", "timestamp": "1700000000", "type": "text", "text": {"body": "Hallo, kurze Frage"}}])))
    with new_session() as s:
        c = contacts.get(s, bid, CUSTOMER)
        assert c.wa_opt_in_source == "customer_initiated_chat"
    _post_webhook(client, _envelope(_value(messages=[{"from": CUSTOMER, "id": "wamid.b", "timestamp": "1700000100", "type": "text", "text": {"body": "STOP"}}])))
    with new_session() as s:
        c = contacts.get(s, bid, CUSTOMER)
        assert c.opted_out_at is not None and not c.wa_opt_in_source
        confirm = g.of("POST", "/messages")[-1][3]
        assert confirm["type"] == "text" and "keine automatischen Erinnerungen" in confirm["text"]["body"]
        c.customer_email = "familie.schmidt@gmx.de"
        s.commit()
        assert select_and_send(s, s.get(FollowUp, fid)) is None
    for word in ("Stopp", "abmelden", "Keine Nachrichten mehr!", "bitte keine nachrichten"):
        assert contacts.is_opt_out(word), word
    assert not contacts.is_opt_out("Stoppen Sie bitte nicht")


def test_webhook_echo_pushes_next_step(fresh_db, g):
    with new_session() as s:
        b, d, fu = _seed(s)
        _connect(s, b)
        contacts.get_or_create(s, b.id, CUSTOMER, "familie.schmidt@gmx.de")
        s.commit()
        before = fu.due_at
        bid, did = b.id, d.id
    client = _client(bid)
    r = _post_webhook(client, _envelope(_value(message_echoes=[{
        "from": "491715550000", "to": CUSTOMER, "id": "wamid.echo1", "timestamp": str(int(utcnow().timestamp())), "type": "text", "text": {"body": "Moin, passt das Angebot?"},
    }]), field="smb_message_echoes"))
    assert r.json()["handled"] == 1
    with new_session() as s:
        d = s.get(Document, did)
        assert d.owner_contact_at is not None and d.status == DocStatus.OPEN
        nxt = min(f.due_at for f in d.followups if f.status == FollowUpStatus.SCHEDULED)
        assert nxt > before and nxt >= utcnow() + timedelta(days=3)


def test_webhook_status_read_sets_read_receipt(fresh_db, g):
    with new_session() as s:
        b, d, fu, wamid = _sent_setup(s, g)
        bid, fid = b.id, fu.id
    client = _client(bid)
    ts = str(int(utcnow().timestamp()))
    _post_webhook(client, _envelope(_value(statuses=[
        {"id": wamid, "status": "delivered", "timestamp": ts, "recipient_id": CUSTOMER, "pricing": {"billable": True, "pricing_model": "PMP", "category": "utility"}},
        {"id": wamid, "status": "read", "timestamp": ts, "recipient_id": CUSTOMER},
    ])))
    with new_session() as s:
        fu = s.get(FollowUp, fid)
        assert fu.wa_read_at is not None
        assert s.scalar(select(WaMessage).where(WaMessage.wamid == wamid)).status == "read"
    page = client.get("/dashboard")
    assert "auf WhatsApp gelesen" in page.text
    assert "WhatsApp-Nachrichten diesen Monat: <b>1</b>" in page.text


def test_webhook_pricing_category_recounts(fresh_db, g):
    with new_session() as s:
        b, d, fu, wamid = _sent_setup(s, g)
        bid = b.id
    _post_webhook(_client(bid), _envelope(_value(statuses=[
        {"id": wamid, "status": "sent", "timestamp": "1", "recipient_id": CUSTOMER, "pricing": {"category": "marketing"}},
    ])))
    with new_session() as s:
        sm = usage.month_summary(s, bid)
        assert sm["counts"]["utility"] == 0 and sm["counts"]["marketing"] == 1
        assert sm["eur"] == round(wab_settings.price_marketing_eur, 2)


def test_webhook_failed_status_reschedules_for_mail(fresh_db, g):
    with new_session() as s:
        b, d, fu, wamid = _sent_setup(s, g)
        bid, fid, did = b.id, fu.id, d.id
    _post_webhook(_client(bid), _envelope(_value(statuses=[
        {"id": wamid, "status": "failed", "timestamp": "1", "recipient_id": CUSTOMER, "errors": [{"code": 131026, "title": "Message undeliverable"}]},
    ])))
    with new_session() as s:
        fu = s.get(FollowUp, fid)
        assert fu.status == FollowUpStatus.SCHEDULED and fu.body == ""
        assert s.get(Document, did).followups_sent == 0
        assert contacts.get(s, bid, CUSTOMER).undeliverable_at is not None
        assert select_and_send(s, fu) is None  # nächster Versuch geht per Mail
        assert usage.month_summary(s, bid)["total"] == 0


def test_webhook_template_updates(fresh_db, g):
    with new_session() as s:
        b, _, _ = _seed(s)
        conn = _connect(s, b, approved=False)
        bid, cid = b.id, conn.id
    client = _client(bid)
    name = tpl.DEFS["offer_1"].name
    _post_webhook(client, {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [
        {"field": "message_template_status_update", "value": {"event": "APPROVED", "message_template_name": name, "message_template_language": "de"}},
        {"field": "template_category_update", "value": {"message_template_name": name, "previous_category": "UTILITY", "new_category": "MARKETING"}},
    ]}]})
    with new_session() as s:
        t = s.scalar(select(WaTemplate).where(WaTemplate.connection_id == cid, WaTemplate.key == "offer_1"))
        assert t.status == "APPROVED" and t.category == "MARKETING"
        assert any("Werbung" in e.text for e in s.scalars(select(Event).where(Event.business_id == bid)))


def test_history_marks_contacts_but_no_opt_in(fresh_db, g):
    with new_session() as s:
        b, _, _ = _seed(s)
        _connect(s, b)
        bid = b.id
    _post_webhook(_client(bid), {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{"field": "history", "value": _value(history=[{
        "metadata": {"phase": 0, "chunk_order": 1, "progress": 100},
        "threads": [{"id": CUSTOMER, "messages": [{"from": CUSTOMER, "id": "h1", "timestamp": "1", "type": "text", "text": {"body": "Hallo"}}]}],
    }])}]}]})
    with new_session() as s:
        c = contacts.get(s, bid, CUSTOMER)
        assert c.history_seen_at is not None and not c.wa_opt_in_source


def test_partner_removed_disconnects(fresh_db, g):
    with new_session() as s:
        b, _, _ = _seed(s)
        _connect(s, b)
        bid = b.id
    _post_webhook(_client(bid), {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [
        {"field": "account_update", "value": {"event": "PARTNER_REMOVED", "waba_info": {"waba_id": WABA}}},
    ]}]})
    with new_session() as s:
        conn = s.scalar(select(WhatsAppConnection).where(WhatsAppConnection.business_id == bid))
        assert conn.status == "disconnected"


# --- Einwilligung über die Dokumentseite, Zählung -----------------------------------------------------

def test_owner_checkbox_sets_manual_opt_in(fresh_db, g):
    with new_session() as s:
        b, d, _ = _seed(s)
        _connect(s, b)
        bid, did = b.id, d.id
    c = _client(bid)
    page = c.get(f"/dokument/{did}")
    assert "Kunde hat mir bereits auf WhatsApp geschrieben oder ist einverstanden" in page.text
    r = c.post(f"/wa-business/kontakt/{did}", data={"mobile": "0170 1112233", "consent": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as s:
        ct = contacts.get(s, bid, CUSTOMER)
        assert ct.wa_opt_in_source == "manual" and ct.customer_email == "familie.schmidt@gmx.de"
    c.post(f"/wa-business/kontakt/{did}", data={"mobile": "0170 1112233"}, follow_redirects=False)
    with new_session() as s:
        assert not contacts.may_send_template(contacts.get(s, bid, CUSTOMER))
    r = c.post(f"/wa-business/kontakt/{did}", data={"mobile": "abc"}, follow_redirects=False)
    assert "wab_nummer" in r.headers["location"]


def test_usage_summary_free_service_tier(fresh_db, g, monkeypatch):
    monkeypatch.setattr(wab_settings, "service_free_per_month", 2)
    with new_session() as s:
        b, _, _ = _seed(s)
        for cat, n in (("utility", 10), ("marketing", 2), ("service", 5)):
            for _ in range(n):
                usage.count(s, b.id, cat)
        s.commit()
        sm = usage.month_summary(s, b.id)
        assert sm["total"] == 17
        expected = 10 * wab_settings.price_utility_eur + 2 * wab_settings.price_marketing_eur + 3 * wab_settings.price_utility_eur
        assert sm["eur"] == round(expected, 2)
        assert s.scalar(select(WaUsage.count).where(WaUsage.business_id == b.id, WaUsage.category == "utility")) == 10
        assert usage.eur_text(1.5) == "1,50 €"


def test_templates_follow_meta_rules():
    for d in tpl.DEFS.values():
        assert re.fullmatch(r"[a-z0-9_]+", d.name) and len(d.name) <= 512
        nums = [int(n) for n in re.findall(r"\{\{(\d+)\}\}", d.body)]
        assert nums == list(range(1, len(nums) + 1)), d.key  # fortlaufend und in Reihenfolge
        assert len(nums) == len(d.example)
        assert not d.body.startswith("{{") and not d.body.rstrip(".").endswith("}}")
        assert "—" not in d.body and "–" not in d.body and ", und " not in d.body
        assert len(d.body) <= 1024
    assert len(tpl.FOOTER) <= 60
