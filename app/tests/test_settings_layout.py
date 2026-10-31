"""Einstellungen entschlackt: vorne nur das Nötige, der Rest unter „Erweitert“. Alles bleibt speicherbar."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from locadas.db import Business, new_session
from locadas.web import auth
from tests.test_summary import _biz

MAIN = ["name", "owner_name", "phone", "signature", "followup_channel", "paused", "whatsapp_number", "whatsapp_opt_in", "google_review_url"]
ADVANCED = ["followup_offers", "followup_invoices", "review_mode", "approval_threshold", "doc_delivery", "tone", "personalize", "notify_opened"]


def _client(fresh_db_unused=None):
    from locadas.main import app

    with new_session() as s:
        b, _ = _biz(s, whatsapp=False)
        s.commit()
        bid = b.id
    c = TestClient(app)
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    return c, bid


def _names(html: str) -> set[str]:
    return set(re.findall(r'name="([a-z_]+)"', html))


def test_main_view_and_erweitert(fresh_db):
    c, _ = _client()
    page = c.get("/einstellungen").text
    i = page.index('<details class="card settings-adv" id="erweitert"')
    main, adv = page[:i], page[i:]
    assert MAIN and set(MAIN) <= _names(main)
    assert not set(ADVANCED) & _names(main), set(ADVANCED) & _names(main)
    assert set(ADVANCED) <= _names(adv)
    assert "<summary><b>Erweitert</b>" in adv and "<details class=\"card settings-adv\" id=\"erweitert\" open" not in page
    # Felder unter „Erweitert“ gehören trotzdem zum selben Formular
    for name in ADVANCED:
        assert re.search(rf'<(input|select)[^>]*form="settings"[^>]*name="{name}"', adv), name
    # Postfach und Trennen stehen unter „Erweitert“
    assert "Verbindung trennen" in adv and "Verbindung trennen" not in main
    assert ">Mail<" in main and ">WhatsApp mit einem Tipp<" in main and ">Beides<" in main


def test_post_saves_everything(fresh_db):
    c, bid = _client()
    r = c.post("/einstellungen", data={
        "name": "Koch Elektro", "owner_name": "Sabine Koch", "phone": "0171 1", "signature": "Gruß Sabine",
        "followup_channel": "both", "paused": "1", "google_review_url": "https://g.page/r/Cabc/review", "review_field": "1",
        "followup_offers": "1", "review_mode": "all", "approval_threshold": "2.500", "doc_delivery": "attachment",
        "tone": "sachlich", "personalize": "", "notify_opened": "1",
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/einstellungen?msg=gespeichert"
    with new_session() as s:
        b = s.get(Business, bid)
        assert (b.name, b.owner_name, b.phone, b.signature) == ("Koch Elektro", "Sabine Koch", "0171 1", "Gruß Sabine")
        assert b.followup_channel == "both" and b.paused and b.google_review_url == "https://g.page/r/Cabc/review"
        assert b.followup_offers and not b.followup_invoices
        assert b.review_mode == "all" and b.approval_threshold == 2500
        assert b.doc_delivery == "attachment" and b.include_view_link and b.tone == "sachlich"
        assert not b.personalize and b.notify_opened
    page = c.get("/einstellungen").text
    assert 'value="both" selected' in page and 'name="paused" value="1" checked' in page


def test_erweitert_opens_for_its_messages(fresh_db):
    c, _ = _client()
    assert 'id="erweitert" open' in c.get("/einstellungen?msg=bestaetigen").text
