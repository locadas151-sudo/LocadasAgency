"""Verschlüsselung der Postfach-Zugangsdaten (Fernet, AES-128-CBC + HMAC).

Der Schlüssel kommt aus ENCRYPTION_KEY. Ohne gesetzten Schlüssel wird in der
Entwicklung ein Schlüssel in DATA_DIR/encryption.key erzeugt, damit lokal alles läuft.
In Produktion muss ENCRYPTION_KEY gesetzt sein (siehe DEPLOY.md).
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_fernet: Fernet | None = None


def _load_key() -> bytes:
    if settings.encryption_key:
        raw = settings.encryption_key.encode()
        try:
            Fernet(raw)
            return raw
        except Exception:
            # Beliebiger String als Passphrase: deterministisch auf 32 Byte bringen
            digest = hashlib.sha256(raw).digest()
            return base64.urlsafe_b64encode(digest)
    key_file = settings.data_dir / "encryption.key"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if key_file.exists():
        return key_file.read_bytes().strip()
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key


def fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt(text: str) -> str:
    return fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:  # pragma: no cover
        raise RuntimeError("Zugangsdaten können nicht entschlüsselt werden (ENCRYPTION_KEY geändert?)") from exc


def sign(value: str) -> str:
    """Kurze HMAC-Signatur, z. B. für Tracking-IDs."""
    return hmac.new(settings.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()[:16]
