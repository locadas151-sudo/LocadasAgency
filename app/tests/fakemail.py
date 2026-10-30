"""Test-Infrastruktur: ein kleiner IMAP-Server (nur die Befehle, die imaplib braucht) und ein SMTP-Sink.

Damit laufen die End-to-End-Tests ohne echtes Postfach, aber über die echten imaplib/smtplib-Pfade.
"""
from __future__ import annotations

import re
import socketserver
import threading
from email.message import EmailMessage
from email.utils import make_msgid

from aiosmtpd.controller import Controller


class _Store:
    def __init__(self):
        self.folders: dict[str, list[tuple[int, bytes]]] = {"INBOX": [], "Gesendet": []}
        self.uidnext: dict[str, int] = {"INBOX": 1, "Gesendet": 1}
        self.uidvalidity: dict[str, int] = {"INBOX": 11, "Gesendet": 22}
        self.lock = threading.Lock()

    def append(self, folder: str, raw: bytes) -> int:
        with self.lock:
            uid = self.uidnext[folder]
            self.uidnext[folder] = uid + 1
            self.folders[folder].append((uid, raw))
            return uid


class _Handler(socketserver.StreamRequestHandler):
    def _send(self, line: str | bytes):
        if isinstance(line, str):
            line = line.encode()
        self.wfile.write(line + b"\r\n")
        self.wfile.flush()

    def handle(self):
        srv: FakeImapServer = self.server  # type: ignore[assignment]
        store = srv.store
        self._send("* OK FakeIMAP ready")
        selected: str | None = None
        authed = False
        while True:
            line = self.rfile.readline()
            if not line:
                return
            line = line.rstrip(b"\r\n")
            parts = line.split(b" ", 2)
            if len(parts) < 2:
                continue
            tag, cmd = parts[0].decode(), parts[1].decode().upper()
            rest = parts[2].decode() if len(parts) > 2 else ""
            srv.log.append(f"{cmd} {rest}")
            if cmd == "CAPABILITY":
                self._send("* CAPABILITY IMAP4rev1 AUTH=PLAIN")
                self._send(f"{tag} OK CAPABILITY completed")
            elif cmd == "LOGIN":
                m = re.match(r'"?([^" ]+)"? "?([^"]*)"?$', rest)
                if m and m.group(1) == srv.user and m.group(2) == srv.password:
                    authed = True
                    self._send(f"{tag} OK LOGIN completed")
                else:
                    self._send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials")
            elif cmd == "LOGOUT":
                self._send("* BYE")
                self._send(f"{tag} OK LOGOUT completed")
                return
            elif not authed:
                self._send(f"{tag} NO Please login first")
            elif cmd == "NOOP":
                self._send(f"{tag} OK NOOP completed")
            elif cmd == "LIST":
                self._send('* LIST (\\HasNoChildren) "/" "INBOX"')
                self._send('* LIST (\\HasNoChildren \\Sent) "/" "Gesendet"')
                self._send(f"{tag} OK LIST completed")
            elif cmd in ("SELECT", "EXAMINE"):
                name = rest.strip().strip('"')
                if name not in store.folders:
                    self._send(f"{tag} NO no such mailbox")
                    continue
                selected = name
                with store.lock:
                    n = len(store.folders[name])
                    self._send(f"* {n} EXISTS")
                    self._send("* 0 RECENT")
                    self._send(f"* OK [UIDVALIDITY {store.uidvalidity[name]}] UIDs valid")
                    self._send(f"* OK [UIDNEXT {store.uidnext[name]}] Predicted next UID")
                self._send(f"{tag} OK [READ-ONLY] {cmd} completed")
            elif cmd == "UID" and selected:
                sub, _, args = rest.partition(" ")
                sub = sub.upper()
                with store.lock:
                    msgs = list(store.folders[selected])
                if sub == "SEARCH":
                    m = re.search(r"UID (\d+):(\*|\d+)", args)
                    lo = int(m.group(1)) if m else 1
                    hi = None if (not m or m.group(2) == "*") else int(m.group(2))
                    found = [str(u) for u, _ in msgs if u >= lo and (hi is None or u <= hi)]
                    self._send("* SEARCH " + " ".join(found) if found else "* SEARCH")
                    self._send(f"{tag} OK SEARCH completed")
                elif sub == "FETCH":
                    m = re.match(r"(\d+) \((.*)\)$", args)
                    uid, what = int(m.group(1)), m.group(2).upper()
                    for seq, (u, raw) in enumerate(msgs, start=1):
                        if u != uid:
                            continue
                        if "RFC822.SIZE" in what:
                            self._send(f"* {seq} FETCH (UID {u} RFC822.SIZE {len(raw)})")
                        elif "HEADER" in what:
                            hdr = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                            self.wfile.write(f"* {seq} FETCH (UID {u} BODY[HEADER] {{{len(hdr)}}}\r\n".encode() + hdr + b")\r\n")
                        else:
                            self.wfile.write(f"* {seq} FETCH (UID {u} BODY[] {{{len(raw)}}}\r\n".encode() + raw + b")\r\n")
                        self.wfile.flush()
                    self._send(f"{tag} OK FETCH completed")
                else:
                    self._send(f"{tag} BAD unknown uid command")
            else:
                self._send(f"{tag} BAD unknown command {cmd}")


class FakeImapServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, user: str, password: str):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.user, self.password = user, password
        self.store = _Store()
        self.log: list[str] = []
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    def start(self) -> "FakeImapServer":
        self._thread.start()
        return self

    def stop(self):
        self.shutdown()
        self.server_close()

    @property
    def port(self) -> int:
        return self.server_address[1]


class _SmtpSink:
    def __init__(self):
        self.messages: list[tuple[str, list[str], bytes]] = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append((envelope.mail_from, list(envelope.rcpt_tos), envelope.content))
        return "250 OK"


class FakeSmtpServer:
    def __init__(self):
        import socket
        self.sink = _SmtpSink()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.controller = Controller(self.sink, hostname="127.0.0.1", port=port)

    def start(self) -> "FakeSmtpServer":
        self.controller.start()
        return self

    def stop(self):
        self.controller.stop()

    @property
    def port(self) -> int:
        return self.controller.port

    @property
    def messages(self):
        return self.sink.messages


def make_mail(from_addr: str, to: str, subject: str, body: str, pdf: tuple[str, bytes] | None = None, in_reply_to: str = "", from_name: str = "", to_name: str = "") -> bytes:
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = f"{to_name} <{to}>" if to_name else to
    msg["Subject"] = subject
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    msg["Date"] = format_datetime(datetime.now(timezone.utc) - timedelta(days=1))
    msg["Message-ID"] = make_msgid(domain="test.local")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    if pdf:
        msg.add_attachment(pdf[1], maintype="application", subtype="pdf", filename=pdf[0])
    return msg.as_bytes()


def make_pdf(lines: list[str]) -> bytes:
    """Kleines Text-PDF per reportlab, so dass pdfplumber Text findet."""
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in lines:
        c.drawString(50, y, line)
        y -= 18
    c.save()
    return buf.getvalue()
