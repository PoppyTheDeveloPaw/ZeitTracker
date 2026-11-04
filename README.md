# ZeitTracker

Arbeitszeiterfassung als Uhr-Symbol unten rechts in der Windows-Taskleiste.

## Installation

1. **Python 3.10+** installieren (python.org, beim Setup „Add Python to PATH“ anhaken).
2. Ordner entpacken, z. B. nach `C:\Tools\ZeitTracker`.
3. Doppelklick auf **`installieren.bat`** (installiert die benötigten Pakete).
4. Doppelklick auf **`zeittracker.pyw`** – das Uhr-Symbol erscheint unten rechts.
   Tipp: Ist es nur unter dem Pfeil `^` zu sehen, kann man es in die Taskleiste ziehen.

Optional als eigenständige EXE (ohne Python auf dem Zielrechner): **`build_exe.bat`** ausführen,
die Datei liegt danach in `dist\ZeitTracker.exe`.

## Release über GitHub

Der Workflow `.github/workflows/release.yml` baut bei jedem Tag `v*` automatisch die EXE und
veröffentlicht sie als GitHub-Release:

```
git tag v1.0.0
git push origin v1.0.0
```

Ablauf auf einem Windows-Server von GitHub: Tests ausführen → `build_exe.bat` → Release
„ZeitTracker v1.0.0“ mit `ZeitTracker.exe` und Prüfsumme (`ZeitTracker.exe.sha256`) sowie automatisch
erzeugten Änderungsnotizen. Schlagen die Tests fehl, wird kein Release angelegt.

Die EXE heißt in jedem Release gleich, die Versionsnummer steht in der Datei selbst
(Rechtsklick › Eigenschaften › Details, erzeugt von `make_version_info.py`). So bleiben
Autostart-Eintrag und Verknüpfungen gültig, wenn die EXE von Hand durch eine neue ersetzt wird.
Ist der Autostart aktiv, setzt der ZeitTracker den Eintrag beim Start außerdem immer auf die
gerade laufende EXE – auch nach Verschieben oder Umbenennen.

## Updates

Die Release-EXE sucht beim Start und danach täglich auf GitHub nach einer neuen Version
(abschaltbar unter Einstellungen › Allgemein › Updates). Ist eine verfügbar, kommt eine
Benachrichtigung und im Rechtsklick-Menü erscheint „Update auf vX.Y.Z installieren…“.
**Installiert wird nur nach Bestätigung:** Der Dialog zeigt die Änderungsnotizen, lädt die neue
EXE herunter, prüft die SHA256-Prüfsumme, tauscht die Datei aus und startet neu. Eine laufende
Zeiterfassung läuft dabei weiter, die Daten in `%APPDATA%\ZeitTracker` bleiben unverändert.

Die Versionsnummer schreibt `build_exe.bat` beim GitHub-Build aus dem Tag in `version.py`.
Lokal gebaute Versionen heißen `dev` und bieten keine automatischen Updates an.

## Das Symbol

| Teil der Uhr | Grün | Rot |
|---|---|---|
| **Kreis** | Mit Überstunden-Saldo hast du heute schon genug gearbeitet | Saldo + heutige Zeit reichen noch nicht |
| **Zeiger** | Allein die heutige Arbeitszeit deckt das Tagessoll | Tagessoll heute noch nicht erreicht |

Zifferblatt: **weiß** = Arbeit läuft, **gelb** (mit Pausenzeichen) = Pause, **grau** = nicht eingestempelt.
Die Zeiger zeigen die aktuelle Uhrzeit.

- **Maus darüber**: heutige Arbeitszeit, noch benötigte Zeit (auch mit Saldo), Status und Saldo.
- **Linksklick**: Übersicht.
- **Rechtsklick**: Arbeit starten / Pause starten / Pause beenden / Feierabend, Übersicht,
  Historie, Urlaub und Krankheit, Einstellungen, Personio-Sync (falls aktiv), Beenden.

## Funktionen

- **Arbeitstage & Soll-Stunden** je Wochentag (Einstellungen › Arbeitszeit). Änderungen können
  „ab Datum“ gelten, damit vergangene Tage ihr altes Soll behalten.
- **Urlaub, Krankheit, Feiertag, Sonstiges, Freizeitausgleich** per Kalender: 1. Klick = Start,
  2. Klick = Ende. Halbe Tage sind nur am ersten und/oder letzten Tag eines Zeitraums möglich
  („Erster Tag halbtags“ = ab mittags frei, „Letzter Tag halbtags“ = bis mittags frei; bei einem
  einzelnen Tag „Halbtags“ mit Auswahl „vormittags frei“ / „nachmittags frei“). Freizeitausgleich senkt das Soll nicht (verbraucht also Überstunden).
- **Gesetzliche Feiertage** werden automatisch je Bundesland erkannt (Abfrage beim ersten Start,
  änderbar unter Einstellungen › Arbeitszeit › Bundesland). An Feiertagen entfällt das Soll, Arbeit
  an Feiertagen zählt komplett als Überstunden. Heiligabend/Silvester sind keine gesetzlichen
  Feiertage – bei Bedarf als halben Urlaubs- oder Feiertag eintragen.
- **Arbeit über Mitternacht / Tageswechsel** (Einstellungen › Arbeitszeit, Standard 5 Uhr):
  Ein Arbeitstag endet nicht um Mitternacht, sondern zum eingestellten Tageswechsel.
  - Arbeit bis zum Tageswechsel zählt zum Vortag – auch nach einer Pause (z. B. 23:30–00:30 Pause,
    weiter bis 02:00 → alles Montag, 1 h Pause).
  - Läuft eine Pause, ein gesperrter Bildschirm oder ein heruntergefahrener PC über den Tageswechsel,
    endet der Arbeitstag zu Beginn dieser Pause; die Pause zählt nicht.
  - Was danach beginnt, ist ein neuer Arbeitstag (z. B. 02:00 Schluss, 09:00 Start → Dienstag ohne Pause davor).
  In der Historie erscheinen solche Zeiten als z. B. „02:00 (+1)“, beim Bearbeiten als „00:30+1“.
- **Übersicht**: Heute, Woche, Monat, Jahr, Gesamt – gearbeitet, Soll, Differenz, Saldo,
  voraussichtlicher Feierabend, Urlaubskonto und Krankheitstage.
- **Saldo-Verlauf** (Rechtsklick-Menü oder Historie): Zeitraum frei wählbar (Schnellwahl Monat, Jahr,
  letzte 12 Monate, gesamt), Diagramm des Überstunden-Saldos mit Mauszeiger-Anzeige je Tag, Saldo zu
  Beginn/Ende, Höchst- und Tiefststand, Tabelle nach Tag/Woche/Monat mit laufendem Saldo, CSV-Export.
- **Historie**: jeder Tag mit Beginn, Ende, Pause, Soll, Differenz. Doppelklick = Zeiten des Tages bearbeiten.
  CSV-Export pro Monat.
- **Start & Ende** (Einstellungen › Allgemein):
  - ZeitTracker nach der Windows-Anmeldung starten und Zeiterfassung sofort beginnen
  - beim Herunterfahren / Abmelden (einstellbar):
    **als Pause werten** (Standard) – wird am selben Tag wieder hochgefahren, gilt die Zeit dazwischen
    als Pause und die Erfassung läuft automatisch weiter; erst am nächsten Tag ist der Arbeitstag beendet;
    alternativ **Feierabend buchen** oder **nichts tun**.
    Wird danach am selben Tag nicht mehr weitergearbeitet, ist die Uhrzeit des Herunterfahrens das Ende
    des Arbeitstags – die angefangene Pause zählt nicht. Klickst du nach einem automatischen Fortsetzen
    innerhalb von 30 Minuten auf „Feierabend“, fragt der ZeitTracker, ob der Tag stattdessen zur Uhrzeit
    des Herunterfahrens enden soll.
  - **Neustart innerhalb von X Minuten** (Standard 10, einstellbar): Die Arbeitszeit läuft einfach
    durch – die Zeit zwischen Herunterfahren und Neustart zählt als Arbeitszeit, keine Pause, kein Feierabend
  - beim Sperren des Bildschirms (Win+L) pausieren, beim Entsperren fortsetzen
    (eine manuell gestartete Pause wird beim Entsperren nicht beendet)
  - Standby oder harter Neustart werden genauso behandelt (gleicher Tag = Pause, Folgetag = Feierabend)
- **Startsaldo** für bereits vorhandene Überstunden.
- **Benachrichtigungen** (Einstellungen › Allgemein, einzeln abschaltbar): Tagessoll erreicht
  (auch mit Saldo), neue/geänderte/entfernte Abwesenheiten aus Personio nach einem automatischen
  Abruf (nur wenn sich etwas geändert hat; Fehler werden einmal gemeldet), neuerer Stand von OneDrive geladen.

## Personio (standardmäßig deaktiviert)

Einstellungen › Personio:

1. HR-Admin erstellt unter *Einstellungen › Integrationen › API-Zugangsdaten* Zugangsdaten mit
   Leserechten für **Personen** und **Abwesenheiten** sowie – nur wenn exportiert werden soll –
   Lese-/Schreibrechten für **Anwesenheiten**.
2. Client-ID, Client-Secret und deine Personio-E-Mail eintragen, „Verbindung testen“.
3. Auswählen, was synchronisiert wird:
   - **Abwesenheiten importieren** (Urlaub/Krankheit inkl. halber Tage, aus Personios Tagesaufschlüsselung;
     bei halben Tagen wird übernommen, ob Vormittag oder Nachmittag frei ist).
     Manuelle Einträge haben Vorrang.
   - **Arbeitszeiten exportieren** (eigener Schalter, standardmäßig aus) – überträgt abgeschlossene
     Arbeitsblöcke ab einem wählbaren Datum, Änderungen und Löschungen werden nachgezogen.

Genutzt wird die Personio-API v2 (`/v2/auth/token`, `/v2/persons`, `/v2/absence-periods`,
`/v2/attendance-periods`). Das Secret liegt in der Windows-Anmeldeinformationsverwaltung.

## Personio ohne API: Kalender-Abo

Ist der API-Zugang in eurer Organisation nicht aktiv, können Abwesenheiten über das Kalender-Abo
übernommen werden (Einstellungen › Personio (Kalender-Abo)):

1. In Personio: *Abwesenheit & Anwesenheit › Team-Kalender › ⋮ › Abonnieren* und die Kalender
   **„Abwesenheit“** sowie die konkreten Arten (z. B. **Urlaub**, **Bildungsurlaub**) wählen,
   Link erzeugen und kopieren. („Mein Kalender“ allein enthält keine Abwesenheiten.)
2. Link im ZeitTracker eintragen und **deinen Namen genau so wie in Personio** als Filter
   (Pflicht, weil der Kalender alle Mitarbeiter enthält), dann „Speichern & jetzt laden“.
3. Der Kalender wird danach automatisch alle 180 Minuten (einstellbar) abgerufen.

So werden die Einträge gelesen (Format z. B. `[Urlaub] Max Mustermann (½ letzter Tag)`):
- Nur eigene Einträge: Der Name vor einer „Vertretung: …“ muss exakt passen. Einträge, bei
  denen du nur als Vertretung genannt bist, werden ignoriert.
- „(½ erster Tag)“ = erster Tag ab mittags frei, „(½ letzter Tag)“ = letzter Tag bis mittags frei.
- Ein Tag mit konkreter Art (`[Urlaub]`, `[Bildungsurlaub]` …) hat Vorrang vor dem doppelten
  `[Abwesenheit]`-Eintrag. Tage, die nur als `[Abwesenheit]` vorkommen (Art unbekannt, z. B.
  Krankheit), werden standardmäßig als „Sonstige Abwesenheit“ übernommen (einstellbar).
- Urlaub → Urlaub, Überstundenabbau → Freizeitausgleich, Bildungsurlaub/Sonderurlaub/Elternzeit/
  Fortbildung usw. → Sonstige Abwesenheit (zählt nicht aufs Urlaubskonto).
- Wochenenden, Feiertage, Geburtstage und Jubiläen werden übersprungen.
- Manuelle Einträge haben Vorrang. Arbeitszeiten können so nicht an Personio übertragen werden.
- Abgeglichen wird nur das Zeitfenster, das der Kalender tatsächlich abdeckt (frühester bis spätester
  Eintrag). Darin werden in Personio gelöschte/gekürzte Abwesenheiten auch hier entfernt; ältere
  Einträge, die Personio nicht mehr mitschickt, bleiben erhalten. Ein leerer Kalender ändert nichts.
- Der Link wird in der Windows-Anmeldeinformationsverwaltung gespeichert (wer ihn kennt, sieht die Einträge).

## OneDrive-Sicherung (privates Microsoft-Konto)

Der ZeitTracker kann `data.json` automatisch in deinem OneDrive sichern und zwischen mehreren PCs
abgleichen – ohne OneDrive-Client.

Einstellungen › **OneDrive**: Haken setzen, „Bei Microsoft anmelden…“ – der Browser öffnet sich,
einmal mit deinem privaten Microsoft-Konto anmelden und zustimmen. Fertig.

Eine App-Registrierung ist bereits eingebaut (Client-ID `3648689d-6632-4211-85d4-8ce97035f174`,
öffentlich und kein Geheimnis). Wer eine eigene verwenden möchte, trägt deren Client-ID in
`%APPDATA%\ZeitTracker\data.json` unter `settings › onedrive › client_id` ein (ZeitTracker vorher beenden).
Eine eigene Registrierung legt man so an:

1. Kostenloses Azure-Konto (https://azure.microsoft.com/free) für ein eigenes Verzeichnis – seit
   Juni 2024 nötig, um mit einem privaten Konto Apps registrieren zu können.
2. https://entra.microsoft.com › App-Registrierungen › Neue Registrierung: Kontotyp
   **Nur persönliche Microsoft-Konten**, Umleitungs-URI **Öffentlicher Client/nativ** `http://localhost`.
3. API-Berechtigungen › Microsoft Graph › Delegiert: `Files.ReadWrite.AppFolder`, `offline_access`.
   („Öffentliche Clientflows zulassen“ wird nicht benötigt.)

### So funktioniert der Abgleich
- Die Datei liegt in OneDrive unter **Apps › ZeitTracker › data.json**. Der ZeitTracker hat nur auf
  diesen App-Ordner Zugriff, nicht auf deine anderen Dateien.
- Abgleich beim Start, alle 15 Minuten (einstellbar), bei Feierabend und beim Beenden. Hochgeladen wird
  nur, wenn sich wirklich etwas geändert hat.
- Neuer PC ohne Daten: der Stand von OneDrive wird automatisch übernommen.
- Wurde auf zwei Geräten gleichzeitig geändert, wird **nichts überschrieben**: Der ZeitTracker fragt,
  welcher Stand gelten soll, und bewahrt den anderen als Sicherung im Ordner `backups` auf.
- Ohne Internet wird einfach lokal weitergearbeitet und später abgeglichen.
- Die Anmeldung wird mit Windows-Verschlüsselung (DPAPI) in `onedrive_token.bin` gespeichert und
  erneuert sich selbst; abmelden über „Abmelden“.

## Daten

`%APPDATA%\ZeitTracker\data.json` (lesbares JSON), tägliche Sicherungen im Unterordner `backups`
(14 Tage), Protokoll in `zeittracker.log`.
