"""Lese-Tracking: Link in der Nachfass-Mail, Öffnen wird gezählt, Betrieb bekommt Bescheid."""
from __future__ import annotations

import email
import email.policy
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from locadas.db import Business, Document, Event, new_session
from locadas.engine import followups as fu
from locadas.engine.scanner import scan_account
from tests.conftest import USER
from tests.test_e2e import WEEKDAY_10, _seed_sent, _setup


def test_view_link_tracks_open(fresh_db, imap, smtp):
    from datetime import timedelta

    from locadas.main import app

    _seed_sent(imap)
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        scan_account(s, account)
        b.auto_mode = True
        offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
        offer.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        assert fu.process_due(s, now=WEEKDAY_10)["sent"] == 1
        token = offer.view_token
        doc_id = offer.id

    body = email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default).get_content()
    link = re.search(r"http://testserver/a/(\S+)", body)
    assert link and link.group(1) == token
    n_before = len(smtp.messages)

    client = TestClient(app)
    r = client.get(f"/a/{token}")
    assert r.status_code == 200 and "A-2026-118" in r.text and "Müller Haustechnik GmbH" in r.text
    r = client.get(f"/a/{token}/pdf")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    client.get(f"/a/{token}")
    assert client.get("/a/falsch").status_code == 404

    with new_session() as s:
        d = s.get(Document, doc_id)
        assert d.open_count == 2 and d.first_opened_at is not None
        assert s.scalars(select(Event).where(Event.kind == "opened")).all().__len__() == 1
    # Genau eine Benachrichtigung an den Betrieb, nur beim ersten Öffnen
    assert len(smtp.messages) == n_before + 1
    assert smtp.messages[-1][1] == [USER]
    assert "geöffnet" in email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default)["Subject"]


def test_view_link_can_be_disabled(fresh_db, imap, smtp):
    from locadas.engine import texts

    _seed_sent(imap)
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        scan_account(s, account)
        offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
        assert "/a/" in texts.body_for(offer, 1)
        b.include_view_link = False
        s.commit()
        assert "/a/" not in texts.body_for(offer, 1)
