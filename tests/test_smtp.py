"""SMTP-Mock, orientiert an der gosmtp-Referenz (smtp_test.go)."""
import base64
import smtplib
import time
import unittest
from unittest.mock import patch

from support import (
    SMTP_HOST,
    SMTP_PORT,
    dial,
    read_line,
    reset_mails,
    smtp_auth_login,
    smtp_ehlo,
    smtp_login,
    smtp_send_mail,
    start_servers,
    write_line,
)

from mailserver_mock import connection, smtp_server, storage

setUpModule = start_servers


class SMTPTests(unittest.TestCase):
    def test_happy_path(self):
        reset_mails()
        smtp_send_mail("<sender@example.com>", "<rcpt@example.com>", "Hello World")
        self.assertEqual(storage.count_mails(), 1)

    def test_auth_fail(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            read_line(r)
            write_line(w, "AUTH LOGIN")
            read_line(r)
            write_line(w, base64.b64encode(b"wrong").decode())
            read_line(r)
            write_line(w, base64.b64encode(b"wrong").decode())
            resp = read_line(r)
            self.assertIn("535", resp)
        finally:
            sock.close()

    def test_auth_plain(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            read_line(r)
            creds = base64.b64encode(b"\x00testuser\x00testpass").decode()
            write_line(w, f"AUTH PLAIN {creds}")
            resp = read_line(r)
            self.assertIn("235", resp)
        finally:
            sock.close()

    def test_auth_plain_sasl_continuation(self):
        # SASL-Continuation-Form ohne Initial-Response, wie sie z.B. Jakarta
        # Mail / Spring Boot Clients standardmäßig senden.
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            read_line(r)
            write_line(w, "AUTH PLAIN")
            resp = read_line(r)
            self.assertIn("334", resp)
            creds = base64.b64encode(b"\x00testuser\x00testpass").decode()
            write_line(w, creds)
            resp = read_line(r)
            self.assertIn("235", resp)
        finally:
            sock.close()

    def test_helo(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            read_line(r)
            write_line(w, "HELO localhost")
            resp = read_line(r)
            self.assertIn("250", resp)
        finally:
            sock.close()

    def test_noop(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            read_line(r)
            write_line(w, "NOOP")
            resp = read_line(r)
            self.assertIn("250", resp)
        finally:
            sock.close()

    def test_rset(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            smtp_login(r, w)
            write_line(w, "MAIL FROM:<a@b>")
            read_line(r)
            write_line(w, "RSET")
            resp = read_line(r)
            self.assertIn("250", resp)

            # Nach RSET muss DATA fehlschlagen, da MAIL FROM zurückgesetzt wurde.
            write_line(w, "DATA")
            resp = read_line(r)
            self.assertIn("503", resp)
        finally:
            sock.close()

    def test_quit(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            read_line(r)
            write_line(w, "QUIT")
            resp = read_line(r)
            self.assertIn("221", resp)
        finally:
            sock.close()

    def test_unknown_command(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            read_line(r)
            write_line(w, "XYZZY")
            resp = read_line(r)
            self.assertIn("502", resp)
        finally:
            sock.close()

    def test_no_auth_mail_from(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            read_line(r)
            write_line(w, "MAIL FROM:<a@b>")
            resp = read_line(r)
            self.assertIn("530", resp)
        finally:
            sock.close()

    def test_no_auth_rcpt_to(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            read_line(r)
            write_line(w, "RCPT TO:<a@b>")
            resp = read_line(r)
            self.assertIn("530", resp)
        finally:
            sock.close()

    def test_data_without_mail_from(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            smtp_login(r, w)
            write_line(w, "DATA")
            resp = read_line(r)
            self.assertIn("503", resp)
        finally:
            sock.close()

    def test_data_without_rcpt_to(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            smtp_login(r, w)
            write_line(w, "MAIL FROM:<a@b>")
            read_line(r)
            write_line(w, "DATA")
            resp = read_line(r)
            self.assertIn("503", resp)
        finally:
            sock.close()

    def test_multiple_recipients(self):
        reset_mails()
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            smtp_login(r, w)
            write_line(w, "MAIL FROM:<sender@example.com>")
            read_line(r)
            for rcpt in ("<alice@example.com>", "<bob@example.com>", "<carol@example.com>"):
                write_line(w, f"RCPT TO:{rcpt}")
                read_line(r)
            write_line(w, "DATA")
            read_line(r)
            write_line(w, "Hello three recipients")
            write_line(w, ".")
            resp = read_line(r)
            self.assertIn("250", resp)
            self.assertEqual(storage.count_mails(), 1)
        finally:
            sock.close()

    def test_dot_stuffing(self):
        reset_mails()
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            smtp_login(r, w)
            write_line(w, "MAIL FROM:<a@b>")
            read_line(r)
            write_line(w, "RCPT TO:<c@d>")
            read_line(r)
            write_line(w, "DATA")
            read_line(r)

            # RFC 5321: ein führender Punkt wird vom Client mit einem zusätzlichen Punkt escaped.
            write_line(w, "..dotline")
            write_line(w, "normal line")
            write_line(w, ".")
            read_line(r)  # 250 Message accepted (Nachricht angenommen)

            content = storage.load_mail(1)
            self.assertIn(b".dotline", content)
            self.assertNotIn(b"..dotline", content)
        finally:
            sock.close()

    def test_two_mails_in_one_session(self):
        reset_mails()
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            smtp_login(r, w)
            for from_addr, rcpt, body in (
                ("<first@example.com>", "<rcpt1@example.com>", "First mail body"),
                ("<second@example.com>", "<rcpt2@example.com>", "Second mail body"),
            ):
                write_line(w, f"MAIL FROM:{from_addr}")
                read_line(r)
                write_line(w, f"RCPT TO:{rcpt}")
                read_line(r)
                write_line(w, "DATA")
                read_line(r)
                write_line(w, body)
                write_line(w, ".")
                read_line(r)
            self.assertEqual(storage.count_mails(), 2)
        finally:
            sock.close()

    def test_too_many_login_delay_and_error(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            start = time.time()
            resp = smtp_auth_login(r, w, "testuser_421", "testpass_421")
            elapsed = time.time() - start
            self.assertIn("421", resp)
            self.assertIn("4.7.0", resp)
            self.assertGreaterEqual(elapsed, 0.9)
        finally:
            sock.close()

    def test_timeout_login_error(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            with patch("mailserver_mock.smtp_server.time.sleep"):
                resp = smtp_auth_login(r, w, "testuser_451", "testpass_451")
            self.assertIn("451", resp)
            self.assertIn("4.4.2", resp)
        finally:
            sock.close()

    def test_quota_error_on_data(self):
        reset_mails()
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            resp = smtp_auth_login(r, w, "testuser_552", "testpass_552")
            self.assertIn("235", resp)
            write_line(w, "MAIL FROM:<a@b>")
            read_line(r)
            write_line(w, "RCPT TO:<c@d>")
            read_line(r)
            write_line(w, "DATA")
            read_line(r)
            write_line(w, "quota test body")
            write_line(w, ".")
            resp = read_line(r)
            self.assertIn("552", resp)
            self.assertIn("5.2.2", resp)
            self.assertEqual(storage.count_mails(), 0)
        finally:
            sock.close()


class SMTPDataBodyTests(unittest.TestCase):
    def test_data_preserves_blank_lines_and_whitespace(self):
        """Regressionstest: line.strip() lief früher auch im DATA-Modus, wodurch
        Leerzeilen, Einrückungen und Leerzeichen am Zeilenende verloren gingen."""
        reset_mails()
        body = "From: a@b\r\nTo: c@d\r\nDate: x\r\nSubject: ws\r\n\r\nPara1\r\n\r\n    indented\r\ntrailing   \r\n\r\nend"
        smtp_send_mail("<a@b>", "<c@d>", body)
        self.assertEqual(storage.load_mail(1), (body + "\r\n").encode())

    def test_missing_header_separator_is_inserted(self):
        reset_mails()
        headers = "From: a@b\r\nTo: c@d\r\nDate: x\r\nSubject: x\r\nX-Folded: a\r\n b\r\n"
        smtp_send_mail("<a@b>", "<c@d>", headers + "body line\r\n\r\n  more")
        self.assertEqual(storage.load_mail(1), (headers + "\r\nbody line\r\n\r\n  more\r\n").encode())


class HeaderCompletionTests(unittest.TestCase):
    def test_headers_without_subject_stay_in_header_block(self):
        """Regressionstest: Fehlte nur Subject, setzte save_mail früher einen
        kompletten zweiten Header-Block davor und schob die echten Header in
        den Body."""
        reset_mails()
        smtp_send_mail(
            "<a@b>", "<c@d>", "From: Alice <alice@example.com>\r\nTo: bob@example.com\r\nDate: x\r\n\r\nHallo"
        )
        self.assertEqual(
            storage.load_mail(1),
            b"Subject: Mock Mail 1\r\nFrom: Alice <alice@example.com>\r\nTo: bob@example.com\r\nDate: x\r\n\r\nHallo\r\n",
        )

    def test_subject_in_body_does_not_suppress_generated_headers(self):
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "Hallo,\r\nsubject: steht nur im Text")
        raw = storage.load_mail(1).decode()
        headers, _, body = raw.partition("\r\n\r\n")
        self.assertEqual(
            [line.split(":", 1)[0] for line in headers.split("\r\n")], ["From", "To", "Subject", "Date"]
        )
        self.assertIn("Subject: Mock Mail 1", headers)
        self.assertEqual(body, "Hallo,\r\nsubject: steht nur im Text\r\n")


class SMTPLimitTests(unittest.TestCase):
    """Timeouts, Zeilenlimit und SIZE (RFC 1870)."""

    def _start_data(self, r, w, mail_from="MAIL FROM:<a@b>"):
        smtp_login(r, w)
        write_line(w, mail_from)
        self.assertIn("250", read_line(r))
        write_line(w, "RCPT TO:<c@d>")
        read_line(r)
        write_line(w, "DATA")
        self.assertIn("354", read_line(r))

    def test_ehlo_announces_size(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            read_line(r)
            self.assertIn(f"SIZE {smtp_server.SMTP_MAX_MESSAGE_SIZE}", smtp_ehlo(r, w))
        finally:
            sock.close()

    def test_mail_from_size_above_limit_is_rejected(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            smtp_login(r, w)
            write_line(w, f"MAIL FROM:<a@b> SIZE={smtp_server.SMTP_MAX_MESSAGE_SIZE + 1}")
            self.assertTrue(read_line(r).startswith("552 5.3.4"))
            # Ohne gültiges MAIL FROM gibt es kein DATA.
            write_line(w, "RCPT TO:<c@d>")
            read_line(r)
            write_line(w, "DATA")
            self.assertIn("503", read_line(r))
        finally:
            sock.close()

    def test_mail_from_size_param_is_not_part_of_address(self):
        reset_mails()
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            self._start_data(r, w, "MAIL FROM:<a@b> SIZE=20")
            write_line(w, "Subject: x\r\n\r\nHallo")
            write_line(w, ".")
            self.assertIn("250", read_line(r))
            self.assertTrue(storage.load_mail(1).startswith(b"From: <a@b>\r\n"))
        finally:
            sock.close()

    def test_smtplib_sends_size_and_succeeds(self):
        reset_mails()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=5) as client:
            client.login("testuser", "testpass")
            self.assertTrue(client.has_extn("size"))
            client.sendmail("a@b", ["c@d"], "Subject: smtplib\r\n\r\nHallo\r\n")
        self.assertEqual(storage.count_mails(), 1)

    def test_data_above_limit_is_rejected_and_session_continues(self):
        reset_mails()
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            with patch.object(smtp_server, "SMTP_MAX_MESSAGE_SIZE", 100):
                self._start_data(r, w)
                for _ in range(10):
                    write_line(w, "x" * 50)
                write_line(w, ".")
                self.assertTrue(read_line(r).startswith("552 5.3.4"))
                self.assertEqual(storage.count_mails(), 0)

                write_line(w, "MAIL FROM:<a@b>")
                read_line(r)
                write_line(w, "RCPT TO:<c@d>")
                read_line(r)
                write_line(w, "DATA")
                read_line(r)
                write_line(w, "klein")
                write_line(w, ".")
                self.assertIn("250", read_line(r))
            self.assertEqual(storage.count_mails(), 1)
        finally:
            sock.close()

    def test_long_data_lines_are_read_in_chunks(self):
        # Kleine Stücke erzwingen Zeilen über mehrere Lesevorgänge, auch mit
        # einem CRLF, das genau an der Stückgrenze liegt.
        reset_mails()
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            with patch.object(smtp_server, "_DATA_CHUNK", 8):
                self._start_data(r, w)
                write_line(w, "Subject: lang\r\n")
                write_line(w, "..punkt am Anfang und eine lange Zeile")
                write_line(w, "1234567")  # 7 Zeichen + CR = 8 Bytes, LF im nächsten Stück
                write_line(w, "abcdefg.")
                write_line(w, "x" * 7 + ".")  # Punkt mitten in der Zeile, kein Ende-Marker
                write_line(w, ".")
                self.assertIn("250", read_line(r))
            self.assertEqual(
                storage.load_mail(1).split(b"\r\n\r\n", 1)[1],
                b".punkt am Anfang und eine lange Zeile\r\n1234567\r\nabcdefg.\r\nxxxxxxx.\r\n",
            )
        finally:
            sock.close()

    def test_overlong_command_line_is_rejected(self):
        sock, r, w = dial((SMTP_HOST, SMTP_PORT))
        try:
            read_line(r)
            write_line(w, "NOOP " + "x" * (connection.MAX_LINE * 2))
            self.assertEqual("500 5.5.2 Line too long", read_line(r))
            write_line(w, "NOOP")
            self.assertEqual("250 OK", read_line(r))
        finally:
            sock.close()

    def test_idle_client_is_disconnected(self):
        with patch.object(smtp_server.SMTPHandler, "timeout", 0.3):
            sock, r, w = dial((SMTP_HOST, SMTP_PORT))
            try:
                read_line(r)
                self.assertTrue(read_line(r).startswith("421 4.4.2"))
                self.assertEqual(b"", r.readline())
            finally:
                sock.close()
