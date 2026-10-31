# WhatsApp-Sekretär einrichten

Alles, was an den Betrieb geht (Morgen-Vorschau, Kunde hat geöffnet, Kunde hat geantwortet, Mail kam nicht an, Freigaben, „Wie ist es ausgegangen?“, Login-Link), läuft über einen Kanal (`locadas/notify/`):

1. **WhatsApp**, wenn der Server eingerichtet ist (`WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`) und der Betrieb seine Nummer hinterlegt und das Häkchen gesetzt hat.
2. Sonst, oder wenn WhatsApp fehlschlägt: **Mail ins eigene Postfach** wie bisher.

Innerhalb von 24 Stunden nach der letzten Nachricht des Betriebs schickt Locadas frei formulierte Nachrichten mit bis zu drei Knöpfen. Außerhalb dieses Fensters erlaubt Meta nur freigegebene Vorlagen. Die Vorlagen unten müssen deshalb genau so im Meta Business Manager eingereicht werden (Name, Sprache, Reihenfolge der Variablen und Knöpfe). Ein Login-Link geht außerhalb des Fensters immer per Mail.

## 1. Einmalig bei Meta

1. **Meta Business Manager** (business.facebook.com): Unternehmen anlegen bzw. verifizieren (Locadas GmbH, Handelsregisterauszug bereithalten).
2. **developers.facebook.com → App erstellen** → Typ „Business“ → Produkt **WhatsApp** hinzufügen, mit dem Business-Konto verknüpfen.
3. **Telefonnummer** für den Sekretär hinzufügen (eigene Nummer, auf der noch kein normales WhatsApp läuft), Anzeigename „Locadas“ beantragen und bestätigen lassen.
4. **Zahlungsmethode** im WhatsApp-Manager hinterlegen (Utility-Vorlagen kosten pro Nachricht, Antworten im 24h-Fenster sind frei).
5. **Dauerhaftes Token**: Business-Einstellungen → Systembenutzer → neuen Systembenutzer (Admin) anlegen → App und WhatsApp-Konto zuweisen → Token erzeugen mit `whatsapp_business_messaging` und `whatsapp_business_management`. Das ist `WHATSAPP_TOKEN`.
6. **Phone Number ID**: WhatsApp → API-Setup → „Phone number ID“ der Nummer. Das ist `WHATSAPP_PHONE_NUMBER_ID`.
7. **App Secret**: App-Einstellungen → Allgemein → „App-Geheimcode“. Das ist `WHATSAPP_APP_SECRET` (prüft die Signatur jedes Webhooks).
8. **Webhook**: WhatsApp → Konfiguration → Callback-URL `https://app.locadas.de/webhook/whatsapp`, Verify-Token = ein selbst gewählter Zufallsstring (`WHATSAPP_VERIFY_TOKEN`). Danach das Feld **messages** abonnieren.
9. **Vorlagen** aus Abschnitt 3 im WhatsApp-Manager → Nachrichtenvorlagen einreichen und auf „Aktiv“ warten.
10. App in den **Live-Modus** schalten.

## 2. Secrets auf dem Server

Als GitHub-Secrets anlegen (landen über den Deploy-Workflow in `app/.env`, nie ins Repo schreiben):

| Secret | Inhalt |
|---|---|
| `WHATSAPP_TOKEN` | dauerhaftes Systembenutzer-Token |
| `WHATSAPP_PHONE_NUMBER_ID` | Phone Number ID |
| `WHATSAPP_APP_SECRET` | App-Geheimcode |
| `WHATSAPP_VERIFY_TOKEN` | selbst gewählter Zufallsstring, gleich wie in der Webhook-Konfiguration |

Optional als Umgebungsvariablen: `WHATSAPP_API_VERSION` (Standard `v21.0`), `WHATSAPP_TEMPLATE_LANG` (Standard `de`), `WHATSAPP_TEMPLATES` zum Umbenennen einzelner Vorlagen, z. B. `preview=locadas_preview_v2,event=locadas_event_v2`.

Ohne diese Werte läuft alles wie bisher per Mail. Die Felder im Onboarding sind trotzdem sichtbar, gespeicherte Nummern werden genutzt, sobald WhatsApp eingerichtet ist (die Begrüßung kommt dann mit der nächsten Änderung der Nummer bzw. des Häkchens).

## 3. Vorlagen (Kategorie „Utility“, Sprache „German“ / `de`)

Variablen in `{{1}}`-Schreibweise, Knöpfe als **Schnellantwort** (Quick Reply) in genau dieser Reihenfolge. Die Beispielwerte braucht Meta zur Prüfung.

### locadas_welcome
Text:
> Hallo {{1}}, ich bin ab jetzt dein Locadas-Sekretär. Ich melde mich hier, bevor ich bei deinen Kunden nachfasse und wenn ein Kunde dein Angebot öffnet oder antwortet. Du antwortest einfach mit einem Knopf. Schreib „abmelden“, wenn du keine Nachrichten mehr willst.

Knöpfe: `Alles klar`
Beispiel: {{1}} = Thomas

### locadas_preview
Text:
> Hallo {{1}}, {{2}} fasse ich bei {{3}} nach. Hat sich schon jemand gemeldet, zum Beispiel am Telefon? Dann tipp auf „Hat angerufen“. Sonst musst du nichts tun.

Knöpfe: `Hat angerufen`, `Passt`
Beispiel: {{1}} = Thomas, {{2}} = heute ab 10 Uhr, {{3}} = Familie Schmidt, Peter Weber

{{2}} ist die echte Versandzeit („heute ab 12:30 Uhr“, „morgen ab 8 Uhr“, „am Montag, 03.11., ab 8 Uhr“).

### locadas_opened
Text:
> Hallo {{1}}, {{2}} hat gerade {{3}} geöffnet. Guter Moment für einen kurzen Anruf.

Knöpfe: `Jetzt anrufen`, `Erledigt`
Beispiel: {{1}} = Thomas, {{2}} = Familie Schmidt, {{3}} = Angebot A-2026-118

### locadas_reply
Text:
> Hallo {{1}}, {{2}} hat auf {{3}} geantwortet: {{4}} Passt das so?

Knöpfe: `Passt`, `Weiter nachfassen`
Beispiel: {{1}} = Thomas, {{2}} = Familie Schmidt, {{3}} = Angebot A-2026-118, {{4}} = Sagt zu, Start im März. Ich habe das Angebot als „Auftrag bekommen“ markiert und fasse nicht mehr nach.

### locadas_approval
Text:
> Hallo {{1}}, ich würde jetzt bei {{2}} nachfassen ({{3}}). Diesen Text würde ich in deinem Namen schicken: {{4}} Soll ich ihn senden?

Knöpfe: `Senden`, `Diesmal nicht`, `Hat sich gemeldet`
Beispiel: {{1}} = Thomas, {{2}} = Familie Schmidt, {{3}} = Angebot A-2026-118 über 3.724,70 €, {{4}} = Guten Tag Familie Schmidt, ich wollte kurz nachhören, ob Sie unser Angebot schon anschauen konnten. Viele Grüße Thomas Müller

Der volle Text muss in {{4}} passen (Meta begrenzt Variablen). Ist er länger, geht die Freigabe außerhalb des 24-Stunden-Fensters per Mail.

### locadas_invoice_open
Text:
> Hallo {{1}}, {{2}} Ich sehe dein Konto nicht, deshalb frage ich vor jeder Zahlungserinnerung.

Knöpfe: `Noch offen`, `Bezahlt`
Beispiel: {{1}} = Thomas, {{2}} = Rechnung R-2026-301 an Peter Weber über 224,91 € ist seit 03.11.2026 fällig. Noch offen?

### locadas_outcome_offer
Text:
> Hallo {{1}}, bei {{2}} ({{3}}) habe ich alles nachgefasst und per Mail kam keine Antwort. Hast du den Auftrag bekommen?

Knöpfe: `Auftrag bekommen`, `Nicht bekommen`
Beispiel: {{1}} = Thomas, {{2}} = Familie Schmidt, {{3}} = Angebot A-2026-118 über 3.724,70 €

### locadas_outcome_invoice
Text:
> Hallo {{1}}, bei {{2}} ({{3}}) habe ich alles nachgefasst und per Mail kam keine Antwort. Ist die Rechnung bezahlt?

Knöpfe: `Ist bezahlt`, `Nicht bezahlt`
Beispiel: {{1}} = Thomas, {{2}} = Peter Weber, {{3}} = Rechnung R-2026-301 über 224,91 €

### locadas_event
Text:
> Hallo {{1}}, kurze Info von deinem Locadas-Sekretär: {{2}} Bei Fragen schreib mir einfach hier.

Knöpfe: `Alles klar`
Beispiel: {{1}} = Thomas, {{2}} = Die Mail an Familie Schmidt zu Angebot A-2026-118 kam nicht an, die Adresse ist unzustellbar. Schick mir hier die richtige Adresse, dann fasse ich weiter nach.

## 4. Was der Sekretär versteht

- **Knöpfe**: Jeder Knopf trägt eine signierte Aktion (`SECRET_KEY`, 60 Tage gültig). Zweimal drücken schadet nicht.
  - „Hat angerufen“ / „Stopp: Name“ / „Hat sich gemeldet“: nicht mehr nachfassen
  - „Passt“, „Alles klar“, „Erledigt“: nur bestätigen
  - „Senden“ / „Diesmal nicht“: Freigabe, danach kommt die nächste offene Freigabe
  - „Bezahlt“ / „Erledigt“ / „Bitte nachfassen“: Frage zu einem Vorgang von vor dem Start. Erst „Bitte nachfassen“ plant eine Nachfass-Mail (normaler Ablauf mit Vorschau). Diese Frage hat keine Vorlage: außerhalb des 24h-Fensters kommt sie per Mail.
  - „Auftrag bekommen“ / „Nicht bekommen“ / „Ist bezahlt“ / „Nicht bezahlt“: Ergebnis notieren, danach die nächste offene Frage
  - „Jetzt anrufen“: Mailadresse und Link aufs PDF (Telefonnummer des Kunden kennen wir nicht, sie steht im Dokument)
  - „Weiter nachfassen“: Vorgang wieder öffnen
- **Text**: „Stopp 3“ stoppt den dritten Kunden aus der letzten Liste. Eine Mailadresse nach „Mail kam nicht an“ wird als neue Kundenadresse übernommen. „abmelden“ schaltet WhatsApp ab. Alles andere bekommt eine kurze Hilfe.

## 5. Ruhe-Regeln

- Höchstens eine Vorschau pro Tag (gebündelt), „Kunde hat geöffnet“ höchstens einmal pro Dokument.
- Freigaben und Abschlussfragen kommen einzeln nacheinander: die nächste erst, wenn die vorige beantwortet ist.
- Alles andere nur, wenn etwas passiert.
