# Launch: Locadas Nachfasser für die ersten 50 bis 100 Betriebe

Alles, was nur du machen kannst, in der Reihenfolge, in der es Sinn ergibt. Details stehen in `DEPLOY.md`, `WHATSAPP.md` und `WHATSAPP_COEXISTENCE.md`.

## A. Pflicht, damit es heute live geht (ca. 60 Minuten)

1. **Claude-Key erneuern.** Der alte Key stand im Chat. In der Anthropic Console einen neuen anlegen, den alten löschen.
2. **GitHub-Secrets** (Repo → Settings → Secrets and variables → Actions):
   - `AWS_HOST`, `AWS_USER`, `AWS_SSH_KEY`
   - `APP_SECRET_KEY` und `APP_ENCRYPTION_KEY` (Befehle in `DEPLOY.md`, den Encryption-Key danach nie mehr ändern)
   - `ANTHROPIC_API_KEY` (der neue)
   - `SYSTEM_SMTP_HOST`, `SYSTEM_SMTP_USER`, `SYSTEM_SMTP_PASSWORD` für info@locadas.de. Ohne das bekommen Tester ohne WhatsApp keine Nachrichten und keinen Login-Link.
3. **GitHub-Variables:** `ADMIN_EMAILS` = deine Adresse für das Cockpit.
4. **DNS:**
   - `A` `app.locadas.de` → Server-IP
   - `MX` `angebot.locadas.de` → `app.locadas.de`, Priorität 10
5. **AWS Security Group:** 80, 443 und 25 für alle offen, 22 nur für deine IP. Auf dem Server darf nichts anderes auf Port 25 lauschen (`sudo ss -ltnp | grep :25`).
6. **Deploy:** Actions → „Deploy Nachfasser (app/) to AWS“ → Run workflow. Danach muss `https://app.locadas.de/healthz` Status 200 liefern.
7. **Selbsttest (15 Minuten):**
   - Auf `app.locadas.de/testen` mit deiner Handynummer anmelden.
   - Ein echtes altes Angebot als PDF an deine neue Adresse weiterleiten. Nach spätestens zwei Minuten kommt „Erkannt: Angebot …“.
   - Im Cockpit `/admin` prüfen, ob du als Anmeldung auftauchst.
   - Mit einem eigenen GMX-, IONOS- oder Gmail-Postfach (App-Passwort) verbinden und schauen, ob Angebote aus dem Gesendet-Ordner erkannt werden.

Damit läuft: Testen ohne Postfach mit Mail-Benachrichtigung, Postfach verbinden per Passwort (GMX, Web.de, T-Online, IONOS, Strato, All-Inkl, Gmail mit App-Passwort und weitere), automatisches Nachfassen, Rechnungen, Cockpit, Alarme per Mail, Sicherungen.

## B. WhatsApp-Sekretär (heute anstoßen, Meta braucht Tage)

1. Meta Business verifizieren, WhatsApp Cloud API einrichten, eigene Nummer für den Sekretär (`WHATSAPP.md`).
2. Secrets `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `ADMIN_WHATSAPP` (deine Nummer, ohne +), Variable `WHATSAPP_DISPLAY_NUMBER`.
3. Webhook in Meta auf `https://app.locadas.de/webhook/whatsapp` setzen.
4. Vorlagen einreichen, Texte stehen in `WHATSAPP.md`: `locadas_welcome`, `locadas_event`, `locadas_event_v`, `locadas_preview_v`, `locadas_tap`, `locadas_preview`, `locadas_approval`, `locadas_invoice_open`, `locadas_opened`, `locadas_reply`, `locadas_outcome_offer`, `locadas_outcome_invoice`, `locadas_summary`, `locadas_connection`.

Bis die Vorlagen durch sind, läuft alles per Mail. Wer dem Sekretär selbst schreibt, bekommt 24 Stunden lang auch WhatsApp.

## C. Microsoft 365 und Outlook (heute anstoßen)

1. Azure App-Registrierung nach `DEPLOY.md`, Abschnitt „Microsoft (Outlook.com und Microsoft 365) über Microsoft Graph“.
2. Secrets `MICROSOFT_CLIENT_ID` und `MICROSOFT_CLIENT_SECRET`, neu deployen.
3. Publisher-Verifizierung im Partner Center beantragen. Bis sie durch ist, brauchen viele Firmenkunden die Freigabe ihrer IT. Die App zeigt dann eine Seite mit Freigabe-Link und deinem Termin-Link.

Ohne diese Schritte sehen Outlook-Kunden den Hinweis auf den kostenlosen Termin mit dir. Im Call: Testen ohne Postfach einrichten (geht immer).

## D. Ablauf Onboarding-Call (15 Minuten)

1. **Zwei Fragen:** Wie viele Angebote schreibst du im Monat? Wie viele davon hörst du nie wieder?
2. **Testen live:** Er meldet sich während des Calls auf `app.locadas.de/testen` an und leitet sein letztes offenes Angebot weiter. Ihr wartet zusammen auf „Erkannt“.
3. **Verbinden:** Wenn das sitzt, Postfach verbinden. GMX, Web.de und T-Online laufen mit dem normalen Passwort, Gmail braucht ein App-Passwort, Outlook läuft über „Mit Microsoft anmelden“.
4. **Einstellungen:** Signatur, Google-Bewertungslink und „Nachfassen per“ gemeinsam setzen. Alles andere bleibt auf Standard.
5. **Notiz:** Im Cockpit beim Betrieb „Onboarding-Call erledigt“ setzen.

## E. Erste Woche beobachten

- **Jeden Morgen** ins Cockpit: gestörte Postfächer, Fehler-Feed, falsch erkannte Dokumente („Falsch erkannt“ der Tester).
- **Tagesbericht** kommt um 19 Uhr.
- **Größtes Risiko:** die Erkennung auf echten Angeboten. Die ersten 20 Dokumente selbst ansehen.
- **Rechtliches:** Datenschutz und AVV sind Entwürfe. Vor dem großen Rollout kurz vom Anwalt prüfen lassen, vor allem die WhatsApp-Einwilligung.

## F. Erste Nachricht an deine Leads

> Hi {Vorname}, kurze Frage: Wie viele deiner Angebote hörst du nie wieder? Ich habe ein Tool gebaut, das in deinem Namen nachhakt, bis der Kunde antwortet. Kostenlos und ohne Anmeldung: Du leitest einfach dein letztes Angebot weiter und siehst sofort, was passiert. Hier testen: app.locadas.de/testen. Wenn du willst, richten wir es in 15 Minuten zusammen ein.
