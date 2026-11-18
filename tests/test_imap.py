"""IMAP-Mock: Login, Mailbox-Kommandos, STORE, SEARCH, Sequenznummern.

Orientiert an der gosmtp-Referenz (imap_test.go).
"""
import time
import unittest
from unittest.mock import patch

from support import (
    IMAP_HOST,
    IMAP_PORT,
    dial,
    http,
    imap_login,
    imap_read_until,
    imap_select,
    read_line,
    reset_mails,
    smtp_send_mail,
    start_servers,
    write_line,
)

from mailserver_mock import connection, imap_server, storage

setUpModule = start_servers


class IMAPTests(unittest.TestCase):
    def test_login(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
        finally:
            sock.close()

    def test_login_fail(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            read_line(r)
            write_line(w, "A1 LOGIN wrong wrong")
            resp = read_line(r)
            self.assertIn("NO", resp)
        finally:
            sock.close()

    _SPECIAL_USER = {"username": "spezial user", "password": 'geh eim"\\pw', "behavior": "normal"}

    def _find_special_user(self, username):
        return self._SPECIAL_USER if username == self._SPECIAL_USER["username"] else None

    def test_login_quoted_with_space_quote_and_backslash(self):
        """Quoted-Strings mit Leerzeichen und Escapes (\\" und \\\\) werden korrekt zerlegt."""
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            read_line(r)
            with patch("mailserver_mock.imap_server.users.find_user", side_effect=self._find_special_user):
                write_line(w, 'A1 LOGIN "spezial user" "geh eim\\"\\\\pw"')
                resp = read_line(r)
            self.assertEqual("A1 OK LOGIN completed", resp)
        finally:
            sock.close()

    def test_login_with_literals(self):
        """Synchronisierende Literale ({n}) werden mit "+ Ready" bestätigt und eingelesen."""
        user = self._SPECIAL_USER["username"].encode("utf-8")
        password = self._SPECIAL_USER["password"].encode("utf-8")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            read_line(r)
            with patch("mailserver_mock.imap_server.users.find_user", side_effect=self._find_special_user):
                write_line(w, f"A1 LOGIN {{{len(user)}}}")
                self.assertTrue(read_line(r).startswith("+"))
                w.write(user + f" {{{len(password)}}}\r\n".encode("utf-8"))
                w.flush()
                self.assertTrue(read_line(r).startswith("+"))
                w.write(password + b"\r\n")
                w.flush()
                resp = read_line(r)
            self.assertEqual("A1 OK LOGIN completed", resp)
        finally:
            sock.close()

    def test_login_with_imaplib_special_password(self):
        """imaplib quotet das Passwort (nicht den Usernamen), Leerzeichen und " müssen ankommen."""
        import imaplib

        user = {**self._SPECIAL_USER, "username": "spezial"}
        with patch("mailserver_mock.imap_server.users.find_user", return_value=user):
            client = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
            try:
                typ, _ = client.login(user["username"], user["password"])
                self.assertEqual("OK", typ)
            finally:
                client.logout()

    def test_login_literal_too_large(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            read_line(r)
            write_line(w, "A1 LOGIN {999999}")
            self.assertEqual("A1 BAD Literal too large", read_line(r))
        finally:
            sock.close()

    def test_login_unbalanced_quote(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            read_line(r)
            write_line(w, 'A1 LOGIN testuser "testpass')
            self.assertEqual("A1 BAD LOGIN syntax", read_line(r))
        finally:
            sock.close()

    def test_list(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            write_line(w, 'A2 LIST "" "*"')
            list_line = read_line(r)
            resp = read_line(r)
            self.assertIn("INBOX", list_line)
            self.assertIn("OK", resp)
        finally:
            sock.close()

    def test_select(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            imap_select(r, w, "A2")
        finally:
            sock.close()

    def test_select_without_auth(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            read_line(r)
            write_line(w, "A1 SELECT INBOX")
            resp = read_line(r)
            self.assertIn("NO", resp)
        finally:
            sock.close()

    def test_search(self):
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "search test body")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            imap_select(r, w, "A2")
            write_line(w, "A3 SEARCH ALL")
            search_line = read_line(r)
            resp = read_line(r)
            self.assertIn("SEARCH", search_line)
            self.assertIn("1", search_line)
            self.assertIn("OK", resp)
        finally:
            sock.close()

    def test_search_unseen_excludes_seen_mail(self):
        """Regressionstest: SEARCH UNSEEN lieferte früher exakt dieselbe Liste
        wie SEARCH ALL, weil die Suchkriterien nie an _handle_search
        durchgereicht wurden (imap_server.py)."""
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "unseen test body")
        smtp_send_mail("<a@b>", "<c@d>", "seen test body")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            imap_select(r, w, "A2")
            write_line(w, r"A3 STORE 2 +FLAGS (\Seen)")
            read_line(r)
            read_line(r)

            write_line(w, "A4 SEARCH ALL")
            all_line = read_line(r)
            read_line(r)
            self.assertIn("1", all_line.split())
            self.assertIn("2", all_line.split())

            write_line(w, "A5 SEARCH UNSEEN")
            unseen_line = read_line(r)
            resp = read_line(r)
            self.assertIn("1", unseen_line.split())
            self.assertNotIn("2", unseen_line.split())
            self.assertIn("OK", resp)
        finally:
            sock.close()

    def test_fetch(self):
        reset_mails()
        smtp_send_mail("<sender@example.com>", "<rcpt@example.com>", "fetch test body")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            imap_select(r, w, "A2")
            write_line(w, "A3 FETCH 1 (FLAGS BODY[])")
            lines = imap_read_until(r, "A3")
            self.assertTrue(any("FETCH" in line for line in lines))
            self.assertIn("OK", lines[-1])
        finally:
            sock.close()

    def test_store(self):
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "store test body")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            imap_select(r, w, "A2")
            write_line(w, r"A3 STORE 1 +FLAGS (\Seen)")
            fetch_line = read_line(r)  # * 1 FETCH (FLAGS (\Seen)) (aktuelle Flags)
            resp = read_line(r)
            self.assertIn(r"\Seen", fetch_line)
            self.assertIn("OK", resp)

            flags = storage.load_flags(1)
            self.assertIn(r"\Seen", flags)
        finally:
            sock.close()

    def test_uid_fetch(self):
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "uid fetch body")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            imap_select(r, w, "A2")
            write_line(w, "A3 UID FETCH 1 (FLAGS BODY[])")
            lines = imap_read_until(r, "A3")
            self.assertIn("OK", lines[-1])
        finally:
            sock.close()

    def test_uid_store(self):
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "uid store body")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            imap_select(r, w, "A2")
            write_line(w, r"A3 UID STORE 1 +FLAGS (\Seen)")
            fetch_line = read_line(r)  # * 1 FETCH (FLAGS (\Seen) UID 1)
            resp = read_line(r)
            self.assertIn(r"\Seen", fetch_line)
            self.assertIn("UID 1", fetch_line)
            self.assertIn("OK", resp)

            flags = storage.load_flags(1)
            self.assertIn(r"\Seen", flags)
        finally:
            sock.close()

    def test_uid_store_silent(self):
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "uid store silent body")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            imap_select(r, w, "A2")
            write_line(w, r"A3 UID STORE 1 +FLAGS.SILENT (\Seen)")
            resp = read_line(r)  # keine unsolicited FETCH-Response, direkt OK
            self.assertIn("OK", resp)

            flags = storage.load_flags(1)
            self.assertIn(r"\Seen", flags)
        finally:
            sock.close()

    def test_fetch_body_marks_seen(self):
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "fetch marks seen body")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            imap_select(r, w, "A2")
            write_line(w, "A3 FETCH 1 (FLAGS BODY[])")
            imap_read_until(r, "A3")

            flags = storage.load_flags(1)
            self.assertIn(r"\Seen", flags)
        finally:
            sock.close()

    def test_fetch_body_peek_does_not_mark_seen(self):
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "fetch peek body")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            imap_select(r, w, "A2")
            write_line(w, "A3 FETCH 1 (FLAGS BODY.PEEK[])")
            imap_read_until(r, "A3")

            flags = storage.load_flags(1)
            self.assertNotIn(r"\Seen", flags)
        finally:
            sock.close()

    def test_idle(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            write_line(w, "A2 IDLE")
            resp = read_line(r)
            self.assertIn("+ idling", resp)
            write_line(w, "DONE")
            resp = read_line(r)
            self.assertIn("OK", resp)
        finally:
            sock.close()

    def test_logout(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            read_line(r)
            write_line(w, "A1 LOGOUT")
            read_line(r)  # * BYE (Verbindung wird beendet)
            resp = read_line(r)
            self.assertIn("OK", resp)
        finally:
            sock.close()

    def test_unknown_command(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            write_line(w, "A2 XYZZY")
            resp = read_line(r)
            self.assertIn("BAD", resp)
        finally:
            sock.close()

    def test_login_too_many(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            read_line(r)
            start = time.time()
            write_line(w, "A1 LOGIN testuser_421 testpass_421")
            resp = read_line(r)
            elapsed = time.time() - start
            self.assertIn("NO", resp)
            self.assertIn("LIMIT", resp)
            self.assertGreaterEqual(elapsed, 0.9)
        finally:
            sock.close()

    def test_login_timeout(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            read_line(r)
            with patch("mailserver_mock.imap_server.time.sleep"):
                write_line(w, "A1 LOGIN testuser_451 testpass_451")
                resp = read_line(r)
            self.assertIn("NO", resp)
            self.assertIn("UNAVAILABLE", resp)
        finally:
            sock.close()

    def test_login_quota_then_select(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            read_line(r)
            write_line(w, "A1 LOGIN testuser_552 testpass_552")
            resp = read_line(r)
            self.assertIn("OK", resp)
            write_line(w, "A2 SELECT INBOX")
            resp = read_line(r)
            self.assertIn("NO", resp)
            self.assertIn("OVERQUOTA", resp)
        finally:
            sock.close()


class IMAPMailboxCommandTests(unittest.TestCase):
    """NOOP, CHECK, EXAMINE, EXPUNGE, CLOSE und die SELECT-Antwortcodes."""

    def test_noop_reports_new_mail(self):
        reset_mails()
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            read_line(r)
            write_line(w, "A1 NOOP")  # auch ohne Login erlaubt
            self.assertEqual("A1 OK NOOP completed", read_line(r))
            write_line(w, "A2 LOGIN testuser testpass")
            read_line(r)
            imap_select(r, w, "A3")
            write_line(w, "A4 NOOP")
            self.assertEqual("A4 OK NOOP completed", read_line(r))

            smtp_send_mail("<a@b>", "<c@d>", "neu")
            write_line(w, "A5 NOOP")
            self.assertEqual(["* 1 EXISTS", "A5 OK NOOP completed"], imap_read_until(r, "A5"))
            write_line(w, "A6 CHECK")
            self.assertEqual("A6 OK CHECK completed", read_line(r))
        finally:
            sock.close()

    def test_noop_reports_external_delete(self):
        reset_mails()
        for i in range(3):
            smtp_send_mail("<a@b>", "<c@d>", f"mail {i}")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            imap_select(r, w, "A2")
            storage.delete_mail(2)
            write_line(w, "A3 NOOP")
            self.assertEqual(["* 2 EXPUNGE", "A3 OK NOOP completed"], imap_read_until(r, "A3"))
        finally:
            sock.close()

    def test_select_reports_uid_metadata(self):
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "eins")
        smtp_send_mail("<a@b>", "<c@d>", "zwei")
        storage.save_flags(1, ["\\Seen"])
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            lines = imap_select(r, w, "A2")
            self.assertIn("* 2 EXISTS", lines)
            self.assertIn("* 0 RECENT", lines)
            self.assertIn("* OK [UNSEEN 2] First unseen", lines)
            self.assertIn(f"* OK [UIDVALIDITY {storage.uid_validity()}] UIDs valid", lines)
            self.assertIn("* OK [UIDNEXT 3] Predicted next UID", lines)
            self.assertTrue(any("PERMANENTFLAGS" in line and "\\*" in line for line in lines))
        finally:
            sock.close()

    def test_uidvalidity_changes_after_reset(self):
        smtp_send_mail("<a@b>", "<c@d>", "x")
        before = storage.uid_validity()
        storage.delete_all()
        self.assertGreater(storage.uid_validity(), before)

    def test_examine_is_read_only(self):
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "nur lesen")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            lines = imap_select(r, w, "A2", cmd="EXAMINE")
            self.assertIn("* OK [PERMANENTFLAGS ()] Flags permitted", lines)
            write_line(w, "A3 FETCH 1 (BODY[])")
            imap_read_until(r, "A3")
            self.assertEqual([], storage.load_flags(1))
            write_line(w, r"A4 STORE 1 +FLAGS (\Deleted)")
            self.assertEqual("A4 NO Mailbox is read-only", read_line(r))
            write_line(w, "A5 EXPUNGE")
            self.assertEqual("A5 NO Mailbox is read-only", read_line(r))
            write_line(w, "A6 CLOSE")
            self.assertEqual("A6 OK CLOSE completed", read_line(r))
            self.assertEqual([1], storage.list_ids())
        finally:
            sock.close()

    def test_expunge_removes_deleted_mails(self):
        reset_mails()
        for i in range(4):
            smtp_send_mail("<a@b>", "<c@d>", f"mail {i}")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            imap_select(r, w, "A2")
            write_line(w, r"A3 STORE 1,3:4 +FLAGS.SILENT (\Deleted)")
            read_line(r)
            write_line(w, "A4 EXPUNGE")
            # Jede EXPUNGE-Antwort verschiebt die folgenden Nummern sofort um eins.
            self.assertEqual(
                ["* 1 EXPUNGE", "* 2 EXPUNGE", "* 2 EXPUNGE", "A4 OK EXPUNGE completed"],
                imap_read_until(r, "A4"),
            )
            self.assertEqual([2], storage.list_ids())
        finally:
            sock.close()
        # Die höchste UID war 4 und wurde gelöscht, trotzdem keine Wiederverwendung.
        smtp_send_mail("<a@b>", "<c@d>", "danach")
        self.assertEqual([2, 5], storage.list_ids())

    def test_close_expunges_silently_and_deselects(self):
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "weg")
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            write_line(w, "A2 EXPUNGE")
            self.assertEqual("A2 BAD No mailbox selected", read_line(r))
            imap_select(r, w, "A3")
            write_line(w, r"A4 STORE 1 +FLAGS.SILENT (\Deleted)")
            read_line(r)
            write_line(w, "A5 CLOSE")
            self.assertEqual("A5 OK CLOSE completed", read_line(r))
            self.assertEqual([], storage.list_ids())
            write_line(w, "A6 CLOSE")
            self.assertEqual("A6 BAD No mailbox selected", read_line(r))
        finally:
            sock.close()

    def test_imaplib_mailbox_commands(self):
        import imaplib

        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "eins")
        smtp_send_mail("<a@b>", "<c@d>", "zwei")
        client = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        try:
            client.login("testuser", "testpass")
            client.select("INBOX")
            self.assertEqual([str(storage.uid_validity()).encode()], client.response("UIDVALIDITY")[1])
            self.assertEqual("OK", client.noop()[0])
            client.store("1", "+FLAGS", r"(\Deleted)")
            typ, data = client.expunge()
            self.assertEqual(("OK", [b"1"]), (typ, data))
            self.assertEqual("OK", client.close()[0])
            self.assertEqual([2], storage.list_ids())
        finally:
            client.logout()


class IMAPSequenceGapTests(unittest.TestCase):
    def test_imap_after_http_delete(self):
        """Regressionstest: Nach DELETE /mails/2 behandelte IMAP die
        Sequenznummern 1..N als UIDs: FETCH lieferte eine leere Mail 2,
        Mail 3 war nicht mehr erreichbar."""
        import imaplib

        reset_mails()
        for i in (1, 2, 3):
            smtp_send_mail("<a@b>", "<c@d>", f"Subject: Mail {i}\r\n\r\nbody {i}")
        status, _, _ = http("/mails/2", "DELETE")
        self.assertEqual(status, 204)

        client = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        try:
            client.login("testuser", "testpass")
            typ, data = client.select("INBOX")
            self.assertEqual(data, [b"2"])

            typ, data = client.fetch("1:*", "(UID BODY.PEEK[])")
            self.assertEqual(typ, "OK")
            parts = [part for part in data if isinstance(part, tuple)]
            self.assertEqual(len(parts), 2)
            self.assertTrue(parts[0][0].startswith(b"1 (UID 1 "))
            self.assertTrue(parts[1][0].startswith(b"2 (UID 3 "))
            self.assertTrue(parts[0][1].endswith(b"body 1\r\n"))
            self.assertTrue(parts[1][1].endswith(b"body 3\r\n"))

            typ, data = client.uid("FETCH", "3", "(BODY.PEEK[])")
            self.assertTrue(data[0][0].startswith(b"2 (UID 3 "))
            typ, data = client.uid("FETCH", "2", "(BODY.PEEK[])")
            self.assertEqual((typ, data), ("OK", [None]))

            typ, data = client.store("2", "+FLAGS", r"(\Seen)")
            self.assertEqual(data, [b"2 (FLAGS (\\Seen))"])
            typ, data = client.uid("STORE", "1:*", "+FLAGS", r"(\Flagged)")
            self.assertEqual(len(data), 2)
            self.assertTrue(data[1].startswith(b"2 (FLAGS"))
            self.assertIn(b"UID 3", data[1])

            self.assertEqual(client.search(None, "ALL")[1], [b"1 2"])
            self.assertEqual(client.uid("SEARCH", "ALL")[1], [b"1 3"])
            self.assertEqual(client.search(None, "SEEN")[1], [b"2"])
            self.assertEqual(client.search(None, "UID", "3")[1], [b"2"])
            self.assertEqual(client.uid("SEARCH", "2")[1], [b"3"])

            typ, data = client.status("INBOX", "(MESSAGES UNSEEN UIDNEXT)")
            self.assertEqual(data, [b'"INBOX" (MESSAGES 2 UNSEEN 1 UIDNEXT 4)'])
        finally:
            client.logout()

        self.assertEqual(storage.load_flags(3), [r"\Seen", r"\Flagged"])
        self.assertEqual(sorted(p.name for p in storage.MAIL_DIR.glob("*.flags")), ["1.flags", "3.flags"])


class IMAPStoreSeqSetTests(unittest.TestCase):
    def test_store_sequence_set_with_imaplib(self):
        """Regressionstest: STORE 1:1 legte früher eine Datei 1:1.flags an und
        antwortete mit "* 1:1 FETCH", woran imaplib abbrach."""
        import imaplib

        reset_mails()
        for i in range(3):
            smtp_send_mail("<a@b>", "<c@d>", f"body {i}")
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
        reset_mails()
        smtp_send_mail("<a@b>", "<c@d>", "body")
        evil = storage.MAIL_DIR.parent.parent / "evil.flags"
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            imap_login(r, w)
            imap_select(r, w, "A2")
            for i, seq in enumerate(["../../evil", "1:1/../x", "0", "-1", "abc"]):
                write_line(w, f"B{i} STORE {seq} FLAGS x")
                self.assertTrue(read_line(r).startswith(f"B{i} BAD"), seq)
            write_line(w, "B9 UID STORE ../../evil FLAGS x")
            self.assertTrue(read_line(r).startswith("B9 BAD"))
        finally:
            sock.close()
        self.assertFalse(evil.exists())
        self.assertEqual([p.name for p in storage.MAIL_DIR.glob("*.flags")], ["1.flags"])

    def test_storage_rejects_non_integer_ids(self):
        for bad in ("1:1", "../../evil", 0, -1, True):
            with self.assertRaises(ValueError):
                storage.load_flags(bad)


class IMAPLimitTests(unittest.TestCase):
    """Timeout und Zeilenlimits."""

    def test_overlong_command_line_is_rejected(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            read_line(r)
            write_line(w, "A1 NOOP " + "x" * (connection.MAX_LINE * 2))
            self.assertEqual("A1 BAD Command line too long", read_line(r))
            write_line(w, "A2 LOGIN testuser testpass")
            self.assertEqual("A2 OK LOGIN completed", read_line(r))
        finally:
            sock.close()

    def test_literals_count_towards_total_limit(self):
        sock, r, w = dial((IMAP_HOST, IMAP_PORT))
        try:
            read_line(r)
            write_line(w, "A1 LOGIN {40000}")
            self.assertTrue(read_line(r).startswith("+"))
            w.write(b"u" * 40000)
            write_line(w, " {40000}")
            self.assertEqual("A1 BAD Literal too large", read_line(r))
        finally:
            sock.close()

    def test_idle_client_is_logged_out(self):
        with patch.object(imap_server.IMAPHandler, "timeout", 0.3):
            sock, r, w = dial((IMAP_HOST, IMAP_PORT))
            try:
                read_line(r)
                self.assertEqual("* BYE Autologout; idle for too long", read_line(r))
                self.assertEqual(b"", r.readline())
            finally:
                sock.close()
