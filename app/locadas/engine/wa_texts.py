"""Kurze Nachfass-Texte für WhatsApp (der Betrieb schickt sie selbst mit einem Tipp, siehe engine/taps.py).

Höchstens 400 Zeichen, kein Betreff, keine Signatur mit Firma und Telefon: Anrede, zwei bis drei Sätze,
Nummer bzw. Begriff aus dem Dokument, bei Link-Versand eine Zeile mit dem PDF-Link, dann „Viele Grüße“ und
der Name. Personalisiert per Claude (gleiche Prüfungen wie bei Mails plus Längengrenze), sonst die Vorlage.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from ..config import settings
from ..db import DocKind, Document
from . import personalize, style, texts

log = logging.getLogger(__name__)

WA_MAX = 400
WA_MAX_WORDS = 50


# --- Bausteine -------------------------------------------------------------------------

def anrede(doc: Document, profile: dict | None = None) -> str:
    """„Hallo Herr Lange,“ / „Hallo Familie Kurz,“ / „Hallo Lea,“ (nur wenn der Betrieb duzt) / „Hallo,“."""
    name = re.sub(r"\s+", " ", (doc.customer_name or "").strip())
    kind = personalize.customer_kind(name)
    if personalize._du_for(doc, profile):
        return f"Hallo {name.split()[0]},"
    if kind == "titled":
        m = personalize._TITLED_RE.match(name)
        title = "Herr" if m.group(1).lower().startswith("h") else "Frau"
        rest = re.sub(r"^n\s+", "", m.group(2)) if m.group(1).lower() == "herrn" else m.group(2)
        return f"Hallo {title} {rest},"
    if re.match(r"(?i)^herrn\s+\S", name):
        return f"Hallo Herr {name.split(' ', 1)[1]},"
    if kind == "family":
        return "Hallo " + re.sub(r"(?i)^fam\.\s*", "Familie ", name) + ","
    if kind == "person":
        return f"Hallo {name},"
    return "Hallo,"


def sign(doc: Document, du: bool = False) -> str:
    b = doc.business
    who = (b.owner_name or b.name or "").strip()
    if du and b.owner_name:
        who = b.owner_name.split()[0]
    return f"Viele Grüße\n{who}" if who else "Viele Grüße"


def link_line(doc: Document) -> str:
    if texts.delivery(doc) != "link":
        return ""
    return f"{texts.der(doc).capitalize()} {texts.term(doc)} als PDF: {texts.view_url(doc)}"


def _ref(doc: Document, with_title: bool) -> str:
    t = texts.term(doc)
    nr = (doc.document_number or "").strip()
    title = (doc.title or "").strip()
    if nr and title and with_title:
        return f"{t} {nr} ({title})"
    if nr:
        return f"{t} {nr}"
    if title:
        return f"{t} ({title})"
    return t


def pay_line(doc: Document, now: datetime | None = None, du: bool = False, with_iban: bool = True) -> str:
    """Kurze Zahlungsaufforderung: Betrag steht schon davor, deshalb „den Betrag“."""
    datum = texts.payment_deadline(now).strftime("%d.%m.%Y")
    s = f"Bitte {'überweise' if du else 'überweisen Sie'} den Betrag bis {datum}"
    iban = (getattr(doc, "iban", "") or "").strip()
    if iban and with_iban:
        s += f" auf IBAN {iban}"
    if doc.document_number:
        s += f", Verwendungszweck {doc.document_number}"
    return s + "."


def closing(doc: Document, step: int, now: datetime | None = None, du: bool = False, with_iban: bool = True) -> str:
    """Fester Schlusssatz, auch für den personalisierten Text."""
    if doc.kind == DocKind.INVOICE:
        return pay_line(doc, now, du, with_iban)
    if step >= 3:
        return "Ich melde mich nicht weiter, du kannst mir aber jederzeit hier schreiben." if du else \
            "Ich melde mich nicht weiter, Sie können mir aber jederzeit hier schreiben."
    return ""


def assemble(anrede_: str, text: str, doc: Document, close: str, du: bool = False, with_link: bool = True) -> str:
    parts = [anrede_, f"{text} {close}".strip() if close else text]
    link = link_line(doc) if with_link else ""
    if link:
        parts.append(link)
    parts.append(sign(doc, du))
    return "\n\n".join(p for p in parts if p)


# --- Vorlage -----------------------------------------------------------------------------

def _offer(doc: Document, step: int, ref: str) -> str:
    opened = texts.opened(doc)
    zu = f"zu {texts.unser(doc, 'dat')} {ref}"
    if step == 1:
        if opened:
            return f"ich melde mich kurz {zu}. Wenn Sie Fragen haben oder etwas angepasst werden soll, schreiben Sie mir gern hier."
        return (f"ich wollte kurz nachhören, ob {texts.unser(doc)} {ref} gut bei Ihnen angekommen ist. "
                f"Haben Sie noch Fragen dazu? Dann melden Sie sich gern, hier oder telefonisch.")
    if step == 2:
        if opened:
            return f"ich melde mich noch einmal {zu}. Wenn Sie zu Umfang oder Termin etwas besprechen möchten, schreiben Sie mir gern. Lassen Sie sich ruhig Zeit."
        return (f"ich melde mich noch einmal {zu}. Haben Sie sich schon entschieden? "
                f"Eine kurze Rückmeldung hilft mir bei der Planung, auch wenn es nichts wird.")
    return f"{zu} habe ich noch nichts von Ihnen gehört. Das ist völlig in Ordnung, falls es gerade nicht passt."


def _invoice(doc: Document, step: int, ref: str) -> str:
    betrag = texts._betrag(doc)
    b = f" über {betrag}" if betrag else ""
    faellig = doc.due_date.strftime("%d.%m.%Y") if doc.due_date else ""
    seit = f", fällig seit {faellig}," if faellig else ""
    if step == 1:
        war = f" {texts.der(doc).capitalize()} {texts.term(doc)} war am {faellig} fällig und ist laut meinen Unterlagen noch offen." if faellig else \
            f" {texts.der(doc).capitalize()} {texts.term(doc)} ist laut meinen Unterlagen noch offen."
        return f"kurze Erinnerung zu {texts.unser(doc, 'dat')} {ref}{b}.{war}"
    if step == 2:
        return f"{texts.unser(doc)} {ref}{b}{seit} ist laut meinen Unterlagen trotz meiner Erinnerung noch offen. Bei Fragen schreiben Sie mir gern."
    return f"{texts.unser(doc)} {ref}{b}{seit} ist trotz zweier Erinnerungen noch offen. Danach muss ich das Mahnverfahren einleiten."


def _fix(text: str) -> str:
    return re.sub(r",\.", ".", re.sub(r"\s{2,}", " ", text)).strip()


def template_text(doc: Document, step: int, now: datetime | None = None) -> str:
    """Feste Vorlage, garantiert höchstens 400 Zeichen (kürzt erst Titel, dann IBAN, dann den Link)."""
    step = min(max(step, 1), 3)
    a = anrede(doc)
    last = ""
    for with_title, with_iban, with_link in ((True, True, True), (False, True, True), (False, False, True), (False, False, False)):
        ref = _ref(doc, with_title)
        body = _offer(doc, step, ref) if doc.kind == DocKind.OFFER else _invoice(doc, step, ref)
        body = _fix(body)
        if doc.kind == DocKind.INVOICE and step == 3:
            # „Danach“ bezieht sich auf die Frist: Zahlungssatz davor
            first, _, after = body.partition(" Danach")
            text = assemble(a, f"{first} {closing(doc, step, now, False, with_iban)} Danach{after}", doc, "", False, with_link)
        else:
            text = assemble(a, body, doc, closing(doc, step, now, False, with_iban), False, with_link)
        last = text
        if len(text) <= WA_MAX:
            return text
    return last[:WA_MAX - 1].rstrip() + "…"


# --- Personalisiert ------------------------------------------------------------------------

def build_prompt(ctx: personalize.Context, problems: list[str] | None = None) -> str:
    p = ctx.profile or {}
    facts = "\n".join(f"- {k}: {v}" for k, v in ctx.facts.items())
    ref = f'Die Nummer "{ctx.doc.document_number}"' if ctx.doc.document_number else f'Die Leistung "{ctx.doc.title}"'
    retry = ""
    if problems:
        retry = "\n\nDein letzter Entwurf wurde abgelehnt, weil: " + "; ".join(problems) + ". Schreib ihn neu und vermeide das."
    closing_ = f"\n- Nach deinem Text hängt der Code diesen Satz an, wiederhole seinen Inhalt nicht:\n  «{ctx.closing}»" if ctx.closing else ""
    return f"""Du schreibst im Namen eines Handwerksbetriebs eine kurze WhatsApp-Nachricht an einen Kunden. Der Inhaber schickt sie selbst von seinem Handy.
Ziel dieser Nachricht: {personalize.step_goal(ctx)}

Fakten (nur diese verwenden):
{facts}

Stil des Betriebs:
- Ansprache: {'du (der Kunde wird geduzt)' if ctx.du else 'Sie (der Kunde wird gesiezt)'}
- Ton: {p.get('formality') or 'neutral'}, locker wie im Chat, aber höflich

Regeln:
- Erste Zeile genau: {ctx.anrede}
- Danach eine Leerzeile, dann der Text. Er beginnt klein, weil er nach der Anrede mit Komma weitergeht.
- {ref} muss im Text vorkommen, ebenso das Wort "{ctx.term}". Nenne das Dokument immer "{ctx.term}", nie anders.
- Höchstens 40 Wörter inklusive Anrede, zwei bis drei kurze Sätze in einem Absatz, Ich-Form des Inhabers.
- Keine Fakten erfinden: keine Daten, Wochentage, Fristen, Preise, Rabatte, Zusagen oder Zahlen, die oben nicht stehen.
- Keine Emojis, keine Werbesprache, höchstens ein Ausrufezeichen, keine Links, keine Platzhalter, kein Betreff.
- Keine Gedankenstriche (– oder —) und keine Bindestriche als Einschub. Kein Komma vor "und".
- Keine Grußformel und kein Name am Ende, die werden automatisch angehängt, ebenso der Link zum Dokument.{closing_}
- Antworte nur mit dem Nachrichtentext, ohne Anführungszeichen, ohne Erklärung.{retry}"""


def validate(text: str, ctx: personalize.Context) -> list[str]:
    problems = personalize.validate(text, ctx)
    words = len(text.split())
    if words > WA_MAX_WORDS:
        problems.append(f"zu lang ({words} Wörter, erlaubt {WA_MAX_WORDS})")
    if text.count("\n\n") > 1:
        problems.append("mehr als ein Absatz")
    return problems


def generate(doc: Document, step: int, profile: dict | None, now=None) -> str | None:
    ctx = personalize.build_context(doc, step, profile, now)
    ctx.anrede = anrede(doc, profile)
    ctx.closing = closing(doc, step, now, ctx.du)
    problems: list[str] | None = None
    for _ in range(personalize.MAX_ATTEMPTS):
        raw = style.claude_text(build_prompt(ctx, problems), max_tokens=3000)
        if raw is None:
            return None
        text = personalize.clean_output(raw, ctx)
        problems = validate(text, ctx)
        if not problems:
            first, _, rest = text.partition("\n")
            final = assemble(first.strip(), rest.strip(), doc, ctx.closing, ctx.du)
            if len(final) <= WA_MAX:
                return final
            problems = [f"zu lang ({len(final)} Zeichen mit Link und Gruß, erlaubt {WA_MAX})"]
        log.info("WhatsApp-Text für Dokument %s Schritt %s verworfen: %s", doc.id, step, "; ".join(problems))
    return None


def text_for(doc: Document, step: int, now: datetime | None = None) -> str:
    """Einstieg: personalisiert, wenn möglich, sonst die feste Vorlage."""
    business = doc.business
    if settings.anthropic_api_key and getattr(business, "personalize", True):
        try:
            profile = style.ensure_profile(business)
            text = generate(doc, step, profile, now)
            if text:
                return text
        except Exception as exc:
            log.warning("WhatsApp-Text: Personalisierung fehlgeschlagen, nehme Vorlage: %s", exc)
    return template_text(doc, step, now)
