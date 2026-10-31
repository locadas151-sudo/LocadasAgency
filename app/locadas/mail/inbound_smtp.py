"""Eingebauter SMTP-Empfänger für die Weiterleitungsadressen (aiosmtpd), startet mit der App, wenn INBOUND_SMTP_PORT gesetzt ist.

Kein Relay: Wir nehmen nur Empfänger *@INBOUND_DOMAIN an, die einem Betrieb gehören, alles andere wird bei RCPT TO
mit 550 abgelehnt. Größe höchstens INBOUND_MAX_BYTES (25 MB). Kein AUTH, kein Weiterleiten, nur Verarbeiten.

STARTTLS wird angeboten (opportunistisch, nicht Pflicht): Zertifikat aus INBOUND_TLS_CERT/INBOUND_TLS_KEY, sonst
einmal selbst signiert für INBOUND_DOMAIN nach DATA_DIR/tls erzeugt. Sendende Mailserver akzeptieren selbst signierte
Zertifikate bei opportunistischem TLS (sie prüfen es ohne MTA-STS/DANE nicht), die Verbindung ist trotzdem verschlüsselt.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


# --- STARTTLS ----------------------------------------------------------------------------------

def _self_signed_paths() -> tuple[Path, Path]:
    d = settings.data_dir / "tls"
    return d / "inbound.crt", d / "inbound.key"


def _generate_cryptography(cert: Path, key: Path, host: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.now(timezone.utc)
    crt = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(pk.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .sign(pk, hashes.SHA256())
    )
    key.write_bytes(pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    cert.write_bytes(crt.public_bytes(serialization.Encoding.PEM))


def _generate_openssl(cert: Path, key: Path, host: str) -> None:
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-subj", f"/CN={host}",
         "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True, timeout=60,
    )


def ensure_cert() -> tuple[Path, Path] | None:
    """Pfade zu Zertifikat und Schlüssel. Eigene aus der Konfiguration, sonst einmal selbst signiert erzeugen."""
    if settings.inbound_tls_cert and settings.inbound_tls_key:
        cert, key = Path(settings.inbound_tls_cert), Path(settings.inbound_tls_key)
        if cert.exists() and key.exists():
            return cert, key
        log.warning("INBOUND_TLS_CERT/INBOUND_TLS_KEY nicht lesbar, nehme selbst signiertes Zertifikat")
    cert, key = _self_signed_paths()
    if cert.exists() and key.exists():
        return cert, key
    cert.parent.mkdir(parents=True, exist_ok=True)
    host = settings.inbound_domain or "localhost"
    try:
        try:
            _generate_cryptography(cert, key, host)
        except ImportError:  # pragma: no cover
            _generate_openssl(cert, key, host)
        key.chmod(0o600)
        log.info("Selbst signiertes Zertifikat für STARTTLS erzeugt (%s)", host)
        return cert, key
    except Exception as exc:
        log.warning("Kein Zertifikat für STARTTLS, Empfänger läuft ohne TLS: %s", exc)
        return None


def tls_context() -> ssl.SSLContext | None:
    if not settings.inbound_tls:
        return None
    paths = ensure_cert()
    if paths is None:
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(str(paths[0]), str(paths[1]))
    except (OSError, ssl.SSLError) as exc:
        log.warning("Zertifikat für STARTTLS nicht ladbar, Empfänger läuft ohne TLS: %s", exc)
        return None
    return ctx


def start(port: int | None = None, host: str | None = None):
    """Controller starten (eigener Thread). Gibt ihn zurück, damit er beim Herunterfahren gestoppt werden kann."""
    from aiosmtpd.controller import Controller

    port = port if port is not None else settings.inbound_smtp_port
    if not port and port != 0:
        return None
    ctx = tls_context()
    ctrl = Controller(
        InboundHandler(), hostname=host or settings.inbound_smtp_host, port=port,
        server_hostname=settings.inbound_domain or None, data_size_limit=settings.inbound_max_bytes,
        decode_data=False, enable_SMTPUTF8=True, ident="Locadas inbound",
        tls_context=ctx, require_starttls=False,  # opportunistisch: wer kein TLS kann, liefert trotzdem
    )
    ctrl.start()
    log.info("SMTP-Empfänger läuft auf %s:%s für @%s (STARTTLS %s)", ctrl.hostname, ctrl.port, settings.inbound_domain, "an" if ctx else "aus")
    return ctrl
