"""OAuth für Gmail (Google) und Outlook / Microsoft 365: Ein-Klick-Verbindung statt Passwort.

Aktiv, sobald GOOGLE_CLIENT_ID/SECRET bzw. MICROSOFT_CLIENT_ID/SECRET gesetzt sind (siehe DEPLOY.md).
Gespeichert wird nur der Refresh-Token (verschlüsselt). Access-Tokens werden bei Bedarf geholt.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import urllib.error
import urllib.request

from ..config import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OAuthProvider:
    key: str
    label: str
    auth_url: str
    token_url: str
    scopes: str
    imap_host: str
    smtp_host: str
    smtp_port: int
    smtp_ssl: bool
    sent_folders: tuple[str, ...]

    @property
    def client_id(self) -> str | None:
        return settings.google_client_id if self.key == "google" else settings.microsoft_client_id

    @property
    def client_secret(self) -> str | None:
        return settings.google_client_secret if self.key == "google" else settings.microsoft_client_secret

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def redirect_uri(self) -> str:
        return f"{settings.base_url}/oauth/{self.key}/callback"


GOOGLE = OAuthProvider(
    "google", "Google / Gmail",
    "https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token",
    "openid email https://mail.google.com/",
    "imap.gmail.com", "smtp.gmail.com", 465, True, ("[Gmail]/Gesendet", "[Gmail]/Sent Mail", "[Gmail]/Sent"),
)


def _microsoft() -> OAuthProvider:
    t = settings.microsoft_tenant or "common"
    return OAuthProvider(
        "microsoft", "Microsoft / Outlook",
        f"https://login.microsoftonline.com/{t}/oauth2/v2.0/authorize", f"https://login.microsoftonline.com/{t}/oauth2/v2.0/token",
        "openid email offline_access https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send",
        "outlook.office365.com", "smtp.office365.com", 587, False, ("Sent Items", "Gesendete Elemente", "Sent"),
    )


def providers() -> dict[str, OAuthProvider]:
    return {"google": GOOGLE, "microsoft": _microsoft()}


def get(key: str) -> OAuthProvider | None:
    return providers().get(key)


def authorize_url(p: OAuthProvider, state: str) -> str:
    params = {
        "client_id": p.client_id,
        "redirect_uri": p.redirect_uri,
        "response_type": "code",
        "scope": p.scopes,
        "state": state,
    }
    if p.key == "google":
        params.update({"access_type": "offline", "prompt": "consent"})
    else:
        params.update({"response_mode": "query", "prompt": "select_account"})
    return f"{p.auth_url}?{urlencode(params)}"


class OAuthError(Exception):
    pass


def _post(url: str, data: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, data=urlencode(data).encode(), method="POST", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        raise OAuthError(f"Token-Server nicht erreichbar: {exc}") from exc


def exchange_code(p: OAuthProvider, code: str) -> dict:
    data = {
        "client_id": p.client_id, "client_secret": p.client_secret, "code": code,
        "grant_type": "authorization_code", "redirect_uri": p.redirect_uri,
    }
    status, body = _post(p.token_url, data)
    if status != 200:
        raise OAuthError(f"Token-Austausch abgelehnt: {body[:300]}")
    tok = json.loads(body)
    if "refresh_token" not in tok:
        raise OAuthError("Kein Refresh-Token erhalten. Bitte die Verbindung in deinem Konto entfernen und erneut verbinden.")
    return tok


def email_from_id_token(id_token: str | None) -> str:
    if not id_token:
        return ""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return ""
    return (data.get("email") or data.get("preferred_username") or data.get("upn") or "").lower()


_cache: dict[str, tuple[str, float]] = {}


def access_token(p: OAuthProvider, refresh_token: str, cache_key: str = "") -> str:
    """Access-Token aus Refresh-Token, mit kleinem Cache (Tokens gelten ~1 h)."""
    now = time.time()
    if cache_key and cache_key in _cache:
        tok, exp = _cache[cache_key]
        if exp - 120 > now:
            return tok
    data = {"client_id": p.client_id, "client_secret": p.client_secret, "refresh_token": refresh_token, "grant_type": "refresh_token"}
    if p.key == "microsoft":
        data["scope"] = p.scopes
    status, body = _post(p.token_url, data)
    if status != 200:
        raise OAuthError(f"Zugriff abgelaufen oder widerrufen ({status}). Bitte Postfach neu verbinden.")
    tok = json.loads(body)
    if cache_key:
        _cache[cache_key] = (tok["access_token"], now + int(tok.get("expires_in", 3600)))
    return tok["access_token"]
