"""Simulation: 20 Betriebe verbinden ihr Postfach und nutzen den Nachfasser drei Wochen lang.

    cd app && python -m sim.run            # alle 20, mit Claude (wenn ANTHROPIC_API_KEY gesetzt)
    python -m sim.run shk glas             # nur ausgewählte
    python -m sim.run --no-critique        # ohne Bewertung durch Claude

Jeder Betrieb bekommt einen eigenen Test-Mailserver (IMAP + SMTP), seine echten Mails liegen im
Gesendet-Ordner, Kundenantworten / Abwesenheitsnotizen / Unzustellbar-Meldungen kommen zum geplanten
Simulationstag in den Posteingang. Die App läuft unverändert (Scan, Erkennung per Claude, Nachfassen,
Freigaben), nur die Uhr wird vorgestellt. Danach prüfen harte Checks, ob alles richtig lief, und Claude
bewertet als der jeweilige Inhaber, was er erlebt hat.
"""
from __future__ import annotations

import email
import email.policy
import re
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
_tmp = tempfile.mkdtemp(prefix="locadas-sim-")
os.environ.setdefault("DATA_DIR", _tmp)
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ.setdefault("SECRET_KEY", "sim")
os.environ.setdefault("ALLOW_INSECURE_DEV", "1")
os.environ.setdefault("BASE_URL", "https://app.locadas.de")
os.environ.setdefault("TZ", "Europe/Berlin")

from sqlalchemy import select  # noqa: E402

from locadas import db as dbmod  # noqa: E402
from locadas.db import Business, DocStatus, Document, FollowUp, FollowUpStatus, MailAccount, new_session  # noqa: E402
from locadas.engine import accounts as acc  # noqa: E402
from locadas.engine import followups as fu  # noqa: E402
from locadas.engine import scanner  # noqa: E402
from locadas.mail.connect import connect_mailbox  # noqa: E402
from locadas.mail.providers import detect_provider  # noqa: E402
from sim.personas import Event, Persona, Sent, build, pdf_attachments  # noqa: E402
from tests.fakemail import FakeImapServer, FakeSmtpServer  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")
# Bei diesen Betrieben meldet der Inhaber am Ende einen Auftrag fertig und der Kunde klickt den Bewertungslink
PORTAL = {"shk", "maler", "elektro"}
REVIEW_URL = "https://g.page/r/CsimReview123/review"
# Bei diesen Betrieben klicken die Kunden den Link in der Nachfass-Mail
CLICKERS = {k: True for k in ("shk", "glas", "solo_ch", "maler", "elektro", "pv", "fenster", "heizung", "reinigung", "volt")}
PASSWORD = "sim-pass"


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _patch_clock(clock: Clock) -> None:
    for mod in (scanner, fu, dbmod):
        mod.utcnow = clock
    import locadas.main as main
    main.utcnow = clock


def _mail(from_addr: str, from_name: str, to: list[tuple[str, str]], subject: str, body: str, date: datetime,
          pdfs: list[tuple[str, bytes]] = (), in_reply_to: str = "", headers: dict | None = None) -> tuple[bytes, str]:
    m = EmailMessage()
    m["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    m["To"] = ", ".join(f"{n} <{a}>" if n else a for n, a in to)
    m["Subject"] = subject
    m["Date"] = format_datetime(date)
    mid = make_msgid(domain=from_addr.split("@")[-1])
    m["Message-ID"] = mid
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    for name, data in pdfs:
        m.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return m.as_bytes(), mid


def _sim_days(start: datetime, n: int) -> list[datetime]:
    """Jeder Kalendertag um 10:30 Berliner Zeit."""
    base = start.astimezone(BERLIN).replace(hour=10, minute=30, second=0, microsecond=0)
    return [(base + timedelta(days=i)).astimezone(timezone.utc) for i in range(n)]


def run_persona(p: Persona, days: int = 22) -> dict:
    dbmod.reset_engine_for_tests(f"sqlite:///{_tmp}/{p.key}.db")
    imap = FakeImapServer(p.email, PASSWORD, sent=p.sent_folder, sent_flag=p.sent_flag).start()
    smtp = FakeSmtpServer().start()
    t0 = datetime.now(timezone.utc).astimezone(BERLIN).replace(hour=9, minute=0, second=0, microsecond=0)
    # Start immer an einem Montag, damit Wochenenden realistisch drin sind
    t0 = (t0 - timedelta(days=t0.weekday())).astimezone(timezone.utc)
    clock = Clock(t0)
    _patch_clock(clock)

    mids: dict[str, str] = {}   # Dokumentnummer -> Message-ID der Ursprungsmail
    for s in p.sent:
        raw, mid = _mail(p.email, p.business, s.to, s.subject, s.body, t0 - timedelta(days=s.days_ago), pdf_attachments(s))
        imap.store.append(p.sent_folder, raw)
        for d in s.docs:
            mids.setdefault(d.number, mid)
        if s.key:
            mids[s.key] = mid

    checks: list[tuple[bool, str]] = []
    timeline: list[str] = []

    def check(ok: bool, msg: str):
        checks.append((bool(ok), msg))

    # --- Verbinden ---------------------------------------------------------
    provider = detect_provider(p.email)
    connect_note = "eigene Domain (Server automatisch gesucht)" if provider is None else f"{provider.label}" + (" (App-Passwort nötig)" if provider.needs_app_password else "") + (" (NUR OAuth möglich)" if provider.oauth_only else "")
    cfg = connect_mailbox(p.email, PASSWORD, imap_host="127.0.0.1", imap_port=imap.port, smtp_host="127.0.0.1", smtp_port=smtp.port, imap_ssl=False, smtp_ssl=False)
    check(cfg.sent_folder == p.sent_folder, f"Gesendet-Ordner gefunden: {cfg.sent_folder!r} (erwartet {p.sent_folder!r})")
    cfg.provider_key = provider.key if provider else "custom"
    with new_session() as s:
        b = Business(name=p.business, owner_name=p.owner, email=p.email, phone=p.phone)
        s.add(b)
        s.flush()
        account = acc.create_account(s, b, cfg, PASSWORD)
        s.commit()
        bid, aid = b.id, account.id

    t_scan = time.time()
    with new_session() as s:
        stats = scanner.scan_account(s, s.get(MailAccount, aid))
    scan_secs = time.time() - t_scan
    timeline.append(f"Tag 0: Postfach verbunden ({connect_note}), Erst-Scan {stats['new']} Dokumente in {scan_secs:.0f} s")

    # --- Erkennung prüfen --------------------------------------------------------
    detected = []
    with new_session() as s:
        docs = s.scalars(select(Document).where(Document.business_id == bid)).all()
        for d in docs:
            detected.append({
                "kind": d.kind.value, "number": d.document_number, "customer": d.customer_name, "email": d.customer_email,
                "amount": d.amount_gross, "currency": d.currency, "due": d.due_date.date().isoformat() if d.due_date else None,
                "valid_until": d.valid_until.date().isoformat() if d.valid_until else None, "title": d.title,
                "source": d.extraction_source, "needs_review": d.needs_review,
            })
        for sent in p.sent:
            if sent.expect is None:
                bad = [d for d in docs if d.subject == sent.subject and d.message_id == mids.get(sent.key or (sent.docs[0].number if sent.docs else ""), "x")]
                check(not bad, f"Ignoriert: „{sent.subject}“ an {sent.to[0][1]}")
                continue
            e = sent.expect
            match = [d for d in docs if (e["number"] and d.document_number == e["number"]) or (not e["number"] and d.customer_email == e["customer"])]
            if not match:
                check(False, f"Erkannt: {e['kind']} {e['number'] or ''} an {e['customer']} (fehlt komplett)")
                continue
            d = match[0]
            check(d.kind.value == e["kind"], f"{e['number'] or sent.subject}: Art {d.kind.value} (erwartet {e['kind']})")
            check(d.customer_email == e["customer"], f"{e['number'] or sent.subject}: Kunde {d.customer_email} (erwartet {e['customer']})")
            if e["amount"] is not None:
                check(d.amount_gross is not None and abs(d.amount_gross - e["amount"]) < 0.02, f"{e['number']}: Betrag {d.amount_gross} (erwartet {e['amount']})")
        # Kollegen / Steuerberater / Weiterleitung dürfen nirgends als Kunde stehen
        for d in docs:
            check(not d.customer_email.endswith(p.email.split("@")[1]) or p.email.split("@")[1] in ("gmail.com", "gmx.de", "web.de", "t-online.de", "outlook.de", "icloud.com", "gmx.ch"),
                  f"{d.document_number}: Kunde ist kein Kollege ({d.customer_email})")

    # --- Drei Wochen laufen lassen -------------------------------------------------
    sent_before = 0
    approvals_mails = 0
    notifications: list[tuple[str, str, str]] = []
    customer_mails: list[tuple[str, str, str, str]] = []
    events_by_day: dict[int, list[Event]] = {}
    for ev in p.events:
        events_by_day.setdefault(ev.day, []).append(ev)

    def collect(day_label: str):
        nonlocal sent_before, approvals_mails
        for frm, rcpts, raw in smtp.messages[sent_before:]:
            m = email.message_from_bytes(raw, policy=email.policy.default)
            body = m.get_body(preferencelist=("plain",)).get_content() if m.is_multipart() else m.get_content()
            if rcpts == [p.email]:
                notifications.append((day_label, str(m["Subject"]), body))
                if "freigeben" in str(m["Subject"]).lower() or str(m["Subject"]).startswith("Nachfassen bei"):
                    approvals_mails += 1
            else:
                customer_mails.append((day_label, ", ".join(rcpts), str(m["Subject"]), body))
                tok = re.search(r"/a/([A-Za-z0-9_-]+)", body)
                if tok and clicker and tok.group(1) not in opened_tokens:
                    to_click.setdefault(int(day_label.split()[1]) + 1, []).append(tok.group(1))
        sent_before = len(smtp.messages)

    from fastapi.testclient import TestClient
    import locadas.main as appmain
    web = TestClient(appmain.app)
    clicker = CLICKERS.get(p.key, False)
    to_click: dict[int, list[str]] = {}
    opened_tokens: set[str] = set()

    for i, day in enumerate(_sim_days(t0, days)):
        clock.now = day
        for token in to_click.pop(i, []):
            if token in opened_tokens:
                continue
            r = web.get(f"/a/{token}")
            opened_tokens.add(token)
            timeline.append(f"Tag {i}: Kunde klickt den Link in der Nachfass-Mail (HTTP {r.status_code})")
        label = f"Tag {i} ({day.astimezone(BERLIN).strftime('%a %d.%m.')})"
        for ev in events_by_day.get(i, []):
            if ev.kind == "sent" and ev.sent is not None:
                raw, mid = _mail(p.email, p.business, ev.sent.to, ev.sent.subject, ev.sent.body, day - timedelta(hours=1), pdf_attachments(ev.sent))
                imap.store.append(p.sent_folder, raw)
                timeline.append(f"{label}: Betrieb verschickt „{ev.sent.subject}“")
            elif ev.kind == "manual":
                to = None
                with new_session() as s:
                    d = s.scalar(select(Document).where(Document.business_id == bid, Document.document_number == ev.doc))
                    to = [(d.customer_name, d.customer_email)] if d else [("", "x@x.de")]
                raw, _ = _mail(p.email, p.owner, to, ev.subject, ev.body, day - timedelta(hours=1), in_reply_to=mids.get(ev.doc, ""))
                imap.store.append(p.sent_folder, raw)
                timeline.append(f"{label}: Inhaber fasst selbst nach („{ev.subject}“)")
            elif ev.kind == "loose":
                # Kunde schreibt ohne Thread-Bezug von einer anderen Adresse
                raw, _ = _mail(ev.from_addr, "", [("", p.email)], ev.subject, ev.body, day - timedelta(hours=2))
                imap.store.append("INBOX", raw)
                timeline.append(f"{label}: Posteingang: Mail ohne Bezug von {ev.from_addr} („{ev.subject}“)")
            else:
                hdr = {}
                if ev.kind == "autoreply":
                    hdr = {"Auto-Submitted": "auto-replied"}
                if ev.kind == "bounce":
                    raw, _ = _mail(ev.from_addr, "Mail Delivery System", [("", p.email)], ev.subject, ev.body + f"\n\n--- Original message ---\nMessage-ID: {mids.get(ev.doc, '')}\n", day - timedelta(hours=2))
                else:
                    raw, _ = _mail(ev.from_addr, "", [("", p.email)], ev.subject, ev.body, day - timedelta(hours=2), in_reply_to=mids.get(ev.doc, ""), headers=hdr)
                imap.store.append("INBOX", raw)
                timeline.append(f"{label}: Posteingang: {ev.kind} von {ev.from_addr} („{ev.subject}“)")
        with new_session() as s:
            scanner.scan_account(s, s.get(MailAccount, aid))
            due_stats = fu.process_due(s, now=day)
        collect(label)
        if due_stats["sent"] or due_stats["approval"] or due_stats.get("capped"):
            timeline.append(f"{label}: {due_stats['sent']} Nachfass-Mails gesendet, {due_stats['approval']} zur Freigabe, {due_stats.get('capped', 0)} wegen Tageslimit verschoben")
        # Inhaber reagiert noch am selben Tag auf Freigaben
        with new_session() as s:
            pending = s.scalars(select(FollowUp).join(Document).where(Document.business_id == bid, FollowUp.status == FollowUpStatus.AWAITING_APPROVAL)).all()
            for f in pending:
                d = f.document
                decision = p.decisions.get(d.document_number) or ("stop" if (d.needs_review and d.kind.value == "invoice") else "send")
                if decision == "send":
                    fu.approve(s, f)
                elif decision == "stop":
                    fu.close_document(s, d, DocStatus.PAID if d.kind.value == "invoice" else DocStatus.REPLIED, "Vom Betrieb als erledigt markiert")
                else:
                    fu.skip(s, f)
                s.commit()
                timeline.append(f"{label}: Inhaber entscheidet {decision} für {d.kind_label} {d.document_number or d.title}")
        collect(label)
        for ev in events_by_day.get(i, []):
            if ev.expect_status:
                with new_session() as s:
                    d = s.scalar(select(Document).where(Document.business_id == bid, Document.document_number == ev.doc))
                    ok_states = {"replied", "won", "lost", "paid"} if ev.expect_status == "replied" else {ev.expect_status}
                    check(d is not None and d.status.value in ok_states, f"{label}: nach {ev.kind} ist {ev.doc} „{d.status.value if d else 'fehlt'}“ (erwartet „{ev.expect_status}“)")
            if ev.kind == "manual":
                with new_session() as s:
                    d = s.scalar(select(Document).where(Document.business_id == bid, Document.document_number == ev.doc))
                    nxt = d.next_followup_at if d else None
                    check(nxt is not None and nxt >= day + timedelta(days=3), f"{label}: nach eigenem Nachfassen nächste Stufe frühestens in 3 Tagen ({nxt and nxt.astimezone(BERLIN).strftime('%d.%m.')})")

    # --- Kundenportal: Auftrag fertig, Kunde klickt den Bewertungslink -------------------------
    if p.key in PORTAL:
        portal_run(p, bid, web, smtp, f"Tag {i} (Ende)", check, timeline, collect)

    # --- Invarianten ------------------------------------------------------------------
    with new_session() as s:
        docs = s.scalars(select(Document).where(Document.business_id == bid)).all()
        for d in docs:
            sent_fus = [f for f in d.followups if f.status == FollowUpStatus.SENT]
            for f in sent_fus:
                wd = f.sent_at.astimezone(BERLIN).weekday()
                check(wd < 5, f"{d.document_number}: Nachfass-Mail {f.step} nicht am Wochenende ({f.sent_at.astimezone(BERLIN):%a})")
                if d.closed_at:
                    check(f.sent_at <= d.closed_at, f"{d.document_number}: keine Nachfass-Mail nach Abschluss")
            check(len(sent_fus) <= 3, f"{d.document_number}: höchstens 3 Nachfass-Mails ({len(sent_fus)})")
            if p.decisions.get(d.document_number) == "stop":
                check(not sent_fus, f"{d.document_number}: als erledigt markiert, nie nachgefasst ({len(sent_fus)} gesendet)")
        # Revision
        for ev in p.events:
            if ev.kind == "sent" and ev.sent and ev.sent.docs:
                nd = ev.sent.docs[0]
                d = next((x for x in docs if x.document_number == nd.number), None)
                check(d is not None and d.amount_gross is not None and abs(d.amount_gross - nd.payable) < 0.02, f"Überarbeitung {nd.number}: Betrag aktualisiert auf {d.amount_gross if d else None} (erwartet {nd.payable})")
                check(len([x for x in docs if x.document_number == nd.number]) == 1, f"Überarbeitung {nd.number}: kein Duplikat")
    if clicker:
        with new_session() as s:
            opened = s.scalars(select(Document).where(Document.business_id == bid, Document.first_opened_at.is_not(None))).all()
        check(len(opened) >= len(opened_tokens) > 0 if opened_tokens else True, f"Geöffnet-Status sichtbar ({len(opened)} Dokumente geöffnet, {len(opened_tokens)} Klicks)")
        check(any("geöffnet" in subj for _, subj, _ in notifications) if opened_tokens else True, "Betrieb bekommt Mail „… hat geöffnet“")
    # Texte an Kunden: ehrlicher Betreff, Frist mit Datum, kein Weichspüler, keine Drohung, richtige Währung
    chf = any(d["currency"] == "CHF" for d in detected)
    for day_label, to, subj, body in customer_mails:
        check(not subj.lower().startswith(("aw:", "re:")), f"{day_label}: Betreff ohne vorgetäuschtes „AW:“ ({subj})")
        if subj.startswith(("Zahlungserinnerung", "Letzte Zahlungserinnerung")):
            check(re.search(r"bis \d{2}\.\d{2}\.\d{4}", body) is not None, f"{day_label}: Zahlungserinnerung mit Datum ({subj})")
        bad = re.search(r"untergegangen|ignorieren|letzte Nachricht|Ja, Nein oder", body)
        check(bad is None, f"{day_label}: keine Weichspüler-/Drohformel in „{subj}“ ({bad.group(0) if bad else ''})")
        if chf:
            check("€" not in body, f"{day_label}: CHF-Betrieb ohne €-Zeichen ({subj})")
    per_day: dict[str, int] = {}
    for day_label, *_ in notifications:
        per_day[day_label] = per_day.get(day_label, 0) + 1
    check(max(per_day.values(), default=0) <= 4, f"Höchstens 4 Mails von uns an den Betrieb pro Tag (max {max(per_day.values(), default=0)})")
    check(len(imap.appended) >= len(customer_mails) if cfg.provider_key not in ("gmail", "outlook") else True, f"Nachfass-Mails im Gesendet-Ordner abgelegt ({len(imap.appended)} von {len(customer_mails)})")

    imap.stop()
    smtp.stop()
    return {
        "persona": p,
        "connect": connect_note,
        "detected": detected,
        "checks": checks,
        "timeline": timeline,
        "customer_mails": customer_mails,
        "notifications": notifications,
        "approval_mails": approvals_mails,
    }


def portal_run(p: Persona, bid: int, web, smtp, label: str, check, timeline: list[str], collect) -> None:
    """Inhaber hinterlegt den Google-Link, meldet einen gewonnenen (sonst den jüngsten) Auftrag fertig, Kunde klickt."""
    from locadas.db import Customer, DocKind
    from locadas.engine import customers as cust
    from locadas.web import auth

    with new_session() as s:
        b = s.get(Business, bid)
        b.google_review_url = REVIEW_URL
        docs = s.scalars(select(Document).where(Document.business_id == bid, Document.customer_email != "").order_by(Document.sent_at)).all()
        won = [d for d in docs if d.kind == DocKind.OFFER and d.status == DocStatus.WON]
        target = (won or docs)[-1] if docs else None
        c = cust.for_document(s, target) if target else None
        s.commit()
        if c is None:
            check(False, "Kundenportal: kein Dokument mit Kunde gefunden")
            return
        cid, addr, who = c.id, c.email, f"{target.kind_label} {target.document_number or target.title}"
    web.cookies.set(auth.COOKIE, auth.session_token(bid))
    before = len(smtp.messages)
    r = web.post(f"/kunde/{cid}/fertig", data={"next": "/dashboard"}, follow_redirects=False)
    r2 = web.post(f"/kunde/{cid}/fertig", data={"next": "/dashboard"}, follow_redirects=False)  # Doppelklick
    web.cookies.clear()
    mails = [(rc, raw) for _, rc, raw in smtp.messages[before:] if rc == [addr]]
    check(r.status_code == 303 and "msg=fertig" in r.headers.get("location", ""), f"Kundenportal: Fertig melden für {who} ({r.status_code} {r.headers.get('location')})")
    check("fertig_schon" in r2.headers.get("location", ""), "Kundenportal: zweites Fertig melden schickt nichts")
    check(len(mails) == 1, f"Kundenportal: genau eine Abschluss-Mail an {addr} ({len(mails)})")
    collect(label)
    if not mails:
        return
    m = email.message_from_bytes(mails[0][1], policy=email.policy.default)
    body = m.get_content()
    portal = re.search(r"https://app\.locadas\.de(/k/[A-Za-z0-9_-]+)\s", body)
    review = re.search(r"https://app\.locadas\.de(/k/[A-Za-z0-9_-]+/bewerten)", body)
    check(portal is not None and review is not None, f"Kundenportal: Abschluss-Mail mit Portal- und Bewertungslink („{m['Subject']}“)")
    check(not str(m["Subject"]).lower().startswith(("aw:", "re:")) and not m["In-Reply-To"], "Kundenportal: Abschluss-Mail ohne vorgetäuschtes „AW:“")
    if not (portal and review):
        return
    page = web.get(portal.group(1))
    check(page.status_code == 200 and "Ihr Auftrag ist fertig" in page.text and "Bei Google bewerten" in page.text, "Kundenportal: Seite zeigt Abschluss und Google-Karte")
    others = [d for d in docs if (d.customer_email or "").lower() != addr]
    leaked = [d.document_number for d in others if d.document_number and d.document_number in page.text]
    check(not leaked, f"Kundenportal: keine Unterlagen anderer Kunden ({leaked})")
    rr = web.get(review.group(1), follow_redirects=False)
    check(rr.status_code == 302 and rr.headers.get("location") == REVIEW_URL, f"Kundenportal: Bewertungslink leitet zu Google ({rr.status_code})")
    with new_session() as s:
        c = s.get(Customer, cid)
        check(c.completed_at is not None and c.review_requested_at is not None and c.review_clicked_at is not None, "Kundenportal: Fertig, Bewertungsbitte und Klick gespeichert")
    timeline.append(f"{label}: Inhaber meldet {who} fertig, Kunde bekommt die Abschluss-Mail und klickt auf „Bei Google bewerten“")


# --- Bewertung durch Claude als Inhaber ----------------------------------------------

CRITIC = """Du bist {owner}, Inhaber von {business} ({trade}, {size}). So arbeitest du: {workflow}
Dein Charakter: {character}

Du hast gerade drei Wochen lang den „Locadas Nachfasser“ getestet: Du hast dein Postfach verbunden, das Tool hat deine verschickten Angebote und Rechnungen erkannt und in deinem Namen bei Kunden nachgefasst, bis sie antworten. Angebote liefen automatisch mit einer Vorschau am Morgen (große Angebote nur mit deiner Freigabe), vor jeder Zahlungserinnerung hat das Tool dich gefragt, ob die Rechnung noch offen ist. Das Tool ist kostenlos.

Hier ist, was du erlebt hast. Sei ein kritischer, ehrlicher Handwerker, kein netter Tester. Wenn etwas peinlich gegenüber deinen Kunden wäre, sag es hart. Wenn es dir Geld bringt, sag das auch.

VERBINDEN: {connect}

ERKANNTE DOKUMENTE:
{detected}

ABLAUF:
{timeline}

MAILS, DIE IN DEINEM NAMEN AN KUNDEN GINGEN (Auswahl):
{customer_mails}

MAILS VOM TOOL AN DICH (Auswahl):
{notifications}

FEHLER, DIE DIE TECHNIK GEFUNDEN HAT:
{failed}

Antworte NUR mit JSON:
{{"score": 1-10 (10 = absolutes Brett, würde ich jedem Kollegen empfehlen),
  "would_keep_using": true/false,
  "verdict": "1-2 Sätze in deinen Worten, wie du es einem Kollegen erzählen würdest",
  "loved": ["max 3 Punkte"],
  "complaints": ["max 4 konkrete Kritikpunkte, so konkret wie möglich: welcher Satz, welche Mail, welcher Ablauf"],
  "must_fix_before_i_tell_colleagues": "der eine wichtigste Punkt"}}"""


def critique(r: dict, model: str) -> dict | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    p: Persona = r["persona"]
    det = "\n".join(
        f"- {d['kind']} {d['number'] or '(ohne Nummer)'} | Kunde {d['customer']} <{d['email']}> | {d['amount']} {d['currency']} | fällig {d['due']} | Titel „{d['title']}“{' | als „schon erledigt?“ markiert' if d['needs_review'] else ''}"
        for d in r["detected"]
    ) or "(nichts)"
    cm = "\n\n".join(f"[{day}] an {to}\nBetreff: {subj}\n{body}" for day, to, subj, body in r["customer_mails"][:4]) or "(keine)"
    nt = "\n\n".join(f"[{day}] Betreff: {subj}\n{body[:1200]}" for day, subj, body in r["notifications"][:4]) or "(keine)"
    failed = "\n".join(f"- {m}" for ok, m in r["checks"] if not ok) or "(keine)"
    prompt = CRITIC.format(
        owner=p.owner, business=p.business, trade=p.trade, size=p.size, workflow=p.workflow, character=p.character,
        connect=r["connect"] + (f". Hinweis: {p.note}" if p.note else ""), detected=det, timeline="\n".join(r["timeline"]),
        customer_mails=cm, notifications=nt, failed=failed,
    )
    client = anthropic.Anthropic()
    for _ in range(3):
        try:
            resp = client.messages.create(model=model, max_tokens=6000, messages=[{"role": "user", "content": prompt}])
            raw = "".join(getattr(b, "text", "") for b in resp.content)
            return json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        except Exception as exc:  # noqa: BLE001
            err = exc
            time.sleep(3)
    return {"score": None, "error": str(err)}


def main(argv: list[str]) -> int:
    only = [a for a in argv if not a.startswith("--")]
    do_critique = "--no-critique" not in argv
    model = os.environ.get("CRITIC_MODEL", "claude-sonnet-5-5")
    personas = [p for p in build() if not only or p.key in only]
    results = []
    for p in personas:
        t = time.time()
        r = run_persona(p)
        r["critique"] = critique(r, model) if do_critique else None
        fails = [m for ok, m in r["checks"] if not ok]
        sc = r["critique"].get("score") if r["critique"] else "–"
        print(f"{p.key:12} checks {len(r['checks']) - len(fails):3}/{len(r['checks']):<3} score {sc}  ({time.time() - t:.0f}s)", flush=True)
        for m in fails:
            print(f"    ✗ {m}")
        results.append(r)
    out = HERE / "report.json"
    out.write_text(json.dumps([{
        "key": r["persona"].key, "business": r["persona"].business, "owner": r["persona"].owner, "trade": r["persona"].trade,
        "connect": r["connect"], "checks": [{"ok": ok, "msg": m} for ok, m in r["checks"]], "detected": r["detected"],
        "timeline": r["timeline"], "customer_mails": r["customer_mails"], "notifications": r["notifications"], "critique": r["critique"],
    } for r in results], ensure_ascii=False, indent=1, default=str))
    total = sum(len(r["checks"]) for r in results)
    bad = sum(1 for r in results for ok, _ in r["checks"] if not ok)
    scores = [r["critique"]["score"] for r in results if r["critique"] and r["critique"].get("score")]
    print(f"\nChecks: {total - bad}/{total} ok · Ø Bewertung {sum(scores) / len(scores):.1f}/10 ({len(scores)} Betriebe)" if scores else f"\nChecks: {total - bad}/{total} ok")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
