"""Hilfen rund um MailAccount: Clients bauen, Secret entschlüsseln, Account anlegen, Verbindungs-Wächter."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..crypto import decrypt, encrypt
from ..db import AccountStatus, Business, DocKind, DocStatus, Document, FollowUpStatus, MailAccount, log_event, utcnow
from ..mail.connect import ConnectionConfig
from ..mail import oauth
from ..mail.graph import GraphClient
from ..mail.imap import ImapClient, MailAuthError, MailConnectError
from ..mail.smtp import SmtpClient

OAUTH_TYPES = ("oauth", "graph")


def _store_refresh_token(account: MailAccount):
    def store(token: str) -> None:
        account.secret_enc = encrypt(token)
    return store


def _secret(account: MailAccount, force_refresh: bool = False) -> str:
    """Passwort, oder bei OAuth ein frisches Access-Token aus dem gespeicherten Refresh-Token.

    Token abgelehnt (invalid_grant, widerrufen): MailAuthError, der Verbindungs-Wächter meldet es sofort.
    Token-Server nicht erreichbar: MailConnectError, zählt erst nach mehreren Durchläufen."""
    raw = decrypt(account.secret_enc)
    if account.auth_type not in OAUTH_TYPES:
        return raw
    provider = oauth.get(account.provider)
    if provider is None:
        raise MailAuthError(f"OAuth-Anbieter {account.provider!r} unbekannt")
    key = f"acc:{account.id}"
    if force_refresh:
        oauth.forget(key)
    # Alte Microsoft-Konten (IMAP/SMTP) behalten ihre Scopes, sonst verlangt Microsoft eine neue Zustimmung
    scopes = provider.legacy_scopes if account.auth_type == "oauth" and provider.legacy_scopes else provider.scopes
    try:
        return oauth.access_token(provider, raw, cache_key=key, scopes=scopes, on_new_refresh_token=_store_refresh_token(account))
    except oauth.OAuthUnavailable as exc:
        raise MailConnectError(str(exc)) from exc
    except oauth.OAuthError as exc:
        raise MailAuthError(str(exc)) from exc


def is_graph(account: MailAccount | None) -> bool:
    return account is not None and account.auth_type == "graph"


def graph_for(account: MailAccount) -> GraphClient:
    """Token wird erst bei der ersten Anfrage geholt, Fehler kommen also im try des Aufrufers an."""
    return GraphClient(lambda force: _secret(account, force_refresh=force), user=account.email)


def imap_for(account: MailAccount) -> ImapClient | GraphClient:
    if is_graph(account):
        return graph_for(account)
    return ImapClient(account.imap_host, account.imap_port, account.imap_ssl, account.username, _secret(account), account.auth_type)


def smtp_for(account: MailAccount) -> SmtpClient | GraphClient:
    if is_graph(account):
        return graph_for(account)
    return SmtpClient(account.smtp_host, account.smtp_port, account.smtp_ssl, account.username, _secret(account), account.auth_type)


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
    """Alt: sofort auf Fehler. Neue Stellen nehmen record_failure (zählt, meldet dem Betrieb)."""
    account.status = AccountStatus.ERROR
    account.last_error = message[:2000]
    log_event(session, account.business_id, "error", f"Postfach {account.email}: {message[:300]}")


# --- Verbindungs-Wächter ---------------------------------------------------------------
# Passwort falsch (MailAuthError): sofort gestört. Server nicht erreichbar (MailConnectError): erst nach 3 Durchläufen
# in Folge. Gestört heißt: kein Nachfassen über dieses Postfach, der Betrieb bekommt einmal Bescheid mit Link zum
# Neu-Verbinden, höchstens alle 3 Tage eine Erinnerung. Läuft es wieder, verteilen sich die liegengebliebenen Stufen.

log = logging.getLogger(__name__)

ERROR_THRESHOLD = 3                     # Verbindungsfehler in Folge, bis das Postfach als gestört gilt
CYCLE_GAP = timedelta(minutes=2)        # mehrere Fehler kurz hintereinander zählen als ein Durchlauf
REMIND_EVERY = timedelta(days=3)        # Erinnerung, solange es gestört bleibt
RETRY_WHILE_BROKEN = timedelta(hours=1)  # gestörtes Postfach nur stündlich probieren (Anbieter sperren sonst gern)
INVOICE_RESUME_GAP = timedelta(days=7)


def is_down(account: MailAccount | None) -> bool:
    return account is not None and account.status == AccountStatus.ERROR


def record_failure(session: Session, account: MailAccount, exc: Exception, now: datetime | None = None) -> None:
    """Fehler beim Lesen oder Senden über das Postfach des Betriebs."""
    now = now or utcnow()
    message = str(exc) or exc.__class__.__name__
    account.last_error = message[:2000]
    same_cycle = account.last_failure_at is not None and abs(now - account.last_failure_at) < CYCLE_GAP
    if not same_cycle:
        account.consecutive_errors = (account.consecutive_errors or 0) + 1
    account.last_failure_at = now
    if account.error_since is None:
        account.error_since = now
    if account.status == AccountStatus.ACTIVE and (isinstance(exc, MailAuthError) or account.consecutive_errors >= ERROR_THRESHOLD):
        account.status = AccountStatus.ERROR
        log_event(session, account.business_id, "error", f"Postfach {account.email}: {message[:300]}")
    if account.status == AccountStatus.ERROR:
        alert(session, account, now)


def alert(session: Session, account: MailAccount, now: datetime | None = None) -> bool:
    """Betrieb einmal je Störung Bescheid geben, danach höchstens alle 3 Tage erinnern."""
    from ..notify import messages as wa_msgs
    from ..notify import send as notify

    now = now or utcnow()
    if account.status != AccountStatus.ERROR:
        return False
    last = account.error_notified_at
    if last is not None and now - last < REMIND_EVERY:
        return False
    try:
        ok = notify(session, wa_msgs.connection_lost(account.business, account, reminder=last is not None), now)
    except Exception as exc:  # nie den Durchlauf daran scheitern lassen
        log.warning("Störungsmeldung für %s ging nicht raus: %s", account.email, exc)
        ok = False
    if ok:
        account.error_notified_at = now
        log_event(session, account.business_id, "connection", f"{'Erinnerung' if last else 'Bescheid'}: Postfach {account.email} ist nicht verbunden, Nachfassen pausiert.")
    return ok


def remind_if_due(session: Session, account: MailAccount, now: datetime | None = None) -> bool:
    """Für Durchläufe, in denen das gestörte Postfach gar nicht probiert wird."""
    return alert(session, account, now) if is_down(account) else False


def retry_due(account: MailAccount, now: datetime | None = None) -> bool:
    """Gestörtes Postfach: nur stündlich neu probieren."""
    if not is_down(account) or account.last_failure_at is None:
        return True
    return (now or utcnow()) - account.last_failure_at >= RETRY_WHILE_BROKEN


def mark_ok(account: MailAccount, session: Session | None = None, now: datetime | None = None) -> None:
    """Lesen bzw. Senden hat geklappt. War das Postfach gestört (und session da): wieder verbunden melden."""
    was_down = account.status == AccountStatus.ERROR
    account.consecutive_errors = 0
    account.last_failure_at = None
    account.last_error = ""
    if not was_down:
        account.error_since = None
        account.error_notified_at = None
        return
    account.status = AccountStatus.ACTIVE
    if session is None:
        account.error_since = None
        account.error_notified_at = None
        return
    recovered(session, account, now)


def recovered(session: Session, account: MailAccount, now: datetime | None = None) -> None:
    from ..notify import messages as wa_msgs
    from ..notify import send as notify

    now = now or utcnow()
    told = account.error_notified_at is not None
    account.status = AccountStatus.ACTIVE
    account.error_since = None
    account.error_notified_at = None
    moved = resume_followups(session, account, now)
    log_event(session, account.business_id, "connection", f"Postfach {account.email} wieder verbunden. Nachfassen läuft weiter{f', {moved} Vorgänge neu verteilt' if moved else ''}.")
    if told:
        try:
            notify(session, wa_msgs.connection_back(account.business, account), now)
        except Exception as exc:
            log.warning("Wieder-verbunden-Meldung für %s ging nicht raus: %s", account.email, exc)


def resume_followups(session: Session, account: MailAccount, now: datetime | None = None) -> int:
    """Nach einer Störung: liegengebliebene Stufen ab jetzt neu verteilen statt alle auf einmal.

    Höchstens MAX_SENDS_PER_DAY Vorgänge je Tag, je Vorgang wieder der übliche Abstand zwischen den Stufen.
    Jede Stufe wird neu angekündigt (Morgen-Vorschau bzw. Freigabe), nichts geht ungefragt raus."""
    from ..config import settings
    from .followups import OFFER_MIN_GAP, next_send_time

    now = now or utcnow()
    docs = session.scalars(select(Document).where(Document.account_id == account.id, Document.status == DocStatus.OPEN)).all()
    backlog = []
    for d in docs:
        pending = sorted((f for f in d.followups if f.status == FollowUpStatus.SCHEDULED), key=lambda f: f.step)
        if pending and pending[0].due_at < now:
            backlog.append((pending[0].due_at, d, pending))
    backlog.sort(key=lambda x: (x[0], x[1].id))
    per_day = max(1, settings.max_sends_per_day)
    for i, (_, d, pending) in enumerate(backlog):
        earliest = next_send_time(now + timedelta(days=i // per_day))
        gap = OFFER_MIN_GAP if d.kind == DocKind.OFFER else INVOICE_RESUME_GAP
        for f in pending:
            if f.due_at < earliest:
                f.due_at = earliest
                f.announced_at = None
                f.send_after = None
            earliest = f.due_at + gap
        d.next_followup_at = min(f.due_at for f in pending)
    return len(backlog)


def reconnect(session: Session, account: MailAccount, cfg: ConnectionConfig, secret: str, auth_type: str = "password", now: datetime | None = None) -> None:
    """Neue Zugangsdaten für dasselbe Postfach: alles bleibt (Dokumente, Verlauf, Stand der Ordner), nur der Fehler geht."""
    if auth_type == "graph" and account.auth_type != "graph" and not account.graph_sync and account.last_scan_at is not None:
        # Wechsel von IMAP auf Graph: dort weitermachen, wo IMAP aufgehört hat, statt neu ab 30 Tagen zu lesen
        import json

        account.graph_sync = json.dumps({
            "sent": {"t": (account.last_scan_at - timedelta(days=1)).isoformat(), "ids": []},
            "inbox": {"t": account.last_scan_at.isoformat(), "ids": []},
        })
    if "graph" in (auth_type, account.auth_type):  # Ordnernamen unterscheiden sich zwischen IMAP und Graph
        account.sent_folder, account.inbox_folder = cfg.sent_folder or account.sent_folder, cfg.inbox_folder or account.inbox_folder
    account.secret_enc = encrypt(secret)
    account.auth_type = auth_type
    account.username = cfg.username
    account.imap_host, account.imap_port, account.imap_ssl = cfg.imap_host, cfg.imap_port, cfg.imap_ssl
    account.smtp_host, account.smtp_port, account.smtp_ssl = cfg.smtp_host, cfg.smtp_port, cfg.smtp_ssl
    log_event(session, account.business_id, "account", f"Postfach {account.email} neu verbunden.")
    was_down = account.status == AccountStatus.ERROR
    account.consecutive_errors = 0
    account.last_failure_at = None
    account.last_error = ""
    if was_down:
        recovered(session, account, now)
    else:
        account.status = AccountStatus.ACTIVE
        account.error_since = None
        account.error_notified_at = None
