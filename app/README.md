# Locadas Nachfasser

Handwerker verbindet sein Postfach, Locadas erkennt Angebote und Rechnungen im Gesendet-Ordner und fasst in seinem Namen nach, bis der Kunde antwortet. Dazu ein kostenloser Angebots-Check für Endkunden.

- `locadas/mail/` IMAP lesen, SMTP senden, Anbieter-Erkennung, Verbindungs-Check
- `locadas/extract/` PDF-Text, Angebot/Rechnung erkennen, Felder per Claude mit Regex-Fallback, Angebots-Check
- `locadas/engine/` Scan, Nachfass-Logik, Texte, Scheduler
- `locadas/notify/` ein Kanal für alles an den Betrieb: WhatsApp-Sekretär mit Knöpfen, sonst Mail (siehe `WHATSAPP.md`)
- `locadas/engine/taps.py` WhatsApp-Nachfassen mit einem Tipp: fertige Nachricht (`engine/wa_texts.py`, höchstens 400 Zeichen), ein Tipp öffnet den Chat des Betriebs mit dem Kunden (wa.me), Kundennummer aus PDF und Mail-Signatur (`extract/phones.py`)
- `locadas/engine/customers.py` Kundenportal `/k/{token}` je Kunde (Stand, Unterlagen, Kontakt), „Auftrag fertig“-Mail und Weg zur Google-Bewertung ohne Review Gating
- `locadas/engine/summary.py` Monatsbilanz am 1. um 9 Uhr (nachgefasst, Antworten, Aufträge, Zahlungen, nur was nach dem Nachfassen kam) und Karte „Dein Monat“ im Dashboard
- `locadas/engine/accounts.py` Verbindungs-Wächter: Postfach gestört (Passwort geändert oder Server dreimal in Folge weg) heißt Nachfassen pausiert, einmal Bescheid mit Link „Neu verbinden“, alle 3 Tage Erinnerung, danach liegengebliebene Stufen neu verteilt
- `locadas/main.py` + `web/` FastAPI, Templates im locadas.de-Design
- `tests/` Tests inkl. End-to-End über Fake-IMAP/SMTP und gemockter WhatsApp Graph API
- `demo.py` lokal durchklicken ohne echtes Postfach

Betrieb und Deploy: siehe `DEPLOY.md`.
