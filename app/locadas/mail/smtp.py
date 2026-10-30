"""SMTP-Versand über das Postfach des Betriebs. Absender ist immer der Betrieb selbst."""
from __future__ import annotations

import logging
import smtplib
import socket
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from .imap import MailAuthError, MailConnectError

log = logging.getLogger(__name__)
_TIMEOUT = 40


def build_message(
    from_name: str,
    from_addr: str,
    to_name: str,
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: list[str] | None = None,
    reply_to: str = "",
    domain: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = formataddr((to_name, to_addr)) if to_name else to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=domain or from_addr.rsplit("@", 1)[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        refs = list(references or [])
        if in_reply_to not in refs:
            refs.append(in_reply_to)
        msg["References"] = " ".join(refs)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    return msg


class SmtpClient:
    def __init__(self, host: str, port: int, ssl: bool, user: str, secret: str, auth_type: str = "password"):
        self.host, self.port, self.ssl, self.user, self.secret, self.auth_type = host, port, ssl, user, secret, auth_type

    def _connect(self) -> smtplib.SMTP:
        try:
            if self.ssl:
                conn: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=_TIMEOUT)
            else:
                conn = smtplib.SMTP(self.host, self.port, timeout=_TIMEOUT)
                conn.ehlo()
                if conn.has_extn("starttls"):
                    conn.starttls()
                    conn.ehlo()
        except (OSError, socket.timeout, smtplib.SMTPException) as exc:
            raise MailConnectError(f"SMTP-Server {self.host}:{self.port} nicht erreichbar: {exc}") from exc
        try:
            if self.auth_type == "oauth":
                auth = f"user={self.user}\x01auth=Bearer {self.secret}\x01\x01"
                conn.ehlo()
                conn.auth("XOAUTH2", lambda _=None: auth, initial_response_ok=True)
            elif conn.has_extn("auth"):
                conn.login(self.user, self.secret)
        except smtplib.SMTPAuthenticationError as exc:
            conn.close()
            raise MailAuthError(f"Login am SMTP-Server fehlgeschlagen: {exc}") from exc
        except smtplib.SMTPException as exc:
            conn.close()
            raise MailConnectError(f"SMTP-Fehler: {exc}") from exc
        return conn

    def test(self) -> None:
        conn = self._connect()
        try:
            conn.noop()
        finally:
            conn.quit()

    def send(self, msg: EmailMessage) -> str:
        conn = self._connect()
        try:
            conn.send_message(msg)
        except smtplib.SMTPException as exc:
            raise MailConnectError(f"Versand fehlgeschlagen: {exc}") from exc
        finally:
            try:
                conn.quit()
            except Exception:
                pass
        return msg["Message-ID"]
