# Nachfasser: Deploy und Betrieb

Die App liegt in `app/`, läuft als ein Docker-Container (Python/FastAPI, SQLite) plus Caddy für HTTPS.
Deploy: jeder Push auf `main`, der etwas in `app/` ändert → GitHub Actions testet und spielt per SSH auf den AWS-Server.

## 1. Einmalig: GitHub-Secrets setzen

Repo → Settings → Secrets and variables → Actions → **Secrets**:

| Secret | Inhalt |
|---|---|
| `AWS_HOST` | IP oder Hostname des AWS-Servers (z. B. `3.120.12.34`) |
| `AWS_USER` | SSH-Benutzer, meist `ubuntu` (bei Amazon Linux `ec2-user`) |
| `AWS_SSH_KEY` | privater SSH-Key (PEM-Inhalt, komplett mit BEGIN/END-Zeilen) |
| `APP_SECRET_KEY` | `python3 -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `APP_ENCRYPTION_KEY` | `python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` – **nie ändern**, sonst sind alle Postfach-Zugangsdaten unlesbar |
| `ANTHROPIC_API_KEY` | der Claude-Key (ohne Key läuft nur die Regex-Heuristik, der Angebots-Check ist dann aus) |
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` | optional, WhatsApp-Sekretär (Einrichtung und Vorlagen: `WHATSAPP.md`). Ohne geht alles per Mail. |

Optional als **Variables**: `APP_DOMAIN` (Default `app.locadas.de`), `APP_BASE_URL` (Default `https://app.locadas.de`), `CLAUDE_MODEL`, `CLAUDE_FAST_MODEL`, `APPROVAL_THRESHOLD`.

Ohne `SECRET_KEY` und `ENCRYPTION_KEY` bricht die App beim Start mit einer klaren Meldung im Log ab. `ALLOW_INSECURE_DEV=1` ist nur für Entwicklung, Tests und Demo gedacht (erlaubt Standard-Schlüssel und Mailserver auf internen Adressen) und darf in Produktion nie gesetzt sein.

Ohne `AWS_HOST` läuft der Workflow nur die Tests und überspringt den Deploy (mit Hinweis im Log).

## 2. Einmalig: Server und DNS

- Security Group: Port 22 (nur deine IP), 80, 443 und 25 (alle) offen. Port 25 nur für die Weiterleitungsadressen, siehe 2a.
- DNS: `A`-Record `app.locadas.de` → Server-IP. Caddy holt das Zertifikat selbst (Let's Encrypt), sobald DNS steht.
- Docker wird vom Workflow installiert, falls es fehlt (braucht `sudo` ohne Passwort, bei Ubuntu-AMIs Standard).
- **Läuft auf dem Server schon etwas auf Port 80/443** (altes Marktplatz-Backend)? Dann in `docker-compose.yml` den `caddy`-Dienst entfernen und die App stattdessen hinter den vorhandenen Reverse-Proxy hängen (`ports: ["127.0.0.1:8000:8000"]` beim `app`-Dienst, Proxy auf `http://127.0.0.1:8000`).

## 2a. Testen ohne Postfach (`/testen`, Weiterleitungsadressen)

Tester melden sich nur mit Name, Betrieb und Handynummer an und bekommen eine Adresse wie `mueller-haustechnik-k3x9pq@angebot.locadas.de`. Mails an diese Adressen nimmt die App selbst an (eingebauter SMTP-Empfänger, `aiosmtpd`) oder über einen Mail-Anbieter per Webhook. Ein Weg reicht.

**Weg A: eigener SMTP-Empfänger (Standard in `docker-compose.yml`)**
- DNS: `MX`-Record für `angebot.locadas.de` → `app.locadas.de` (Priorität 10). Dazu muss `app.locadas.de` einen `A`-Record auf den Server haben (gibt es schon). Kein SPF/DKIM nötig, wir senden von dieser Domain nichts.
- AWS Security Group: **Port 25 eingehend** für alle (0.0.0.0/0) öffnen. Ausgehend braucht es Port 25 nicht.
- `docker-compose.yml` mappt außen 25 auf 2525 im Container (`INBOUND_SMTP_PORT=2525`), die App läuft weiter ohne Root.
- Auf dem Server darf nichts anderes auf Port 25 lauschen (`sudo ss -ltnp | grep :25`, ggf. `sudo systemctl disable --now postfix`).
- Sicherheit: Angenommen wird nur bei `RCPT TO` an eine existierende Adresse `@angebot.locadas.de`, alles andere wird mit 550 abgelehnt (kein offenes Relay). Höchstens 25 MB je Mail, höchstens 20 Mails je Adresse und Stunde (`INBOUND_PER_HOUR`).
- Prüfen: `swaks --to <adresse>@angebot.locadas.de --server app.locadas.de --attach angebot.pdf` (oder einfach eine Mail weiterleiten). Im Log steht `Eingang per SMTP: created`.
- **STARTTLS:** Der Empfänger bietet STARTTLS an (opportunistisch, also nicht Pflicht). Ohne eigene Einstellung erzeugt die App beim ersten Start ein selbst signiertes Zertifikat für `INBOUND_DOMAIN` in `DATA_DIR/tls/` (bleibt im Volume). Sendende Mailserver prüfen bei opportunistischem TLS das Zertifikat nicht, die Verbindung ist trotzdem verschlüsselt. Wer ein echtes Zertifikat will: `INBOUND_TLS_CERT` und `INBOUND_TLS_KEY` auf PEM-Dateien im Container zeigen lassen (z. B. per Volume aus Caddy oder certbot). Prüfen: `openssl s_client -starttls smtp -connect app.locadas.de:25 -crlf` zeigt das Zertifikat.

**Weg B: Mail-Anbieter mit Rohmail-Webhook (Mailgun, Amazon SES, Postmark)**
- Beim Anbieter die Domain `angebot.locadas.de` als Empfangsdomain einrichten (MX-Records laut Anbieter) und eine Route „alles an `*@angebot.locadas.de`“ → `POST https://app.locadas.de/inbound/raw` mit der **Rohmail (MIME) als Body**.
- Header `X-Inbound-Secret: <INBOUND_WEBHOOK_SECRET>` mitschicken, optional `X-Envelope-To: <Empfänger>` (für BCC) und `X-Authentication-Results`.
- Antworten: 200 verarbeitet, 404 unbekannte Adresse, 403 falsches Secret, 413 zu groß, 429 Rate-Limit.
- Dann in `docker-compose.yml` das `ports: "25:2525"` entfernen.

**Umgebungsvariablen** (GitHub: Secrets bzw. Variables, siehe Workflow):

| Variable | Inhalt |
|---|---|
| `ADMIN_EMAILS` (Variable) | Kommaliste der Adressen mit Zugang zum Cockpit `/admin`. Leer = kein Cockpit, keine Mail-Alarme |
| `ADMIN_WHATSAPP` (Secret) | Handynummer für Alarme und Tagesbericht (E.164 ohne +). Leer = Alarme per Mail an `ADMIN_EMAILS` |
| `ADMIN_NAME`, `ADMIN_DIGEST_HOUR` | Anrede in den Alarmen (Standard `Julius`), Stunde des Tagesberichts (Standard `19`) |
| `BACKUP_S3_BUCKET`, `BACKUP_S3_PREFIX` | optional, Sicherung zusätzlich nach S3 (braucht boto3, siehe 3b) |
| `SCAN_WORKERS`, `SCAN_ACCOUNT_TIMEOUT` | Postfächer parallel lesen (Standard 8), Zeitlimit je Postfach in Sekunden (Standard 300) |
| `INBOUND_TLS_CERT`, `INBOUND_TLS_KEY` | optional, eigenes Zertifikat für STARTTLS am SMTP-Empfänger (Pfade im Container). Leer = selbst signiert |
| `INBOUND_DOMAIN` | Standard `angebot.locadas.de` |
| `INBOUND_SMTP_PORT` | Port des SMTP-Empfängers im Container, Standard `2525` (leer = aus) |
| `INBOUND_WEBHOOK_SECRET` | Secret, langer Zufallswert (`python3 -c "import secrets;print(secrets.token_urlsafe(32))"`). Leer = Webhook aus |
| `WHATSAPP_DISPLAY_NUMBER` | Nummer des WhatsApp-Sekretärs für den Link „Chat mit dem Sekretär öffnen“ auf `/testen/los` (E.164 ohne +, z. B. `4921519999999`). Leer = nur Weiterleiten |
| `SYSTEM_SMTP_HOST`, `SYSTEM_SMTP_PORT`, `SYSTEM_SMTP_USER`, `SYSTEM_SMTP_PASSWORD`, `SYSTEM_FROM` | Versand an Tester, die kein WhatsApp-Fenster offen haben und eine Mailadresse angegeben haben (z. B. ein Postfach `info@locadas.de`). Ohne: nur WhatsApp |
| `CALENDLY_URL` | Standard `https://calendly.com/julius-locadas/julius-komorek` |
| `TRIAL_MAX_DOCS`, `TRIAL_WINDOW_DAYS`, `TRIAL_DELETE_AFTER_DAYS` | Standard 10 Dokumente je 30 Tage, Löschen nach 60 Tagen ohne Aktivität (Scheduler alle 12 Stunden) |

**WhatsApp**: Für PDFs und Fotos an den Sekretär muss im Meta-Webhook das Feld `messages` abonniert sein (ist es schon). Die App lädt die Datei über `GET /{media-id}` und dann die URL mit demselben Token. Fotos liest Claude (`CLAUDE_FAST_MODEL`), ohne `ANTHROPIC_API_KEY` bitten wir um ein PDF. Außerhalb des 24h-Fensters nutzt der Test die Vorlage `locadas_event` (keine neue Vorlage nötig).

## 3. Deploy auslösen

Push auf `main` oder Actions → „Deploy Nachfasser (app/) to AWS“ → Run workflow. Danach: `https://app.locadas.de/health` muss `{"ok":true}` liefern.

Auf dem Server liegt alles unter `~/locadas-app`:

```
cd ~/locadas-app
docker compose logs -f app        # Live-Log (jeder Durchlauf loggt "Durchlauf: {...}")
docker compose restart app
docker compose exec app python -c "from locadas.engine.scheduler import run_cycle;print(run_cycle())"   # Scan sofort
```

Daten (SQLite, PDFs, Verschlüsselungs-Key-Fallback) liegen im Docker-Volume `locadas-app_locadas-data`. Komplettes Volume (inklusive PDFs) von Hand sichern: `docker run --rm -v locadas-app_locadas-data:/data -v $PWD:/out alpine tar czf /out/locadas-backup.tgz /data`.

Health-Check: `GET /healthz` liefert JSON mit `db`, `scheduler`, `last_cycle_age_s` und `inbound_smtp`, Status 200 wenn alles läuft, sonst 503. Docker nutzt ihn als Healthcheck (`docker compose ps` zeigt `healthy`). `/health` bleibt als einfacher Ping.

## 3a. Admin-Cockpit und Alarme

- `ADMIN_EMAILS` (Variable, Kommaliste) setzen, z. B. `julius@locadas.de`. Wer mit einem Betrieb unter dieser Adresse angemeldet ist, sieht `/admin`. Sonst unter `/admin/login` einen Magic-Link anfordern (2 Stunden gültig, geht an `ADMIN_WHATSAPP` bzw. per Mail). Für alle anderen ist `/admin` eine 404.
- Cockpit: Kennzahlen (Anmeldungen, Verbindungen, Dokumente, Nachfassen, Antworten, gestörte Postfächer, nicht zugestellte Nachrichten, Claude-Aufrufe und geschätzte Kosten, WhatsApp-Vorlagen und Kosten), Funnel `/testen`, Liste aller Betriebe mit Suche und Filter, Detailseite mit Pausieren, „Jetzt scannen“, „Login-Link schicken“ und Notiz „Onboarding-Call erledigt“, Fehler-Feed (Log ab WARNING).
- `ADMIN_WHATSAPP` (Secret, E.164 ohne +): Alarme bei neuer Anmeldung, neu verbundenem Postfach, gestörtem Postfach und Fehlern im Hintergrund (höchstens einer je 15 Minuten und Art), dazu der Tagesbericht um 19 Uhr. Außerhalb des 24h-Fensters geht das als Vorlage `locadas_event` (kostet wie jede Utility-Vorlage). Schreib dem Sekretär einmal am Tag irgendwas, dann laufen die Alarme 24 Stunden kostenlos als normale Nachricht. Ohne WhatsApp gehen Alarme per Mail an `ADMIN_EMAILS` (über `SYSTEM_SMTP_*` oder das verbundene Postfach des Admin-Betriebs).

## 3b. Sicherung und Wiederherstellung

- Jede Nacht um 03:15 legt die App mit dem SQLite-Online-Backup eine konsistente Kopie an: `DATA_DIR/backups/locadas-YYYYMMDD.sqlite.gz` (im Volume), die neuesten 14 bleiben (`BACKUP_KEEP`). Das Cockpit zeigt die letzte Sicherung, dort gibt es auch „Sicherung jetzt“. Schlägt sie fehl, kommt ein Alarm.
- Optional zusätzlich nach S3: Variable `BACKUP_S3_BUCKET` (und `BACKUP_S3_PREFIX`) setzen, `boto3` ins Image nehmen (`pip install boto3` in der `Dockerfile`) und der Instanz eine IAM-Rolle mit `s3:PutObject` auf den Bucket geben. Ohne boto3 steht nur ein Hinweis im Log, lokal wird trotzdem gesichert. Im Bucket eine Lifecycle-Regel (z. B. 30 Tage) anlegen.
- Die PDFs (`DATA_DIR/pdf`) sind nicht in dieser Sicherung. Sie liegen im selben Volume, das Volume einmal pro Woche per `tar` (siehe oben) oder per EBS-Snapshot sichern.
- Sicherungen auflisten: `docker compose exec app ls -la /data/backups`
- **Wiederherstellen:**
  ```
  cd ~/locadas-app
  docker compose stop app
  docker compose run --rm --no-deps app python -m locadas.ops.backup --restore /data/backups/locadas-20261101.sqlite.gz
  docker compose start app
  ```
  Das überschreibt `/data/locadas.db` und löscht die alten `-wal`/`-shm`-Dateien. Vorher die aktuelle Datei sichern, falls man sie noch braucht: `docker compose run --rm --no-deps app cp /data/locadas.db /data/locadas.db.vorher`.
- Von S3 zurück: Datei mit `aws s3 cp s3://<bucket>/locadas/locadas-YYYYMMDD.sqlite.gz .` holen, per `docker cp` nach `/data/backups/` im Container legen, dann wie oben.

## 4. Lokal ausprobieren (ohne echtes Postfach)

```
cd app
pip install -r requirements.txt pytest aiosmtpd reportlab
python demo.py
```

Startet einen Test-Mailserver mit drei Beispiel-PDFs und die App auf http://localhost:8000. Im Formular unter „Server manuell eintragen“ die angezeigten Ports eintragen. Nachfass-Mails erscheinen im Terminal statt bei Kunden.

Tests: `cd app && python -m pytest -q` (26 Tests, darunter ein kompletter Durchlauf über IMAP und SMTP gegen Testserver).

## 5. Was noch offen ist

- **Gmail-Ein-Klick und Outlook / Microsoft 365 (OAuth):** fertig gebaut (`/oauth/google/*`, `/oauth/microsoft/*`), erscheint auf der Verbinden-Seite automatisch, sobald die Secrets gesetzt sind. Google einmal anlegen:
  - Google: Cloud Console → APIs & Dienste → OAuth-Zustimmungsbildschirm (extern, App „Locadas Nachfasser“, Scope `https://mail.google.com/`) → Anmeldedaten → OAuth-Client-ID (Webanwendung), autorisierte Weiterleitungs-URI `https://app.locadas.de/oauth/google/callback`. Bis zur Google-Verifizierung können nur Test-Nutzer verbinden (unter Zielgruppe eintragen).
  - Microsoft: siehe nächster Abschnitt (Microsoft Graph).
  - Secrets `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET` in GitHub eintragen, neu deployen.

### Microsoft (Outlook.com und Microsoft 365) über Microsoft Graph

Alle Microsoft-Konten laufen über Microsoft Graph (`mail/graph.py`, `auth_type="graph"`), nicht mehr über IMAP/SMTP. Grund: In vielen Microsoft-365-Tenants sind IMAP und vor allem SMTP AUTH abgeschaltet, Graph geht trotzdem. Lesen: Gesendete Elemente und Posteingang. Senden: dieselbe MIME-Mail wie bisher über `POST /me/sendMail` (Antwort-Verkettung bleibt, Microsoft legt die Mail selbst in „Gesendete Elemente“ ab). Alte Konten mit `auth_type="oauth"` (IMAP) laufen weiter, wechseln beim nächsten „Mit Microsoft anmelden“ automatisch auf Graph und machen dort weiter, wo sie waren.

Einmal im Azure-Portal einrichten (ca. 15 Minuten), mit einem Konto, das App-Registrierungen anlegen darf (am besten im Locadas-eigenen Tenant, nicht in einem Kunden-Tenant):

1. https://entra.microsoft.com → **Identität → Anwendungen → App-Registrierungen → Neue Registrierung**
   - Name: `Locadas Nachfasser` (sehen die Kunden beim Anmelden)
   - Unterstützte Kontotypen: **Konten in einem beliebigen Organisationsverzeichnis (beliebiger Microsoft Entra ID-Mandant – mehrinstanzenfähig) und persönliche Microsoft-Konten (z. B. Skype, Xbox)**
   - Umleitungs-URI: Plattform **Web**, `https://app.locadas.de/oauth/microsoft/callback` (genau so, die App baut sie aus `BASE_URL` + `/oauth/microsoft/callback`; gleiche URI dient auch als Rückkehr nach der Admin-Freigabe)
2. **Übersicht**: „Anwendungs-ID (Client)“ kopieren → GitHub-Secret **`MICROSOFT_CLIENT_ID`**.
3. **Zertifikate & Geheimnisse → Neuer geheimer Clientschlüssel** (Laufzeit 24 Monate, Ablaufdatum im Kalender eintragen!) → den **Wert** (nicht die Geheimnis-ID) kopieren → GitHub-Secret **`MICROSOFT_CLIENT_SECRET`**. Nie ins Repo schreiben, das Repo ist öffentlich.
4. **API-Berechtigungen → Berechtigung hinzufügen → Microsoft Graph → Delegierte Berechtigungen**: `offline_access`, `openid`, `email`, `profile`, `Mail.ReadWrite`, `Mail.Send`, `User.Read`. Die alten Berechtigungen `IMAP.AccessAsUser.All` und `SMTP.Send` (Office 365 Exchange Online) können drinbleiben, solange es noch alte IMAP-Konten gibt. **Keine** Anwendungsberechtigungen (Application permissions) anlegen. „Administratorzustimmung erteilen“ ist hier nicht nötig (das gilt nur für den eigenen Tenant).
5. **Branding & Eigenschaften**: Startseiten-URL `https://locadas.de`, Link zu Nutzungsbedingungen und Datenschutzerklärung (`https://app.locadas.de/datenschutz`), Logo. Herausgeberdomäne `locadas.de` verifizieren (DNS-TXT bzw. Datei unter `/.well-known/microsoft-identity-association.json`).
6. **Herausgeberüberprüfung (Publisher Verification)** so bald wie möglich: braucht ein Microsoft-Partner-Network-Konto (MPN/Partner Center, kostenlos) mit verifizierter Firma (Locadas GmbH) und die verifizierte Herausgeberdomäne. Ohne sie sehen Microsoft-365-Nutzer „nicht überprüft“ und **in vielen Tenants dürfen Nutzer unverifizierten Apps gar nicht selbst zustimmen** (Standard seit 2020 für Mail-Berechtigungen). Dann erscheint bei uns automatisch die Seite „Einmal kurz deine IT fragen“ mit dem Admin-Freigabe-Link `https://login.microsoftonline.com/<domain oder organizations>/adminconsent?client_id=…&redirect_uri=…` und dem Angebot für den kostenlosen Termin.
7. GitHub → Repository → Settings → Secrets and variables → Actions: `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET` setzen (optional `MICROSOFT_TENANT`, Standard `common`; nicht ändern). Neu deployen.
8. Testen: mit einem privaten Outlook.com-Konto und einem Microsoft-365-Konto auf https://app.locadas.de/verbinden „Mit Microsoft anmelden“. Im Dashboard sollten nach ein paar Minuten die Angebote der letzten 30 Tage stehen. Eine Nachfass-Mail an sich selbst schicken und in Outlook prüfen, dass sie im selben Verlauf hängt und in „Gesendete Elemente“ liegt.

Gut zu wissen:
- Eigene Domains bei Microsoft 365 (MX `*.mail.protection.outlook.com`) erkennt die Verbinden-Seite per DNS und zeigt dann „Mit Microsoft anmelden“ statt Passwortfeld.
- Microsoft rotiert die Refresh-Tokens, die App speichert den jeweils neuen (sonst liefe die Verbindung nach 90 Tagen ab). Widerruft der Kunde den Zugriff oder ändert seine IT die Richtlinien, meldet der Verbindungs-Wächter das sofort mit Link zum Neu-Verbinden.
- Graph drosselt mit 429. Die App wartet kurz (Retry-After, höchstens 30 s), sonst geht es im nächsten Durchlauf weiter.
- Postfächer, die noch auf einem eigenen Exchange-Server liegen (nicht Exchange Online), gehen über Graph nicht. Die bekommen eine klare Meldung, Lösung: Passwort-Login mit eigenem Server über „Server manuell eintragen“.
- **Lese-Tracking** läuft über den Link in den Nachfass-Mails (`/a/<token>`): erstes Öffnen erzeugt einen Eintrag im Verlauf und eine Mail an den Betrieb, im Dashboard steht „geöffnet“. Die Ursprungsmail mit dem Angebot ist nicht getrackt, sie geht weiter direkt vom Betrieb raus. Abschaltbar in den Einstellungen.
- **Claude-Extraktion** ist mit gemockter API getestet, nicht live. Nach dem ersten echten Postfach im Log prüfen, ob `extraction_source=claude` auftaucht und Beträge stimmen.
- Rechnungen ohne erkennbares Zahlungsziel bekommen 14 Tage ab Versand (`DEFAULT_INVOICE_DUE_DAYS`).
