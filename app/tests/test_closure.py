"""Morgen-Vorschau mit Stopp (Kunde hat angerufen) und Abschlussfrage nach der letzten Stufe."""
from __future__ import annotations

import email
import email.policy
import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from locadas.db import DocStatus, Document, FollowUpStatus, new_session
from locadas.engine import followups as fu
from locadas.engine.scanner import scan_account
from tests.test_e2e import WEEKDAY_10, _seed_sent, _setup


def _last(smtp):
    return email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default)


def test_preview_stop_prevents_send(fresh_db, imap, smtp):
    from locadas.main import app

    _seed_sent(imap)
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        scan_account(s, account)
        b.auto_mode = True
        offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
        offer.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        fu.process_due(s, now=WEEKDAY_10)
        doc_id = offer.id
    stop = re.search(r"http://testserver(/f/\S+/stop)", _last(smtp).get_content()).group(1)
    n = len(smtp.messages)
    assert TestClient(app).get(stop).status_code == 200  # Kunde hat angerufen
    with new_session() as s:
        assert fu.process_due(s, now=WEEKDAY_10 + timedelta(hours=3))["sent"] == 0
        d = s.get(Document, doc_id)
        assert d.status == DocStatus.REPLIED and not any(f.status == FollowUpStatus.SENT for f in d.followups)
    assert len(smtp.messages) == n


def test_outcome_question_after_last_step(fresh_db, imap, smtp):
    from locadas.main import app

    _seed_sent(imap)
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        scan_account(s, account)
        b.auto_mode = True
        offer = s.scalars(select(Document).where(Document.document_number == "A-2026-118")).one()
        t = WEEKDAY_10
        for f in offer.followups:
            f.status, f.sent_at, f.message_id = FollowUpStatus.SENT, t - timedelta(days=10), "<x@y>"
        offer.next_followup_at = None
        s.commit()
        assert fu.finish_exhausted(s, now=t) == 1
        s.commit()
        doc_id = offer.id
        assert fu.finish_exhausted(s, now=t + timedelta(days=1)) == 0  # nur einmal fragen
    m = _last(smtp)
    assert "Wie ist es ausgegangen" in m["Subject"]
    won = re.search(r"http://testserver(/o/\S+/won)", m.get_content()).group(1)
    assert TestClient(app).get(won).status_code == 200
    with new_session() as s:
        assert s.get(Document, doc_id).status == DocStatus.WON
