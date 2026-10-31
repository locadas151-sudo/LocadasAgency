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
    auto_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    accounts: Mapped[list["MailAccount"]] = relationship(back_populates="business", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="business", cascade="all, delete-orphan")
    events: Mapped[list["Event"]] = relationship(back_populates="business", cascade="all, delete-orphan")

    @property
    def needs_approval(self) -> bool:
        return not self.auto_mode and self.approvals_done < settings.approvals_before_auto


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    business: Mapped[Business] = relationship(back_populates="accounts")
    seen: Mapped[list["SeenMessage"]] = relationship(cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("mail_accounts.id"), index=True)
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
    # Dokument
    document_number: Mapped[str] = mapped_column(String(100), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    amount_gross: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    due_date: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    pdf_path: Mapped[str] = mapped_column(String(500), default="")
    pdf_filename: Mapped[str] = mapped_column(String(300), default="")
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
    # Beim ersten Scan gefundene ältere Dokumente: Nachfassen nur mit Freigabe (könnte schon erledigt sein)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    # Lese-Tracking über den Link in den Nachfass-Mails
    view_token: Mapped[str] = mapped_column(String(64), default=lambda: __import__("secrets").token_urlsafe(18), index=True)
    first_opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    open_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    business: Mapped[Business] = relationship(back_populates="documents")
    account: Mapped[MailAccount] = relationship()
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
    def reply_intent_label(self) -> str:
        return REPLY_INTENT_LABEL.get(self.reply_intent, "Antwort") if self.reply_intent else ""


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


class SeenMessage(Base):
    """Verhindert doppelte Verarbeitung derselben Mail (Message-ID je Account)."""

    __tablename__ = "seen_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("mail_accounts.id"), index=True)
    message_id: Mapped[str] = mapped_column(String(500), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# --- Engine / Session -------------------------------------------------------

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine():
    global _engine
    if _engine is None:
        url = settings.sqlalchemy_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _sqlite_pragma(dbapi_conn, _):  # pragma: no cover
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
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
