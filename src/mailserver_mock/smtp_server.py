"""Minimaler SMTP-Mock: EHLO/HELO, AUTH LOGIN/PLAIN, MAIL/RCPT/DATA, RSET, NOOP, QUIT."""
import base64
import binascii
import logging
import os
import socketserver
import time
from email import message_from_string

from . import storage, users

SMTP_HOST = os.environ.get("SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "1025"))

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


class SMTPHandler(socketserver.StreamRequestHandler):
    def _write(self, msg):
        self.wfile.write((msg + "\r\n").encode("utf-8"))

    def _read(self):
        line = self.rfile.readline()
        if not line:
            return None
        return line.decode("utf-8", errors="replace")

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
        self._write("220 localhost ESMTP ready")

        auth = False
        quota_error = None
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
                    if quota_error:
                        self._write(_format_error(quota_error, "552", "5.2.2", "Mailbox full, quota exceeded"))
                    else:
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
                        self._write("250 Message accepted")
                    data_lines = []
                    data_mode = False
                    mail_from = ""
                    rcpt_to = []
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
                logger.warning("unhandled command line=%r", cmd)
                self._write("502 Command not implemented")


class SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_smtp(host=SMTP_HOST, port=SMTP_PORT):
    return SMTPServer((host, port), SMTPHandler)
