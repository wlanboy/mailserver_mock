"""Minimaler SMTP-Mock: EHLO/HELO, AUTH LOGIN/PLAIN, MAIL/RCPT/DATA, RSET, NOOP, QUIT."""
import base64
import binascii
import logging
import os
import socketserver
from email import message_from_string

from . import storage

SMTP_HOST = os.environ.get("SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "1025"))

USER = os.environ.get("MAIL_USER", "testuser")
PASS = os.environ.get("MAIL_PASS", "testpass")

logger = logging.getLogger(__name__)


def _b64decode(s):
    try:
        return base64.b64decode(s).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return ""


class SMTPHandler(socketserver.StreamRequestHandler):
    def _write(self, msg):
        self.wfile.write((msg + "\r\n").encode("utf-8"))

    def _read(self):
        line = self.rfile.readline()
        if not line:
            return None
        return line.decode("utf-8", errors="replace")

    def handle(self):
        self._write("220 localhost ESMTP ready")

        auth = False
        mail_from = ""
        rcpt_to = []
        data_mode = False
        data_lines = []

        while True:
            line = self._read()
            if line is None:
                return
            cmd = line.strip()
            if cmd == "":
                continue
            upper = cmd.upper()

            if data_mode:
                if cmd == ".":
                    msg_id = storage.save_mail(mail_from, rcpt_to, "".join(data_lines))
                    raw = storage.load_mail(msg_id)
                    headers = message_from_string(raw)
                    logger.info(
                        "incoming id=%s from=%s to=%s subject=%r",
                        msg_id,
                        headers.get("From", mail_from),
                        headers.get("To", ", ".join(rcpt_to)),
                        headers.get("Subject", ""),
                    )
                    data_lines = []
                    data_mode = False
                    mail_from = ""
                    rcpt_to = []
                    self._write("250 Message accepted")
                else:
                    # RFC 5321 Dot-Stuffing: hebt das führende-Punkt-Escaping des Clients wieder auf.
                    stripped = cmd[1:] if cmd.startswith(".") else cmd
                    data_lines.append(stripped + "\n")
                continue

            if upper.startswith("EHLO"):
                self._write("250-localhost greets you")
                self._write("250-AUTH LOGIN PLAIN")
                self._write("250 OK")

            elif upper.startswith("HELO"):
                self._write("250 localhost")

            elif upper == "NOOP":
                self._write("250 OK")

            elif upper == "RSET":
                mail_from = ""
                rcpt_to = []
                data_lines = []
                data_mode = False
                self._write("250 OK")

            elif upper.startswith("AUTH LOGIN"):
                self._write("334 VXNlcm5hbWU6")  # Benutzername:
                u64 = self._read()
                if u64 is None:
                    return
                user = _b64decode(u64.strip())

                self._write("334 UGFzc3dvcmQ6")  # Passwort:
                p64 = self._read()
                if p64 is None:
                    return
                password = _b64decode(p64.strip())

                if user == USER and password == PASS:
                    auth = True
                    self._write("235 Authentication successful")
                else:
                    self._write("535 Authentication failed")

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
                    initial_response = resp.strip()
                else:
                    self._write("501 Syntax error")
                    continue

                decoded = _b64decode(initial_response)
                fields = decoded.split("\x00")
                if len(fields) == 3 and fields[1] == USER and fields[2] == PASS:
                    auth = True
                    self._write("235 Authentication successful")
                else:
                    self._write("535 Authentication failed")

            elif upper.startswith("MAIL FROM:"):
                if not auth:
                    self._write("530 Authentication required")
                    continue
                mail_from = cmd[len("MAIL FROM:"):]
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

            elif upper == "QUIT":
                self._write("221 Bye")
                return

            else:
                self._write("502 Command not implemented")


class SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_smtp(host=SMTP_HOST, port=SMTP_PORT):
    return SMTPServer((host, port), SMTPHandler)
