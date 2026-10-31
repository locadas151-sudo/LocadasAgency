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


WHATSAPP_TEMPLATE_DEFAULTS = {
    "welcome": "locadas_welcome",
    "preview": "locadas_preview",
    "opened": "locadas_opened",
    "replied": "locadas_reply",
    "approval": "locadas_approval",
    "invoice_open": "locadas_invoice_open",
    "outcome_offer": "locadas_outcome_offer",
    "outcome_invoice": "locadas_outcome_invoice",
    "event": "locadas_event",
}


def _templates(raw: str) -> dict[str, str]:
    out = dict(WHATSAPP_TEMPLATE_DEFAULTS)
    for part in raw.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


@dataclass
class Settings:
    # Basis
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "http://localhost:8000").rstrip("/"))
    # Domain für den Dokument-Link in Kunden-Mails (später z. B. eine neutrale Domain), Standard wie BASE_URL
    view_base_url: str = field(default_factory=lambda: (_env("VIEW_BASE_URL") or _env("BASE_URL", "http://localhost:8000")).rstrip("/"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", DEFAULT_SECRET_KEY))
    encryption_key: str | None = field(default_factory=lambda: _env("ENCRYPTION_KEY"))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))
    database_url: str | None = field(default_factory=lambda: _env("DATABASE_URL"))
    debug: bool = field(default_factory=lambda: _env_bool("DEBUG", False))

    # Claude
    anthropic_api_key: str | None = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-sonnet-5-5"))
    # Günstiges Modell für Erkennung (PDF-Felder) und Zuordnung von Kundenmails; Texte schreibt CLAUDE_MODEL
    claude_fast_model: str = field(default_factory=lambda: _env("CLAUDE_FAST_MODEL", "claude-haiku-5-5"))
    # Nur für Entwicklung, Tests und Demo: Standard-Schlüssel und lokale Mailserver ohne TLS erlauben
    allow_insecure_dev: bool = field(default_factory=lambda: _env_bool("ALLOW_INSECURE_DEV", False))

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
    # Angebote über diesem Bruttobetrag gehen nur mit Freigabe raus (Standard je Betrieb, in den Einstellungen änderbar)
    approval_threshold: float = field(default_factory=lambda: float(_env("APPROVAL_THRESHOLD", "10000")))
    # Morgen-Vorschau: so viele Stunden vor dem Versand bekommt der Betrieb die Liste mit Stopp-Links (0 = aus)
    preview_hours: int = field(default_factory=lambda: _env_int("PREVIEW_HOURS", 2))
    # Nach der letzten Stufe so viele Tage warten, dann „Auftrag bekommen? / Bezahlt?“ fragen
    outcome_after_days: int = field(default_factory=lambda: _env_int("OUTCOME_AFTER_DAYS", 7))
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

    # WhatsApp-Sekretär (Meta WhatsApp Cloud API). Ohne Token/Nummer/App-Secret geht alles per Mail.
    whatsapp_token: str | None = field(default_factory=lambda: _env("WHATSAPP_TOKEN"))
    whatsapp_phone_number_id: str | None = field(default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID"))
    whatsapp_app_secret: str | None = field(default_factory=lambda: _env("WHATSAPP_APP_SECRET"))
    whatsapp_verify_token: str | None = field(default_factory=lambda: _env("WHATSAPP_VERIFY_TOKEN"))
    whatsapp_api_version: str = field(default_factory=lambda: _env("WHATSAPP_API_VERSION", "v21.0"))
    whatsapp_template_lang: str = field(default_factory=lambda: _env("WHATSAPP_TEMPLATE_LANG", "de"))
    # Namen der freigegebenen Vorlagen, überschreibbar per WHATSAPP_TEMPLATES="preview=meine_vorschau,event=..."
    whatsapp_templates: dict[str, str] = field(default_factory=lambda: _templates(_env("WHATSAPP_TEMPLATES", "")))

    @property
    def whatsapp_enabled(self) -> bool:
        return bool(self.whatsapp_token and self.whatsapp_phone_number_id and self.whatsapp_app_secret)

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


DEFAULT_SECRET_KEY = "dev-secret-change-me"


def insecure_config_problems(s: "Settings") -> list[str]:
    """Was in Produktion fehlt. Leer = alles gut."""
    problems = []
    if not s.secret_key or s.secret_key == DEFAULT_SECRET_KEY:
        problems.append("SECRET_KEY ist nicht gesetzt (Standardwert)")
    if not s.encryption_key:
        problems.append("ENCRYPTION_KEY ist nicht gesetzt")
    return problems


def check_startup(s: "Settings | None" = None) -> None:
    """Beim Start: ohne eigene Schlüssel nicht hochfahren, außer DEBUG=1 oder ALLOW_INSECURE_DEV=1."""
    import logging

    s = s or settings
    problems = insecure_config_problems(s)
    if not problems:
        return
    if s.debug or s.allow_insecure_dev:
        logging.getLogger(__name__).warning("Unsichere Konfiguration (nur für Entwicklung): %s", "; ".join(problems))
        return
    msg = "Start abgebrochen: " + "; ".join(problems) + ". Setz die Werte in der .env (siehe DEPLOY.md) oder für lokale Tests ALLOW_INSECURE_DEV=1."
    logging.getLogger(__name__).critical(msg)
    raise RuntimeError(msg)


settings = Settings()
