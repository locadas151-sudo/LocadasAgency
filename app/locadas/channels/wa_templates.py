"""Unsere Nachfass-Vorlagen, die beim Verbinden im WhatsApp-Konto des Betriebs angelegt werden.

Regeln von Meta, die hier eingehalten werden:
- Variablen {{1}}, {{2}} … fortlaufend, nicht am Anfang oder Ende des Textes, keine Zeilenumbrüche in den Werten.
- Utility nur für nicht werbende Nachrichten zu einem konkreten Vorgang, den der Kunde ausgelöst hat
  (er hat das Angebot angefragt bzw. die Leistung beauftragt). Zahlungserinnerungen sind klar Utility.
  Angebots-Nachfassungen sind ein Grenzfall: Meta kann sie jederzeit auf MARKETING umstufen
  (1 Tag Vorwarnung per Mail und Webhook `template_category_update`). Die tatsächliche Kategorie
  speichern wir in WaTemplate.category und zählen danach ab. Siehe WHATSAPP_COEXISTENCE.md.
- Footer mit Abmeldehinweis („STOP“), den der Webhook auswertet.

Der Name enthält eine Version. Wer einen Text ändert, erhöht die Version, damit Meta neu prüft.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import settings
from ..db import DocKind, Document
from ..engine import texts

FOOTER = "Keine Nachrichten mehr? Antworten Sie mit STOP."
BUTTON_TEXT = "Dokument ansehen"
VERSION = "v1"


@dataclass(frozen=True)
class TemplateDef:
    key: str          # offer_1 … invoice_3
    body: str         # Meta-Text mit {{n}}
    example: tuple[str, ...]
    category: str = "UTILITY"

    @property
    def name(self) -> str:
        return f"locadas_nf_{self.key}_{VERSION}"


_OFFER_EX = ("Familie Schmidt", "Müller Haustechnik", "28.10.2026", "unser Angebot Nr. A-2026-118")
_INV_EX = ("Familie Schmidt", "Müller Haustechnik", "unserer Rechnung Nr. R-2026-301", "1.250,00 €", "14.11.2026")

DEFS: dict[str, TemplateDef] = {d.key: d for d in (
    TemplateDef(
        "offer_1",
        "Guten Tag {{1}}, hier ist {{2}}. Am {{3}} haben wir Ihnen {{4}} geschickt. "
        "Ist alles gut bei Ihnen angekommen? Wenn Sie Fragen dazu haben, antworten Sie einfach auf diese Nachricht.",
        _OFFER_EX,
    ),
    TemplateDef(
        "offer_2",
        "Guten Tag {{1}}, hier ist noch einmal {{2}}. Wir hatten Ihnen am {{3}} {{4}} geschickt. "
        "Gibt es dazu noch offene Fragen oder Änderungswünsche? Antworten Sie gern direkt hier.",
        _OFFER_EX,
    ),
    TemplateDef(
        "offer_3",
        "Guten Tag {{1}}, hier ist {{2}}. Am {{3}} haben wir Ihnen {{4}} geschickt. Wir melden uns dazu ein letztes Mal. "
        "Wenn sich das Thema erledigt hat, ist das völlig in Ordnung. Eine kurze Rückmeldung hilft uns aber bei der Planung.",
        _OFFER_EX,
    ),
    TemplateDef(
        "invoice_1",
        "Guten Tag {{1}}, hier ist {{2}}. Zu {{3}} über {{4}} haben wir noch keinen Zahlungseingang gesehen. "
        "Bitte überweisen Sie den Betrag bis {{5}}. Falls Sie schon bezahlt haben, ist diese Nachricht gegenstandslos.",
        _INV_EX,
    ),
    TemplateDef(
        "invoice_2",
        "Guten Tag {{1}}, hier ist {{2}}. {{3}} über {{4}} ist bei uns weiterhin offen. "
        "Bitte überweisen Sie den Betrag bis {{5}}. Bei Fragen zur Rechnung antworten Sie einfach auf diese Nachricht.",
        ("Familie Schmidt", "Müller Haustechnik", "Unsere Rechnung Nr. R-2026-301", "1.250,00 €", "14.11.2026"),
    ),
    TemplateDef(
        "invoice_3",
        "Guten Tag {{1}}, hier ist {{2}}. {{3}} über {{4}} ist trotz unserer Erinnerungen noch offen. "
        "Bitte überweisen Sie den Betrag bis spätestens {{5}}. Gibt es ein Problem, melden Sie sich bitte kurz hier.",
        ("Familie Schmidt", "Müller Haustechnik", "Unsere Rechnung Nr. R-2026-301", "1.250,00 €", "14.11.2026"),
    ),
)}


def key_for(doc: Document, step: int) -> str:
    kind = "invoice" if doc.kind == DocKind.INVOICE else "offer"
    return f"{kind}_{max(1, min(3, step))}"


def view_base() -> str:
    return settings.view_base_url.rstrip("/") + "/a/"


def create_payload(d: TemplateDef, language: str) -> dict:
    """Body für POST /{waba_id}/message_templates."""
    return {
        "name": d.name,
        "language": language,
        "category": d.category,
        "components": [
            {"type": "BODY", "text": d.body, "example": {"body_text": [list(d.example)]}},
            {"type": "FOOTER", "text": FOOTER},
            {"type": "BUTTONS", "buttons": [{
                "type": "URL", "text": BUTTON_TEXT, "url": view_base() + "{{1}}", "example": [view_base() + "beispiel123"],
            }]},
        ],
    }


# --- Werte für einen konkreten Vorgang --------------------------------------------------

def _name(doc: Document) -> str:
    """„Guten Tag {{1}},“ braucht immer einen Wert: Name aus der Anrede oder neutral."""
    a = texts._anrede(doc).strip().rstrip(",")
    rest = a[len("Guten Tag"):].strip() if a.startswith("Guten Tag") else ""
    return rest or "zusammen"


def _sender(doc: Document) -> str:
    b = doc.business
    if b.owner_name and b.name and b.owner_name != b.name:
        return f"{b.owner_name} von {b.name}"
    return b.name or b.owner_name or "Ihr Handwerksbetrieb"


def _doc_phrase(doc: Document, case: str) -> str:
    nr = f" Nr. {doc.document_number}" if doc.document_number else ""
    return f"{texts.unser(doc, case)} {texts.term(doc)}{nr}"


def params_for(doc: Document, step: int, now=None) -> list[str]:
    key = key_for(doc, step)
    sent = doc.sent_at.strftime("%d.%m.%Y") if doc.sent_at else ""
    if key.startswith("offer"):
        return [_name(doc), _sender(doc), sent or "kürzlich", _doc_phrase(doc, "acc")]
    amount = texts._betrag(doc) or "den offenen Betrag"
    deadline = texts.payment_deadline(now).strftime("%d.%m.%Y")
    phrase = _doc_phrase(doc, "dat") if key == "invoice_1" else _doc_phrase(doc, "nom")
    if key != "invoice_1":
        phrase = phrase[:1].upper() + phrase[1:]
    return [_name(doc), _sender(doc), phrase, amount, deadline]


def render(key: str, params: list[str]) -> str:
    """Text so, wie der Kunde ihn sieht (für Verlauf und Dashboard)."""
    text = DEFS[key].body
    for i, p in enumerate(params, 1):
        text = text.replace("{{%d}}" % i, p)
    return text
