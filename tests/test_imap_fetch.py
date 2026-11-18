"""IMAP FETCH: Items, Sections, BODYSTRUCTURE."""
import re
import unittest

from support import (
    ALTERNATIVE_MAIL,
    IMAP_HOST,
    IMAP_PORT,
    dial,
    imap_login,
    imap_select,
    reset_mails,
    smtp_send_mail,
    start_servers,
    write_line,
)

from mailserver_mock import storage

setUpModule = start_servers


class IMAPFetchItemTests(unittest.TestCase):
    """FETCH liefert genau die angefragten Items."""

    def setUp(self):
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "From: a@b\r\nSubject: Hallo\r\nX-Test: 1\r\n\r\nZeile eins\r\nZeile zwei\r\n")
        self.sock, self.r, self.w = dial((IMAP_HOST, IMAP_PORT))
        imap_login(self.r, self.w)
        imap_select(self.r, self.w, "A2")

    def tearDown(self):
        self.sock.close()

    def _fetch(self, command):
        """Schickt ein FETCH und liefert die Antwort bis zur getaggten Zeile als Bytes."""
        write_line(self.w, command)
        tag = command.split(" ", 1)[0].encode()
        data = b""
        while True:
            line = self.r.readline()
            data += line
            if match := re.search(rb"\{(\d+)\}\r\n$", line):
                data += self.r.read(int(match.group(1)))
            elif line.startswith(tag + b" "):
                return data

    def test_flags_only(self):
        self.assertEqual(b"* 1 FETCH (FLAGS ())\r\nA3 OK FETCH completed\r\n", self._fetch("A3 FETCH 1 (FLAGS)"))

    def test_uid_fetch_always_contains_uid(self):
        self.assertEqual(b"* 1 FETCH (UID 1 FLAGS ())\r\nA3 OK FETCH completed\r\n", self._fetch("A3 UID FETCH 1 (FLAGS)"))

    def test_rfc822_size_and_internaldate(self):
        size = len(storage.load_mail(1))
        resp = self._fetch("A3 FETCH 1 FAST")
        self.assertRegex(
            resp.decode(),
            rf'^\* 1 FETCH \(FLAGS \(\) INTERNALDATE "\d\d-[A-Z][a-z]{{2}}-\d{{4}} \d\d:\d\d:\d\d \+0000" RFC822.SIZE {size}\)\r\n',
        )

    def test_body_peek_header(self):
        header = storage.load_mail(1).split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        resp = self._fetch("A3 FETCH 1 (BODY.PEEK[HEADER])")
        self.assertEqual(f"* 1 FETCH (BODY[HEADER] {{{len(header)}}}\r\n".encode() + header + b")\r\nA3 OK FETCH completed\r\n", resp)
        self.assertEqual([], storage.load_flags(1))

    def test_header_fields_and_not(self):
        resp = self._fetch("A3 FETCH 1 (BODY.PEEK[HEADER.FIELDS (Subject x-test)])")
        self.assertIn(b"BODY[HEADER.FIELDS (SUBJECT X-TEST)] {29}\r\nSubject: Hallo\r\nX-Test: 1\r\n\r\n)", resp)
        resp = self._fetch("A4 FETCH 1 (BODY.PEEK[HEADER.FIELDS.NOT (SUBJECT X-TEST TO DATE)])")
        self.assertIn(b"{13}\r\nFrom: a@b\r\n\r\n)", resp)

    def test_text_and_partial(self):
        resp = self._fetch("A3 FETCH 1 (BODY.PEEK[TEXT] BODY.PEEK[]<0.9>)")
        self.assertIn(b"BODY[TEXT] {26}\r\nZeile eins\r\nZeile zwei\r\n\r\n", resp)
        self.assertIn(b"BODY[]<0> {9}\r\n" + storage.load_mail(1)[:9], resp)

    def test_body_marks_seen_and_reports_flags(self):
        resp = self._fetch("A3 FETCH 1 (BODY[TEXT])")
        self.assertIn(b"FLAGS (\\Seen))\r\n", resp)
        self.assertEqual(["\\Seen"], storage.load_flags(1))

    def test_invalid_items_are_rejected(self):
        self.assertTrue(self._fetch("A3 FETCH 1 (FLAGS BOGUS)").startswith(b"A3 BAD"))
        self.assertTrue(self._fetch("A4 FETCH 1").startswith(b"A4 BAD"))
        self.assertTrue(self._fetch("A5 FETCH 1 (BODY[HEADER.FIELDS])").startswith(b"A5 BAD"))

    def test_bodystructure_and_parts_with_imaplib(self):
        import imaplib

        smtp_send_mail("<a@b>", "<c@d>", ALTERNATIVE_MAIL)
        client = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        try:
            client.login("testuser", "testpass")
            client.select("INBOX")
            typ, data = client.fetch("2", "(BODYSTRUCTURE)")
            self.assertEqual("OK", typ)
            assert isinstance(data[0], bytes)
            structure = data[0].decode()
            self.assertIn('("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 10 1)', structure)
            self.assertIn('"HTML"', structure)
            self.assertTrue(structure.endswith(' "ALTERNATIVE"))'))

            typ, data = client.fetch("2", "(BODY.PEEK[2] BODY.PEEK[1.MIME])")
            literals = [part[1] for part in data if isinstance(part, tuple)]
            self.assertEqual([b"<p>html</p>", b"Content-Type: text/plain; charset=utf-8\r\n\r\n"], literals)

            typ, data = client.fetch("1", "(BODY.PEEK[1] RFC822.HEADER)")
            literals = [part[1] for part in data if isinstance(part, tuple)]
            self.assertEqual(b"Zeile eins\r\nZeile zwei\r\n\r\n", literals[0])
            self.assertTrue(literals[1].startswith(b"From: a@b\r\n") or b"Subject: Hallo" in literals[1])
        finally:
            client.logout()
