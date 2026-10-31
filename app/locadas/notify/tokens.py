"""Signierte Knopf-Payloads und Links für den WhatsApp-Sekretär.

Ein Knopf trägt „aktion:id-id“ plus Zeitstempel und Signatur (itsdangerous, SECRET_KEY).
So kann niemand per gefälschtem Webhook fremde Dokumente schließen.
"""
from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner, URLSafeTimedSerializer

from ..config import settings

BUTTON_MAX_AGE = 60 * 60 * 24 * 60   # Knöpfe gelten 60 Tage
DOC_LINK_MAX_AGE = 60 * 60 * 24 * 30  # PDF-Link für den Betrieb 30 Tage

ACTIONS = {
    "ok",       # „Passt“, nur Bestätigung
    "stop",     # Kunde hat sich gemeldet (z. B. angerufen): nicht mehr nachfassen
    "gone",     # wie stop, aus einer Freigabe heraus (danach kommt die nächste Freigabe)
    "send",     # Freigabe: senden
    "skip",     # Freigabe: diesmal nicht
    "settle",   # Vorgang von vor dem Start: schon bezahlt / erledigt
    "won", "lost", "paid", "unpaid",  # Abschlussfrage
    "call",     # Kontaktdaten zum Anrufen schicken
    "done",     # erledigt, nur abhaken
    "reopen",   # doch weiter nachfassen
    "pick",     # Vorschau mit mehreren Kunden: wer hat angerufen?
}


def _signer() -> TimestampSigner:
    return TimestampSigner(settings.secret_key, salt="locadas-wa-button")


def payload(action: str, *ids: int) -> str:
    if action not in ACTIONS:
        raise ValueError(f"Unbekannte Aktion {action}")
    raw = action + (":" + "-".join(str(int(i)) for i in ids) if ids else "")
    return _signer().sign(raw).decode()


def parse(value: str) -> tuple[str, list[int]] | None:
    """Payload prüfen; None bei falscher Signatur, abgelaufen oder unbekannter Aktion."""
    if not value:
        return None
    try:
        raw = _signer().unsign(value, max_age=BUTTON_MAX_AGE).decode()
    except (BadSignature, SignatureExpired):
        return None
    action, _, rest = raw.partition(":")
    if action not in ACTIONS:
        return None
    try:
        ids = [int(x) for x in rest.split("-") if x]
    except ValueError:
        return None
    return action, ids


def button(label: str, action: str, *ids: int) -> tuple[str, str]:
    """(Beschriftung höchstens 20 Zeichen, signierte Aktion)."""
    return label[:20], payload(action, *ids)


def _doc_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="locadas-wa-doc")


def doc_link(doc_id: int) -> str:
    """Link aufs PDF für den Betrieb selbst (zählt nicht als Öffnen durch den Kunden)."""
    return f"{settings.base_url}/wa/d/{_doc_serializer().dumps(doc_id)}"


def doc_id_from_link(token: str) -> int | None:
    try:
        return int(_doc_serializer().loads(token, max_age=DOC_LINK_MAX_AGE))
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None
