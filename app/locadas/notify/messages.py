"""Was der Sekretär per WhatsApp schreibt. Kurz, ruhig, immer mit Knöpfen zum Antworten.

Die Mail-Texte (Fallback) baut die jeweilige Stelle im Engine-Code wie bisher und reicht sie hier durch.
"""
from __future__ import annotations

from .. import fmt
from ..db import Business, DocKind, Document, FollowUp
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


def preview(b: Business, fus: list[FollowUp], send_time: str, subject: str, body: str) -> Notification:
    docs = [f.document for f in fus]
    lines = [f"Heute ab {send_time} Uhr fasse ich hier nach:", ""]
    for i, (f, d) in enumerate(zip(fus, docs), start=1):
        lines.append(f"{i}. {who(d)}: {ref(d)}{dmoney(d)} (Mail {f.step})")
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
        template="preview", template_params=[hello(b), send_time, names], template_buttons=tpl_buttons,
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
    """Vorgang von vor dem Start: erst fragen. Keine Vorlage (Knöpfe passen nicht), außerhalb des 24h-Fensters per Mail."""
    from ..engine.followups import review_labels, review_question

    d = fu.document
    done, go = review_labels(d)
    return Notification(
        business=b, kind="approval", title=f"{who(d)}: schon {'bezahlt' if d.kind == DocKind.INVOICE else 'erledigt'}?",
        text=f"{review_question(d)}\n\nNachgefasst wird nur, wenn du „{go}“ tippst. Den Text siehst du vorher noch.",
        buttons=[button(done, "settle", fu.id), button(go, "send", fu.id)],
        email_subject=subject, email_body=body, doc_ids=[d.id], footer=_pos(index, total),
    )


def approval(b: Business, fu: FollowUp, index: int, total: int, subject: str, body: str, approvals_left: int | None) -> Notification:
    d = fu.document
    if d.needs_review:
        return review(b, fu, index, total, subject, body)
    text = (
        f"{ref(d)}{dmoney(d)}, gesendet {d.sent_at.strftime('%d.%m.')}\n\n"
        f"Diesen Text würde ich in deinem Namen schicken:\n\n{_quote(fu.body, 650)}"
    )
    footer_parts = [_pos(index, total)]
    if approvals_left:
        footer_parts.append(f"Noch {approvals_left} Freigaben bis automatisch")
    return Notification(
        business=b, kind="approval", title=f"Nachfassen bei {who(d)}?", text=text,
        buttons=[button("Senden", "send", fu.id), button("Diesmal nicht", "skip", fu.id), button("Hat sich gemeldet", "gone", fu.id)],
        email_subject=subject, email_body=body, doc_ids=[d.id], footer=" · ".join(p for p in footer_parts if p),
        template="approval", template_params=[hello(b), who(d), f"{ref(d)}{dmoney(d)}", fu.body],
    )


def opened(d: Document, subject: str, body: str) -> Notification:
    b = d.business
    return Notification(
        business=b, kind="opened", title=f"{who(d)} hat dein {d.kind_label} geöffnet",
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
        business=b, kind="bounced", title=f"Mail an {who(d)} kam nicht an", text=text,
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
            "und bevor ich nachfasse. Du antwortest einfach mit den Knöpfen.\n\nSchreib „abmelden“, wenn du keine Nachrichten mehr willst."
        ),
        buttons=[button("Alles klar", "ok")], template="welcome", template_params=[hello(b)],
    )


def info(b: Business, title: str, text: str, subject: str = "", body: str = "") -> Notification:
    return Notification(
        business=b, kind="info", title=title, text=text, buttons=[button("Alles klar", "ok")],
        email_subject=subject, email_body=body, template="event", template_params=[hello(b), f"{title}. {text}"],
    )


HELP = (
    "Ich bin dein Locadas-Sekretär und melde mich hier, wenn\n"
    "• ich heute bei Kunden nachfasse (du kannst stoppen, wenn jemand angerufen hat)\n"
    "• ein Kunde dein Angebot öffnet oder antwortet\n"
    "• ich eine Freigabe brauche oder wissen will, wie ein Vorgang ausgegangen ist\n\n"
    "Antworte einfach mit den Knöpfen unter meinen Nachrichten. Außerdem verstehe ich:\n"
    "„Stopp 2“ stoppt den zweiten Kunden aus meiner letzten Liste\n"
    "eine Mailadresse, wenn eine Mail nicht ankam\n"
    "„abmelden“ beendet die Nachrichten hier, dann kommt alles wieder per Mail"
)


def _quote(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text
