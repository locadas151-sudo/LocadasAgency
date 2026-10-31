"""Eingebauter SMTP-Empfänger für die Weiterleitungsadressen (aiosmtpd), startet mit der App, wenn INBOUND_SMTP_PORT gesetzt ist.

Kein Relay: Wir nehmen nur Empfänger *@INBOUND_DOMAIN an, die einem Betrieb gehören, alles andere wird bei RCPT TO
mit 550 abgelehnt. Größe höchstens INBOUND_MAX_BYTES (25 MB). Kein AUTH, kein Weiterleiten, nur Verarbeiten.
"""
from __future__ import annotations

import asyncio
import logging

from ..config import settings

log = logging.getLogger(__name__)


def _known(address: str) -> bool:
    from ..db import new_session
    from ..engine.inbound_mail import business_for_address

    with new_session() as s:
        return business_for_address(s, address) is not None


class InboundHandler:
    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        from ..engine.inbound_mail import is_inbound

        addr = (address or "").strip().lower()
        if not is_inbound(addr):
            return "550 5.7.1 Relaying denied"
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, _known, addr):
            return "550 5.1.1 Mailbox unavailable"
        if len(envelope.rcpt_tos) >= 20:
            return "452 4.5.3 Too many recipients"
        envelope.rcpt_tos.append(addr)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        from ..engine.inbound_mail import process_raw

        data = envelope.original_content or envelope.content or b""
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        if len(data) > settings.inbound_max_bytes:
            return "552 5.3.4 Message too big"
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, process_raw, data, list(envelope.rcpt_tos))
        except Exception:  # pragma: no cover
            log.exception("Eingehende Mail nicht verarbeitet")
            return "451 4.3.0 Temporary failure"
        log.info("Eingang per SMTP: %s (Betrieb %s)", result.status, result.business_id)
        if result.status == "unknown":
            return "550 5.1.1 Mailbox unavailable"
        if result.status == "too_large":
            return "552 5.3.4 Message too big"
        if result.status == "rate_limited":
            return "452 4.2.2 Too many messages, try later"
        return "250 OK"


def start(port: int | None = None, host: str | None = None):
    """Controller starten (eigener Thread). Gibt ihn zurück, damit er beim Herunterfahren gestoppt werden kann."""
    from aiosmtpd.controller import Controller

    port = port if port is not None else settings.inbound_smtp_port
    if not port and port != 0:
        return None
    ctrl = Controller(
        InboundHandler(), hostname=host or settings.inbound_smtp_host, port=port,
        server_hostname=settings.inbound_domain or None, data_size_limit=settings.inbound_max_bytes,
        decode_data=False, enable_SMTPUTF8=True, ident="Locadas inbound",
    )
    ctrl.start()
    log.info("SMTP-Empfänger läuft auf %s:%s für @%s", ctrl.hostname, ctrl.port, settings.inbound_domain)
    return ctrl
