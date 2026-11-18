"""Storage: ID-Vergabe, Rohbytes, ENVELOPE-Aufbau."""
import threading
import unittest

from support import (
    IMAP_HOST,
    IMAP_PORT,
    http,
    http_json,
    reset_mails,
    smtp_send_mail,
    start_servers,
)

from mailserver_mock import storage

setUpModule = start_servers


class StorageConcurrencyTests(unittest.TestCase):
    def test_concurrent_save_mail_assigns_unique_ids(self):
        """Regressionstest: next_id()+Schreiben liefen früher ohne Lock ab, wodurch
        parallele Sessions dieselbe ID berechnen und sich gegenseitig überschreiben
        konnten (ThreadingTCPServer bedient Verbindungen in eigenen Threads)."""
        reset_mails()
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


class EnvelopeEscapingTests(unittest.TestCase):
    def test_quotes_and_backslashes_are_escaped(self):
        raw = 'From: "Bob \\"B\\" Smith" <bob@example.com>\r\nSubject: He said "hi" \\o/\r\nDate: x\r\n\r\nbody'
        env = storage.get_envelope(raw.encode())
        self.assertIn(r'"He said \"hi\" \\o/"', env)
        self.assertIn(r'(("Bob \"B\" Smith" NIL "bob" "example.com"))', env)

    def test_non_ascii_subject_is_sent_as_literal(self):
        env = storage.get_envelope("Subject: Grüße\r\nDate: x\r\n\r\nbody".encode())
        self.assertIn('"x" {7}\r\nGrüße ', env)

    def test_folded_subject_is_unfolded(self):
        env = storage.get_envelope(b"Subject: line one\r\n line two\r\nDate: x\r\n\r\nbody")
        self.assertIn('"line one line two"', env)

    def test_fetch_envelope_parses_with_imaplib(self):
        import imaplib

        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", 'From: "Ä \\"Q\\"" <a@b.de>\r\nSubject: He said "hi" – Grüße\r\n\r\nbody')
        client = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        try:
            client.login("testuser", "testpass")
            client.select("INBOX")
            typ, data = client.fetch("1", "(ENVELOPE BODY.PEEK[])")
            self.assertEqual(typ, "OK")
            # Envelope-Literals für Betreff und Anzeigename, dann BODY[]-Literal.
            literals = [part[1] for part in data if isinstance(part, tuple)]
            self.assertIn('Ä "Q"'.encode(), literals)
            self.assertIn('He said "hi" – Grüße'.encode(), literals)
            self.assertTrue(literals[-1].endswith(b"body\r\n"))
        finally:
            client.logout()


_LATIN1_MAIL = (
    "From: J\u00f6rg <joerg@example.com>\r\n"
    "To: c@d\r\n"
    "Subject: Gr\u00fc\u00dfe\r\n"
    "Date: Tue, 03 Nov 2026 10:00:00 +0000\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: text/plain; charset=iso-8859-1\r\n"
    "Content-Transfer-Encoding: 8bit\r\n"
    "\r\n"
    "Sch\u00f6ne Gr\u00fc\u00dfe aus K\u00f6ln\r\n"
).encode("latin-1")


class EightBitMailTests(unittest.TestCase):
    """Regressionstest: SMTP dekodierte DATA früher als UTF-8 mit errors="replace".
    Umlaute in ISO-8859-1 wurden zu U+FFFD, Größe und Inhalt stimmten nicht mehr."""

    def setUp(self):
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", _LATIN1_MAIL.removesuffix(b"\r\n"))

    def test_storage_keeps_bytes(self):
        self.assertEqual(storage.load_mail(1), _LATIN1_MAIL)

    def test_http_size_raw_and_decoded_text(self):
        _, body = http_json("/mails")
        self.assertEqual(body["mails"][0]["size"], len(_LATIN1_MAIL))
        _, _, raw = http("/mails/1/raw")
        self.assertEqual(raw, _LATIN1_MAIL)
        _, mail = http_json("/mails/1")
        self.assertEqual(mail["text"], "Sch\u00f6ne Gr\u00fc\u00dfe aus K\u00f6ln\r\n")

    def test_imap_delivers_original_bytes(self):
        import imaplib

        client = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        try:
            client.login("testuser", "testpass")
            client.select("INBOX")
            typ, data = client.fetch("1", "(RFC822.SIZE ENVELOPE BODY.PEEK[])")
            self.assertEqual(typ, "OK")
            assert isinstance(data[0], tuple)
            self.assertIn(f"RFC822.SIZE {len(_LATIN1_MAIL)}".encode(), data[0][0])
            literals = [part[1] for part in data if isinstance(part, tuple)]
            self.assertIn("Gr\u00fc\u00dfe".encode("latin-1"), literals)  # ENVELOPE-Subject
            self.assertEqual(literals[-1], _LATIN1_MAIL)
        finally:
            client.logout()

    def test_get_envelope_counts_original_bytes(self):
        env = storage.get_envelope(b"Subject: Gr\xfc\xdfe\r\nDate: x\r\n\r\nbody")
        self.assertEqual(storage.encode_response(env).split(b" ", 2)[1], b"{5}\r\nGr\xfc\xdfe")
