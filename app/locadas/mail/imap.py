"""IMAP-Zugriff: nur lesen. Gesendet-Ordner für neue Angebote/Rechnungen, Posteingang für Kundenantworten."""
from __future__ import annotations

import email
import imaplib
import logging
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

from .providers import COMMON_SENT_FOLDERS

log = logging.getLogger(__name__)

imaplib._MAXLINE = 50_000_000  # große Anhänge
_TIMEOUT = 40


class MailAuthError(Exception):
    """Login fehlgeschlagen (falsches Passwort, App-Passwort nötig, IMAP abgeschaltet)."""


class MailConnectError(Exception):
    """Server nicht erreichbar."""


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class ParsedMail:
    uid: int
    message_id: str
    subject: str
    from_addr: str
    from_name: str
    to: list[tuple[str, str]]  # (name, addr)
    date: datetime
    in_reply_to: str
    references: list[str]
    text: str
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def pdf_attachments(self) -> list[Attachment]:
        return [a for a in self.attachments if a.filename.lower().endswith(".pdf") or "pdf" in a.content_type]


def decode_str(value) -> str:
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _decode_utf7(name: bytes | str) -> str:
    if isinstance(name, bytes):
        name = name.decode("ascii", "replace")
    # Modified UTF-7 der IMAP-Ordnernamen
    def repl(m):
        chunk = m.group(1)
        if not chunk:
            return "&"
        import base64
        b = base64.b64decode(chunk.replace(",", "/") + "=" * (-len(chunk) % 4))
        return b.decode("utf-16-be", "replace")
    return re.sub(r"&([A-Za-z0-9+,]*)-", repl, name)


def _encode_utf7(name: str) -> str:
    out = []
    buf = []

    def flush():
        if buf:
            import base64
            b = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(b).decode().rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) "?(?P<delim>[^" ]*)"? (?P<name>.+)$')


def _parse_list_line(line: bytes) -> tuple[set[str], str] | None:
    m = _LIST_RE.match(line)
    if not m:
        return None
    flags = {f.decode().lower() for f in m.group("flags").split()}
    name = m.group("name").strip()
    if name.startswith(b'"') and name.endswith(b'"'):
        name = name[1:-1]
    return flags, _decode_utf7(name)


def parse_message(uid: int, raw: bytes) -> ParsedMail:
    msg: Message = email.message_from_bytes(raw)
    subject = decode_str(msg.get("Subject", ""))
    from_pairs = getaddresses([decode_str(msg.get("From", ""))])
    from_name, from_addr = (from_pairs[0] if from_pairs else ("", ""))
    to_pairs = [(decode_str(n), a.lower()) for n, a in getaddresses([decode_str(msg.get("To", "")), decode_str(msg.get("Cc", ""))]) if a]
    try:
        date = parsedate_to_datetime(msg.get("Date"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
    except Exception:
        date = datetime.now(timezone.utc)
    refs = (msg.get("References") or "").split()
    text_parts: list[str] = []
    attachments: list[Attachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition") or "").lower()
        filename = decode_str(part.get_filename())
        if filename or "attachment" in disp or ctype == "application/pdf":
            payload = part.get_payload(decode=True) or b""
            if payload:
                attachments.append(Attachment(filename=filename or "anhang", content_type=ctype, data=payload))
            continue
        if ctype == "text/plain":
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text_parts.append(payload.decode(charset, "replace"))
        elif ctype == "text/html" and not text_parts:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            html = payload.decode(charset, "replace")
            text_parts.append(re.sub(r"<[^>]+>", " ", html))
    return ParsedMail(
        uid=uid,
        message_id=(msg.get("Message-ID") or "").strip(),
        subject=subject,
        from_addr=(from_addr or "").lower(),
        from_name=from_name,
        to=to_pairs,
        date=date,
        in_reply_to=(msg.get("In-Reply-To") or "").strip(),
        references=refs,
        text="\n".join(text_parts).strip(),
        attachments=attachments,
    )


class ImapClient:
    """Dünner Wrapper um imaplib mit UID-basiertem, inkrementellem Lesen."""

    def __init__(self, host: str, port: int, ssl: bool, user: str, secret: str, auth_type: str = "password"):
        self.host, self.port, self.ssl, self.user, self.secret, self.auth_type = host, port, ssl, user, secret, auth_type
        self.conn: imaplib.IMAP4 | None = None

    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        try:
            if self.ssl:
                self.conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=_TIMEOUT)
            else:
                self.conn = imaplib.IMAP4(self.host, self.port, timeout=_TIMEOUT)
        except (OSError, socket.timeout, imaplib.IMAP4.error) as exc:
            raise MailConnectError(f"IMAP-Server {self.host}:{self.port} nicht erreichbar: {exc}") from exc
        try:
            if self.auth_type == "oauth":
                auth = f"user={self.user}\x01auth=Bearer {self.secret}\x01\x01".encode()
                self.conn.authenticate("XOAUTH2", lambda _: auth)
            else:
                self.conn.login(self.user, self.secret)
        except imaplib.IMAP4.error as exc:
            raise MailAuthError(f"Login am IMAP-Server fehlgeschlagen: {exc}") from exc

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    # -- Ordner ------------------------------------------------------------
    def list_folders(self) -> list[tuple[set[str], str]]:
        assert self.conn
        typ, data = self.conn.list()
        out = []
        if typ != "OK":
            return out
        for line in data:
            if isinstance(line, tuple):
                line = line[0]
            if not line:
                continue
            parsed = _parse_list_line(line)
            if parsed:
                out.append(parsed)
        return out

    def find_sent_folder(self, candidates: tuple[str, ...] = ()) -> str | None:
        folders = self.list_folders()
        # 1) Server markiert den Ordner selbst (RFC 6154)
        for flags, name in folders:
            if "\\sent" in flags:
                return name
        names = {n for _, n in folders}
        lower = {n.lower(): n for n in names}
        for cand in tuple(candidates) + COMMON_SENT_FOLDERS:
            if cand in names:
                return cand
            if cand.lower() in lower:
                return lower[cand.lower()]
        # 2) irgendein Ordner, der "sent" oder "gesendet" enthält
        for name in names:
            if "sent" in name.lower() or "gesendet" in name.lower():
                return name
        return None

    def select(self, folder: str, readonly: bool = True) -> tuple[int | None, int]:
        """Gibt (UIDVALIDITY, UIDNEXT) zurück."""
        assert self.conn
        quoted = '"' + _encode_utf7(folder).replace('\\', '\\\\').replace('"', '\\"') + '"'
        typ, _ = self.conn.select(quoted, readonly=readonly)
        if typ != "OK":
            raise MailConnectError(f"Ordner {folder!r} kann nicht geöffnet werden")
        validity = self.conn.response("UIDVALIDITY")[1]
        uidnext = self.conn.response("UIDNEXT")[1]
        v = int(validity[0]) if validity and validity[0] else None
        n = int(uidnext[0]) if uidnext and uidnext[0] else 0
        return v, n

    # -- Nachrichten -------------------------------------------------------
    def search_uids(self, min_uid: int = 1, since: datetime | None = None) -> list[int]:
        assert self.conn
        criteria = [f"UID {min_uid}:*"]
        if since is not None:
            criteria.append(f"SINCE {since.strftime('%d-%b-%Y')}")
        typ, data = self.conn.uid("SEARCH", *criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        uids = [int(x) for x in data[0].split()]
        return [u for u in uids if u >= min_uid]

    def fetch_message(self, uid: int) -> ParsedMail | None:
        assert self.conn
        typ, data = self.conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data:
            return None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                return parse_message(uid, item[1])
        return None

    def fetch_headers(self, uid: int) -> ParsedMail | None:
        assert self.conn
        typ, data = self.conn.uid("FETCH", str(uid), "(BODY.PEEK[HEADER])")
        if typ != "OK" or not data:
            return None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                return parse_message(uid, item[1])
        return None

    def fetch_message_size(self, uid: int) -> int:
        assert self.conn
        typ, data = self.conn.uid("FETCH", str(uid), "(RFC822.SIZE)")
        if typ != "OK" or not data or not data[0]:
            return 0
        m = re.search(rb"RFC822.SIZE (\d+)", data[0] if isinstance(data[0], bytes) else data[0][0])
        return int(m.group(1)) if m else 0


def test_login(host: str, port: int, ssl: bool, user: str, secret: str, auth_type: str = "password") -> str | None:
    """Probiert Login und gibt den gefundenen Gesendet-Ordner zurück. Wirft MailAuthError/MailConnectError."""
    with ImapClient(host, port, ssl, user, secret, auth_type) as c:
        return c.find_sent_folder()


def since_days(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)
