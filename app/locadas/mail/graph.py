"""Microsoft Graph als Mail-Zugang für alle Microsoft-Konten (Outlook.com, Hotmail, Live und Microsoft 365).

Viele Microsoft-365-Tenants haben IMAP und vor allem SMTP AUTH abgeschaltet. Graph geht dort trotzdem, mit den
delegierten Rechten Mail.ReadWrite, Mail.Send und User.Read (siehe oauth.MICROSOFT_GRAPH_SCOPES).

GraphClient hat dieselbe Oberfläche, die Scanner und Versand von ImapClient/SmtpClient nutzen (Kontextmanager,
fetch_message/fetch_headers/fetch_text über eine Nummer je Mail, send, test). Nur das Auflisten neuer Mails ist
anders: statt IMAP-UIDs gibt es einen Zeitstempel als Wasserstand (scanner._scan_sent_graph/_scan_inbox_graph).

- Rohmail: GET /me/messages/{id}/$value, danach derselbe Parser wie bei IMAP (PDF-Erkennung unverändert).
- Versand: dieselbe MIME-Mail wie per SMTP, als Base64 an POST /me/sendMail. In-Reply-To/References bleiben,
  Microsoft legt die Mail selbst in „Gesendete Elemente“ ab. Über 4 MB: Entwurf aus MIME, Anhänge per
  Upload-Session, dann senden.
- Fehler: 401 → Token einmal erneuern, dann MailAuthError. 429/503/504 → Retry-After abwarten (kurz), sonst
  MailConnectError. Netz und 5xx → MailConnectError. So greift der Verbindungs-Wächter (engine/accounts.py).

Nur Standardbibliothek (urllib), TLS-Prüfung immer an. Tests biegen GRAPH_BASE auf einen lokalen Fake-Server um.
"""
from __future__ import annotations

import base64
import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from email.message import EmailMessage

from .imap import MailAuthError, MailConnectError, ParsedMail, extract_text, is_local_host, parse_message, tls_context

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TIMEOUT = 40
MAX_RETRIES = 3          # 429/503/504 je Anfrage
MAX_WAIT = 30            # Sekunden, länger blockieren wir den Durchlauf nicht
PAGE_SIZE = 50
MIME_LIMIT = 3_500_000   # Base64-Größe für /sendMail (Graph-Grenze 4 MB je Anfrage)
SMALL_ATTACHMENT = 2_500_000
UPLOAD_CHUNK = 320 * 1024 * 10  # Vielfaches von 320 KiB, wie von Graph verlangt
OWN_HEADER = "X-Locadas-Nachfasser"  # markiert unsere eigenen Mails, falls Exchange die Message-ID neu setzt

SENT = "sentitems"
INBOX = "inbox"

SENT_SELECT = "id,internetMessageId,subject,toRecipients,ccRecipients,sentDateTime,hasAttachments,conversationId"
INBOX_SELECT = "id,internetMessageId,subject,from,receivedDateTime,hasAttachments,conversationId,internetMessageHeaders"
# PR_MESSAGE_SIZE: damit Riesenmails übersprungen werden, ohne sie zu laden
SIZE_PROP = "Integer 0x0E08"


def _sleep(seconds: float) -> None:  # Tests ersetzen das
    time.sleep(seconds)


def http_request(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, dict[str, str], bytes]:
    """Eine HTTP-Anfrage. Gibt (Status, Header, Rumpf) zurück, auch bei 4xx/5xx. Netzwerkfehler als OSError."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    ctx = tls_context() if url.startswith("https://") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310 (nur https bzw. lokal in Tests)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() or b""


def graph_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_graph_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class GraphError(MailConnectError):
    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class GraphNotFound(GraphError):
    pass


def _error_code(data: bytes) -> tuple[str, str]:
    try:
        err = json.loads(data or b"{}").get("error") or {}
        return str(err.get("code", "")), str(err.get("message", ""))[:300]
    except (ValueError, AttributeError):
        return "", ""


class GraphClient:
    """Mail über Microsoft Graph. `token(force)` liefert ein Access-Token, force=True heißt: neu holen."""

    is_graph = True

    def __init__(self, token: Callable[[bool], str], user: str = "", base: str | None = None):
        self._token_fn = token
        self._token: str | None = None
        self.user = user
        self.base = (base or GRAPH_BASE).rstrip("/")
        parsed = urllib.parse.urlparse(self.base)
        if parsed.scheme != "https" and not is_local_host(parsed.hostname or ""):
            raise MailConnectError("Microsoft Graph nur über HTTPS")
        self._host = parsed.netloc
        self._meta: dict[int, dict] = {}
        self._next_handle = 1

    # -- wie ImapClient/SmtpClient ---------------------------------------------------
    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        pass

    def close(self) -> None:
        self._meta.clear()

    def find_sent_folder(self, candidates: tuple[str, ...] = ()) -> str:
        return SENT

    def select(self, folder: str, readonly: bool = True) -> tuple[int | None, int]:
        return None, 0

    def append(self, folder: str, raw: bytes) -> bool:
        return True  # Graph legt verschickte Mails selbst in „Gesendete Elemente“ ab

    # -- HTTP ----------------------------------------------------------------------------
    def _access_token(self, force: bool = False) -> str:
        if force or not self._token:
            self._token = self._token_fn(force)
        return self._token

    def _url(self, path_or_url: str, params: dict[str, str] | None = None) -> str:
        if path_or_url.startswith(("https://", "http://")):
            if urllib.parse.urlparse(path_or_url).netloc != self._host:
                raise GraphError("Unerwartete Weiterleitung von Microsoft Graph")
            url = path_or_url
        else:
            url = self.base + path_or_url
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="$,()'=")
        return url

    def request(
        self, method: str, path_or_url: str, params: dict[str, str] | None = None, body: bytes | None = None,
        content_type: str = "", headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        url = self._url(path_or_url, params)
        refreshed = False
        attempts = 0
        while True:
            hdrs = {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json", **(headers or {})}
            if content_type:
                hdrs["Content-Type"] = content_type
            try:
                status, rh, data = http_request(method, url, hdrs, body, TIMEOUT)
            except (urllib.error.URLError, OSError, socket.timeout) as exc:
                raise MailConnectError(f"Microsoft Graph nicht erreichbar: {exc}") from exc
            if status < 400:
                return status, rh, data
            code, message = _error_code(data)
            if status == 401:
                if not refreshed:
                    refreshed = True
                    self._access_token(force=True)
                    continue
                raise MailAuthError(f"Microsoft hat den Zugriff abgelehnt (401{' ' + code if code else ''}). Bitte Postfach neu verbinden.")
            # Senden nur bei 429 wiederholen: nach 503/504 könnte die Mail schon raus sein (kein Doppelversand)
            retryable = (429, 503, 504) if method == "GET" else (429,)
            if status in retryable and attempts < MAX_RETRIES:
                attempts += 1
                wait = _retry_after(rh.get("retry-after"), attempts)
                if wait is None:
                    raise GraphError(f"Microsoft bremst gerade ({status}), später noch einmal.", status, code)
                log.info("Graph %s: %s, warte %ss", status, code, wait)
                _sleep(wait)
                continue
            if status == 403 or code in ("MailboxNotEnabledForRESTAPI", "MailboxNotSupportedForRESTAPI", "ErrorAccessDenied", "InvalidAuthenticationToken"):
                raise MailAuthError(_auth_message(status, code, message))
            if status == 404:
                raise GraphNotFound(f"Nicht gefunden ({code})", status, code)
            if status >= 500 or status == 429:
                raise GraphError(f"Microsoft Graph antwortet mit Fehler {status} {code}".strip(), status, code)
            raise GraphError(f"Microsoft Graph lehnt die Anfrage ab ({status} {code}: {message})", status, code)

    def get_json(self, path_or_url: str, params: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> dict:
        _, _, data = self.request("GET", path_or_url, params, headers=headers)
        try:
            return json.loads(data or b"{}")
        except ValueError as exc:
            raise GraphError("Microsoft Graph hat keine gültige Antwort geschickt") from exc

    # -- Konto ---------------------------------------------------------------------------
    def me(self) -> dict:
        return self.get_json("/me", {"$select": "mail,userPrincipalName,displayName"})

    def email(self) -> str:
        me = self.me()
        return (me.get("mail") or me.get("userPrincipalName") or "").strip().lower()

    def test(self) -> None:
        """Wie SmtpClient.test: Zugriff aufs Postfach klappt (Gesendet-Ordner lesbar)."""
        self.get_json(f"/me/mailFolders/{SENT}", {"$select": "id"})

    # -- Auflisten -----------------------------------------------------------------------
    def list_messages(self, folder: str, time_field: str, since: datetime, select: str, limit: int, with_size: bool = False) -> list[dict]:
        params = {
            "$filter": f"{time_field} ge {graph_time(since)}",
            "$select": select,
            "$orderby": f"{time_field} asc",
            "$top": str(PAGE_SIZE),
        }
        if with_size:
            params["$expand"] = f"singleValueExtendedProperties($filter=id eq '{SIZE_PROP}')"
        out: list[dict] = []
        url: str | None = f"/me/mailFolders/{folder}/messages"
        while url and len(out) < limit:
            data = self.get_json(url, params)
            out.extend(v for v in data.get("value", []) if isinstance(v, dict) and v.get("id"))
            url = data.get("@odata.nextLink")
            params = None  # nextLink enthält alles
        return out[:limit]

    def remember(self, meta: dict) -> int:
        """Nummer für eine gelistete Mail (gilt nur in dieser Sitzung), damit fetch_* wie bei IMAP arbeiten."""
        handle = self._next_handle
        self._next_handle += 1
        self._meta[handle] = meta
        return handle

    def _id(self, uid: int) -> str:
        meta = self._meta.get(uid)
        if not meta:
            raise GraphError(f"Mail {uid} unbekannt")
        return urllib.parse.quote(meta["id"], safe="=-_")

    # -- Lesen ---------------------------------------------------------------------------
    def fetch_raw(self, uid: int) -> bytes | None:
        try:
            _, _, data = self.request("GET", f"/me/messages/{self._id(uid)}/$value", headers={"Accept": "*/*"})
        except GraphNotFound:
            return None
        return data

    def fetch_message(self, uid: int) -> ParsedMail | None:
        raw = self.fetch_raw(uid)
        return parse_message(uid, raw) if raw else None

    def fetch_headers(self, uid: int) -> ParsedMail | None:
        meta = self._meta.get(uid) or {}
        headers = meta.get("internetMessageHeaders") or []
        if not headers:
            # Interne Mails (gleicher Tenant) haben oft keine Internet-Header: dann die ganze Mail
            return self.fetch_message(uid)
        lines = []
        for h in headers:
            name, value = str(h.get("name", "")).strip(), str(h.get("value", ""))
            if name and ":" not in name:
                lines.append(f"{name}: {value}".replace("\r", " ").replace("\n", " "))
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8", "replace")
        return parse_message(uid, raw)

    def fetch_text(self, uid: int, limit: int = 4000, max_bytes: int = 256_000) -> str:
        """Nur der Text (ohne Anhänge zu laden)."""
        try:
            data = self.get_json(f"/me/messages/{self._id(uid)}", {"$select": "body"}, headers={"Prefer": 'outlook.body-content-type="text"'})
        except GraphNotFound:
            return ""
        body = data.get("body") or {}
        text = str(body.get("content") or "")
        if (body.get("contentType") or "").lower() == "html":
            text = extract_text(b"Content-Type: text/html; charset=utf-8\r\n\r\n" + text.encode("utf-8", "replace"), limit)
        return text.strip()[:limit]

    def fetch_message_size(self, uid: int) -> int:
        meta = self._meta.get(uid) or {}
        for prop in meta.get("singleValueExtendedProperties") or []:
            kind, _, tag = str(prop.get("id", "")).partition(" ")
            try:
                if kind.lower() == "integer" and int(tag, 16) == 0x0E08:  # Graph liefert „Integer 0xe08“
                    return int(prop.get("value") or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    # -- Senden --------------------------------------------------------------------------
    def send(self, msg: EmailMessage) -> str:
        if OWN_HEADER not in msg:
            msg[OWN_HEADER] = "1"
        raw = _crlf(msg)
        b64 = base64.b64encode(raw)
        if len(b64) <= MIME_LIMIT:
            self.request("POST", "/me/sendMail", body=b64, content_type="text/plain")
        else:
            self._send_large(msg)
        return msg["Message-ID"]

    def _send_large(self, msg: EmailMessage) -> None:
        """Über 4 MB: Entwurf aus der MIME-Mail ohne Anhänge, Anhänge einzeln hochladen, dann senden."""
        attachments = [part for part in msg.iter_attachments()]
        bare = _without_attachments(msg)
        _, _, data = self.request("POST", "/me/messages", body=base64.b64encode(_crlf(bare)), content_type="text/plain")
        try:
            draft_id = urllib.parse.quote(json.loads(data)["id"], safe="=-_")
        except (ValueError, KeyError) as exc:
            raise GraphError("Entwurf konnte nicht angelegt werden") from exc
        for part in attachments:
            payload = part.get_payload(decode=True) or b""
            name = part.get_filename() or "anhang.pdf"
            ctype = part.get_content_type()
            if len(payload) <= SMALL_ATTACHMENT:
                body = json.dumps({
                    "@odata.type": "#microsoft.graph.fileAttachment", "name": name, "contentType": ctype,
                    "contentBytes": base64.b64encode(payload).decode(),
                }).encode()
                self.request("POST", f"/me/messages/{draft_id}/attachments", body=body, content_type="application/json")
            else:
                self._upload(draft_id, name, ctype, payload)
        self.request("POST", f"/me/messages/{draft_id}/send", body=b"", content_type="application/json")

    def _upload(self, draft_id: str, name: str, ctype: str, payload: bytes) -> None:
        body = json.dumps({"AttachmentItem": {"attachmentType": "file", "name": name, "size": len(payload), "contentType": ctype}}).encode()
        _, _, data = self.request("POST", f"/me/messages/{draft_id}/attachments/createUploadSession", body=body, content_type="application/json")
        try:
            upload_url = json.loads(data)["uploadUrl"]
        except (ValueError, KeyError) as exc:
            raise GraphError("Upload für den Anhang nicht möglich") from exc
        host = urllib.parse.urlparse(upload_url)
        if host.scheme != "https" and not is_local_host(host.hostname or ""):
            raise GraphError("Upload-Adresse ohne HTTPS")
        total = len(payload)
        for start in range(0, total, UPLOAD_CHUNK):
            chunk = payload[start:start + UPLOAD_CHUNK]
            end = start + len(chunk) - 1
            # Upload-URL ist vorab signiert: kein Authorization-Header mitschicken
            try:
                status, _, resp = http_request("PUT", upload_url, {
                    "Content-Type": "application/octet-stream", "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                }, chunk, TIMEOUT)
            except (urllib.error.URLError, OSError, socket.timeout) as exc:
                raise MailConnectError(f"Upload des Anhangs abgebrochen: {exc}") from exc
            if status >= 400:
                code, _ = _error_code(resp)
                raise GraphError(f"Upload des Anhangs abgelehnt ({status} {code})", status, code)


def _crlf(msg: EmailMessage) -> bytes:
    """MIME mit CRLF-Zeilenenden, wie Exchange es erwartet."""
    policy = getattr(msg, "policy", None)
    if policy is None or not hasattr(policy, "clone"):
        return msg.as_bytes()
    return msg.as_bytes(policy=policy.clone(linesep="\r\n"))


def _without_attachments(msg: EmailMessage) -> EmailMessage:
    bare = EmailMessage()
    for key, value in msg.items():
        if key.lower() in ("content-type", "content-transfer-encoding", "mime-version"):
            continue
        bare[key] = value
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        bare.set_content("")
    else:
        content = body.get_content()
        if body.get_content_subtype() == "html":
            bare.set_content(content, subtype="html")
        else:
            bare.set_content(content)
    return bare


def _retry_after(value: str | None, attempt: int) -> float | None:
    """Sekunden bis zum nächsten Versuch. None: zu lange, lieber im nächsten Durchlauf."""
    try:
        wait = float(value) if value else float(2 ** attempt)
    except ValueError:
        wait = float(2 ** attempt)
    if wait > 120:
        return None
    return max(0.0, min(wait, MAX_WAIT))


def _auth_message(status: int, code: str, message: str) -> str:
    if code in ("MailboxNotEnabledForRESTAPI", "MailboxNotSupportedForRESTAPI"):
        return "Dieses Microsoft-Konto hat kein Exchange-Online-Postfach (z. B. Postfach noch auf eigenem Exchange-Server). Bitte melde dich bei uns."
    return f"Microsoft verweigert den Zugriff aufs Postfach ({status} {code}). Bitte Postfach neu verbinden oder die App von deiner IT freigeben lassen."


def is_graph(client) -> bool:
    return bool(getattr(client, "is_graph", False))


__all__ = [
    "GRAPH_BASE", "GraphClient", "GraphError", "GraphNotFound", "INBOX", "INBOX_SELECT", "OWN_HEADER", "SENT", "SENT_SELECT",
    "graph_time", "is_graph", "parse_graph_time",
]
