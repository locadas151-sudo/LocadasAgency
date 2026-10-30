"""Login ohne Passwort: Session-Cookie plus Magic-Link per Mail (über das eigene Postfach des Betriebs)."""
from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import Business

COOKIE = "locadas_session"
_SESSION_MAX_AGE = 60 * 60 * 24 * 90   # 90 Tage
_LINK_MAX_AGE = 60 * 60 * 2            # 2 Stunden


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=salt)


def session_token(business_id: int) -> str:
    return _serializer("session").dumps({"b": business_id})


def business_from_token(session: Session, token: str | None) -> Business | None:
    if not token:
        return None
    try:
        data = _serializer("session").loads(token, max_age=_SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return session.get(Business, int(data.get("b", 0)))


def login_link_token(business_id: int) -> str:
    return _serializer("login").dumps({"b": business_id})


def business_from_login_token(session: Session, token: str) -> Business | None:
    try:
        data = _serializer("login").loads(token, max_age=_LINK_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return session.get(Business, int(data.get("b", 0)))


def business_by_email(session: Session, email: str) -> Business | None:
    return session.scalar(select(Business).where(Business.email == email.strip().lower()))
