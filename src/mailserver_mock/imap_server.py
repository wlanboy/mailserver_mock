"""Minimaler IMAP4rev1-Mock: LOGIN, LIST, SELECT/EXAMINE, STATUS, FETCH, UID, STORE,
SEARCH, NOOP, CHECK, EXPUNGE, CLOSE, IDLE, LOGOUT."""
import logging
import os
import re
import shlex
import socketserver
import time
from email import message_from_string

from . import connection, imap_fetch, storage, users

IMAP_HOST = os.environ.get("IMAP_HOST", "127.0.0.1")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "1143"))
# RFC 3501 §5.4: Autologout frühestens nach 30 Minuten Inaktivität. Das passt
# auch zu IDLE, das Clients laut RFC 2177 spätestens alle 29 Minuten erneuern.
IMAP_TIMEOUT = connection.env_timeout("IMAP_TIMEOUT", "1800")

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
            ids.extend(range(max(start, 1), min(end, max_id) + 1))
        else:
            i = _parse_seq_id(part, max_id)
            if 0 < i <= max_id:
                ids.append(i)
    return sorted(set(ids))


def _resolve_messages(seq_set, uids, is_uid):
    """Bildet ein Sequence-Set auf (Sequenznummer, UID)-Paare ab.

    ``uids`` ist ``storage.list_ids()``: Sequenznummer n gehört zu ``uids[n-1]``.
    Bei ``is_uid`` bezieht sich das Set auf UIDs, ``*`` ist dann die höchste UID.
    UIDs ohne Mail (Lücken) werden übersprungen.
    """
    if is_uid:
        seq_of = {uid: n for n, uid in enumerate(uids, start=1)}
        wanted = _parse_seq_set(seq_set, uids[-1] if uids else 0)
        return [(seq_of[uid], uid) for uid in wanted if uid in seq_of]
    return [(n, uids[n - 1]) for n in _parse_seq_set(seq_set, len(uids))]


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


_LITERAL_RE = re.compile(r"\{(\d+)\}$")
# Gilt für die ganze Kommandozeile samt aller Literale, nicht pro Literal.
_MAX_LITERAL_SIZE = 64 * 1024


def _quote_literal(data):
    """Wandelt Literal-Inhalt in einen IMAP-Quoted-String um.

    Die Escapes (``\\`` und ``\"``) versteht ``shlex.split`` im POSIX-Modus
    genauso wie ein IMAP-Parser, die Handler sehen danach nur noch Quoted-Strings.
    """
    return '"' + data.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _tokenize_search(args):
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


def _search_text_matches(msg_id, key, needle):
    text = storage.load_mail(msg_id).decode("utf-8", errors="replace")
    msg = message_from_string(text)
    if key == "TEXT":
        haystack = text
    elif key == "BODY":
        haystack = str(msg.get_payload() or "")
    else:
        haystack = msg.get(_TEXT_SEARCH_KEYS[key], "")
    return needle in haystack.lower()


_SYSTEM_FLAGS = "\\Answered \\Flagged \\Deleted \\Seen \\Draft"
_READ_WRITE = "READ-WRITE"
_READ_ONLY = "READ-ONLY"

# Mindestzustand für ein Kommando (RFC 3501 §3): jederzeit, nach LOGIN oder
# nach SELECT/EXAMINE. Die Prüfung macht handle() vor dem Aufruf des Handlers.
_ANY = "any"
_AUTH = "authenticated"
_SELECTED = "selected"

# Rückgabewert eines Handlers, der die Verbindung beenden will (LOGOUT).
_CLOSE = object()


class IMAPHandler(socketserver.StreamRequestHandler):
    # setup() setzt damit settimeout() auf dem Client-Socket.
    timeout = IMAP_TIMEOUT

    def _write(self, msg):
        self.wfile.write((msg + "\r\n").encode("utf-8"))

    def _read(self):
        """Liest eine Zeile (höchstens connection.MAX_LINE Bytes) und meldet Überlänge mit.

        Rückgabe ``(line, too_long)``; ``line`` ist ``None`` bei Verbindungsende.
        """
        line, too_long = connection.read_line(self.rfile)
        if line is None:
            return None, False
        return line.decode("utf-8", errors="replace"), too_long

    def _read_command(self):
        """Liest eine Kommandozeile samt synchronisierender Literale (``{n}``).

        Endet die Zeile auf ``{n}``, bestätigt der Server mit ``+ Ready``, liest
        genau n Bytes und danach den Rest der Zeile. Das Literal wird als
        Quoted-String in die Zeile eingesetzt. Gibt ``None`` bei Verbindungsende
        zurück und ``""``, wenn ein zu großes Literal abgelehnt wurde.
        """
        line, too_long = self._read()
        if line is None:
            return None
        tag = line.split(" ", 1)[0]
        if too_long:
            self._write(f"{tag or '*'} BAD Command line too long")
            return ""
        line = line.rstrip("\r\n")
        while match := _LITERAL_RE.search(line):
            size = int(match.group(1))
            if len(line) + size > _MAX_LITERAL_SIZE:
                self._write(f"{tag} BAD Literal too large")
                return ""
            self._write("+ Ready for literal data")
            self.wfile.flush()
            data = self.rfile.read(size)
            rest, too_long = self._read()
            if len(data) < size or rest is None:
                return None
            if too_long:
                self._write(f"{tag} BAD Command line too long")
                return ""
            literal = _quote_literal(data.decode("utf-8", errors="replace"))
            line = line[: match.start()] + literal + rest.rstrip("\r\n")
        return line

    def handle(self):
        try:
            self._session()
        except TimeoutError:
            logger.info("client idle for %ss, closing connection", self.timeout)
            try:
                self._write("* BYE Autologout; idle for too long")
            except OSError:
                pass

    def _session(self):
        self._write("* OK IMAP4rev1 Service Ready")

        self.auth = False
        self.quota_error = None
        self.selected = None  # None, _READ_WRITE (SELECT) oder _READ_ONLY (EXAMINE)
        self.known_uids = []  # UIDs, die der Client nach dem letzten Bericht kennt

        while True:
            line = self._read_command()
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

            entry = _COMMANDS.get(cmd)
            if entry is None:
                logger.warning("unhandled command cmd=%s args=%r", cmd, args)
                self._write(f"{tag} BAD Unknown command")
                continue
            handler, state = entry
            if state != _ANY and not self.auth:
                self._write(f"{tag} NO Authenticate first")
                continue
            if state == _SELECTED and self.selected is None:
                self._write(f"{tag} BAD No mailbox selected")
                continue
            if handler(self, tag, cmd, args) is _CLOSE:
                return

    # --- Kommandos ohne eigene Logik -----------------------------------------
    # Alle Handler haben die Signatur (tag, cmd, args), siehe _COMMANDS.

    def _cmd_capability(self, tag, cmd, args):
        self._write("* CAPABILITY IMAP4rev1 AUTH=LOGIN")
        self._write(f"{tag} OK CAPABILITY completed")

    def _cmd_login(self, tag, cmd, args):
        try:
            a = shlex.split(args)
        except ValueError:
            a = []
        if len(a) != 2:
            self._write(f"{tag} BAD LOGIN syntax")
            return
        self._handle_login(tag, a[0], a[1])

    def _cmd_list(self, tag, cmd, args):
        self._write('* LIST (\\HasNoChildren) "/" "INBOX"')
        self._write(f"{tag} OK LIST completed")

    def _cmd_noop(self, tag, cmd, args):
        self._report_changes()
        self._write(f"{tag} OK NOOP completed")

    def _cmd_check(self, tag, cmd, args):
        self._report_changes()
        self._write(f"{tag} OK CHECK completed")

    def _cmd_close(self, tag, cmd, args):
        if self.selected == _READ_WRITE:
            self._expunge_deleted()  # CLOSE löscht ohne EXPUNGE-Antworten
        self.selected = None
        self.known_uids = []
        self._write(f"{tag} OK CLOSE completed")

    def _cmd_idle(self, tag, cmd, args):
        self._write("+ idling")
        self._read()  # liest das "DONE" des Clients
        self._write(f"{tag} OK IDLE terminated")

    def _cmd_logout(self, tag, cmd, args):
        self._write("* BYE IMAP server logging out")
        self._write(f"{tag} OK LOGOUT completed")
        return _CLOSE

    # --- Kommandos mit eigener Logik -----------------------------------------

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

    def _handle_select(self, tag, cmd):
        # Ein fehlgeschlagenes SELECT hebt die bisherige Auswahl auf (RFC 3501 §6.3.1).
        self.selected = None
        self.known_uids = []
        if self.quota_error:
            self._write(f"{tag} NO {_format_response_code(self.quota_error, 'OVERQUOTA', 'Quota exceeded')}")
            return

        mode = _READ_ONLY if cmd == "EXAMINE" else _READ_WRITE
        uids = storage.list_ids()
        first_unseen = next(
            (n for n, uid in enumerate(uids, start=1) if "\\Seen" not in storage.load_flags(uid)),
            None,
        )
        permanent = f"{_SYSTEM_FLAGS} \\*" if mode == _READ_WRITE else ""

        self._write(f"* FLAGS ({_SYSTEM_FLAGS})")
        self._write(f"* OK [PERMANENTFLAGS ({permanent})] Flags permitted")
        self._write(f"* {len(uids)} EXISTS")
        self._write("* 0 RECENT")
        if first_unseen is not None:
            self._write(f"* OK [UNSEEN {first_unseen}] First unseen")
        self._write(f"* OK [UIDVALIDITY {storage.uid_validity()}] UIDs valid")
        self._write(f"* OK [UIDNEXT {storage.next_id()}] Predicted next UID")
        self.selected = mode
        self.known_uids = uids
        self._write(f"{tag} OK [{mode}] {cmd} completed")

    def _report_changes(self):
        """Meldet Mails, die seit dem letzten Bericht verschwunden oder neu sind.

        Das ist der Grund, warum Clients NOOP schicken: neue Mails tauchen als
        ``* <n> EXISTS`` auf, extern gelöschte als ``* <n> EXPUNGE``.
        """
        if self.selected is None:
            return
        current = storage.list_ids()
        present = set(current)
        # Absteigend melden, dann bleiben die Sequenznummern davor gültig.
        for seq in range(len(self.known_uids), 0, -1):
            if self.known_uids[seq - 1] not in present:
                self._write(f"* {seq} EXPUNGE")
        remaining = sum(1 for uid in self.known_uids if uid in present)
        if len(current) > remaining:
            self._write(f"* {len(current)} EXISTS")
        self.known_uids = current

    def _expunge_deleted(self):
        """Löscht alle Mails mit \\Deleted, liefert die zu meldenden Sequenznummern.

        Jede EXPUNGE-Antwort verschiebt die folgenden Nummern sofort um eins
        (RFC 3501 §7.4.1), daher wird der Versatz mitgezählt.
        """
        uids = storage.list_ids()
        expunged = []
        kept = []
        for seq, uid in enumerate(uids, start=1):
            if "\\Deleted" in storage.load_flags(uid) and storage.delete_mail(uid):
                expunged.append(seq - len(expunged))
            else:
                kept.append(uid)
        self.known_uids = kept
        return expunged

    def _handle_expunge(self, tag):
        if self.selected == _READ_ONLY:
            self._write(f"{tag} NO Mailbox is read-only")
            return
        self._report_changes()  # Sequenznummern des Clients auf den aktuellen Stand bringen
        for seq in self._expunge_deleted():
            self._write(f"* {seq} EXPUNGE")
        self._write(f"{tag} OK EXPUNGE completed")

    def _handle_status(self, tag, args):
        paren_start = args.find("(")
        paren_end = args.rfind(")")
        if paren_start == -1 or paren_end == -1 or paren_end < paren_start:
            self._write(f"{tag} BAD STATUS syntax")
            return

        mailbox = args[:paren_start].strip().strip('"') or "INBOX"
        items = args[paren_start + 1 : paren_end].split()

        uids = storage.list_ids()
        unseen = sum(1 for uid in uids if "\\Seen" not in storage.load_flags(uid))
        values = {
            "MESSAGES": len(uids),
            "RECENT": 0,
            "UIDNEXT": storage.next_id(),
            "UIDVALIDITY": storage.uid_validity(),
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
            self._handle_fetch(tag, parts[1], is_uid=True)
        elif sub == "SEARCH":
            self._handle_search(tag, parts[1] if len(parts) > 1 else "", is_uid=True)
        elif sub == "STORE":
            if len(parts) < 2:
                self._write(f"{tag} BAD UID STORE syntax")
                return
            self._handle_store(tag, parts[1], is_uid=True)
        else:
            logger.warning("unhandled UID subcommand sub=%s args=%r", sub, args)
            self._write(f"{tag} BAD UID command not supported")

    def _handle_fetch(self, tag, args, is_uid=False):
        fields = args.split(None, 1)
        if len(fields) < 2 or not _SEQ_SET_RE.match(fields[0]):
            self._write(f"{tag} BAD FETCH syntax")
            return
        try:
            items = imap_fetch.parse_items(fields[1])
        except ValueError as exc:
            self._write(f"{tag} BAD FETCH {exc}")
            return

        mark_seen = self.selected != _READ_ONLY and imap_fetch.marks_seen(items)
        log_content = imap_fetch.delivers_content(items)

        for seq, msg_id in _resolve_messages(fields[0], storage.list_ids(), is_uid):
            flags = storage.load_flags(msg_id)
            flags_changed = mark_seen and "\\Seen" not in flags
            if flags_changed:
                flags.append("\\Seen")
                storage.save_flags(msg_id, flags)

            message = imap_fetch.StoredMessage(msg_id)
            self.wfile.write(imap_fetch.render(seq, message, flags, items, include_uid=is_uid, include_flags=flags_changed))

            if log_content:
                headers = storage.parse_lenient(message.raw)
                logger.info(
                    "outgoing id=%s from=%s to=%s subject=%r",
                    msg_id,
                    headers.get("From", "NIL"),
                    headers.get("To", "NIL"),
                    headers.get("Subject", ""),
                )
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
        if self.selected == _READ_ONLY:
            self._write(f"{tag} NO Mailbox is read-only")
            return

        for seq, msg_id in _resolve_messages(seq_set, storage.list_ids(), is_uid):
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
                    self._write(f"* {seq} FETCH (FLAGS ({' '.join(current)}) UID {msg_id})")
                else:
                    self._write(f"* {seq} FETCH (FLAGS ({' '.join(current)}))")
        self._write(f"{tag} OK STORE completed")

    def _handle_search(self, tag, args="", is_uid=False):
        """Wertet eine Teilmenge der RFC-3501-SEARCH-Kriterien aus.

        Unterstützt: ALL, (UN)SEEN, (UN)ANSWERED, (UN)DELETED, (UN)FLAGGED,
        (UN)DRAFT, NEW, OLD, RECENT, UID <seq-set>, SUBJECT/FROM/TO/CC/BCC/
        BODY/TEXT <string> sowie nackte Sequence-Sets. Mehrere Kriterien
        werden UND-verknüpft. NOT/OR/Datumskriterien werden nicht ausgewertet
        (unbekannte Tokens filtern nicht, statt einen Fehler zu werfen).

        Gefiltert wird auf (Sequenznummer, UID)-Paaren. Die Antwort enthält
        Sequenznummern, bei ``UID SEARCH`` die UIDs.
        """
        uids = storage.list_ids()
        ids = list(enumerate(uids, start=1))

        tokens = _tokenize_search(args)
        i = 0
        while i < len(tokens):
            key = tokens[i].upper()
            if key == "ALL":
                i += 1
            elif key in _FLAG_SEARCH_KEYS:
                flag = _FLAG_SEARCH_KEYS[key]
                ids = [m for m in ids if flag in storage.load_flags(m[1])]
                i += 1
            elif key.startswith("UN") and key[2:] in _FLAG_SEARCH_KEYS:
                flag = _FLAG_SEARCH_KEYS[key[2:]]
                ids = [m for m in ids if flag not in storage.load_flags(m[1])]
                i += 1
            elif key == "NEW":
                ids = [m for m in ids if "\\Seen" not in storage.load_flags(m[1])]
                i += 1
            elif key == "OLD":
                i += 1  # kein \Recent-Tracking -> jede Mail gilt als "old"
            elif key == "RECENT":
                ids = []  # kein \Recent-Tracking -> nie ein Treffer
                i += 1
            elif key == "UID" and i + 1 < len(tokens):
                wanted = {uid for _, uid in _resolve_messages(tokens[i + 1], uids, is_uid=True)}
                ids = [m for m in ids if m[1] in wanted]
                i += 2
            elif key in _TEXT_SEARCH_KEYS or key in ("BODY", "TEXT"):
                if i + 1 >= len(tokens):
                    i += 1
                    continue
                needle = tokens[i + 1].lower()
                ids = [m for m in ids if _search_text_matches(m[1], key, needle)]
                i += 2
            else:
                seq = set(_parse_seq_set(tokens[i], len(uids)))
                ids = [m for m in ids if m[0] in seq] if seq else ids
                i += 1

        self._write("* SEARCH " + " ".join(str(uid if is_uid else seq) for seq, uid in ids))
        self._write(f"{tag} OK SEARCH completed")


_COMMANDS = {
    "CAPABILITY": (IMAPHandler._cmd_capability, _ANY),
    "LOGIN": (IMAPHandler._cmd_login, _ANY),
    "LIST": (IMAPHandler._cmd_list, _ANY),
    "NOOP": (IMAPHandler._cmd_noop, _ANY),
    "LOGOUT": (IMAPHandler._cmd_logout, _ANY),
    "SELECT": (lambda h, tag, cmd, args: h._handle_select(tag, cmd), _AUTH),
    "EXAMINE": (lambda h, tag, cmd, args: h._handle_select(tag, cmd), _AUTH),
    "STATUS": (lambda h, tag, cmd, args: h._handle_status(tag, args), _AUTH),
    "UID": (lambda h, tag, cmd, args: h._handle_uid(tag, args), _AUTH),
    "FETCH": (lambda h, tag, cmd, args: h._handle_fetch(tag, args), _AUTH),
    "STORE": (lambda h, tag, cmd, args: h._handle_store(tag, args), _AUTH),
    "SEARCH": (lambda h, tag, cmd, args: h._handle_search(tag, args), _AUTH),
    "IDLE": (IMAPHandler._cmd_idle, _AUTH),
    "CHECK": (IMAPHandler._cmd_check, _SELECTED),
    "CLOSE": (IMAPHandler._cmd_close, _SELECTED),
    "EXPUNGE": (lambda h, tag, cmd, args: h._handle_expunge(tag), _SELECTED),
}


class IMAPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_imap(host=IMAP_HOST, port=IMAP_PORT):
    return IMAPServer((host, port), IMAPHandler)
