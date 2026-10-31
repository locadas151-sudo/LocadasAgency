"""Nachfass-Texte. Freundlich, kurz, im Namen des Betriebs. Keine Floskeln, kein Druck beim ersten Mal."""
from __future__ import annotations

from ..db import DocKind, Document


def _anrede(doc: Document) -> str:
    name = (doc.customer_name or "").strip()
    if not name or "@" in name:
        return "Guten Tag,"
    parts = name.split()
    lowered = name.lower()
    if lowered.startswith(("herr ", "hr. ", "frau ", "fr. ")):
        return f"Guten Tag {name},"
    # Firma oder Einzelperson ohne Anrede: neutral bleiben
    if len(parts) >= 2 and not any(w in lowered for w in ("gmbh", "ug", "kg", "ohg", "e.k", "ag", "&", "und söhne")):
        return f"Guten Tag {name},"
    return "Guten Tag,"


def _betrag(doc: Document) -> str:
    if doc.amount_gross is None:
        return ""
    return f"{doc.amount_gross:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def _ref(doc: Document) -> str:
    bits = []
    if doc.document_number:
        bits.append(f"Nr. {doc.document_number}")
    if doc.title:
        bits.append(doc.title)
    return " ".join(bits) if bits else doc.subject


def _sig(doc: Document) -> str:
    b = doc.business
    sig = (b.signature or "").strip()
    if sig:
        return sig
    lines = ["Viele Grüße", b.owner_name or b.name]
    if b.owner_name and b.name and b.owner_name != b.name:
        lines.append(b.name)
    if b.phone:
        lines.append(b.phone)
    return "\n".join(lines)


def _datum(dt) -> str:
    return dt.strftime("%d.%m.%Y") if dt else ""


def subject_for(doc: Document, step: int) -> str:
    base = doc.subject.strip() or (f"{doc.kind_label} {doc.document_number}".strip())
    if base.lower().startswith(("re:", "aw:", "wg:")):
        return base
    return f"AW: {base}"


def _link(doc: Document) -> str:
    from ..config import settings

    if not getattr(doc.business, "include_view_link", False) or not getattr(doc, "view_token", "") or not doc.pdf_path:
        return ""
    what = "das Angebot" if doc.kind == DocKind.OFFER else "die Rechnung"
    return f"Hier können Sie {what} jederzeit noch einmal ansehen:\n{settings.base_url}/a/{doc.view_token}\n\n"


def body_for(doc: Document, step: int) -> str:
    link = _link(doc)
    anrede = _anrede(doc)
    ref = _ref(doc)
    betrag = _betrag(doc)
    sig = _sig(doc)
    gesendet = _datum(doc.sent_at)

    if doc.kind == DocKind.OFFER:
        if step == 1:
            text = (
                f"{anrede}\n\n"
                f"am {gesendet} habe ich Ihnen unser Angebot ({ref}) geschickt. "
                f"Ich wollte kurz nachhören, ob alles angekommen ist und ob noch Fragen offen sind.\n\n"
                f"Wenn etwas unklar ist oder Sie etwas anpassen möchten, melden Sie sich einfach, ich antworte schnell.\n\n"
                f"{link}{sig}"
            )
        elif step == 2:
            text = (
                f"{anrede}\n\n"
                f"ich melde mich noch einmal wegen unseres Angebots ({ref}). "
                f"Falls Sie sich schon entschieden haben, freue ich mich über eine kurze Rückmeldung, auch wenn es nichts wird. "
                f"Dann kann ich besser planen.\n\n"
                f"Falls Sie noch überlegen: Gern besprechen wir Umfang oder Termin, so dass es für Sie passt.\n\n"
                f"{link}{sig}"
            )
        else:
            text = (
                f"{anrede}\n\n"
                f"eine letzte Nachricht zu unserem Angebot ({ref}). "
                f"Ich möchte Sie nicht drängen, brauche aber eine Rückmeldung, um den Termin für Sie freizuhalten"
                f"{' und die Konditionen aus dem Angebot zu sichern' if doc.valid_until else ''}.\n\n"
                f"Eine kurze Antwort reicht: Ja, Nein oder später. Vielen Dank.\n\n"
                f"{link}{sig}"
            )
        return text

    # Rechnung
    faellig = _datum(doc.due_date)
    betrag_txt = f" über {betrag}" if betrag else ""
    if step == 1:
        text = (
            f"{anrede}\n\n"
            f"unsere Rechnung ({ref}){betrag_txt} war am {faellig} fällig. "
            f"Vielleicht ist sie untergegangen, das passiert. "
            f"Könnten Sie den Betrag in den nächsten Tagen überweisen?\n\n"
            f"Falls die Zahlung schon unterwegs ist, bitte diese Nachricht einfach ignorieren.\n\n"
            f"{link}{sig}"
        )
    elif step == 2:
        text = (
            f"{anrede}\n\n"
            f"zu unserer Rechnung ({ref}){betrag_txt}, fällig seit dem {faellig}, ist bei uns noch kein Zahlungseingang zu sehen. "
            f"Bitte überweisen Sie den Betrag innerhalb der nächsten 7 Tage.\n\n"
            f"Sollte es einen Grund für die Verzögerung geben oder eine Frage zur Rechnung, melden Sie sich bitte kurz, dann finden wir eine Lösung.\n\n"
            f"{link}{sig}"
        )
    else:
        text = (
            f"{anrede}\n\n"
            f"trotz zweier Erinnerungen ist unsere Rechnung ({ref}){betrag_txt}, fällig seit dem {faellig}, noch offen. "
            f"Bitte überweisen Sie den Betrag bis spätestens in 7 Tagen. "
            f"Danach müssen wir das Mahnverfahren einleiten, was zusätzliche Kosten für Sie bedeutet, und das möchten wir beide vermeiden.\n\n"
            f"Bei Fragen erreichen Sie mich jederzeit.\n\n"
            f"{link}{sig}"
        )
    return text
