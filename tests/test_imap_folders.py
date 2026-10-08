"""Role folderów IMAP i wybór folderów do przeszukania (czysta logika)."""

from __future__ import annotations

import unittest

from perno.domain import imap_folders as f


class ClassifyTests(unittest.TestCase):
    def test_special_use_flags_win_over_names(self):
        self.assertEqual(f.classify("Moje wysłane rzeczy", ["\\HasNoChildren", "\\Sent"]), f.ROLE_SENT)
        self.assertEqual(f.classify("Sent", ["\\Junk"]), f.ROLE_JUNK)  # flaga mówi więcej niż nazwa
        self.assertEqual(f.classify("X", ["\\SENT"]), f.ROLE_SENT)

    def test_names_are_the_fallback_in_several_languages(self):
        for name in ("Sent", "sent items", "Wysłane", "Elementy wysłane", "Gesendet", "Enviados", "INBOX.Sent", "INBOX/Wysłane"):
            with self.subTest(name=name):
                self.assertEqual(f.classify(name, [], "." if "." in name else "/"), f.ROLE_SENT)
        for name in ("Junk", "Spam", "Junk E-mail", "INBOX.Spam"):
            with self.subTest(name=name):
                self.assertEqual(f.classify(name, [], "."), f.ROLE_JUNK)

    def test_the_inbox_is_recognised_in_any_case_and_other_folders_have_no_role(self):
        self.assertEqual(f.classify("INBOX"), f.ROLE_INBOX)
        self.assertEqual(f.classify("inbox"), f.ROLE_INBOX)
        for name in ("Drafts", "Trash", "Faktury", "INBOX.Faktury"):
            self.assertEqual(f.classify(name, [], "."), f.ROLE_OTHER, name)


    def test_a_server_without_a_hierarchy_delimiter_still_gets_roles_from_names(self):
        self.assertEqual(f.classify("Sent", [], ""), f.ROLE_SENT)
        self.assertEqual(f.classify("Sent", [], "."), f.ROLE_SENT)
        self.assertEqual(f.classify("Archiwum.Sent", [], "."), f.ROLE_SENT)
        self.assertEqual(f.classify("Archiwum.Sent", [], ""), f.ROLE_OTHER)  # bez separatora nazwa jest jedną całością

    def test_a_folder_record_is_immutable(self):
        with self.assertRaises(AttributeError):
            f.Folder("INBOX").name = "x"  # type: ignore[misc]


class ParseListTests(unittest.TestCase):
    def test_a_typical_line(self):
        self.assertEqual(f.parse_list_line('(\\HasNoChildren \\Sent) "." "Sent"'), ("Sent", ("\\HasNoChildren", "\\Sent"), "."))

    def test_unquoted_names_nil_delimiters_and_escaped_quotes(self):
        self.assertEqual(f.parse_list_line('(\\HasNoChildren) "/" INBOX'), ("INBOX", ("\\HasNoChildren",), "/"))
        self.assertEqual(f.parse_list_line('() NIL "Płaski"'), ("Płaski", (), ""))
        self.assertEqual(f.parse_list_line('() "." "a \\"b\\""'), ('a "b"', (), "."))

    def test_degenerate_quoted_names(self):
        self.assertEqual(f.parse_list_line('() "." ""')[0], "")
        self.assertEqual(f.parse_list_line('() "." "x"')[0], "x")
        self.assertEqual(f.parse_list_line('() "." "')[0], '"')

    def test_garbage_is_ignored(self):
        for line in ("", "* OK", "(\\Sent)"):
            self.assertIsNone(f.parse_list_line(line), line)


class DefaultFoldersTests(unittest.TestCase):
    def folders(self, *items):
        return [f.Folder(name, tuple(flags), f.classify(name, flags)) for name, flags in items]

    def test_inbox_sent_and_junk_in_that_order(self):
        listing = self.folders(("Drafts", ["\\Drafts"]), ("Junk", ["\\Junk"]), ("Sent", ["\\Sent"]), ("INBOX", []), ("Trash", ["\\Trash"]))
        self.assertEqual(f.default_folders(listing), ["INBOX", "Sent", "Junk"])

    def test_unselectable_and_duplicate_roles_are_skipped(self):
        listing = self.folders(("INBOX", []), ("Sent", ["\\Sent", "\\Noselect"]), ("Sent Items", []), ("Wysłane", ["\\Sent"]))
        self.assertEqual(f.default_folders(listing), ["INBOX", "Sent Items"])

    def test_a_mailbox_without_special_folders_still_has_its_inbox(self):
        self.assertEqual(f.default_folders(self.folders(("INBOX", []))), ["INBOX"])
        self.assertEqual(f.default_folders([]), [])


class SelectionTests(unittest.TestCase):
    def test_names_are_split_trimmed_and_deduplicated(self):
        self.assertEqual(f.parse_selection(" INBOX , Wysłane,INBOX,, "), ["INBOX", "Wysłane"])
        self.assertEqual(f.parse_selection(""), [])


if __name__ == "__main__":
    unittest.main()
