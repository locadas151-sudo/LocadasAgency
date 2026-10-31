"""Ein Kanal für alles, was an den Betrieb geht: WhatsApp-Sekretär zuerst, sonst Mail ins eigene Postfach."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import settings
from ..db import Business, utcnow
from ..mail.imap import MailAuthError, MailConnectError
from ..mail.smtp import build_message
from . import whatsapp as wa

log = logging.getLogger(__name__)

KINDS = {"preview", "opened", "replied", "bounced", "outcome", "approval", "login", "info", "completion", "tap", "summary", "connection", "trial"}


@dataclass
class Notification:
    business: Business
    kind: str
    title: str                      # erste, fette Zeile in WhatsApp
    text: str                       # WhatsApp-Text (kurz)
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (Beschriftung ≤20, signierte Aktion), höchstens 3
    email_subject: str = ""         # Mail-Fallback: genau der bisherige Text
    email_body: str = ""
    doc_ids: list[int] = field(default_factory=list)
    footer: str = ""
    # Außerhalb des 24h-Fensters: Vorlage (Schlüssel in settings.whatsapp_templates) mit festen Knöpfen.
    # None = keine Vorlage, dann geht es außerhalb des Fensters per Mail.
    template: str | None = None
    template_params: list[str] = field(default_factory=list)
    template_buttons: list[str] | None = None   # Payloads der Vorlagen-Knöpfe, Standard: die von buttons
    # „In WhatsApp öffnen“: Nachricht mit URL-Knopf (cta_url), danach die Antwort-Knöpfe als eigene Nachricht
    cta: tuple[str, str] | None = None          # (Beschriftung ≤20, URL)
    buttons_text: str = ""                      # Text über den Antwort-Knöpfen, wenn cta gesetzt ist
    template_url: str | None = None             # Vorlage mit URL-Knopf: dynamischer Teil der URL (Index 0)
    context: dict | None = None     # wird bei Zustellung per WhatsApp am Betrieb gemerkt (z. B. Liste für „Stopp 3“)
    channel: str | None = None      # nach send(): "whatsapp", "email" oder None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"Unbekannte Art {self.kind}")
        if len(self.buttons) > wa.MAX_BUTTONS:
            raise ValueError("Höchstens 3 Knöpfe")


def whatsapp_ready(business: Business) -> bool:
    return bool(settings.whatsapp_enabled and business.whatsapp_opt_in and business.whatsapp_number)


def send(session: Session, n: Notification, now: datetime | None = None) -> bool:
    """Zustellen. WhatsApp, wenn eingerichtet und erlaubt; bei Fehler oder ohne passende Vorlage per Mail."""
    now = now or utcnow()
    b = n.business
    if whatsapp_ready(b):
        try:
            if _deliver_whatsapp(b, n, now):
                n.channel = "whatsapp"
                if n.context is not None:
                    b.whatsapp_context = json.dumps(n.context)
                return True
        except wa.WhatsAppError as exc:
            log.warning("WhatsApp an Betrieb %s fehlgeschlagen, nehme Mail: %s", b.id, exc)
    if not n.email_subject:
        return False
    if send_email(session, b, n.email_subject, n.email_body):
        n.channel = "email"
        return True
    return False


def _deliver_whatsapp(b: Business, n: Notification, now: datetime) -> bool:
    to = b.whatsapp_number
    if wa.in_window(b, now):
        text = f"*{n.title}*\n\n{n.text}" if n.title else n.text
        if n.cta:
            wa.send_cta(to, text, n.cta[0], n.cta[1], n.footer)
            if n.buttons:
                wa.send_buttons(to, n.buttons_text or "Erledigt?", n.buttons)
            return True
        if len(text) > wa.BODY_MAX and n.buttons:
            # Knopf-Nachrichten sind auf 1024 Zeichen begrenzt: vollständigen Text vorweg, dann kurz die Knöpfe
            wa.send_text(to, text)
            wa.send_buttons(to, f"*{n.title or 'Deine Entscheidung'}*\n\nDer vollständige Text steht direkt darüber.", n.buttons, n.footer)
        else:
            wa.send_buttons(to, text, n.buttons, n.footer)
        return True
    if n.template is None:
        return False
    name = settings.whatsapp_templates.get(n.template)
    if not name:
        return False
    payloads = n.template_buttons if n.template_buttons is not None else [p for _, p in n.buttons]
    wa.send_template(to, name, n.template_params, payloads, url_param=n.template_url)
    return True


def send_email(session: Session, business: Business, subject: str, body: str) -> bool:
    """Mail an den Betrieb in sein eigenes Postfach, über sein eigenes SMTP.

    Ist das Postfach gestört (Verbindungs-Wächter), wird es gar nicht erst probiert. In dem Fall oder wenn es
    fehlschlägt geht die Mail über den Locadas-Systemversand (SYSTEM_SMTP_*), falls eingerichtet."""
    from ..engine import accounts as acc  # spät, vermeidet Import-Kreis

    account = next((a for a in business.accounts if a.status.value != "disconnected"), None)
    if account is None:
        return send_system_email(business.owner_email, business.owner_name or business.name, subject, body)
    if not acc.is_down(account):
        msg = build_message(
            from_name="Locadas Nachfasser",
            from_addr=account.email,
            to_name=business.owner_name or business.name,
            to_addr=account.email,
            subject=subject,
            body=body,
        )
        try:
            acc.smtp_for(account).send(msg)
            return True
        except (MailAuthError, MailConnectError) as exc:
            acc.record_failure(session, account, exc)
    return send_system_email(account.email or business.email, business.owner_name or business.name, subject, body)


def send_system_email(to_addr: str, to_name: str, subject: str, body: str) -> bool:
    """Versand über Locadas selbst, nur wenn SYSTEM_SMTP_HOST gesetzt ist (z. B. „Postfach nicht verbunden“)."""
    if not settings.system_smtp_host or not to_addr:
        return False
    from email.utils import parseaddr

    from ..mail.smtp import SmtpClient

    from_name, from_addr = parseaddr(settings.system_from)
    msg = build_message(from_name=from_name or "Locadas", from_addr=from_addr or "info@locadas.de", to_name=to_name, to_addr=to_addr, subject=subject, body=body)
    try:
        SmtpClient(
            settings.system_smtp_host, settings.system_smtp_port, settings.system_smtp_port == 465,
            settings.system_smtp_user or "", settings.system_smtp_password or "",
        ).send(msg)
        return True
    except (MailAuthError, MailConnectError) as exc:
        log.warning("Systemversand an %s fehlgeschlagen: %s", to_addr, exc)
        return False
