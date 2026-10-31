"""Nachfass-Texte im Stil des Betriebs, per Claude geschrieben und danach hart geprüft.

Claude schreibt nur Anrede und Text. Link und Signatur hängt der Code selbst an, damit beides
wörtlich stimmt. Fällt eine Prüfung durch (zu lang, Zahl erfunden, Gedankenstrich, falsches Du/Sie ...),
wird einmal nachgebessert, sonst gilt die feste Vorlage aus `texts.body_for`. Ein Claude-Fehler
blockiert nie den Versand.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from ..config import settings
from ..db import DocKind, Document
from . import style, texts

log = logging.getLogger(__name__)

MAX_WORDS = 90
MAX_ATTEMPTS = 2

# --- Kunde und Anrede -----------------------------------------------------------

_COMPANY_RE = re.compile(
    r"gmbh|mbh|\bug\b|\bkg\b|\bohg\b|\bag\b|\bgbr\b|e\.\s?k\.|e\.\s?v\.|&|\bund söhne\b|\bco\.|"
    r"verwaltung|immobilien|\bbau\b|\bstadt\b|\bgemeinde\b|\bfirma\b|\bhotel\b|\bpraxis\b|\bgbr\b|\bwohnungs",
    re.I,
)
_FAMILY_RE = re.compile(r"^(?:familie\b|fam\.|eheleute\b|ehepaar\b|herr und frau\b|frau und herr\b)", re.I)
_TITLED_RE = re.compile(r"^(herr|hr\.|frau|fr\.)\s+(.+)$", re.I)
_NAME_PART_RE = re.compile(r"^[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?\.?$")


def customer_kind(name: str) -> str:
    """company | family | titled (Herr/Frau X) | person (Vorname Nachname) | unknown"""
    n = (name or "").strip()
    if not n or "@" in n:
        return "unknown"
    if _COMPANY_RE.search(n):
        return "company"
    if _FAMILY_RE.match(n):
        return "family"
    if _TITLED_RE.match(n):
        return "titled"
    parts = n.split()
    if 2 <= len(parts) <= 4 and all(_NAME_PART_RE.match(p) for p in parts):
        return "person"
    return "unknown"


def uses_du(profile: dict | None, kind: str) -> bool:
    """Duzen nur bei Privatkunden mit Vornamen und nur, wenn der Betrieb eindeutig duzt."""
    return bool(profile) and profile.get("tone") == "du" and kind == "person"


def _du_for(doc: Document, profile: dict | None) -> bool:
    name = (doc.customer_name or "").strip()
    return uses_du(profile, customer_kind(name)) and "." not in name


def anrede(doc: Document, profile: dict | None) -> str:
    name = re.sub(r"\s+", " ", (doc.customer_name or "").strip())
    kind = customer_kind(name)
    g = (profile or {}).get("greeting") or "Guten Tag"
    if g not in style.GREETINGS:
        g = "Guten Tag"
    casual = "Hallo" if g in ("Hi", "Sehr geehrte") else g  # für Anreden mit Namen
    if _du_for(doc, profile):
        first = name.split()[0]
        return f"{g if g in ('Hallo', 'Moin', 'Servus', 'Hi') else 'Hallo'} {first},"
    if kind == "titled":
        m = _TITLED_RE.match(name)
        title = "Herr" if m.group(1).lower().startswith("h") else "Frau"
        full = f"{title} {m.group(2)}"
        if g == "Sehr geehrte":
            return f"{'Sehr geehrter' if title == 'Herr' else 'Sehr geehrte'} {full},"
        return f"{casual} {full},"
    if kind == "family":
        full = re.sub(r"^fam\.\s*", "Familie ", name, flags=re.I)
        if g == "Sehr geehrte" and full.lower().startswith("familie"):
            return f"Sehr geehrte {full},"
        return f"{casual if g != 'Sehr geehrte' else 'Guten Tag'} {full},"
    if kind == "person":
        return f"{'Guten Tag' if g == 'Sehr geehrte' else casual} {name},"
    if g == "Sehr geehrte":
        return "Sehr geehrte Damen und Herren,"
    if g in ("Moin", "Servus", "Grüß Gott"):
        return f"{g},"
    return "Guten Tag,"


# --- Signatur und Link ---------------------------------------------------------------

_SIGNOFF_LINE_RE = re.compile(
    r"^(?:(?:viele|beste|liebe|freundliche|herzliche|schöne|sonnige|mit freundlichen|mit besten|besten)\s+gr[üu](?:ß|ss)e?\b.*|"
    r"gr[üu](?:ß|ss)e?\b.*|gruß.*|lg|vg|bg|mfg|mit freundlichem gruß.*|bis bald.*|danke und gruß.*)[,.!]?$",
    re.I,
)


def _is_signoff(line: str) -> bool:
    return bool(_SIGNOFF_LINE_RE.match(line.strip()))


def signature(doc: Document, profile: dict | None, step: int) -> str:
    b = doc.business
    sign_off = (profile or {}).get("sign_off") or "Viele Grüße"
    if doc.kind == DocKind.INVOICE and step >= 2 and len(sign_off) <= 3:
        sign_off = "Viele Grüße"  # "LG" passt nicht zur zweiten Zahlungserinnerung
    own = (b.signature or "").strip()
    if own:
        first = own.split("\n", 1)[0]
        return own if _is_signoff(first) else f"{sign_off}\n{own}"
    learned = ((profile or {}).get("signature") or "").strip()
    if learned:
        return f"{sign_off}\n{learned}"
    lines = [sign_off, b.owner_name or b.name]
    if b.owner_name and b.name and b.owner_name != b.name:
        lines.append(b.name)
    if b.phone:
        lines.append(b.phone)
    return "\n".join(lines)


def link_block(doc: Document, du: bool) -> str:
    return texts._link(doc, du)


# --- Fakten und Prompt -----------------------------------------------------------------

@dataclass
class Context:
    doc: Document
    step: int
    anrede: str
    du: bool
    facts: dict[str, str]
    profile: dict | None
    allowed_numbers: set[str] = field(default_factory=set)
    required_ref: list[str] = field(default_factory=list)
    term: str = ""
    closing: str = ""
    tone: str = "freundlich"
    opened: bool = False


def _fmt_date(dt) -> str:
    return dt.strftime("%d.%m.%Y") if dt else ""


def build_context(doc: Document, step: int, profile: dict | None, now=None) -> Context:
    du = _du_for(doc, profile)
    term = texts.term(doc)
    facts = {
        "Art des Dokuments (genau dieses Wort verwenden)": term,
        "Kunde": (doc.customer_name or "").strip() or "unbekannt",
        "Nummer": (doc.document_number or "").strip(),
        "Leistung": (doc.title or "").strip(),
        "Betrag": texts._betrag(doc),
        "Verschickt am": _fmt_date(doc.sent_at),
    }
    opened = texts.opened(doc) and doc.kind == DocKind.OFFER
    tone = "freundlich"
    if doc.kind == DocKind.INVOICE:
        tone = texts.invoice_tone(doc)
        facts["Fällig seit"] = _fmt_date(doc.due_date)
        facts["Neue Zahlungsfrist bis"] = _fmt_date(texts.payment_deadline(now))
        if getattr(doc, "ack_at", None):
            facts["Kunde hat den Eingang automatisch bestätigt am"] = _fmt_date(doc.ack_at)
    else:
        if getattr(doc, "valid_until", None):
            facts[f"{term} gültig bis"] = _fmt_date(doc.valid_until)
        if opened:
            facts[f"Kunde hat {texts.der(doc, 'acc')} {term} schon geöffnet (nur zu deiner Info, nie erwähnen)"] = "ja"
    facts = {k: v for k, v in facts.items() if v}
    ctx = Context(doc=doc, step=step, anrede=anrede(doc, profile), du=du, facts=facts, profile=profile, term=term,
                  closing=texts.closing(doc, step, now, du), tone=tone, opened=opened)
    allowed_src = " ".join(facts.values()) + " " + (doc.subject or "")
    ctx.allowed_numbers = set(_NUM_RE.findall(allowed_src)) | set(re.findall(r"\d+", allowed_src))
    if doc.document_number:
        ctx.required_ref = [doc.document_number.strip()]
    elif doc.title:
        ctx.required_ref = [doc.title.strip()]
    return ctx


_INVOICE_TONE = {
    "freundlich": "freundlich und klar, auf Augenhöhe, ohne Entschuldigungen und ohne Ausreden für den Kunden",
    "sachlich": "sachlich und neutral wie unter Geschäftspartnern (B2B), keine Gefühle, keine Floskeln",
    "bestimmt": "bestimmt und deutlich, höflich aber ohne Weichmacher",
}


def step_goal(ctx: Context) -> str:
    t = ctx.term
    d = ctx.doc
    step = min(max(ctx.step, 1), 3)
    if d.kind == DocKind.OFFER:
        if ctx.opened:
            secret = " Niemals erwähnen oder andeuten, dass du weißt, dass er es geöffnet oder angesehen hat."
            return {
                1: f"Erste freundliche Nachricht. Der Kunde hat {texts.der(d, 'acc')} {t} schon geöffnet, er hat also Interesse. Das nicht erwähnen, sondern anbieten, Fragen {texts.zum(d)} {t} zu beantworten oder etwas anzupassen. Nicht nach einer Entscheidung oder Rückmeldung fragen, kein Druck.",
                2: f"Zweite Nachricht. Der Kunde hat {texts.der(d, 'acc')} {t} schon angesehen. Nur Hilfe anbieten (Fragen {texts.zum(d)} {t}, Umfang, Termin) und signalisieren, dass er sich Zeit lassen kann. Ausdrücklich nicht nach einer Entscheidung fragen.",
                3: f"Dritte und ruhige Nachricht. Der Kunde hat {texts.der(d, 'acc')} {t} angesehen. Kurz anbieten, offene Fragen zu klären. Nicht nach einer Entscheidung fragen. Nicht sagen, dass es die letzte Nachricht ist; der Satz, dass du dich nicht weiter meldest, wird automatisch angehängt.",
            }[step] + secret
        return {
            1: f"Erste freundliche Nachfrage. Fragen, ob {texts.der(d)} {t} angekommen ist und ob noch Fragen offen sind. Anbieten, etwas zu erklären oder anzupassen. Kein Druck.",
            2: "Zweite Nachfrage, etwas direkter. Um eine kurze Rückmeldung bitten, auch eine Absage ist in Ordnung, weil der Betrieb planen muss. Gesprächsbereitschaft zu Umfang oder Termin zeigen.",
            3: "Dritte und ruhige Nachricht. Verständnis zeigen, dass es vielleicht gerade nicht passt oder der Kunde sich anders entschieden hat, beides ist in Ordnung. Nicht drängen, nicht sagen, dass es die letzte Nachricht ist, keine Ja/Nein-Formel. Der Satz, dass du dich nicht weiter meldest, wird automatisch angehängt.",
        }[step]
    tone = _INVOICE_TONE.get(ctx.tone, _INVOICE_TONE["freundlich"])
    ack = " Der Kunde hat den Eingang bestätigt, die Rechnung ist also angekommen." if "Kunde hat den Eingang automatisch bestätigt am" in ctx.facts else ""
    offen = f"Der Inhaber hat gerade bestätigt, dass die {t} laut seinen Unterlagen noch offen ist. Sag das neutral, etwa: \"Laut meinen Unterlagen ist die {t} noch offen.\""
    base = {
        1: f"Erste Zahlungserinnerung, Ton {tone}. Die {t} ist seit dem Fälligkeitsdatum fällig. {offen}{ack}",
        2: f"Zweite Zahlungserinnerung, Ton {tone}. Trotz der ersten Erinnerung laut Unterlagen noch offen. {offen}{ack} Bei Fragen zur {t} soll sich der Kunde melden.",
        3: f"Dritte und letzte Zahlungserinnerung, Ton {tone}. Trotz zweier Erinnerungen laut Unterlagen noch offen. Sagen, dass nach Ablauf der Frist das Mahnverfahren eingeleitet wird. Das Wort \"Mahnverfahren\" muss vorkommen.",
    }[step]
    base += " Behaupte nie, du hättest keinen Zahlungseingang gefunden, festgestellt oder gesehen, und schreib nicht, dass keine Zahlung eingegangen sei."
    if ctx.tone == "bestimmt" and step == 2:
        base += " Sagen, dass der Kunde im Zahlungsverzug ist und bei Fristablauf Mahnkosten berechnet werden. Das Wort \"Mahnkosten\" muss vorkommen."
    return base + " Nicht sagen, die Rechnung sei vielleicht untergegangen, und nicht anbieten, die Nachricht zu ignorieren. Schildere nur kurz die Lage und biete an, dass sich der Kunde bei Fragen meldet. Keine eigene Zahlungsaufforderung, keine Frist, keine Bankverbindung und kein Hinweis wie \"siehe unten\": Die Zahlungsaufforderung folgt direkt im automatisch angehängten Absatz."


def build_prompt(ctx: Context, problems: list[str] | None = None) -> str:
    p = ctx.profile or {}
    facts = "\n".join(f"- {k}: {v}" for k, v in ctx.facts.items())
    ref = f'Die Nummer "{ctx.doc.document_number}"' if ctx.doc.document_number else f'Die Leistung "{ctx.doc.title}"'
    style_lines = [
        f"- Ansprache: {'du (der Kunde wird geduzt)' if ctx.du else 'Sie (der Kunde wird gesiezt)'}",
        f"- Ton: {p.get('formality') or 'neutral'}" + (f", {p['notes']}" if p.get("notes") else ""),
    ]
    if p.get("avg_words"):
        style_lines.append(f"- Typische Mail-Länge des Betriebs: etwa {p['avg_words']} Wörter")
    retry = ""
    if problems:
        retry = "\n\nDein letzter Entwurf wurde abgelehnt, weil: " + "; ".join(problems) + ". Schreib ihn neu und vermeide das."
    closing = f"\n- Nach deinem Text hängt der Code diesen Absatz an, wiederhole seinen Inhalt nicht:\n  «{ctx.closing}»" if ctx.closing else ""
    return f"""Du schreibst im Namen eines Handwerksbetriebs eine kurze Nachfass-E-Mail an einen Kunden, im selben Mailverlauf wie das ursprüngliche Dokument.
Ziel dieser Mail: {step_goal(ctx)}

Fakten (nur diese verwenden):
{facts}

Stil des Betriebs:
{chr(10).join(style_lines)}

Regeln:
- Erste Zeile genau: {ctx.anrede}
- Danach eine Leerzeile, dann der Text. Er beginnt klein, weil er nach der Anrede mit Komma weitergeht.
- {ref} muss im Text vorkommen, ebenso das Wort "{ctx.term}". Nenne das Dokument immer "{ctx.term}", nie anders.
- Beträge genau so schreiben wie oben angegeben, mit derselben Währung.
- Höchstens 70 Wörter inklusive Anrede, 2 bis 3 kurze Absätze, in der Ich-Form des Betriebsinhabers.
- Keine Fakten erfinden: keine Daten, Wochentage, Fristen, Preise, Rabatte, Zusagen oder Zahlen, die oben nicht stehen.
- Keine Werbesprache, keine Emojis, höchstens ein Ausrufezeichen, keine Links, keine Platzhalter.
- Keine Gedankenstriche (– oder —) und keine Bindestriche als Einschub. Kein Komma vor "und".
- Keine Grußformel und keine Signatur am Ende, die werden automatisch angehängt, ebenso der Link oder Anhang zum Dokument.{closing}
- Antworte nur mit dem Mailtext, ohne Betreff, ohne Anführungszeichen, ohne Erklärung.{retry}"""


# --- Prüfung -----------------------------------------------------------------------

_NUM_RE = re.compile(r"\d+(?:[.,:/]\d+)*")
_WEEKDAY_MONTH_RE = re.compile(
    r"\b(?:montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonnabend|sonntag|januar|februar|märz|april|mai|juni|juli|"
    r"august|september|oktober|november|dezember|morgen|übermorgen|wochenende|monatsende|jahresende)\b",
    re.I,
)
_SPELLED_PERIOD_RE = re.compile(
    r"\b(?:eine[mnr]?|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn|elf|zwölf|vierzehn)\s+"
    r"(?:werk)?(?:tag|tage|tagen|woche|wochen|monat|monate|monaten)\b",
    re.I,
)
_NEXT_PERIOD_RE = re.compile(r"\bnächste[nmr]?\s+(?:woche|monat)\b|\bbis\s+ende\b", re.I)
_PROMISE_MARKETING_RE = re.compile(
    r"rabatt|skonto|nachlass|sonderpreis|sonderkondition|kostenlos|gratis|garantie|garantiert|verspreche|"
    r"exklusiv|einmalig|unschlagbar|schnäppchen|\baktion|limitiert|nur noch|zögern sie nicht|zögere nicht|"
    r"premium|top-qualität|beste qualität|höchste qualität|erstklassig|rundum-sorglos|win-win|verlänger|"
    r"preisgarantie|bestpreis|vorteil",
    re.I,
)
_OTHER_TERMS = ("angebot", "offerte", "kostenvoranschlag")
_SOFT_INVOICE_RE = re.compile(r"untergegangen|ignorieren|ignorier|übersehen|vergessen haben|betrachten sie diese|gegenstandslos", re.I)
_LAST_MESSAGE_RE = re.compile(r"letzte[nrs]?\s+(?:nachricht|mail|e-mail|mal|erinnerung)|ja, nein oder|ja oder nein", re.I)
_PRESSURE_RE = re.compile(r"entscheid|zusage|noch keine rückmeldung|nichts (?:mehr )?(?:von ihnen |von dir )?gehört|bitte (?:um )?(?:eine )?(?:kurze )?rückmeldung|planen", re.I)
_TRACKING_RE = re.compile(r"geöffnet|angesehen|angeschaut|reingeschaut|durchgesehen|ich sehe,? dass|wie ich sehe|gesehen,? dass|bemerkt", re.I)
# Wir sehen das Konto des Betriebs nicht: keine Behauptung über (fehlende) Zahlungseingänge
_PAYMENT_CLAIM_RE = re.compile(
    r"zahlungseingang|zahlung (?:ist )?(?:bisher |noch )*(?:nicht )?eingegangen|kein geld|"
    r"nicht (?:finden|feststellen|sehen)|(?:finden|feststellen|sehen) (?:konnte|können|kann)|nicht verbucht",
    re.I,
)
_LEGAL_RE = re.compile(r"mahn|inkasso|anwalt|gericht|rechtliche|verzugszins", re.I)
_SIE_FORMS_RE = re.compile(r"\b(?:du|dich|dir|dein|deine|deinen|deinem|deiner|deines|euch|euer|eure)\b", re.I)
_DU_FORMAL_RE = re.compile(r"\b(?:Ihnen|Ihre|Ihren|Ihrem|Ihrer|Ihres)\b|[a-zäöüß,] Sie\b")
_EN_RE = re.compile(r"\b(?:the|and|please|your|regarding|invoice|offer|thanks|best regards)\b", re.I)
_DE_RE = re.compile(
    r"\b(?:ich|wir|und|die|der|das|den|dem|nicht|noch|ist|zu|mit|uns|unser|unsere|unseren|unserem|für|ob|eine?[nmr]?|kurz|"
    r"bei|wenn|schon|habe|hast|haben|bitte|gern|gerne|einfach|auf|sich|auch|oder|wie|kann|können|mir|mich|dir|dich|sie|ihnen)\b",
    re.I,
)


def _has_emoji(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        if 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or cp in (0xFE0F, 0x200D):
            return True
        if unicodedata.category(ch) == "So":
            return True
    return False


def clean_output(raw: str, ctx: Context) -> str:
    """Formales Aufräumen ohne Inhalt zu ändern: Anführungszeichen/Fences weg, Grußformel am Ende weg."""
    text = (raw or "").strip()
    text = re.sub(r"^```\w*\s*|\s*```$", "", text).strip()
    if len(text) >= 2 and text[0] in "\"„“'" and text[-1] in "\"“”'":
        text = text[1:-1].strip()
    text = re.sub(r"[ \t]+\n", "\n", text.replace("\r\n", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    b = ctx.doc.business
    names = {n.strip().lower() for n in (b.owner_name, b.name, b.phone) if n and n.strip()}
    lines = text.split("\n")
    # Grußformel/Name/Telefon am Ende abschneiden, die hängt der Code selbst an
    while len(lines) > 1 and (not lines[-1].strip() or _is_signoff(lines[-1]) or lines[-1].strip().lower() in names):
        lines.pop()
    return _fix_case("\n".join(lines).strip())


# Wörter, die nach "Anrede," klein weitergehen (Substantive bleiben groß)
_LOWER_AFTER_ANREDE = {
    "ich", "wir", "leider", "vielleicht", "zu", "unser", "unsere", "unseren", "unserem", "am", "vor", "mit", "hast", "haben",
    "kurz", "eine", "ein", "die", "der", "das", "bei", "wie", "danke", "gern", "gerne", "nur", "noch", "hier", "heute", "seit",
    "trotz", "falls", "wenn", "ob", "sind", "ist", "konnten", "kannst", "könnten", "darf", "dürfte", "anbei", "nach",
}


def _fix_case(text: str) -> str:
    """Erster Absatz nach der Anrede klein (wenn Funktionswort), alle weiteren Absätze groß."""
    paras = text.split("\n\n")
    for i in range(1, len(paras)):
        p = paras[i]
        if not p:
            continue
        if i == 1:
            first = re.match(r"[A-Za-zÄÖÜäöüß]+", p)
            if first and first.group(0).lower() in _LOWER_AFTER_ANREDE and p[0].isupper():
                paras[i] = p[0].lower() + p[1:]
        elif p[0].islower():
            paras[i] = p[0].upper() + p[1:]
    return "\n\n".join(paras)


def validate(text: str, ctx: Context) -> list[str]:
    problems: list[str] = []
    lines = text.split("\n")
    if not text:
        return ["leer"]
    if lines[0].strip() != ctx.anrede:
        problems.append(f'erste Zeile ist nicht genau "{ctx.anrede}"')
    body = "\n".join(lines[1:]).strip()
    if not body:
        return problems + ["kein Text nach der Anrede"]
    words = len(text.split())
    if words > MAX_WORDS:
        problems.append(f"zu lang ({words} Wörter, erlaubt {MAX_WORDS})")
    if ctx.required_ref and not any(r.lower() in text.lower() for r in ctx.required_ref):
        problems.append(f'"{ctx.required_ref[0]}" fehlt')
    if ctx.term and ctx.term.lower() not in body.lower():
        problems.append(f'Wort "{ctx.term}" fehlt')
    for other in _OTHER_TERMS:
        if other != ctx.term.lower() and other not in ctx.term.lower() and re.search(r"\b" + other + r"\b", body, re.I):
            problems.append(f'"{other.capitalize()}" statt "{ctx.term}"')
            break
    for tok in _NUM_RE.findall(body):
        if tok not in ctx.allowed_numbers:
            problems.append(f'erfundene Zahl "{tok}"')
            break
    money_re = r"€|\beuro\b|\beur\b|\bchf\b|\bfranken\b"
    if re.search(money_re, body, re.I) and not ctx.facts.get("Betrag"):
        problems.append("Betrag erwähnt, obwohl keiner bekannt ist")
    elif ctx.facts.get("Betrag"):
        is_chf = ctx.facts["Betrag"].startswith("CHF")
        if (is_chf and re.search(r"€|\beuro\b|\beur\b", body, re.I)) or (not is_chf and re.search(r"\bchf\b|\bfranken\b", body, re.I)):
            problems.append("falsche Währung")
    if _WEEKDAY_MONTH_RE.search(body) or _NEXT_PERIOD_RE.search(body):
        problems.append("Datum oder Termin erfunden")
    m = _SPELLED_PERIOD_RE.search(body)
    if m:
        problems.append(f'Frist erfunden ("{m.group(0)}")')
    m = _PROMISE_MARKETING_RE.search(body)
    if m:
        problems.append(f'Werbung oder Zusage ("{m.group(0)}")')
    if ctx.doc.kind == DocKind.INVOICE and ctx.step >= 3:
        if "mahnverfahren" not in body.lower():
            problems.append('"Mahnverfahren" fehlt')
    elif ctx.doc.kind == DocKind.INVOICE and ctx.step == 2 and ctx.tone == "bestimmt":
        if "mahnkosten" not in body.lower():
            problems.append('"Mahnkosten" fehlt')
        if re.search(r"inkasso|anwalt|gericht|mahnverfahren", body, re.I):
            problems.append("rechtliche Schritte zu früh")
    elif _LEGAL_RE.search(body):
        problems.append("Mahnung/rechtliche Schritte zu früh")
    if ctx.closing and re.search(r"\bunten\b|nachfolgend|im folgenden|weiter unten", body, re.I):
        problems.append("Verweis auf den angehängten Absatz")
    m = _PAYMENT_CLAIM_RE.search(body) if ctx.doc.kind == DocKind.INVOICE else None
    if m:
        problems.append(f'Behauptung über Zahlungseingang ("{m.group(0)}")')
    m = _SOFT_INVOICE_RE.search(body)
    if ctx.doc.kind == DocKind.INVOICE and m:
        problems.append(f'zu weich ("{m.group(0)}")')
    if ctx.doc.kind == DocKind.OFFER:
        m = _LAST_MESSAGE_RE.search(body)
        if m:
            problems.append(f'klingt nach Drohung ("{m.group(0)}")')
        m = _PRESSURE_RE.search(body) if ctx.opened else None
        if m:
            problems.append(f'drängt, obwohl der Kunde es geöffnet hat ("{m.group(0)}")')
        m = _TRACKING_RE.search(body) if ctx.opened else None
        if m:
            problems.append(f'verrät das Lese-Tracking ("{m.group(0)}")')
    if any(p.strip() and p.strip()[-1] not in ".?!:" for p in body.split("\n\n")):
        problems.append("Satz ohne Satzzeichen am Ende")
    if text.count("!") > 1:
        problems.append("zu viele Ausrufezeichen")
    if _has_emoji(text):
        problems.append("Emoji")
    if "–" in text or "—" in text or re.search(r"\s-\s", text):
        problems.append("Gedankenstrich")
    if re.search(r",\s+und\b", body):
        problems.append('Komma vor "und"')
    if re.search(r"https?://|www\.|@", body):
        problems.append("Link oder Adresse im Text")
    if re.search(r"[\[\]{}<>]|\bXX+\b|\.\.\.|…", body):
        problems.append("Platzhalter")
    if any(_is_signoff(ln) for ln in body.split("\n")):
        problems.append("Grußformel im Text")
    if re.search(r"^\s*betreff\s*:", text, re.I | re.M):
        problems.append("Betreffzeile im Text")
    if ctx.du and _DU_FORMAL_RE.search(body):
        problems.append("siezt statt duzt")
    if not ctx.du and _SIE_FORMS_RE.search(body):
        problems.append("duzt statt siezt")
    if len(_DE_RE.findall(body)) < 3 or len(_EN_RE.findall(body)) >= 2:
        problems.append("nicht deutsch")
    return problems


def assemble(text: str, doc: Document, ctx: Context) -> str:
    closing = f"{ctx.closing}\n\n" if ctx.closing else ""
    return f"{text}\n\n{closing}{link_block(doc, ctx.du)}{signature(doc, ctx.profile, ctx.step)}"


def generate(doc: Document, step: int, profile: dict | None, now=None) -> str | None:
    """Personalisierter Text oder None (dann gilt die Vorlage)."""
    ctx = build_context(doc, step, profile, now)
    problems: list[str] | None = None
    for _ in range(MAX_ATTEMPTS):
        raw = style.claude_text(build_prompt(ctx, problems), max_tokens=4000)  # Denk-Blöcke zählen mit
        if raw is None:
            return None
        text = clean_output(raw, ctx)
        problems = validate(text, ctx)
        if not problems:
            final = assemble(text, doc, ctx)
            link = link_block(doc, ctx.du).strip()
            sig = signature(doc, ctx.profile, step)
            if final.endswith(sig) and (not link or link in final) and len(final) < 2500:
                return final
            problems = ["Aufbau"]
        log.info("Personalisierter Text für Dokument %s Schritt %s verworfen: %s", doc.id, step, "; ".join(problems))
    return None


def body_for(doc: Document, step: int, now=None) -> str:
    """Einstieg für `followups._prepare`: personalisiert, wenn möglich, sonst die feste Vorlage."""
    business = doc.business
    if settings.anthropic_api_key and getattr(business, "personalize", True):
        try:
            profile = style.ensure_profile(business)
            text = generate(doc, step, profile, now)
            if text:
                return text
        except Exception as exc:
            log.warning("Personalisierung fehlgeschlagen, nehme Vorlage: %s", exc)
    return texts.body_for(doc, step, now)
