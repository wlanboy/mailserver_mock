"""HTTP-API und Web-UI."""
import unittest
from unittest.mock import patch

from support import (
    MULTIPART_MAIL,
    http,
    http_json,
    reset_mails,
    smtp_send_mail,
    start_servers,
)

from mailserver_mock import server, storage

setUpModule = start_servers


class HTTPApiTests(unittest.TestCase):
    def setUp(self):
        reset_mails()

    def test_health_reports_all_listeners_up(self):
        status, body = http_json("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["checks"], {"smtp": "up", "imap": "up", "mail_dir": "up"})
        self.assertEqual(body["mails"], 0)

    def test_health_returns_503_when_a_check_fails(self):
        http_srv = server.start_mail_server()[2]
        with patch.object(http_srv, "health_check", return_value={"smtp": True, "imap": False}):
            status, body = http_json("/health")
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["checks"]["imap"], "down")

    def test_list_empty(self):
        status, body = http_json("/mails")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"total": 0, "count": 0, "mails": []})

    def test_mail_sent_via_smtp_is_visible(self):
        smtp_send_mail("<juergen@example.com>", "<alice@example.com>", MULTIPART_MAIL)
        status, body = http_json("/mails")
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
        smtp_send_mail("<juergen@example.com>", "<alice@example.com>", MULTIPART_MAIL)
        status, mail = http_json("/mails/1")
        self.assertEqual(status, 200)
        self.assertIn("https://example.com/reset", mail["text"])
        self.assertIn("<b>Alice</b>", mail["html"])
        self.assertEqual(mail["attachments"], [
            {"filename": "rechnung.pdf", "content_type": "application/pdf", "size": 9},
        ])
        self.assertIn(["Cc", "carol@example.com"], mail["headers"])

    def test_plain_mail_without_mime_headers(self):
        smtp_send_mail("<a@b>", "<c@d>", "nur ein Body")
        _, mail = http_json("/mails/1")
        self.assertEqual(mail["subject"], "Mock Mail 1")
        self.assertEqual(mail["text"].strip(), "nur ein Body")
        self.assertIsNone(mail["html"])

    def test_raw(self):
        smtp_send_mail("<a@b>", "<c@d>", "Subject: Roh\r\n\r\nInhalt")
        status, headers, body = http("/mails/1/raw")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "message/rfc822")
        self.assertEqual(body, storage.load_mail(1))

    def test_filters(self):
        smtp_send_mail("<a@b>", "<c@d>", "From: a@b\r\nTo: alice@example.com\r\nSubject: Willkommen\r\n\r\nx")
        smtp_send_mail("<a@b>", "<c@d>", "From: shop@b\r\nTo: bob@example.com\r\nSubject: Rechnung\r\n\r\nx")
        _, body = http_json("/mails?to=BOB")
        self.assertEqual([m["id"] for m in body["mails"]], [2])
        self.assertEqual(body["total"], 2)
        _, body = http_json("/mails?subject=willkommen")
        self.assertEqual([m["id"] for m in body["mails"]], [1])
        _, body = http_json("/mails?from=shop&subject=willkommen")
        self.assertEqual(body["mails"], [])

    def test_newest_first(self):
        for i in range(3):
            smtp_send_mail("<a@b>", "<c@d>", f"Subject: {i}\r\n\r\nx")
        _, body = http_json("/mails")
        self.assertEqual([m["id"] for m in body["mails"]], [3, 2, 1])

    def test_seen_flag_from_imap_is_reported(self):
        smtp_send_mail("<a@b>", "<c@d>", "Subject: s\r\n\r\nx")
        storage.save_flags(1, ["\\Seen"])
        _, mail = http_json("/mails/1")
        self.assertTrue(mail["seen"])
        self.assertEqual(mail["flags"], ["\\Seen"])

    def test_unknown_and_invalid_ids(self):
        for path in ("/mails/99", "/mails/99/raw", "/mails/0", "/mails/abc", "/mails/..%2F..%2Fetc", "/nope"):
            status, body = http_json(path)
            self.assertEqual(status, 404, path)
            self.assertIn("error", body)

    def test_delete_all(self):
        for _ in range(3):
            smtp_send_mail("<a@b>", "<c@d>", "Subject: s\r\n\r\nx")
        status, body = http_json("/mails", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"deleted": 3})
        self.assertEqual(storage.count_mails(), 0)
        self.assertEqual(list(storage.MAIL_DIR.glob("*")), [])
        # IDs beginnen nach dem Reset wieder bei 1.
        smtp_send_mail("<a@b>", "<c@d>", "Subject: s\r\n\r\nx")
        self.assertEqual(storage.list_ids(), [1])

    def test_delete_single(self):
        smtp_send_mail("<a@b>", "<c@d>", "Subject: 1\r\n\r\nx")
        smtp_send_mail("<a@b>", "<c@d>", "Subject: 2\r\n\r\nx")
        status, _, body = http("/mails/1", "DELETE")
        self.assertEqual((status, body), (204, b""))
        self.assertEqual(storage.list_ids(), [2])
        status, _ = http_json("/mails/1", "DELETE")
        self.assertEqual(status, 404)

    def test_method_not_allowed(self):
        status, headers, _ = http("/mails", "POST")
        self.assertEqual(status, 405)
        self.assertIn("DELETE", headers["Allow"])
        status, _, _ = http("/health", "DELETE")
        self.assertEqual(status, 405)

    def test_ui_is_served_with_csp(self):
        status, headers, body = http("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn(b"/static/app.js", body)
        for path, ctype in (("/static/app.js", "javascript"), ("/static/app.css", "text/css")):
            status, headers, _ = http(path)
            self.assertEqual(status, 200, path)
            self.assertIn(ctype, headers["Content-Type"])
        status, _, _ = http("/static/../http_server.py")
        self.assertEqual(status, 404)
