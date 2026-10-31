"""Fake Microsoft Graph (Mail) auf localhost, ohne Netzwerk.

Kann: /me, Ordner, Nachrichtenliste mit $filter (ge), $orderby asc und Paging über @odata.nextLink, Rohmail ($value),
Text (body), sendMail mit MIME, Entwurf aus MIME plus Anhänge plus Upload-Session plus send. Access-Tokens werden
geprüft (401 bei unbekanntem Token), einzelne Antworten lassen sich einmalig auf Fehler stellen (429, 503 …).
Dazu ein Fake-Token-Endpunkt für oauth._post (Refresh mit Rotation, invalid_grant auf Knopfdruck).
"""
from __future__ import annotations

import base64
import email
import email.header
import email.policy
import itertools
import json
import threading
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recips(value: str) -> list[dict]:
    return [{"emailAddress": {"name": n, "address": a}} for n, a in getaddresses([value or ""]) if a]


class GraphStore:
    def __init__(self, user: str):
        self.user = user
        self.messages: list[dict] = []          # {"id", "folder", "meta", "raw"}
        self.valid_tokens: set[str] = set()
        self.page_size = 2
        self.fail: list[tuple[str, str, int, dict]] = []  # (Methode, Pfad-Anfang, Status, Header), je einmal
        self.log: list[tuple[str, str]] = []
        self.auth_headers: list[str | None] = []
        self.sent_mime: list[bytes] = []
        self.send_content_types: list[str] = []
        self.drafts: dict[str, dict] = {}
        self.uploads: list[dict] = []
        self.token_calls: list[dict] = []
        self.token_error: str = ""
        self.mailbox_error: str = ""             # z. B. MailboxNotEnabledForRESTAPI
        self._ids = itertools.count(1)
        self._tok = itertools.count(1)
        self.base = ""
        self.lock = threading.Lock()

    # -- Tokens (für oauth._post) ---------------------------------------------------------------
    def issue_token(self) -> str:
        tok = f"at-{next(self._tok)}"
        self.valid_tokens.add(tok)
        return tok

    def token_post(self, url: str, data: dict) -> tuple[int, str]:
        self.token_calls.append(dict(data))
        if self.token_error:
            return 400, json.dumps({"error": self.token_error, "error_description": "AADSTS70000: The provided grant has expired"})
        n = len(self.token_calls)
        return 200, json.dumps({"access_token": self.issue_token(), "refresh_token": f"rt-rotated-{n}", "expires_in": 3600})

    # -- Postfach -------------------------------------------------------------------------------
    def add(self, folder: str, raw: bytes, when: datetime | None = None) -> dict:
        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
        try:
            date = parsedate_to_datetime(msg["Date"]) if msg["Date"] else datetime.now(timezone.utc)
        except Exception:
            date = datetime.now(timezone.utc)
        when = when or date
        mid = f"AAMk{next(self._ids):04d}-x_y="
        has_att = any(part.get_filename() for part in msg.walk() if not part.is_multipart())
        meta = {
            "id": mid,
            "internetMessageId": (msg["Message-ID"] or "").strip(),
            "subject": str(email.header.make_header(email.header.decode_header(msg["Subject"] or ""))),
            "toRecipients": _recips(msg["To"] or ""),
            "ccRecipients": _recips(msg["Cc"] or ""),
            "from": (_recips(msg["From"] or "") or [{}])[0],
            "hasAttachments": has_att,
            "conversationId": "conv-1",
            "sentDateTime": _iso(when),
            "receivedDateTime": _iso(when),
            "internetMessageHeaders": [{"name": k, "value": str(v)} for k, v in msg.items()],
            "singleValueExtendedProperties": [{"id": "Integer 0xe08", "value": str(len(raw))}],
        }
        with self.lock:
            self.messages.append({"id": mid, "folder": folder, "meta": meta, "raw": raw})
        return meta

    def fail_once(self, method: str, path_prefix: str, status: int, headers: dict | None = None) -> None:
        self.fail.append((method, path_prefix, status, headers or {}))

    def values_fetched(self) -> int:
        return sum(1 for m, p in self.log if m == "GET" and p.endswith("/$value"))


class _Handler(BaseHTTPRequestHandler):
    store: GraphStore

    def log_message(self, *a):  # still
        pass

    def _reply(self, status: int, body: bytes | dict | None = None, headers: dict | None = None, ctype: str = "application/json"):
        data = json.dumps(body).encode() if isinstance(body, dict) else (body or b"")
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _err(self, status: int, code: str, headers: dict | None = None):
        self._reply(status, {"error": {"code": code, "message": code}}, headers)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _handle(self, method: str):
        s = self.store
        url = urlparse(self.path)
        path = url.path
        q = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        s.log.append((method, path))
        body = self._body()
        if path.startswith("/upload/"):
            s.auth_headers.append(self.headers.get("Authorization"))
            draft = s.drafts[path.rsplit("/", 1)[-1]]
            draft["chunks"].append((self.headers.get("Content-Range"), body))
            return self._reply(200 if len(b"".join(c for _, c in draft["chunks"])) < draft["upload_size"] else 201, {})
        if not path.startswith("/v1.0/"):
            return self._err(404, "NotFound")
        path = path[len("/v1.0"):]
        for i, (m, prefix, status, hdrs) in enumerate(s.fail):
            if m == method and path.startswith(prefix):
                s.fail.pop(i)
                return self._err(status, "TooManyRequests" if status == 429 else "ServiceUnavailable", hdrs)
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in s.valid_tokens:
            return self._err(401, "InvalidAuthenticationToken")
        if s.mailbox_error and path.startswith("/me/mailFolders"):
            return self._err(404, s.mailbox_error)

        if method == "GET" and path == "/me":
            return self._reply(200, {"mail": None, "userPrincipalName": s.user.upper(), "displayName": "Thomas"})
        if method == "GET" and path.startswith("/me/mailFolders/") and path.count("/") == 3:
            return self._reply(200, {"id": path.rsplit("/", 1)[-1]})
        if method == "GET" and path.startswith("/me/mailFolders/") and path.endswith("/messages"):
            folder = path.split("/")[3]
            field, _, since = (q.get("$filter") or "").split(" ", 2)
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            assert q.get("$orderby") == f"{field} asc"
            rows = [m for m in s.messages if m["folder"] == folder and datetime.fromisoformat(m["meta"][field].replace("Z", "+00:00")) >= since_dt]
            rows.sort(key=lambda m: m["meta"][field])
            skip = int(q.get("$skip") or 0)
            page = rows[skip:skip + s.page_size]
            select = set((q.get("$select") or "").split(","))
            value = []
            for m in page:
                meta = {k: v for k, v in m["meta"].items() if k in select or k == "id"}
                if "$expand" in q:
                    meta["singleValueExtendedProperties"] = m["meta"]["singleValueExtendedProperties"]
                value.append(meta)
            out = {"value": value}
            if skip + s.page_size < len(rows):
                nq = dict(q)
                nq["$skip"] = str(skip + s.page_size)
                out["@odata.nextLink"] = f"{s.base}{path}?{urlencode(nq, quote_via=quote)}"
            return self._reply(200, out)
        if path.startswith("/me/messages/") and method == "GET":
            parts = path.split("/")
            mid = parts[3]
            msg = next((m for m in s.messages if m["id"] == mid), None)
            if msg is None:
                return self._err(404, "ErrorItemNotFound")
            if len(parts) == 5 and parts[4] == "$value":
                return self._reply(200, msg["raw"], ctype="message/rfc822")
            from locadas.mail.imap import extract_text
            assert "text" in (self.headers.get("Prefer") or "")
            return self._reply(200, {"body": {"contentType": "text", "content": extract_text(msg["raw"], 20000)}})
        if method == "POST" and path == "/me/sendMail":
            s.send_content_types.append(self.headers.get("Content-Type", ""))
            raw = base64.b64decode(body)
            s.sent_mime.append(raw)
            s.add("sentitems", raw, datetime.now(timezone.utc))
            return self._reply(202, b"")
        if method == "POST" and path == "/me/messages":
            raw = base64.b64decode(body)
            did = f"draft{len(s.drafts) + 1}"
            s.drafts[did] = {"raw": raw, "attachments": [], "chunks": [], "upload_size": 0, "sent": False}
            return self._reply(201, {"id": did})
        if method == "POST" and path.startswith("/me/messages/"):
            parts = path.split("/")
            draft = s.drafts.get(parts[3])
            if draft is None:
                return self._err(404, "ErrorItemNotFound")
            if parts[4:] == ["attachments"]:
                draft["attachments"].append(json.loads(body))
                return self._reply(201, {"id": "att"})
            if parts[4:] == ["attachments", "createUploadSession"]:
                item = json.loads(body)["AttachmentItem"]
                draft["upload_size"] = item["size"]
                draft["upload_name"] = item["name"]
                return self._reply(201, {"uploadUrl": f"{s.base.rsplit('/v1.0', 1)[0]}/upload/{parts[3]}"})
            if parts[4:] == ["send"]:
                draft["sent"] = True
                s.sent_mime.append(draft["raw"])
                return self._reply(202, b"")
        return self._err(400, "BadRequest")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")


class FakeGraphServer:
    def __init__(self, user: str):
        self.store = GraphStore(user)
        handler = type("H", (_Handler,), {"store": self.store})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.store.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1.0"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return self.store.base

    def start(self) -> "FakeGraphServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
