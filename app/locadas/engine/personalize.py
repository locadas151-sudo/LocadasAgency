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
PAYMENT_DAYS = 7  # Frist in Schritt 2 und 3 bei Rechnungen, wie in den Vorlagen

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
    link = texts._link(doc)
    if link and du:
        link = link.replace("Hier können Sie", "Hier kannst du")
    return link


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


def _fmt_date(dt) -> str:
    return dt.strftime("%d.%m.%Y") if dt else ""


def build_context(doc: Document, step: int, profile: dict | None) -> Context:
    du = _du_for(doc, profile)
    facts = {
        "Art": doc.kind_label,
        "Kunde": (doc.customer_name or "").strip() or "unbekannt",
        "Nummer": (doc.document_number or "").strip(),
        "Leistung": (doc.title or "").strip(),
        "Betrag": texts._betrag(doc),
        "Verschickt am": _fmt_date(doc.sent_at),
    }
    if doc.kind == DocKind.INVOICE:
        facts["Fällig seit"] = _fmt_date(doc.due_date)
        if step >= 2:
            facts["Neue Zahlungsfrist"] = f"{PAYMENT_DAYS} Tage"
    elif doc.valid_until:
        facts["Angebot gültig bis"] = _fmt_date(doc.valid_until)
    facts = {k: v for k, v in facts.items() if v}
    ctx = Context(doc=doc, step=step, anrede=anrede(doc, profile), du=du, facts=facts, profile=profile)
    allowed_src = " ".join(facts.values()) + " " + (doc.subject or "")
    ctx.allowed_numbers = set(_NUM_RE.findall(allowed_src)) | set(re.findall(r"\d+", allowed_src))
    if doc.document_number:
        ctx.required_ref = [doc.document_number.strip()]
    elif doc.title:
        ctx.required_ref = [doc.title.strip()]
    return ctx


_STEP_GOALS = {
    (DocKind.OFFER, 1): "Erste freundliche Nachfrage. Fragen, ob das Angebot angekommen ist und ob noch Fragen offen sind. Anbieten, etwas zu erklären oder anzupassen. Kein Druck.",
    (DocKind.OFFER, 2): "Zweite Nachfrage, etwas direkter. Um eine kurze Rückmeldung oder Entscheidung bitten, auch eine Absage ist in Ordnung, weil der Betrieb planen muss. Gesprächsbereitschaft zu Umfang oder Termin zeigen.",
    (DocKind.OFFER, 3): "Letzte höfliche Nachricht zu diesem Angebot. Ausdrücklich sagen, dass dies die letzte Nachricht dazu ist. Um eine kurze Antwort bitten (Ja, Nein oder später). Nicht drängen.",
    (DocKind.INVOICE, 1): "Freundliche Zahlungserinnerung. Die Rechnung ist seit dem Fälligkeitsdatum offen, vielleicht ist sie untergegangen. Bitten, den Betrag in den nächsten Tagen zu überweisen. Falls schon bezahlt, die Nachricht einfach ignorieren.",
    (DocKind.INVOICE, 2): f"Zweite Erinnerung, klar und sachlich. Noch kein Zahlungseingang. Bitten, innerhalb von {PAYMENT_DAYS} Tagen zu überweisen. Bei Fragen zur Rechnung oder Problemen soll sich der Kunde melden.",
    (DocKind.INVOICE, 3): f"Dritte und letzte Erinnerung, bestimmt aber höflich. Trotz zweier Erinnerungen noch offen. Bitten, innerhalb von {PAYMENT_DAYS} Tagen zu überweisen, sonst wird das Mahnverfahren eingeleitet. Das Wort \"Mahnverfahren\" muss vorkommen.",
}


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
    return f"""Du schreibst im Namen eines deutschen Handwerksbetriebs eine kurze Nachfass-E-Mail an einen Kunden, als Antwort im selben Mailverlauf.
Ziel dieser Mail: {_STEP_GOALS[(ctx.doc.kind, min(max(ctx.step, 1), 3))]}

Fakten (nur diese verwenden):
{facts}

Stil des Betriebs:
{chr(10).join(style_lines)}

Regeln:
- Erste Zeile genau: {ctx.anrede}
- Danach eine Leerzeile, dann der Text. Er beginnt klein, weil er nach der Anrede mit Komma weitergeht.
- {ref} muss im Text vorkommen.
- Höchstens 70 Wörter inklusive Anrede, 2 bis 3 kurze Absätze, in der Ich-Form des Betriebsinhabers.
- Keine Fakten erfinden: keine Daten, Wochentage, Fristen, Preise, Rabatte, Zusagen oder Zahlen, die oben nicht stehen.
- Keine Werbesprache, keine Emojis, höchstens ein Ausrufezeichen, keine Links, keine Platzhalter.
- Keine Gedankenstriche (– oder —) und keine Bindestriche als Einschub. Kein Komma vor "und".
- Keine Grußformel und keine Signatur am Ende, die werden automatisch angehängt, ebenso ein Link zum Dokument.
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
    for tok in _NUM_RE.findall(body):
        if tok not in ctx.allowed_numbers:
            problems.append(f'erfundene Zahl "{tok}"')
            break
    if re.search(r"€|\beuro\b|\beur\b", body, re.I) and not ctx.facts.get("Betrag"):
        problems.append("Betrag erwähnt, obwohl keiner bekannt ist")
    if _WEEKDAY_MONTH_RE.search(body) or _NEXT_PERIOD_RE.search(body):
        problems.append("Datum oder Termin erfunden")
    m = _SPELLED_PERIOD_RE.search(body)
    if m and not (ctx.facts.get("Neue Zahlungsfrist") and m.group(0).lower().startswith("sieben")):
        problems.append(f'Frist erfunden ("{m.group(0)}")')
    m = _PROMISE_MARKETING_RE.search(body)
    if m:
        problems.append(f'Werbung oder Zusage ("{m.group(0)}")')
    if ctx.doc.kind == DocKind.INVOICE and ctx.step >= 3:
        if "mahnverfahren" not in body.lower():
            problems.append('"Mahnverfahren" fehlt')
    elif _LEGAL_RE.search(body):
        problems.append("Mahnung/rechtliche Schritte zu früh")
    if ctx.doc.kind == DocKind.OFFER and ctx.step >= 3 and "letzt" not in body.lower():
        problems.append("nicht als letzte Nachricht erkennbar")
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
    return f"{text}\n\n{link_block(doc, ctx.du)}{signature(doc, ctx.profile, ctx.step)}"


def generate(doc: Document, step: int, profile: dict | None) -> str | None:
    """Personalisierter Text oder None (dann gilt die Vorlage)."""
    ctx = build_context(doc, step, profile)
    problems: list[str] | None = None
    for _ in range(MAX_ATTEMPTS):
        raw = style.claude_text(build_prompt(ctx, problems), max_tokens=500)
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


def body_for(doc: Document, step: int) -> str:
    """Einstieg für `followups._prepare`: personalisiert, wenn möglich, sonst die feste Vorlage."""
    business = doc.business
    if settings.anthropic_api_key and getattr(business, "personalize", True):
        try:
            profile = style.ensure_profile(business)
            text = generate(doc, step, profile)
            if text:
                return text
        except Exception as exc:
            log.warning("Personalisierung fehlgeschlagen, nehme Vorlage: %s", exc)
    return texts.body_for(doc, step)
