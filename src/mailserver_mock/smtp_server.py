"""Minimaler SMTP-Mock: EHLO/HELO, AUTH LOGIN/PLAIN, MAIL/RCPT/DATA, RSET, NOOP, QUIT."""
import base64
import binascii
import logging
import os
import socketserver
import time

from . import connection, storage, users

SMTP_HOST = os.environ.get("SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "1025"))
# RFC 5321 §4.5.3.2 empfiehlt mindestens 5 Minuten pro Kommando bzw. DATA-Block.
SMTP_TIMEOUT = connection.env_timeout("SMTP_TIMEOUT", "300")
# Größte angenommene Mail in Bytes (nach Dot-Unstuffing), wird per SIZE angekündigt.
SMTP_MAX_MESSAGE_SIZE = int(os.environ.get("SMTP_MAX_MESSAGE_SIZE", str(10 * 1024 * 1024)))

# DATA wird in Stücken dieser Größe gelesen, damit überlange Zeilen nicht
# komplett im Speicher landen, bevor die Größenprüfung greift.
_DATA_CHUNK = 64 * 1024
_SIZE_EXCEEDED = "552 5.3.4 Message size exceeds fixed maximum message size"

logger = logging.getLogger(__name__)


def _b64decode(s):
    try:
        return base64.b64decode(s).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return ""


def _format_error(cfg, default_code, default_enhanced, default_message):
    code = cfg.get("code", default_code)
    enhanced = cfg.get("enhanced_code", default_enhanced)
    message = cfg.get("message", default_message)
    return f"{code} {enhanced} {message}".strip()


def _parse_mail_from(arg):
    """Trennt ``<adresse> SIZE=n ...`` in Adresse und angekündigte Größe (oder None)."""
    arg = arg.strip()
    if arg.startswith("<") and ">" in arg:
        end = arg.index(">") + 1
        address, params = arg[:end], arg[end:]
    else:
        address, _, params = arg.partition(" ")
    size = None
    for param in params.split():
        key, _, value = param.partition("=")
        if key.upper() == "SIZE" and value.isdigit():
            size = int(value)
    return address, size


class SMTPHandler(socketserver.StreamRequestHandler):
    # setup() setzt damit settimeout() auf dem Client-Socket.
    timeout = SMTP_TIMEOUT

    def _write(self, msg):
        self.wfile.write((msg + "\r\n").encode("utf-8"))

    def _read_raw(self):
        """Liest eine Kommandozeile; zu lange Zeilen werden mit 500 abgelehnt und als b"" geliefert."""
        line, too_long = connection.read_line(self.rfile)
        if too_long:
            self._write("500 5.5.2 Line too long")
            return b""
        return line

    def _read(self):
        """Wie _read_raw, aber dekodiert; "" bedeutet zu lange Zeile (500 schon gesendet)."""
        line = self._read_raw()
        return None if line is None else line.decode("utf-8", errors="replace")

    def _read_data_chunk(self):
        """Liest im DATA-Modus bis zum Zeilenende, aber höchstens _DATA_CHUNK Bytes.

        Endet ein Stück mitten in einem CRLF, wird das LF noch nachgelesen, damit
        das Zeilenende nicht auf zwei Stücke verteilt ist.
        """
        chunk = self.rfile.readline(_DATA_CHUNK)
        if chunk.endswith(b"\r"):
            chunk += self.rfile.readline(1)
        return chunk or None

    def _login(self, username, password):
        """Prüft Zugangsdaten gegen users.json und wendet Test-Fehlerverhalten an.

        Rückgabe: (auth_ok, quota_error_config, close_connection).
        """
        account = users.find_user(username)
        if account is None or account.get("password") != password:
            self._write("535 Authentication failed")
            return False, None, False

        behavior = account.get("behavior", "normal")
        if behavior == "normal":
            self._write("235 Authentication successful")
            return True, None, False

        if behavior == "quota":
            self._write("235 Authentication successful")
            return True, account.get("smtp", {}), False

        # too_many / timeout: verzögerte temporäre Fehlerantwort, danach Verbindungsabbau
        time.sleep(float(account.get("delay_seconds", 0)))
        message = _format_error(
            account.get("smtp", {}), "421", "4.7.0", "Service not available, closing transmission channel"
        )
        self._write(message)
        return False, None, True

    def handle(self):
        try:
            self._session()
        except TimeoutError:
            logger.info("client idle for %ss, closing connection", self.timeout)
            try:
                self._write("421 4.4.2 localhost Error: timeout exceeded")
            except OSError:
                pass

    def _session(self):
        self._write("220 localhost ESMTP ready")

        auth = False
        quota_error = None
        mail_from = ""
        rcpt_to = []
        data_mode = False
        data_lines = []
        data_size = 0
        at_line_start = True

        while True:
            if data_mode:
                raw_line = self._read_data_chunk()
                if raw_line is None:
                    return
                # Im DATA-Modus die Rohbytes verarbeiten: nur das Zeilenende abschneiden.
                # Leerzeilen, Einrückungen, Leerzeichen am Zeilenende und 8-Bit-Zeichen
                # in beliebigem Charset bleiben erhalten. Ein Stück ohne Zeilenende ist
                # der Anfang einer überlangen Zeile, der Rest folgt im nächsten Stück.
                complete = raw_line.endswith(b"\n")
                if raw_line.endswith(b"\r\n"):
                    data_line = raw_line[:-2]
                elif complete:
                    data_line = raw_line[:-1]
                else:
                    data_line = raw_line
                line_start, at_line_start = at_line_start, complete
                if line_start and complete and data_line == b".":
                    if data_size > SMTP_MAX_MESSAGE_SIZE:
                        logger.warning("rejected message: size exceeds %d bytes", SMTP_MAX_MESSAGE_SIZE)
                        self._write(_SIZE_EXCEEDED)
                    elif quota_error:
                        self._write(_format_error(quota_error, "552", "5.2.2", "Mailbox full, quota exceeded"))
                    else:
                        msg_id = storage.save_mail(mail_from, rcpt_to, b"".join(data_lines))
                        headers = storage.parse_lenient(storage.load_mail(msg_id))
                        logger.info(
                            "incoming id=%s from=%s to=%s subject=%r",
                            msg_id,
                            headers.get("From", mail_from),
                            headers.get("To", ", ".join(rcpt_to)),
                            headers.get("Subject", ""),
                        )
                        self._write("250 Message accepted")
                    data_lines = []
                    data_size = 0
                    data_mode = False
                    mail_from = ""
                    rcpt_to = []
                else:
                    # RFC 5321 Dot-Stuffing: hebt das führende-Punkt-Escaping des Clients wieder auf.
                    if line_start and data_line.startswith(b"."):
                        data_line = data_line[1:]
                    piece = data_line + b"\r\n" if complete else data_line
                    data_size += len(piece)
                    if data_size > SMTP_MAX_MESSAGE_SIZE:
                        # Weiterlesen bis zum Ende-Punkt, aber nichts mehr puffern.
                        data_lines = []
                    else:
                        data_lines.append(piece)
                continue

            raw_line = self._read_raw()
            if raw_line is None:
                return
            cmd = raw_line.decode("utf-8", errors="replace").strip()
            if cmd == "":
                continue
            upper = cmd.upper()

            if upper.startswith("EHLO"):
                self._write("250-localhost greets you")
                self._write("250-AUTH LOGIN PLAIN")
                self._write(f"250-SIZE {SMTP_MAX_MESSAGE_SIZE}")
                self._write("250 OK")

            elif upper.startswith("HELO"):
                self._write("250 localhost")

            elif upper == "NOOP":
                self._write("250 OK")

            elif upper == "RSET":
                mail_from = ""
                rcpt_to = []
                self._write("250 OK")

            elif upper.startswith("AUTH LOGIN"):
                self._write("334 VXNlcm5hbWU6")  # Benutzername:
                u64 = self._read()
                if u64 is None:
                    return
                if u64 == "":  # zu lang, 500 ist schon raus
                    continue
                user = _b64decode(u64.strip())

                self._write("334 UGFzc3dvcmQ6")  # Passwort:
                p64 = self._read()
                if p64 is None:
                    return
                if p64 == "":  # zu lang, 500 ist schon raus
                    continue
                password = _b64decode(p64.strip())

                auth, quota_error, close = self._login(user, password)
                if close:
                    return

            elif upper.startswith("AUTH PLAIN"):
                parts = cmd.split(" ")
                if len(parts) == 3:
                    initial_response = parts[2]
                elif len(parts) == 2:
                    # SASL continuation: no initial response, ask for it.
                    self._write("334 ")
                    resp = self._read()
                    if resp is None:
                        return
                    if resp == "":  # zu lang, 500 ist schon raus
                        continue
                    initial_response = resp.strip()
                else:
                    self._write("501 Syntax error")
                    continue

                decoded = _b64decode(initial_response)
                fields = decoded.split("\x00")
                if len(fields) != 3:
                    self._write("535 Authentication failed")
                    continue

                auth, quota_error, close = self._login(fields[1], fields[2])
                if close:
                    return

            elif upper.startswith("MAIL FROM:"):
                if not auth:
                    self._write("530 Authentication required")
                    continue
                address, size = _parse_mail_from(cmd[len("MAIL FROM:"):])
                if size is not None and size > SMTP_MAX_MESSAGE_SIZE:
                    self._write(_SIZE_EXCEEDED)
                    continue
                mail_from = address
                rcpt_to = []
                self._write("250 OK")

            elif upper.startswith("RCPT TO:"):
                if not auth:
                    self._write("530 Authentication required")
                    continue
                rcpt_to.append(cmd[len("RCPT TO:"):])
                self._write("250 OK")

            elif upper == "DATA":
                if not auth:
                    self._write("530 Authentication required")
                    continue
                if mail_from == "":
                    self._write("503 Bad sequence of commands: MAIL FROM required")
                    continue
                if not rcpt_to:
                    self._write("503 Bad sequence of commands: RCPT TO required")
                    continue
                self._write("354 End data with <CR><LF>.<CR><LF>")
                data_mode = True
                data_lines = []
                data_size = 0
                at_line_start = True

            elif upper == "QUIT":
                self._write("221 Bye")
                return

            else:
                logger.warning("unhandled command line=%r", cmd)
                self._write("502 Command not implemented")


class SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_smtp(host=SMTP_HOST, port=SMTP_PORT):
    return SMTPServer((host, port), SMTPHandler)
