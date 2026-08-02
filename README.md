# mailserver-mock

Minimaler SMTP- und IMAP-Mock-Server für lokale Tests, in reinem Python
(nur Standardbibliothek, keine Abhängigkeiten). Angelehnt an
[gosmtp](https://github.com/wlanboy/gosmtp) — verschickte Mails werden als
`.eml`-Dateien in einem lokalen `mails/`-Verzeichnis abgelegt.

## Ports

| Protokoll | Standardadresse   |
|-----------|-------------------|
| SMTP      | 127.0.0.1:1025    |
| IMAP      | 127.0.0.1:1143    |

Standard-Zugangsdaten: `testuser` / `testpass`

Alle Werte lassen sich per Umgebungsvariable überschreiben:
`SMTP_HOST`, `SMTP_PORT`, `IMAP_HOST`, `IMAP_PORT`, `MAIL_USER`, `MAIL_PASS`, `MAIL_DIR`, `USERS_FILE`.

## Test-User & simulierte Fehlerszenarien (`users.json`)

Zugangsdaten werden aus `users.json` (Pfad überschreibbar via `USERS_FILE`,
Default `./users.json`) geladen. Fehlt die Datei, wird ein einzelner
Normal-User aus `MAIL_USER`/`MAIL_PASS` gebildet (Abwärtskompatibilität).

Jeder Eintrag hat `username`, `password` und `behavior`. Neben `normal`
stehen drei Verhaltensweisen bereit, die einen Fehler nach RFC-Vorgabe für
SMTP und IMAP gleichermaßen simulieren:

| User               | Verhalten                                    | SMTP-Antwort        | IMAP-Antwortcode    |
|--------------------|-----------------------------------------------|----------------------|----------------------|
| `testuser`         | normaler Login, wie bisher                    | `235`                | `OK`                 |
| `testuser_421`      | nach 1s Verzögerung: zu viele Fehler/Anfragen | `421 4.7.0`          | `NO [LIMIT]`         |
| `testuser_451`      | nach 60s keine Antwort, dann Timeout          | `451 4.4.2`          | `NO [UNAVAILABLE]`   |
| `testuser_552`      | Login gelingt, danach Quota-Fehler            | `552 5.2.2`          | `NO [OVERQUOTA]`     |

Bei `testuser_421`/`testuser_451` schlägt der Login selbst fehl (nach der
konfigurierten Verzögerung) und die Verbindung wird geschlossen. Bei
`testuser_552` gelingt der Login, aber SMTP `DATA` (nach dem
abschließenden `.`) bzw. IMAP `SELECT` liefern den Quota-Fehler.

Jeder Eintrag kann `smtp`/`imap`-Objekte mit `code`/`enhanced_code`/
`message` (SMTP) bzw. `response_code`/`message` (IMAP) sowie
`delay_seconds` zur Anpassung mitgeben, siehe `users.json` im Repo-Root.

## Unterstützte Funktionen

**SMTP:** `EHLO`/`HELO`, `AUTH LOGIN`, `AUTH PLAIN` (inline und als
SASL-Continuation, wie z. B. Jakarta Mail/Spring Boot es nutzt), `MAIL FROM`,
`RCPT TO` (mehrere Empfänger), `DATA` inkl. Dot-Stuffing, `NOOP`, `RSET`,
`QUIT`, Auth-Pflicht vor `MAIL FROM`/`RCPT TO`/`DATA`, mehrere Mails pro
Session.

**IMAP:** `CAPABILITY`, `LOGIN`, `LIST`, `SELECT`, `STATUS`, `FETCH`,
`UID FETCH`, `UID SEARCH`, `STORE` (`FLAGS`/`+FLAGS`/`-FLAGS`, inkl.
`.SILENT`), `SEARCH`, `IDLE`, `LOGOUT`.

## Installation & Start

Mit [uv](https://docs.astral.sh/uv/) (empfohlen, `pyproject.toml` ist bereits konfiguriert):

```bash
uv sync
uv run mailserver-mock
```

Ohne uv, mit venv:

```bash
python3.9 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/mailserver-mock
```

Der Server läuft im Vordergrund und beendet SMTP + IMAP gemeinsam bei `Ctrl+C`.

## Tests

Reines `unittest`, keine zusätzliche Abhängigkeit nötig:

```bash
uv run python -m unittest discover -s tests -v
```

## Manueller Test mit swaks

```bash
sudo apt-get install swaks

swaks \
  --to "me@test.com" \
  --from "you@test.com" \
  --server 127.0.0.1 \
  --port 1025 \
  --auth-user testuser \
  --auth-password testpass \
  --body "This is the email body"
```

## Manueller Test mit dem Bash-Script

```bash
./scripts/send_test_mail.sh
```

Sendet eine Test-Mail per curl per SMTP. Konfiguration über die
Umgebungsvariablen `SMTP_HOST`, `SMTP_PORT`, `MAIL_USER`, `MAIL_PASS`,
`MAIL_FROM`, `MAIL_TO`.

Für jeden Test-User aus `users.json` gibt es zusätzlich ein Wrapper-Script,
das `MAIL_USER`/`MAIL_PASS` passend vorbelegt und die simulierten
Fehlerszenarien direkt testbar macht:

```bash
./scripts/send_test_mail_testuser.sh       # normal
./scripts/send_test_mail_testuser_421.sh   # too_many (SMTP 421)
./scripts/send_test_mail_testuser_451.sh   # timeout (SMTP 451, ~60s Verzoegerung)
./scripts/send_test_mail_testuser_552.sh   # quota (SMTP 552)
```

## Manueller Test mit den Python-Standardbibliotheken

```python
import smtplib
from email.mime.text import MIMEText

msg = MIMEText("Hello from smtplib")
msg["Subject"] = "Test"
msg["From"] = "sender@example.com"
msg["To"] = "rcpt@example.com"

s = smtplib.SMTP("127.0.0.1", 1025)
s.ehlo()
s.login("testuser", "testpass")
s.sendmail("sender@example.com", ["rcpt@example.com"], msg.as_string())
s.quit()
```

```python
import imaplib

m = imaplib.IMAP4("127.0.0.1", 1143)
m.login("testuser", "testpass")
m.select("INBOX")
typ, ids = m.search(None, "ALL")
typ, data = m.fetch(ids[0].split()[-1], "(FLAGS BODY[])")
m.logout()
```

