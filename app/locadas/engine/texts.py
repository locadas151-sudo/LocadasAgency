"""Nachfass-Texte (feste Vorlagen) und alles, was Vorlage und personalisierter Text gemeinsam brauchen.

Kurz, im Namen des Betriebs, mit dem Begriff und der Währung aus dem Dokument selbst
(„Offerte“, „CHF 2'151.19“). Angebote: nie drängen, schon gar nicht, wenn der Kunde es geöffnet hat.
Rechnungen: immer mit konkretem Zahlungsdatum und, wenn bekannt, Bankverbindung. Der Ton der
Zahlungserinnerung kommt aus der Einstellung des Betriebs, Firmenkunden bekommen mindestens „sachlich“.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import db as _db
from .. import fmt
from ..db import DocKind, Document

PAYMENT_DAYS = 7
TONES = ("freundlich", "sachlich", "bestimmt")
DELIVERY = ("link", "attachment")

# --- Begriff, Artikel ----------------------------------------------------------------

_GENDER = {"angebot": "n", "offerte": "f", "kostenvoranschlag": "m"}  # alle …rechnung: f


def term(doc) -> str:
    t = (getattr(doc, "doc_term", "") or "").strip()
    if t:
        return t
    return "Angebot" if doc.kind == DocKind.OFFER else "Rechnung"


def _gender(doc) -> str:
    return _GENDER.get(term(doc).lower(), "f")


def der(doc, case: str = "nom") -> str:
    """Bestimmter Artikel: das/die/der (nom), das/die/den (acc), dem/der/dem (dat)."""
    g = _gender(doc)
    return {
        "nom": {"n": "das", "f": "die", "m": "der"},
        "acc": {"n": "das", "f": "die", "m": "den"},
        "dat": {"n": "dem", "f": "der", "m": "dem"},
    }[case][g]


def unser(doc, case: str = "nom") -> str:
    g = _gender(doc)
    return {
        "nom": {"n": "unser", "f": "unsere", "m": "unser"},
        "acc": {"n": "unser", "f": "unsere", "m": "unseren"},
        "dat": {"n": "unserem", "f": "unserer", "m": "unserem"},
    }[case][g]


def zum(doc) -> str:
    return "zur" if _gender(doc) == "f" else "zum"


def dies(doc, case: str = "nom") -> str:
    g = _gender(doc)
    return {"nom": {"n": "Dieses", "f": "Diese", "m": "Dieser"}, "acc": {"n": "Dieses", "f": "Diese", "m": "Diesen"}}[case][g]


def es(doc) -> str:
    return {"n": "es", "f": "sie", "m": "er"}[_gender(doc)]


# --- Kunde ------------------------------------------------------------------------

FREEMAIL = {
    "gmx.de", "gmx.net", "gmx.at", "gmx.ch", "gmx.com", "web.de", "t-online.de", "magenta.de", "gmail.com", "googlemail.com",
    "yahoo.de", "yahoo.com", "ymail.com", "outlook.de", "outlook.com", "hotmail.de", "hotmail.com", "live.de", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "aol.de", "freenet.de", "posteo.de", "posteo.net", "mailbox.org", "arcor.de",
    "online.de", "1und1.de", "vodafone.de", "kabelmail.de", "ewe.net", "email.de", "bluewin.ch", "sunrise.ch", "hispeed.ch",
    "aon.at", "a1.net", "chello.at", "proton.me", "protonmail.com", "unitybox.de", "htp-tel.de", "netcologne.de", "mail.de",
}
_COMPANY_RE = re.compile(
    r"gmbh|mbh|\bug\b|\bkg\b|\bohg\b|\bag\b|\bgbr\b|e\.\s?k\.|e\.\s?v\.|&|\bund söhne\b|\bco\.|\bse\b|\bltd\b|\binc\b|"
    r"verwaltung|immobilien|\bbau\b|bauprojekt|\bgu\b|generalunternehm|\bstadt\b|\bgemeinde\b|\bfirma\b|\bhotel\b|\bpraxis\b|"
    r"\bwohnungs|\bweg\b|eigentümergemeinschaft|genossenschaft|\bkanzlei\b|\bhochbau\b|\bholding\b",
    re.I,
)


def is_business_customer(doc) -> bool:
    """Firmenkunde: Firmenname oder Firmen-Mailadresse (keine Freemail-Domain)."""
    if _COMPANY_RE.search(getattr(doc, "customer_name", "") or ""):
        return True
    addr = getattr(doc, "customer_email", "") or ""
    domain = addr.rsplit("@", 1)[-1].lower().strip()
    return "@" in addr and bool(domain) and domain not in FREEMAIL


def invoice_tone(doc) -> str:
    """Ton der Zahlungserinnerung: Einstellung des Betriebs, bei Firmenkunden mindestens „sachlich“."""
    tone = getattr(doc.business, "tone", "") or "freundlich"
    if tone not in TONES:
        tone = "freundlich"
    if tone == "freundlich" and is_business_customer(doc):
        tone = "sachlich"
    return tone


# --- Bausteine --------------------------------------------------------------------------

def _anrede(doc: Document) -> str:
    name = (doc.customer_name or "").strip()
    if not name or "@" in name:
        return "Guten Tag,"
    parts = name.split()
    lowered = name.lower()
    if lowered.startswith(("herr ", "hr. ", "frau ", "fr. ")):
        return f"Guten Tag {name},"
    # Firma oder Einzelperson ohne Anrede: neutral bleiben
    if len(parts) >= 2 and not _COMPANY_RE.search(name):
        return f"Guten Tag {name},"
    return "Guten Tag,"


def _betrag(doc: Document) -> str:
    return fmt.money(doc.amount_gross, getattr(doc, "currency", "EUR"))


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


def payment_deadline(now: datetime | None = None) -> datetime:
    """Versandtag + 7 Tage (lokale Zeit), fällt das aufs Wochenende oder einen Feiertag: der nächste Werktag."""
    from ..config import settings

    from ..holidays import is_workday

    now = now or _db.utcnow()
    local = now.astimezone(ZoneInfo(settings.timezone)) + timedelta(days=PAYMENT_DAYS)
    while not is_workday(local.date()):
        local += timedelta(days=1)
    return local


def payment_line(doc, now: datetime | None = None, du: bool = False) -> str:
    """„Bitte überweisen Sie 318,92 € bis 10.11.2026 auf IBAN …, Verwendungszweck: NB-55102.“"""
    betrag = _betrag(doc) or "den offenen Betrag"
    datum = _datum(payment_deadline(now))
    s = f"Bitte {'überweise' if du else 'überweisen Sie'} {betrag} bis {datum}"
    iban = (getattr(doc, "iban", "") or "").strip()
    if iban:
        s += f" auf IBAN {iban}"
        extra = []
        if getattr(doc, "account_holder", ""):
            extra.append(f"Kontoinhaber {doc.account_holder}")
        if getattr(doc, "bic", ""):
            extra.append(f"BIC {doc.bic}")
        if getattr(doc, "bank_name", "") and not getattr(doc, "bic", ""):
            extra.append(doc.bank_name)
        if extra:
            s += f" ({', '.join(extra)})"
    if doc.document_number:
        s += f", Verwendungszweck: {doc.document_number}"
    return s + "."


def ack_line(doc, du: bool = False) -> str:
    ack = getattr(doc, "ack_at", None)
    if not ack:
        return ""
    return f"{'Du hattest' if du else 'Sie hatten'} den Eingang am {_datum(ack)} bestätigt."


def opened(doc) -> bool:
    return bool(getattr(doc, "first_opened_at", None))


def subject_for(doc: Document, step: int) -> str:
    """Angebote: Originalbetreff (Thread über In-Reply-To). Rechnungen: ehrlich als Zahlungserinnerung."""
    base = (doc.subject or "").strip() or f"{term(doc)} {doc.document_number}".strip()
    if doc.kind == DocKind.OFFER:
        return base
    clean = re.sub(r"^(?:(?:re|aw|wg|fw|fwd)\s*:\s*)+", "", base, flags=re.I).strip() or base
    if re.match(r"(?i)(letzte\s+)?zahlungserinnerung", clean):
        clean = re.sub(r"(?i)^(letzte\s+)?zahlungserinnerung\s*:\s*", "", clean)
    return f"{'Letzte Zahlungserinnerung' if step >= 3 else 'Zahlungserinnerung'}: {clean}"


def delivery(doc) -> str:
    """"link", "attachment" oder "" (nichts beifügen)."""
    b = doc.business
    if not getattr(b, "include_view_link", False) or not getattr(doc, "pdf_path", ""):
        return ""
    mode = getattr(b, "doc_delivery", "link") or "link"
    if mode == "attachment":
        return "attachment"
    return "link" if getattr(doc, "view_token", "") else ""


def view_url(doc) -> str:
    from ..config import settings

    return f"{settings.view_base_url}/a/{doc.view_token}"


def _link(doc: Document, du: bool = False) -> str:
    mode = delivery(doc)
    if mode == "link":
        return f"{der(doc).capitalize()} {term(doc)} als PDF: {view_url(doc)}\n\n"
    if mode == "attachment":
        return f"{der(doc, 'acc').capitalize()} {term(doc)} hänge ich {'dir' if du else 'Ihnen'} noch einmal an.\n\n"
    return ""


def _phone_line(doc, du: bool = False) -> str:
    phone = (getattr(doc.business, "phone", "") or "").strip()
    if phone:
        return f"Ich melde mich nicht weiter, {'du erreichst' if du else 'Sie erreichen'} mich aber jederzeit unter {phone}."
    if du:
        return "Ich melde mich nicht weiter. Wenn du später darauf zurückkommen möchtest, antworte einfach auf diese Mail."
    return "Ich melde mich nicht weiter. Wenn Sie später darauf zurückkommen möchten, antworten Sie einfach auf diese Mail."


def closing(doc, step: int, now: datetime | None = None, du: bool = False) -> str:
    """Fester Schlussabsatz, den auch der personalisierte Text bekommt: Zahlungsaufforderung bzw. „melde mich nicht weiter“."""
    if doc.kind == DocKind.INVOICE:
        ack = ack_line(doc, du)
        return f"{ack} {payment_line(doc, now, du)}" if ack else payment_line(doc, now, du)
    if step >= 3:
        return _phone_line(doc, du)
    return ""


# --- Vorlagen -----------------------------------------------------------------------------

def _offer_text(doc: Document, step: int) -> str:
    t, ref = term(doc), _ref(doc)
    gesendet = _datum(doc.sent_at)
    if opened(doc):
        if step == 1:
            return (
                f"am {gesendet} habe ich Ihnen {unser(doc, 'acc')} {t} ({ref}) geschickt. "
                f"Falls Sie noch Fragen {zum(doc)} {t} haben oder etwas anders haben möchten, melden Sie sich gern. "
                f"Ich erkläre Ihnen auch gern einzelne Positionen am Telefon."
            )
        if step == 2:
            return (
                f"ich melde mich noch einmal zu {unser(doc, 'dat')} {t} ({ref}). "
                f"Falls Sie noch Fragen {zum(doc)} {t} haben oder am Umfang etwas anpassen möchten, sagen Sie mir gern Bescheid.\n\n"
                f"Lassen Sie sich ruhig die Zeit, die Sie brauchen."
            )
        valid = f" {der(doc).capitalize()} {t} gilt bis {_datum(doc.valid_until)}." if getattr(doc, "valid_until", None) else ""
        return (
            f"falls Sie noch Fragen {zum(doc)} {t} ({ref}) haben, helfe ich Ihnen gern weiter.{valid}\n\n"
            f"{_phone_line(doc)}"
        )
    if step == 1:
        return (
            f"am {gesendet} habe ich Ihnen {unser(doc, 'acc')} {t} ({ref}) geschickt. "
            f"Ich wollte kurz nachhören, ob alles angekommen ist und ob noch Fragen offen sind.\n\n"
            f"Wenn etwas unklar ist oder Sie etwas anpassen möchten, melden Sie sich einfach. Ich antworte schnell."
        )
    if step == 2:
        return (
            f"ich melde mich noch einmal zu {unser(doc, 'dat')} {t} ({ref}). "
            f"Falls Sie sich schon entschieden haben, freue ich mich über eine kurze Rückmeldung, auch wenn es nichts wird. "
            f"Dann kann ich besser planen.\n\n"
            f"Falls Sie noch überlegen: Gern besprechen wir Umfang oder Termin, damit es für Sie passt."
        )
    valid = f" {der(doc).capitalize()} {t} gilt bis {_datum(doc.valid_until)}." if getattr(doc, "valid_until", None) else ""
    return (
        f"zu {unser(doc, 'dat')} {t} ({ref}) habe ich bisher nichts von Ihnen gehört. "
        f"Vielleicht passt es gerade nicht oder Sie haben sich anders entschieden, beides ist völlig in Ordnung.{valid}\n\n"
        f"{_phone_line(doc)}"
    )


def _invoice_text(doc: Document, step: int, now: datetime | None) -> str:
    t, ref = term(doc), _ref(doc)
    betrag = _betrag(doc)
    b_txt = f" über {betrag}" if betrag else ""
    faellig = _datum(doc.due_date)
    ack = ack_line(doc)
    ack = f" {ack}" if ack else ""
    pay = payment_line(doc, now)
    tone = invoice_tone(doc)
    head = f"{unser(doc)} {t} ({ref}){b_txt}"
    offen = f"Laut meinen Unterlagen ist {der(doc)} {t} noch offen."
    # Nur nach Bestätigung durch den Betrieb („Noch offen“). Nie behaupten, wir hätten keinen Zahlungseingang gefunden.
    if tone == "bestimmt":
        if step == 1:
            return (
                f"{head} war am {faellig} fällig. {offen}{ack}\n\n{pay}\n\n"
                f"Bei Fragen zur {t} melden Sie sich bitte umgehend bei mir."
            )
        if step == 2:
            return (
                f"{head} ist seit dem {faellig} überfällig und laut meinen Unterlagen trotz unserer Zahlungserinnerung noch offen.{ack}\n\n{pay}\n\n"
                f"Sie befinden sich mit der Zahlung im Verzug. Geht der Betrag nicht fristgerecht ein, müssen wir Mahnkosten berechnen."
            )
        return (
            f"trotz zweier Zahlungserinnerungen ist {head}, fällig seit dem {faellig}, laut meinen Unterlagen weiterhin offen.{ack}\n\n{pay}\n\n"
            f"Dies ist unsere letzte Zahlungserinnerung. Geht bis dahin keine Zahlung ein, leiten wir das Mahnverfahren ein. "
            f"Die entstehenden Mahnkosten und Verzugszinsen gehen zu Ihren Lasten."
        )
    if tone == "sachlich":
        if step == 1:
            return (
                f"{head}, fällig am {faellig}, ist laut meinen Unterlagen noch offen.{ack}\n\n{pay}\n\n"
                f"Für Rückfragen zur {t} stehe ich gern zur Verfügung."
            )
        if step == 2:
            return (
                f"{head}, fällig seit dem {faellig}, ist laut meinen Unterlagen trotz unserer Erinnerung noch offen.{ack}\n\n{pay}\n\n"
                f"Sollte die {t} bei Ihnen noch in Prüfung sein oder etwas fehlen, geben Sie mir bitte kurz Bescheid."
            )
        return (
            f"{head} ist seit dem {faellig} fällig und laut meinen Unterlagen trotz zweier Erinnerungen noch offen.{ack}\n\n{pay}\n\n"
            f"Geht bis dahin keine Zahlung ein, leiten wir das Mahnverfahren ein."
        )
    # freundlich
    if step == 1:
        return (
            f"{head} war am {faellig} fällig. {offen}{ack}\n\n{pay}\n\n"
            f"Haben Sie Fragen zur {t}, melden Sie sich gern bei mir."
        )
    if step == 2:
        return (
            f"{head}, fällig seit dem {faellig}, ist laut meinen Unterlagen noch offen.{ack}\n\n{pay}\n\n"
            f"Gibt es einen Grund für die Verzögerung oder eine Frage zur {t}, melden Sie sich bitte kurz. Dann finden wir eine Lösung."
        )
    return (
        f"trotz zweier Erinnerungen ist {head}, fällig seit dem {faellig}, laut meinen Unterlagen noch offen.{ack}\n\n{pay}\n\n"
        f"Danach müssen wir das Mahnverfahren einleiten. Das bedeutet zusätzliche Kosten für Sie und das möchten wir beide vermeiden."
    )


def body_for(doc: Document, step: int, now: datetime | None = None) -> str:
    step = min(max(step, 1), 3)
    text = _offer_text(doc, step) if doc.kind == DocKind.OFFER else _invoice_text(doc, step, now)
    return f"{_anrede(doc)}\n\n{text}\n\n{_link(doc)}{_sig(doc)}"
