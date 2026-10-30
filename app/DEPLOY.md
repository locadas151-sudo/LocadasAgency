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

Optional als **Variables**: `APP_DOMAIN` (Default `app.locadas.de`), `APP_BASE_URL` (Default `https://app.locadas.de`), `CLAUDE_MODEL`.

Ohne `AWS_HOST` läuft der Workflow nur die Tests und überspringt den Deploy (mit Hinweis im Log).

## 2. Einmalig: Server und DNS

- Security Group: Port 22 (nur deine IP), 80 und 443 (alle) offen.
- DNS: `A`-Record `app.locadas.de` → Server-IP. Caddy holt das Zertifikat selbst (Let's Encrypt), sobald DNS steht.
- Docker wird vom Workflow installiert, falls es fehlt (braucht `sudo` ohne Passwort, bei Ubuntu-AMIs Standard).
- **Läuft auf dem Server schon etwas auf Port 80/443** (altes Marktplatz-Backend)? Dann in `docker-compose.yml` den `caddy`-Dienst entfernen und die App stattdessen hinter den vorhandenen Reverse-Proxy hängen (`ports: ["127.0.0.1:8000:8000"]` beim `app`-Dienst, Proxy auf `http://127.0.0.1:8000`).

## 3. Deploy auslösen

Push auf `main` oder Actions → „Deploy Nachfasser (app/) to AWS“ → Run workflow. Danach: `https://app.locadas.de/health` muss `{"ok":true}` liefern.

Auf dem Server liegt alles unter `~/locadas-app`:

```
cd ~/locadas-app
docker compose logs -f app        # Live-Log (jeder Durchlauf loggt "Durchlauf: {...}")
docker compose restart app
docker compose exec app python -c "from locadas.engine.scheduler import run_cycle;print(run_cycle())"   # Scan sofort
```

Daten (SQLite, PDFs, Verschlüsselungs-Key-Fallback) liegen im Docker-Volume `locadas-app_locadas-data`. Backup: `docker run --rm -v locadas-app_locadas-data:/data -v $PWD:/out alpine tar czf /out/locadas-backup.tgz /data`.

## 4. Lokal ausprobieren (ohne echtes Postfach)

```
cd app
pip install -r requirements.txt pytest aiosmtpd reportlab
python demo.py
```

Startet einen Test-Mailserver mit drei Beispiel-PDFs und die App auf http://localhost:8000. Im Formular unter „Server manuell eintragen“ die angezeigten Ports eintragen. Nachfass-Mails erscheinen im Terminal statt bei Kunden.

Tests: `cd app && python -m pytest -q` (22 Tests, darunter ein kompletter Durchlauf über IMAP und SMTP gegen Testserver).

## 5. Was noch offen ist

- **Outlook / Microsoft 365 und Gmail-Ein-Klick-Login (OAuth):** Code-Seite vorbereitet (`auth_type=oauth`, XOAUTH2 in IMAP und SMTP), aber es fehlen die App-Registrierungen. Google: Cloud Console → OAuth-Client (Web), Redirect `https://app.locadas.de/oauth/google/callback`, Scope `https://mail.google.com/`. Microsoft: Entra → App registration, Redirect `https://app.locadas.de/oauth/microsoft/callback`, Scopes `IMAP.AccessAsUser.All`, `SMTP.Send`, `offline_access`. Die Routen `/oauth/*` sind noch nicht gebaut; Secrets sind im Workflow schon vorgesehen.
- **Lese-Tracking** („Kunde hat das Angebot geöffnet“) gibt es beim Postfach-Weg nicht, nur Antwort-Erkennung. Dafür müsste das Angebot als Link über Locadas verschickt werden, das ist eine zweite Ausbaustufe.
- **Claude-Extraktion** ist mit gemockter API getestet, nicht live. Nach dem ersten echten Postfach im Log prüfen, ob `extraction_source=claude` auftaucht und Beträge stimmen.
- Rechnungen ohne erkennbares Zahlungsziel bekommen 14 Tage ab Versand (`DEFAULT_INVOICE_DUE_DAYS`).
