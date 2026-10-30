"""Demo-Modus: lokaler Test-Mailserver mit Beispielangeboten, dazu die App.

    cd app && python demo.py

Dann http://localhost:8000/verbinden, als Server 127.0.0.1 mit den angezeigten Ports eintragen.
Nachfass-Mails landen nicht bei echten Kunden, sondern werden im Terminal ausgegeben.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DATA_DIR", os.path.join(tempfile.gettempdir(), "locadas-demo"))
os.environ.setdefault("SECRET_KEY", "demo-secret")
os.environ.setdefault("SCAN_INTERVAL_MINUTES", "1")
os.environ.setdefault("BASE_URL", "http://localhost:8000")

from tests.fakemail import FakeImapServer, FakeSmtpServer, make_mail, make_pdf  # noqa: E402

USER, PASSWORD = "info@mueller-haustechnik.de", "demo"


def seed(imap: FakeImapServer) -> None:
    offer = make_pdf([
        "Müller Haustechnik GmbH", "Angebot Nr. A-2026-118", "Familie Schmidt, Gartenweg 4, 41061 Mönchengladbach",
        "Pos. 1 Demontage Altgerät 1 St 180,00 EUR", "Pos. 2 Gastherme Vaillant ecoTEC 1 St 2.950,00 EUR",
        "Netto 3.130,00 EUR", "MwSt 19% 594,70 EUR", "Gesamtbetrag 3.724,70 EUR", "Angebot gültig bis 30.11.2026",
    ])
    invoice = make_pdf([
        "Müller Haustechnik GmbH", "Rechnung Nr. R-2026-301", "Herr Peter Weber, Hauptstr. 9, 52525 Heinsberg",
        "Wartung Gastherme 1 St 189,00 EUR", "Netto 189,00 EUR", "MwSt 19% 35,91 EUR", "Rechnungsbetrag 224,91 EUR",
        "Zahlbar innerhalb von 14 Tagen ohne Abzug",
    ])
    kva = make_pdf(["Müller Haustechnik GmbH", "Kostenvoranschlag Nr. KV-77", "Badsanierung EG", "Gesamt brutto 12.400,00 EUR"])
    imap.store.append("Gesendet", make_mail(USER, "familie.schmidt@gmx.de", "Angebot Gastherme", "Anbei unser Angebot.", ("Angebot_A-2026-118.pdf", offer), from_name="Müller Haustechnik", to_name="Familie Schmidt"))
    imap.store.append("Gesendet", make_mail(USER, "p.weber@web.de", "Ihre Rechnung R-2026-301", "Anbei die Rechnung.", ("Rechnung_R-2026-301.pdf", invoice), to_name="Peter Weber"))
    imap.store.append("Gesendet", make_mail(USER, "bauherr@example.org", "Kostenvoranschlag Bad", "Wie besprochen.", ("KV-77.pdf", kva), to_name="Sabine Koch"))
    imap.store.append("Gesendet", make_mail(USER, "kollege@mueller-haustechnik.de", "Urlaubsplan", "Ohne PDF."))


def main() -> None:
    imap = FakeImapServer(USER, PASSWORD).start()
    smtp = FakeSmtpServer().start()
    seed(imap)
    print("=" * 64)
    print("Demo-Postfach")
    print(f"  Mailadresse: {USER}")
    print(f"  Passwort:    {PASSWORD}")
    print(f"  IMAP-Server: 127.0.0.1  Port {imap.port}")
    print(f"  SMTP-Server: 127.0.0.1  Port {smtp.port}")
    print("  (im Formular unter 'Server manuell eintragen')")
    print("=" * 64)

    def watch():
        seen = 0
        while True:
            if len(smtp.messages) > seen:
                for frm, to, raw in smtp.messages[seen:]:
                    print(f"\n--- MAIL von {frm} an {', '.join(to)} ---\n{raw.decode('utf-8', 'replace')[:1500]}\n")
                seen = len(smtp.messages)
            time.sleep(1)

    threading.Thread(target=watch, daemon=True).start()
    import uvicorn

    uvicorn.run("locadas.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="info")


if __name__ == "__main__":
    main()
