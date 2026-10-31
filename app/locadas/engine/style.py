"""Stilprofil des Betriebs aus seinen eigenen gesendeten Mails.

Beim Scan des Gesendet-Ordners merken wir uns gekürzte Klartexte der letzten Mails an Kunden
(ohne Zitate, max. 15 Stück). Sobald mindestens 3 da sind und das Profil fehlt oder älter als
14 Tage ist, leitet ein Claude-Aufruf daraus ein kleines Profil ab: Du/Sie, Begrüßung, Grußformel,
Signatur, Förmlichkeit. Danach werden die Rohtexte wieder verworfen.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import Business, FollowUp, MailAccount, utcnow
from ..extract.classify import looks_like_customer

log = logging.getLogger(__name__)

MAX_SAMPLES = 15
MIN_SAMPLES = 3
MAX_SAMPLE_CHARS = 1200
MIN_SAMPLE_WORDS = 8
PROFILE_MAX_AGE = timedelta(days=14)

# Begrüßungen, die wir übernehmen. Alles andere wird "Guten Tag".
GREETINGS = ("Guten Tag", "Hallo", "Moin", "Servus", "Grüß Gott", "Sehr geehrte", "Hi")
TONES = ("du", "sie", "gemischt")
FORMALITY = ("locker", "neutral", "förmlich")

_QUOTE_START = re.compile(
    r"^\s*(?:"
    r">|"
    r"-{2,}\s*(?:Ursprüngliche Nachricht|Original Message|Weitergeleitete Nachricht)|"
    r"_{5,}|"
    r"Am .{4,120}(?:schrieb|wrote).*:\s*$|"
    r"On .{4,120}wrote:\s*$|"
    r"(?:Von|From):\s.+$"
    r")",
    re.I,
)


def clean_body(text: str) -> str:
    """Nur den selbst geschriebenen Teil: Zitate/weitergeleitete Mails ab, Leerzeilen zusammen, gekürzt."""
    lines: list[str] = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if _QUOTE_START.match(line):
            break
        lines.append(line.rstrip())
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out[:MAX_SAMPLE_CHARS]


def _load_list(raw: str) -> list:
    try:
        data = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def load_profile(business: Business) -> dict | None:
    try:
        data = json.loads(business.style_json) if business.style_json else None
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def collect_sample(session: Session, account: MailAccount, mail) -> None:
    """Hook aus dem Scanner: gesendete Mail an einen Kunden als Stilprobe merken. Wirft nie."""
    try:
        _collect(session, account, mail)
    except Exception as exc:  # pragma: no cover - Stil ist nie wichtiger als der Scan
        log.warning("Stilprobe übersprungen: %s", exc)


def _collect(session: Session, account: MailAccount, mail) -> None:
    own_domain = account.email.split("@")[-1].lower()
    customers = [a for _, a in mail.to if looks_like_customer(a, account.email) and a.lower().split("@")[-1] != own_domain]
    if not customers:
        return
    # Unsere eigenen Nachfass-Mails nicht als Stil des Betriebs lernen
    if mail.message_id and session.scalar(select(FollowUp.id).where(FollowUp.message_id == mail.message_id)) is not None:
        return
    body = clean_body(mail.text)
    if len(body.split()) < MIN_SAMPLE_WORDS:
        return
    business = account.business
    samples = _load_list(business.style_samples_json)
    if body in samples:
        return
    samples.append(body)
    business.style_samples_json = json.dumps(samples[-MAX_SAMPLES:], ensure_ascii=False)


def needs_update(business: Business, now=None) -> bool:
    now = now or utcnow()
    if len(_load_list(business.style_samples_json)) < MIN_SAMPLES:
        return False
    if not business.style_json or business.style_updated_at is None:
        return True
    return now - business.style_updated_at >= PROFILE_MAX_AGE


_PROFILE_PROMPT = """Hier sind E-Mails, die ein deutscher Handwerksbetrieb selbst an seine Kunden geschrieben hat (nur der selbst geschriebene Teil).
Beschreibe den Schreibstil des Betriebs. Antworte NUR mit einem JSON-Objekt, ohne Erklärung, ohne Markdown:

{
  "anrede": "du" wenn der Betrieb Kunden fast immer duzt, "sie" wenn er fast immer siezt, sonst "gemischt",
  "greeting": typische Begrüßung, genau eine von: "Guten Tag", "Hallo", "Moin", "Servus", "Grüß Gott", "Sehr geehrte", "Hi",
  "sign_off": typische Grußformel vor dem Namen genau wie in den Mails, z. B. "Viele Grüße", "Beste Grüße", "LG", "Mit freundlichen Grüßen",
  "signature": Signaturblock nach der Grußformel (Name, Firma, Telefon), Zeilen mit \\n getrennt, wörtlich aus den Mails übernommen, höchstens 5 Zeilen, sonst "",
  "formality": "locker", "neutral" oder "förmlich",
  "avg_words": ungefähre Wortzahl einer typischen Mail ohne Signatur als Zahl,
  "notes": "ein kurzer Satz zum Ton, z. B. kurze Sätze, direkt, freundlich"
}

Erfinde nichts. Wenn etwas nicht eindeutig ist, nimm "Guten Tag", "Viele Grüße", "sie" bzw. "".

E-Mails:
{samples}"""


def claude_text(prompt: str, max_tokens: int) -> str | None:
    """Ein Claude-Aufruf, Text zurück oder None. Kein temperature-Parameter (aktuelle Modelle lehnen ihn ab)."""
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return None
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=40.0, max_retries=1)
        resp = client.messages.create(
            model=settings.claude_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.warning("Claude-Aufruf fehlgeschlagen: %s", exc)
        return None
    return "".join(getattr(b, "text", "") or "" for b in resp.content).strip()


def parse_profile(raw: str, samples: list[str]) -> dict | None:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    tone = str(data.get("anrede") or "").lower().strip()
    greeting = str(data.get("greeting") or "").strip()
    greeting = next((g for g in GREETINGS if g.lower() == greeting.lower()), "Guten Tag")
    sign_off = str(data.get("sign_off") or "").strip().rstrip(",")
    if not sign_off or len(sign_off) > 40 or len(sign_off.split()) > 4 or re.search(r"[\d@:/]", sign_off):
        sign_off = "Viele Grüße"
    # Signatur nur übernehmen, wenn jede Zeile wörtlich in einer Probe steht (nichts Ausgedachtes)
    sig_lines = [ln.strip() for ln in str(data.get("signature") or "").split("\n") if ln.strip()]
    corpus = "\n".join(samples)
    if not sig_lines or len(sig_lines) > 5 or any(ln not in corpus for ln in sig_lines) or len("\n".join(sig_lines)) > 300:
        sig_lines = []
    formality = str(data.get("formality") or "").lower().strip()
    try:
        avg_words = int(float(data.get("avg_words") or 0))
    except (TypeError, ValueError):
        avg_words = 0
    notes = re.sub(r"\s+", " ", str(data.get("notes") or ""))[:200]
    return {
        "tone": tone if tone in TONES else "sie",
        "greeting": greeting,
        "sign_off": sign_off,
        "signature": "\n".join(sig_lines),
        "formality": formality if formality in FORMALITY else "neutral",
        "avg_words": max(0, min(avg_words, 400)),
        "notes": notes,
        "samples": len(samples),
    }


def derive_profile(business: Business, now=None) -> dict | None:
    """Ein Claude-Aufruf aus den gesammelten Proben. Bei Erfolg Profil speichern und Rohtexte verwerfen."""
    samples = _load_list(business.style_samples_json)[-MAX_SAMPLES:]
    if len(samples) < MIN_SAMPLES:
        return None
    blocks = "\n\n".join(f"--- Mail {i} ---\n{s}" for i, s in enumerate(samples, start=1))
    raw = claude_text(_PROFILE_PROMPT.replace("{samples}", blocks), max_tokens=2000)
    if raw is None:
        return None
    profile = parse_profile(raw, samples)
    if profile is None:
        log.warning("Stilprofil für Betrieb %s nicht lesbar", business.id)
        return None
    business.style_json = json.dumps(profile, ensure_ascii=False)
    business.style_updated_at = now or utcnow()
    business.style_samples_json = ""
    return profile


def ensure_profile(business: Business) -> dict | None:
    """Aktuelles Profil, bei Bedarf vorher neu ableiten. Wirft nie."""
    try:
        if needs_update(business):
            derive_profile(business)
    except Exception as exc:  # pragma: no cover
        log.warning("Stilprofil-Ableitung fehlgeschlagen: %s", exc)
    return load_profile(business)
