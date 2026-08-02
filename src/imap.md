# IMAP-Implementierung

Dieses Dokument beschreibt, welche Teile von IMAP4rev1 (RFC 3501) der Mock
in [`src/mailserver_mock/imap_server.py`](src/mailserver_mock/imap_server.py)
implementiert, wie sich die Befehle im Detail verhalten und welche Teile des
RFCs bewusst ausgelassen wurden. Ziel des Mocks ist **nicht** Vollständigkeit,
sondern ein Server, der reale IMAP-Clients (z. B. `imaplib`, Jakarta Mail,
Thunderbird) für Testzwecke bedienen kann, ohne eine echte Mailbox-Semantik
(Persistenz über Sessions, mehrere Ordner, Expunge, ...) nachzubilden.

## Session-Ablauf

Nach Verbindungsaufbau sendet der Server sofort eine Greeting-Zeile:

```
* OK IMAP4rev1 Service Ready
```

Danach liest der Server zeilenweise Client-Kommandos im Format
`<tag> <command> [args]` (RFC 3501 §2.2, "Client Protocol Sender"). Es wird
**nicht** zwischen Connection-State (Not Authenticated / Authenticated /
Selected) unterschieden — stattdessen gibt es nur ein einziges Flag `auth`.
Befehle, die laut RFC einen ausgewählten Mailbox-State voraussetzen (z. B.
`FETCH`), prüfen daher nur `auth`, nicht ob zuvor tatsächlich `SELECT`
aufgerufen wurde.

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

Der übergebene Mailbox-Name wird ignoriert — es wird immer dieselbe (einzige)
Mailbox "geöffnet". Antwort:

```
* <n> EXISTS
* FLAGS (\Seen \Deleted \Answered)
* OK [PERMANENTFLAGS (\Seen \Deleted \Answered)]
a OK [READ-WRITE] SELECT completed
```

`<n>` ist `storage.count_mails()`, also die Anzahl der `.eml`-Dateien im
`MAIL_DIR`. Nicht implementiert: `* <n> RECENT`, `UIDVALIDITY`,
`UIDNEXT`, `UNSEEN`-Response-Code — Clients, die diese für korrektes
UID-Handling benötigen, bekommen sie nicht.

Ist der eingeloggte User im `quota`-Fehlerszenario, antwortet `SELECT`
stattdessen mit `NO` und dem konfigurierten Response-Code/Message aus
`users.json` (z. B. `NO [OVERQUOTA] Quota exceeded`) — passend zu RFC 5530
("IMAP4 Response Codes"), das `OVERQUOTA` als Standard-Response-Code
definiert.

`EXAMINE` (read-only-Variante von `SELECT`, RFC 3501 §6.3.2) ist **nicht**
implementiert.

### FETCH (RFC 3501 §6.4.5)

```
a FETCH <sequence-set> <items>
```

Der `<sequence-set>` wird über `_parse_seq_set` interpretiert (unterstützt
Einzel-IDs, `*` für die höchste Nachrichtennummer, Bereiche `a:b` und
Kommalisten, z. B. `1,3,5:7`). **Der `<items>`-Teil wird komplett
ignoriert** — unabhängig davon, ob der Client `(FLAGS)`, `(BODY[HEADER])`
oder `(RFC822.SIZE)` anfragt, liefert der Server für jede passende Nachricht
immer denselben festen Datensatz:

```
* <id> FETCH (UID <id> FLAGS (...) ENVELOPE (...) BODY[] {<n>}
<n Bytes Rohnachricht>
)
```

Damit sind insbesondere folgende FETCH-Items **nicht** unterstützt:
`BODY[HEADER]`, `BODY[TEXT]`, `BODY[]<start.length>` (partial fetch),
`BODYSTRUCTURE`, `RFC822.SIZE`, `INTERNALDATE`. Clients, die gezielt nur
Header oder nur einen Teilbereich abrufen wollen, bekommen trotzdem immer
die komplette Nachricht.

Die `ENVELOPE`-Struktur wird in
[`storage.get_envelope`](src/mailserver_mock/storage.py) gebaut und folgt dem
in RFC 3501 §7.4.2 definierten Format
`(date subject from sender reply-to to cc bcc in-reply-to message-id)` —
`sender` wird dabei mit `from` gleichgesetzt, `cc`/`bcc`/`in-reply-to`/
`message-id` sind immer `NIL`.

### UID (RFC 3501 §6.4.8)

```
a UID FETCH <ids> <items>
a UID SEARCH <criteria>
```

`UID FETCH` wird 1:1 an `_handle_fetch` durchgereicht, `UID SEARCH` an
`_handle_search`. Das funktioniert nur deshalb korrekt, weil im Mock die
Message-ID **gleichzeitig als Sequenznummer und als UID** verwendet wird
(Nachrichten werden nie umnummeriert oder gelöscht/expunged, siehe unten) —
echte IMAP-Server müssen UID und Sequenznummer sauber trennen, da UIDs über
Sessions stabil bleiben müssen, Sequenznummern aber nicht.

**`UID STORE` ist nicht implementiert.** Der Dispatcher kennt für `UID` nur
die Subcommands `FETCH` und `SEARCH`; `UID STORE ...` fällt in den
`else`-Zweig und liefert `<tag> BAD UID command not supported`. Clients, die
Flags ausschließlich über `UID STORE` setzen (viele tun das, um
Sequenznummer-Drift zu vermeiden), funktionieren mit diesem Mock nicht.

### STORE (RFC 3501 §6.4.6)

```
a STORE <msg-num> <FLAGS|+FLAGS|-FLAGS>[.SILENT] (<flag list>)
```

Unterstützt alle vier Standard-Varianten (`FLAGS`, `+FLAGS`, `-FLAGS`, jeweils
mit optionalem `.SILENT`-Suffix). Bei `.SILENT` wird die sonst übliche
`* <n> FETCH (FLAGS (...))`-Untagged-Response unterdrückt, wie in RFC 3501
§6.4.6 beschrieben. `<msg-num>` wird als reiner String an
`storage.load_flags`/`save_flags` durchgereicht — es gibt keine Validierung,
dass die Nachricht existiert, und keine Sequence-Set-Syntax (Bereiche/Kommas)
wie bei `FETCH`, sondern nur eine einzelne ID.

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
| `EXAMINE` | RFC 3501 §6.3.2 | Kein Unterschied zwischen Read-Write und Read-Only-Zustand nötig für Testzwecke. |
| `CREATE` / `DELETE` / `RENAME` | RFC 3501 §6.3.3–6.3.5 | Es gibt nur eine feste Mailbox (`INBOX`); Mailbox-Verwaltung ist out of scope. |
| `SUBSCRIBE` / `UNSUBSCRIBE` / `LSUB` | RFC 3501 §6.3.6/6.3.7/6.3.9 | Keine Mehrfach-Mailbox-Verwaltung vorhanden. |
| `STATUS` | RFC 3501 §6.3.10 | Kein Bedarf, da `SELECT` bereits die Nachrichtenanzahl liefert. |
| `APPEND` | RFC 3501 §6.3.11 | Mails werden ausschließlich per SMTP eingeliefert, nicht direkt per IMAP hochgeladen. |
| `CHECK` | RFC 3501 §6.4.1 | No-Op laut RFC, für den Mock ohne Nutzen. |
| `CLOSE` | RFC 3501 §6.4.2 | Da `EXPUNGE` nicht implementiert ist (siehe unten), hat `CLOSE` (das implizit expunged) keinen sinnvollen Effekt. |
| `EXPUNGE` | RFC 3501 §6.4.3 | Mit `\Deleted` markierte Nachrichten werden nie tatsächlich gelöscht — Flags werden zwar über `STORE` gesetzt, aber nichts entfernt sie aus dem Speicher. Das vereinfacht die ID-Stabilität (siehe UID-Abschnitt oben), bedeutet aber auch, dass Clients, die auf echtes Löschen testen wollen, damit nicht bedient werden. |
| `COPY` | RFC 3501 §6.4.7 | Keine Mehrfach-Mailbox-Unterstützung, daher kein Kopierziel. |
| `NAMESPACE` | RFC 2342 | Nur ein einziger, impliziter Namespace vorhanden. |
| `ID` | RFC 2971 | Reine Metadaten-Extension ohne Testrelevanz. |

## Weitere Vereinfachungen

- **Keine Trennung von Sequenznummer und UID:** Beide sind identisch mit der
  fortlaufenden Datei-ID aus `storage.py`. Das ist nur korrekt, solange nie
  expunged wird.
- **Kommando-Parsing:** Zeilen werden mit `line.split(" ", 2)` in maximal drei
  Teile zerlegt (Tag, Kommando, Rest). Die IMAP-Literal-Syntax
  (`{octet-count}\r\n`) für lange oder binäre Argumente wird nirgends
  unterstützt.
- **Kein Connection-State-Machine-Enforcement:** Es wird nur geprüft, ob
  überhaupt eingeloggt ist (`auth`), nicht ob zuvor `SELECT` erfolgreich war.
  `FETCH`/`STORE`/`SEARCH` funktionieren also technisch auch ohne vorheriges
  `SELECT`.
- **Logging:** Bei jedem `FETCH` wird `From`/`To`/`Subject` der ausgelieferten
  Nachricht geloggt (siehe `logger.info("outgoing ...")`), nützlich um in
  Tests nachzuvollziehen, was ein Client tatsächlich abgeholt hat.
