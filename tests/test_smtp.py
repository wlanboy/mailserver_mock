"""SMTP-Mock, orientiert an der gosmtp-Referenz (smtp_test.go)."""
import base64
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
    smtp_login,
    smtp_send_mail,
    start_servers,
    write_line,
)

from mailserver_mock import storage

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
