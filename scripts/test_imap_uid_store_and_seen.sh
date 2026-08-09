#!/usr/bin/env bash
# Prueft gegen den laufenden mailserver_mock, dass FETCH/UID FETCH \Seen
# implizit setzen (ausser bei BODY.PEEK[]) und dass UID STORE die UID in
# der unsolicited FETCH-Antwort mitliefert. Siehe src/imap.md (§6.4.5,
# §6.4.8) fuer die zugrunde liegende RFC-3501-Semantik.
#
# Voraussetzung: mailserver_mock laeuft bereits (z.B. `uv run mailserver-mock`).
#
# Konfiguration ueber Umgebungsvariablen (Defaults passen zu imap_server.py):
#   IMAP_HOST, IMAP_PORT
#
# Nutzung:
#   ./scripts/test_imap_uid_store_and_seen.sh
set -euo pipefail

IMAP_HOST="${IMAP_HOST:-127.0.0.1}"
IMAP_PORT="${IMAP_PORT:-1143}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== Testmail per SMTP senden =="
"${SCRIPT_DIR}/send_test_mail_testuser.sh" >/dev/null 2>&1

echo "== IMAP-Verbindung zu ${IMAP_HOST}:${IMAP_PORT} oeffnen =="
exec 3<>"/dev/tcp/${IMAP_HOST}/${IMAP_PORT}"
trap 'exec 3<&- 3>&- 2>/dev/null || true' EXIT

imap_send() {
    printf '%s\r\n' "$1" >&3
}

# Liest Zeilen bis (inklusive) der Zeile, die mit "<tag> " beginnt, und gibt
# sie auf stdout aus (fuer $(...)-Erfassung durch den Aufrufer).
imap_read_until() {
    local tag="$1" line
    while IFS= read -r -u 3 line; do
        line="${line%$'\r'}"
        echo "$line"
        [[ "$line" == "${tag} "* ]] && break
    done
}

get_unseen() {
    imap_send "S STATUS INBOX (UNSEEN)"
    local resp
    resp="$(imap_read_until "S")"
    echo "$resp" | sed -n 's/.*UNSEEN \([0-9]\+\).*/\1/p' | head -1
}

fail() {
    echo "FEHLER: $1" >&2
    exit 1
}

# Begruessung abholen.
read -r -u 3 greeting
echo "Greeting: ${greeting%$'\r'}"

imap_send "A1 LOGIN testuser testpass"
imap_read_until "A1" >/dev/null

imap_send "A2 SELECT INBOX"
imap_read_until "A2" >/dev/null

imap_send "A3 UID SEARCH ALL"
search_resp="$(imap_read_until "A3")"
last_uid="$(echo "$search_resp" | grep '^\* SEARCH' | awk '{print $NF}')"
[[ -n "$last_uid" ]] || fail "keine Nachricht in der Mailbox gefunden"
echo "Teste mit UID ${last_uid}"

echo "== UNSEEN vor FETCH =="
unseen_before="$(get_unseen)"
echo "UNSEEN=${unseen_before}"

echo "== UID FETCH ${last_uid} (BODY.PEEK[]) — darf \\Seen NICHT setzen =="
imap_send "A4 UID FETCH ${last_uid} (BODY.PEEK[])"
imap_read_until "A4" >/dev/null
unseen_after_peek="$(get_unseen)"
echo "UNSEEN=${unseen_after_peek}"
[[ "$unseen_after_peek" == "$unseen_before" ]] || fail "BODY.PEEK[] hat \\Seen faelschlich gesetzt"
echo "OK: BODY.PEEK[] hat UNSEEN nicht veraendert"

echo "== UID FETCH ${last_uid} (BODY[]) — muss \\Seen setzen =="
imap_send "A5 UID FETCH ${last_uid} (BODY[])"
imap_read_until "A5" >/dev/null
unseen_after_body="$(get_unseen)"
echo "UNSEEN=${unseen_after_body}"
expected=$((unseen_before - 1))
[[ "$unseen_after_body" == "$expected" ]] || fail "BODY[] hat \\Seen nicht implizit gesetzt (UNSEEN=${unseen_after_body}, erwartet ${expected})"
echo "OK: BODY[] hat \\Seen implizit gesetzt"

echo "== UID STORE ${last_uid} -FLAGS (\\Seen) — Antwort muss UID enthalten =="
imap_send "A6 UID STORE ${last_uid} -FLAGS (\\Seen)"
store_resp="$(imap_read_until "A6")"
echo "$store_resp" | grep -q "UID ${last_uid}" || fail "UID STORE Antwort enthaelt kein 'UID ${last_uid}'"
echo "OK: UID STORE liefert UID in der FETCH-Antwort"

unseen_after_store="$(get_unseen)"
echo "UNSEEN=${unseen_after_store}"
[[ "$unseen_after_store" == "$unseen_before" ]] || fail "UID STORE -FLAGS (\\Seen) hat UNSEEN nicht zurueckgesetzt"
echo "OK: UNSEEN wieder bei ${unseen_after_store}"

imap_send "A7 LOGOUT"
imap_read_until "A7" >/dev/null

echo "Alle Pruefungen erfolgreich."
