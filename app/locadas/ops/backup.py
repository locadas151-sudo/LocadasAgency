"""Nächtliche Sicherung der SQLite-Datenbank (Online-Backup, App läuft weiter).

DATA_DIR/backups/locadas-YYYYMMDD.sqlite.gz, die neuesten BACKUP_KEEP (14) bleiben. Mit BACKUP_S3_BUCKET zusätzlich
nach S3, wenn boto3 installiert ist (sonst nur ein Hinweis im Log). PDFs liegen daneben in DATA_DIR/pdf und sind
nicht Teil dieser Sicherung (siehe DEPLOY.md, Volume-Sicherung).
Wiederherstellen: siehe DEPLOY.md, Abschnitt „Sicherung und Wiederherstellung“.
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import settings
from ..db import new_session, utcnow
from . import state

log = logging.getLogger(__name__)
PREFIX = "locadas-"
SUFFIX = ".sqlite.gz"


def backup_dir() -> Path:
    d = settings.data_dir / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path | None:
    url = settings.sqlalchemy_url
    if not url.startswith("sqlite:///") or ":memory:" in url:
        return None
    return Path(url[len("sqlite:///"):])


def list_backups() -> list[dict]:
    d = settings.data_dir / "backups"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob(f"{PREFIX}*{SUFFIX}"), reverse=True):
        st = p.stat()
        out.append({"file": p.name, "size": st.st_size, "at": datetime.fromtimestamp(st.st_mtime, tz=ZoneInfo("UTC"))})
    return out


def prune(keep: int | None = None) -> list[str]:
    keep = settings.backup_keep if keep is None else keep
    files = sorted(backup_dir().glob(f"{PREFIX}*{SUFFIX}"), reverse=True)
    gone = []
    for p in files[max(keep, 1):]:
        try:
            p.unlink()
            gone.append(p.name)
        except OSError:  # pragma: no cover
            pass
    return gone


def _upload_s3(path: Path) -> str:
    """Optional nach S3. Gibt den Schlüssel zurück oder "" (übersprungen)."""
    bucket = settings.backup_s3_bucket
    if not bucket:
        return ""
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError:
        log.warning("BACKUP_S3_BUCKET gesetzt, aber boto3 ist nicht installiert: Sicherung nur lokal")
        return ""
    key = f"{settings.backup_s3_prefix}{path.name}"
    boto3.client("s3").upload_file(str(path), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})
    return f"s3://{bucket}/{key}"


def run_backup(now: datetime | None = None) -> dict:
    """Eine Sicherung anlegen, alte löschen, optional hochladen. Ergebnis landet in ops_state „last_backup“."""
    now = now or utcnow()
    src_path = db_path()
    info: dict = {"at": now.isoformat(), "ok": False}
    if src_path is None or not src_path.exists():
        info["error"] = "keine SQLite-Datei"
        return info
    day = now.astimezone(ZoneInfo(settings.timezone)).strftime("%Y%m%d")
    target = backup_dir() / f"{PREFIX}{day}{SUFFIX}"
    fd, tmp_name = tempfile.mkstemp(prefix="backup-", suffix=".sqlite", dir=backup_dir())
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        src = sqlite3.connect(str(src_path), timeout=60)
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)  # konsistenter Stand, auch während die App schreibt
            dst.execute("PRAGMA journal_mode=DELETE")
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
            src.close()
        if ok != "ok":
            raise RuntimeError(f"integrity_check: {ok}")
        part = target.with_suffix(".part")
        with open(tmp, "rb") as fin, gzip.open(part, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout)
        os.replace(part, target)
        info.update(ok=True, file=target.name, size=target.stat().st_size)
        info["pruned"] = prune()
        try:
            info["s3"] = _upload_s3(target)
        except Exception as exc:
            info["s3_error"] = str(exc)[:300]
            log.error("Sicherung nach S3 fehlgeschlagen: %s", exc)
        log.info("Sicherung angelegt: %s (%s Bytes)", target.name, info["size"])
    except Exception as exc:
        info["error"] = str(exc)[:500]
        log.error("Sicherung fehlgeschlagen: %s", exc)
    finally:
        tmp.unlink(missing_ok=True)
    try:
        with new_session() as s:
            state.put(s, "last_backup", info)
            s.commit()
    except Exception:  # pragma: no cover
        log.debug("Stand der Sicherung nicht gespeichert", exc_info=True)
    return info


NOTIFY_KEEP_DAYS = 90


def prune_ops_data(now: datetime | None = None) -> dict:
    """Aufräumen vor der Sicherung: Protokolle gelöschter Betriebe weg, Benachrichtigungs-Log nach 90 Tagen weg."""
    from datetime import timedelta

    from sqlalchemy import delete, select

    from ..db import Business, NotificationLog, OpsLog

    now = now or utcnow()
    with new_session() as s:
        alive = select(Business.id)
        a = s.execute(delete(NotificationLog).where(NotificationLog.business_id.is_not(None), NotificationLog.business_id.not_in(alive))).rowcount
        b = s.execute(delete(NotificationLog).where(NotificationLog.created_at < now - timedelta(days=NOTIFY_KEEP_DAYS))).rowcount
        c = s.execute(delete(OpsLog).where(OpsLog.business_id.is_not(None), OpsLog.business_id.not_in(alive))).rowcount
        s.commit()
    return {"notify_orphans": a or 0, "notify_old": b or 0, "log_orphans": c or 0}


def backup_job() -> None:
    try:
        prune_ops_data()
    except Exception as exc:  # pragma: no cover
        log.warning("Aufräumen der Protokolle fehlgeschlagen: %s", exc)
    if settings.backup_enabled:
        info = run_backup()
        if not info.get("ok"):
            from . import alerts

            alerts.job_error("backup", info.get("error", "unbekannt"), "Nächtliche Sicherung")


def restore(archive: Path, target: Path) -> None:
    """Sicherung entpacken (nur bei gestoppter App aufrufen, siehe DEPLOY.md)."""
    with gzip.open(archive, "rb") as fin, open(target, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    for extra in ("-wal", "-shm"):
        Path(str(target) + extra).unlink(missing_ok=True)


if __name__ == "__main__":  # python -m locadas.ops.backup [--restore DATEI]
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "--restore":
        dest = db_path()
        if dest is None:
            sys.exit("Keine SQLite-Datenbank konfiguriert")
        restore(Path(sys.argv[2]), dest)
        print(f"Wiederhergestellt nach {dest}")
    else:
        print(run_backup())
