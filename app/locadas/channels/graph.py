"""Minimaler Graph-API-Client für Stufe 2 (nur Standardbibliothek). Tests ersetzen `http`."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .config import wab_settings

GRAPH = "https://graph.facebook.com"


class GraphError(Exception):
    """Fehler der Graph API mit Meta-Fehlercode (z. B. 131047) oder 0 bei Netzwerkfehlern."""

    def __init__(self, message: str, code: int = 0, subcode: int = 0, details: str = "", status: int = 0):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.details = details
        self.status = status

    @classmethod
    def from_payload(cls, err: dict, status: int = 0) -> "GraphError":
        data = err.get("error_data") or {}
        details = data.get("details", "") if isinstance(data, dict) else str(data)
        try:
            code = int(err.get("code") or 0)
        except (TypeError, ValueError):
            code = 0
        try:
            sub = int(err.get("error_subcode") or 0)
        except (TypeError, ValueError):
            sub = 0
        return cls(str(err.get("message") or err.get("title") or "Graph-Fehler")[:500], code, sub, str(details)[:500], status)


def url(path: str) -> str:
    return f"{GRAPH}/{wab_settings.graph_version}/{path.lstrip('/')}"


def http(method: str, full_url: str, token: str | None = None, payload: dict | None = None, params: dict | None = None) -> dict:
    """Der einzige Netzwerkaufruf von Stufe 2. Liefert JSON (oder {"_text": ...} bei Klartext)."""
    if params:
        full_url += ("&" if "?" in full_url else "?") + urllib.parse.urlencode(params)
    headers = {}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(full_url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            raise GraphError.from_payload(body["error"], exc.code) from exc
        raise GraphError(f"Graph API {exc.code}: {raw[:300]}", status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GraphError(f"Graph API nicht erreichbar: {exc}") from exc
    try:
        body = json.loads(raw or "{}")
    except ValueError:
        return {"_text": raw.strip()}
    return body if isinstance(body, dict) else {"_data": body}


def call(method: str, path: str, token: str | None = None, payload: dict | None = None, params: dict | None = None) -> dict:
    data = http(method, url(path), token, payload, params)
    if isinstance(data.get("error"), dict):
        raise GraphError.from_payload(data["error"])
    return data
