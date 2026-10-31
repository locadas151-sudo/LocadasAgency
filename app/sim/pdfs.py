"""Realistische Angebots- und Rechnungs-PDFs in verschiedenen Layouts (Lexware, sevDesk, Word, Hero, Plancraft, Scan …)."""
from __future__ import annotations

import io
from dataclasses import dataclass, field

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def de(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def ch(v: float) -> str:
    return f"{v:,.2f}".replace(",", "'")


@dataclass
class Doc:
    kind: str                      # "Angebot" | "Rechnung" | "Kostenvoranschlag" | "Abschlagsrechnung"
    number: str
    date: str                      # "21.10.2026"
    issuer: list[str]              # Briefkopf
    recipient: list[str]           # Anschrift
    positions: list[tuple[str, float, str, float]]  # (Text, Menge, Einheit, Einzelpreis netto)
    style: str = "lexware"         # lexware | sevdesk | word | hero | plancraft | kleinunternehmer | abschlag | gu | swiss | scan
    terms: list[str] = field(default_factory=list)
    vat: float = 0.19
    extra: dict = field(default_factory=dict)

    @property
    def net(self) -> float:
        return round(sum(q * p for _, q, _, p in self.positions), 2)

    @property
    def gross(self) -> float:
        if self.style in ("kleinunternehmer",):
            return self.net
        if self.style == "swiss":
            return round(self.net * 1.081, 2)
        return round(self.net * (1 + self.vat), 2)

    @property
    def payable(self) -> float:
        if self.style == "abschlag":
            return round(self.gross * self.extra.get("share", 0.3), 2)
        if self.style == "gu":
            return round(self.gross * 0.95, 2)
        if self.style == "word":
            return float(round(self.gross))
        return self.gross


def _lines(d: Doc) -> list[str]:
    L: list[str] = []
    L += d.issuer + [""]
    L += d.recipient + [""]
    title = d.kind.upper() if d.style in ("word", "kleinunternehmer") else d.kind
    if d.style == "plancraft":
        L += [f"{title} {d.number}", f"Datum: {d.date}", "Bauvorhaben: " + d.extra.get("bv", "Gartenneugestaltung"), ""]
    elif d.style == "word":
        L += [f"{title} Nr. {d.number}", f"{d.extra.get('ort', 'Essen')}, den {d.date}", ""]
    else:
        L += [f"{title} Nr. {d.number}", f"{'Rechnungsdatum' if 'echnung' in d.kind else 'Datum'}: {d.date}", ""]
    if d.style != "word":
        L.append("Pos.  Bezeichnung                                Menge  Einheit  Einzelpreis  Gesamt")
    for i, (t, q, u, p) in enumerate(d.positions, 1):
        cur = "CHF " if d.style == "swiss" else ""
        if d.style == "word":
            L.append(f"{i}. {t}  {q:g} {u} à {de(p)} €  =  {de(q * p)} €")
        elif d.style == "swiss":
            L.append(f"{i:>3}   {t:<40} {q:g} {u}  {cur}{ch(p)}  {cur}{ch(q * p)}")
        else:
            L.append(f"{i:>3}   {t:<40} {q:g} {u}  {de(p)} €  {de(q * p)} €")
    L.append("")
    s = d.style
    if s == "lexware":
        L += [f"Nettobetrag {de(d.net)} €", f"zzgl. 19 % USt. {de(d.gross - d.net)} €", f"Gesamtbetrag {de(d.gross)} €"]
    elif s == "sevdesk":
        L += [f"Gesamt netto {de(d.net)} EUR", f"Umsatzsteuer 19 % {de(d.gross - d.net)} EUR", f"Gesamt brutto {de(d.gross)} EUR"]
    elif s == "hero":
        L += [f"Summe netto {de(d.net)} €", f"MwSt. 19 % {de(d.gross - d.net)} €", f"Angebotssumme {de(d.gross)} €"]
    elif s == "plancraft":
        L += [f"Zwischensumme netto {de(d.net)} €", f"19 % MwSt. {de(d.gross - d.net)} €", f"Gesamtsumme {de(d.gross)} €"]
    elif s == "word":
        L += [f"Summe netto: {de(d.net)} €", f"+ 19% MwSt: {de(d.gross - d.net)} €", f"Summe: {de(round(d.gross)).split(',')[0]},- €"]
    elif s == "kleinunternehmer":
        L += [f"Endbetrag {de(d.gross)} €", "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet."]
    elif s == "abschlag":
        share = d.extra.get("share", 0.3)
        L += [f"Auftragssumme brutto {de(d.gross)} €", f"{int(share * 100)} % Abschlag laut Zahlungsplan", f"Zahlbetrag {de(d.payable)} €"]
    elif s == "gu":
        L += [f"Nettobetrag {de(d.net)} €", f"Umsatzsteuer: Steuerschuldnerschaft des Leistungsempfängers (§ 13b UStG)" if d.vat == 0 else f"zzgl. 19 % USt. {de(d.gross - d.net)} €",
              f"Rechnungsbetrag {de(d.gross)} €", f"abzgl. 5 % Sicherheitseinbehalt {de(d.gross * 0.05)} €", f"Zahlbetrag {de(d.payable)} €"]
    elif s == "swiss":
        L += [f"Total exkl. MWST CHF {ch(d.net)}", f"MWST 8.1 % CHF {ch(d.gross - d.net)}", f"Total inkl. MWST CHF {ch(d.gross)}"]
    L.append("")
    L += d.terms
    L += ["", d.extra.get("footer", "Bankverbindung: Sparkasse · IBAN DE89 3704 0044 0532 0130 00 · BIC COBADEFFXXX · USt-IdNr. DE123456789")]
    return L


def render(d: Doc) -> bytes:
    lines = _lines(d)
    if d.style == "scan":
        return _scan_pdf(lines)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 9.5)
    y = 805
    for line in lines:
        if y < 50:
            c.showPage()
            c.setFont("Helvetica", 9.5)
            y = 805
        c.drawString(48, y, line)
        y -= 14
    c.save()
    return buf.getvalue()


def _scan_pdf(lines: list[str]) -> bytes:
    """Bild-PDF ohne Textebene, wie aus dem Kopierer."""
    from PIL import Image, ImageDraw

    img = Image.new("L", (1240, 1754), 245)
    dr = ImageDraw.Draw(img)
    y = 60
    for line in lines:
        dr.text((70, y), line, fill=20)
        y += 26
    out = io.BytesIO()
    img.save(out, "PDF", resolution=150)
    return out.getvalue()


def agb_pdf(company: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for page in range(3):
        y = 800
        c.drawString(48, y, f"Allgemeine Geschäftsbedingungen {company} (Seite {page + 1})")
        for i in range(48):
            y -= 15
            c.drawString(48, y, f"§ {page * 48 + i + 1} Die Vergütung richtet sich nach dem vereinbarten Leistungsumfang und der VOB/B in der jeweils gültigen Fassung.")
        c.showPage()
    c.save()
    return buf.getvalue()


def datasheet_pdf(product: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawString(48, 800, f"Technisches Datenblatt {product}")
    for i in range(30):
        c.drawString(48, 780 - i * 15, f"Kennwert {i}: {i * 3.7:.1f} kW / Schutzart IP{20 + i % 50}")
    c.save()
    return buf.getvalue()
