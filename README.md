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
| HTTP (API + Web-UI) | 127.0.0.1:8025 |

Standard-Zugangsdaten: `testuser` / `testpass`

Alle Werte lassen sich per Umgebungsvariable überschreiben:
`SMTP_HOST`, `SMTP_PORT`, `IMAP_HOST`, `IMAP_PORT`, `HTTP_HOST`, `HTTP_PORT`, `MAIL_USER`, `MAIL_PASS`, `MAIL_DIR`, `USERS_FILE`.

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
`UID FETCH`, `UID SEARCH`, `UID STORE`, `STORE` (`FLAGS`/`+FLAGS`/`-FLAGS`,
inkl. `.SILENT`), `SEARCH`, `IDLE`, `LOGOUT`. `FETCH`/`UID FETCH` setzen
`\Seen` implizit, außer bei `BODY.PEEK[...]` (RFC 3501 §6.4.5).

**HTTP:** kleine JSON-API und Web-UI nach dem Vorbild von MailHog/Mailpit,
siehe [HTTP-API & Web-UI](#http-api--web-ui).

Ausführliche Beschreibung der jeweiligen RFC-Implementierung inkl. bewusst
ausgelassener Teile: [`src/smtp.md`](src/smtp.md), [`src/imap.md`](src/imap.md).

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

Der Server läuft im Vordergrund und beendet SMTP, IMAP und HTTP gemeinsam bei `Ctrl+C`.

## HTTP-API & Web-UI

Unter <http://127.0.0.1:8025/> läuft eine Weboberfläche (statisches HTML/CSS/JS, kein Framework),
die eingegangene Mails live anzeigt: Liste mit Suche, Detailansicht mit HTML-, Text-, Header- und
Quelltext-Tab, Download als `.eml`, Löschen einzelner oder aller Mails. HTML-Mails werden in einem
`<iframe sandbox>` ohne Skripte und ohne externe Ressourcen gerendert. Tastatur: `j`/`k` bzw.
Pfeiltasten zum Blättern, `/` Suche, `r` neu laden, `Entf` löschen, `Esc` schließen.

Dieselben Daten liefert die JSON-API, damit Integrationstests prüfen können, ob eine Mail
angekommen ist, ohne IMAP zu sprechen:

| Methode  | Pfad               | Beschreibung |
|----------|--------------------|--------------|
| `GET`    | `/health`          | `200` mit Status von SMTP, IMAP und Mail-Verzeichnis, `503` wenn etwas davon nicht läuft |
| `GET`    | `/mails`           | Alle Mails, neueste zuerst. Optionale Filter (Teilstring, Groß-/Kleinschreibung egal): `?to=`, `?from=`, `?subject=` |
| `GET`    | `/mails/{id}`      | Eine Mail mit Headern, `text`, `html` und Anhangsliste |
| `GET`    | `/mails/{id}/raw`  | Rohe Nachricht als `message/rfc822` |
| `DELETE` | `/mails`           | Alle Mails löschen (Reset zwischen Tests), Antwort `{"deleted": n}` |
| `DELETE` | `/mails/{id}`      | Eine Mail löschen, `204` bzw. `404` |

`{id}` entspricht der IMAP-UID bzw. dem Dateinamen `<id>.eml`. Die API setzt keine IMAP-Flags;
`seen`/`flags` zeigen den aktuellen Stand aus IMAP.

```bash
curl -s localhost:8025/health
# {"status": "ok", "checks": {"smtp": "up", "imap": "up", "mail_dir": "up"}, "mails": 1}

curl -s 'localhost:8025/mails?to=me@test.com' | jq '.mails[0] | {id, subject, from, to}'
curl -s localhost:8025/mails/1 | jq -r .text
curl -s -X DELETE localhost:8025/mails
```

Ein Integrationstest kann so z. B. vor jedem Testfall `DELETE /mails` aufrufen und danach
`GET /mails?to=<empfänger>` pollen, bis `count` größer 0 ist.

## Tests

Reines `unittest`, keine zusätzliche Abhängigkeit nötig:

```bash
uv run python -m unittest discover -s tests -v
```

Die CI (`.github/workflows/ci.yml`) prüft zusätzlich mit Pyright die Typen
und führt die Tests via `pytest` aus:

```bash
uv run pyright .
uv run pytest
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

## Manueller Test der IMAP `\Seen`-Semantik

```bash
./scripts/test_imap_uid_store_and_seen.sh
```

Sendet per `send_test_mail_testuser.sh` eine Testmail und prüft anschließend
per rohem IMAP-Dialog (`/dev/tcp`), dass `FETCH`/`UID FETCH` `\Seen` implizit
setzen (außer bei `BODY.PEEK[...]`) und dass `UID STORE` die UID in der
unsolicited FETCH-Antwort mitliefert (siehe [`src/imap.md`](src/imap.md)
§6.4.5, §6.4.8). Voraussetzung: der Mock läuft bereits. Konfiguration über
`IMAP_HOST`, `IMAP_PORT`.

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

## Docker & Kubernetes (Helm)

Container-Image, `update.sh` und das Helm-Chart [`mailserver-mock-chart/`](mailserver-mock-chart)
liegen im Repo-Root. Details zum Chart (Werte, Istio-TCP-Gateway, Hinweise) stehen in
[`mailserver-mock-chart/mailserver-mock-chart.md`](mailserver-mock-chart/mailserver-mock-chart.md).

### Image bauen, taggen, veröffentlichen

```bash
docker build -t mailserver .
docker tag mailserver registry.gmk.lan/mailserver:latest
docker push registry.gmk.lan/mailserver:latest
```

### Chart installieren

```bash
kubectl create namespace mailmock
kubectl label namespace mailmock istio-injection=enabled --overwrite
helm install mailserver-mock ./mailserver-mock-chart -n mailmock --create-namespace
```

### Status prüfen

```bash
helm status mailserver-mock -n mailmock
kubectl get pods,svc,gateway,virtualservice -n mailmock
```

Im Cluster kommt `users.json` aus einer ConfigMap (Default-Inhalt in `values.yaml`). Fehlerszenarien
lassen sich so ohne Rebuild ändern: `helm upgrade ... --set-file users.json=users.json`.

### Chart aktualisieren

```bash
helm upgrade mailserver-mock ./mailserver-mock-chart -n mailmock
```

### Deployment neu ausrollen

```bash
kubectl rollout restart deployment/mailserver-mock -n mailmock
```

Alle vier Schritte (Image bauen/taggen/pushen, Helm-Upgrade, Rollout-Restart) fasst
[`update.sh`](update.sh) zusammen:

```bash
./update.sh
```

### Deployment testen

Voraussetzung: DNS für `mailmock.gmk.lan` zeigt auf die Istio-Ingress-Gateway-VIP, und deren Service
exponiert zusätzlich die Ports 1025/1143 (siehe Hinweise in
[`mailserver-mock-chart.md`](mailserver-mock-chart/mailserver-mock-chart.md)).

SMTP-Test per `swaks` (siehe auch [oben](#manueller-test-mit-swaks)):

```bash
swaks \
  --to "me@test.com" \
  --from "you@test.com" \
  --server mailmock.gmk.lan \
  --port 1025 \
  --auth-user testuser \
  --auth-password testpass \
  --body "This is the email body"
```

IMAP-Test per rohem Dialog (`/dev/tcp`), analog zu
[`scripts/test_imap_uid_store_and_seen.sh`](scripts/test_imap_uid_store_and_seen.sh):

```bash
exec 3<>/dev/tcp/mailmock.gmk.lan/1143
printf 'a1 LOGIN testuser testpass\r\n' >&3
printf 'a2 SELECT INBOX\r\n' >&3
printf 'a3 SEARCH ALL\r\n' >&3
printf 'a4 LOGOUT\r\n' >&3
timeout 2 cat <&3
exec 3<&- 3>&-
```

Alternativ direkt aus einem Pod im Cluster gegen den `ClusterIP`-Service testen (kein Gateway nötig):

```bash
kubectl run mailtest --rm -it --restart=Never -n mailmock --image=debian:stable-slim -- \
  bash -c "apt-get update -qq && apt-get install -y -qq swaks >/dev/null && \
    swaks --to me@test.com --from you@test.com \
      --server mailserver-mock-svc.mailmock.svc.cluster.local --port 1025 \
      --auth-user testuser --auth-password testpass --body 'This is the email body'"
```

HTTP-API und Web-UI laufen über das Gateway auf Port 80 unter `mailmock.gmk.lan`:

```bash
curl -s http://mailmock.gmk.lan/health
curl -s http://mailmock.gmk.lan/mails | jq '.mails[] | {id, subject}'
```

Erwartetes Ergebnis in beiden Fällen: SMTP meldet `235` bei Login und `250` nach `DATA`, die IMAP-Session
liefert nach `LOGIN`/`SELECT`/`SEARCH ALL` mindestens eine Message-ID zurück.

