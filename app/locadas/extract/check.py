"""Angebots-Check für Endkunden: Ist das Angebot vollständig, klar und fair aufgebaut?

Bewertet bewusst nicht „zu teuer / zu günstig“ (ohne Region, Material und Zustand ist das
Raten), sondern Vollständigkeit, Klarheit, Risiken und welche Fragen der Kunde stellen sollte.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from ..config import settings

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    summary: str = ""
    trade: str = ""
    amount_gross: float | None = None
    score: int = 0  # 0 bis 100, Vollständigkeit/Klarheit
    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    price_note: str = ""
    verdict: str = ""


_PROMPT = """Du bist ein neutraler Berater für Privatkunden, die ein Handwerker-Angebot bekommen haben. Du bewertest NICHT, ob der Preis zu hoch oder zu niedrig ist (das geht ohne Region, Material und Zustand nicht seriös). Du bewertest, ob das Angebot vollständig, klar und fair aufgebaut ist, und was der Kunde vor der Unterschrift klären sollte.

Antworte NUR mit einem JSON-Objekt, ohne Markdown:
{
  "summary": "2 Sätze: Was wird angeboten, von wem, Gesamtbetrag",
  "trade": "Gewerk, z. B. SHK, Elektro, Maler, Dachdecker, GaLaBau, Fliesen, Fenster, sonstiges",
  "amount_gross": Bruttobetrag als Zahl oder null,
  "score": 0-100 (Vollständigkeit und Klarheit: 90+ = alles da, 70-89 = kleine Lücken, 50-69 = wichtige Angaben fehlen, unter 50 = so nicht unterschreiben),
  "present": ["kurze Punkte, was sauber drinsteht, z. B. 'Positionen mit Mengen und Einzelpreisen', 'Gültigkeitsdatum', 'Zahlungsbedingungen'"],
  "missing": ["kurze Punkte, was fehlt oder unklar ist, z. B. 'Keine Angabe, ob Entsorgung enthalten', 'Pauschale ohne Aufschlüsselung', 'Kein Ausführungszeitraum'"],
  "risks": ["konkrete Risiken für den Kunden, z. B. 'Stundenlohnarbeiten nach Aufwand ohne Obergrenze', 'Anzahlung über 30 %', 'Materialpreise mit Änderungsvorbehalt'"],
  "questions": ["3 bis 6 Fragen, die der Kunde dem Betrieb vor der Beauftragung stellen sollte, als direkte Frage formuliert"],
  "price_note": "1-2 Sätze zur Preisstruktur (z. B. Anteil Material/Lohn, Pauschale vs. Positionen, was üblich ist), ohne Urteil über Höhe",
  "verdict": "1 Satz Fazit, freundlich und klar"
}

Angebotstext:
<<<
{text}
>>>"""


def check_offer(text: str) -> CheckResult | None:
    if not settings.anthropic_api_key or not text.strip():
        return None
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return None
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    try:
        resp = client.messages.create(
            model=settings.claude_model,
            max_tokens=1500,
            messages=[{"role": "user", "content": _PROMPT.replace("{text}", text[:20000])}],
        )
    except Exception as exc:
        log.warning("Angebots-Check fehlgeschlagen: %s", exc)
        return None
    raw = "".join(getattr(b, "text", "") for b in resp.content)
    return parse_check_json(raw)


def parse_check_json(raw: str) -> CheckResult | None:
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None

    def lst(key):
        v = d.get(key) or []
        return [str(x) for x in v][:8] if isinstance(v, list) else []

    amount = d.get("amount_gross")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    try:
        score = max(0, min(100, int(d.get("score") or 0)))
    except (TypeError, ValueError):
        score = 0
    return CheckResult(
        summary=str(d.get("summary") or ""),
        trade=str(d.get("trade") or ""),
        amount_gross=amount,
        score=score,
        present=lst("present"),
        missing=lst("missing"),
        risks=lst("risks"),
        questions=lst("questions"),
        price_note=str(d.get("price_note") or ""),
        verdict=str(d.get("verdict") or ""),
    )
