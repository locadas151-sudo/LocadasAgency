"""Datenmodell (SQLAlchemy 2.0, SQLite per Default)."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from sqlalchemy.types import TypeDecorator

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Speichert immer UTC (naiv in SQLite) und liefert immer tz-aware UTC zurück."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class DocKind(str, enum.Enum):
    OFFER = "offer"      # Angebot
    INVOICE = "invoice"  # Rechnung


REPLY_INTENT_LABEL = {
    "accept": "Zusage",
    "decline": "Absage",
    "question": "Rückfrage",
    "later": "Meldet sich später",
    "payment_done": "Hat bezahlt",
    "complaint": "Beschwerde",
    "other": "Antwort",
}


class DocStatus(str, enum.Enum):
    OPEN = "open"            # wird nachgefasst
    REPLIED = "replied"      # Kunde hat geantwortet, Nachfassen gestoppt
    WON = "won"              # Auftrag bekommen (Angebot)
    LOST = "lost"            # Absage (Angebot)
    PAID = "paid"            # bezahlt (Rechnung)
    DONE = "done"            # alle Nachfass-Schritte durch, keine Antwort
    IGNORED = "ignored"      # vom Betrieb ausgeschlossen
    BOUNCED = "bounced"      # Adresse unzustellbar


class FollowUpStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_TAP = "awaiting_tap"  # WhatsApp-Nachfassen: Betrieb schickt selbst mit einem Tipp, wir warten auf „Gesendet“
    SENDING = "sending"      # gerade beim SMTP-Versand (atomar beansprucht, verhindert Doppelversand)
    SENT = "sent"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AccountStatus(str, enum.Enum):
    ACTIVE = "active"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    owner_name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    signature: Mapped[str] = mapped_column(Text, default="")
    phone: Mapped[str] = mapped_column(String(60), default="")
    # Steuerung
    followup_offers: Mapped[bool] = mapped_column(Boolean, default=True)
    followup_invoices: Mapped[bool] = mapped_column(Boolean, default=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_opened: Mapped[bool] = mapped_column(Boolean, default=True)
    include_view_link: Mapped[bool] = mapped_column(Boolean, default=True)
    # Dokument in Nachfass-Mails: "link" (mit Öffnen-Info) oder "attachment" (PDF erneut anhängen, kein Tracking)
    doc_delivery: Mapped[str] = mapped_column(String(20), default="link", server_default="link")
    # Ton der Zahlungserinnerungen: "freundlich" | "sachlich" | "bestimmt" (Firmenkunden mindestens sachlich)
    tone: Mapped[str] = mapped_column(String(20), default="freundlich", server_default="freundlich")
    # Personalisierte Nachfass-Texte im Stil des Betriebs
    personalize: Mapped[bool] = mapped_column(Boolean, default=True)
    style_json: Mapped[str] = mapped_column(Text, default="")          # abgeleitetes Stilprofil
    style_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    style_samples_json: Mapped[str] = mapped_column(Text, default="")  # gekürzte Mailtexte, nur bis zur nächsten Ableitung
    approvals_done: Mapped[int] = mapped_column(Integer, default=0)
    # WhatsApp-Sekretär: Nummer in E.164 ohne „+“ (49171…), nur mit ausdrücklichem Opt-in
    whatsapp_number: Mapped[str] = mapped_column(String(20), default="", server_default="", index=True)
    whatsapp_opt_in: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    whatsapp_last_inbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)  # 24h-Fenster
    whatsapp_context: Mapped[str] = mapped_column(Text, default="", server_default="")  # JSON: nummerierte Liste der letzten Vorschau u. ä.
    auto_mode: Mapped[bool] = mapped_column(Boolean, default=False)  # alt, nicht mehr benutzt (siehe review_mode)
    # Freigaben: "auto" = Angebote laufen automatisch mit Morgen-Vorschau, "all" = jede Nachfass-Mail vorher freigeben.
    # Rechnungen fragen immer erst „Noch offen?“, Angebote über approval_threshold immer mit Freigabe.
    review_mode: Mapped[str] = mapped_column(String(10), default="auto", server_default="auto")
    approval_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)  # None = settings.approval_threshold
    avv_accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Kundenportal: Link zur Google-Bewertung (jeder Kunde bekommt denselben Link, siehe engine/customers.py)
    google_review_url: Mapped[str] = mapped_column(String(500), default="", server_default="")
    review_hint_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)  # einmaliger Tipp „Bewertungslink hinterlegen“
    # Nachfass-Kanal: "mail" (Standard), "whatsapp_tap" (WhatsApp mit einem Tipp, wenn Kunden-Handy bekannt, sonst Mail),
    # "both" (Mail automatisch plus WhatsApp-Vorschlag). Siehe engine/taps.py
    followup_channel: Mapped[str] = mapped_column(String(20), default="mail", server_default="mail")
    # Paket: "free" | "pro" (WhatsApp über die eigene Nummer braucht "pro", siehe channels/usage.py)
    plan: Mapped[str] = mapped_column(String(10), default="free", server_default="free")
    # Monatsbilanz (engine/summary.py): letzter Monat, für den sie raus ist bzw. ohne Aktivität übersprungen wurde ("2026-10")
    summary_sent_for: Mapped[str] = mapped_column(String(7), default="", server_default="")
    # "connected" = Postfach verbunden (Standard, auch für alle bestehenden), "trial" = Testen ohne Postfach (engine/trial.py)
    mode: Mapped[str] = mapped_column(String(12), default="connected", server_default="connected")
    # Persönliche Weiterleitungsadresse <slug>-<6 Zeichen>@INBOUND_DOMAIN (eindeutig, Index ux_business_inbound)
    inbound_address: Mapped[str | None] = mapped_column(String(320), nullable=True)
    trial_active_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)   # letzte Aktivität (Löschen nach 60 Tagen)
    trial_aha_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)      # erste „Erkannt“-Nachricht
    upgrade_prompt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True) # „Soll das künftig automatisch laufen?“ (einmal)
    trial_limit_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)    # Limit-Hinweis zuletzt
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # WhatsApp-Einwilligung (Meta verlangt ein klares Opt-in): wann und welche Fassung des Einwilligungstexts
    wa_optin_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    wa_optin_text: Mapped[str] = mapped_column(String(40), default="", server_default="")
    wa_optout_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)  # „STOP“ im Chat
    # Herkunft der Anmeldung: "testen" (/testen) | "verbinden" (Postfach direkt) | "" (unbekannt, alt)
    signup_source: Mapped[str] = mapped_column(String(12), default="", server_default="")
    # Admin-Cockpit (web/admin_routes.py): Notiz aus dem Onboarding-Call und wann er war
    admin_note: Mapped[str] = mapped_column(Text, default="", server_default="")
    onboarded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    accounts: Mapped[list["MailAccount"]] = relationship(back_populates="business", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="business", cascade="all, delete-orphan")
    events: Mapped[list["Event"]] = relationship(back_populates="business", cascade="all, delete-orphan")
    customers: Mapped[list["Customer"]] = relationship(back_populates="business", cascade="all, delete-orphan")

    __table_args__ = (Index("ux_business_inbound", "inbound_address", unique=True),)

    @property
    def is_trial(self) -> bool:
        return self.mode == "trial"

    @property
    def owner_email(self) -> str:
        """Echte Mailadresse des Betriebs, leer bei Testern ohne Mail (Platzhalter *.invalid)."""
        e = (self.email or "").strip().lower()
        return "" if not e or e.endswith(".invalid") else e

    @property
    def needs_approval(self) -> bool:
        """Betrieb will jede Nachfass-Mail selbst freigeben."""
        return self.review_mode == "all"

    @property
    def threshold(self) -> float:
        return self.approval_threshold if self.approval_threshold is not None else settings.approval_threshold


class MailAccount(Base):
    __tablename__ = "mail_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    email: Mapped[str] = mapped_column(String(320), index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    auth_type: Mapped[str] = mapped_column(String(20), default="password")  # password | oauth
    username: Mapped[str] = mapped_column(String(320))
    secret_enc: Mapped[str] = mapped_column(Text)  # Passwort oder OAuth-Refresh-Token, verschlüsselt
    imap_host: Mapped[str] = mapped_column(String(200))
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    imap_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    smtp_host: Mapped[str] = mapped_column(String(200))
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_ssl: Mapped[bool] = mapped_column(Boolean, default=False)  # True = SMTPS 465, False = STARTTLS
    sent_folder: Mapped[str] = mapped_column(String(200), default="Sent")
    inbox_folder: Mapped[str] = mapped_column(String(200), default="INBOX")
    sent_uidvalidity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_last_uid: Mapped[int] = mapped_column(Integer, default=0)
    inbox_uidvalidity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inbox_last_uid: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[AccountStatus] = mapped_column(Enum(AccountStatus), default=AccountStatus.ACTIVE)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Verbindungs-Wächter (engine/accounts.py): Fehler in Folge (höchstens einer je Durchlauf), seit wann gestört,
    # wann der Betrieb zuletzt Bescheid bekam (None = in dieser Störung noch nicht)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_failure_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    error_since: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    error_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    business: Mapped[Business] = relationship(back_populates="accounts")
    seen: Mapped[list["SeenMessage"]] = relationship(cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    # None = ohne Postfach eingegangen (Testen per Weiterleitung oder WhatsApp, siehe engine/inbound_mail.py)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("mail_accounts.id"), index=True, nullable=True)
    kind: Mapped[DocKind] = mapped_column(Enum(DocKind))
    status: Mapped[DocStatus] = mapped_column(Enum(DocStatus), default=DocStatus.OPEN, index=True)
    # Ursprungsmail
    message_id: Mapped[str] = mapped_column(String(500), default="", index=True)
    imap_uid: Mapped[int] = mapped_column(Integer, default=0)
    subject: Mapped[str] = mapped_column(String(500), default="")
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # Kunde
    customer_name: Mapped[str] = mapped_column(String(200), default="")
    customer_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    # Telefon des Kunden aus PDF bzw. Mail-Signatur (E.164 ohne „+“), Handy nur bei WhatsApp-fähiger Mobilvorwahl
    customer_phone: Mapped[str] = mapped_column(String(20), default="", server_default="")
    customer_mobile: Mapped[str] = mapped_column(String(20), default="", server_default="")
    # Kanal nur für dieses Dokument: "" = wie im Betrieb eingestellt, sonst "mail" | "whatsapp_tap" | "both"
    followup_channel: Mapped[str] = mapped_column(String(20), default="", server_default="")
    # CC der Ursprungsmail (JSON-Liste [[Name, Adresse], ...], ohne eigene Adresse), Nachfass-Mails behalten sie
    cc_json: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Dokument
    document_number: Mapped[str] = mapped_column(String(100), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    amount_gross: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    due_date: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    pdf_path: Mapped[str] = mapped_column(String(500), default="")
    pdf_filename: Mapped[str] = mapped_column(String(300), default="")
    # Begriff aus dem Dokument selbst („Offerte“, „Kostenvoranschlag“, „Abschlagsrechnung“ …), leer = Angebot/Rechnung
    doc_term: Mapped[str] = mapped_column(String(40), default="", server_default="")
    # Bankverbindung aus der Rechnung (IBAN mit Prüfsumme geprüft)
    iban: Mapped[str] = mapped_column(String(40), default="", server_default="")
    bic: Mapped[str] = mapped_column(String(20), default="", server_default="")
    bank_name: Mapped[str] = mapped_column(String(100), default="", server_default="")
    account_holder: Mapped[str] = mapped_column(String(200), default="", server_default="")
    extraction_source: Mapped[str] = mapped_column(String(20), default="")  # claude | heuristic
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # Nachfassen
    followups_sent: Mapped[int] = mapped_column(Integer, default=0)
    next_followup_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_customer_reply_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    close_reason: Mapped[str] = mapped_column(String(200), default="")
    # Kundenantwort, von Claude eingeordnet (Mailtext selbst wird nicht gespeichert)
    reply_intent: Mapped[str] = mapped_column(String(20), default="", server_default="")
    reply_summary: Mapped[str] = mapped_column(String(300), default="", server_default="")
    # Abschlussfrage an den Betrieb („Auftrag bekommen?“ / „Bezahlt?“)
    outcome_token: Mapped[str] = mapped_column(String(64), default=lambda: __import__("secrets").token_urlsafe(18), index=True)
    outcome_asked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    outcome_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)  # per Link beantwortet (nur einmal)
    # Betrieb hat selbst an den Kunden geschrieben (letztes Mal), Abwesenheit des Kunden bis
    owner_contact_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    away_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Beim ersten Scan gefundene ältere Dokumente: Nachfassen nur mit Freigabe (könnte schon erledigt sein)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    # Kunde hat den Eingang automatisch bestätigt (z. B. Rechnungseingang eines GU)
    ack_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Lese-Tracking über den Link in den Nachfass-Mails
    view_token: Mapped[str] = mapped_column(String(64), default=lambda: __import__("secrets").token_urlsafe(18), index=True)
    first_opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    open_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")
    # Herkunft: mailbox (Gesendet-Ordner) | forward | cc | forward_unverified (Absender ist nicht der Betrieb) | whatsapp
    source: Mapped[str] = mapped_column(String(24), default="mailbox", server_default="mailbox")
    inbound_from: Mapped[str] = mapped_column(String(320), default="", server_default="")   # Absender der Weiterleitung
    inbound_auth: Mapped[str] = mapped_column(Text, default="", server_default="")          # Authentication-Results des Anbieters
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    business: Mapped[Business] = relationship(back_populates="documents")
    account: Mapped[MailAccount | None] = relationship()
    followups: Mapped[list["FollowUp"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="FollowUp.step"
    )

    @property
    def is_open(self) -> bool:
        return self.status == DocStatus.OPEN

    @property
    def kind_label(self) -> str:
        return "Angebot" if self.kind == DocKind.OFFER else "Rechnung"

    @property
    def term(self) -> str:
        """Begriff, den der Kunde aus dem Dokument kennt."""
        return self.doc_term or self.kind_label

    @property
    def cc(self) -> list[tuple[str, str]]:
        import json

        try:
            data = json.loads(self.cc_json) if self.cc_json else []
        except (ValueError, TypeError):
            return []
        return [(str(n), str(a)) for n, a in data if a] if isinstance(data, list) else []

    @property
    def reply_intent_label(self) -> str:
        return REPLY_INTENT_LABEL.get(self.reply_intent, "Antwort") if self.reply_intent else ""


def _portal_token() -> str:
    import secrets

    return secrets.token_urlsafe(24)  # 192 Bit, nicht erratbar


class Customer(Base):
    """Endkunde eines Betriebs (eine Mailadresse). Dokumente hängen über (business_id, customer_email) dran.

    Das Kundenportal /k/{portal_token} zeigt nur Dokumente dieses Kunden, ohne Login.
    """

    __tablename__ = "customers"
    __table_args__ = (Index("ux_customer_business_email", "business_id", "email", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    email: Mapped[str] = mapped_column(String(320))  # immer kleingeschrieben
    name: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(20), default="", server_default="")   # E.164 ohne „+“
    mobile: Mapped[str] = mapped_column(String(20), default="", server_default="")  # nur Mobilnummern (WhatsApp)
    portal_token: Mapped[str] = mapped_column(String(64), default=_portal_token, unique=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)         # Auftrag fertig gemeldet
    completion_asked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)  # Betrieb einmal gefragt „Ist der Auftrag fertig?“
    review_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)  # höchstens einmal je Kunde
    review_clicked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    feedback_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    feedback_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    business: Mapped[Business] = relationship(back_populates="customers")


class FollowUp(Base):
    __tablename__ = "followups"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    step: Mapped[int] = mapped_column(Integer)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    status: Mapped[FollowUpStatus] = mapped_column(Enum(FollowUpStatus), default=FollowUpStatus.SCHEDULED, index=True)
    subject: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    approval_token: Mapped[str] = mapped_column(String(64), default="", index=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    message_id: Mapped[str] = mapped_column(String(500), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    announced_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)  # in der Morgen-Vorschau angekündigt
    send_after: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)    # in der Vorschau genannte Versandzeit
    requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)  # Freigabe angefragt (Link-Ablauf)
    # WhatsApp mit einem Tipp (engine/taps.py): Text für den Chat, eigener Token für /t/ (Weiterleitung) und /w/ (Aktionen)
    sent_via: Mapped[str] = mapped_column(String(20), default="", server_default="")   # mail | whatsapp_tap | mail+whatsapp_tap
    wa_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    tap_token: Mapped[str] = mapped_column(String(64), default="", server_default="", index=True)
    tap_state: Mapped[str] = mapped_column(String(12), default="", server_default="")  # "" | open | sent | replied | skipped | expired | cancelled
    tap_notice: Mapped[str] = mapped_column(String(12), default="", server_default="")  # whatsapp | email | dashboard
    tap_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    tapped_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)   # Link „In WhatsApp öffnen“ geöffnet
    tap_reminded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    wa_read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)    # per WhatsApp (eigene Nummer) gelesen, echte Lesebestätigung
    # Art des Tipps: "" bzw. "wa" = wa.me, "mailto" = fertige Mail im eigenen Mailprogramm, "manual" = Text kopieren (Kontakt fehlt)
    tap_kind: Mapped[str] = mapped_column(String(10), default="", server_default="")

    document: Mapped[Document] = relationship(back_populates="followups")


class Event(Base):
    """Aktivitätslog für das Dashboard."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)

    business: Mapped[Business] = relationship(back_populates="events")


class WhatsAppInbound(Base):
    """Bereits verarbeitete WhatsApp-Nachrichten (Meta schickt Webhooks bei Fehlern mehrfach)."""

    __tablename__ = "whatsapp_inbound"

    id: Mapped[int] = mapped_column(primary_key=True)
    wamid: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    business_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class InboundMail(Base):
    """Eingegangene Mails an Weiterleitungsadressen (Rate-Limit, Dubletten, Nachvollziehbarkeit). Kein Mailtext."""

    __tablename__ = "inbound_mails"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    address: Mapped[str] = mapped_column(String(320), default="", index=True)
    message_id: Mapped[str] = mapped_column(String(500), default="", index=True)
    sender: Mapped[str] = mapped_column(String(320), default="")
    result: Mapped[str] = mapped_column(String(40), default="")
    document_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auth_results: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class SeenMessage(Base):
    """Verhindert doppelte Verarbeitung derselben Mail (Message-ID je Account)."""

    __tablename__ = "seen_messages"
    __table_args__ = (Index("ux_seen_account_message", "account_id", "message_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("mail_accounts.id"), index=True)
    message_id: Mapped[str] = mapped_column(String(500), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# --- WhatsApp über die eigene Nummer des Betriebs (Stufe 2, Coexistence) ----------------
# Alles hier wird nur benutzt, wenn channels/config.py es freischaltet. Logik in locadas/channels/.

class WhatsAppConnection(Base):
    """Per Embedded Signup verbundenes WhatsApp-Business-Konto des Betriebs (eine Nummer je Betrieb)."""

    __tablename__ = "wa_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), unique=True, index=True)
    waba_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    phone_number_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    display_phone: Mapped[str] = mapped_column(String(40), default="")
    verified_name: Mapped[str] = mapped_column(String(200), default="")
    business_token_enc: Mapped[str] = mapped_column(Text, default="")  # Business Integration System User Token, verschlüsselt
    register_pin_enc: Mapped[str] = mapped_column(Text, default="")    # nur ohne Coexistence (neue Nummer)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | active | error | disconnected
    coexistence: Mapped[bool] = mapped_column(Boolean, default=True)
    connected_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    quality_rating: Mapped[str] = mapped_column(String(20), default="")
    messaging_limit_tier: Mapped[str] = mapped_column(String(40), default="")
    templates_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    history_request_id: Mapped[str] = mapped_column(String(100), default="")
    contacts_request_id: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class WaTemplate(Base):
    """Unsere Nachfass-Vorlagen im WhatsApp-Konto des Betriebs, mit Status und Kategorie laut Meta."""

    __tablename__ = "wa_templates"
    __table_args__ = (Index("ux_wa_template_conn_name", "connection_id", "name", "language", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("wa_connections.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    language: Mapped[str] = mapped_column(String(10), default="de")
    category: Mapped[str] = mapped_column(String(20), default="")   # UTILITY | MARKETING (Meta kann umstufen)
    status: Mapped[str] = mapped_column(String(30), default="")     # APPROVED | PENDING | REJECTED | PAUSED | DISABLED | ERROR
    meta_id: Mapped[str] = mapped_column(String(40), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class WaContact(Base):
    """Endkunde als WhatsApp-Kontakt eines Betriebs: Einwilligung, Abmeldung, 24h-Fenster."""

    __tablename__ = "wa_contacts"
    __table_args__ = (Index("ux_wa_contact_business_phone", "business_id", "phone", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    phone: Mapped[str] = mapped_column(String(20))                    # E.164 ohne „+“
    customer_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    wa_opt_in_source: Mapped[str] = mapped_column(String(40), default="")  # customer_initiated_chat | offer_form | manual
    opt_in_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    opted_out_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)  # öffnet das 24h-Fenster
    history_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)  # im Chatverlauf der App gesehen (nur Hinweis)
    undeliverable_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class WaMessage(Base):
    """Von uns über die Nummer des Betriebs verschickte Nachricht (für Lesebestätigung und Abrechnung)."""

    __tablename__ = "wa_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    followup_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    document_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    wamid: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    to_phone: Mapped[str] = mapped_column(String(20), default="")
    kind: Mapped[str] = mapped_column(String(20), default="template")  # template | text
    template_key: Mapped[str] = mapped_column(String(40), default="")
    category: Mapped[str] = mapped_column(String(20), default="")      # utility | marketing | service (wie gezählt)
    status: Mapped[str] = mapped_column(String(20), default="accepted")  # accepted | sent | delivered | read | failed
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class WaUsage(Base):
    """Gesendete Nachrichten je Betrieb, Monat (lokale Zeit) und Kategorie."""

    __tablename__ = "wa_usage"
    __table_args__ = (Index("ux_wa_usage", "business_id", "month", "category", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    month: Mapped[str] = mapped_column(String(7))      # 2026-11
    category: Mapped[str] = mapped_column(String(20))  # utility | marketing | service
    count: Mapped[int] = mapped_column(Integer, default=0)


# --- Betrieb von Locadas (locadas/ops/) ----------------------------------------------

class OpsLog(Base):
    """Warnungen und Fehler aus dem Log (Ringpuffer, siehe ops/logstore.py) für den Fehler-Feed im Admin-Cockpit."""

    __tablename__ = "ops_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10), default="WARNING")
    logger: Mapped[str] = mapped_column(String(120), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    business_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)


class ClaudeUsage(Base):
    """Claude-Aufrufe je Tag (lokal), Zweck und Modell, mit Tokens für die Kostenschätzung (ops/usage.py)."""

    __tablename__ = "claude_usage"
    __table_args__ = (Index("ux_claude_usage", "day", "purpose", "model", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[str] = mapped_column(String(10))       # 2026-11-01
    purpose: Mapped[str] = mapped_column(String(30))   # extract | vision | match | style | check
    model: Mapped[str] = mapped_column(String(80))
    calls: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)


class NotificationLog(Base):
    """Jede Benachrichtigung an einen Betrieb oder an den Admin: Kanal, Art der WhatsApp-Nachricht, Erfolg."""

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    business_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # None = an den Admin
    kind: Mapped[str] = mapped_column(String(20), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    channel: Mapped[str] = mapped_column(String(10), default="")   # whatsapp | email | "" (nicht zugestellt)
    wa_mode: Mapped[str] = mapped_column(String(10), default="")   # session | template | ""
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")


class OpsState(Base):
    """Kleiner Schlüssel-Wert-Speicher (Wasserstände der Alarme, letzte Sicherung, letzte Admin-Nachricht)."""

    __tablename__ = "ops_state"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


# --- Engine / Session -------------------------------------------------------

SQLITE_BUSY_MS = 30_000  # Schreibsperre: so lange warten, bevor „database is locked“ (parallele Scans)

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine():
    global _engine
    if _engine is None:
        url = settings.sqlalchemy_url
        kwargs: dict = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": SQLITE_BUSY_MS / 1000}
            if ":memory:" not in url:
                kwargs.update(pool_size=12, max_overflow=24)  # Scan-Worker plus Web-Threads
        _engine = create_engine(url, future=True, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _sqlite_pragma(dbapi_conn, _):  # pragma: no cover
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_MS}")
                cur.execute("PRAGMA synchronous=NORMAL")  # mit WAL sicher und schneller
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
    return _engine


def session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


def new_session() -> Session:
    return session_factory()()


def init_db() -> None:
    Base.metadata.create_all(get_engine())
    _add_missing_columns()
    _ensure_unique_seen()
    _documents_account_nullable()
    _ensure_indexes()


def _ensure_indexes() -> None:
    """Indizes auf neuen Spalten bestehender Tabellen (create_all legt sie nur bei neuen Tabellen an)."""
    from sqlalchemy import text

    with get_engine().begin() as conn:
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_business_inbound ON businesses (inbound_address)"))
        # Herkunft der Anmeldung für ältere Betriebe nachtragen (Funnel im Admin-Cockpit)
        conn.execute(text(
            "UPDATE businesses SET signup_source='testen' WHERE signup_source='' AND id IN "
            "(SELECT business_id FROM events WHERE kind='trial' AND text LIKE 'Testzugang angelegt%')"
        ))
        conn.execute(text("UPDATE businesses SET signup_source='verbinden' WHERE signup_source='' AND mode='connected'"))


def _documents_account_nullable() -> None:
    """Bestehende SQLite-Datenbanken: documents.account_id war NOT NULL. Tabelle einmal neu aufbauen (Daten bleiben).

    Vorgehen wie in der SQLite-Doku empfohlen: neue Tabelle anlegen, kopieren, alte löschen, umbenennen.
    Fremdschlüssel sind dabei kurz aus, damit followups/events nicht auf die alte Tabelle umgebogen werden."""
    eng = get_engine()
    if eng.dialect.name != "sqlite":
        return
    from sqlalchemy import inspect
    from sqlalchemy.schema import CreateTable

    cols = {c["name"]: c for c in inspect(eng).get_columns("documents")}
    if "account_id" not in cols or cols["account_id"].get("nullable", True):
        return
    table = Document.__table__
    ddl = str(CreateTable(table).compile(eng)).strip()
    ddl = ddl.replace("CREATE TABLE documents", "CREATE TABLE documents_new", 1)
    names = ", ".join(f'"{c}"' for c in cols if c in table.c)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")
        raw.commit()
        cur.execute("BEGIN")
        cur.execute("DROP TABLE IF EXISTS documents_new")
        cur.execute(ddl)
        cur.execute(f"INSERT INTO documents_new ({names}) SELECT {names} FROM documents")
        cur.execute("DROP TABLE documents")
        cur.execute("ALTER TABLE documents_new RENAME TO documents")
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        try:
            raw.cursor().execute("PRAGMA foreign_keys=ON")
        finally:
            raw.close()
    with eng.begin() as conn:
        for idx in table.indexes:
            idx.create(conn, checkfirst=True)


def _ensure_unique_seen() -> None:
    """Bestehende Datenbanken: Dubletten in seen_messages entfernen, dann den eindeutigen Index anlegen."""
    from sqlalchemy import text

    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text(
            "DELETE FROM seen_messages WHERE id NOT IN (SELECT MIN(id) FROM seen_messages GROUP BY account_id, message_id)"
        ))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_seen_account_message ON seen_messages (account_id, message_id)"))


def _add_missing_columns() -> None:
    """Mini-Migration für SQLite: neue Spalten an bestehende Tabellen hängen (create_all tut das nicht)."""
    eng = get_engine()
    if eng.dialect.name != "sqlite":
        return
    from sqlalchemy import inspect, text

    insp = inspect(eng)
    with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have or col.primary_key:
                    continue
                default = col.default.arg if col.default is not None and col.default.is_scalar else None
                ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col.type.compile(eng.dialect)}'
                if isinstance(default, bool):
                    ddl += f" NOT NULL DEFAULT {int(default)}"
                elif isinstance(default, (int, float)):
                    ddl += f" NOT NULL DEFAULT {default}"
                elif isinstance(default, str):
                    ddl += " NOT NULL DEFAULT '" + default.replace("'", "''") + "'"
                conn.execute(text(ddl))


def reset_engine_for_tests(url: str) -> None:
    """Nur für Tests: Engine auf eine andere Datenbank umbiegen."""
    global _engine, _SessionLocal
    settings.database_url = url
    _engine = None
    _SessionLocal = None
    init_db()


def log_event(session: Session, business_id: int, kind: str, text: str, document_id: int | None = None) -> Event:
    ev = Event(business_id=business_id, document_id=document_id, kind=kind, text=text)
    session.add(ev)
    return ev
