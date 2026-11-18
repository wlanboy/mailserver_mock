# IMAP-Implementierung

Dieses Dokument beschreibt, welche Teile von IMAP4rev1 (RFC 3501) der Mock
in [`src/mailserver_mock/imap_server.py`](src/mailserver_mock/imap_server.py)
implementiert, wie sich die Befehle im Detail verhalten und welche Teile des
RFCs bewusst ausgelassen wurden. Ziel des Mocks ist **nicht** Vollständigkeit,
sondern ein Server, der reale IMAP-Clients (z. B. `imaplib`, Jakarta Mail,
Thunderbird) für Testzwecke bedienen kann, ohne eine echte Mailbox-Semantik
(mehrere Ordner, `\Recent`, Push bei IDLE, ...) nachzubilden.

## Session-Ablauf

Nach Verbindungsaufbau sendet der Server sofort eine Greeting-Zeile:

```
* OK IMAP4rev1 Service Ready
```

Danach liest der Server zeilenweise Client-Kommandos im Format
`<tag> <command> [args]` (RFC 3501 §2.2, "Client Protocol Sender"). Die Session
merkt sich zwei Dinge: `auth` (eingeloggt) und `selected` (keine Mailbox,
`READ-WRITE` nach `SELECT`, `READ-ONLY` nach `EXAMINE`). `CHECK`, `CLOSE` und
`EXPUNGE` verlangen eine ausgewählte Mailbox. `FETCH`, `STORE` und `SEARCH`
prüfen aus Kompatibilitätsgründen weiterhin nur `auth`.

### Timeout und Zeilenlimits

- **Timeout.** Jede Verbindung bekommt ein Socket-Timeout von `IMAP_TIMEOUT`
  Sekunden (Default 1800). Das entspricht dem Autologout-Minimum aus RFC 3501
  §5.4 und passt zu IDLE, das Clients laut RFC 2177 spätestens alle 29 Minuten
  erneuern. Danach sendet der Server `* BYE Autologout; idle for too long` und
  schließt die Verbindung. `IMAP_TIMEOUT=0` schaltet das Timeout ab.
- **Zeilenlänge.** Eine Zeile darf höchstens 8 KiB lang sein, sonst antwortet
  der Server mit `<tag> BAD Command line too long` und verwirft den Rest der
  Zeile.
- **Literale.** Ein Kommando darf samt aller Literale höchstens 64 KiB groß
  sein, sonst `<tag> BAD Literal too large`.

## Implementierte Befehle

### CAPABILITY (RFC 3501 §6.1.1)

Antwortet immer statisch mit:

```
* CAPABILITY IMAP4rev1 AUTH=LOGIN
```

Es wird keine Server-Capability dynamisch aus dem tatsächlichen
Funktionsumfang abgeleitet. `AUTH=LOGIN` in der Capability-Liste würde laut
RFC 3501 §6.2.2 eigentlich bedeuten, dass der SASL-Mechanismus `LOGIN` über
den Befehl `AUTHENTICATE` nutzbar ist — dieser Befehl ist im Mock **nicht**
implementiert (siehe unten). Der eigentliche Login läuft ausschließlich über
den separaten IMAP-Befehl `LOGIN`.

### LOGIN (RFC 3501 §6.2.3)

```
a LOGIN "user" "pass"
```

- Anführungszeichen werden entfernt, danach wird an Leerzeichen gesplittet.
  Passwörter mit Leerzeichen oder die IMAP-Literal-Syntax (`{n}\r\n<bytes>`)
  werden **nicht** unterstützt.
- Zugangsdaten werden gegen `users.json` geprüft (siehe
  [`users.py`](src/mailserver_mock/users.py)). Je nach `behavior`-Feld des
  Users:
  - `normal`: `<tag> OK LOGIN completed`, Session ist danach authentifiziert.
  - `quota`: Login gelingt, aber es wird intern ein Fehlerzustand
    (`quota_error`) gemerkt, der erst bei `SELECT` als `NO`-Antwort mit
    IMAP-Response-Code ausgeliefert wird (siehe RFC 5530, z. B. `OVERQUOTA`).
  - `too_many` / `timeout`: Login schlägt **immer** fehl. Nach
    `delay_seconds` Wartezeit antwortet der Server mit
    `<tag> NO [<response_code>] <message>` (Default `UNAVAILABLE`). Die
    Verbindung bleibt aber offen (anders als bei SMTP, siehe `smtp.md`).
  - Unbekannter User oder falsches Passwort: `<tag> NO LOGIN failed`.

### LIST (RFC 3501 §6.3.8)

Ignoriert die übergebenen Argumente (Referenz-Name, Mailbox-Pattern)
vollständig und liefert immer genau einen statischen Eintrag:

```
* LIST (\HasNoChildren) "/" "INBOX"
a OK LIST completed
```

Es gibt nur eine Mailbox (`INBOX`); Hierarchien, Wildcards (`%`, `*`) oder
Sonderfälle wie leere Referenz/leeres Pattern (RFC 3501 §6.3.8, Sonderfall
"nur Hierarchie-Trenner zurückgeben") werden nicht ausgewertet.

### SELECT (RFC 3501 §6.3.1)

Der Server ignoriert den übergebenen Mailbox-Namen und öffnet immer die
einzige Mailbox. Antwort:

```
* FLAGS (\Answered \Flagged \Deleted \Seen \Draft)
* OK [PERMANENTFLAGS (\Answered \Flagged \Deleted \Seen \Draft \*)] Flags permitted
* <n> EXISTS
* 0 RECENT
* OK [UNSEEN <seq>] First unseen
* OK [UIDVALIDITY <v>] UIDs valid
* OK [UIDNEXT <uid>] Predicted next UID
a OK [READ-WRITE] SELECT completed
```

- `<n>` ist die Anzahl der `.eml`-Dateien im `MAIL_DIR`.
- `UNSEEN` nennt die Sequenznummer der ersten Mail ohne `\Seen` und fehlt,
  wenn alle gelesen sind.
- `UIDVALIDITY` steht in `MAIL_DIR/.uidvalidity` und wird beim ersten Zugriff
  aus der aktuellen Uhrzeit angelegt. `DELETE /mails` (HTTP) löscht die Datei,
  danach beginnen die UIDs wieder bei 1 und es gibt eine neue, größere
  `UIDVALIDITY`.
- `UIDNEXT` ist `storage.next_id()`: die höchste vorhandene UID plus 1, aber
  mindestens der Stand in `MAIL_DIR/.uidnext`. Wird die neueste Mail gelöscht,
  wird ihre UID nicht neu vergeben (RFC 3501 §2.3.1.1).
- `RECENT` ist immer 0, der Mock bildet `\Recent` nicht nach.

Ist der eingeloggte User im `quota`-Fehlerszenario, antwortet `SELECT`
stattdessen mit `NO` und dem konfigurierten Response-Code/Message aus
`users.json` (z. B. `NO [OVERQUOTA] Quota exceeded`) — passend zu RFC 5530
("IMAP4 Response Codes"), das `OVERQUOTA` als Standard-Response-Code
definiert.

### EXAMINE (RFC 3501 §6.3.2)

Liefert dieselben Antworten wie `SELECT`, aber mit `PERMANENTFLAGS ()` und
`OK [READ-ONLY] EXAMINE completed`. Im Read-only-Zustand setzt `FETCH` kein
implizites `\Seen`, `STORE` und `EXPUNGE` antworten mit
`NO Mailbox is read-only`.

### STATUS (RFC 3501 §6.3.10)

```
a STATUS <mailbox> (MESSAGES RECENT UIDNEXT UIDVALIDITY UNSEEN)
* STATUS "<mailbox>" (MESSAGES <n> RECENT 0 UIDNEXT <n+1> UIDVALIDITY 1 UNSEEN <u>)
a OK STATUS completed
```

Erlaubt Abfragen der Mailbox-Metadaten **ohne** vorheriges `SELECT` — der
Client bekommt nur `auth` geprüft, keinen Mailbox-Namen validiert. Wie bei
`SELECT`/`LIST` gibt es nur eine (implizite) Mailbox, der übergebene
Mailbox-Name wird lediglich in der Antwort gespiegelt, nicht ausgewertet.
Unterstützte Items:

- `MESSAGES` — `storage.count_mails()`, identisch zu `SELECT`s `EXISTS`.
- `RECENT` — immer `0`, da der Mock kein `\Recent`-Flag nachbildet.
- `UIDNEXT` — `storage.next_id()`.
- `UIDVALIDITY` — derselbe Wert wie bei `SELECT`.
- `UNSEEN` — Anzahl der Nachrichten, deren gespeicherte Flags **kein**
  `\Seen` enthalten (linear über `storage.load_flags` pro Nachricht
  ermittelt, kein Caching).

Unbekannte Items (z. B. aus zukünftigen RFC-Extensions) werden stillschweigend
ignoriert statt mit `BAD` abgelehnt.

### FETCH (RFC 3501 §6.4.5)

```
a FETCH <sequence-set> <items>
```

Der `<sequence-set>` wird über `_parse_seq_set` interpretiert (Einzel-IDs,
`*`, Bereiche `a:b`, Kommalisten wie `1,3,5:7`). `<items>` ist ein einzelnes
Item oder eine Liste in Klammern. Geliefert werden genau die angefragten Items
in der angefragten Reihenfolge ([`imap_fetch.py`](src/mailserver_mock/imap_fetch.py)):

| Item | Antwort |
|---|---|
| `FLAGS` | gespeicherte Flags |
| `UID` | UID der Mail (bei `UID FETCH` immer enthalten, auch ungefragt) |
| `ENVELOPE` | siehe unten |
| `INTERNALDATE` | mtime der `.eml`-Datei in UTC, z. B. `"18-Nov-2026 10:53:18 +0000"` |
| `RFC822.SIZE` | Größe der Rohnachricht in Bytes |
| `RFC822`, `RFC822.HEADER`, `RFC822.TEXT` | ganze Nachricht, Header-Block, Body |
| `BODY[]`, `BODY[HEADER]`, `BODY[TEXT]` | ganze Nachricht, Header-Block inkl. Leerzeile, Body |
| `BODY[HEADER.FIELDS (A B)]`, `BODY[HEADER.FIELDS.NOT (A B)]` | nur die genannten bzw. alle anderen Header-Felder, gefaltete Zeilen bleiben erhalten |
| `BODY[1]`, `BODY[1.2]`, `BODY[2.MIME]` | Inhalt bzw. MIME-Header eines MIME-Parts. Eine Nachricht ohne Multipart hat nur Part `1` (ihren Body) |
| `BODY[...]<start.length>` | Ausschnitt in Bytes, Antwort als `BODY[...]<start>` |
| `BODY`, `BODYSTRUCTURE` | MIME-Struktur ohne Extension-Daten (die sind laut Grammatik optional) |
| `ALL`, `FAST`, `FULL` | Makros nach RFC 3501 |

`BODY.PEEK[...]` liefert dasselbe wie `BODY[...]`, die Antwort heißt `BODY[...]`.
Unbekannte Items, ein fehlender Item-Teil oder `HEADER.FIELDS` ohne Feldliste
werden mit `BAD` abgelehnt.

`\Seen` wird implizit gesetzt bei jedem `BODY[...]` ohne `.PEEK` sowie bei
`RFC822` und `RFC822.TEXT`; `BODY.PEEK[...]`, `RFC822.HEADER` und
`RFC822.SIZE` lassen es unangetastet. Nach `EXAMINE` wird nie `\Seen` gesetzt.
Ändert sich `\Seen` und hat der Client `FLAGS` nicht angefragt, hängt der
Server `FLAGS (...)` an die Antwort an, damit der Client den neuen Stand kennt.

Die Mail wird nur von der Platte gelesen, wenn ein Item ihren Inhalt braucht.
`FETCH 1:* (UID FLAGS)` öffnet also keine `.eml`-Datei.

Die `ENVELOPE`-Struktur wird in
[`storage.get_envelope`](src/mailserver_mock/storage.py) gebaut und folgt dem
in RFC 3501 §7.4.2 definierten Format
`(date subject from sender reply-to to cc bcc in-reply-to message-id)`.
`sender` ist dabei gleich `from`, `cc`/`bcc`/`in-reply-to`/`message-id` sind
immer `NIL`.

### UID (RFC 3501 §6.4.8)

```
a UID FETCH <ids> <items>
a UID SEARCH <criteria>
```

`UID FETCH` wird an `_handle_fetch` durchgereicht, `UID SEARCH` an
`_handle_search`, `UID STORE` an `_handle_store` (jeweils mit `is_uid=True`).
`_resolve_messages` bildet Sequenznummern und UIDs aufeinander ab: Die
Sequenznummern 1..N gehören zu den aufsteigend sortierten UIDs der
vorhandenen `.eml`-Dateien. Lücken durch gelöschte Mails bleiben Lücken in
den UIDs.

`UID STORE` unterscheidet sich von der Sequenznummer-Variante nur darin,
dass die unsolicited `* <n> FETCH (...)`-Response (falls nicht `.SILENT`)
zusätzlich `UID <n>` enthält, wie es RFC 3501 §6.4.8 für alle über `UID`
ausgelösten `FETCH`-Antworten verlangt.

### STORE (RFC 3501 §6.4.6)

```
a STORE <msg-num> <FLAGS|+FLAGS|-FLAGS>[.SILENT] (<flag list>)
```

Unterstützt alle vier Standard-Varianten (`FLAGS`, `+FLAGS`, `-FLAGS`, jeweils
mit optionalem `.SILENT`-Suffix). Bei `.SILENT` wird die sonst übliche
`* <n> FETCH (FLAGS (...))`-Untagged-Response unterdrückt, wie in RFC 3501
§6.4.6 beschrieben. `<msg-num>` ist ein Sequence-Set wie bei `FETCH`
(`1`, `2:4`, `1,3:*`), ungültige Sets werden mit `BAD` abgelehnt. Nach
`EXAMINE` antwortet `STORE` mit `NO Mailbox is read-only`.

### SEARCH (RFC 3501 §6.4.4)

```
a SEARCH <criteria>
```

Die Suchkriterien werden **komplett ignoriert**. Der Server liefert immer
alle vorhandenen Nachrichten-IDs von `1` bis `count_mails()`:

```
* SEARCH 1 2 3 ...
a OK SEARCH completed
```

Das heißt: `SEARCH UNSEEN`, `SEARCH FROM "x@y"`, `SEARCH SINCE <date>` usw.
verhalten sich alle identisch zu `SEARCH ALL`. Für Tests, die nur prüfen
wollen "sind Mails da / wie viele", reicht das; für Tests, die eine echte
Filterlogik erwarten, nicht.

### IDLE (RFC 2177)

```
a IDLE
+ idling
DONE
a OK IDLE terminated
```

Der Server sendet die Continuation-Response `+ idling`, blockiert dann auf
einer einzelnen `readline()` (die vom Client gesendete `DONE`-Zeile) und
schließt danach mit `OK` ab. Es werden **keine** echten asynchronen
Push-Benachrichtigungen (z. B. `* <n> EXISTS` bei neu eintreffender Mail
während des Idle) gesendet — IDLE ist hier reines Protokoll-Stubbing, kein
funktionierendes Event-Push.

### NOOP und CHECK (RFC 3501 §6.1.2, §6.4.1)

```
a NOOP
* 3 EXPUNGE
* 5 EXISTS
a OK NOOP completed
```

`NOOP` ist in jedem Zustand erlaubt, `CHECK` nur mit ausgewählter Mailbox.
Ist eine Mailbox ausgewählt, vergleichen beide den aktuellen Inhalt von
`MAIL_DIR` mit dem Stand, den der Client zuletzt gemeldet bekam: verschwundene
Mails (z. B. per HTTP-API oder aus einer anderen Session gelöscht) kommen als
`* <n> EXPUNGE` (absteigend), neue Mails als `* <n> EXISTS`. Clients, die
`NOOP` als Keepalive oder zum Pollen schicken, sehen so neue Mails.

### EXPUNGE (RFC 3501 §6.4.3)

```
a EXPUNGE
* 1 EXPUNGE
* 2 EXPUNGE
a OK EXPUNGE completed
```

Löscht alle Mails mit `\Deleted` samt `.flags`-Datei. Jede
`* <n> EXPUNGE`-Antwort verschiebt die folgenden Sequenznummern sofort um
eins (RFC 3501 §7.4.1). Werden die Mails 1, 3 und 4 von vier gelöscht, lautet
die Folge daher `1, 2, 2`. Vorher meldet der Server wie bei `NOOP` externe
Änderungen, damit die Nummern zum Stand des Clients passen. Ohne ausgewählte
Mailbox kommt `BAD No mailbox selected`, nach `EXAMINE` `NO`.

### CLOSE (RFC 3501 §6.4.2)

Löscht wie `EXPUNGE` alle Mails mit `\Deleted`, aber ohne
`* <n> EXPUNGE`-Antworten, und hebt die Auswahl der Mailbox auf. Nach
`EXAMINE` wird nichts gelöscht.

### LOGOUT (RFC 3501 §6.1.3)

```
* BYE IMAP server logging out
a OK LOGOUT completed
```

Entspricht dem RFC, danach wird die Verbindung geschlossen.

## Nicht implementierte Befehle

Folgende IMAP4rev1-Standardbefehle existieren im Mock nicht und werden über
den generischen `else`-Zweig mit `<tag> BAD Unknown command` beantwortet:

| Befehl | RFC-Referenz | Warum nicht implementiert |
|---|---|---|
| `STARTTLS` | RFC 3501 §6.2.1 | Mock ist nur für lokale/Test-Verbindungen gedacht, keine TLS-Terminierung nötig. |
| `AUTHENTICATE` | RFC 3501 §6.2.2 | Login läuft ausschließlich über den einfacheren `LOGIN`-Befehl mit Klartext-Zugangsdaten; SASL-Mechanismen (`LOGIN`, `PLAIN`, `CRAM-MD5`, ...) sind nicht nötig, da es sich nicht um einen sicherheitskritischen Server handelt. Die `AUTH=LOGIN`-Angabe in `CAPABILITY` ist daher etwas irreführend, wird aber von den meisten getesteten Clients (`imaplib.login()`) ignoriert. |
| `CREATE` / `DELETE` / `RENAME` | RFC 3501 §6.3.3–6.3.5 | Es gibt nur eine feste Mailbox (`INBOX`); Mailbox-Verwaltung ist out of scope. |
| `SUBSCRIBE` / `UNSUBSCRIBE` / `LSUB` | RFC 3501 §6.3.6/6.3.7/6.3.9 | Keine Mehrfach-Mailbox-Verwaltung vorhanden. |
| `APPEND` | RFC 3501 §6.3.11 | Mails werden ausschließlich per SMTP eingeliefert, nicht direkt per IMAP hochgeladen. |
| `COPY` | RFC 3501 §6.4.7 | Keine Mehrfach-Mailbox-Unterstützung, daher kein Kopierziel. |
| `NAMESPACE` | RFC 2342 | Nur ein einziger, impliziter Namespace vorhanden. |
| `ID` | RFC 2971 | Reine Metadaten-Extension ohne Testrelevanz. |

## Weitere Vereinfachungen

- **Kommando-Parsing.** Zeilen werden mit `line.split(" ", 2)` in maximal drei
  Teile zerlegt (Tag, Kommando, Rest). Synchronisierende Literale
  (`{octet-count}\r\n`) werden vorher eingelesen und als Quoted-String in die
  Zeile eingesetzt; nicht-synchronisierende Literale (`{n+}`, RFC 7888) gibt es
  nicht.
- **Lockerer Selected-State.** `FETCH`/`STORE`/`SEARCH` funktionieren auch
  ohne vorheriges `SELECT`, geprüft wird nur der Login.
- **Sequenznummern sind nicht eingefroren.** Ein echter Server hält die
  Nummerierung einer Session stabil, bis er Änderungen per `EXISTS`/`EXPUNGE`
  gemeldet hat. Der Mock rechnet bei jedem Befehl mit dem aktuellen Inhalt von
  `MAIL_DIR`. Löscht jemand per HTTP-API eine Mail, verschieben sich die Nummern
  für den Client schon vor dem nächsten `NOOP`. UIDs sind davon nicht betroffen.
- **Logging.** Bei jedem `FETCH`, das Header oder Body ausliefert, wird
  `From`/`To`/`Subject` der Nachricht geloggt (siehe `logger.info("outgoing ...")`), nützlich um in
  Tests nachzuvollziehen, was ein Client tatsächlich abgeholt hat.
