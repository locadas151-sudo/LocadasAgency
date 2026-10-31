"""Betrieb: Sicherung, Health-Check, STARTTLS am SMTP-Empfänger, paralleler Scan-Durchlauf. Ohne Netzwerk."""
from __future__ import annotations

import gzip
import smtplib
import socket
import sqlite3
import ssl
import threading
import time


import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from locadas.config import settings
from locadas.db import AccountStatus, Business, Document, MailAccount, get_engine, new_session, utcnow
from locadas.engine import scheduler
from locadas.ops import backup, health, state
from tests.conftest import PASSWORD, USER
from tests.fakemail import make_mail, make_pdf


# --- Sicherung --------------------------------------------------------------------------------------

def test_backup_creates_restorable_gzip_and_keeps_14(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with new_session() as s:
        s.add(Business(name="Sicher GmbH", email="a@b.de"))
        s.commit()
    info = backup.run_backup()
    assert info["ok"] and info["file"].startswith("locadas-") and info["file"].endswith(".sqlite.gz")
    path = tmp_path / "data" / "backups" / info["file"]
    raw = tmp_path / "restore.sqlite"
    backup.restore(path, raw)
    con = sqlite3.connect(raw)
    assert con.execute("SELECT name FROM businesses").fetchone()[0] == "Sicher GmbH"
    con.close()
    with gzip.open(path) as fh:
        assert fh.read(16).startswith(b"SQLite format 3")
    with new_session() as s:
        assert state.get(s, "last_backup")["file"] == info["file"]
    # 20 alte Sicherungen: es bleiben die neuesten 14
    for i in range(1, 21):
        (tmp_path / "data" / "backups" / f"locadas-202601{i:02d}.sqlite.gz").write_bytes(b"x")
    gone = backup.prune()
    names = [b["file"] for b in backup.list_backups()]
    assert len(names) == 14 and names[0] == info["file"] and "locadas-20260101.sqlite.gz" in gone


def test_backup_s3_skipped_without_boto3(fresh_db, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "backup_s3_bucket", "locadas-backups")
    import builtins

    real_import = builtins.__import__

    def no_boto(name, *a, **kw):
        if name == "boto3":
            raise ImportError("no boto3")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_boto)
    info = backup.run_backup()
    assert info["ok"] and info["s3"] == "" and "boto3 ist nicht installiert" in caplog.text


def test_backup_uploads_with_boto3(fresh_db, tmp_path, monkeypatch):
    import sys
    import types

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "backup_s3_bucket", "bucket")
    uploads = []
    fake = types.SimpleNamespace(client=lambda name: types.SimpleNamespace(upload_file=lambda f, b, k, ExtraArgs=None: uploads.append((b, k))))
    monkeypatch.setitem(sys.modules, "boto3", fake)
    info = backup.run_backup()
    assert info["s3"].startswith("s3://bucket/locadas/locadas-") and uploads[0][0] == "bucket"


# --- Health -----------------------------------------------------------------------------------------

def _client():
    from locadas.main import app

    return TestClient(app)


def test_healthz(fresh_db, monkeypatch):
    c = _client()
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json()["db"] == "ok" and r.json()["scheduler"] == "disabled"
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(health.state, "scheduler", None)
    r = c.get("/healthz")
    assert r.status_code == 503 and r.json()["scheduler"] == "stopped"
    monkeypatch.setattr(health.state, "scheduler", type("S", (), {"running": True})())
    monkeypatch.setattr(scheduler, "last_cycle_at", time.time() - 60)
    assert c.get("/healthz").status_code == 200
    monkeypatch.setattr(scheduler, "last_cycle_at", time.time() - 3 * 3600)  # Durchlauf hängt
    r = c.get("/healthz")
    assert r.status_code == 503 and r.json()["last_cycle_age_s"] > 3600
    monkeypatch.setattr(scheduler, "last_cycle_at", time.time())
    monkeypatch.setattr(settings, "inbound_smtp_port", _free_port())  # niemand lauscht
    monkeypatch.setattr(health.state, "inbound", None)
    r = c.get("/healthz")
    assert r.status_code == 503 and r.json()["inbound_smtp"] == "down"


# --- STARTTLS ---------------------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_inbound_smtp_offers_starttls_with_self_signed_cert(fresh_db, tmp_path, monkeypatch):
    from locadas.engine import trial
    from locadas.mail import inbound_smtp
    from locadas.notify import inbound as wa_inbound

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "inbound_domain", "angebot.locadas.de")
    monkeypatch.setattr(wa_inbound, "MEDIA_ASYNC", False)
    with new_session() as s:
        b = trial.create(s, "Thomas", "Müller Haustechnik", "491712345678")
        s.commit()
        addr = b.inbound_address
    port = _free_port()
    ctrl = inbound_smtp.start(port=port, host="127.0.0.1")
    try:
        assert (tmp_path / "data" / "tls" / "inbound.crt").exists()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # wie sendende MTAs bei opportunistischem TLS
        with smtplib.SMTP("127.0.0.1", port, timeout=10) as smtp:
            smtp.ehlo("mx.example.org")
            assert smtp.has_extn("starttls")
            smtp.starttls(context=ctx)
            smtp.ehlo("mx.example.org")
            assert isinstance(smtp.sock, ssl.SSLSocket) and smtp.sock.version() in ("TLSv1.2", "TLSv1.3")
            with pytest.raises(smtplib.SMTPRecipientsRefused):  # auch mit TLS kein Relay
                smtp.sendmail("x@y.de", ["opfer@gmail.com"], b"Subject: spam\r\n\r\nspam")
            raw = make_mail("info@mueller-shk.de", addr, "WG: Notiz", "ohne PDF").replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            assert smtp.sendmail("info@mueller-shk.de", [addr], raw) == {}
        with smtplib.SMTP("127.0.0.1", port, timeout=10) as smtp:  # ohne TLS geht es weiter (opportunistisch)
            assert smtp.sendmail("info@mueller-shk.de", [addr], raw) == {}
    finally:
        ctrl.stop()
    # Zweiter Start nimmt dasselbe Zertifikat
    before = (tmp_path / "data" / "tls" / "inbound.crt").read_bytes()
    assert inbound_smtp.ensure_cert() is not None and (tmp_path / "data" / "tls" / "inbound.crt").read_bytes() == before


def test_inbound_tls_uses_configured_cert(tmp_path, monkeypatch):
    from locadas.mail import inbound_smtp

    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    inbound_smtp._generate_cryptography(cert, key, "angebot.locadas.de")
    monkeypatch.setattr(settings, "inbound_tls_cert", str(cert))
    monkeypatch.setattr(settings, "inbound_tls_key", str(key))
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    assert inbound_smtp.ensure_cert() == (cert, key)
    assert inbound_smtp.tls_context() is not None
    assert not (tmp_path / "data" / "tls").exists()


# --- Paralleler Durchlauf ----------------------------------------------------------------------------

def _accounts(s, n: int, imap_port: int = 1, smtp_port: int = 1) -> list[int]:
    from locadas.crypto import encrypt

    ids = []
    for i in range(n):
        b = Business(name=f"Betrieb {i}", email=f"info{i}@betrieb.de")
        s.add(b)
        s.flush()
        a = MailAccount(business_id=b.id, provider="custom", email=USER, username=USER, secret_enc=encrypt(PASSWORD),
                        imap_host="127.0.0.1", imap_port=imap_port, imap_ssl=False, smtp_host="127.0.0.1", smtp_port=smtp_port,
                        smtp_ssl=False, sent_folder="Gesendet", status=AccountStatus.ACTIVE)
        s.add(a)
        s.flush()
        ids.append(a.id)
    s.commit()
    return ids


def test_sqlite_wal_and_busy_timeout(fresh_db):
    with get_engine().connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() >= 10000


def test_cycle_scans_30_accounts_in_parallel_without_double_processing(fresh_db, monkeypatch):
    with new_session() as s:
        ids = _accounts(s, 30)
    active: set[int] = set()
    seen: list[int] = []
    guard = threading.Lock()
    max_parallel = [0]
    doubled = []

    def fake_scan(session, account, now=None, force=False):
        with guard:
            if account.id in active:
                doubled.append(account.id)
            active.add(account.id)
            max_parallel[0] = max(max_parallel[0], len(active))
        time.sleep(0.05)
        account.last_scan_at = utcnow()
        session.commit()
        with guard:
            active.discard(account.id)
            seen.append(account.id)
        return {"new": 1, "replies": 0}

    monkeypatch.setattr(scheduler, "scan_account", fake_scan)
    monkeypatch.setattr(settings, "scan_workers", 8)
    monkeypatch.setattr(scheduler, "_executor", None)
    # Gleichzeitig ein „Jetzt prüfen“ für dasselbe Postfach: wird übersprungen statt doppelt zu laufen
    extra = threading.Thread(target=lambda: [scheduler.scan_one(ids[0], force=True) for _ in range(5)])
    t0 = time.monotonic()
    extra.start()
    totals = scheduler.run_cycle()
    extra.join()
    took = time.monotonic() - t0
    assert not doubled
    assert set(seen) == set(ids)
    assert totals["new"] == totals["accounts"] and totals["accounts"] + totals.get("skipped", 0) == 30
    assert max_parallel[0] > 1 and took < 30 * 0.05  # parallel, nicht nacheinander
    assert scheduler.last_cycle_at is not None
    with new_session() as s:
        assert all(a.last_scan_at is not None for a in s.scalars(select(MailAccount)).all())


def test_cycle_does_not_wait_for_hanging_account(fresh_db, monkeypatch):
    with new_session() as s:
        ids = _accounts(s, 4)
    release = threading.Event()

    def fake_scan(session, account, now=None, force=False):
        if account.id == ids[0]:
            release.wait(10)
        return {"new": 0, "replies": 0}

    monkeypatch.setattr(scheduler, "scan_account", fake_scan)
    monkeypatch.setattr(settings, "scan_account_timeout", 1)
    monkeypatch.setattr(scheduler, "_executor", None)
    t0 = time.monotonic()
    totals = scheduler.run_cycle()
    assert time.monotonic() - t0 < 5 and totals["timed_out"] == 1 and totals["accounts"] == 3
    # Nächster Durchlauf: das hängende Postfach ist noch gesperrt und wird übersprungen
    totals = scheduler.run_cycle()
    assert totals.get("skipped") == 1
    release.set()
    time.sleep(0.2)
    assert scheduler.account_lock(ids[0]).acquire(blocking=False)
    scheduler.account_lock(ids[0]).release()


def test_cycle_with_real_fake_imap_30_accounts(fresh_db, imap, smtp, monkeypatch):
    """30 Postfächer gegen den Fake-IMAP-Server, jedes erkennt sein Angebot genau einmal."""
    pdf = make_pdf(["Müller Haustechnik GmbH", "Angebot Nr. A-2026-500", "Familie Schmidt", "Gesamtbetrag brutto 1.234,00 EUR"])
    imap.store.append("Gesendet", make_mail(USER, "familie.schmidt@gmx.de", "Angebot Heizung", "Anbei.", ("Angebot_A-2026-500.pdf", pdf), to_name="Familie Schmidt"))
    with new_session() as s:
        _accounts(s, 30, imap.port, smtp.port)
    monkeypatch.setattr(scheduler, "_executor", None)
    totals = scheduler.run_cycle()
    assert totals["accounts"] == 30 and totals["new"] == 30 and not totals.get("failed")
    totals = scheduler.run_cycle()
    assert totals["new"] == 0  # zweiter Durchlauf: nichts doppelt
    with new_session() as s:
        docs = s.scalars(select(Document)).all()
        assert len(docs) == 30 and len({d.business_id for d in docs}) == 30
        assert all(a.status == AccountStatus.ACTIVE for a in s.scalars(select(MailAccount)).all())




def test_prune_ops_data_removes_deleted_businesses(fresh_db):
    from datetime import timedelta

    from locadas.db import NotificationLog, OpsLog

    with new_session() as s:
        b = Business(name="Bleibt", email="a@b.de")
        s.add(b)
        s.flush()
        s.add_all([NotificationLog(business_id=b.id, kind="info"), NotificationLog(business_id=b.id + 99, kind="info"),
                   NotificationLog(business_id=None, kind="admin:watch", created_at=utcnow() - timedelta(days=200)),
                   OpsLog(business_id=b.id + 99, message="Familie Brandt"), OpsLog(business_id=None, message="allgemein")])
        s.commit()
    assert backup.prune_ops_data() == {"notify_orphans": 1, "notify_old": 1, "log_orphans": 1}
    with new_session() as s:
        assert len(s.scalars(select(NotificationLog)).all()) == 1 and len(s.scalars(select(OpsLog)).all()) == 1
