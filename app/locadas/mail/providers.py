"""Anbieter-Voreinstellungen: aus der Mailadresse werden IMAP- und SMTP-Server abgeleitet.

Der Handwerker gibt nur Adresse und Passwort ein. Bei Gmail muss es ein App-Passwort
sein (bei aktivierter 2-Faktor-Authentifizierung), bei Outlook.com/Microsoft 365 ist
Passwort-Login per IMAP abgeschaltet, dort geht nur OAuth (vorbereitet in oauth.py).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    imap_host: str
    imap_port: int
    imap_ssl: bool
    smtp_host: str
    smtp_port: int
    smtp_ssl: bool  # True = SMTPS (465), False = STARTTLS
    sent_folders: tuple[str, ...]
    needs_app_password: bool = False
    oauth_only: bool = False
    hint: str = ""


GMAIL = Provider(
    "gmail", "Gmail", "imap.gmail.com", 993, True, "smtp.gmail.com", 465, True,
    ("[Gmail]/Gesendet", "[Gmail]/Sent Mail", "[Google Mail]/Gesendet", "[Gmail]/Sent"),
    needs_app_password=True,
    hint="Bei Gmail brauchst du ein App-Passwort (Google-Konto → Sicherheit → App-Passwörter). Dein normales Passwort funktioniert nicht.",
)
OUTLOOK = Provider(
    "outlook", "Outlook / Microsoft 365", "outlook.office365.com", 993, True, "smtp.office365.com", 587, False,
    ("Sent Items", "Gesendete Elemente", "Sent"),
    oauth_only=True,
    hint="Microsoft erlaubt bei Outlook.com und Microsoft 365 keinen Passwort-Login mehr. Die Verbindung läuft über „Mit Microsoft anmelden“.",
)
GMX = Provider(
    "gmx", "GMX", "imap.gmx.net", 993, True, "mail.gmx.net", 465, True, ("Gesendet", "Sent"),
    hint="Bei GMX muss unter E-Mail → Einstellungen → POP3/IMAP der IMAP-Zugriff eingeschaltet sein.",
)
WEBDE = Provider(
    "webde", "WEB.DE", "imap.web.de", 993, True, "smtp.web.de", 465, True, ("Gesendet", "Sent"),
    hint="Bei WEB.DE muss unter E-Mail → Einstellungen → POP3/IMAP der IMAP-Zugriff eingeschaltet sein.",
)
TONLINE = Provider(
    "tonline", "T-Online", "secureimap.t-online.de", 993, True, "securesmtp.t-online.de", 465, True,
    ("Gesendete Objekte", "Gesendet", "Sent"),
    hint="Bei T-Online brauchst du ein eigenes E-Mail-Passwort (Kundencenter → Passwörter → Passwort für E-Mail-Programme).",
)
IONOS = Provider(
    "ionos", "IONOS", "imap.ionos.de", 993, True, "smtp.ionos.de", 465, True, ("Gesendete Objekte", "Gesendet", "Sent"),
)
STRATO = Provider(
    "strato", "STRATO", "imap.strato.de", 993, True, "smtp.strato.de", 465, True, ("Gesendet", "Sent"),
)
ALLINKL = Provider(
    "allinkl", "ALL-INKL", "imap.all-inkl.com", 993, True, "smtp.all-inkl.com", 465, True, ("Sent", "Gesendet"),
)
HOSTEUROPE = Provider(
    "hosteurope", "Host Europe", "imap.hosteurope.de", 993, True, "smtp.hosteurope.de", 465, True, ("Sent", "Gesendet"),
)
POSTEO = Provider("posteo", "Posteo", "posteo.de", 993, True, "posteo.de", 465, True, ("Sent", "Gesendet"))
MAILBOX = Provider("mailbox", "mailbox.org", "imap.mailbox.org", 993, True, "smtp.mailbox.org", 465, True, ("Sent", "Gesendet"))
ICLOUD = Provider(
    "icloud", "iCloud", "imap.mail.me.com", 993, True, "smtp.mail.me.com", 587, False, ("Sent Messages", "Gesendet"),
    needs_app_password=True,
    hint="Bei iCloud brauchst du ein anwendungsspezifisches Passwort (appleid.apple.com → Anmeldung und Sicherheit).",
)
YAHOO = Provider(
    "yahoo", "Yahoo", "imap.mail.yahoo.com", 993, True, "smtp.mail.yahoo.com", 465, True, ("Sent",),
    needs_app_password=True, hint="Bei Yahoo brauchst du ein App-Passwort.",
)
FREENET = Provider("freenet", "freenet", "mx.freenet.de", 993, True, "mx.freenet.de", 465, True, ("Gesendet", "Sent"))

PROVIDERS: dict[str, Provider] = {
    p.key: p for p in [GMAIL, OUTLOOK, GMX, WEBDE, TONLINE, IONOS, STRATO, ALLINKL, HOSTEUROPE, POSTEO, MAILBOX, ICLOUD, YAHOO, FREENET]
}

_DOMAIN_MAP: dict[str, Provider] = {
    "gmail.com": GMAIL, "googlemail.com": GMAIL,
    "outlook.com": OUTLOOK, "outlook.de": OUTLOOK, "hotmail.com": OUTLOOK, "hotmail.de": OUTLOOK, "live.de": OUTLOOK, "live.com": OUTLOOK, "msn.com": OUTLOOK,
    "gmx.de": GMX, "gmx.net": GMX, "gmx.at": GMX, "gmx.ch": GMX,
    "web.de": WEBDE,
    "t-online.de": TONLINE, "magenta.de": TONLINE,
    "ionos.de": IONOS, "1und1.de": IONOS, "1and1.de": IONOS,
    "posteo.de": POSTEO, "mailbox.org": MAILBOX,
    "icloud.com": ICLOUD, "me.com": ICLOUD, "mac.com": ICLOUD,
    "yahoo.de": YAHOO, "yahoo.com": YAHOO, "ymail.com": YAHOO,
    "freenet.de": FREENET,
}

# Typische Gesendet-Ordnernamen für unbekannte Server (werden beim Verbinden durchprobiert)
COMMON_SENT_FOLDERS = (
    "Sent", "Gesendet", "Gesendete Objekte", "Gesendete Elemente", "Sent Items", "Sent Messages",
    "INBOX.Sent", "INBOX/Sent", "INBOX.Gesendet", "[Gmail]/Gesendet", "[Gmail]/Sent Mail",
)


def detect_provider(email: str) -> Provider | None:
    domain = email.rsplit("@", 1)[-1].lower().strip()
    return _DOMAIN_MAP.get(domain)


def guess_custom_provider(email: str) -> Provider:
    """Eigene Domain (z. B. info@mueller-shk.de): übliche Hostnamen raten, Login testet die Kandidaten."""
    domain = email.rsplit("@", 1)[-1].lower().strip()
    return Provider(
        "custom", domain, f"imap.{domain}", 993, True, f"smtp.{domain}", 465, True, COMMON_SENT_FOLDERS,
        hint="Eigene Domain: Wir suchen deinen Mailserver über die Domain-Einstellungen (Autoconfig, MX). Wenn es nicht klappt, trag die Server deines Anbieters ein (stehen in den Einstellungen deines Mailprogramms).",
    )
