# SMTP-Implementierung

Dieses Dokument beschreibt, welche Teile von SMTP (RFC 5321) und SMTP AUTH
(RFC 4954) der Mock in
[`src/mailserver_mock/smtp_server.py`](src/mailserver_mock/smtp_server.py)
implementiert, wie sich die Befehle im Detail verhalten und welche Teile des
RFCs bewusst ausgelassen wurden. Ziel ist ein Server, der reale SMTP-Clients
(`smtplib`, Jakarta Mail/Spring Boot, swaks, ...) für lokale Tests annehmen
und die Nachrichten als `.eml`-Dateien ablegen kann — inklusive gezielt
simulierbarer Fehlerszenarien (Timeouts, Quota, Auth-Fehler). Kein Relaying,
keine echte Zustellung, keine Warteschlange.

## Session-Ablauf

Nach Verbindungsaufbau sendet der Server sofort:

```
220 localhost ESMTP ready
```

Danach wird zeilenweise gelesen. Es gibt keinen State für
"Connection established / greeted", nur die drei Variablen `auth`,
`mail_from`/`rcpt_to` (Transaktionszustand) und `data_mode` (ob gerade der
DATA-Body eingelesen wird).

### Timeout und Größenlimits

- **Timeout.** Jede Verbindung bekommt ein Socket-Timeout von `SMTP_TIMEOUT`
  Sekunden (Default 300, RFC 5321 §4.5.3.2). Kommt in dieser Zeit nichts vom
  Client, antwortet der Server mit `421 4.4.2 localhost Error: timeout exceeded`
  und schließt die Verbindung. `SMTP_TIMEOUT=0` schaltet das Timeout ab.
- **Zeilenlänge.** Kommandozeilen sind auf 8 KiB begrenzt. Längere Zeilen verwirft der
  Server bis zum Zeilenende und antwortet mit `500 5.5.2 Line too long`.
- **Nachrichtengröße.** `SMTP_MAX_MESSAGE_SIZE` (Default 10 MiB) wird im `EHLO`
  als `SIZE` angekündigt. Ein `MAIL FROM:<...> SIZE=n` über der Grenze lehnt der
  Server sofort mit `552 5.3.4 Message size exceeds fixed maximum message size`
  ab. Wird die Grenze erst während `DATA` überschritten, liest der Server bis
  zum abschließenden `.` weiter, puffert aber nichts mehr und antwortet dann mit
  derselben 552-Meldung. Die Session bleibt danach nutzbar.
- `DATA` wird in Stücken von höchstens 64 KiB gelesen, damit auch eine einzelne
  überlange Zeile nicht ungeprüft im Speicher landet.
- Parameter hinter der Adresse in `MAIL FROM` (z. B. `SIZE=`, `BODY=`) werden
  abgetrennt und nicht als Teil des Absenders gespeichert.

## Implementierte Befehle

### EHLO / HELO (RFC 5321 §4.1.1.1)

```
250-localhost greets you
250-AUTH LOGIN PLAIN
250-SIZE 10485760
250 OK
```

`EHLO` liefert eine minimale Extension-Liste: `AUTH LOGIN PLAIN` und `SIZE`
(RFC 1870) mit der Obergrenze aus `SMTP_MAX_MESSAGE_SIZE`. Es gibt
keine Auswertung des vom Client übergebenen Domain-Parameters. `HELO`
antwortet knapp mit `250 localhost` (keine Extension-Liste, wie im RFC für
den Basis-Befehl vorgesehen).

Nicht beworben und nicht implementiert: `8BITMIME`, `PIPELINING`,
`ENHANCEDSTATUSCODES`, `STARTTLS`, `SMTPUTF8`, `CHUNKING`/`BDAT`. Da diese
nicht in der `EHLO`-Antwort auftauchen, verhalten sich RFC-konforme Clients
korrekt, indem sie diese Fähigkeiten gar nicht erst nutzen.

### AUTH LOGIN (RFC 4954, Mechanismus definiert außerhalb des RFCs als
De-facto-Standard, base64-kodierter Username/Passwort-Dialog)

```
a: AUTH LOGIN
s: 334 VXNlcm5hbWU6        (base64 "Username:")
c: <base64 username>
s: 334 UGFzc3dvcmQ6        (base64 "Password:")
c: <base64 password>
s: 235 Authentication successful   (oder 535 / 421 / 451, siehe unten)
```

Fehlerhaftes Base64 wird über `_b64decode` abgefangen und als leerer String
behandelt (führt in der Regel zu `535 Authentication failed`, da der
User-Lookup fehlschlägt), nicht zu einem Protokollfehler.

### AUTH PLAIN (RFC 4616 SASL-Mechanismus, RFC 4954 Einbettung in SMTP)

Unterstützt **beide** Varianten aus RFC 4954 §4:

- **Initial Response inline:** `AUTH PLAIN <base64>` (3 Token in der Zeile)
- **SASL-Continuation:** `AUTH PLAIN` ohne Argument → Server antwortet
  `334 ` (leerer Prompt) → Client sendet die base64-kodierte Antwort in der
  nächsten Zeile.

  Diese zweite Form wird explizit unterstützt, weil sie u. a. von Jakarta
  Mail / Spring Boot Mail Sender so genutzt wird (siehe Kommentar im Code).

Die dekodierte Payload wird als `\x00`-getrennte 3-Feld-Struktur
(`authzid\x00authcid\x00passwd`) erwartet, wie in RFC 4616 §2 definiert.
`authzid` wird ignoriert, `authcid`/`passwd` werden für den Login verwendet.
Bei abweichender Feldanzahl: `535 Authentication failed`.

### Login-Fehlerszenarien (`users.json`)

Beide AUTH-Varianten laufen durch dieselbe `_login()`-Methode, die
Zugangsdaten gegen `users.json` prüft (siehe
[`users.py`](src/mailserver_mock/users.py)) und je nach `behavior`-Feld
reagiert:

| `behavior` | SMTP-Antwort | Verbindung danach |
|---|---|---|
| `normal` | `235 Authentication successful` | offen, authentifiziert |
| `quota` | `235 Authentication successful` | offen, authentifiziert, aber `DATA` liefert später einen konfigurierten Fehler (siehe unten) |
| `too_many` / `timeout` | nach `delay_seconds` Wartezeit: konfigurierbare Antwort, Default `421 4.7.0 Service not available, closing transmission channel` | **wird vom Server geschlossen** (`return` in `handle()`) |
| unbekannter User / falsches Passwort | `535 Authentication failed` | offen, nicht authentifiziert, Client kann erneut `AUTH` versuchen |

Die `421`/`451`-Antworten samt SMTP-Enhanced-Status-Code (RFC 3463) werden
aus dem `smtp`-Objekt des jeweiligen Users in `users.json` gebaut
(`_format_error`: `"<code> <enhanced_code> <message>"`). Damit lassen sich
beliebige temporäre (4xx) oder permanente (5xx) Fehlercodes simulieren, ohne
den Server-Code anzufassen — siehe `users.json` im Repo-Root für die
mitgelieferten Beispiel-User `testuser_421`, `testuser_451`, `testuser_552`.

Wichtig: Bei `too_many`/`timeout` schlägt laut RFC 5321 §3.3 zwar nur die
Authentifizierung fehl, hier wird aber zusätzlich die TCP-Verbindung
geschlossen — das bildet reale Szenarien nach, in denen ein überlasteter
Server die Verbindung kappt, statt weitere Kommandos entgegenzunehmen.

### MAIL FROM / RCPT TO (RFC 5321 §4.1.1.2 / §4.1.1.3)

```
MAIL FROM:<sender@example.com>
250 OK
RCPT TO:<rcpt@example.com>
250 OK
```

- Beide Befehle (und `DATA`) erfordern im Default vorherige Authentifizierung
  (`530 Authentication required`, falls nicht). Das ist eine
  Test-Server-Konvention, kein RFC-5321-Zwang (SMTP AUTH ist laut RFC 4954
  grundsätzlich optional). Mit `SMTP_AUTH_REQUIRED=false` entfällt die
  Prüfung, und der Server nimmt Mails wie MailHog oder Mailpit auch ohne
  `AUTH` an. `EHLO` kündigt `AUTH LOGIN PLAIN` trotzdem an. Meldet sich ein
  Client an, gilt weiter das `behavior` aus `users.json`; ein fehlgeschlagenes
  `AUTH` (`535`) blockiert die anschließende Transaktion nicht.
- Der komplette Rest der Zeile nach `MAIL FROM:`/`RCPT TO:` wird
  **unverarbeitet als String übernommen** — keine Prüfung auf
  spitze Klammern (`<...>`), keine Adress-Syntaxvalidierung, keine
  Auswertung von ESMTP-Parametern wie `SIZE=`, `BODY=8BITMIME` etc. (RFC
  5321 §4.1.1.11). Ein Client kann faktisch beliebigen Text als
  "Absender"/"Empfänger" schicken.
- `RCPT TO` kann beliebig oft wiederholt werden (mehrere Empfänger pro
  Transaktion, RFC 5321 §3.3), alle werden in `rcpt_to` gesammelt.
- Es gibt **keine** Prüfung, ob der Empfänger überhaupt existiert/zustellbar
  ist (kein `550 No such user`) — jeder Empfänger wird akzeptiert.

### DATA (RFC 5321 §4.1.1.4)

```
DATA
354 End data with <CR><LF>.<CR><LF>
<Header/Body-Zeilen>
.
250 Message accepted
```

- Voraussetzung: Auth **und** vorheriges `MAIL FROM` **und** mindestens ein
  `RCPT TO`, sonst `503 Bad sequence of commands`.
- **Dot-Stuffing** wird korrekt gemäß RFC 5321 §4.5.2 rückgängig gemacht:
  Zeilen, die mit einem zusätzlichen `.` beginnen, weil die Originalzeile
  selbst mit `.` begann, werden beim Einlesen um dieses eine führende
  `.` reduziert.
- Das Ende der Daten wird durch eine Zeile erkannt, die exakt `.` ist.
- Die DATA-Zeilen werden als Bytes gelesen und nicht dekodiert. Eine Mail mit
  `Content-Transfer-Encoding: 8bit` in ISO-8859-1 oder einem anderen Charset
  landet Byte für Byte in der `.eml`-Datei. `8BITMIME` bewirbt der Server
  trotzdem nicht (siehe oben).
- Nach erfolgreichem Abschluss wird die Nachricht über
  `storage.save_mail(mail_from, rcpt_to, data)` als `<id>.eml` gespeichert
  (siehe [`storage.py`](src/mailserver_mock/storage.py)):
  - Der Header-Block endet an der ersten Leerzeile oder an der ersten Zeile,
    die weder Header-Feld noch Fortsetzungszeile ist. Im zweiten Fall fügt
    der Mock die fehlende Leerzeile ein (RFC 5322 §2.1).
  - Fehlen `From`, `To`, `Subject` oder `Date` im Header-Block, stellt der
    Mock genau diese Header voran. Ein `Subject:` im Body zählt nicht.
  - Vorhandene Header und der Body bleiben unverändert.
- `RFC822.SIZE` bei IMAP und `size` in der HTTP-API sind die Bytegröße der
  `.eml`-Datei, also das, was der Client gesendet hat, plus ergänzte Header.
- Ist der eingeloggte User im `quota`-Fehlerszenario, wird die Nachricht
  **nicht gespeichert**. Stattdessen antwortet der Server nach dem
  abschließenden `.` mit dem konfigurierten Fehler (Default
  `552 5.2.2 Mailbox full, quota exceeded`, RFC 3463/RFC 5321 §4.5.3.1.10
  für den 552-Code).
- Nach Abschluss (Erfolg oder Quota-Fehler) wird der Transaktionszustand
  zurückgesetzt (`mail_from`/`rcpt_to` geleert), sodass **mehrere Mails pro
  Session** möglich sind, wie es RFC 5321 §4.1.1.4 vorsieht.

### NOOP (RFC 5321 §4.1.1.9)

```
NOOP
250 OK
```

Kein Parameter-Handling nötig, entspricht dem RFC.

### RSET (RFC 5321 §4.1.1.5)

```
RSET
250 OK
```

Setzt `mail_from`, `rcpt_to`, `data_lines`, `data_mode` zurück. Der
Auth-Status bleibt erhalten (RFC 5321 verlangt das nicht explizit, ist aber
das in der Praxis erwartete Verhalten — ein `RSET` sollte keinen erneuten
Login erzwingen).

### QUIT (RFC 5321 §4.1.1.10)

```
QUIT
221 Bye
```

Danach wird die Verbindung serverseitig geschlossen.

## Nicht implementierte Befehle

| Befehl | RFC-Referenz | Warum nicht implementiert |
|---|---|---|
| `STARTTLS` | RFC 3207 | Nicht in `EHLO`-Antwort beworben; Mock läuft nur lokal/für Tests, keine Transportverschlüsselung nötig. |
| `VRFY` / `EXPN` | RFC 5321 §3.5 | Adressverifikation/Mailinglisten-Expansion ohne Testrelevanz; aus Sicherheitssicht in echten Servern ohnehin meist deaktiviert. |
| `BDAT` (CHUNKING) | RFC 3030 | Alternative zu `DATA` für binäre/gechunkte Übertragung, nicht benötigt, da Testnachrichten klein und textbasiert sind. |
| Weitere `AUTH`-Mechanismen (`CRAM-MD5`, `XOAUTH2`, ...) | RFC 4954 / diverse | Nur `LOGIN` und `PLAIN` werden von den referenzierten Test-Clients (`smtplib`, Jakarta Mail) genutzt; weitere Mechanismen bringen für Testzwecke keinen Mehrwert. |

Jeder unbekannte Befehl (inkl. der oben genannten) wird generisch mit
`502 Command not implemented` beantwortet (RFC 5321 §4.2.4).

## Weitere Vereinfachungen

- **Keine Adress-/Domain-Validierung** bei `MAIL FROM`/`RCPT TO` — jeder
  String wird akzeptiert.
- **Kein Empfänger-Limit, keine Relay-Beschränkung**, kein
  `Received:`-Header wird vom Server selbst ergänzt (im Gegensatz zu echten
  MTAs, die pro Hop einen `Received`-Header voranstellen, RFC 5321 §4.4).
- **Quota unabhängig von der Größe:** Die `552 5.2.2`-Quota-Antwort ist rein
  konfigurationsgesteuert über `users.json`. Nur die feste Obergrenze
  `SMTP_MAX_MESSAGE_SIZE` hängt von der tatsächlichen Nachrichtengröße ab
  (`552 5.3.4`, siehe oben).
- **Logging:** Nach erfolgreichem `DATA`-Abschluss wird `From`/`To`/`Subject`
  der gespeicherten Nachricht geloggt (`logger.info("incoming ...")`),
  nützlich um in Tests nachzuvollziehen, was tatsächlich empfangen wurde.
