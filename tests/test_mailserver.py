"""End-to-End-Socket-Tests für den SMTP- + IMAP-Mock, die sich an der
gosmtp-Referenz-Testsuite (smtp_test.go / imap_test.go) orientieren, damit
das Verhalten zwischen beiden Implementierungen vergleichbar bleibt.
"""
import base64
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("MAIL_DIR", tempfile.mkdtemp(prefix="mailserver_mock_tests_"))

from mailserver_mock import server, storage  # noqa: E402
from mailserver_mock.imap_server import IMAP_HOST, IMAP_PORT  # noqa: E402
from mailserver_mock.smtp_server import SMTP_HOST, SMTP_PORT  # noqa: E402


def setUpModule():
    server.start_mail_server()
    time.sleep(0.2)


def _reset_mails():
    for p in storage.MAIL_DIR.glob("*"):
        p.unlink()


###############################################################################
# Low-Level-Socket-Hilfsfunktionen
###############################################################################

def _dial(addr):
    sock = socket.create_connection(addr, timeout=5)
    return sock, sock.makefile("rb"), sock.makefile("wb")


def _read(r):
    line = r.readline()
    return line.decode("utf-8", errors="replace").strip()


def _write(w, s):
    w.write((s + "\r\n").encode("utf-8"))
    w.flush()


###############################################################################
# Storage-Tests
###############################################################################

class StorageConcurrencyTests(unittest.TestCase):
    def test_concurrent_save_mail_assigns_unique_ids(self):
        """Regressionstest: next_id()+Schreiben liefen früher ohne Lock ab, wodurch
        parallele Sessions dieselbe ID berechnen und sich gegenseitig überschreiben
        konnten (ThreadingTCPServer bedient Verbindungen in eigenen Threads)."""
        _reset_mails()
        n = 20
        barrier = threading.Barrier(n)

        def send(i):
            barrier.wait()
            storage.save_mail(f"<sender{i}@example.com>", [f"<rcpt{i}@example.com>"], f"body {i}")

        threads = [threading.Thread(target=send, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(storage.count_mails(), n)


###############################################################################
# SMTP-Hilfsfunktionen
###############################################################################

def _smtp_login(r, w):
    _read(r)  # 220 Banner
    _write(w, "EHLO localhost")
    _read(r)  # 250-localhost greets you (Begrüßung)
    _read(r)  # 250-AUTH LOGIN PLAIN
    _read(r)  # 250 OK
    _write(w, "AUTH LOGIN")
    _read(r)  # 334 Benutzername:
    _write(w, base64.b64encode(b"testuser").decode())
    _read(r)  # 334 Passwort:
    _write(w, base64.b64encode(b"testpass").decode())
    resp = _read(r)
    assert "235" in resp, resp


def _smtp_auth_login(r, w, username, password):
    """Führt EHLO + AUTH LOGIN mit beliebigen Zugangsdaten aus und gibt die
    finale Server-Antwort zurück (ohne Erfolg vorauszusetzen)."""
    _read(r)  # 220 Banner
    _write(w, "EHLO localhost")
    _read(r)
    _read(r)
    _read(r)
    _write(w, "AUTH LOGIN")
    _read(r)  # 334 Benutzername:
    _write(w, base64.b64encode(username.encode()).decode())
    _read(r)  # 334 Passwort:
    _write(w, base64.b64encode(password.encode()).decode())
    return _read(r)


def _smtp_send_mail(from_addr, to_addr, body):
    sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
    try:
        _smtp_login(r, w)
        _write(w, f"MAIL FROM:{from_addr}")
        _read(r)
        _write(w, f"RCPT TO:{to_addr}")
        _read(r)
        _write(w, "DATA")
        _read(r)
        _write(w, body)
        _write(w, ".")
        resp = _read(r)
        assert "250" in resp, resp
    finally:
        sock.close()


###############################################################################
# SMTP-Tests
###############################################################################

class SMTPTests(unittest.TestCase):
    def test_happy_path(self):
        _reset_mails()
        _smtp_send_mail("<sender@example.com>", "<rcpt@example.com>", "Hello World")
        self.assertEqual(storage.count_mails(), 1)

    def test_auth_fail(self):
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _read(r)
            _write(w, "AUTH LOGIN")
            _read(r)
            _write(w, base64.b64encode(b"wrong").decode())
            _read(r)
            _write(w, base64.b64encode(b"wrong").decode())
            resp = _read(r)
            self.assertIn("535", resp)
        finally:
            sock.close()

    def test_auth_plain(self):
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _read(r)
            creds = base64.b64encode(b"\x00testuser\x00testpass").decode()
            _write(w, f"AUTH PLAIN {creds}")
            resp = _read(r)
            self.assertIn("235", resp)
        finally:
            sock.close()

    def test_auth_plain_sasl_continuation(self):
        # SASL-Continuation-Form ohne Initial-Response, wie sie z.B. Jakarta
        # Mail / Spring Boot Clients standardmäßig senden.
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _read(r)
            _write(w, "AUTH PLAIN")
            resp = _read(r)
            self.assertIn("334", resp)
            creds = base64.b64encode(b"\x00testuser\x00testpass").decode()
            _write(w, creds)
            resp = _read(r)
            self.assertIn("235", resp)
        finally:
            sock.close()

    def test_helo(self):
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _read(r)
            _write(w, "HELO localhost")
            resp = _read(r)
            self.assertIn("250", resp)
        finally:
            sock.close()

    def test_noop(self):
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _read(r)
            _write(w, "NOOP")
            resp = _read(r)
            self.assertIn("250", resp)
        finally:
            sock.close()

    def test_rset(self):
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _smtp_login(r, w)
            _write(w, "MAIL FROM:<a@b>")
            _read(r)
            _write(w, "RSET")
            resp = _read(r)
            self.assertIn("250", resp)

            # Nach RSET muss DATA fehlschlagen, da MAIL FROM zurückgesetzt wurde.
            _write(w, "DATA")
            resp = _read(r)
            self.assertIn("503", resp)
        finally:
            sock.close()

    def test_quit(self):
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _read(r)
            _write(w, "QUIT")
            resp = _read(r)
            self.assertIn("221", resp)
        finally:
            sock.close()

    def test_unknown_command(self):
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _read(r)
            _write(w, "XYZZY")
            resp = _read(r)
            self.assertIn("502", resp)
        finally:
            sock.close()

    def test_no_auth_mail_from(self):
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _read(r)
            _write(w, "MAIL FROM:<a@b>")
            resp = _read(r)
            self.assertIn("530", resp)
        finally:
            sock.close()

    def test_no_auth_rcpt_to(self):
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _read(r)
            _write(w, "RCPT TO:<a@b>")
            resp = _read(r)
            self.assertIn("530", resp)
        finally:
            sock.close()

    def test_data_without_mail_from(self):
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _smtp_login(r, w)
            _write(w, "DATA")
            resp = _read(r)
            self.assertIn("503", resp)
        finally:
            sock.close()

    def test_data_without_rcpt_to(self):
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _smtp_login(r, w)
            _write(w, "MAIL FROM:<a@b>")
            _read(r)
            _write(w, "DATA")
            resp = _read(r)
            self.assertIn("503", resp)
        finally:
            sock.close()

    def test_multiple_recipients(self):
        _reset_mails()
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _smtp_login(r, w)
            _write(w, "MAIL FROM:<sender@example.com>")
            _read(r)
            for rcpt in ("<alice@example.com>", "<bob@example.com>", "<carol@example.com>"):
                _write(w, f"RCPT TO:{rcpt}")
                _read(r)
            _write(w, "DATA")
            _read(r)
            _write(w, "Hello three recipients")
            _write(w, ".")
            resp = _read(r)
            self.assertIn("250", resp)
            self.assertEqual(storage.count_mails(), 1)
        finally:
            sock.close()

    def test_dot_stuffing(self):
        _reset_mails()
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _smtp_login(r, w)
            _write(w, "MAIL FROM:<a@b>")
            _read(r)
            _write(w, "RCPT TO:<c@d>")
            _read(r)
            _write(w, "DATA")
            _read(r)

            # RFC 5321: ein führender Punkt wird vom Client mit einem zusätzlichen Punkt escaped.
            _write(w, "..dotline")
            _write(w, "normal line")
            _write(w, ".")
            _read(r)  # 250 Message accepted (Nachricht angenommen)

            content = storage.load_mail(1)
            self.assertIn(".dotline", content)
            self.assertNotIn("..dotline", content)
        finally:
            sock.close()

    def test_two_mails_in_one_session(self):
        _reset_mails()
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            _smtp_login(r, w)
            for from_addr, rcpt, body in (
                ("<first@example.com>", "<rcpt1@example.com>", "First mail body"),
                ("<second@example.com>", "<rcpt2@example.com>", "Second mail body"),
            ):
                _write(w, f"MAIL FROM:{from_addr}")
                _read(r)
                _write(w, f"RCPT TO:{rcpt}")
                _read(r)
                _write(w, "DATA")
                _read(r)
                _write(w, body)
                _write(w, ".")
                _read(r)
            self.assertEqual(storage.count_mails(), 2)
        finally:
            sock.close()

    def test_too_many_login_delay_and_error(self):
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            start = time.time()
            resp = _smtp_auth_login(r, w, "testuser_421", "testpass_421")
            elapsed = time.time() - start
            self.assertIn("421", resp)
            self.assertIn("4.7.0", resp)
            self.assertGreaterEqual(elapsed, 0.9)
        finally:
            sock.close()

    def test_timeout_login_error(self):
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            with patch("mailserver_mock.smtp_server.time.sleep"):
                resp = _smtp_auth_login(r, w, "testuser_451", "testpass_451")
            self.assertIn("451", resp)
            self.assertIn("4.4.2", resp)
        finally:
            sock.close()

    def test_quota_error_on_data(self):
        _reset_mails()
        sock, r, w = _dial((SMTP_HOST, SMTP_PORT))
        try:
            resp = _smtp_auth_login(r, w, "testuser_552", "testpass_552")
            self.assertIn("235", resp)
            _write(w, "MAIL FROM:<a@b>")
            _read(r)
            _write(w, "RCPT TO:<c@d>")
            _read(r)
            _write(w, "DATA")
            _read(r)
            _write(w, "quota test body")
            _write(w, ".")
            resp = _read(r)
            self.assertIn("552", resp)
            self.assertIn("5.2.2", resp)
            self.assertEqual(storage.count_mails(), 0)
        finally:
            sock.close()


###############################################################################
# IMAP-Hilfsfunktionen
###############################################################################

def _imap_login(r, w):
    _read(r)  # * OK IMAP4rev1 Service Ready (Server bereit)
    _write(w, "A1 LOGIN testuser testpass")
    resp = _read(r)
    assert "OK" in resp, resp


def _imap_select(r, w, tag):
    _write(w, f"{tag} SELECT INBOX")
    _read(r)  # * N EXISTS (Anzahl Nachrichten)
    _read(r)  # * FLAGS (...)
    _read(r)  # * OK [PERMANENTFLAGS ...]
    resp = _read(r)
    assert "OK" in resp, resp


def _imap_read_until(r, tag):
    lines = []
    while True:
        line = _read(r)
        lines.append(line)
        if line.startswith(tag):
            return lines


###############################################################################
# IMAP-Tests
###############################################################################

class IMAPTests(unittest.TestCase):
    def test_login(self):
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
        finally:
            sock.close()

    def test_login_fail(self):
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _read(r)
            _write(w, "A1 LOGIN wrong wrong")
            resp = _read(r)
            self.assertIn("NO", resp)
        finally:
            sock.close()

    def test_list(self):
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _write(w, 'A2 LIST "" "*"')
            list_line = _read(r)
            resp = _read(r)
            self.assertIn("INBOX", list_line)
            self.assertIn("OK", resp)
        finally:
            sock.close()

    def test_select(self):
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _imap_select(r, w, "A2")
        finally:
            sock.close()

    def test_select_without_auth(self):
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _read(r)
            _write(w, "A1 SELECT INBOX")
            resp = _read(r)
            self.assertIn("NO", resp)
        finally:
            sock.close()

    def test_search(self):
        _reset_mails()
        _smtp_send_mail("<a@b>", "<c@d>", "search test body")
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _imap_select(r, w, "A2")
            _write(w, "A3 SEARCH ALL")
            search_line = _read(r)
            resp = _read(r)
            self.assertIn("SEARCH", search_line)
            self.assertIn("1", search_line)
            self.assertIn("OK", resp)
        finally:
            sock.close()

    def test_fetch(self):
        _reset_mails()
        _smtp_send_mail("<sender@example.com>", "<rcpt@example.com>", "fetch test body")
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _imap_select(r, w, "A2")
            _write(w, "A3 FETCH 1 (FLAGS BODY[])")
            lines = _imap_read_until(r, "A3")
            self.assertTrue(any("FETCH" in line for line in lines))
            self.assertIn("OK", lines[-1])
        finally:
            sock.close()

    def test_store(self):
        _reset_mails()
        _smtp_send_mail("<a@b>", "<c@d>", "store test body")
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _imap_select(r, w, "A2")
            _write(w, r"A3 STORE 1 +FLAGS (\Seen)")
            fetch_line = _read(r)  # * 1 FETCH (FLAGS (\Seen)) (aktuelle Flags)
            resp = _read(r)
            self.assertIn(r"\Seen", fetch_line)
            self.assertIn("OK", resp)

            flags = storage.load_flags(1)
            self.assertIn(r"\Seen", flags)
        finally:
            sock.close()

    def test_uid_fetch(self):
        _reset_mails()
        _smtp_send_mail("<a@b>", "<c@d>", "uid fetch body")
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _imap_select(r, w, "A2")
            _write(w, "A3 UID FETCH 1 (FLAGS BODY[])")
            lines = _imap_read_until(r, "A3")
            self.assertIn("OK", lines[-1])
        finally:
            sock.close()

    def test_idle(self):
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _write(w, "A2 IDLE")
            resp = _read(r)
            self.assertIn("+ idling", resp)
            _write(w, "DONE")
            resp = _read(r)
            self.assertIn("OK", resp)
        finally:
            sock.close()

    def test_logout(self):
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _read(r)
            _write(w, "A1 LOGOUT")
            _read(r)  # * BYE (Verbindung wird beendet)
            resp = _read(r)
            self.assertIn("OK", resp)
        finally:
            sock.close()

    def test_unknown_command(self):
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _write(w, "A2 XYZZY")
            resp = _read(r)
            self.assertIn("BAD", resp)
        finally:
            sock.close()

    def test_login_too_many(self):
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _read(r)
            start = time.time()
            _write(w, "A1 LOGIN testuser_421 testpass_421")
            resp = _read(r)
            elapsed = time.time() - start
            self.assertIn("NO", resp)
            self.assertIn("LIMIT", resp)
            self.assertGreaterEqual(elapsed, 0.9)
        finally:
            sock.close()

    def test_login_timeout(self):
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _read(r)
            with patch("mailserver_mock.imap_server.time.sleep"):
                _write(w, "A1 LOGIN testuser_451 testpass_451")
                resp = _read(r)
            self.assertIn("NO", resp)
            self.assertIn("UNAVAILABLE", resp)
        finally:
            sock.close()

    def test_login_quota_then_select(self):
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _read(r)
            _write(w, "A1 LOGIN testuser_552 testpass_552")
            resp = _read(r)
            self.assertIn("OK", resp)
            _write(w, "A2 SELECT INBOX")
            resp = _read(r)
            self.assertIn("NO", resp)
            self.assertIn("OVERQUOTA", resp)
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main()
