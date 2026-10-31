# Locadas Nachfasser

Handwerker verbindet sein Postfach, Locadas erkennt Angebote und Rechnungen im Gesendet-Ordner und fasst in seinem Namen nach, bis der Kunde antwortet. Dazu ein kostenloser Angebots-Check für Endkunden.

- `locadas/mail/` IMAP lesen, SMTP senden, Anbieter-Erkennung, Verbindungs-Check
- `locadas/extract/` PDF-Text, Angebot/Rechnung erkennen, Felder per Claude mit Regex-Fallback, Angebots-Check
- `locadas/engine/` Scan, Nachfass-Logik, Texte, Scheduler
- `locadas/notify/` ein Kanal für alles an den Betrieb: WhatsApp-Sekretär mit Knöpfen, sonst Mail (siehe `WHATSAPP.md`)
- `locadas/main.py` + `web/` FastAPI, Templates im locadas.de-Design
- `tests/` Tests inkl. End-to-End über Fake-IMAP/SMTP und gemockter WhatsApp Graph API
- `demo.py` lokal durchklicken ohne echtes Postfach

Betrieb und Deploy: siehe `DEPLOY.md`.
