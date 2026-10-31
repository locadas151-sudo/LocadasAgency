"""Postfach verbinden: Anbieter erkennen, Login testen, Gesendet-Ordner finden."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .imap import ImapClient, MailAuthError, MailConnectError
from . import autoconfig
from .providers import Provider, detect_provider, guess_custom_provider
from .smtp import SmtpClient

log = logging.getLogger(__name__)


@dataclass
class ConnectionConfig:
    provider_key: str
    provider_label: str
    email: str
    username: str
    imap_host: str
    imap_port: int
    imap_ssl: bool
    smtp_host: str
    smtp_port: int
    smtp_ssl: bool
    sent_folder: str
    inbox_folder: str = "INBOX"


class ConnectFailed(Exception):
    def __init__(self, message: str, hint: str = "", provider: Provider | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.provider = provider


def _try(imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, username, password, sent_candidates) -> tuple[str, str] | None:
    """Gibt (sent_folder, smtp_host) zurück oder None, wenn der Server nicht erreichbar ist. Wirft MailAuthError bei falschem Login."""
    try:
        with ImapClient(imap_host, imap_port, imap_ssl, username, password) as c:
            sent = c.find_sent_folder(tuple(sent_candidates))
    except MailConnectError:
        return None
    if not sent:
        raise ConnectFailed("Kein Gesendet-Ordner gefunden. Prüf bitte, ob dein Mailprogramm gesendete Mails auf dem Server speichert (IMAP, nicht POP3).")
    # SMTP prüfen, bei Fehlschlag Alternativen (587 STARTTLS / 465 SSL) probieren
    for host, port, ssl in ((smtp_host, smtp_port, smtp_ssl), (smtp_host, smtp_port, not smtp_ssl), (smtp_host, 587, False), (smtp_host, 465, True)):
        try:
            SmtpClient(host, port, ssl, username, password).test()
            return sent, f"{host}|{port}|{int(ssl)}"
        except MailAuthError:
            raise
        except MailConnectError:
            continue
    raise ConnectFailed("Empfang klappt, aber der Versand-Server (SMTP) antwortet nicht. Trag bitte den SMTP-Server deines Anbieters ein.")


def connect_mailbox(
    email: str,
    password: str,
    imap_host: str | None = None,
    imap_port: int | None = None,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    username: str | None = None,
    imap_ssl: bool | None = None,
    smtp_ssl: bool | None = None,
) -> ConnectionConfig:
    email = email.strip().lower()
    username_given = bool(username and username.strip())
    username = (username or email).strip()
    provider = detect_provider(email)
    domain = email.rsplit("@", 1)[-1]

    if provider and provider.oauth_only and not imap_host:
        raise ConnectFailed(f"{provider.label} lässt keinen Passwort-Login zu.", hint=provider.hint, provider=provider)

    candidates: list[tuple[str, int, bool, str, int, bool, tuple[str, ...]]] = []
    if imap_host:
        # Vom Nutzer eingetragene Server haben Vorrang. Ohne Angabe immer verschlüsselt:
        # 143 = STARTTLS (Pflicht), alles andere SSL; SMTP 465 = SSL, 25/587/2525 = STARTTLS (Pflicht)
        i_port = imap_port or 993
        s_host = smtp_host or imap_host.replace("imap", "smtp", 1)
        s_port = smtp_port or 465
        # Beide Varianten sind verschlüsselt: SSL oder STARTTLS (Pflicht, sonst kein Login). Bei Standardports ist klar, welche.
        if imap_ssl is not None:
            i_opts = [imap_ssl]
        else:
            i_opts = [True] if i_port == 993 else [False] if i_port == 143 else [True, False]
        s_ssl = smtp_ssl if smtp_ssl is not None else (s_port not in (25, 587, 2525))
        for i_ssl in i_opts:
            candidates.append((imap_host, i_port, i_ssl, s_host, s_port, s_ssl, ()))
    elif provider:
        candidates.append((provider.imap_host, provider.imap_port, provider.imap_ssl, provider.smtp_host, provider.smtp_port, provider.smtp_ssl, provider.sent_folders))
    else:
        # Eigene Domain: Server über Autoconfig/MX ermitteln, dann nur diesen einen mit dem Passwort ansprechen
        provider = guess_custom_provider(email)
        resolved = autoconfig.resolve(email)
        if resolved is not None and resolved.provider is not None and resolved.provider.oauth_only:
            raise ConnectFailed(
                f"Deine Domain liegt bei {resolved.provider.label}. Dort ist kein Passwort-Login möglich.",
                hint=resolved.provider.hint, provider=resolved.provider,
            )
        if resolved is not None:
            if resolved.provider is not None:
                provider = resolved.provider
            if resolved.username and not username_given:
                username = resolved.username
            sent = provider.sent_folders if resolved.provider is not None else guess_custom_provider(email).sent_folders
            candidates.append((resolved.imap_host, resolved.imap_port, resolved.imap_ssl, resolved.smtp_host, resolved.smtp_port, resolved.smtp_ssl, sent))

    last_auth_error: MailAuthError | None = None
    for ih, ip, issl, sh, sp, sssl, sent_cands in candidates:
        try:
            result = _try(ih, ip, issl, sh, sp, sssl, username, password, sent_cands)
        except MailAuthError as exc:
            last_auth_error = exc
            # Bei bekanntem Anbieter ist falsches Passwort eindeutig, nicht weiterprobieren
            if provider and provider.key != "custom":
                break
            continue
        if result is None:
            continue
        sent, smtp_spec = result
        s_host, s_port, s_ssl = smtp_spec.split("|")
        return ConnectionConfig(
            provider_key=provider.key if provider else "custom",
            provider_label=provider.label if provider else domain,
            email=email,
            username=username,
            imap_host=ih,
            imap_port=ip,
            imap_ssl=issl,
            smtp_host=s_host,
            smtp_port=int(s_port),
            smtp_ssl=bool(int(s_ssl)),
            sent_folder=sent,
        )

    if last_auth_error is not None:
        hint = provider.hint if provider else ""
        if provider and provider.needs_app_password:
            msg = f"Login abgelehnt. {provider.label} verlangt ein App-Passwort."
        else:
            msg = "Login abgelehnt. Prüf bitte Adresse und Passwort."
        raise ConnectFailed(msg, hint=hint, provider=provider)
    raise ConnectFailed(
        "Wir finden den Mailserver zu dieser Adresse nicht automatisch.",
        hint="Trag unten die Server deines Anbieters ein (IMAP und SMTP). Die stehen in den Konto-Einstellungen deines Mailprogramms oder auf der Hilfeseite deines Anbieters.",
        provider=provider,
    )
