"""Versandkanäle für Nachfass-Nachrichten. Stufe 2: WhatsApp über die eigene Nummer des Betriebs (Coexistence).

INTEGRATION (für die Nachfass-Engine, bewusst noch nicht verdrahtet)
-------------------------------------------------------------------
Ein einziger Aufruf, vor dem Mailversand. In ``locadas/engine/followups.py``:

1. ``process_due``: statt ::

       if send_followup(session, fu):

   so ::

       from ..channels import select_and_send
       result = select_and_send(session, fu, now)
       if result is None:              # WhatsApp nicht möglich → wie bisher per Mail
           result = send_followup(session, fu)
       if result:
           ...

   Die Prüfungen davor (Versandfenster, Tageslimit, Pause, Freigabe, Vorschau) bleiben wie sie sind.
   Per WhatsApp verschickte Schritte stehen danach auf SENT und zählen beim Tageslimit mit.

2. ``approve`` (freigegebene Nachfassung): dieselbe Reihenfolge vor ``send_followup``.

3. Stufe 1 (Knopf-Link für den Betrieb, Status AWAITING_TAP, ``followup_channel``): Ist Stufe 2 für den Betrieb
   aktiv (``channels.business_channel_active(session, business)`` ist True), sollte Stufe 1 keinen Tipp-Link
   anbieten, sondern direkt ``select_and_send`` nutzen. Für alle anderen bleibt Stufe 1 unverändert.

Rückgabewerte von ``select_and_send``:
- ``True``: per WhatsApp verschickt (Status SENT, Verlauf, Zählung erledigt).
- ``False``: nicht senden, auch nicht per Mail (Schritt ist schon in Arbeit oder erledigt).
- ``None``: WhatsApp kommt nicht in Frage (Stufe 2 aus, Betrieb nicht verbunden oder nicht im Pro-Paket,
  keine Handynummer, keine Einwilligung, abgemeldet, Vorlage nicht freigegeben, Fehler bei Meta) → Mail nehmen.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ..db import Business, FollowUp, FollowUpStatus, WaMessage, log_event, utcnow
from . import contacts, usage
from . import wa_business as wab
from . import wa_templates as tpl
from .config import wab_settings

log = logging.getLogger(__name__)

__all__ = ["select_and_send", "business_channel_active"]


def business_channel_active(session: Session, business: Business) -> bool:
    return bool(wab_settings.enabled and usage.plan_allows(business) and wab.active_connection(session, business.id) is not None)


def _free_text(doc, fu: FollowUp, now: datetime) -> str:
    """Text im 24h-Fenster: WhatsApp-Variante aus Stufe 1, falls vorhanden, sonst der Mailtext."""
    from ..engine import texts

    variant = getattr(texts, "whatsapp_body", None)
    if callable(variant):
        try:
            text = variant(doc, fu.step)
            if text:
                return str(text)
        except Exception as exc:  # nie am Text scheitern
            log.info("WhatsApp-Text aus Stufe 1 nicht nutzbar: %s", exc)
    return fu.body


def select_and_send(session: Session, followup: FollowUp, now: datetime | None = None) -> bool | None:
    """Nachfass-Schritt über die WhatsApp-Nummer des Betriebs schicken, wenn möglich. Siehe Modul-Doku."""
    from ..engine import followups as fe

    now = now or utcnow()
    if not wab_settings.enabled:
        return None
    doc = followup.document
    business = doc.business
    if not usage.plan_allows(business):
        return None
    conn = wab.active_connection(session, business.id)
    if conn is None:
        return None
    phone = contacts.customer_mobile(session, doc)
    if not phone:
        return None
    contact = contacts.get_or_create(session, business.id, phone, doc.customer_email, doc.customer_name)
    if contact.opted_out_at is not None or contact.undeliverable_at is not None:
        return None

    window = contacts.in_window(contact, now)
    template = None
    key = tpl.key_for(doc, followup.step)
    if not window:
        if not contacts.may_send_template(contact):
            return None  # ohne Einwilligung keine Vorlage (Meta-Regeln, DSGVO, UWG)
        from .onboarding import template_for

        template = template_for(session, conn, key)
        if template is None or template.status != "APPROVED":
            return None

    if template is None:
        fe._prepare(followup, now)
    before = fe._claim(session, followup)
    if before is None:
        return False
    try:
        if template is not None:
            params = tpl.params_for(doc, followup.step, now)
            wamid = wab.send_template(conn, phone, template.name, params, button_suffix=doc.view_token or None, language=template.language)
            followup.subject = f"WhatsApp an +{phone}"
            followup.body = tpl.render(key, params)
            category = (template.category or "UTILITY").lower()
            kind = "template"
        else:
            wamid = wab.send_text(conn, phone, _free_text(doc, followup, now))
            category, kind = "service", "text"
    except wab.WaSendError as exc:
        _release(followup, before)
        handle_send_error(session, business, conn, contact, exc.err, now, doc_id=doc.id, template=template)
        session.commit()
        return None
    except Exception:
        _release(followup, before)
        session.commit()
        raise

    followup.status = FollowUpStatus.SENT
    followup.sent_at = now
    followup.error = ""
    doc.followups_sent += 1
    fe.space_following(doc, followup, now)
    remaining = [f.due_at for f in doc.followups if f.status == FollowUpStatus.SCHEDULED]
    doc.next_followup_at = min(remaining) if remaining else None
    session.add(WaMessage(
        business_id=business.id, followup_id=followup.id, document_id=doc.id, wamid=wamid, to_phone=phone,
        kind=kind, template_key=key if template is not None else "", category=category,
    ))
    usage.count(session, business.id, category, now)
    log_event(session, business.id, "sent", f"Nachfassung {followup.step} für {doc.kind_label} {doc.document_number or doc.title} per WhatsApp an +{phone} geschickt.", doc.id)
    session.commit()
    return True


def _release(fu: FollowUp, before: FollowUpStatus) -> None:
    """Beanspruchung zurücknehmen. _claim hat per UPDATE „sending“ geschrieben, das ORM kennt noch den alten Wert:
    ohne flag_modified sähe es keine Änderung und die Zeile bliebe auf „sending“ hängen."""
    from sqlalchemy.orm.attributes import flag_modified

    fu.status = before
    fu.sent_at = None
    flag_modified(fu, "status")
    flag_modified(fu, "sent_at")


def handle_send_error(session: Session, business: Business, conn, contact, err: wab.MappedError, now: datetime,
                      doc_id: int | None = None, template=None) -> None:
    """Fehler merken und dem Betrieb klar sagen, was los ist. Der Aufrufer fällt auf Mail zurück."""
    if err.scope == "recipient" and contact is not None:
        if err.code == 131050:
            contacts.opt_out(contact, now)
        elif err.code in (131026, 131021):
            contact.undeliverable_at = now
    elif err.scope == "template" and template is not None:
        template.status = "PAUSED" if err.code == 132015 else ("DISABLED" if err.code == 132016 else "ERROR")
        template.note = err.owner_text[:500]
    elif err.scope == "account" and conn is not None:
        conn.status = "error"
        conn.last_error = err.owner_text[:1000]
        _tell_owner(session, business, err.owner_text)
    log_event(session, business.id, "whatsapp", err.owner_text, doc_id)


def _tell_owner(session: Session, business: Business, text: str) -> None:
    try:
        from ..notify import send
        from ..notify.messages import info

        send(session, info(business, "WhatsApp-Nachfasser", text, "WhatsApp-Nachfasser braucht dich", text))
    except Exception as exc:  # Hinweis darf nie den Ablauf stoppen
        log.info("Hinweis an Betrieb nicht zugestellt: %s", exc)
