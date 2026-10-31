# WhatsApp-Nachfasser über die eigene Nummer (Stufe 2, Coexistence)

Locadas fasst bei Kunden direkt per WhatsApp nach, und zwar von der Nummer des Handwerksbetriebs. Der Betrieb nutzt seine **WhatsApp Business App** weiter wie gewohnt. Dieselbe Nummer wird per **Embedded Signup** zusätzlich an die Cloud API angebunden („Coexistence“). Was Locadas per API schickt, steht im Chat seiner App. Antworten der Kunden kommen in der App an und gleichzeitig per Webhook bei uns.

Stand der Recherche: 31.10.2026. Quellen am Ende, Meta ändert hier oft. Vor dem Livegang die markierten Punkte noch einmal prüfen.

## Was im Code fertig ist

| Teil | Wo |
|---|---|
| Datenmodell (`wa_connections`, `wa_templates`, `wa_contacts`, `wa_messages`, `wa_usage`, `Business.plan`, `FollowUp.wa_read_at`) | `locadas/db.py` (Block „Stufe 2“), Mini-Migration läuft automatisch |
| Konfiguration (eigene Datei, keine Überschneidung mit Stufe 1) | `locadas/channels/config.py` |
| Embedded Signup: Karte in den Einstellungen, Facebook JS SDK, `POST /wa-business/connect`, Trennen, Vorlagen abgleichen | `templates/_wa_business_card.html`, `locadas/channels/web.py`, `locadas/channels/onboarding.py` |
| Versand (Vorlage oder freier Text im 24h-Fenster), Fehler in klares Deutsch | `locadas/channels/wa_business.py` |
| Nachfass-Vorlagen (Angebot 1 bis 3, Rechnung 1 bis 3, Deutsch) | `locadas/channels/wa_templates.py` |
| Kanalwahl für die Engine: `select_and_send(session, followup)` | `locadas/channels/__init__.py` (Anleitung zum Einbau oben im Modul) |
| Webhook für die Nummern der Betriebe (Antworten, Echos, Lesebestätigungen, Vorlagen-Status) | `locadas/channels/webhook.py`, eingehängt in `/webhook/whatsapp` |
| Einwilligung je Kunde, Abmeldung per „STOP“ | `locadas/channels/contacts.py`, Formular auf der Dokumentseite |
| Zählung je Monat und Kategorie, Anzeige im Dashboard, Pro-Sperre | `locadas/channels/usage.py`, `templates/_wa_business_dashboard.html` |
| Tests ohne Netzwerk | `tests/test_wa_business.py` |

Alles ist aus, solange `WA_BUSINESS_ENABLED` nicht `1` ist und `META_APP_ID`, `META_APP_SECRET` (oder `WHATSAPP_APP_SECRET`) und `META_CONFIG_ID` fehlen. Dann gibt es keine Karte in den Einstellungen und `select_and_send` liefert immer `None` (Mail wie bisher).

**Noch nicht verdrahtet:** Der Aufruf von `select_and_send` in `followups.process_due` und `followups.approve`. Das gehört dem Entwickler von Stufe 1. Die genaue Stelle steht oben in `locadas/channels/__init__.py`.

## Was Julius bei Meta tun muss

### 1. Tech Provider werden
Coexistence gibt es nur für **Solution Partner oder Tech Provider**. Für Locadas reicht Tech Provider: Meta rechnet dann direkt mit jedem Betrieb ab (jeder Betrieb hinterlegt seine eigene Zahlungsmethode im WhatsApp Manager), Locadas braucht keine Kreditlinie.

1. **Business-Verifizierung** der Locadas GmbH im Meta Business Manager abschließen (Handelsregisterauszug, Adresse, Website, Domain bestätigen). Ohne Verifizierung keine erweiterten Berechtigungen.
2. In der bestehenden App (developers.facebook.com, Typ „Business“, Produkt WhatsApp) den Weg **„Tech Provider“** starten (App Dashboard → WhatsApp → Quickstart bzw. „Become a Tech Provider“).
3. **App Review** mit „Advanced Access“ für:
   - `whatsapp_business_management` (Vorlagen anlegen, Konten und Nummern lesen, Webhooks abonnieren)
   - `whatsapp_business_messaging` (Nachrichten senden)
   - `business_management` (Business-Portfolio des Kunden lesen, für Embedded Signup nötig)
   
   Für den Review braucht Meta je Berechtigung ein kurzes **Screencast-Video**: Betrieb verbindet sich in den Einstellungen, eine Nachfass-Vorlage geht an eine Testnummer raus, die Antwort stoppt das Nachfassen. Dazu eine Datenschutzerklärung mit WhatsApp-Abschnitt und eine Anleitung, wie Meta sich einloggen kann (Test-Betrieb mit Login-Link).
4. **App-Modus „Live“**.

### 2. Embedded Signup konfigurieren
1. App Dashboard → **Facebook Login for Business** → Konfigurationen → neue Konfiguration aus der Vorlage **„WhatsApp Embedded Signup“**. Token-Typ: System User Access Token (Business Integration), Assets: WhatsApp-Konten, Berechtigungen wie oben. Die **Konfigurations-ID** ist `META_CONFIG_ID`.
2. **Erlaubte Domains** für das JS SDK: `app.locadas.de` (Facebook Login → Einstellungen → „Allowed Domains for the JavaScript SDK“, „Login with the JavaScript SDK“ einschalten).
3. Embedded Signup **v4** verwenden. v2 und v3 sind seit 15.10.2026 abgeschaltet. Unser Aufruf setzt `extras.featureType = "whatsapp_business_app_onboarding"` und `sessionInfoVersion = "3"`. In v4 startet der Coexistence-Weg laut Meta auch automatisch, sobald der Betrieb eine Nummer eingibt, die schon in der WhatsApp Business App läuft. In v5 ist Coexistence Standard. Falls Meta den `featureType` später ablehnt: `META_ES_FEATURE_TYPE` leer setzen.
4. **Webhooks** (App Dashboard → WhatsApp → Konfiguration), Callback wie beim Sekretär `https://app.locadas.de/webhook/whatsapp`, gleicher Verify-Token. Zusätzlich abonnieren: `messages`, `smb_message_echoes`, `history`, `smb_app_state_sync`, `account_update`, `message_template_status_update`, `template_category_update`, `phone_number_quality_update`.
5. Ist der WhatsApp-Sekretär (Locadas-Nummer) in **derselben App**, reicht ein App-Geheimcode. Liegt Stufe 2 in einer anderen App, `META_APP_SECRET` getrennt setzen. Der Webhook prüft beide Signaturen.

### 3. Secrets auf dem Server
Als GitHub-Secrets anlegen, landen über den Deploy-Workflow in `app/.env` (nie ins Repo):

| Variable | Inhalt |
|---|---|
| `WA_BUSINESS_ENABLED` | `1`, erst nach Freigabe durch Meta |
| `META_APP_ID` | App-ID |
| `META_APP_SECRET` | App-Geheimcode (leer = `WHATSAPP_APP_SECRET`) |
| `META_CONFIG_ID` | Konfigurations-ID aus Schritt 2.1 |
| `META_GRAPH_VERSION` | optional, Standard `v26.0` |
| `WHATSAPP_BUSINESS_PRICE_EUR` | optional, Utility-Preis für die Anzeige, Standard `0.0456` |
| `WHATSAPP_BUSINESS_PRICE_MARKETING_EUR` | optional, Standard `0.1131` |
| `WA_BUSINESS_REQUIRES_PRO` | optional, Standard `1` (nur Betriebe mit `plan = "pro"`) |

Paket freischalten: `UPDATE businesses SET plan='pro' WHERE email='…';` (eine Oberfläche dafür gibt es noch nicht).

### 4. Test vor dem Livegang
1. Eigene WhatsApp-Business-App-Nummer (nicht die Locadas-Nummer) als Test-Betrieb verbinden.
2. Prüfen: Karte zeigt „verbunden“, sechs Vorlagen „in Prüfung bei Meta“, nach Freigabe „freigegeben“.
3. Auf einer Dokumentseite eine eigene Zweitnummer mit Häkchen eintragen, Nachfassung auslösen, Nachricht im Chat der Business App sehen, lesen, Haken „gelesen“ im Dashboard.
4. Von der Zweitnummer antworten: Dokument steht auf „Kunde hat geantwortet“, Hinweis kommt beim Betrieb an.
5. „STOP“ schicken: Kontakt abgemeldet, nächste Nachfassung geht per Mail.

## Ablauf technisch

1. Betrieb tippt in den Einstellungen auf **„Mit WhatsApp verbinden“**. Das Facebook JS SDK öffnet Embedded Signup (`FB.login` mit `config_id`, `response_type: "code"`, `override_default_response_type: true`).
2. Das Popup meldet per `postMessage` (`type: "WA_EMBEDDED_SIGNUP"`, `event: "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"` bzw. `FINISH`) die `waba_id` und meist die `phone_number_id`. `FB.login` liefert den **Code** (30 Sekunden gültig). Die Seite schickt beides an `POST /wa-business/connect`.
3. Server: `GET /oauth/access_token?client_id&client_secret&code` → Business Integration System User Token (verschlüsselt gespeichert).
4. `POST /{waba_id}/subscribed_apps` → unsere App bekommt die Webhooks dieses Kontos.
5. Nummer lesen (`GET /{phone_number_id}?fields=display_phone_number,verified_name,quality_rating,messaging_limit_tier,is_on_biz_app,platform_type`). Fehlt die `phone_number_id`, über `GET /{waba_id}/phone_numbers`.
6. **Coexistence: keine Registrierung** (`/register` darf nicht aufgerufen werden, die Nummer ist schon registriert). Stattdessen innerhalb von **24 Stunden** Kontakte und Verlauf anfordern: `POST /{phone_number_id}/smb_app_data` mit `sync_type` `smb_app_state_sync` und `history`. Ohne Coexistence (neue Nummer): `POST /{phone_number_id}/register` mit 6-stelliger PIN.
7. Vorlagen abgleichen: `GET /{waba_id}/message_templates`, fehlende per `POST` anlegen. Status und Kategorie speichern wir.

Versand (`select_and_send`):
- Kunde hat in den letzten 24 Stunden geschrieben → **freier Text** (die WhatsApp-Variante aus Stufe 1, falls vorhanden, sonst der Mailtext). Dafür braucht es keine Vorlage.
- Sonst → **Vorlage**, aber nur mit gespeicherter Einwilligung und nur, wenn Meta die Vorlage freigegeben hat.
- Fehler → klarer Hinweis im Verlauf, Rückfall auf Mail. Kontofehler (Token abgelaufen, Konto gesperrt, keine Zahlungsmethode) setzen die Verbindung auf „Fehler“ und gehen per WhatsApp-Sekretär bzw. Mail an den Betrieb.

Webhook (`channels/webhook.py`):
- **messages** von Kunden: 24h-Fenster auf, Einwilligung „customer_initiated_chat“, Abmeldewörter (STOP, Stopp, abmelden, abbestellen, keine Nachrichten …) → abgemeldet mit kurzer Bestätigung. Sonst wird die Nachricht wie eine Mail-Antwort behandelt (Claude ordnet Absicht und Kurzfassung ein, wenn ein Schlüssel da ist), Nachfassen stoppt, Betrieb bekommt einen Hinweis.
- **smb_message_echoes**: Der Betrieb hat aus seiner App selbst geschrieben → wie „selbst nachgefasst“, die nächste Stufe rückt um mindestens 4 Tage nach hinten.
- **statuses**: `delivered`, `read` → `FollowUp.wa_read_at`, im Dashboard „auf WhatsApp gelesen“. Das ist eine echte Lesebestätigung, solange der Kunde Lesebestätigungen nicht abgeschaltet hat. `failed` → Schritt wird neu eingeplant und geht per Mail. `pricing.category` korrigiert die Zählung.
- **history**: Kunden, die dem Betrieb früher schon geschrieben haben, bekommen einen Hinweis auf der Dokumentseite. Das ist **keine** Einwilligung.
- **account_update** `PARTNER_REMOVED` (Betrieb hat in der App getrennt) → Verbindung getrennt. **template_category_update** → neue Kategorie gespeichert, Hinweis bei Umstufung auf Marketing.

## Kosten (Deutschland)

Meta rechnet seit 01.07.2025 **pro zugestellter Vorlage** ab, nach Kategorie. Seit **01.10.2026** gilt zusätzlich:

| Nachricht | Preis DE | Bemerkung |
|---|---|---|
| Utility-Vorlage | ca. **0,0456 €** | auch innerhalb des 24h-Fensters (vorher dort kostenlos) |
| Marketing-Vorlage | ca. **0,1131 €** | immer, plus Meta-Limit für Werbung pro Empfänger |
| Freier Text im 24h-Fenster (Service) | **1.000 pro Nummer und Monat frei**, danach ca. 0,0456 € | ohne Zahlungsmethode stoppt Meta nach der 1.000. |
| Nachrichten aus der WhatsApp Business App selbst | kostenlos | öffnen kein Cloud-API-Fenster |
| Eingehende Nachrichten | kostenlos | |

Beispiel: Ein Betrieb mit 40 Nachfassungen im Monat zahlt mit Utility-Vorlagen rund 1,80 €, mit Marketing-Einstufung rund 4,50 €. Die Rechnung kommt von Meta direkt an den Betrieb (Tech Provider), Locadas zeigt nur eine Schätzung. Preise können sich nur zum 1. Januar, April, Juli oder Oktober ändern.

## Kategorie: Utility oder Marketing

- **Zahlungserinnerungen** zu einer konkreten Rechnung sind laut Meta-Beispielen klar **Utility**, solange sie nichts bewerben.
- **Angebots-Nachfassungen** sind ein **Grenzfall**. Utility verlangt: ausgelöst durch eine Handlung des Kunden (er hat das Angebot angefragt), konkret auf den Vorgang bezogen (Nummer, Datum) und ohne werbende oder überredende Absicht. Alles mit „Angebot“, Rabatt, Dringlichkeit oder allgemeinem „Haben Sie Interesse?“ wird Marketing. Unsere Texte sind deshalb sachlich gehalten (angekommen? Fragen? Rückmeldung zur Planung).
- Meta stuft freigegebene Vorlagen **automatisch** um, mit 1 Tag Vorwarnung per Mail und Webhook `template_category_update`. Einspruch geht bis 60 Tage danach. `allow_category_change` gibt es nicht mehr. Nach einer Missbrauchs-Warnung entfällt die Vorwarnung.
- Risiko: Werden die Angebots-Vorlagen Marketing, kostet jede Nachricht etwa 2,5-mal so viel und Meta liefert Marketing pro Kunde nur begrenzt aus (Fehler 131049, dann fassen wir per Mail nach). Kunden können Marketing gezielt abbestellen (131050).
- Ändern wir einen Vorlagentext, erhöhen wir `VERSION` in `wa_templates.py`. Dann legt der nächste Abgleich neue Vorlagen an und Meta prüft neu.

## Einwilligung (Opt-in) und Abmeldung

Meta verlangt vor Nachrichten außerhalb des 24h-Fensters eine Einwilligung, in der klar steht, dass der Kunde WhatsApp-Nachrichten von **diesem Betrieb** bekommen möchte. Sie darf über jeden Kanal eingeholt werden (Formular, Gespräch, WhatsApp-Chat). Abmeldungen müssen respektiert werden. In Deutschland kommen DSGVO und UWG § 7 dazu.

Umsetzung:
- Vorlagen gehen **nur** an Kontakte mit `wa_opt_in_source`: `customer_initiated_chat` (Kunde hat nach dem Verbinden selbst geschrieben), `offer_form` (für ein späteres Anfrageformular) oder `manual` (Häkchen des Betriebs auf der Dokumentseite: „Kunde hat mir bereits auf WhatsApp geschrieben oder ist einverstanden“).
- Freier Text nur im 24h-Fenster nach einer Kundennachricht.
- „STOP“ und ähnliche Wörter melden ab. Jede Vorlage hat dazu den Hinweis in der Fußzeile.
- Empfehlung für den Betrieb (Vertrieb/Onboarding): Im Angebot oder in der Auftragsbestätigung einen Satz aufnehmen wie „Wir melden uns zu diesem Angebot gern auch per WhatsApp. Sagen Sie einfach Bescheid, wenn Sie das nicht möchten.“ Rechtlich sauberer ist ein aktives Häkchen. Das sollte ein Anwalt einmal prüfen.

## Voraussetzungen und Grenzen von Coexistence

- WhatsApp Business App **ab Version 2.24.17**, auf einem Handy mit Kamera (QR-Code beim Verbinden).
- Die Nummer sollte schon eine Weile aktiv in der Business App genutzt werden. Meta nennt selbst keine feste Zahl. Anbieter berichten von mindestens 7 Tagen, besser 1 bis 2 Monaten (nicht offiziell bestätigt).
- Die App muss **mindestens alle 14 Tage geöffnet** werden, sonst kann die Verbindung abreißen (laut Anbietern, nicht in Metas Doku gefunden).
- Verfügbarkeit in der EU: laut Anbietern seit Oktober/November 2025 auch für deutsche Nummern. Metas Doku nennt keine Länderliste. **Vor dem Livegang mit einer deutschen Nummer testen.**
- **Gruppen** werden nicht synchronisiert. In 1:1-Chats werden **verschwindende Nachrichten, einmal ansehen und Live-Standort abgeschaltet**, **Broadcast-Listen** werden schreibgeschützt.
- Begleitgeräte: höchstens 4. **WhatsApp für Windows und WearOS** werden nicht unterstützt, beim Verbinden werden alle Begleitgeräte abgemeldet.
- Durchsatz fest **20 Nachrichten pro Sekunde** (für uns egal).
- Verlauf: einmalig bis 180 Tage zurück, Medien nur 14 Tage. Abgleich muss innerhalb von 24 Stunden nach dem Verbinden angefordert werden.
- Nachrichten aus der App öffnen **kein** Cloud-API-Fenster. Nur Nachrichten, die der Kunde nach dem Verbinden schickt.
- Trennen geht nur aus der App (Einstellungen → Konto → Business-Plattform → Trennen) bzw. über unseren Knopf „Trennen“ (Webhook-Abo weg). Die Deregister-API geht bei Coexistence nicht.
- Fehler 131060: Nachricht konnte in der App nicht angezeigt werden (z. B. nicht unterstütztes Begleitgerät).
- Meta Verified (blauer Haken) kann beim Verbinden entfallen, der Anzeigename ist danach schwer zu ändern (laut Anbietern).

## Zeitplan (Schätzung)

| Schritt | Dauer |
|---|---|
| Business-Verifizierung | 2 bis 10 Werktage |
| Tech-Provider-Antrag und App Review (3 Berechtigungen, Videos) | 1 bis 3 Wochen, oft eine Nachbesserungsrunde |
| Embedded-Signup-Konfiguration, Webhooks, Secrets | 1 Stunde |
| Test mit eigener Nummer | 1 Tag (Vorlagen-Prüfung durch Meta meist Minuten bis 24 Stunden) |
| Pilot mit 2 bis 3 Betrieben | 2 Wochen |

## Offene Risiken

1. **Umstufung der Angebots-Vorlagen auf Marketing**: höhere Kosten, Zustell-Limits. Gegenmittel: sachliche Texte, Zahlen im Dashboard beobachten, notfalls Angebots-Nachfassungen nur im 24h-Fenster per WhatsApp und sonst per Mail.
2. **Einwilligung**: Die Verantwortung liegt beim Betrieb (Häkchen). Ein sauberes Opt-in im Anfrageformular (`offer_form`) wäre besser.
3. **Qualitätsbewertung**: Blockieren oder melden Kunden die Nachrichten, sinkt die Qualität der Nummer des Betriebs und Meta drosselt. Im Extremfall leidet seine ganze WhatsApp-Kommunikation. Wir zeigen die Qualität in der Karte an.
4. **Graph-API-Version**: Der Sekretär nutzt noch `v21.0` (`WHATSAPP_API_VERSION`). Graph-Versionen laufen nach etwa zwei Jahren aus. Bitte prüfen und auf eine aktuelle Version heben.
5. **Coexistence in Deutschland** ist nur über Anbieter-Seiten belegt, nicht in Metas Doku. Erst mit eigener Nummer testen.
6. **Service-Nachrichten kosten seit 01.10.2026** nach 1.000 freien pro Monat. Für Nachfassungen unkritisch, aber ohne Zahlungsmethode beim Betrieb stoppt Meta danach.
7. Die Zählung im Dashboard ist eine Schätzung. Maßgeblich ist die Rechnung von Meta.

## Quellen

- Meta: Onboarding WhatsApp Business app users (Coexistence): https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users
- Meta: Embedded Signup Implementation (FB.login, Session-Events): https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation/
- Meta: Onboarding business customers as a Tech Provider (Code-Tausch, subscribed_apps, register): https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-customers-as-a-tech-provider
- Meta: Embedded Signup v4 (v2/v3 abgeschaltet zum 15.10.2026): https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/version-4-public-preview/
- Meta: Pricing on the WhatsApp Business Platform: https://developers.facebook.com/docs/whatsapp/pricing und https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/
- Meta: Template categorization guidelines (Utility/Marketing, Umstufung, Einspruch): https://developers.facebook.com/docs/whatsapp/updates-to-pricing/new-template-guidelines
- Meta: Getting opt-in for WhatsApp: https://developers.facebook.com/docs/whatsapp/overview/getting-opt-in
- 360dialog: Coexistence Webhooks (Payload von smb_message_echoes, history, smb_app_state_sync): https://docs.360dialog.com/partner/onboarding/whatsapp-coexistence/coexistence-webhooks
- meetergo: WhatsApp-Preise ab Oktober 2026 (DE-Preise, 1.000 freie Service-Nachrichten, Utility im Fenster kostenpflichtig): https://help.meetergo.com/en/integrations/automation/whatsapp-pricing
- ChatMaxima: Service Message Pricing from October 1, 2026: https://chatmaxima.com/blog/?p=3623
- respond.io: Coexistence-Voraussetzungen (App 2.24.17, alle 14 Tage öffnen): https://respond.io/help/whatsapp/whatsapp-coexistence
- seven.io: Coexistence-Länder (Deutschland verfügbar), Grenzen: https://help.seven.io/en/whatsapp/whatsapp-coexistence
- Chakra: Coexistence in EU/UK seit Oktober 2025, häufige Fehler: https://chakrahq.com/article/whatsapp-coexistence-live-eu-uk-europe-whatsapp-business-for-api-live/ und https://chakrahq.com/article/issues-whatsapp-coexistence-onboarding-setup/amp/
- YCloud: Utility vs. Marketing seit 01.07.2025: https://www.ycloud.com/blog/whatsapp-api-message-template-category-guidelines-update
- AiSensy: Fehlercodes der Cloud API (131047, 131026, 131049 …): https://m.aisensy.com/blog/whatsapp-business-api-errors/
