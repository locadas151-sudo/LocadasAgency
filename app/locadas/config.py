"""Zentrale Konfiguration über Umgebungsvariablen (siehe .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # Basis
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "http://localhost:8000").rstrip("/"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", "dev-secret-change-me"))
    encryption_key: str | None = field(default_factory=lambda: _env("ENCRYPTION_KEY"))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))
    database_url: str | None = field(default_factory=lambda: _env("DATABASE_URL"))
    debug: bool = field(default_factory=lambda: _env_bool("DEBUG", False))

    # Claude
    anthropic_api_key: str | None = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-sonnet-5-5"))

    # Scheduler
    scan_interval_minutes: int = field(default_factory=lambda: _env_int("SCAN_INTERVAL_MINUTES", 10))
    scheduler_enabled: bool = field(default_factory=lambda: _env_bool("SCHEDULER_ENABLED", True))
    # Nachfass-Rhythmus in Tagen nach Versand (Angebote) bzw. nach Fälligkeit (Rechnungen)
    offer_followup_days: tuple[int, ...] = field(
        default_factory=lambda: tuple(int(x) for x in _env("OFFER_FOLLOWUP_DAYS", "3,7,14").split(","))
    )
    invoice_followup_days: tuple[int, ...] = field(
        default_factory=lambda: tuple(int(x) for x in _env("INVOICE_FOLLOWUP_DAYS", "3,10,20").split(","))
    )
    default_invoice_due_days: int = field(default_factory=lambda: _env_int("DEFAULT_INVOICE_DUE_DAYS", 14))
    # Wie viele Nachfass-Mails der Betrieb anfangs freigeben muss, bevor es automatisch läuft
    approvals_before_auto: int = field(default_factory=lambda: _env_int("APPROVALS_BEFORE_AUTO", 5))
    # Höchstens so viele Nachfass-Mails pro Betrieb und Tag (schützt vor Spam-Einstufung beim Mailanbieter)
    max_sends_per_day: int = field(default_factory=lambda: _env_int("MAX_SENDS_PER_DAY", 15))
    # Nur Mails, die jünger sind als X Tage, werden beim ersten Scan berücksichtigt
    initial_scan_days: int = field(default_factory=lambda: _env_int("INITIAL_SCAN_DAYS", 30))
    # Versandfenster (lokale Zeit), damit keine Nachfass-Mail nachts rausgeht
    send_hour_from: int = field(default_factory=lambda: _env_int("SEND_HOUR_FROM", 8))
    send_hour_to: int = field(default_factory=lambda: _env_int("SEND_HOUR_TO", 18))
    timezone: str = field(default_factory=lambda: _env("TZ", "Europe/Berlin"))

    # OAuth (vorbereitet, optional)
    google_client_id: str | None = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str | None = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    microsoft_client_id: str | None = field(default_factory=lambda: _env("MICROSOFT_CLIENT_ID"))
    microsoft_client_secret: str | None = field(default_factory=lambda: _env("MICROSOFT_CLIENT_SECRET"))
    microsoft_tenant: str = field(default_factory=lambda: _env("MICROSOFT_TENANT", "common"))

    # Mailversand von Locadas selbst (nur für System-Mails wie Login-Links, wenn kein Postfach verbunden ist)
    system_smtp_host: str | None = field(default_factory=lambda: _env("SYSTEM_SMTP_HOST"))
    system_smtp_port: int = field(default_factory=lambda: _env_int("SYSTEM_SMTP_PORT", 587))
    system_smtp_user: str | None = field(default_factory=lambda: _env("SYSTEM_SMTP_USER"))
    system_smtp_password: str | None = field(default_factory=lambda: _env("SYSTEM_SMTP_PASSWORD"))
    system_from: str = field(default_factory=lambda: _env("SYSTEM_FROM", "Locadas <info@locadas.de>"))

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'locadas.db').as_posix()}"

    @property
    def pdf_dir(self) -> Path:
        d = self.data_dir / "pdf"
        d.mkdir(parents=True, exist_ok=True)
        return d


settings = Settings()
