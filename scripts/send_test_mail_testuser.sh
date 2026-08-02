#!/usr/bin/env bash
# Sendet eine Test-Mail als Test-User "testuser" (Verhalten: normal).
# Siehe users.json / README.md fuer Details zum Testszenario.
#
# Nutzung:
#   ./scripts/send_test_mail_testuser.sh
#   MAIL_TO=other@example.com ./scripts/send_test_mail_testuser.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MAIL_USER="${MAIL_USER:-testuser}" \
MAIL_PASS="${MAIL_PASS:-testpass}" \
exec "${SCRIPT_DIR}/send_test_mail.sh" "$@"
