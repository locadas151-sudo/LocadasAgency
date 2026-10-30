"""Erkennen, ob eine gesendete Mail ein Angebot oder eine Rechnung enthält.

Billig und ohne KI: Betreff, Dateiname und die ersten Zeilen des PDF-Texts.
Alles andere (Lieferschein, Mahnung, Auftragsbestätigung, Newsletter) wird ignoriert.
"""
from __future__ import annotations

import re

from ..db import DocKind

_OFFER_WORDS = ("angebot", "kostenvoranschlag", "kva", "offerte", "quote", "quotation", "proposal")
_INVOICE_WORDS = ("rechnung", "invoice", "abschlagsrechnung", "schlussrechnung", "teilrechnung")
# Dokumente, die wie Rechnungen aussehen, aber nicht nachgefasst werden sollen
_EXCLUDE_WORDS = (
    "mahnung", "zahlungserinnerung", "gutschrift", "storno", "lieferschein", "auftragsbestätigung",
    "auftragsbestaetigung", "bestellung", "newsletter", "rechnungskorrektur", "stornorechnung",
    "eingangsrechnung", "ihre rechnung von", "ihre bestellung",
)
_WORD_RE = re.compile(r"[a-zäöüß]+")


def _score(text: str, words: tuple[str, ...]) -> int:
    t = text.lower()
    return sum(1 for w in words if w in t)


def classify(subject: str, filenames: list[str], pdf_text: str) -> DocKind | None:
    header = " ".join([subject] + filenames).lower()
    head_text = (pdf_text or "")[:1500].lower()

    if any(w in header for w in _EXCLUDE_WORDS):
        return None

    offer_h, inv_h = _score(header, _OFFER_WORDS), _score(header, _INVOICE_WORDS)
    if offer_h and not inv_h:
        return DocKind.OFFER
    if inv_h and not offer_h:
        return DocKind.INVOICE
    if offer_h and inv_h:
        # z. B. "Rechnung zu Angebot 123": Dateiname entscheidet, sonst Rechnung
        fn = " ".join(filenames).lower()
        if _score(fn, _OFFER_WORDS) and not _score(fn, _INVOICE_WORDS):
            return DocKind.OFFER
        return DocKind.INVOICE

    # Betreff und Dateiname sagen nichts, PDF-Text anschauen
    if any(w in head_text for w in _EXCLUDE_WORDS):
        return None
    offer_t, inv_t = _score(head_text, _OFFER_WORDS), _score(head_text, _INVOICE_WORDS)
    if offer_t > inv_t:
        return DocKind.OFFER
    if inv_t > offer_t:
        return DocKind.INVOICE
    return None


def looks_like_customer(addr: str, own_addr: str) -> bool:
    """Empfänger plausibel ein Kunde? Eigene Adresse, No-Reply und Großhändler raus."""
    a = addr.lower()
    if not a or a == own_addr.lower():
        return False
    if any(x in a for x in ("noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster")):
        return False
    return True
