#!/usr/bin/env bash
# Sendet eine Test-Mail per SMTP an den mailserver_mock.
#
# Konfiguration ueber Umgebungsvariablen (Defaults passen zu smtp_server.py):
#   SMTP_HOST, SMTP_PORT, MAIL_USER, MAIL_PASS, MAIL_FROM, MAIL_TO
#
# Nutzung:
#   ./scripts/send_test_mail.sh
#   MAIL_TO=other@example.com ./scripts/send_test_mail.sh
set -euo pipefail

SMTP_HOST="${SMTP_HOST:-127.0.0.1}"
SMTP_PORT="${SMTP_PORT:-1025}"
MAIL_USER="${MAIL_USER:-testuser}"
MAIL_PASS="${MAIL_PASS:-testpass}"
MAIL_FROM="${MAIL_FROM:-sender@example.com}"
MAIL_TO="${MAIL_TO:-recipient@example.com}"

if ! command -v curl >/dev/null 2>&1; then
    echo "Fehler: curl wird benoetigt, ist aber nicht installiert." >&2
    exit 1
fi

MESSAGE_FILE="$(mktemp)"
trap 'rm -f "$MESSAGE_FILE"' EXIT

cat > "$MESSAGE_FILE" <<EOF
From: ${MAIL_FROM}
To: ${MAIL_TO}
Subject: Test mail from send_test_mail.sh
Date: $(date -R)

Dies ist eine Testmail an den mailserver_mock.
EOF

curl --url "smtp://${SMTP_HOST}:${SMTP_PORT}" \
     --mail-from "${MAIL_FROM}" \
     --mail-rcpt "${MAIL_TO}" \
     --user "${MAIL_USER}:${MAIL_PASS}" \
     --upload-file "${MESSAGE_FILE}"

echo "Mail an ${MAIL_TO} ueber ${SMTP_HOST}:${SMTP_PORT} gesendet."
