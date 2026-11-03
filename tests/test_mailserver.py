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
from typing import Any
from unittest.mock import patch

os.environ.setdefault("MAIL_DIR", tempfile.mkdtemp(prefix="mailserver_mock_tests_"))

from mailserver_mock import server, storage  # noqa: E402
from mailserver_mock.http_server import HTTP_HOST, HTTP_PORT  # noqa: E402
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

    def test_search_unseen_excludes_seen_mail(self):
        """Regressionstest: SEARCH UNSEEN lieferte früher exakt dieselbe Liste
        wie SEARCH ALL, weil die Suchkriterien nie an _handle_search
        durchgereicht wurden (imap_server.py)."""
        _reset_mails()
        _smtp_send_mail("<a@b>", "<c@d>", "unseen test body")
        _smtp_send_mail("<a@b>", "<c@d>", "seen test body")
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _imap_select(r, w, "A2")
            _write(w, r"A3 STORE 2 +FLAGS (\Seen)")
            _read(r)
            _read(r)

            _write(w, "A4 SEARCH ALL")
            all_line = _read(r)
            _read(r)
            self.assertIn("1", all_line.split())
            self.assertIn("2", all_line.split())

            _write(w, "A5 SEARCH UNSEEN")
            unseen_line = _read(r)
            resp = _read(r)
            self.assertIn("1", unseen_line.split())
            self.assertNotIn("2", unseen_line.split())
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

    def test_uid_store(self):
        _reset_mails()
        _smtp_send_mail("<a@b>", "<c@d>", "uid store body")
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _imap_select(r, w, "A2")
            _write(w, r"A3 UID STORE 1 +FLAGS (\Seen)")
            fetch_line = _read(r)  # * 1 FETCH (FLAGS (\Seen) UID 1)
            resp = _read(r)
            self.assertIn(r"\Seen", fetch_line)
            self.assertIn("UID 1", fetch_line)
            self.assertIn("OK", resp)

            flags = storage.load_flags(1)
            self.assertIn(r"\Seen", flags)
        finally:
            sock.close()

    def test_uid_store_silent(self):
        _reset_mails()
        _smtp_send_mail("<a@b>", "<c@d>", "uid store silent body")
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _imap_select(r, w, "A2")
            _write(w, r"A3 UID STORE 1 +FLAGS.SILENT (\Seen)")
            resp = _read(r)  # keine unsolicited FETCH-Response, direkt OK
            self.assertIn("OK", resp)

            flags = storage.load_flags(1)
            self.assertIn(r"\Seen", flags)
        finally:
            sock.close()

    def test_fetch_body_marks_seen(self):
        _reset_mails()
        _smtp_send_mail("<a@b>", "<c@d>", "fetch marks seen body")
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _imap_select(r, w, "A2")
            _write(w, "A3 FETCH 1 (FLAGS BODY[])")
            _imap_read_until(r, "A3")

            flags = storage.load_flags(1)
            self.assertIn(r"\Seen", flags)
        finally:
            sock.close()

    def test_fetch_body_peek_does_not_mark_seen(self):
        _reset_mails()
        _smtp_send_mail("<a@b>", "<c@d>", "fetch peek body")
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _imap_select(r, w, "A2")
            _write(w, "A3 FETCH 1 (FLAGS BODY.PEEK[])")
            _imap_read_until(r, "A3")

            flags = storage.load_flags(1)
            self.assertNotIn(r"\Seen", flags)
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


###############################################################################
# Regressionstests: DATA-Body, STORE-Sequence-Sets, Path Traversal, ENVELOPE
###############################################################################

class SMTPDataBodyTests(unittest.TestCase):
    def test_data_preserves_blank_lines_and_whitespace(self):
        """Regressionstest: line.strip() lief früher auch im DATA-Modus, wodurch
        Leerzeilen, Einrückungen und Leerzeichen am Zeilenende verloren gingen."""
        _reset_mails()
        body = "Subject: ws\r\n\r\nPara1\r\n\r\n    indented\r\ntrailing   \r\n\r\nend"
        _smtp_send_mail("<a@b>", "<c@d>", body)
        self.assertEqual(storage.load_mail(1), body + "\r\n")

    def test_missing_header_separator_is_inserted(self):
        _reset_mails()
        _smtp_send_mail("<a@b>", "<c@d>", "Subject: x\r\nX-Folded: a\r\n b\r\nbody line\r\n\r\n  more")
        self.assertEqual(
            storage.load_mail(1), "Subject: x\r\nX-Folded: a\r\n b\r\n\r\nbody line\r\n\r\n  more\r\n"
        )


class IMAPStoreSeqSetTests(unittest.TestCase):
    def test_store_sequence_set_with_imaplib(self):
        """Regressionstest: STORE 1:1 legte früher eine Datei 1:1.flags an und
        antwortete mit "* 1:1 FETCH", woran imaplib abbrach."""
        import imaplib

        _reset_mails()
        for i in range(3):
            _smtp_send_mail("<a@b>", "<c@d>", f"body {i}")
        client = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        try:
            client.login("testuser", "testpass")
            client.select("INBOX")
            typ, data = client.store("1:1", "+FLAGS", r"(\Seen)")
            self.assertEqual(typ, "OK")
            self.assertEqual(len(data), 1)
            self.assertTrue(data[0].startswith(b"1 (FLAGS"))

            typ, data = client.store("2:*", "+FLAGS", r"(\Flagged)")
            self.assertEqual(typ, "OK")
            self.assertEqual(len(data), 2)
        finally:
            client.logout()

        self.assertEqual(storage.load_flags(1), [r"\Seen"])
        self.assertEqual(storage.load_flags(2), [r"\Flagged"])
        self.assertEqual(storage.load_flags(3), [r"\Flagged"])
        self.assertEqual(sorted(p.name for p in storage.MAIL_DIR.glob("*.flags")), ["1.flags", "2.flags", "3.flags"])

    def test_store_rejects_path_traversal(self):
        _reset_mails()
        _smtp_send_mail("<a@b>", "<c@d>", "body")
        evil = storage.MAIL_DIR.parent.parent / "evil.flags"
        sock, r, w = _dial((IMAP_HOST, IMAP_PORT))
        try:
            _imap_login(r, w)
            _imap_select(r, w, "A2")
            for i, seq in enumerate(["../../evil", "1:1/../x", "0", "-1", "abc"]):
                _write(w, f"B{i} STORE {seq} FLAGS x")
                self.assertTrue(_read(r).startswith(f"B{i} BAD"), seq)
            _write(w, "B9 UID STORE ../../evil FLAGS x")
            self.assertTrue(_read(r).startswith("B9 BAD"))
        finally:
            sock.close()
        self.assertFalse(evil.exists())
        self.assertEqual([p.name for p in storage.MAIL_DIR.glob("*.flags")], ["1.flags"])

    def test_storage_rejects_non_integer_ids(self):
        for bad in ("1:1", "../../evil", 0, -1, True):
            with self.assertRaises(ValueError):
                storage.load_flags(bad)


class EnvelopeEscapingTests(unittest.TestCase):
    def test_quotes_and_backslashes_are_escaped(self):
        raw = 'From: "Bob \\"B\\" Smith" <bob@example.com>\r\nSubject: He said "hi" \\o/\r\nDate: x\r\n\r\nbody'
        env = storage.get_envelope(raw)
        self.assertIn(r'"He said \"hi\" \\o/"', env)
        self.assertIn(r'(("Bob \"B\" Smith" NIL "bob" "example.com"))', env)

    def test_non_ascii_subject_is_sent_as_literal(self):
        env = storage.get_envelope("Subject: Grüße\r\nDate: x\r\n\r\nbody")
        self.assertIn('"x" {7}\r\nGrüße ', env)

    def test_folded_subject_is_unfolded(self):
        env = storage.get_envelope("Subject: line one\r\n line two\r\nDate: x\r\n\r\nbody")
        self.assertIn('"line one line two"', env)

    def test_fetch_envelope_parses_with_imaplib(self):
        import imaplib

        _reset_mails()
        _smtp_send_mail("<a@b>", "<c@d>", 'From: "Ä \\"Q\\"" <a@b.de>\r\nSubject: He said "hi" – Grüße\r\n\r\nbody')
        client = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        try:
            client.login("testuser", "testpass")
            client.select("INBOX")
            typ, data = client.fetch("1", "(BODY.PEEK[])")
            self.assertEqual(typ, "OK")
            # Envelope-Literals für Betreff und Anzeigename, dann BODY[]-Literal.
            literals = [part[1] for part in data if isinstance(part, tuple)]
            self.assertIn('Ä "Q"'.encode(), literals)
            self.assertIn('He said "hi" – Grüße'.encode(), literals)
            self.assertTrue(literals[-1].endswith(b"body\r\n"))
        finally:
            client.logout()


###############################################################################
# HTTP-API
###############################################################################

def _http(path, method="GET"):
    """Liefert (status, headers, body) ohne bei 4xx/5xx eine Exception zu werfen."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"http://{HTTP_HOST}:{HTTP_PORT}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _http_json(path, method="GET") -> "tuple[int, Any]":
    import json

    status, _, body = _http(path, method)
    return status, json.loads(body) if body else None


_MULTIPART_MAIL = (
    "From: =?utf-8?q?J=C3=BCrgen?= <juergen@example.com>\r\n"
    "To: Alice <alice@example.com>, bob@example.com\r\n"
    "Cc: carol@example.com\r\n"
    "Subject: =?utf-8?q?Gr=C3=BC=C3=9Fe?= aus dem Test\r\n"
    "Date: Tue, 03 Nov 2026 10:00:00 +0000\r\n"
    "MIME-Version: 1.0\r\n"
    'Content-Type: multipart/mixed; boundary="B1"\r\n'
    "\r\n"
    "--B1\r\n"
    'Content-Type: multipart/alternative; boundary="B2"\r\n'
    "\r\n"
    "--B2\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Hallo Alice,\r\nder Link: https://example.com/reset\r\n"
    "--B2\r\n"
    "Content-Type: text/html; charset=utf-8\r\n"
    "\r\n"
    "<p>Hallo <b>Alice</b></p>\r\n"
    "--B2--\r\n"
    "--B1\r\n"
    "Content-Type: application/pdf\r\n"
    'Content-Disposition: attachment; filename="rechnung.pdf"\r\n'
    "Content-Transfer-Encoding: base64\r\n"
    "\r\n"
    "JVBERi0xLjQK\r\n"
    "--B1--\r\n"
)


class HTTPApiTests(unittest.TestCase):
    def setUp(self):
        _reset_mails()

    def test_health_reports_all_listeners_up(self):
        status, body = _http_json("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["checks"], {"smtp": "up", "imap": "up", "mail_dir": "up"})
        self.assertEqual(body["mails"], 0)

    def test_health_returns_503_when_a_check_fails(self):
        http_srv = server.start_mail_server()[2]
        with patch.object(http_srv, "health_check", return_value={"smtp": True, "imap": False}):
            status, body = _http_json("/health")
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["checks"]["imap"], "down")

    def test_list_empty(self):
        status, body = _http_json("/mails")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"total": 0, "count": 0, "mails": []})

    def test_mail_sent_via_smtp_is_visible(self):
        _smtp_send_mail("<juergen@example.com>", "<alice@example.com>", _MULTIPART_MAIL)
        status, body = _http_json("/mails")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 1)
        mail = body["mails"][0]
        self.assertEqual(mail["id"], 1)
        self.assertEqual(mail["subject"], "Grüße aus dem Test")
        self.assertEqual(mail["from"], [{"name": "Jürgen", "address": "juergen@example.com"}])
        self.assertEqual([a["address"] for a in mail["to"]], ["alice@example.com", "bob@example.com"])
        self.assertEqual(mail["cc"], [{"name": "", "address": "carol@example.com"}])
        self.assertFalse(mail["seen"])
        self.assertTrue(mail["snippet"].startswith("Hallo Alice,"))

    def test_mail_detail(self):
        _smtp_send_mail("<juergen@example.com>", "<alice@example.com>", _MULTIPART_MAIL)
        status, mail = _http_json("/mails/1")
        self.assertEqual(status, 200)
        self.assertIn("https://example.com/reset", mail["text"])
        self.assertIn("<b>Alice</b>", mail["html"])
        self.assertEqual(mail["attachments"], [
            {"filename": "rechnung.pdf", "content_type": "application/pdf", "size": 9},
        ])
        self.assertIn(["Cc", "carol@example.com"], mail["headers"])

    def test_plain_mail_without_mime_headers(self):
        _smtp_send_mail("<a@b>", "<c@d>", "nur ein Body")
        _, mail = _http_json("/mails/1")
        self.assertEqual(mail["subject"], "Mock Mail 1")
        self.assertEqual(mail["text"].strip(), "nur ein Body")
        self.assertIsNone(mail["html"])

    def test_raw(self):
        _smtp_send_mail("<a@b>", "<c@d>", "Subject: Roh\r\n\r\nInhalt")
        status, headers, body = _http("/mails/1/raw")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "message/rfc822")
        self.assertEqual(body, storage.load_mail(1).encode())

    def test_filters(self):
        _smtp_send_mail("<a@b>", "<c@d>", "From: a@b\r\nTo: alice@example.com\r\nSubject: Willkommen\r\n\r\nx")
        _smtp_send_mail("<a@b>", "<c@d>", "From: shop@b\r\nTo: bob@example.com\r\nSubject: Rechnung\r\n\r\nx")
        _, body = _http_json("/mails?to=BOB")
        self.assertEqual([m["id"] for m in body["mails"]], [2])
        self.assertEqual(body["total"], 2)
        _, body = _http_json("/mails?subject=willkommen")
        self.assertEqual([m["id"] for m in body["mails"]], [1])
        _, body = _http_json("/mails?from=shop&subject=willkommen")
        self.assertEqual(body["mails"], [])

    def test_newest_first(self):
        for i in range(3):
            _smtp_send_mail("<a@b>", "<c@d>", f"Subject: {i}\r\n\r\nx")
        _, body = _http_json("/mails")
        self.assertEqual([m["id"] for m in body["mails"]], [3, 2, 1])

    def test_seen_flag_from_imap_is_reported(self):
        _smtp_send_mail("<a@b>", "<c@d>", "Subject: s\r\n\r\nx")
        storage.save_flags(1, ["\\Seen"])
        _, mail = _http_json("/mails/1")
        self.assertTrue(mail["seen"])
        self.assertEqual(mail["flags"], ["\\Seen"])

    def test_unknown_and_invalid_ids(self):
        for path in ("/mails/99", "/mails/99/raw", "/mails/0", "/mails/abc", "/mails/..%2F..%2Fetc", "/nope"):
            status, body = _http_json(path)
            self.assertEqual(status, 404, path)
            self.assertIn("error", body)

    def test_delete_all(self):
        for _ in range(3):
            _smtp_send_mail("<a@b>", "<c@d>", "Subject: s\r\n\r\nx")
        status, body = _http_json("/mails", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"deleted": 3})
        self.assertEqual(storage.count_mails(), 0)
        self.assertEqual(list(storage.MAIL_DIR.glob("*")), [])
        # IDs beginnen nach dem Reset wieder bei 1.
        _smtp_send_mail("<a@b>", "<c@d>", "Subject: s\r\n\r\nx")
        self.assertEqual(storage.list_ids(), [1])

    def test_delete_single(self):
        _smtp_send_mail("<a@b>", "<c@d>", "Subject: 1\r\n\r\nx")
        _smtp_send_mail("<a@b>", "<c@d>", "Subject: 2\r\n\r\nx")
        status, _, body = _http("/mails/1", "DELETE")
        self.assertEqual((status, body), (204, b""))
        self.assertEqual(storage.list_ids(), [2])
        status, _ = _http_json("/mails/1", "DELETE")
        self.assertEqual(status, 404)

    def test_method_not_allowed(self):
        status, headers, _ = _http("/mails", "POST")
        self.assertEqual(status, 405)
        self.assertIn("DELETE", headers["Allow"])
        status, _, _ = _http("/health", "DELETE")
        self.assertEqual(status, 405)

    def test_ui_is_served_with_csp(self):
        status, headers, body = _http("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn(b"/static/app.js", body)
        for path, ctype in (("/static/app.js", "javascript"), ("/static/app.css", "text/css")):
            status, headers, _ = _http(path)
            self.assertEqual(status, 200, path)
            self.assertIn(ctype, headers["Content-Type"])
        status, _, _ = _http("/static/../http_server.py")
        self.assertEqual(status, 404)
