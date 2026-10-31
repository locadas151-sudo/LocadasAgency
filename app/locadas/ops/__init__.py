"""Betrieb von Locadas selbst: Admin-Kennzahlen, Alarme an den Admin, Fehler-Feed, Sicherungen, Health-Check.

Nichts hier ist für die Betriebe sichtbar. Einstieg beim App-Start: on_startup().
"""
from __future__ import annotations


def on_startup(scheduler=None, inbound=None) -> None:
    """Aus main._startup: Log-Handler für den Fehler-Feed einhängen, Health-Check kennt Scheduler und SMTP-Empfänger."""
    from . import health, logstore

    logstore.install()
    health.state.scheduler = scheduler
    health.state.inbound = inbound
