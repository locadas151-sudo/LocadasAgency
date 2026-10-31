"""OAuth für Gmail (Google) und Outlook / Microsoft 365: Ein-Klick-Verbindung statt Passwort.

Aktiv, sobald GOOGLE_CLIENT_ID/SECRET bzw. MICROSOFT_CLIENT_ID/SECRET gesetzt sind (siehe DEPLOY.md).
Gespeichert wird nur der Refresh-Token (verschlüsselt). Access-Tokens werden bei Bedarf geholt.

Microsoft läuft seit Version 2 über Microsoft Graph (mail/graph.py, auth_type "graph"): viele Microsoft-365-Tenants
haben IMAP und SMTP AUTH abgeschaltet, Graph geht dort trotzdem. Alte Konten mit auth_type "oauth" (IMAP/SMTP mit
XOAUTH2) laufen weiter und holen ihre Tokens mit den alten IMAP-Scopes (legacy_scopes).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections.abc import Callable
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
    legacy_scopes: str = ""  # Microsoft: Scopes der alten IMAP/SMTP-Konten (auth_type "oauth")

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


MICROSOFT_GRAPH_SCOPES = (
    "offline_access openid email profile https://graph.microsoft.com/Mail.ReadWrite "
    "https://graph.microsoft.com/Mail.Send https://graph.microsoft.com/User.Read"
)
MICROSOFT_IMAP_SCOPES = "openid email offline_access https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send"
MICROSOFT_LOGIN = "https://login.microsoftonline.com"


def _microsoft() -> OAuthProvider:
    t = settings.microsoft_tenant or "common"
    return OAuthProvider(
        "microsoft", "Microsoft / Outlook",
        f"{MICROSOFT_LOGIN}/{t}/oauth2/v2.0/authorize", f"{MICROSOFT_LOGIN}/{t}/oauth2/v2.0/token",
        MICROSOFT_GRAPH_SCOPES,
        "outlook.office365.com", "smtp.office365.com", 587, False, ("Sent Items", "Gesendete Elemente", "Sent"),
        legacy_scopes=MICROSOFT_IMAP_SCOPES,
    )


def providers() -> dict[str, OAuthProvider]:
    return {"google": GOOGLE, "microsoft": _microsoft()}


def get(key: str) -> OAuthProvider | None:
    return providers().get(key)


def authorize_url(p: OAuthProvider, state: str, login_hint: str = "") -> str:
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
    if login_hint:
        params["login_hint"] = login_hint
    return f"{p.auth_url}?{urlencode(params)}"


def admin_consent_url(p: OAuthProvider, tenant: str = "") -> str:
    """Link für den Admin des Betriebs: erlaubt die App einmal für den ganzen Microsoft-365-Tenant.

    Danach landet Microsoft wieder auf unserem Callback (admin_consent=True), deshalb reicht eine Redirect-URI."""
    tenant = re.sub(r"[^A-Za-z0-9.-]", "", tenant or "") or "organizations"
    return f"{MICROSOFT_LOGIN}/{tenant}/adminconsent?{urlencode({'client_id': p.client_id or '', 'redirect_uri': p.redirect_uri})}"


# AADSTS65001: niemand hat zugestimmt (Nutzer dürfen nicht selbst), 90094/90095: Admin-Freigabe nötig bzw. beantragt
_ADMIN_CONSENT_RE = re.compile(r"AADSTS(65001|90094|90095|900941)\b")


def needs_admin_consent(*texts: str) -> bool:
    return any(_ADMIN_CONSENT_RE.search(t or "") for t in texts)


class OAuthError(Exception):
    """Token abgelehnt (z. B. invalid_grant: Zugriff widerrufen, Passwort geändert). Postfach neu verbinden."""


class OAuthUnavailable(OAuthError):
    """Token-Server nicht erreichbar oder überlastet. Kein Grund, das Postfach als gestört zu melden."""


def _post(url: str, data: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, data=urlencode(data).encode(), method="POST", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        raise OAuthUnavailable(f"Token-Server nicht erreichbar: {exc}") from exc


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


def forget(cache_key: str) -> None:
    _cache.pop(cache_key, None)


def remember(cache_key: str, tok: dict) -> None:
    if cache_key and tok.get("access_token"):
        _cache[cache_key] = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)))


def access_token(
    p: OAuthProvider, refresh_token: str, cache_key: str = "", scopes: str | None = None,
    on_new_refresh_token: Callable[[str], None] | None = None,
) -> str:
    """Access-Token aus Refresh-Token, mit kleinem Cache (Tokens gelten ~1 h).

    Microsoft gibt bei jedem Refresh einen neuen Refresh-Token aus. Der wird über on_new_refresh_token gespeichert,
    sonst läuft die Verbindung nach 90 Tagen ab. Wirft OAuthError (abgelehnt) bzw. OAuthUnavailable (Netz, 5xx)."""
    now = time.time()
    if cache_key and cache_key in _cache:
        tok, exp = _cache[cache_key]
        if exp - 120 > now:
            return tok
    data = {"client_id": p.client_id, "client_secret": p.client_secret, "refresh_token": refresh_token, "grant_type": "refresh_token"}
    if p.key == "microsoft":
        data["scope"] = scopes or p.scopes
    status, body = _post(p.token_url, data)
    if status == 429 or status >= 500:
        raise OAuthUnavailable(f"Token-Server antwortet gerade nicht ({status}).")
    if status != 200:
        code = ""
        try:
            code = json.loads(body).get("error", "")
        except Exception:
            pass
        raise OAuthError(f"Zugriff abgelaufen oder widerrufen ({status}{', ' + code if code else ''}). Bitte Postfach neu verbinden.")
    try:
        tok = json.loads(body)
        access = tok["access_token"]
    except (ValueError, KeyError) as exc:
        raise OAuthUnavailable("Token-Server hat keine gültige Antwort geschickt.") from exc
    if cache_key:
        _cache[cache_key] = (access, now + int(tok.get("expires_in", 3600)))
    new_rt = tok.get("refresh_token")
    if new_rt and new_rt != refresh_token and on_new_refresh_token is not None:
        try:
            on_new_refresh_token(new_rt)
        except Exception as exc:  # nie den Abruf daran scheitern lassen, der alte Token gilt weiter
            log.warning("Neuer Refresh-Token nicht gespeichert: %s", exc)
    return access
