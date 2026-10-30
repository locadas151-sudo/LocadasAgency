"""Hilfen rund um MailAccount: Clients bauen, Secret entschlüsseln, Account anlegen."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..crypto import decrypt, encrypt
from ..db import AccountStatus, Business, MailAccount, log_event
from ..mail.connect import ConnectionConfig
from ..mail.imap import ImapClient
from ..mail.smtp import SmtpClient


def imap_for(account: MailAccount) -> ImapClient:
    return ImapClient(account.imap_host, account.imap_port, account.imap_ssl, account.username, decrypt(account.secret_enc), account.auth_type)


def smtp_for(account: MailAccount) -> SmtpClient:
    return SmtpClient(account.smtp_host, account.smtp_port, account.smtp_ssl, account.username, decrypt(account.secret_enc), account.auth_type)


def create_account(session: Session, business: Business, cfg: ConnectionConfig, secret: str, auth_type: str = "password") -> MailAccount:
    account = MailAccount(
        business_id=business.id,
        provider=cfg.provider_key,
        email=cfg.email,
        display_name=business.name,
        auth_type=auth_type,
        username=cfg.username,
        secret_enc=encrypt(secret),
        imap_host=cfg.imap_host,
        imap_port=cfg.imap_port,
        imap_ssl=cfg.imap_ssl,
        smtp_host=cfg.smtp_host,
        smtp_port=cfg.smtp_port,
        smtp_ssl=cfg.smtp_ssl,
        sent_folder=cfg.sent_folder,
        inbox_folder=cfg.inbox_folder,
        status=AccountStatus.ACTIVE,
    )
    session.add(account)
    session.flush()
    log_event(session, business.id, "account", f"Postfach {cfg.email} verbunden ({cfg.provider_label}).")
    return account


def mark_error(session: Session, account: MailAccount, message: str) -> None:
    account.status = AccountStatus.ERROR
    account.last_error = message[:2000]
    log_event(session, account.business_id, "error", f"Postfach {account.email}: {message[:300]}")


def mark_ok(account: MailAccount) -> None:
    if account.status == AccountStatus.ERROR:
        account.status = AccountStatus.ACTIVE
    account.last_error = ""
