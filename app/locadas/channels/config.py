"""Konfiguration für WhatsApp über die eigene Nummer des Betriebs (Stufe 2, Coexistence).

Bewusst getrennt von locadas/config.py, damit Stufe 1 und Stufe 2 sich beim Zusammenführen nicht ins Gehege kommen.
Ohne WA_BUSINESS_ENABLED=1 und ohne Meta-Zugangsdaten ist alles aus: keine Karte in den Einstellungen,
select_and_send() gibt immer None zurück, der Webhook behandelt fremde Nummern wie bisher.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _flag(name: str, default: bool) -> bool:
    v = _env(name)
    return default if v is None else v.lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    raw = (_env(name) or "").replace(",", ".")
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass
class WaBusinessSettings:
    # Hauptschalter (erst nach Freigabe als Tech Provider einschalten)
    enabled_flag: bool = field(default_factory=lambda: _flag("WA_BUSINESS_ENABLED", False))
    # Meta-App (Facebook Login for Business + Embedded Signup)
    app_id: str | None = field(default_factory=lambda: _env("META_APP_ID"))
    # App-Geheimcode: für den Code-Tausch und die Webhook-Signatur. Standard: derselbe wie beim Sekretär
    app_secret: str | None = field(default_factory=lambda: _env("META_APP_SECRET") or _env("WHATSAPP_APP_SECRET"))
    config_id: str | None = field(default_factory=lambda: _env("META_CONFIG_ID"))
    # Graph-API-Version für Stufe 2 (Meta-Beispiele nutzen aktuell v26.0; alte Versionen laufen nach rund 2 Jahren aus)
    graph_version: str = field(default_factory=lambda: _env("META_GRAPH_VERSION", "v26.0"))
    # Embedded Signup: Coexistence-Flow (WhatsApp Business App bleibt auf dem Handy)
    feature_type: str = field(default_factory=lambda: _env("META_ES_FEATURE_TYPE", "whatsapp_business_app_onboarding"))
    session_info_version: str = field(default_factory=lambda: _env("META_ES_SESSION_INFO_VERSION", "3"))
    template_lang: str = field(default_factory=lambda: _env("WA_BUSINESS_TEMPLATE_LANG", "de"))
    # Anzeige im Dashboard (Meta rechnet selbst ab, das hier ist nur eine Schätzung)
    price_utility_eur: float = field(default_factory=lambda: _float("WHATSAPP_BUSINESS_PRICE_EUR", 0.0456))
    price_marketing_eur: float = field(default_factory=lambda: _float("WHATSAPP_BUSINESS_PRICE_MARKETING_EUR", 0.1131))
    service_free_per_month: int = field(default_factory=lambda: int(_float("WHATSAPP_BUSINESS_SERVICE_FREE", 1000)))
    # Paket-Sperre: Stufe 2 nur für Betriebe mit plan == "pro"
    requires_pro: bool = field(default_factory=lambda: _flag("WA_BUSINESS_REQUIRES_PRO", True))

    @property
    def enabled(self) -> bool:
        return bool(self.enabled_flag and self.app_id and self.app_secret and self.config_id)


wab_settings = WaBusinessSettings()
