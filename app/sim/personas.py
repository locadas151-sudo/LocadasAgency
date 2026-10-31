"""20 simulierte Betriebe mit realistischen Abläufen. Jeder hat Mails im Gesendet-Ordner und Ereignisse im Posteingang.

Erwartung pro Mail: `expect=None` heißt „darf NICHT als Dokument auftauchen“.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .pdfs import Doc, agb_pdf, datasheet_pdf, render


@dataclass
class Sent:
    days_ago: float
    to: list[tuple[str, str]]
    subject: str
    body: str = "Guten Tag,\n\nanbei wie besprochen.\n\nViele Grüße"
    docs: list[Doc] = field(default_factory=list)
    extra_pdfs: list[tuple[str, bytes]] = field(default_factory=list)
    filenames: list[str] = field(default_factory=list)
    expect: dict | None = None          # {"kind": "offer"|"invoice", "number": str, "amount": float, "customer": email}
    in_reply_to_doc: str | None = None  # Nummer eines Dokuments, auf dessen Mail geantwortet wird (manuelles Nachfassen)
    key: str = ""


@dataclass
class Event:
    day: int                   # Simulationstag ab Verbinden (0 = heute)
    kind: str                  # reply | autoreply | bounce | manual | sent
    doc: str = ""              # Dokumentnummer, auf die sich das Ereignis bezieht
    from_addr: str = ""
    subject: str = ""
    body: str = ""
    sent: Sent | None = None   # bei kind="sent": neue Mail im Gesendet-Ordner
    expect_status: str | None = None  # erwarteter Status des Dokuments nach dem Ereignis


@dataclass
class Persona:
    key: str
    business: str
    owner: str
    trade: str
    size: str
    email: str
    phone: str
    workflow: str
    character: str
    sent_folder: str = "Gesendet"
    sent_flag: bool = True
    sent: list[Sent] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    # Wie er Freigaben entscheidet: Nummer -> "send" | "stop" | "skip"; Default send (bei needs_review-Rechnungen "stop")
    decisions: dict[str, str] = field(default_factory=dict)
    note: str = ""


def _hdr(name, street, city, phone):
    return [name, street, city, f"Tel. {phone}"]


def _exp(kind, d: Doc, customer, amount=None):
    return {"kind": kind, "number": d.number, "amount": round(amount if amount is not None else d.payable, 2), "customer": customer}


def build() -> list[Persona]:
    P: list[Persona] = []

    # 1 SHK, 10 Leute, Büro mit Lexware, eigene Domain bei IONOS
    h = _hdr("Selbmann Haustechnik GmbH", "Industriestr. 4", "41063 Mönchengladbach", "02161 4455")
    a1 = Doc("Angebot", "AN-2026-0412", "27.10.2026", h, ["Familie Brandt", "Lindenweg 3", "41065 Mönchengladbach"],
             [("Gas-Brennwerttherme inkl. Montage", 1, "St", 4890), ("Abgasanlage", 1, "Psch", 980), ("Demontage/Entsorgung Altgerät", 1, "Psch", 340)],
             "lexware", ["Dieses Angebot ist gültig bis 30.11.2026."])
    r1 = Doc("Rechnung", "RE-2026-0877", "06.10.2026", h, ["Herr Klaus Peters", "Ahornstr. 11", "41061 Mönchengladbach"],
             [("Wartung Gastherme", 1, "St", 165), ("Kleinmaterial", 1, "Psch", 23.5)], "lexware", ["Zahlbar innerhalb von 14 Tagen ohne Abzug."])
    r2 = Doc("Rechnung", "RE-2026-0901", "29.10.2026", h, ["WEG Gartenstraße 12", "c/o Hausverwaltung Kraus", "41061 Mönchengladbach"],
             [("Austausch Steigleitung", 14, "m", 89), ("Arbeitszeit Monteur", 9, "Std", 68)], "lexware", ["Zahlbar bis 12.11.2026 ohne Abzug."])
    P.append(Persona(
        "shk", "Selbmann Haustechnik GmbH", "Julius Selbmann", "SHK", "10 Leute, Büro", "info@selbmann-haustechnik.de", "02161 4455",
        "Büro schreibt in Lexware, verschickt als PDF über Outlook am PC (IONOS-Postfach). Inhaber sieht Angebote danach nie wieder.",
        "Skeptisch, will auf keinen Fall Verantwortung abgeben, sagt erst mal 'alles gut'.",
        sent_folder="Gesendete Objekte", sent_flag=False,
        sent=[
            Sent(3, [("Familie Brandt", "brandt.familie@t-online.de")], "Ihr Angebot AN-2026-0412 Heizungstausch", docs=[a1], expect=_exp("offer", a1, "brandt.familie@t-online.de")),
            Sent(24, [("Klaus Peters", "k.peters@gmx.de")], "Rechnung RE-2026-0877", docs=[r1], expect=_exp("invoice", r1, "k.peters@gmx.de")),
            Sent(1, [("Hausverwaltung Kraus", "technik@hv-kraus.de")], "Rechnung RE-2026-0901 WEG Gartenstraße 12", docs=[r2], expect=_exp("invoice", r2, "technik@hv-kraus.de")),
            Sent(2, [("Kanzlei Dr. Hoff", "belege@stb-hoff.de")], "WG: Rechnung Großhandel Oktober", docs=[Doc("Rechnung", "GH-55120", "28.10.2026", ["Richter+Frenzel"], ["Selbmann Haustechnik"], [("Material", 1, "Psch", 2400)])], expect=None),
            Sent(1, [("Marco", "marco@selbmann-haustechnik.de")], "Angebot Brandt zur Info", docs=[a1], expect=None, key="kollege"),
        ],
        events=[Event(9, "reply", "AN-2026-0412", "brandt.familie@t-online.de", "AW: Ihr Angebot AN-2026-0412 Heizungstausch", "Hallo Herr Selbmann, wir machen das. Wann können Sie anfangen?", expect_status="replied")],
        decisions={"RE-2026-0877": "stop"},
        note="Alte Rechnung RE-0877 ist längst bezahlt. Weiterleitung an Steuerberater und Kopie an Kollegen dürfen nicht auftauchen.",
    ))

    # 2 GaLaBau, 10 Leute, Plancraft
    h = _hdr("Landschaft Kreativ GmbH", "Am Kamp 7", "47803 Krefeld", "02151 77880")
    a2 = Doc("Angebot", "A-26-1187", "22.10.2026", h, ["Dr. Sabine Wolff", "Uerdinger Str. 210", "47800 Krefeld"],
             [("Rollrasen inkl. Bodenvorbereitung", 180, "m²", 14.5), ("Natursteinterrasse Basalt", 42, "m²", 189), ("Hochbeete Cortenstahl", 3, "St", 1150), ("Bewässerungsanlage", 1, "Psch", 2900)],
             "plancraft", ["Bindefrist: 30 Tage ab Angebotsdatum."], extra={"bv": "Gartenneugestaltung Wolff"})
    a3 = Doc("Angebot", "A-26-1203", "29.10.2026", h, ["Firma Rheinlogistik GmbH", "Hafenstr. 3", "47809 Krefeld"],
             [("Grünflächenpflege Jahresvertrag", 12, "Mon", 640)], "plancraft", ["Gültig bis 31.12.2026."], extra={"bv": "Pflegevertrag Rheinlogistik"})
    P.append(Persona(
        "galabau", "Landschaft Kreativ GmbH", "Tim Albers", "Garten- und Landschaftsbau", "10 Leute", "info@landschaft-kreativ.de", "02151 77880",
        "Projektleiter erstellt Angebote in Plancraft, schickt sie aus dem eigenen Postfach (Strato). Große Gartenprojekte, Kunden brauchen Wochen.",
        "Kritisch gegenüber 'Tools', will nicht wachsen, sondern mit gleichem Team mehr schaffen. Hasst aufdringliche Kommunikation.",
        sent=[
            Sent(9, [("Sabine Wolff", "s.wolff@web.de")], "Angebot Gartenneugestaltung", docs=[a2], expect=_exp("offer", a2, "s.wolff@web.de")),
            Sent(2, [("Rheinlogistik Einkauf", "einkauf@rheinlogistik.de")], "Angebot Pflegevertrag 2027", docs=[a3], expect=_exp("offer", a3, "einkauf@rheinlogistik.de")),
        ],
        events=[
            Event(1, "autoreply", "A-26-1203", "einkauf@rheinlogistik.de", "Abwesenheitsnotiz: Angebot Pflegevertrag 2027", "Ich bin bis 15.11. nicht im Haus.", expect_status="open"),
            Event(12, "reply", "A-26-1203", "einkauf@rheinlogistik.de", "AW: Angebot Pflegevertrag 2027", "Wir besprechen das intern und melden uns Mitte Dezember.", expect_status="replied"),
            Event(13, "loose", "A-26-1187", "dr.wolff@praxis-wolff.de", "Garten", "Hallo Herr Albers, was ist denn jetzt mit unserem Garten? Wir wollten im Frühjahr draußen sitzen. Gruß S. Wolff", expect_status="replied"),
        ],
        decisions={"A-26-1187": "send"},
    ))

    # 3 Glasduschen München, Partnerbetriebe, Gmail, Word-Vorlage
    h = _hdr("Kraft Glasdesign", "Leopoldstr. 88", "80802 München", "0176 2233445")
    a4 = Doc("Angebot", "2026-071", "23.10.2026", h, ["Raumausstattung Huber", "z. Hd. Frau Huber", "Bauvorhaben Familie Meier, Grünwald"],
             [("Walk-In-Dusche ESG 10 mm, geklebt, Klarglas", 1, "St", 2480), ("Wandprofil und Stabilisator Edelstahl", 1, "Set", 340), ("Aufmaß, Lieferung, Montage", 1, "Psch", 590)],
             "word", ["Lieferzeit ca. 3 Wochen nach Auftrag.", "Zahlung: 50 % bei Auftrag, Rest nach Montage."], extra={"ort": "München"})
    a5 = Doc("Angebot", "2026-074", "29.10.2026", h, ["Familie Graf", "Isarweg 2", "82049 Pullach"],
             [("Eckdusche Satinato, geklebt", 1, "St", 1890), ("Montage", 1, "Psch", 420)], "word", [], extra={"ort": "München"})
    P.append(Persona(
        "glas", "Kraft Glasdesign", "Jeffrey Kraft", "Glasduschen", "4–7 Leute", "kraft.glasdesign@gmail.com", "0176 2233445",
        "Inhaber montiert selbst, schreibt Angebote abends in Word, verschickt per Gmail. Großteil der Aufträge über Partnerbetriebe (Raumausstatter).",
        "Pragmatisch, wenig Zeit, will nichts lernen müssen. Partner dürfen sich nicht belästigt fühlen.",
        sent_folder="[Gmail]/Gesendet",
        sent=[
            Sent(8, [("Andrea Huber", "a.huber@raumausstattung-huber.de")], "Dusche Meier Grünwald", docs=[a4], filenames=["Angebot Meier Grünwald.pdf"], expect=_exp("offer", a4, "a.huber@raumausstattung-huber.de")),
            Sent(2, [("Thomas Graf", "thomas.graf@icloud.com")], "Ihr Angebot Eckdusche", docs=[a5], expect=_exp("offer", a5, "thomas.graf@icloud.com")),
        ],
        events=[Event(4, "manual", "2026-071", subject="AW: Dusche Meier Grünwald", body="Hallo Andrea, hat Frau Meier schon was gesagt? LG Jeffrey")],
    ))

    # 4 Frisch selbstständig, Schweiz, alleine, GMX, CHF
    h = _hdr("Pannasch Renovationen", "Bahnhofstrasse 14", "8400 Winterthur", "+41 78 123 45 67")
    a6 = Doc("Offerte", "O-0007", "26.10.2026", h, ["Frau Lea Brunner", "Rosenweg 5", "8400 Winterthur"],
             [("Badezimmer Plattenarbeiten", 18, "m²", 95), ("Silikonfugen erneuern", 1, "Pauschal", 280)], "swiss", ["Gültig 30 Tage."], extra={"footer": "IBAN CH93 0076 2011 6238 5295 7 · MWST CHE-123.456.789"})
    a6.kind = "Offerte"
    P.append(Persona(
        "solo_ch", "Pannasch Renovationen", "Mykhaylo Pannasch", "Renovation/Plattenleger", "allein, frisch selbstständig", "m.pannasch@gmx.ch", "+41 78 123 45 67",
        "Ganz frisch, 4–7 Aufträge bisher, schreibt Offerten mit einer Vorlage, schickt alles vom Handy über GMX.",
        "Motiviert, aber unsicher, will wie ein richtiger Betrieb wirken. Jeder Auftrag zählt.",
        sent=[Sent(4, [("Lea Brunner", "lea.brunner@bluewin.ch")], "Offerte Badezimmer", docs=[a6], expect=_exp("offer", a6, "lea.brunner@bluewin.ch"))],
        note="Schweizer Zahlenformat und CHF, Wort 'Offerte' statt Angebot.",
    ))

    # 5 Solo-Fliesenleger, Word, GMX
    h = _hdr("Fliesen Kowalski", "Hauptstr. 9", "45127 Essen", "0171 9988776")
    a7 = Doc("Angebot", "17/2026", "28.10.2026", h, ["Herrn Dieter Lange", "Rüttenscheider Str. 50", "45130 Essen"],
             [("Bodenfliesen 60x60 verlegen", 24, "m²", 52), ("Fliesen entfernen und entsorgen", 24, "m²", 18), ("Sockelleisten", 22, "lfm", 9.5)],
             "word", [], extra={"ort": "Essen"})
    P.append(Persona(
        "fliesen", "Fliesen Kowalski", "Marek Kowalski", "Fliesenleger", "allein", "fliesen.kowalski@gmx.de", "0171 9988776",
        "Word-Vorlage, PDF gespeichert, abends am Laptop per GMX-Webmail verschickt. Kein Büro, keine Software.",
        "Ehrlich, direkt, misstrauisch bei 'Passwort eingeben'. Will, dass Kunden ihn nicht für aufdringlich halten.",
        sent=[Sent(2, [("Dieter Lange", "d.lange@arcor.de")], "Ihr Angebot", docs=[a7], filenames=["Lange Boden.pdf"], expect=_exp("offer", a7, "d.lange@arcor.de"))],
        events=[Event(5, "bounce", "17/2026", "mailer-daemon@gmx.net", "Mail delivery failed: returning message to sender", "This message was created automatically by mail delivery software.\n\nA message that you sent could not be delivered to one or more of its recipients.\n\n  d.lange@arcor.de\n    mailbox unavailable", expect_status="bounced")],
        note="Kundenadresse existiert nicht mehr (arcor abgeschaltet).",
    ))

    # 6 Maler, 3 Leute, sevDesk, WEB.DE
    h = _hdr("Malerbetrieb Yilmaz", "Kölner Str. 120", "40227 Düsseldorf", "0211 5566778")
    r3 = Doc("Rechnung", "RE-1044", "20.10.2026", h, ["Frau Petra Schulz", "Brunnenstr. 7", "40223 Düsseldorf"],
             [("Wohnzimmer streichen, 2x deckend", 62, "m²", 11), ("Abdeck- und Schutzarbeiten", 1, "Psch", 120)], "sevdesk", ["Bitte überweisen Sie den Betrag bis zum 03.11.2026."])
    a8 = Doc("Angebot", "AN-1051", "28.10.2026", h, ["Kita Sonnenschein e.V.", "Parkstr. 2", "40225 Düsseldorf"],
             [("Gruppenräume streichen", 3, "Raum", 890), ("Lackierarbeiten Türen", 9, "St", 85)], "sevdesk", ["Angebot gültig 4 Wochen."])
    P.append(Persona(
        "maler", "Malerbetrieb Yilmaz", "Emre Yilmaz", "Maler", "3 Leute", "maler.yilmaz@web.de", "0211 5566778",
        "sevDesk, lädt PDF runter und schickt es über WEB.DE, weil Kunden auf sevDesk-Mails nicht reagieren.",
        "Zahlenmensch, ärgert sich über offene Rechnungen, will Geld schneller sehen.",
        sent=[
            Sent(11, [("Petra Schulz", "petra.schulz@gmx.de")], "Rechnung RE-1044", docs=[r3], expect=_exp("invoice", r3, "petra.schulz@gmx.de")),
            Sent(3, [("Kita Sonnenschein", "leitung@kita-sonnenschein-ddorf.de")], "Angebot AN-1051 Renovierung Gruppenräume", docs=[a8], expect=_exp("offer", a8, "leitung@kita-sonnenschein-ddorf.de")),
        ],
        events=[Event(7, "reply", "RE-1044", "petra.schulz@gmx.de", "AW: Rechnung RE-1044", "Habe heute überwiesen, sorry für die Verspätung!", expect_status="replied")],
        decisions={"RE-1044": "send"},
    ))

    # 7 Elektriker, 6 Leute, Microsoft 365
    h = _hdr("Elektro Brandes GmbH", "Gewerbering 12", "50259 Pulheim", "02238 4040")
    a9 = Doc("Angebot", "ANG-26-3310", "27.10.2026", h, ["Herr und Frau Becker", "Venloer Str. 400", "50259 Pulheim"],
             [("Wallbox 11 kW inkl. Installation", 1, "St", 1490), ("Zählerschrank erneuern", 1, "St", 2280), ("Anmeldung Netzbetreiber", 1, "Psch", 150)],
             "lexware", ["Dieses Angebot ist 6 Wochen gültig."])
    P.append(Persona(
        "elektro", "Elektro Brandes GmbH", "Stefan Brandes", "Elektro", "6 Leute, Büro", "info@elektro-brandes.de", "02238 4040",
        "Büro mit Microsoft 365 / Outlook, Angebote aus Großhändler-Software als PDF.",
        "Technikaffin, aber Datenschutz-kritisch, fragt nach AVV.",
        sent_folder="Gesendete Elemente", sent_flag=True,
        sent=[Sent(3, [("Familie Becker", "becker.pulheim@gmail.com")], "Angebot Wallbox und Zählerschrank", docs=[a9], expect=_exp("offer", a9, "becker.pulheim@gmail.com"))],
        note="Microsoft 365: Passwort-Login geht nicht, nur OAuth. Im Test simuliert per Passwort.",
    ))

    # 8 Dachdecker, 15 Leute, T-Online, Abschlag an Hausverwaltung
    h = _hdr("Dachtechnik Wegener GmbH & Co. KG", "Zechenstr. 3", "44787 Bochum", "0234 667788")
    r4 = Doc("Abschlagsrechnung", "AR-2026-118", "19.10.2026", h, ["Hausverwaltung Ruhr GmbH", "für WEG Königsallee 44", "44789 Bochum"],
             [("Dachsanierung lt. Auftrag", 1, "Psch", 48000)], "abschlag", ["Zahlungsziel: 21 Tage netto."], extra={"share": 0.3})
    a10 = Doc("Angebot", "AN-2026-301", "29.10.2026", h, ["Herr Uwe Fischer", "Am Ehrenmal 3", "44799 Bochum"],
              [("Dachrinne erneuern Zink", 24, "lfm", 68), ("Gerüst", 1, "Psch", 890)], "lexware", [])
    P.append(Persona(
        "dach", "Dachtechnik Wegener GmbH & Co. KG", "Ralf Wegener", "Dachdecker", "15 Leute, 2 im Büro", "buero@dach-wegener.de", "0234 667788",
        "Zwei Bürokräfte, T-Online-Postfach, Abschlagsrechnungen an Hausverwaltungen, Angebote an Private.",
        "Alter Hase, will nichts, was den Ruf bei Hausverwaltungen gefährdet.",
        sent_folder="Gesendete Objekte", sent_flag=False,
        sent=[
            Sent(12, [("HV Ruhr Buchhaltung", "rechnungen@hv-ruhr.de")], "1. Abschlagsrechnung AR-2026-118 Dachsanierung Königsallee 44", docs=[r4], expect=_exp("invoice", r4, "rechnungen@hv-ruhr.de")),
            Sent(1, [("Uwe Fischer", "uwe.fischer@t-online.de")], "Angebot Dachrinne", docs=[a10], expect=_exp("offer", a10, "uwe.fischer@t-online.de")),
        ],
        decisions={"AR-2026-118": "send"},
    ))

    # 9 Hausmeisterdienst, nur Rechnungen, viele kleine
    h = _hdr("Hausmeisterservice Krüger", "Friedhofstr. 1", "52525 Heinsberg", "02452 9090")
    invs = []
    for i, (who, mail, amt, days) in enumerate([
        ("WEG Bahnhofstr. 5", "verwaltung@immo-heinsberg.de", 380, 2), ("Frau Erika Jansen", "erika.jansen@web.de", 95, 1),
        ("Praxis Dr. Klein", "praxis@dr-klein-heinsberg.de", 210, 1), ("Herr Otto Brück", "o.brueck@online.de", 140, 2),
    ]):
        d = Doc("Rechnung", f"2026-{310 + i}", "29.10.2026", h, [who, "52525 Heinsberg"], [("Hausmeisterleistungen Oktober", 1, "Psch", amt)], "kleinunternehmer", ["Zahlbar innerhalb 7 Tagen."])
        invs.append(Sent(days, [(who, mail)], f"Rechnung {d.number}", docs=[d], expect=_exp("invoice", d, mail)))
    P.append(Persona(
        "hausmeister", "Hausmeisterservice Krüger", "Bernd Krüger", "Hausmeisterdienst", "2 Leute", "hausmeister.krueger@t-online.de", "02452 9090",
        "Keine Angebote, nur Monatsrechnungen als Kleinunternehmer aus Word, per T-Online verschickt.",
        "Pain ist nur 'zahlt nicht'. Will auf keinen Fall Stammkunden verärgern.",
        sent_folder="Gesendete Objekte", sent_flag=False, sent=invs,
    ))

    # 10 Trockenbau-Sub an GU, 13b, Sicherheitseinbehalt
    h = _hdr("TB Nowak Trockenbau", "Werkstr. 21", "46045 Oberhausen", "0208 112233")
    r5 = Doc("Rechnung", "R-26-077", "10.10.2026", h, ["Hochtief Hochbau Projekt GmbH", "Bauleitung Neubau Centro Park", "46047 Oberhausen"],
             [("Ständerwände GK doppelt beplankt", 420, "m²", 38), ("Abhangdecken", 260, "m²", 44)], "gu", ["Zahlungsziel: 30 Tage."], vat=0)
    P.append(Persona(
        "trockenbau", "TB Nowak Trockenbau", "Piotr Nowak", "Trockenbau (Nachunternehmer)", "5 Leute", "nowak.trockenbau@gmail.com", "0208 112233",
        "Arbeitet nur für Generalunternehmer, Rechnungen mit §13b und Sicherheitseinbehalt, lange Zahlungsziele.",
        "Hängt finanziell an GU-Zahlungen, will die GU aber nicht verärgern.",
        sent_folder="[Gmail]/Gesendet",
        sent=[Sent(21, [("Bauleitung Centro Park", "rechnungseingang.centro@gu-bauprojekt.de")], "Rechnung R-26-077 Trockenbau Centro Park", docs=[r5], expect=_exp("invoice", r5, "rechnungseingang.centro@gu-bauprojekt.de"))],
        events=[Event(2, "autoreply", "R-26-077", "rechnungseingang.centro@gu-bauprojekt.de", "Automatische Antwort: Eingangsbestätigung", "Ihre Rechnung ist eingegangen und wird geprüft.", expect_status="open")],
        decisions={"R-26-077": "send"},
        note="Rechnungseingangs-Adresse des GU. Darf NICHT als Steuerberater-Adresse rausfallen.",
    ))

    # 11 Elektro-Solo mit Hero, Angebot + AGB + Datenblatt
    h = _hdr("Elektro Schmitz", "Kirchweg 4", "53111 Bonn", "0228 334455")
    a11 = Doc("Angebot", "HERO-A-1093", "28.10.2026", h, ["Frau Anna Vogel", "Poppelsdorfer Allee 80", "53115 Bonn"],
              [("PV-Anlage 9,8 kWp", 1, "St", 13900), ("Speicher 10 kWh", 1, "St", 5200)], "hero", ["Angebot gültig bis 30.11.2026."])
    P.append(Persona(
        "pv", "Elektro Schmitz", "Jan Schmitz", "Elektro / PV", "allein + 1 Azubi", "jan@elektro-schmitz-bonn.de", "0228 334455",
        "Hero-Software, schickt Angebot über eigenes Postfach (all-inkl), immer mit AGB und Datenblatt im Anhang.",
        "Verkaufsorientiert, viele Angebote, wenige Abschlüsse. Will wissen, wer wirklich interessiert ist.",
        sent=[Sent(3, [("Anna Vogel", "anna.vogel@posteo.de")], "Ihr PV-Angebot", docs=[a11], extra_pdfs=[("AGB_Elektro_Schmitz.pdf", agb_pdf("Elektro Schmitz")), ("Datenblatt_Speicher.pdf", datasheet_pdf("Speicher 10 kWh"))],
                   expect=_exp("offer", a11, "anna.vogel@posteo.de"))],
        note="AGB-PDF ist größer als das Angebot.",
    ))

    # 12 Schreinerei, gescanntes Angebot
    h = _hdr("Schreinerei Holzwerk Berger", "Mühlenweg 2", "41189 Mönchengladbach", "02166 5050")
    a12 = Doc("Angebot", "A-2026-55", "27.10.2026", h, ["Familie Kurz", "Eichenweg 9", "41189 Mönchengladbach"], [("Einbauschrank Eiche nach Maß", 1, "St", 4300)], "scan")
    P.append(Persona(
        "schreiner", "Schreinerei Holzwerk Berger", "Hans Berger", "Schreiner", "8 Leute", "info@holzwerk-berger.de", "02166 5050",
        "Angebote handschriftlich ergänzt, über den Kopierer gescannt, als Scan_0012.pdf verschickt.",
        "Traditionell, digital wenig fit, Ehefrau macht Büro.",
        sent=[Sent(4, [("Familie Kurz", "familie.kurz@gmx.de")], "Angebot Einbauschrank", docs=[a12], filenames=["Scan_0012.pdf"], expect={"kind": "offer", "number": None, "amount": None, "customer": "familie.kurz@gmx.de"})],
        note="Bild-PDF ohne Text: Nummer und Betrag nicht lesbar, aber Angebot über Betreff erkennbar.",
    ))

    # 13 Fensterbauer, überarbeitetes Angebot gleiche Nummer
    h = _hdr("Fensterbau Lindner GmbH", "Industriepark 8", "47906 Kempen", "02152 8080")
    a13 = Doc("Angebot", "AN-26-0950", "21.10.2026", h, ["Herr Martin Roth", "Burgring 4", "47906 Kempen"],
              [("Kunststofffenster 3-fach verglast", 9, "St", 780), ("Montage RAL", 9, "St", 190)], "lexware", [])
    a13b = Doc("Angebot", "AN-26-0950", "30.10.2026", h, ["Herr Martin Roth", "Burgring 4", "47906 Kempen"],
               [("Kunststofffenster 3-fach verglast", 9, "St", 740), ("Montage RAL", 9, "St", 190)], "lexware", ["Überarbeitete Fassung."])
    P.append(Persona(
        "fenster", "Fensterbau Lindner GmbH", "Claudia Lindner", "Fensterbau", "12 Leute", "angebote@fensterbau-lindner.de", "02152 8080",
        "Vertrieb schickt Angebote, Kunden verhandeln, dann kommt eine überarbeitete Fassung mit gleicher Nummer.",
        "Vertriebsprofi, hasst doppelte Arbeit und falsche Zahlen im System.",
        sent=[Sent(10, [("Martin Roth", "martin.roth@gmail.com")], "Angebot AN-26-0950 Fenster", docs=[a13], expect=_exp("offer", a13, "martin.roth@gmail.com"))],
        events=[Event(1, "sent", "AN-26-0950", sent=Sent(0, [("Martin Roth", "martin.roth@gmail.com")], "Angebot AN-26-0950 Fenster (überarbeitet)", docs=[a13b]), expect_status="open")],
        decisions={"AN-26-0950": "send"},
        note="Nach Überarbeitung muss der Betrag 8.370,46 € statt 8.799,21 € sein und das Nachfassen neu starten.",
    ))

    # 14 Heizungsbauer, fasst selbst nach
    h = _hdr("Heizung & Bad Özdemir", "Ringstr. 30", "47051 Duisburg", "0203 4455667")
    a14 = Doc("Angebot", "2026-0233", "26.10.2026", h, ["Frau Gisela Hoppe", "Mülheimer Str. 99", "47057 Duisburg"],
              [("Wärmepumpe Luft/Wasser 8 kW", 1, "St", 14900), ("Pufferspeicher", 1, "St", 1900)], "sevdesk", [])
    P.append(Persona(
        "heizung", "Heizung & Bad Özdemir", "Kemal Özdemir", "Heizung/Bad", "4 Leute", "oezdemir.heizung@gmail.com", "0203 4455667",
        "sevDesk, verschickt über Gmail. Fasst bei großen Angeboten selbst nach, mal per Mail, mal per Telefon.",
        "Will die Kontrolle bei großen Aufträgen behalten, Automatik nur für den Rest.",
        sent_folder="[Gmail]/Gesendet",
        sent=[Sent(4, [("Gisela Hoppe", "gisela.hoppe@t-online.de")], "Angebot Wärmepumpe 2026-0233", docs=[a14], expect=_exp("offer", a14, "gisela.hoppe@t-online.de"))],
        events=[Event(1, "manual", "2026-0233", subject="AW: Angebot Wärmepumpe 2026-0233", body="Hallo Frau Hoppe, haben Sie noch Fragen zur Förderung? Gruß K. Özdemir")],
        note="Eigenes Nachfassen am Tag 1 muss die automatische Stufe nach hinten schieben.",
    ))

    # 15 Gebäudereinigung, viele Monatsrechnungen
    h = _hdr("Clean Pro Gebäudeservice GmbH", "Kanalstr. 50", "45879 Gelsenkirchen", "0209 900800")
    many = []
    for i in range(12):
        d = Doc("Rechnung", f"GR-2610-{i + 1:03d}", "01.10.2026", h, [f"Kunde {i + 1} GmbH", "45879 Gelsenkirchen"],
                [("Unterhaltsreinigung September", 1, "Mon", 900 + i * 75)], "sevdesk", ["Zahlbar innerhalb von 14 Tagen."])
        many.append(Sent(29 - (i % 3), [(f"Buchhaltung Kunde {i + 1}", f"buchhaltung@kunde{i + 1}-ge.de")], f"Rechnung {d.number} Unterhaltsreinigung September", docs=[d], expect=_exp("invoice", d, f"buchhaltung@kunde{i + 1}-ge.de")))
    P.append(Persona(
        "reinigung", "Clean Pro Gebäudeservice GmbH", "Sandra Kolb", "Gebäudereinigung", "20 Leute", "rechnung@cleanpro-ge.de", "0209 900800",
        "Monatsrechnungen an Firmenkunden, alle am Monatsanfang. Die meisten zahlen, ein paar immer zu spät.",
        "Ordnungsliebend, will keine Flut von Mails, sondern einen Überblick.",
        sent=many,
        decisions={**{f"GR-2610-{i + 1:03d}": "stop" for i in range(9)}, **{f"GR-2610-{i + 1:03d}": "send" for i in range(9, 12)}},
        note="Erst-Scan: 12 alte Rechnungen, 9 davon längst bezahlt. Erwartet eine gebündelte Freigabe, keine 12 Mails.",
    ))

    # 16 Umzug, Antwort von anderer Adresse im Thread
    h = _hdr("Umzüge Petrovic", "Lagerstr. 10", "40476 Düsseldorf", "0211 445566")
    a15 = Doc("Kostenvoranschlag", "KV-26-088", "28.10.2026", h, ["Familie Neumann", "Kaiserswerther Str. 30", "40477 Düsseldorf"],
              [("Umzug 3-Zimmer, 4 Mann, LKW 7,5 t", 1, "Psch", 1650), ("Halteverbotszone", 2, "St", 95)], "lexware", [])
    P.append(Persona(
        "umzug", "Umzüge Petrovic", "Dragan Petrovic", "Umzüge", "6 Leute", "kontakt@umzuege-petrovic.de", "0211 445566",
        "Kostenvoranschläge nach Besichtigung, Kunden vergleichen 3 Angebote, Entscheidung in wenigen Tagen.",
        "Schnell, verkaufsstark, will wissen, wann er anrufen soll.",
        sent=[Sent(2, [("Jonas Neumann", "jonas.neumann@gmail.com")], "Kostenvoranschlag Umzug KV-26-088", docs=[a15], expect=_exp("offer", a15, "jonas.neumann@gmail.com"))],
        events=[Event(3, "reply", "KV-26-088", "lisa.neumann@web.de", "AW: Kostenvoranschlag Umzug KV-26-088", "Hallo, leider zu teuer, wir haben uns für ein anderes Angebot entschieden.", expect_status="replied")],
        note="Antwort kommt von der Ehefrau (andere Adresse), aber im selben Thread.",
    ))

    # 17 Bodenleger, Mail an Hausverwaltung, CC an Kollegen
    h = _hdr("Böden Arslan", "Weststr. 7", "42103 Wuppertal", "0202 778899")
    a16 = Doc("Angebot", "AB-2026-19", "29.10.2026", h, ["Mieter Familie Wagner", "c/o Hausverwaltung Bergmann", "42103 Wuppertal"],
              [("Vinylboden verlegen", 78, "m²", 34), ("Untergrund spachteln", 78, "m²", 9)], "kleinunternehmer", [])
    P.append(Persona(
        "boden", "Böden Arslan", "Murat Arslan", "Bodenleger", "2 Leute", "boeden.arslan@outlook.de", "0202 778899",
        "Viele Aufträge über Hausverwaltungen. Angebot geht an die Verwaltung, nicht an den Mieter. Partner in CC.",
        "Will die Hausverwaltung als Kunden halten, nicht nerven.",
        sent=[Sent(1, [("HV Bergmann", "auftraege@hv-bergmann.de"), ("Ali Arslan", "ali.arslan@outlook.de")], "Angebot AB-2026-19 Wohnung Wagner", docs=[a16], expect=_exp("offer", a16, "auftraege@hv-bergmann.de"))],
        note="outlook.de ist Microsoft: im Echtbetrieb nur per OAuth.",
    ))

    # 18 Sanitär-Notdienst, zahlbar sofort
    h = _hdr("Rohrblitz Notdienst UG", "Hafenweg 1", "47119 Duisburg", "0800 112211")
    r6 = Doc("Rechnung", "NB-55102", "30.10.2026", h, ["Herr Sven Krüger", "Ruhrorter Str. 8", "47119 Duisburg"],
             [("Notdiensteinsatz Rohrverstopfung", 1, "Psch", 189), ("Anfahrt Nacht", 1, "Psch", 79)], "lexware", ["Zahlbar sofort ohne Abzug."])
    P.append(Persona(
        "notdienst", "Rohrblitz Notdienst UG", "Kevin Lorenz", "Sanitär-Notdienst", "5 Leute", "rechnung@rohrblitz-notdienst.de", "0800 112211",
        "Rechnung direkt nach dem Einsatz per Mail, Kunde zahlt oft nicht, weil der Ärger vorbei ist.",
        "Hart im Ton, will Geld schnell, aber keine Bewertungsschlacht.",
        sent=[Sent(1, [("Sven Krüger", "sven.krueger@gmx.net")], "Ihre Rechnung NB-55102", docs=[r6], expect=_exp("invoice", r6, "sven.krueger@gmx.net"))],
    ))

    # 19 Elektro für Firmenkunde, englische Auto-Antwort
    h = _hdr("Volt Technik Hennes", "Robert-Bosch-Str. 5", "40668 Meerbusch", "02150 7070")
    a17 = Doc("Angebot", "VT-2026-412", "26.10.2026", h, ["Nordic Foods Germany GmbH", "Facility Management", "40668 Meerbusch"],
              [("Beleuchtung Lagerhalle LED", 64, "St", 210), ("Hubsteiger", 3, "Tag", 290)], "lexware", [])
    P.append(Persona(
        "volt", "Volt Technik Hennes", "Markus Hennes", "Elektro (Gewerbe)", "9 Leute", "angebot@volt-hennes.de", "02150 7070",
        "Gewerbekunden, Ansprechpartner oft im Urlaub, Entscheidungen über Einkauf.",
        "Will professionell wirken, keine 'Werbe-Mails'.",
        sent=[Sent(4, [("Lars Eriksson", "lars.eriksson@nordicfoods.com")], "Angebot VT-2026-412 LED Lagerhalle", docs=[a17], expect=_exp("offer", a17, "lars.eriksson@nordicfoods.com"))],
        events=[Event(0, "autoreply", "VT-2026-412", "lars.eriksson@nordicfoods.com", "Automatic reply: Angebot VT-2026-412 LED Lagerhalle", "I am out of office until 10 November.", expect_status="open")],
    ))

    # 20 Gartenpflege solo, iCloud, schickt meist per WhatsApp
    h = _hdr("Grünpflege Ahrens", "Feldweg 3", "41352 Korschenbroich", "0157 3344556")
    a18 = Doc("Angebot", "3/26", "29.10.2026", h, ["Frau Ilse Martens", "Am Bach 1", "41352 Korschenbroich"], [("Heckenschnitt", 40, "lfm", 7), ("Grünschnitt entsorgen", 1, "Psch", 60)], "kleinunternehmer", [])
    P.append(Persona(
        "gruen", "Grünpflege Ahrens", "Lukas Ahrens", "Gartenpflege", "allein", "lukas.ahrens@icloud.com", "0157 3344556",
        "Schickt Preise meist per WhatsApp, nur ältere Kunden bekommen ein PDF per Mail.",
        "Handy-only, ungeduldig, findet Mail altmodisch.",
        sent_folder="Sent Messages",
        sent=[Sent(2, [("Ilse Martens", "ilse.martens@t-online.de")], "Angebot Heckenschnitt", docs=[a18], expect=_exp("offer", a18, "ilse.martens@t-online.de"))],
        note="Der größte Teil seiner Angebote geht über WhatsApp und ist für den Nachfasser unsichtbar.",
    ))
    return P


def pdf_attachments(s: Sent) -> list[tuple[str, bytes]]:
    out = []
    for i, d in enumerate(s.docs):
        name = s.filenames[i] if i < len(s.filenames) else f"{d.kind}_{d.number.replace('/', '-')}.pdf"
        out.append((name, render(d)))
    return out + list(s.extra_pdfs)
