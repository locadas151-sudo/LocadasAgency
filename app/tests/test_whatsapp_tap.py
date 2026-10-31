"""WhatsApp-Nachfassen mit einem Tipp: Telefonnummern, wa.me-Links, Kanal, AWAITING_TAP, Weiterleitung, Bündeln, Payloads.

Ohne Netzwerk: Graph API gemockt, Mail über die Fake-Server.
"""
from __future__ import annotations

import email
import email.policy
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest
from sqlalchemy import select

from locadas.config import settings
from locadas.db import Business, Customer, DocKind, DocStatus, Document, Event, FollowUp, FollowUpStatus, new_session
from locadas.engine import followups as fu
from locadas.engine import taps, wa_texts
from locadas.engine.scanner import scan_account
from locadas.extract import phones
from locadas.holidays import is_workday
from locadas.notify import tokens
from tests.conftest import USER, click
from tests.fakemail import make_mail, make_pdf
from tests.test_e2e import _setup
from tests.test_whatsapp import NUM, _client, _press, graph  # noqa: F401  (Fixture)


def _workday_10() -> datetime:
    """Nächster Werktag 10:00 Berlin (dynamisch, damit Link-Ablauf und Fenster immer passen)."""
    d = datetime.now(timezone.utc) + timedelta(days=1)
    while not is_workday(d.date()):
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, 9, 0, tzinfo=timezone.utc)


NOW = _workday_10()
MOBILE = "491724455667"


# --- Telefonnummern aus PDF-Text -------------------------------------------------------------

LEX_FOOTER = "Selbmann Haustechnik GmbH · Tel. 02161 4455 · Fax 02161 4456 · info@selbmann-haustechnik.de\nSparkasse · IBAN DE89 3704 0044 0532 0130 00 · BIC COBADEFFXXX · USt-IdNr. DE123456789"

PDF_CASES = [
    # (id, text, customer_name, own phone, business names, expected phone, expected mobile)
    ("word_mobil", """Fliesen Kowalski
Hauptstr. 9
45127 Essen
Tel. 0171 9988776
Herrn Dieter Lange
Rüttenscheider Str. 50
45130 Essen
Mobil: 0172 4455667
ANGEBOT Nr. 17/2026
Essen, den 28.10.2026
1. Bodenfliesen 60x60 verlegen 24 m² à 52,00 €
Summe: 1.784,- €
Bankverbindung: Sparkasse · IBAN DE89 3704 0044 0532 0130 00 · BIC COBADEFFXXX""",
     "Dieter Lange", "0171 9988776", ["Fliesen Kowalski", "Marek Kowalski"], "491724455667", "491724455667"),
    ("letterhead_mobile_without_settings", """Grünpflege Ahrens
Feldweg 3
41352 Korschenbroich
Tel. 0157 3344556
Frau Ilse Martens
Am Bach 1
41352 Korschenbroich
Angebot Nr. 3/26
Heckenschnitt 40 lfm 7,00 €
Endbetrag 340,00 €
Grünpflege Ahrens · 0157 3344556 · lukas.ahrens@icloud.com""",
     "Ilse Martens", "", ["Grünpflege Ahrens", "Lukas Ahrens"], "", ""),
    ("lexware_merged_columns_landline", """Selbmann Haustechnik GmbH · Industriestr. 4 · 41063 Mönchengladbach
Familie Brandt                                Selbmann Haustechnik GmbH
Lindenweg 3                                   Industriestr. 4
41065 Mönchengladbach                         Telefon 02161 4455
Tel. 02161 998877                             Datum: 27.10.2026
Angebot Nr. AN-2026-0412
Pos. Bezeichnung Menge Einheit Einzelpreis Gesamt
1 Gas-Brennwerttherme inkl. Montage 1 St 4.890,00 € 4.890,00 €
Nettobetrag 6.210,00 €
zzgl. 19 % USt. 1.179,90 €
Gesamtbetrag 7.389,90 €
""" + LEX_FOOTER,
     "Familie Brandt", "", ["Selbmann Haustechnik GmbH", "Julius Selbmann"], "492161998877", ""),
    ("sevdesk_business_contact_person", """Malerbetrieb Yilmaz · Kölner Str. 120 · 40227 Düsseldorf
Frau Petra Schulz
Brunnenstr. 7
40223 Düsseldorf
Handy 0151 22334455
Rechnung Nr. RE-1044
Kundennummer: 0100234567
Ihr Ansprechpartner: Emre Yilmaz, Tel 0211 5566778
Rechnungsdatum: 20.10.2026
Gesamt brutto 970,90 EUR
Bitte überweisen Sie den Betrag bis zum 03.11.2026.""",
     "Petra Schulz", "", ["Malerbetrieb Yilmaz", "Emre Yilmaz"], "491512233445" + "5", "4915122334455"),
    ("swiss_natel", """Pannasch Renovationen
Bahnhofstrasse 14
8400 Winterthur
Tel. +41 78 123 45 67
Frau Lea Brunner
Rosenweg 5
8400 Winterthur
Natel 079 555 12 34
Offerte Nr. O-0007
Total inkl. MWST CHF 2'151.19
IBAN CH93 0076 2011 6238 5295 7 · MWST CHE-123.456.789""",
     "Lea Brunner", "+41 78 123 45 67", ["Pannasch Renovationen", "Mykhaylo Pannasch"], "41795551234", "41795551234"),
    ("austria_mobil", """Tischlerei Gruber
Hauptplatz 2
4020 Linz
Herrn Florian Huber
Landstraße 15
4020 Linz
Mobil: 0664 1234567
Angebot Nr. 2026-15
Gesamt 4.200,00 €
Raiffeisenbank · IBAN AT61 1904 3002 3457 3201 · ATU12345678""",
     "Florian Huber", "", ["Tischlerei Gruber"], "436641234567", "436641234567"),
    ("customer_label_far_from_address", """Elektro Brandes GmbH · Gewerbering 12 · 50259 Pulheim
Herr und Frau Becker
Venloer Str. 400
50259 Pulheim
Angebot Nr. ANG-26-3310
Datum: 27.10.2026
Pos. 1 Wallbox 11 kW inkl. Installation 1 St 1.490,00 €
Pos. 2 Zählerschrank erneuern 1 St 2.280,00 €
Pos. 3 Anmeldung Netzbetreiber 1 Psch 150,00 €
Hinweis zur Ausführung: Zugang über die Garage
Termin nach Absprache
Ansprechpartner vor Ort: Herr Becker, 0176 99887766
Gesamtbetrag 4.664,80 €
Dieses Angebot ist 6 Wochen gültig.
Elektro Brandes GmbH · Tel. 02238 4040 · info@elektro-brandes.de
Kreissparkasse Köln · IBAN DE89 3704 0044 0532 0130 00""",
     "Familie Becker", "02238 4040", ["Elektro Brandes GmbH", "Stefan Brandes"], "4917699887766", "4917699887766"),
    ("no_phone_only_iban_dates_numbers", """Dachtechnik Wegener GmbH & Co. KG
Zechenstr. 3
44787 Bochum
Hausverwaltung Ruhr GmbH
für WEG Königsallee 44
44789 Bochum
Abschlagsrechnung Nr. AR-2026-118
Rechnungsdatum: 19.10.2026
Kd-Nr. 0012345678
Auftrag 0234-2026-118
Zahlbetrag 17.136,00 €
Sparkasse Bochum · IBAN DE89 3704 0044 0532 0130 00 · BIC COBADEFFXXX""",
     "Hausverwaltung Ruhr GmbH", "0234 667788", ["Dachtechnik Wegener GmbH & Co. KG"], "", ""),
    ("plus49_with_zero", """Heizung & Bad Özdemir · Ringstr. 30 · 47051 Duisburg
Frau Gisela Hoppe
Mülheimer Str. 99
47057 Duisburg
Mobil +49 (0) 171 2345678
Angebot Nr. 2026-0233
Summe brutto 19.992,00 €""",
     "Gisela Hoppe", "0203 4455667", ["Heizung & Bad Özdemir"], "491712345678", "491712345678"),
    ("business_name_on_merged_line", """Malerbetrieb Yilmaz
Kita Sonnenschein e.V.
Parkstr. 2              Malerbetrieb Yilmaz Mobil 0157 1112223
40225 Düsseldorf
Angebot Nr. AN-1051
Gesamt brutto 4.088,95 EUR""",
     "Kita Sonnenschein e.V.", "", ["Malerbetrieb Yilmaz", "Emre Yilmaz"], "", ""),
    ("fax_only", """Hausmeisterservice Krüger
Frau Erika Jansen
Lindenstr. 4
52525 Heinsberg
Fax 02452 12345
Rechnung Nr. 2026-311
Endbetrag 95,00 €""",
     "Erika Jansen", "02452 9090", ["Hausmeisterservice Krüger"], "", ""),
    ("hero_your_contact_person", """Elektro Schmitz
Frau Anna Vogel
Poppelsdorfer Allee 80
53115 Bonn
Ihr Ansprechpartner: Jan Schmitz, Mobil 0176 5554443
Angebot Nr. HERO-A-1093
Angebotssumme 22.729,00 €""",
     "Anna Vogel", "", ["Elektro Schmitz", "Jan Schmitz"], "", ""),
    ("landline_and_mobile_in_block", """Umzüge Petrovic · Lagerstr. 10 · 40476 Düsseldorf
Familie Neumann
Kaiserswerther Str. 30
40477 Düsseldorf
Tel. 0211 4433221 · Handy 0160 9988776
Kostenvoranschlag Nr. KV-26-088
Gesamtbetrag 2.190,25 €""",
     "Jonas Neumann", "0211 445566", ["Umzüge Petrovic"], "492114433221", "491609988776"),
]


@pytest.mark.parametrize("case", PDF_CASES, ids=[c[0] for c in PDF_CASES])
def test_customer_phone_from_pdf(case):
    _, text, name, own_phone, names, exp_phone, exp_mobile = case
    cc = phones.country_code(text)
    own = phones.own_numbers(own_phone, cc=cc)
    got = phones.from_document(text, name, own, names, cc)
    assert (got.phone, got.mobile) == (exp_phone, exp_mobile)
    # Nie die eigene Nummer des Betriebs
    if own_phone:
        assert phones.normalize(own_phone, cc) not in (got.phone, got.mobile)


def test_mobile_prefixes():
    assert phones.is_mobile("491512233445") and phones.is_mobile("491712345678") and phones.is_mobile("4916012345678")
    assert not phones.is_mobile("492161998877") and not phones.is_mobile("4930123456")
    assert phones.is_mobile("436641234567") and not phones.is_mobile("43732123456")
    assert phones.is_mobile("41795551234") and not phones.is_mobile("41441234567")
    assert phones.country_code("Total CHF 100.00") == "41" and phones.country_code("IBAN AT61 1904 3002 3457 3201") == "43"
    assert phones.country_code("Gesamt 100,00 €") == "49"


def test_phone_from_reply_signature_ignores_quoted_business_number():
    text = (
        "Hallo Herr Ahrens,\n\nja, machen Sie das bitte nächste Woche.\n\nViele Grüße\nIlse Martens\nTel. 02182 1234\nMobil 0160 5544332\n\n"
        "Am 29.10.2026 um 10:00 schrieb Grünpflege Ahrens <lukas.ahrens@icloud.com>:\n> Guten Tag,\n> anbei das Angebot.\n> Lukas Ahrens\n> 0157 3344556\n"
    )
    got = phones.from_reply(text, phones.own_numbers("0157 3344556"))
    assert got.mobile == "491605544332" and got.phone == "4921821234"
    only_quote = "Danke!\n\nAm 29.10.2026 schrieb Grünpflege Ahrens:\n> Lukas Ahrens\n> Mobil 0157 3344556\n"
    assert phones.from_reply(only_quote, set()).mobile == ""


def test_phone_from_quoted_inquiry_in_own_mail():
    text = (
        "Guten Tag Herr Lange,\n\nanbei unser Angebot.\n\nViele Grüße\nMarek Kowalski\nMobil 0171 9988776\n\n"
        "Am 20.10.2026 schrieb Dieter Lange <d.lange@arcor.de>:\n> Hallo, können Sie mir ein Angebot für den Boden machen?\n> Gruß Dieter Lange\n> Handy: 0172 4455667\n"
    )
    fresh, _ = phones.split_quote(text)
    own = phones.own_numbers(fresh)
    assert "491719988776" in own
    got = phones.from_thread(text, own)
    assert got.mobile == "491724455667"


# --- wa.me-Link ---------------------------------------------------------------------------------

def test_wa_link_encoding_umlauts_and_line_breaks():
    link = taps.wa_link("+49 172 4455667", "Hallo Frau Müller,\n\nGrüße & mehr? 100 %")
    assert link == "https://wa.me/491724455667?text=Hallo%20Frau%20M%C3%BCller%2C%0A%0AGr%C3%BC%C3%9Fe%20%26%20mehr%3F%20100%20%25"
    assert unquote(link.split("text=")[1]) == "Hallo Frau Müller,\n\nGrüße & mehr? 100 %"


# --- Hilfen für den Ablauf -------------------------------------------------------------------------

def _biz(s, imap, smtp, channel: str = "whatsapp_tap"):
    b, account = _setup(s, imap, smtp)
    b.followup_channel = channel
    s.commit()
    return b, account


_nr = iter(range(100, 10_000))


def _doc(s, b, account, name="Herr Dieter Lange", addr="d.lange@arcor.de", mobile=MOBILE, kind=DocKind.OFFER, amount=1784.0, pdf: str = "") -> Document:
    n = next(_nr)
    number = f"{'A' if kind == DocKind.OFFER else 'R'}-{n}"
    d = Document(
        business_id=b.id, account_id=account.id, kind=kind, status=DocStatus.OPEN, subject=f"Ihr {'Angebot' if kind == DocKind.OFFER else 'Rechnung'} {number}",
        sent_at=NOW - timedelta(days=5), customer_name=name, customer_email=addr, customer_mobile=mobile, document_number=number,
        title="Bodenfliesen verlegen", amount_gross=amount, currency="EUR", message_id=f"<{number}@mueller-shk.de>", pdf_path=pdf,
        due_date=(NOW - timedelta(days=4)) if kind == DocKind.INVOICE else None,
    )
    s.add(d)
    s.flush()
    fu.schedule_followups(s, d, now=NOW - timedelta(days=5))
    d.followups[0].due_at = NOW - timedelta(hours=1)
    s.commit()
    return d


def _customer_mails(smtp, addr: str) -> list:
    return [m for m in smtp.messages if addr in m[1]]


def _owner_mails(smtp) -> list[email.message.EmailMessage]:
    return [email.message_from_bytes(raw, policy=email.policy.default) for _, rc, raw in smtp.messages if rc == [USER]]


# --- Kanal ---------------------------------------------------------------------------------------

def test_channel_routing(fresh_db, imap, smtp):
    with new_session() as s:
        b, account = _biz(s, imap, smtp, "whatsapp_tap")
        with_mobile = _doc(s, b, account)
        without = _doc(s, b, account, name="Frau Erika Jansen", addr="erika.jansen@web.de", mobile="")
        override = _doc(s, b, account, name="Herr Otto Brück", addr="o.brueck@online.de")
        override.followup_channel = "mail"
        s.commit()
        assert taps.channel_for(with_mobile) == "whatsapp_tap"
        assert taps.channel_for(without) == "mail"
        assert taps.channel_for(override) == "mail"
        b.followup_channel = "mail"
        assert taps.channel_for(with_mobile) == "mail"
        with_mobile.followup_channel = "whatsapp_tap"
        assert taps.channel_for(with_mobile) == "whatsapp_tap"
        b.followup_channel = "both"
        assert taps.channel_for(override) == "mail" and taps.channel_for(without) == "mail"
        with_mobile.followup_channel = ""
        assert taps.channel_for(with_mobile) == "both"


def test_whatsapp_tap_flow_no_mail_to_customer(fresh_db, imap, smtp):
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        d = _doc(s, b, account)
        other = _doc(s, b, account, name="Frau Erika Jansen", addr="erika.jansen@web.de", mobile="")
        stats = fu.process_due(s, now=NOW)
        assert stats["tap"] == 1
        f1 = d.followups[0]
        assert f1.status == FollowUpStatus.AWAITING_TAP and f1.tap_state == "open" and f1.tap_token
        assert len(f1.wa_text) <= wa_texts.WA_MAX and "Hallo Herr Dieter Lange," in f1.wa_text and d.document_number in f1.wa_text
        assert f1.tap_notified_at is not None and f1.tap_notice == "email"
        # Kunde mit Handy bekommt keine Mail, der ohne Handy läuft normal (Morgen-Vorschau)
        assert not _customer_mails(smtp, "d.lange@arcor.de")
        assert other.followups[0].announced_at is not None
        mails = _owner_mails(smtp)
        tap_mail = next(m for m in mails if m["Subject"].startswith("Heute per WhatsApp nachfassen"))
        body = tap_mail.get_content()
        assert f"/t/{f1.tap_token}" in body and f"/w/{f1.tap_token}/sent" in body and f"/w/{f1.tap_token}/replied" in body
        assert "Per Mail schicke ich diesen Kunden nichts" in body
        # Zweiter Lauf am selben Tag: keine neue Nachricht, nichts doppelt
        n = len(smtp.messages)
        fu.process_due(s, now=NOW + timedelta(minutes=30))
        assert len(smtp.messages) == n
        # Die Mail an den Kunden ohne Handy geht wie gewohnt nach der Vorschau
        fu.process_due(s, now=other.followups[0].send_after + timedelta(minutes=1))
        assert len(_customer_mails(smtp, "erika.jansen@web.de")) == 1 and not _customer_mails(smtp, "d.lange@arcor.de")


def test_taps_due_later_today_are_bundled_in_the_morning(fresh_db, imap, smtp):
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        a = _doc(s, b, account)
        c = _doc(s, b, account, name="Familie Kurz", addr="familie.kurz@gmx.de", mobile="491601234567")
        c.followups[0].due_at = NOW + timedelta(hours=5)  # heute Nachmittag
        s.commit()
        fu.process_due(s, now=NOW)
        assert a.followups[0].status == FollowUpStatus.AWAITING_TAP and c.followups[0].status == FollowUpStatus.AWAITING_TAP
        tap_mails = [m for m in _owner_mails(smtp) if "WhatsApp" in m["Subject"]]
        assert len(tap_mails) == 1 and "diesen 2 Kunden" in tap_mails[0].get_content()


def test_lifecycle_sent_via_links_confirm_then_post(fresh_db, imap, smtp):
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        d = _doc(s, b, account)
        fu.process_due(s, now=NOW)
        f1 = d.followups[0]
        token, doc_id = f1.tap_token, d.id
    client = _client()
    r = client.get(f"/w/{token}/sent")
    assert r.status_code == 200 and 'method="post"' in r.text and "Abgeschickt?" in r.text
    with new_session() as s:
        assert s.get(Document, doc_id).followups[0].status == FollowUpStatus.AWAITING_TAP  # GET ändert nichts
    r = client.post(f"/w/{token}/sent")
    assert r.status_code == 200 and "als gesendet vermerkt" in r.text
    with new_session() as s:
        d = s.get(Document, doc_id)
        f1, f2 = d.followups[0], d.followups[1]
        assert f1.status == FollowUpStatus.SENT and f1.sent_via == "whatsapp_tap" and f1.tap_state == "sent"
        assert d.followups_sent == 1 and f1.body == f1.wa_text
        assert f2.status == FollowUpStatus.SCHEDULED and f2.due_at >= f1.sent_at + fu.OFFER_MIN_GAP
        assert d.next_followup_at == f2.due_at
    r = client.post(f"/w/{token}/sent")  # zweimal: idempotent
    assert "Schon notiert" in r.text
    assert not _customer_mails(smtp, "d.lange@arcor.de")


def test_lifecycle_replied_and_skip(fresh_db, imap, smtp):
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        d1 = _doc(s, b, account)
        d2 = _doc(s, b, account, name="Familie Kurz", addr="familie.kurz@gmx.de", mobile="491601234567")
        fu.process_due(s, now=NOW)
        t1, t2, id1, id2 = d1.followups[0].tap_token, d2.followups[0].tap_token, d1.id, d2.id
    client = _client()
    click(client, f"/w/{t1}/replied")
    click(client, f"/w/{t2}/skip")
    with new_session() as s:
        d1, d2 = s.get(Document, id1), s.get(Document, id2)
        assert d1.status == DocStatus.REPLIED and d1.close_reason == "Kunde hat per WhatsApp geantwortet"
        assert all(f.status == FollowUpStatus.CANCELLED for f in d1.followups)
        assert d2.status == DocStatus.OPEN and d2.followups[0].status == FollowUpStatus.SKIPPED and d2.followups[0].tap_state == "skipped"
        assert d2.followups[1].status == FollowUpStatus.SCHEDULED and d2.next_followup_at == d2.followups[1].due_at
    r = client.post(f"/w/{t2}/sent")
    assert "Nicht mehr offen" in r.text


def test_unconfirmed_tap_reminds_once_then_expires_never_mails(fresh_db, imap, smtp):
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        d = _doc(s, b, account)
        fu.process_due(s, now=NOW)
        f1 = d.followups[0]
        next_day = NOW + timedelta(days=1)
        while not is_workday(next_day.date()):
            next_day += timedelta(days=1)
        n = len(smtp.messages)
        fu.process_due(s, now=next_day)
        assert f1.tap_reminded_at is not None and f1.status == FollowUpStatus.AWAITING_TAP
        reminder = _owner_mails(smtp)[-1]
        assert len(smtp.messages) == n + 1 and reminder["Subject"].startswith("Noch offen per WhatsApp")
        assert "Noch offen von gestern" in reminder.get_content()
        fu.process_due(s, now=next_day + timedelta(hours=3))
        assert len(smtp.messages) == n + 1  # nur einmal erinnern
        fu.process_due(s, now=NOW + timedelta(days=4))
        assert f1.status == FollowUpStatus.SKIPPED and f1.tap_state == "expired"
        assert not _customer_mails(smtp, "d.lange@arcor.de")


def test_tap_redirect_records_tapped_at_and_assumes_sent(fresh_db, imap, smtp):
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        d = _doc(s, b, account, name="Frau Lea Müller")
        fu.process_due(s, now=NOW)
        token, doc_id, text = d.followups[0].tap_token, d.id, d.followups[0].wa_text
    client = _client()
    r = client.get(f"/t/{token}", headers={"User-Agent": "WhatsApp/2.24.1 A"}, follow_redirects=False)
    assert r.status_code == 302
    with new_session() as s:
        assert s.get(Document, doc_id).followups[0].tapped_at is None  # Link-Vorschau zählt nicht
    r = client.get(f"/t/{token}", headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}, follow_redirects=False)
    loc = r.headers["location"]
    assert r.status_code == 302 and loc.startswith(f"https://wa.me/{MOBILE}?text=")
    assert unquote(loc.split("text=", 1)[1]) == text and "M%C3%BCller" in loc
    assert client.get("/t/falsch", follow_redirects=False).status_code == 404
    with new_session() as s:
        d = s.get(Document, doc_id)
        f1 = d.followups[0]
        assert f1.tapped_at is not None
        fu.process_due(s, now=NOW + timedelta(days=1, hours=1) if is_workday((NOW + timedelta(days=1)).date()) else NOW + timedelta(days=3, hours=1))
        assert f1.status == FollowUpStatus.SENT and f1.sent_via == "whatsapp_tap" and f1.sent_at == f1.tapped_at
        assert "Link geöffnet" in " ".join(e.text for e in s.scalars(select(Event)).all())


def test_channel_both_mail_plus_suggestion(fresh_db, imap, smtp):
    with new_session() as s:
        b, account = _biz(s, imap, smtp, "both")
        d = _doc(s, b, account)
        fu.process_due(s, now=NOW)  # Morgen-Vorschau
        f1 = d.followups[0]
        assert f1.announced_at is not None and f1.tap_state == ""
        fu.process_due(s, now=f1.send_after + timedelta(minutes=1))
        assert f1.status == FollowUpStatus.SENT and f1.sent_via == "mail" and len(_customer_mails(smtp, "d.lange@arcor.de")) == 1
        assert f1.tap_state == "open" and f1.tap_notified_at is not None and f1.wa_text
        tap_mail = _owner_mails(smtp)[-1]
        assert "Die Mail ist schon raus" in tap_mail.get_content() and "Per Mail schicke ich" not in tap_mail.get_content()
        taps.handle(s, f1, "sent")
        assert f1.sent_via == "mail+whatsapp_tap" and d.followups_sent == 1


# --- WhatsApp an den Betrieb --------------------------------------------------------------------

def _wa_business(s, imap, smtp, window=True):
    b, account = _biz(s, imap, smtp)
    b.whatsapp_number = NUM
    b.whatsapp_opt_in = True
    b.whatsapp_last_inbound_at = NOW - timedelta(hours=1) if window else None
    s.commit()
    return b, account


def test_cta_url_payload_and_reply_buttons(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, account = _wa_business(s, imap, smtp)
        d = _doc(s, b, account, kind=DocKind.INVOICE, amount=224.91, name="Herr Peter Weber", addr="p.weber@web.de")
        d.iban = "DE89 3704 0044 0532 0130 00"
        s.commit()
        fu.process_due(s, now=NOW)
        f1 = d.followups[0]
        assert f1.status == FollowUpStatus.AWAITING_TAP and f1.tap_notice == "whatsapp"
        cta, btns = graph.calls[-2], graph.calls[-1]
        assert cta["type"] == "interactive" and cta["to"] == NUM
        i = cta["interactive"]
        assert i["type"] == "cta_url" and i["action"]["name"] == "cta_url"
        assert i["action"]["parameters"] == {"display_text": "In WhatsApp öffnen", "url": f"http://testserver/t/{f1.tap_token}"}
        assert f1.wa_text in i["body"]["text"] and len(i["body"]["text"]) <= 1024
        assert btns["interactive"]["type"] == "button"
        b_ = btns["interactive"]["action"]["buttons"]
        assert [x["reply"]["title"] for x in b_] == ["Gesendet", "Ist bezahlt", "Diesmal nicht"]
        assert [tokens.parse(x["reply"]["id"]) for x in b_] == [("tsent", [f1.id]), ("tpaid", [f1.id]), ("tskip", [f1.id])]
        assert not [m for m in smtp.messages if m[1] == [USER]]  # keine Mail an den Betrieb
        assert "Bitte überweisen Sie den Betrag bis" in f1.wa_text and "IBAN DE89" in f1.wa_text
        fid, doc_id = f1.id, d.id
    # Knopf „Gesendet“ per Webhook
    c = _client()
    assert _press(c, tokens.payload("tsent", fid)).status_code == 200
    assert "als gesendet vermerkt" in graph.texts()[-1]
    with new_session() as s:
        f1 = s.get(FollowUp, fid)
        assert f1.status == FollowUpStatus.SENT and f1.sent_via == "whatsapp_tap"
    _press(c, tokens.payload("tsent", fid))
    assert "ist als gesendet vermerkt" in graph.texts()[-1]  # zweimal drücken: nur der Hinweis, nichts doppelt
    with new_session() as s:
        assert s.get(Document, doc_id).followups_sent == 1
    _press(c, tokens.payload("tpaid", fid))
    with new_session() as s:
        assert s.get(Document, doc_id).status == DocStatus.PAID


def test_template_with_url_button_outside_window(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, account = _wa_business(s, imap, smtp, window=False)
        d = _doc(s, b, account)
        fu.process_due(s, now=NOW)
        f1 = d.followups[0]
        p = graph.last
        assert p["type"] == "template"
        t = p["template"]
        assert t["name"] == "locadas_tap" and t["language"]["code"] == "de"
        body = t["components"][0]
        assert body["type"] == "body" and [x["text"] for x in body["parameters"]][:3] == ["Thomas", "Herr Dieter Lange", f"Angebot {d.document_number} über 1.784,00 €"]
        assert "\n" not in body["parameters"][3]["text"]
        btns = [c for c in t["components"] if c["type"] == "button"]
        assert btns[0] == {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": f1.tap_token}]}
        assert [c["sub_type"] for c in btns[1:]] == ["quick_reply"] * 3 and [c["index"] for c in btns[1:]] == ["1", "2", "3"]
        assert [tokens.parse(c["parameters"][0]["payload"])[0] for c in btns[1:]] == ["tsent", "treplied", "tskip"]
        assert f1.tap_notice == "whatsapp"


def test_bundle_limit_five_cta_per_day(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, account = _wa_business(s, imap, smtp)
        docs = [_doc(s, b, account, name=f"Herr Kunde{i} Muster", addr=f"k{i}@web.de", mobile=f"4917{i}1234567"[:13]) for i in range(7)]
        fu.process_due(s, now=NOW)
        ctas = [c for c in graph.calls if c["type"] == "interactive" and c["interactive"]["type"] == "cta_url"]
        assert len(ctas) == taps.MAX_CTA_PER_DAY == 5
        overflow = graph.texts()[-1]
        assert "2 weitere" in overflow and "/dashboard#whatsapp" in overflow
        notices = sorted(d.followups[0].tap_notice for d in docs)
        assert notices.count("whatsapp") == 5 and notices.count("dashboard") == 2
        assert all(d.followups[0].status == FollowUpStatus.AWAITING_TAP for d in docs)
        # Neuer Tap am selben Tag: Kontingent ist aufgebraucht, kommt nur ins Dashboard
        late = _doc(s, b, account, name="Frau Spät Muster", addr="spaet@web.de", mobile="491609999999")
        n = len(graph.calls)
        fu.process_due(s, now=NOW + timedelta(hours=1))
        assert late.followups[0].tap_notice == "dashboard"
        assert not [c for c in graph.calls[n:] if c["type"] == "interactive" and c["interactive"]["type"] == "cta_url"]
        bid = b.id
    # Dashboard listet alle offenen Taps mit großen Knöpfen
    from locadas.web import auth

    c = _client()
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    r = c.get("/dashboard")
    assert r.status_code == 200 and "Heute per WhatsApp (8)" in r.text
    assert r.text.count('class="wa-open"') == 8


def test_next_tap_message_offers_replied_after_previous_whatsapp(fresh_db, imap, smtp, graph):
    with new_session() as s:
        b, account = _wa_business(s, imap, smtp)
        d = _doc(s, b, account)
        fu.process_due(s, now=NOW)
        taps.handle(s, d.followups[0], "sent", NOW)
        s.commit()
        f2 = d.followups[1]
        later = f2.due_at + timedelta(hours=1)
        while not (is_workday(later.date()) and fu.in_send_window(later)):
            later += timedelta(hours=6)
        b.whatsapp_last_inbound_at = later - timedelta(hours=1)
        s.commit()
        fu.process_due(s, now=later)
        assert f2.status == FollowUpStatus.AWAITING_TAP
        cta = [c for c in graph.calls if c["type"] == "interactive" and c["interactive"]["type"] == "cta_url"][-1]
        assert "per WhatsApp raus" in cta["interactive"]["body"]["text"] and "Hat geantwortet" in cta["interactive"]["body"]["text"]
        titles = [x["reply"]["title"] for x in graph.last["interactive"]["action"]["buttons"]]
        assert "Hat geantwortet" in titles


def test_daily_mail_preview_offers_replied_for_docs_with_previous_tap(fresh_db, imap, smtp):
    with new_session() as s:
        b, account = _biz(s, imap, smtp, "mail")
        d = _doc(s, b, account)
        f1 = d.followups[0]
        f1.status, f1.sent_via, f1.sent_at = FollowUpStatus.SENT, "whatsapp_tap", NOW - timedelta(days=5)
        d.followups[1].due_at = NOW - timedelta(minutes=5)
        s.commit()
        fu.process_due(s, now=NOW)
        preview = _owner_mails(smtp)[-1].get_content()
        assert "Zuletzt per WhatsApp am" in preview and "Kunde hat dort geantwortet" in preview


# --- Texte -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", [DocKind.OFFER, DocKind.INVOICE])
@pytest.mark.parametrize("step", [1, 2, 3])
def test_whatsapp_texts_short_and_clean(fresh_db, imap, smtp, kind, step, tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        b.owner_name = "Maximilian-Alexander Schneider-Grünewald"
        d = _doc(s, b, account, kind=kind, name="Frau Dr. Annegret Müller-Lüdenscheidt", pdf=str(pdf))
        d.title = "Komplettsanierung Badezimmer Obergeschoss mit bodengleicher Dusche und Fußbodenheizung"
        d.iban, d.bic = "DE89 3704 0044 0532 0130 00", "COBADEFFXXX"
        s.commit()
        t = wa_texts.template_text(d, step, NOW)
        assert len(t) <= wa_texts.WA_MAX, (len(t), t)
        assert t.startswith("Hallo Frau Dr. Annegret Müller-Lüdenscheidt,")
        assert d.document_number in t and d.term in t
        assert "–" not in t and "—" not in t and not re.search(r",\s+und\b", t)
        assert "Betreff" not in t and "@" not in t
        assert t.rstrip().endswith("Maximilian-Alexander Schneider-Grünewald")
        if kind == DocKind.INVOICE:
            assert "Bitte überweisen Sie den Betrag bis" in t
            if step == 3:
                assert "Mahnverfahren" in t
        elif step == 3:
            assert "Ich melde mich nicht weiter" in t


def test_whatsapp_text_has_doc_link_when_delivery_link(fresh_db, imap, smtp, tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        d = _doc(s, b, account, name="Familie Kurz", pdf=str(pdf))
        t = wa_texts.template_text(d, 1, NOW)
        assert t.startswith("Hallo Familie Kurz,") and f"/a/{d.view_token}" in t and "Das Angebot als PDF:" in t
        b.doc_delivery = "attachment"
        assert "/a/" not in wa_texts.template_text(d, 1, NOW)


def test_personalized_whatsapp_text_guardrails(fresh_db, imap, smtp, monkeypatch):
    from locadas.engine import style

    with new_session() as s:
        b, account = _biz(s, imap, smtp)
        d = _doc(s, b, account)
        answers = iter([
            "Hallo Herr Dieter Lange,\n\nich melde mich wegen unserem Angebot – haben Sie bis Freitag Zeit?",  # Gedankenstrich, Datum
            f"Hallo Herr Dieter Lange,\n\nich wollte kurz nachhören, ob unser Angebot {d.document_number} angekommen ist. Haben Sie noch Fragen?",
        ])
        monkeypatch.setattr(style, "claude_text", lambda prompt, max_tokens: next(answers))
        t = wa_texts.generate(d, 1, {"tone": "sie"}, NOW)
        assert t is not None and len(t) <= wa_texts.WA_MAX and "–" not in t and t.endswith("Viele Grüße\nThomas Müller")
        monkeypatch.setattr(style, "claude_text", lambda prompt, max_tokens: "Hallo Herr Dieter Lange,\n\n" + "sehr " * 60 + "gut.")
        assert wa_texts.generate(d, 1, None, NOW) is None


# --- Scan: Nummer aus dem PDF, Signatur aus der Antwort ----------------------------------------------

def test_scan_extracts_customer_mobile_not_business_number(fresh_db, imap, smtp):
    pdf = make_pdf([
        "Mueller Haustechnik GmbH", "Tel. 0171 9988776", "Angebot Nr. A-2026-500",
        "Familie Schmidt", "Gartenweg 4, 41061 Moenchengladbach", "Mobil: 0172 4455667",
        "Gastherme Vaillant 1 St 2.950,00 EUR", "Gesamtbetrag 3.510,50 EUR",
        "Mueller Haustechnik GmbH - Tel. 0171 9988776 - IBAN DE89 3704 0044 0532 0130 00",
    ])
    imap.store.append("Gesendet", make_mail(USER, "familie.schmidt@gmx.de", "Angebot Gastherme", "Anbei unser Angebot.\n\nThomas Müller\n0171 9988776", ("Angebot_A-2026-500.pdf", pdf), to_name="Familie Schmidt"))
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        b.phone = ""  # auch ohne hinterlegte Nummer: Briefkopf und Signatur zählen als eigene
        s.commit()
        scan_account(s, account)
        d = s.scalars(select(Document)).one()
        assert d.customer_mobile == "491724455667" and d.customer_phone == "491724455667"
        c = s.scalars(select(Customer)).one()
        assert c.mobile == "491724455667"


def test_reply_signature_fills_customer_mobile(fresh_db, imap, smtp):
    pdf = make_pdf(["Mueller Haustechnik GmbH", "Angebot Nr. A-2026-501", "Herr Peter Weber, Hauptstr. 9", "Gesamtbetrag 900,00 EUR"])
    raw = make_mail(USER, "p.weber@web.de", "Angebot Wartung", "Anbei.", ("Angebot_A-2026-501.pdf", pdf), to_name="Peter Weber")
    imap.store.append("Gesendet", raw)
    with new_session() as s:
        b, account = _setup(s, imap, smtp)
        scan_account(s, account)
        d = s.scalars(select(Document)).one()
        assert d.customer_mobile == ""
        mid = email.message_from_bytes(raw)["Message-ID"]
        imap.store.append("INBOX", make_mail("p.weber@web.de", USER, "AW: Angebot Wartung", "Passt so.\n\nGruß Peter Weber\nMobil 0151 7654321\n\nAm 01.11. schrieb Mueller:\n> Thomas Müller\n> 0171 1111111", in_reply_to=mid))
        scan_account(s, account)
        s.refresh(d)
        assert d.status != DocStatus.OPEN and d.customer_mobile == "491517654321"
        assert s.scalars(select(Customer)).one().mobile == "491517654321"


# --- Web: Dokument und Einstellungen ------------------------------------------------------------------

def test_document_edit_phone_and_toggle_and_settings(fresh_db, imap, smtp):
    from locadas.web import auth

    with new_session() as s:
        b, account = _biz(s, imap, smtp, "mail")
        d = _doc(s, b, account, mobile="")
        bid, doc_id = b.id, d.id
    c = _client()
    c.cookies.set(auth.COOKIE, auth.session_token(bid))
    page = c.get(f"/dokument/{doc_id}")
    assert "Diesen Kunden per WhatsApp nachfassen" in page.text and "Kunden-Handy" in page.text
    r = c.post(f"/dokument/{doc_id}/bearbeiten", data={"customer_name": "Herr Dieter Lange", "customer_email": "d.lange@arcor.de", "customer_mobile": "0172 4455667", "customer_phone": "0201 123456"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=gespeichert" in r.headers["location"]
    r = c.post(f"/dokument/{doc_id}/kanal", data={"whatsapp": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as s:
        d = s.get(Document, doc_id)
        assert (d.customer_mobile, d.customer_phone) == ("491724455667", "49201123456")
        assert d.followup_channel == "whatsapp_tap" and taps.channel_for(d) == "whatsapp_tap"
        assert s.scalars(select(Customer).where(Customer.email == "d.lange@arcor.de")).one().mobile == "491724455667"
    # Festnetz als Handy: nicht WhatsApp-fähig
    r = c.post(f"/dokument/{doc_id}/bearbeiten", data={"customer_name": "Herr Dieter Lange", "customer_email": "d.lange@arcor.de", "customer_mobile": "0201 123456", "customer_phone": ""}, follow_redirects=False)
    assert "nummer_ungueltig" in r.headers["location"]
    with new_session() as s:
        d = s.get(Document, doc_id)
        assert d.customer_mobile == "" and d.customer_phone == "49201123456" and taps.channel_for(d) == "mail"
    c.post(f"/dokument/{doc_id}/kanal", data={}, follow_redirects=False)
    with new_session() as s:
        assert s.get(Document, doc_id).followup_channel == ""  # wieder wie im Betrieb (Mail)
    page = c.get("/einstellungen")
    assert 'name="followup_channel"' in page.text
    r = c.post("/einstellungen", data={"name": "Müller Haustechnik GmbH", "followup_channel": "whatsapp_tap"}, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as s:
        assert s.get(Business, bid).followup_channel == "whatsapp_tap"
    c.post("/einstellungen", data={"name": "Müller Haustechnik GmbH", "followup_channel": "quatsch"}, follow_redirects=False)
    with new_session() as s:
        assert s.get(Business, bid).followup_channel == "whatsapp_tap"
