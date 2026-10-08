"""Klient IMAP na atrapie połączenia: tylko odczyt, poprawne cytowanie folderów, porcjowanie i mapowanie błędów."""

from __future__ import annotations

import imaplib
import unittest
from datetime import date, datetime

from perno.domain import imap_folders
from perno.mailer import imap_client as ic
from tests.fake_imap import WRITE_COMMANDS, FakeConnection, auth_failure


def client(conn: FakeConnection, **kw) -> ic.ImapClient:
    return ic.ImapClient("imap.example.com", 993, "roman@z3x.pl", "tajne-haslo", connect=lambda host, port: conn, **kw)


class FolderNameTests(unittest.TestCase):
    VECTORS = (("Wysłane", "Wys&AUI-ane"), ("INBOX.Sent", "INBOX.Sent"), ("R&D", "R&-D"), ("Śmieci", "&AVo-mieci"),
               ("a/b c", "a/b c"), ("日本語", "&ZeVnLIqe-"))

    def test_known_vectors_from_the_rfc_style_encoding(self):
        for name, wire in self.VECTORS:
            with self.subTest(name=name):
                self.assertEqual(ic.encode_folder(name), wire)
                self.assertEqual(ic.decode_folder(wire), name)

    def test_characters_outside_the_bmp_survive_a_round_trip(self):
        for name in ("Poczta 📧", "&&&", "Zażółć gęślą jaźń", "a&b&c", "&"):
            self.assertEqual(ic.decode_folder(ic.encode_folder(name)), name)

    def test_a_broken_sequence_is_left_alone(self):
        self.assertEqual(ic.decode_folder("Abc&!!!-def"), "Abc&!!!-def")


class SearchHelpersTests(unittest.TestCase):
    def test_dates_use_english_months_whatever_the_locale(self):
        self.assertEqual(ic.imap_date(date(2026, 10, 8)), "08-Oct-2026")
        self.assertEqual(ic.imap_date(date(2026, 1, 31)), "31-Jan-2026")
        self.assertEqual(ic.imap_date(date(2026, 12, 1)), "01-Dec-2026")

    def test_criteria_prefer_the_cursor_then_the_start_date(self):
        self.assertEqual(ic.search_criteria(date(2026, 9, 29), 120), "UID 121:*")
        self.assertEqual(ic.search_criteria(date(2026, 9, 29), 0), "SINCE 29-Sep-2026")
        self.assertEqual(ic.search_criteria(None, 0), "ALL")

    def test_fetch_replies_are_parsed_with_the_uid_in_either_place(self):
        usual = [(b"1 (UID 41 BODY[HEADER.FIELDS (FROM)] {20}", b"From: a@x.pl\r\n\r\n"), b")",
                 (b"2 (UID 42 BODY[HEADER.FIELDS (FROM)] {20}", b"From: b@x.pl\r\n\r\n"), b")"]
        self.assertEqual(ic.parse_fetch(usual), {41: b"From: a@x.pl\r\n\r\n", 42: b"From: b@x.pl\r\n\r\n"})
        after_literal = [(b"1 (BODY[] {5}", b"hello"), b" UID 77)", (b"2 (BODY[] {3}", b"abc"), b" UID 78)"]
        self.assertEqual(ic.parse_fetch(after_literal), {77: b"hello", 78: b"abc"})
        self.assertEqual(ic.parse_fetch([None, b")"]), {})  # type: ignore[list-item]


class ReadOnlyTests(unittest.TestCase):
    def test_selecting_examines_the_folder_read_only_with_an_encoded_quoted_name(self):
        conn = FakeConnection(exists=b"12", uidvalidity=b"555", uidnext=b"901")
        with client(conn) as imap:
            state = imap.select("Wysłane")
        self.assertEqual(state, ic.FolderState(555, 901, 12))
        self.assertIn(("select", '"Wys&AUI-ane"', True), conn.calls)

    def test_listing_gives_decoded_names_with_roles(self):
        conn = FakeConnection(list_lines=[b'(\\HasNoChildren) "." "INBOX"', b'(\\HasNoChildren \\Sent) "." "Wys&AUI-ane"',
                                          b'(\\HasNoChildren \\Junk) "." Junk', b"garbage"])
        with client(conn) as imap:
            folders = imap.list_folders()
        self.assertEqual([(x.name, x.role) for x in folders],
                         [("INBOX", "inbox"), ("Wysłane", "sent"), ("Junk", "junk")])
        self.assertEqual(imap_folders.default_folders(folders), ["INBOX", "Wysłane", "Junk"])

    def test_search_drops_the_one_old_uid_the_server_returns_for_a_star_range(self):
        conn = FakeConnection(search_result=b"90")  # serwer zwraca ostatnią wiadomość mimo UID 121:*
        with client(conn) as imap:
            self.assertEqual(imap.search_uids(after_uid=120), [])
        conn = FakeConnection(search_result=b"130 121 125")
        with client(conn) as imap:
            self.assertEqual(imap.search_uids(after_uid=120), [121, 125, 130])
        self.assertIn(("uid", "SEARCH", None, "UID 121:*"), conn.calls)

    def test_first_search_starts_at_the_given_day(self):
        conn = FakeConnection(search_result=b"1 2")
        with client(conn) as imap:
            self.assertEqual(imap.search_uids(since=date(2026, 9, 29)), [1, 2])
        self.assertIn(("uid", "SEARCH", None, "SINCE 29-Sep-2026"), conn.calls)

    def test_headers_are_fetched_in_chunks_with_peek_and_only_the_needed_fields(self):
        reply = [(f"{n} (UID {n} BODY[HEADER.FIELDS (FROM)] {{5}}".encode(), b"h" + str(n).encode()) for n in range(1, 4)]
        conn = FakeConnection(fetch_reply=reply)
        with client(conn) as imap:
            imap.fetch_headers(list(range(1, 251)))
        fetches = [call for call in conn.calls if call[:2] == ("uid", "FETCH")]
        self.assertEqual(len(fetches), 3)  # 100 + 100 + 50
        self.assertTrue(all("BODY.PEEK[HEADER.FIELDS (" in call[3] for call in fetches))
        self.assertTrue(all("MESSAGE-ID" in call[3] and "IN-REPLY-TO" in call[3] for call in fetches))
        self.assertEqual(fetches[0][2].count(","), 99)

    def test_a_full_message_is_peeked_and_a_missing_one_is_an_error(self):
        conn = FakeConnection(fetch_reply=[(b"1 (UID 9 BODY[] {5}", b"mail!"), b")"])
        with client(conn) as imap:
            self.assertEqual(imap.fetch_message(9), b"mail!")
            with self.assertRaises(ic.ImapError):
                imap.fetch_message(10)
        self.assertIn(("uid", "FETCH", "9", "(UID BODY.PEEK[])"), conn.calls)

    def test_reading_never_issues_a_command_that_changes_the_server(self):
        conn = FakeConnection(search_result=b"5", fetch_reply=[(b"1 (UID 5 BODY[] {1}", b"x"), b")"])
        with client(conn) as imap:
            imap.list_folders()
            imap.select("INBOX")
            imap.search_uids(after_uid=1)
            imap.fetch_headers([5])
            imap.fetch_message(5)
        self.assertFalse(set(conn.commands()) & WRITE_COMMANDS, conn.commands())
        self.assertNotIn("append", conn.commands())


class AppendTests(unittest.TestCase):
    def test_a_copy_goes_to_the_named_folder_marked_as_read(self):
        conn = FakeConnection()
        with client(conn) as imap:
            imap.append("Wysłane", b"Subject: x\r\n\r\nbody", datetime(2026, 10, 8, 12, 0))
        call = next(c for c in conn.calls if c[0] == "append")
        self.assertEqual((call[1], call[2], call[3]), ('"Wys&AUI-ane"', "(\\Seen)", b"Subject: x\r\n\r\nbody"))


class ErrorTests(unittest.TestCase):
    def test_a_rejected_login_is_an_auth_error_and_never_echoes_the_password(self):
        conn = FakeConnection(login_error=auth_failure())
        with self.assertRaises(ic.ImapError) as ctx, client(conn):
            pass
        self.assertEqual(ctx.exception.kind, ic.KIND_AUTH)
        self.assertNotIn("tajne-haslo", str(ctx.exception))
        self.assertIn(("logout",), conn.calls)

    def test_a_network_failure_is_reported_as_such(self):
        conn = FakeConnection(login_error=ConnectionResetError("połączenie zerwane"))
        with self.assertRaises(ic.ImapError) as ctx, client(conn):
            pass
        self.assertEqual(ctx.exception.kind, ic.KIND_NETWORK)

    def test_a_refused_command_is_a_protocol_error(self):
        conn = FakeConnection(status="NO")
        with client(conn) as imap, self.assertRaises(ic.ImapError) as ctx:
            imap.select("INBOX")
        self.assertEqual(ctx.exception.kind, ic.KIND_PROTOCOL)

    def test_a_dropped_connection_while_reading_is_a_network_error(self):
        class Dropping(FakeConnection):
            def uid(self, command, *args):
                raise TimeoutError("timed out")

        with client(Dropping()) as imap, self.assertRaises(ic.ImapError) as ctx:
            imap.search_uids()
        self.assertEqual(ctx.exception.kind, ic.KIND_NETWORK)

    def test_the_connection_is_closed_even_when_the_block_fails(self):
        conn = FakeConnection()
        with self.assertRaises(RuntimeError), client(conn):
            raise RuntimeError("w bloku")
        self.assertEqual(conn.calls[-1], ("logout",))


class GuardTests(unittest.TestCase):
    def test_tests_cannot_reach_a_real_imap_server_by_accident(self):
        with self.assertRaisesRegex(RuntimeError, "prawdziwe połączenie IMAP"):
            imaplib.IMAP4_SSL("imap.example.com", 993)
        with self.assertRaisesRegex(RuntimeError, "prawdziwe połączenie IMAP"):
            ic.ImapClient("imap.example.com", 993, "u", "p", True).__enter__()
        with self.assertRaisesRegex(RuntimeError, "prawdziwe połączenie IMAP"):
            ic.ImapClient("imap.example.com", 143, "u", "p", False).__enter__()

    def test_the_guard_keeps_the_exception_classes_the_client_relies_on(self):
        self.assertTrue(issubclass(imaplib.IMAP4.error, Exception))


if __name__ == "__main__":
    unittest.main()
