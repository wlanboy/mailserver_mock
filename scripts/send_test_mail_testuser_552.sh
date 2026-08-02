#!/usr/bin/env bash
# Sendet eine Test-Mail als Test-User "testuser_552" (Verhalten: quota).
# Login gelingt, DATA schlaegt danach mit SMTP 552 5.2.2 (Quota) fehl.
# Siehe users.json / README.md fuer Details zum Testszenario.
#
# Nutzung:
#   ./scripts/send_test_mail_testuser_552.sh
#   MAIL_TO=other@example.com ./scripts/send_test_mail_testuser_552.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MAIL_USER="${MAIL_USER:-testuser_552}" \
MAIL_PASS="${MAIL_PASS:-testpass}" \
exec "${SCRIPT_DIR}/send_test_mail.sh" "$@"
