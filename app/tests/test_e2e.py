"""End-to-End: Postfach verbinden → Dokumente erkennen → Freigabe → Nachfass-Mail → Antwort stoppt."""
from __future__ import annotations

import email
import email.policy
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from locadas.db import Business, DocKind, DocStatus, Document, FollowUp, FollowUpStatus, new_session
from locadas.engine import accounts as acc
from locadas.engine import followups as fu
from locadas.engine.scanner import scan_account
from locadas.mail.connect import ConnectFailed, connect_mailbox
from tests.conftest import PASSWORD, USER
from tests.fakemail import make_mail, make_pdf

WEEKDAY_10 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)  # Dienstag 10:00 Berlin


def _offer_pdf():
    return make_pdf([
        "Mueller Haustechnik GmbH", "Angebot Nr. A-2026-118",
        "Familie Schmidt, Gartenweg 4, 41061 Moenchengladbach",
        "Pos. 1 Demontage Altgeraet 1 St 180,00 EUR", "Pos. 2 Gastherme Vaillant 1 St 2.950,00 EUR",
        "Netto 3.130,00 EUR", "MwSt 19% 594,70 EUR", "Gesamtbetrag 3.724,70 EUR",
        "Angebot gueltig bis 26.11.2026",
    ])


def _invoice_pdf():
    return make_pdf([
        "Mueller Haustechnik GmbH", "Rechnung Nr. R-2026-301",
        "Herr Peter Weber, Hauptstr. 9, 52525 Heinsberg",
        "Wartung Gastherme 1 St 189,00 EUR", "Netto 189,00 EUR", "MwSt 19% 35,91 EUR",
        "Rechnungsbetrag 224,91 EUR", "Zahlbar bis 03.11.2026 ohne Abzug",
    ])


def _seed_sent(imap):
    imap.store.append("Gesendet", make_mail(USER, "familie.schmidt@gmx.de", "Angebot Gastherme", "Anbei unser Angebot.", ("Angebot_A-2026-118.pdf", _offer_pdf()), from_name="Müller Haustechnik", to_name="Familie Schmidt"))
    imap.store.append("Gesendet", make_mail(USER, "p.weber@web.de", "Ihre Rechnung R-2026-301", "Anbei die Rechnung.", ("Rechnung_R-2026-301.pdf", _invoice_pdf()), to_name="Peter Weber"))
    imap.store.append("Gesendet", make_mail(USER, "kollege@mueller-shk.de", "Urlaubsplan", "Ohne PDF."))
    imap.store.append("Gesendet", make_mail(USER, "kunde@example.org", "Lieferschein 55", "Lieferschein.", ("Lieferschein.pdf", make_pdf(["Lieferschein 55"]))))


def _connect(imap, smtp):
    return connect_mailbox(USER, PASSWORD, imap_host="127.0.0.1", imap_port=imap.port, smtp_host="127.0.0.1", smtp_port=smtp.port, imap_ssl=False, smtp_ssl=False)


def _setup(session, imap, smtp):
    cfg = _connect(imap, smtp)
    b = Business(name="Müller Haustechnik GmbH", owner_name="Thomas Müller", email=USER, phone="0171 1")
    session.add(b)
    session.flush()
    account = acc.create_account(session, b, cfg, PASSWORD)
    session.commit()
    return b, account


def test_connect_finds_sent_folder_and_rejects_wrong_password(imap, smtp):
    cfg = _connect(imap, smtp)
    assert cfg.sent_folder == "Gesendet"
    assert cfg.smtp_port == smtp.port
    try:
        connect_mailbox(USER, "falsch", imap_host="127.0.0.1", imap_port=imap.port, smtp_host="127.0.0.1", smtp_port=smtp.port, imap_ssl=False, smtp_ssl=False)
    except ConnectFailed as exc:
        assert "Login abgelehnt" in exc.message
    else:
        raise AssertionError("falsches Passwort wurde akzeptiert")


def test_full_flow(fresh_db, imap, smtp):
    _seed_sent(imap)
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        stats = scan_account(s, account)
        assert stats["new"] == 2, imap.log
        docs = s.scalars(select(Document).order_by(Document.id)).all()
        offer = next(d for d in docs if d.kind == DocKind.OFFER)
        inv = next(d for d in docs if d.kind == DocKind.INVOICE)
        assert offer.customer_email == "familie.schmidt@gmx.de"
        assert offer.customer_name == "Familie Schmidt"
        assert offer.amount_gross == 3724.70
        assert offer.document_number == "A-2026-118"
        assert offer.valid_until is not None and offer.valid_until.date().isoformat() == "2026-11-26"
        assert inv.amount_gross == 224.91
        assert inv.due_date.date().isoformat() == "2026-11-03"
        assert inv.document_number == "R-2026-301"
        assert len(offer.followups) == 3 and len(inv.followups) == 3
        assert offer.followups[0].due_at.date() == (offer.sent_at + timedelta(days=3)).date()
        assert inv.followups[0].due_at.date() == (inv.due_date + timedelta(days=3)).date()

        # Zweiter Scan: nichts Neues, keine Duplikate
        assert scan_account(s, account)["new"] == 0
        assert len(s.scalars(select(Document)).all()) == 2

        # Fälligkeit simulieren: erste Nachfass-Mail des Angebots ist dran, Betrieb gibt jede Mail frei
        b.review_mode = "all"
        offer.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        assert fu.in_send_window(WEEKDAY_10)
        assert not fu.in_send_window(datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc))  # Sonntag
        stats = fu.process_due(s, now=WEEKDAY_10)
        assert stats["approval"] == 1 and stats["sent"] == 0
        s.commit()
        first = offer.followups[0]
        assert first.status == FollowUpStatus.AWAITING_APPROVAL
        # Freigabe-Mail ging an den Betrieb selbst, über sein eigenes SMTP
        assert len(smtp.messages) == 1
        mail_from, rcpts, raw = smtp.messages[0]
        assert rcpts == [USER]
        text = email.message_from_bytes(raw, policy=email.policy.default).get_content()
        assert f"/f/{first.approval_token}/send" in text
        assert "Guten Tag Familie Schmidt" in text

        # Freigabe über den Link (HTTP, ohne Login)
        from locadas.main import app
        client = TestClient(app)
        r = client.get(f"/f/{first.approval_token}/send")
        assert r.status_code == 200 and 'method="post"' in r.text
        s.expire_all()
        assert s.get(FollowUp, first.id).status == FollowUpStatus.AWAITING_APPROVAL  # GET ändert nichts
        r = client.post(f"/f/{first.approval_token}/send")
        assert r.status_code == 200 and "Gesendet" in r.text
        s.expire_all()
        first = s.get(FollowUp, first.id)
        assert first.status == FollowUpStatus.SENT
        assert len(smtp.messages) == 2
        mail_from, rcpts, raw = smtp.messages[1]
        m = email.message_from_bytes(raw, policy=email.policy.default)
        assert rcpts == ["familie.schmidt@gmx.de"]
        assert mail_from == USER
        assert "Müller Haustechnik" in str(email.header.make_header(email.header.decode_header(m["From"])))
        assert m["In-Reply-To"] == offer.message_id
        assert m["Subject"] == "Angebot Gastherme"  # Originalbetreff, kein vorgetäuschtes „AW:“
        assert "A-2026-118" in m.get_content()
        b = s.get(Business, b.id)
        assert b.approvals_done == 1
        offer = s.get(Document, offer.id)
        assert offer.followups_sent == 1 and offer.next_followup_at == offer.followups[1].due_at

        # Kunde antwortet im Posteingang (als Antwort auf die Nachfass-Mail) → Nachfassen stoppt, Betrieb wird informiert
        imap.store.append("INBOX", make_mail("familie.schmidt@gmx.de", USER, "AW: Angebot Gastherme", "Passt, machen wir!", in_reply_to=m["Message-ID"]))
        stats = scan_account(s, account)
        assert stats["replies"] == 1
        s.expire_all()
        offer = s.get(Document, offer.id)
        assert offer.status == DocStatus.REPLIED
        assert all(f.status in (FollowUpStatus.SENT, FollowUpStatus.CANCELLED) for f in offer.followups)
        assert len(smtp.messages) == 3 and smtp.messages[2][1] == [USER]
        assert "hat geantwortet" in email.message_from_bytes(smtp.messages[2][2], policy=email.policy.default)["Subject"]

        # Rechnung: auch bei Automatik nie ungefragt. Erst „Noch offen?“, dann geht die Erinnerung raus
        b.review_mode = "auto"
        inv = s.get(Document, inv.id)
        inv.followups[0].due_at = WEEKDAY_10 - timedelta(hours=1)
        s.commit()
        stats = fu.process_due(s, now=WEEKDAY_10)
        assert stats["sent"] == 0 and stats["approval"] == 1
        q = email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default)
        assert smtp.messages[-1][1] == [USER] and q["Subject"].startswith("Noch offen?")
        qtext = q.get_content()
        assert "Rechnung R-2026-301 an Peter Weber über 224,91 € ist seit 03.11.2026 fällig. Noch offen?" in qtext
        inv_fu = s.get(Document, inv.id).followups[0]
        assert f"/f/{inv_fu.approval_token}/send" in qtext and f"/f/{inv_fu.approval_token}/done" in qtext
        assert fu.process_due(s, now=WEEKDAY_10 + timedelta(hours=3))["sent"] == 0  # ohne Antwort keine Erinnerung
        r = client.post(f"/f/{inv_fu.approval_token}/send")
        assert r.status_code == 200 and "Gesendet" in r.text
        m = email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default)
        assert smtp.messages[-1][1] == ["p.weber@web.de"]
        body = m.get_content()
        assert "224,91" in body and "03.11.2026" in body
        assert "Laut meinen Unterlagen ist die Rechnung noch offen." in body and "Zahlungseingang" not in body
        # Unbekannte Antwort ohne Thread-Bezug, aber vom Kunden → trotzdem erkannt
        imap.store.append("INBOX", make_mail("p.weber@web.de", USER, "Zahlung", "Ist überwiesen."))
        imap.store.append("INBOX", make_mail("spam@example.com", USER, "Werbung", "Kaufen Sie jetzt."))
        assert scan_account(s, account)["replies"] == 1
        s.expire_all()
        assert s.get(Document, inv.id).status == DocStatus.REPLIED


def test_web_connect_and_dashboard(fresh_db, imap, smtp):
    _seed_sent(imap)
    from locadas.main import app
    client = TestClient(app)
    r = client.post("/verbinden", data={
        "business_name": "Müller Haustechnik GmbH", "owner_name": "Thomas Müller", "email": USER, "password": PASSWORD,
        "imap_host": "127.0.0.1", "imap_port": str(imap.port), "smtp_host": "127.0.0.1", "smtp_port": str(smtp.port), "consent": "1", "avv": "1",
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/start"
    assert "locadas_session" in r.cookies
    # Hintergrund-Scan abwarten
    import time
    from locadas.db import MailAccount
    for _ in range(100):
        with new_session() as s:
            a = s.scalars(select(MailAccount)).first()
            if a is not None and a.last_scan_at is not None:
                break
        time.sleep(0.1)
    r = client.get("/start")
    assert r.status_code == 200 and "2 Dokumente erkannt" in r.text
    r = client.get("/dashboard")
    assert r.status_code == 200 and "Familie Schmidt" in r.text and "Peter Weber" in r.text
    with new_session() as s:
        doc = s.scalars(select(Document).where(Document.kind == DocKind.OFFER)).one()
    r = client.get(f"/dokument/{doc.id}")
    assert r.status_code == 200 and "Nachfass-Mail 1" in r.text and "Guten Tag Familie Schmidt" in r.text
    r = client.post(f"/dokument/{doc.id}/status", data={"action": "won"}, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as s:
        d = s.get(Document, doc.id)
        assert d.status == DocStatus.WON and all(f.status == FollowUpStatus.CANCELLED for f in d.followups)
    r = client.post("/einstellungen", data={"name": "Müller Haustechnik GmbH", "owner_name": "T. Müller", "followup_offers": "1", "review_mode": "all", "approval_threshold": "5.000"}, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as s:
        b = s.scalars(select(Business)).one()
        assert b.review_mode == "all" and b.approval_threshold == 5000 and not b.followup_invoices
    # Falsches Passwort im Web-Formular zeigt Fehler statt Account anzulegen
    r = client.post("/verbinden", data={"business_name": "X", "email": "neu@mueller-shk.de", "password": "nope", "imap_host": "127.0.0.1", "imap_port": str(imap.port), "smtp_host": "127.0.0.1", "smtp_port": str(smtp.port), "consent": "1", "avv": "1"})
    assert r.status_code == 200 and "Login abgelehnt" in r.text
    # Trennen löscht alles
    r = client.post("/einstellungen/trennen", data={"confirm": "ja"}, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as s:
        assert s.scalars(select(Business)).first() is None
        assert s.scalars(select(Document)).first() is None


def test_login_link(fresh_db, imap, smtp):
    from locadas.main import app
    with new_session() as s:
        _setup(s, imap, smtp)
    client = TestClient(app)
    r = client.post("/login", data={"email": USER})
    assert r.status_code == 200 and "Link ist raus" in r.text
    text = email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default).get_content()
    link = next(l for l in text.splitlines() if "/login/" in l).strip()
    r = client.get(link, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    assert client.get("/dashboard").status_code == 200
    assert client.get("/login/abgelaufen", follow_redirects=False).status_code == 400
