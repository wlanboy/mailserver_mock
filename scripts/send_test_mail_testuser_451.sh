#!/usr/bin/env bash
# Sendet eine Test-Mail als Test-User "testuser_451" (Verhalten: timeout).
# Login liefert nach ca. 60s keine Antwort und schlaegt dann mit SMTP
# 451 4.4.2 fehl. Das Script laeuft entsprechend lange, bevor curl abbricht.
# Siehe users.json / README.md fuer Details zum Testszenario.
#
# Nutzung:
#   ./scripts/send_test_mail_testuser_451.sh
#   MAIL_TO=other@example.com ./scripts/send_test_mail_testuser_451.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MAIL_USER="${MAIL_USER:-testuser_451}" \
MAIL_PASS="${MAIL_PASS:-testpass_451}" \
exec "${SCRIPT_DIR}/send_test_mail.sh" "$@"
