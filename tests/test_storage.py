"""Storage: ID-Vergabe, Rohbytes, ENVELOPE-Aufbau."""
import threading
import unittest

from support import (
    IMAP_HOST,
    IMAP_PORT,
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
