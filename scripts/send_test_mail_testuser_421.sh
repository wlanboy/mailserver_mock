#!/usr/bin/env bash
# Sendet eine Test-Mail als Test-User "testuser_421" (Verhalten: too_many).
# Login schlaegt nach ca. 1s Verzoegerung mit SMTP 421 4.7.0 fehl.
# Siehe users.json / README.md fuer Details zum Testszenario.
#
# Nutzung:
#   ./scripts/send_test_mail_testuser_421.sh
#   MAIL_TO=other@example.com ./scripts/send_test_mail_testuser_421.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MAIL_USER="${MAIL_USER:-testuser_421}" \
MAIL_PASS="${MAIL_PASS:-testpass_421}" \
exec "${SCRIPT_DIR}/send_test_mail.sh" "$@"
