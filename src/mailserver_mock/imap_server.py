"""Minimaler IMAP4rev1-Mock: LOGIN, LIST, SELECT, FETCH, UID, STORE, SEARCH, IDLE, LOGOUT."""
import logging
import os
import re
import shlex
import socketserver
import time
from email import message_from_string

from . import storage, users

IMAP_HOST = os.environ.get("IMAP_HOST", "127.0.0.1")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "1143"))

logger = logging.getLogger(__name__)


def _format_response_code(cfg, default_code, default_message):
    code = cfg.get("response_code", default_code)
    message = cfg.get("message", default_message)
    return f"[{code}] {message}"


def _parse_seq_id(s, max_id):
    if s == "*":
        return max_id
    try:
        return int(s)
    except ValueError:
        return 0


_SEQ_NUM = r"(?:[1-9][0-9]*|\*)"
_SEQ_SET_RE = re.compile(rf"^{_SEQ_NUM}(?::{_SEQ_NUM})?(?:,{_SEQ_NUM}(?::{_SEQ_NUM})?)*$")


def _parse_seq_set(seq_set, max_id):
    """Parst IMAP-Sequence-Sets: "1", "1:*", "2:4", "1,3,5:7"."""
    ids = []
    for part in seq_set.split(","):
        part = part.strip()
        if ":" in part:
            range_parts = part.split(":")
            if len(range_parts) != 2:
                continue
            start = _parse_seq_id(range_parts[0], max_id)
            end = _parse_seq_id(range_parts[1], max_id)
            if start > end:
                start, end = end, start
            for i in range(start, end + 1):
                if 0 < i <= max_id:
                    ids.append(i)
        else:
            i = _parse_seq_id(part, max_id)
            if 0 < i <= max_id:
                ids.append(i)
    return sorted(set(ids))


_FLAG_SEARCH_KEYS = {
    "ANSWERED": "\\Answered",
    "DELETED": "\\Deleted",
    "DRAFT": "\\Draft",
    "FLAGGED": "\\Flagged",
    "SEEN": "\\Seen",
}
_TEXT_SEARCH_KEYS = {
    "SUBJECT": "Subject",
    "FROM": "From",
    "TO": "To",
    "CC": "Cc",
    "BCC": "Bcc",
}


def _tokenize_search(args):
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


def _search_text_matches(msg_id, key, needle):
    raw = storage.load_mail(msg_id)
    msg = message_from_string(raw)
    if key == "TEXT":
        haystack = raw
    elif key == "BODY":
        haystack = str(msg.get_payload() or "")
    else:
        haystack = msg.get(_TEXT_SEARCH_KEYS[key], "")
    return needle in haystack.lower()


def _fetch_marks_seen(items):
    """Prüft, ob angefragte FETCH-Items laut RFC 3501 §6.4.5 implizit \\Seen setzen.

    BODY.PEEK[...] und RFC822.HEADER/.SIZE setzen \\Seen nicht; jedes andere
    BODY[...] sowie das nackte RFC822/RFC822.TEXT-Makro schon.
    """
    upper = items.upper()
    without_peek = upper.replace("BODY.PEEK[", "")
    if "BODY[" in without_peek:
        return True
    return bool(re.search(r"RFC822(?!\.(?:HEADER|SIZE))\b", upper))


class IMAPHandler(socketserver.StreamRequestHandler):
    def _write(self, msg):
        self.wfile.write((msg + "\r\n").encode("utf-8"))

    def _read(self):
        line = self.rfile.readline()
        if not line:
            return None
        return line.decode("utf-8", errors="replace")

    def handle(self):
        self._write("* OK IMAP4rev1 Service Ready")

        self.auth = False
        self.quota_error = None

        while True:
            line = self._read()
            if line is None:
                return
            line = line.strip()
            if line == "":
                continue

            parts = line.split(" ", 2)
            if len(parts) < 2:
                continue
            tag = parts[0]
            cmd = parts[1].upper()
            args = parts[2] if len(parts) == 3 else ""

            if cmd == "CAPABILITY":
                self._write("* CAPABILITY IMAP4rev1 AUTH=LOGIN")
                self._write(f"{tag} OK CAPABILITY completed")

            elif cmd == "LOGIN":
                clean = args.replace('"', "")
                a = clean.split()
                if len(a) != 2:
                    self._write(f"{tag} BAD LOGIN syntax")
                    continue
                self._handle_login(tag, a[0], a[1])

            elif cmd == "LIST":
                self._write('* LIST (\\HasNoChildren) "/" "INBOX"')
                self._write(f"{tag} OK LIST completed")

            elif cmd == "SELECT":
                if not self.auth:
                    self._write(f"{tag} NO Authenticate first")
                    continue
                if self.quota_error:
                    self._write(f"{tag} NO {_format_response_code(self.quota_error, 'OVERQUOTA', 'Quota exceeded')}")
                    continue
                count = storage.count_mails()
                self._write(f"* {count} EXISTS")
                self._write("* FLAGS (\\Seen \\Deleted \\Answered)")
                self._write("* OK [PERMANENTFLAGS (\\Seen \\Deleted \\Answered)]")
                self._write(f"{tag} OK [READ-WRITE] SELECT completed")

            elif cmd == "STATUS":
                if not self.auth:
                    self._write(f"{tag} NO Authenticate first")
                    continue
                self._handle_status(tag, args)

            elif cmd == "UID":
                if not self.auth:
                    self._write(f"{tag} NO Authenticate first")
                    continue
                self._handle_uid(tag, args)

            elif cmd == "FETCH":
                if not self.auth:
                    self._write(f"{tag} NO Authenticate first")
                    continue
                self._handle_fetch(tag, args)

            elif cmd == "STORE":
                if not self.auth:
                    self._write(f"{tag} NO Authenticate first")
                    continue
                self._handle_store(tag, args)

            elif cmd == "SEARCH":
                if not self.auth:
                    self._write(f"{tag} NO Authenticate first")
                    continue
                self._handle_search(tag, args)

            elif cmd == "IDLE":
                if not self.auth:
                    self._write(f"{tag} NO Authenticate first")
                    continue
                self._write("+ idling")
                self._read()  # liest das "DONE" des Clients
                self._write(f"{tag} OK IDLE terminated")

            elif cmd == "LOGOUT":
                self._write("* BYE IMAP server logging out")
                self._write(f"{tag} OK LOGOUT completed")
                return

            else:
                logger.warning("unhandled command cmd=%s args=%r", cmd, args)
                self._write(f"{tag} BAD Unknown command")

    def _handle_login(self, tag, username, password):
        account = users.find_user(username)
        if account is None or account.get("password") != password:
            self._write(f"{tag} NO LOGIN failed")
            return

        behavior = account.get("behavior", "normal")
        if behavior == "normal":
            self.auth = True
            self._write(f"{tag} OK LOGIN completed")
            return

        if behavior == "quota":
            self.auth = True
            self.quota_error = account.get("imap", {})
            self._write(f"{tag} OK LOGIN completed")
            return

        # too_many / timeout: verzögerte temporäre Fehlerantwort statt Login-Erfolg
        time.sleep(float(account.get("delay_seconds", 0)))
        response = _format_response_code(account.get("imap", {}), "UNAVAILABLE", "Temporary failure, please try again later")
        self._write(f"{tag} NO {response}")

    def _handle_status(self, tag, args):
        paren_start = args.find("(")
        paren_end = args.rfind(")")
        if paren_start == -1 or paren_end == -1 or paren_end < paren_start:
            self._write(f"{tag} BAD STATUS syntax")
            return

        mailbox = args[:paren_start].strip().strip('"') or "INBOX"
        items = args[paren_start + 1 : paren_end].split()

        count = storage.count_mails()
        unseen = sum(1 for i in range(1, count + 1) if "\\Seen" not in storage.load_flags(i))
        values = {
            "MESSAGES": count,
            "RECENT": 0,
            "UIDNEXT": storage.next_id(),
            "UIDVALIDITY": 1,
            "UNSEEN": unseen,
        }

        result = []
        for item in items:
            key = item.upper()
            if key in values:
                result.append(f"{key} {values[key]}")

        self._write(f'* STATUS "{mailbox}" ({" ".join(result)})')
        self._write(f"{tag} OK STATUS completed")

    def _handle_uid(self, tag, args):
        parts = args.split(" ", 1)
        sub_fields = parts[0].split() if parts else []
        if not sub_fields:
            self._write(f"{tag} BAD UID syntax")
            return
        sub = sub_fields[0].upper()

        if sub == "FETCH":
            if len(parts) < 2:
                self._write(f"{tag} BAD UID FETCH syntax")
                return
            self._handle_fetch(tag, parts[1])
        elif sub == "SEARCH":
            self._handle_search(tag, parts[1] if len(parts) > 1 else "")
        elif sub == "STORE":
            if len(parts) < 2:
                self._write(f"{tag} BAD UID STORE syntax")
                return
            self._handle_store(tag, parts[1], is_uid=True)
        else:
            logger.warning("unhandled UID subcommand sub=%s args=%r", sub, args)
            self._write(f"{tag} BAD UID command not supported")

    def _handle_fetch(self, tag, args):
        fields = args.split(None, 1)
        if not fields:
            self._write(f"{tag} BAD FETCH syntax")
            return

        max_id = storage.count_mails()
        ids = _parse_seq_set(fields[0], max_id)
        mark_seen = _fetch_marks_seen(fields[1] if len(fields) > 1 else "")

        for msg_id in ids:
            flags = storage.load_flags(msg_id)
            if mark_seen and "\\Seen" not in flags:
                flags.append("\\Seen")
                storage.save_flags(msg_id, flags)
            body_str = storage.load_mail(msg_id)
            envelope = storage.get_envelope(body_str)
            body = body_str.encode("utf-8")

            headers = message_from_string(body_str)
            logger.info(
                "outgoing id=%s from=%s to=%s subject=%r",
                msg_id,
                headers.get("From", "NIL"),
                headers.get("To", "NIL"),
                headers.get("Subject", ""),
            )

            self.wfile.write(
                (
                    f"* {msg_id} FETCH (UID {msg_id} FLAGS ({' '.join(flags)}) "
                    f"ENVELOPE {envelope} BODY[] {{{len(body)}}}\r\n"
                ).encode("utf-8")
            )
            self.wfile.write(body)
            self.wfile.write(b")\r\n")
        self.wfile.flush()

        self._write(f"{tag} OK FETCH completed")

    def _handle_store(self, tag, args, is_uid=False):
        parts = args.split()
        if len(parts) < 3:
            self._write(f"{tag} BAD STORE syntax")
            return
        seq_set = parts[0]
        action = parts[1].upper()
        flags_str = " ".join(parts[2:]).strip("()")
        new_flags = flags_str.split()

        if not _SEQ_SET_RE.match(seq_set):
            self._write(f"{tag} BAD STORE invalid sequence set")
            return
        if action not in ("FLAGS", "FLAGS.SILENT", "+FLAGS", "+FLAGS.SILENT", "-FLAGS", "-FLAGS.SILENT"):
            self._write(f"{tag} BAD STORE action")
            return

        for msg_id in _parse_seq_set(seq_set, storage.count_mails()):
            current = storage.load_flags(msg_id)
            if action.startswith("+"):
                for f in new_flags:
                    if f not in current:
                        current.append(f)
            elif action.startswith("-"):
                current = [f for f in current if f not in new_flags]
            else:
                current = list(new_flags)

            storage.save_flags(msg_id, current)

            if ".SILENT" not in action:
                if is_uid:
                    self._write(f"* {msg_id} FETCH (FLAGS ({' '.join(current)}) UID {msg_id})")
                else:
                    self._write(f"* {msg_id} FETCH (FLAGS ({' '.join(current)}))")
        self._write(f"{tag} OK STORE completed")

    def _handle_search(self, tag, args=""):
        """Wertet eine Teilmenge der RFC-3501-SEARCH-Kriterien aus.

        Unterstützt: ALL, (UN)SEEN, (UN)ANSWERED, (UN)DELETED, (UN)FLAGGED,
        (UN)DRAFT, NEW, OLD, RECENT, UID <seq-set>, SUBJECT/FROM/TO/CC/BCC/
        BODY/TEXT <string> sowie nackte Sequence-Sets. Mehrere Kriterien
        werden UND-verknüpft. NOT/OR/Datumskriterien werden nicht ausgewertet
        (unbekannte Tokens filtern nicht, statt einen Fehler zu werfen).
        """
        max_id = storage.count_mails()
        ids = list(range(1, max_id + 1))

        tokens = _tokenize_search(args)
        i = 0
        while i < len(tokens):
            key = tokens[i].upper()
            if key == "ALL":
                i += 1
            elif key in _FLAG_SEARCH_KEYS:
                flag = _FLAG_SEARCH_KEYS[key]
                ids = [n for n in ids if flag in storage.load_flags(n)]
                i += 1
            elif key.startswith("UN") and key[2:] in _FLAG_SEARCH_KEYS:
                flag = _FLAG_SEARCH_KEYS[key[2:]]
                ids = [n for n in ids if flag not in storage.load_flags(n)]
                i += 1
            elif key == "NEW":
                ids = [n for n in ids if "\\Seen" not in storage.load_flags(n)]
                i += 1
            elif key == "OLD":
                i += 1  # kein \Recent-Tracking -> jede Mail gilt als "old"
            elif key == "RECENT":
                ids = []  # kein \Recent-Tracking -> nie ein Treffer
                i += 1
            elif key == "UID" and i + 1 < len(tokens):
                wanted = set(_parse_seq_set(tokens[i + 1], max_id))
                ids = [n for n in ids if n in wanted]
                i += 2
            elif key in _TEXT_SEARCH_KEYS or key in ("BODY", "TEXT"):
                if i + 1 >= len(tokens):
                    i += 1
                    continue
                needle = tokens[i + 1].lower()
                ids = [n for n in ids if _search_text_matches(n, key, needle)]
                i += 2
            else:
                seq = _parse_seq_set(tokens[i], max_id)
                ids = [n for n in ids if n in set(seq)] if seq else ids
                i += 1

        self._write("* SEARCH " + " ".join(str(n) for n in ids))
        self._write(f"{tag} OK SEARCH completed")


class IMAPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_imap(host=IMAP_HOST, port=IMAP_PORT):
    return IMAPServer((host, port), IMAPHandler)
