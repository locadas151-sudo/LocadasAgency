"""Was der Sekretär per WhatsApp schreibt. Kurz, ruhig, immer mit Knöpfen zum Antworten.

Die Mail-Texte (Fallback) baut die jeweilige Stelle im Engine-Code wie bisher und reicht sie hier durch.
"""
from __future__ import annotations

from .. import fmt
from ..db import Business, DocKind, Document, FollowUp
from . import whatsapp as wa
from .core import Notification
from .tokens import button, payload

PREVIEW_BUTTONS_PER_DOC = 2  # plus „Passt“ = 3 Knöpfe


def hello(b: Business) -> str:
    return (b.owner_name or "").split(" ")[0] or b.name


def who(d: Document) -> str:
    return (d.customer_name or d.customer_email).split(" <")[0]


def ref(d: Document) -> str:
    return f"{getattr(d, 'term', '') or d.kind_label} {d.document_number or d.title}".strip()


def money(v, currency: str | None = "EUR") -> str:
    return fmt.over(v, currency)


def dmoney(d: Document) -> str:
    return fmt.over(d.amount_gross, getattr(d, "currency", "EUR"))


def _pos(i: int, total: int) -> str:
    return f"{i} von {total}" if total > 1 else ""


def preview(b: Business, fus: list[FollowUp], when: str, subject: str, body: str) -> Notification:
    """`when` ist die echte Versandzeit als Satzanfang, z. B. „heute ab 12:30 Uhr“ oder „morgen ab 8 Uhr“."""
    docs = [f.document for f in fus]
    lines = [f"{when[:1].upper()}{when[1:]} fasse ich hier nach:", ""]
    for i, (f, d) in enumerate(zip(fus, docs), start=1):
        own = f" · du hast am {d.owner_contact_at.strftime('%d.%m.')} selbst geschrieben" if getattr(d, "owner_contact_at", None) else ""
        lines.append(f"{i}. {who(d)}: {ref(d)}{dmoney(d)} (Mail {f.step}){own}")
    lines += ["", "Hat sich schon jemand gemeldet, zum Beispiel am Telefon? Dann tipp auf den Namen."]
    if len(docs) > PREVIEW_BUTTONS_PER_DOC:
        lines.append(f"Für die anderen schreib einfach „Stopp {PREVIEW_BUTTONS_PER_DOC + 1}“ usw.")
    lines.append("Sonst musst du nichts tun.")
    if len(docs) == 1:
        buttons = [button("Hat angerufen", "stop", docs[0].id), button("Passt", "ok")]
        tpl_buttons = None
    else:
        buttons = [button(f"Stopp: {who(d)}", "stop", d.id) for d in docs[:PREVIEW_BUTTONS_PER_DOC]] + [button("Passt", "ok")]
        # Vorlage hat feste Knöpfe „Hat angerufen“ / „Passt“: erst fragen, wer
        tpl_buttons = [payload("pick", *[d.id for d in docs[:10]]), payload("ok")]
    names = ", ".join(who(d) for d in docs[:3]) + (f" und {len(docs) - 3} weiteren" if len(docs) > 3 else "")
    return Notification(
        business=b, kind="preview", title="Heute fasse ich nach", text="\n".join(lines), buttons=buttons,
        email_subject=subject, email_body=body, doc_ids=[d.id for d in docs],
        template="preview", template_params=[hello(b), when, names], template_buttons=tpl_buttons,
        context={"list": [d.id for d in docs]},
    )


def pick(b: Business, docs: list[Document]) -> Notification:
    lines = ["Wer hat sich gemeldet?", ""] + [f"{i}. {who(d)}: {ref(d)}" for i, d in enumerate(docs, start=1)]
    if len(docs) > 3:
        lines += ["", "Tipp auf den Namen oder schreib „Stopp 4“ usw."]
    return Notification(
        business=b, kind="preview", title="", text="\n".join(lines),
        buttons=[button(f"Stopp: {who(d)}", "stop", d.id) for d in docs[:3]], doc_ids=[d.id for d in docs],
        context={"list": [d.id for d in docs]},
    )


def review(b: Business, fu: FollowUp, index: int, total: int, subject: str, body: str) -> Notification:
    """Frage vor dem Nachfassen: Rechnung „Noch offen?“ bzw. Angebot von vor dem Start „Schon erledigt?“."""
    from ..engine.followups import review_labels, review_question

    d = fu.document
    done, go = review_labels(d)
    if d.kind == DocKind.INVOICE:
        return Notification(
            business=b, kind="approval", title=f"{who(d)}: Rechnung noch offen?",
            text=f"{review_question(d)}\n\nIch sehe dein Konto nicht. Die Zahlungserinnerung geht nur raus, wenn du „Noch offen“ tippst.",
            buttons=[button("Noch offen", "send", fu.id), button("Bezahlt", "settle", fu.id)],
            email_subject=subject, email_body=body, doc_ids=[d.id], footer=_pos(index, total),
            template="invoice_open", template_params=[hello(b), review_question(d)],
        )
    # Keine Vorlage (Knöpfe passen nicht), außerhalb des 24h-Fensters per Mail
    return Notification(
        business=b, kind="approval", title=f"{who(d)}: schon erledigt?",
        text=f"{review_question(d)}\n\nNachgefasst wird nur, wenn du „{go}“ tippst. Den Text siehst du vorher noch.",
        buttons=[button(done, "settle", fu.id), button(go, "send", fu.id)],
        email_subject=subject, email_body=body, doc_ids=[d.id], footer=_pos(index, total),
    )


def approval(b: Business, fu: FollowUp, index: int, total: int, subject: str, body: str, approvals_left: int | None = None) -> Notification:
    """Freigabe mit dem vollständigen Text, der rausgehen würde."""
    from ..engine.followups import approval_reason, is_question

    d = fu.document
    if is_question(fu):
        return review(b, fu, index, total, subject, body)
    text = (
        f"{ref(d)}{dmoney(d)}, gesendet {d.sent_at.strftime('%d.%m.')}\n\n"
        f"Diesen Text würde ich in deinem Namen schicken:\n\nBetreff: {fu.subject}\n\n{(fu.body or '').strip()}\n\n{approval_reason(b, d)}"
    )
    # Vorlagen-Parameter sind begrenzt: passt der volle Text nicht hinein, außerhalb des 24h-Fensters lieber per Mail
    template = "approval" if len(fu.body or "") <= wa.PARAM_MAX else None
    return Notification(
        business=b, kind="approval", title=f"Nachfassen bei {who(d)}?", text=text,
        buttons=[button("Senden", "send", fu.id), button("Diesmal nicht", "skip", fu.id), button("Hat sich gemeldet", "gone", fu.id)],
        email_subject=subject, email_body=body, doc_ids=[d.id], footer=_pos(index, total),
        template=template, template_params=[hello(b), who(d), f"{ref(d)}{dmoney(d)}", fu.body],
    )


def tap(b: Business, fu: FollowUp, reminder: bool = False) -> Notification:
    """WhatsApp mit einem Tipp: „In WhatsApp öffnen“ (URL-Knopf), danach Gesendet / Hat geantwortet / Diesmal nicht.

    Keine Mail-Fassung hier: die Taps eines Tages kommen gebündelt in einer Mail (engine/taps.bundle_mail)."""
    from ..engine import taps

    d = fu.document
    if fu.tap_kind in ("mailto", "manual"):
        return _tap_other(b, fu, reminder)
    both = fu.status.value != "awaiting_tap"
    head = f"{ref(d)}{dmoney(d)}, Nachricht {fu.step} von 3"
    if both:
        intro = f"{head}. Die Mail ist schon raus. Willst du zusätzlich per WhatsApp nachhaken? Tipp auf „In WhatsApp öffnen“, der Text steht schon drin."
    else:
        intro = f"{head}. Tipp auf „In WhatsApp öffnen“, dann ist dein Chat mit {who(d)} offen und der Text steht schon drin. Nur noch abschicken."
    prev = taps._prev_wa(fu)
    if prev is not None and prev.sent_at:
        intro += f"\nDie letzte Nachricht ging am {prev.sent_at.strftime('%d.%m.')} per WhatsApp raus. Hat {who(d)} dort geantwortet? Dann tipp unten auf „Hat geantwortet“."
    third = button("Ist bezahlt", "tpaid", fu.id) if d.kind == DocKind.INVOICE else button("Hat geantwortet", "treplied", fu.id)
    return Notification(
        business=b, kind="tap",
        title=f"{'Noch offen: ' if reminder else ''}{who(d)} per WhatsApp nachfassen",
        text=f"{intro}\n\n{fu.wa_text}",
        buttons=[button("Gesendet", "tsent", fu.id), third, button("Diesmal nicht", "tskip", fu.id)],
        buttons_text=f"Hast du die Nachricht an {fmt.dativ(who(d))} abgeschickt?",
        cta=("In WhatsApp öffnen", taps.tap_url(fu)),
        doc_ids=[d.id], footer="Erinnerung" if reminder else "",
        template="tap", template_params=[hello(b), who(d), f"{ref(d)}{dmoney(d)}", fu.wa_text or ""],
        template_url=fu.tap_token,
        template_buttons=[payload("tsent", fu.id), payload("treplied", fu.id), payload("tskip", fu.id)],
    )


def _tap_other(b: Business, fu: FollowUp, reminder: bool) -> Notification:
    """Testen ohne Postfach: fertige Mail im eigenen Mailprogramm (mailto) oder Text zum Kopieren, wenn der Kontakt fehlt."""
    from ..engine import taps

    d = fu.document
    head = f"{ref(d)}{dmoney(d)}, Nachricht {fu.step} von 3"
    if fu.tap_kind == "mailto":
        intro = f"{head}. Tipp auf „Mail öffnen“, dann ist die Mail an {fmt.dativ(who(d))} in deinem Mailprogramm fertig. Nur noch abschicken."
        event = f"Bei {who(d)} ({ref(d)}) ist heute die Nachfass-Mail dran. Ein Tipp öffnet sie fertig in deinem Mailprogramm: {taps.tap_url(fu)}"
    else:
        intro = (f"{head}. Mir fehlt die Handynummer oder Mailadresse von {who(d) or 'deinem Kunden'}. Schick sie mir einfach hier, "
                 "dann ist die Nachricht mit einem Tipp fertig. Oder kopier den Text und schick ihn selbst.")
        event = f"Bei {who(d) or 'deinem Kunden'} ({ref(d)}) ist heute die Nachfass-Nachricht dran. Mir fehlt die Nummer oder Mailadresse, schick sie mir hier. Text zum Kopieren: {taps.tap_url(fu)}"
    third = button("Ist bezahlt", "tpaid", fu.id) if d.kind == DocKind.INVOICE else button("Hat geantwortet", "treplied", fu.id)
    return Notification(
        business=b, kind="tap",
        title=f"{'Noch offen: ' if reminder else ''}{who(d) or ref(d)} nachfassen",
        text=f"{intro}\n\n{fu.wa_text}",
        buttons=[button("Gesendet", "tsent", fu.id), third, button("Diesmal nicht", "tskip", fu.id)],
        buttons_text=f"Hast du die Nachricht an {fmt.dativ(who(d)) or 'deinen Kunden'} abgeschickt?",
        cta=(taps.open_label(fu), taps.tap_url(fu)),
        doc_ids=[d.id], footer="Erinnerung" if reminder else "",
        template="event", template_params=[hello(b), event], template_buttons=[payload("ok")],
    )


def tap_overflow(b: Business, fus: list[FollowUp]) -> Notification:
    """Mehr als 5 an einem Tag: der Rest steht im Dashboard (eine Nachricht je Kunde wäre zu viel)."""
    from ..config import settings

    names = ", ".join(who(f.document) for f in fus[:4]) + (f" und {len(fus) - 4} weitere" if len(fus) > 4 else "")
    text = f"Heute sind noch {len(fus)} weitere WhatsApp-Nachrichten dran: {names}. Du findest sie mit je einem Knopf im Dashboard:\n{settings.base_url}/dashboard#whatsapp"
    return Notification(
        business=b, kind="tap", title="Noch mehr per WhatsApp", text=text, buttons=[button("Alles klar", "ok")],
        doc_ids=[f.document.id for f in fus], template="event", template_params=[hello(b), text],
    )


def opened(d: Document, subject: str, body: str) -> Notification:
    b = d.business
    return Notification(
        business=b, kind="opened", title=f"{who(d)} hat {fmt.possessive(d.kind_label)} geöffnet",
        text=f"{ref(d)}{dmoney(d)} wurde gerade geöffnet. Guter Moment für einen kurzen Anruf.",
        buttons=[button("Jetzt anrufen", "call", d.id), button("Erledigt", "done", d.id)],
        email_subject=subject, email_body=body, doc_ids=[d.id],
        template="opened", template_params=[hello(b), who(d), ref(d)],
    )


def replied(d: Document, note: str, subject: str, body: str) -> Notification:
    b = d.business
    summary = f"» {d.reply_summary}\n\n" if d.reply_summary else ""
    return Notification(
        business=b, kind="replied", title=f"{who(d)} hat geantwortet",
        text=f"Auf {ref(d)}.\n\n{summary}{note}",
        buttons=[button("Passt", "ok", d.id), button("Weiter nachfassen", "reopen", d.id)],
        email_subject=subject, email_body=body, doc_ids=[d.id],
        template="replied", template_params=[hello(b), who(d), ref(d), (d.reply_summary + " " if d.reply_summary else "") + note],
    )


def bounced(d: Document, subject: str, body: str) -> Notification:
    b = d.business
    text = (
        f"Die Nachfass-Mail zu {ref(d)} an {d.customer_email} ist zurückgekommen, die Adresse ist unzustellbar. "
        "Ich fasse dort nicht mehr nach.\n\nSchick mir hier einfach die richtige Mailadresse, dann fasse ich dort weiter nach."
    )
    return Notification(
        business=b, kind="bounced", title=f"Mail an {fmt.dativ(who(d))} kam nicht an", text=text,
        buttons=[button("Alles klar", "done", d.id)], email_subject=subject, email_body=body, doc_ids=[d.id],
        template="event", template_params=[hello(b), f"Die Mail an {who(d)} ({d.customer_email}) zu {ref(d)} kam nicht an, die Adresse ist unzustellbar. Schick mir hier die richtige Adresse, dann fasse ich weiter nach."],
        context={"bounced": d.id},
    )


def outcome(d: Document, index: int, total: int, subject: str, body: str) -> Notification:
    b = d.business
    if d.kind == DocKind.OFFER:
        question, buttons, tpl = "Hast du den Auftrag bekommen?", [button("Auftrag bekommen", "won", d.id), button("Nicht bekommen", "lost", d.id)], "outcome_offer"
    else:
        question, buttons, tpl = "Ist die Rechnung bezahlt?", [button("Ist bezahlt", "paid", d.id), button("Nicht bezahlt", "unpaid", d.id)], "outcome_invoice"
    return Notification(
        business=b, kind="outcome", title="Wie ist es ausgegangen?",
        text=f"{who(d)}: {ref(d)}{dmoney(d)}\nIch habe alles nachgefasst, per Mail kam keine Antwort. {question}",
        buttons=buttons, email_subject=subject, email_body=body, doc_ids=[d.id], footer=_pos(index, total),
        template=tpl, template_params=[hello(b), who(d), f"{ref(d)}{dmoney(d)}"],
    )


def login(b: Business, link: str, subject: str, body: str) -> Notification:
    # Keine Vorlage: außerhalb des 24h-Fensters kommt der Login-Link per Mail
    return Notification(
        business=b, kind="login", title="Dein Login-Link", text=f"Zwei Stunden gültig:\n{link}",
        email_subject=subject, email_body=body,
    )


def welcome(b: Business) -> Notification:
    return Notification(
        business=b, kind="info", title="Hallo, hier ist dein Locadas-Sekretär",
        text=(
            "Ab jetzt melde ich mich hier statt per Mail. Ich sage dir Bescheid, wenn ein Kunde dein Angebot öffnet oder antwortet, "
            "und bevor ich nachfasse. Du antwortest einfach mit den Knöpfen.\n\nSchreib STOP, wenn du keine Nachrichten mehr willst."
        ),
        buttons=[button("Alles klar", "ok")], template="welcome", template_params=[hello(b)],
    )


def info(b: Business, title: str, text: str, subject: str = "", body: str = "") -> Notification:
    return Notification(
        business=b, kind="info", title=title, text=text, buttons=[button("Alles klar", "ok")],
        email_subject=subject, email_body=body, template="event", template_params=[hello(b), f"{title}. {text}"],
    )


def connection_lost(b: Business, account, reminder: bool = False) -> Notification:
    """Verbindungs-Wächter: Postfach nicht mehr verbunden. Ein Knopf „Neu verbinden“ mit vorausgefüllter Adresse."""
    from .tokens import reconnect_link, reconnect_token

    link = reconnect_link(account.id)
    text = f"Dein Postfach {account.email} ist nicht mehr verbunden (Passwort geändert?). Solange fasse ich nicht nach."
    if reminder and account.error_since:
        text = f"Dein Postfach {account.email} ist seit {fmt_day(account.error_since)} nicht mehr verbunden (Passwort geändert?). Solange fasse ich nicht nach."
    body = (
        f"Hallo {hello(b)},\n\n{text}\n\n"
        f"Neu verbinden (deine Adresse ist schon eingetragen, du brauchst nur das Passwort):\n{link}\n\n"
        "Deine Vorgänge bleiben alle erhalten. Danach fasse ich wieder nach, verteilt auf die nächsten Tage.\n\n"
        f"Fehlermeldung des Mailservers: {(account.last_error or '-')[:300]}\n\nLocadas Nachfasser"
    )
    return Notification(
        business=b, kind="connection", title=("Erinnerung: " if reminder else "") + "Postfach nicht verbunden",
        text=f"{text}\n\nTipp auf „Neu verbinden“, deine Adresse ist schon eingetragen. Deine Vorgänge bleiben erhalten.",
        cta=("Neu verbinden", link),
        email_subject=f"{'Erinnerung: ' if reminder else ''}Dein Postfach ist nicht mehr verbunden", email_body=body,
        template="connection", template_params=[hello(b), account.email], template_url=reconnect_token(account.id), template_buttons=[],
    )


def connection_back(b: Business, account) -> Notification:
    text = "Wieder verbunden, alles läuft."
    more = f"Dein Postfach {account.email} ist wieder verbunden. Was liegen geblieben ist, verteile ich auf die nächsten Tage."
    return Notification(
        business=b, kind="connection", title="Postfach wieder verbunden", text=f"{text} {more}",
        buttons=[button("Alles klar", "ok")],
        email_subject="Wieder verbunden, alles läuft", email_body=f"Hallo {hello(b)},\n\n{text}\n\n{more}\n\nLocadas Nachfasser",
        template="event", template_params=[hello(b), f"{text} {more}"],
    )


def summary(b: Business, text: str, subject: str, body: str) -> Notification:
    """Monatsbilanz (engine/summary.py), eine Nachricht im Monat."""
    return Notification(
        business=b, kind="summary", title="Deine Monatsbilanz", text=text,
        buttons=[button("Alles klar", "ok")], email_subject=subject, email_body=body,
        template="summary", template_params=[hello(b), text],
    )


def fmt_day(dt) -> str:
    from zoneinfo import ZoneInfo

    from ..config import settings

    return dt.astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m.")


HELP = (
    "Ich bin dein Locadas-Sekretär und melde mich hier, wenn\n"
    "• ich heute bei Kunden nachfasse (du kannst stoppen, wenn jemand angerufen hat)\n"
    "• ein Kunde dein Angebot öffnet oder antwortet\n"
    "• ich eine Freigabe brauche oder wissen will, wie ein Vorgang ausgegangen ist\n\n"
    "Antworte einfach mit den Knöpfen unter meinen Nachrichten. Außerdem verstehe ich:\n"
    "„Stopp 2“ stoppt den zweiten Kunden aus meiner letzten Liste\n"
    "eine Mailadresse, wenn eine Mail nicht ankam\n"
    "STOP beendet die Nachrichten hier, dann kommt alles wieder per Mail"
)


def _quote(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text
