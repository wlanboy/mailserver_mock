"""Minimaler IMAP4rev1-Mock: LOGIN, LIST, SELECT, FETCH, UID, STORE, SEARCH, IDLE, LOGOUT."""
import logging
import os
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
                self._handle_search(tag)

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
            self._handle_search(tag)
        else:
            logger.warning("unhandled UID subcommand sub=%s args=%r", sub, args)
            self._write(f"{tag} BAD UID command not supported")

    def _handle_fetch(self, tag, args):
        fields = args.split()
        if not fields:
            self._write(f"{tag} BAD FETCH syntax")
            return

        max_id = storage.count_mails()
        ids = _parse_seq_set(fields[0], max_id)

        for msg_id in ids:
            flags = storage.load_flags(msg_id)
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

    def _handle_store(self, tag, args):
        parts = args.split()
        if len(parts) < 3:
            self._write(f"{tag} BAD STORE syntax")
            return
        msg_num = parts[0]
        action = parts[1].upper()
        flags_str = " ".join(parts[2:]).strip("()")
        new_flags = flags_str.split()

        current = storage.load_flags(msg_num)

        if action == "FLAGS":
            current = new_flags
        elif action in ("+FLAGS", "+FLAGS.SILENT"):
            for f in new_flags:
                if f not in current:
                    current.append(f)
        elif action in ("-FLAGS", "-FLAGS.SILENT"):
            current = [f for f in current if f not in new_flags]
        else:
            self._write(f"{tag} BAD STORE action")
            return

        storage.save_flags(msg_num, current)

        if ".SILENT" not in action:
            self._write(f"* {msg_num} FETCH (FLAGS ({' '.join(current)}))")
        self._write(f"{tag} OK STORE completed")

    def _handle_search(self, tag):
        count = storage.count_mails()
        ids = [str(i) for i in range(1, count + 1)]
        self._write("* SEARCH " + " ".join(ids))
        self._write(f"{tag} OK SEARCH completed")


class IMAPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_imap(host=IMAP_HOST, port=IMAP_PORT):
    return IMAPServer((host, port), IMAPHandler)
