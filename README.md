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
`SMTP_HOST`, `SMTP_PORT`, `IMAP_HOST`, `IMAP_PORT`, `MAIL_USER`, `MAIL_PASS`, `MAIL_DIR`.

## Unterstützte Funktionen

**SMTP:** `EHLO`/`HELO`, `AUTH LOGIN`, `AUTH PLAIN` (inline und als
SASL-Continuation, wie z. B. Jakarta Mail/Spring Boot es nutzt), `MAIL FROM`,
`RCPT TO` (mehrere Empfänger), `DATA` inkl. Dot-Stuffing, `NOOP`, `RSET`,
`QUIT`, Auth-Pflicht vor `MAIL FROM`/`RCPT TO`/`DATA`, mehrere Mails pro
Session.

**IMAP:** `CAPABILITY`, `LOGIN`, `LIST`, `SELECT`, `FETCH`, `UID FETCH`,
`UID SEARCH`, `STORE` (`FLAGS`/`+FLAGS`/`-FLAGS`, inkl. `.SILENT`),
`SEARCH`, `IDLE`, `LOGOUT`.

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

## Projektstruktur

```
src/mailserver_mock/
    server.py        — startet SMTP- und IMAP-Listener in Threads
    smtp_server.py    — SMTP-Protokoll-Handler
    imap_server.py    — IMAP-Protokoll-Handler
    storage.py        — Dateibasierte Ablage in mails/ (.eml + .flags)
scripts/
    send_test_mail.sh — Sendet per curl eine Test-Mail per SMTP
tests/
    test_mailserver.py — End-to-End-Socket-Tests für SMTP + IMAP
```
