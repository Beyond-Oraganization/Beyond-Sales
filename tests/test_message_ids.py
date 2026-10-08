"""Identyfikatory Message-ID: własne, porównywalne i wyciągane z nagłówków odpowiedzi."""

from __future__ import annotations

import re
import unittest

from perno.domain import message_ids as mid


class NewMessageIdTests(unittest.TestCase):
    def test_it_lives_in_the_senders_domain_and_has_angle_brackets(self):
        value = mid.new_message_id("Roman@Z3X.pl")
        self.assertTrue(re.fullmatch(r"<[^<>@\s]+@z3x\.pl>", value), value)

    def test_every_identifier_is_unique(self):
        self.assertEqual(len({mid.new_message_id("a@b.pl") for _ in range(200)}), 200)

    def test_a_sender_without_a_domain_still_gets_an_identifier(self):
        self.assertRegex(mid.new_message_id("bez-malpy"), r"^<[^<>\s]+@[^<>\s]+>$")

    def test_a_generated_identifier_fits_the_database_column(self):
        self.assertLessEqual(len(mid.key(mid.new_message_id("roman@bardzo-dluga-domena-poczty.example.com"))), mid.MAX_KEY)
        self.assertEqual(mid.MAX_KEY, 255)  # tyle ma kolumna maile.message_id


class KeyTests(unittest.TestCase):
    def test_brackets_and_case_are_dropped(self):
        self.assertEqual(mid.key("<AbC.1@Mail.Example.COM>"), "abc.1@mail.example.com")
        self.assertEqual(mid.key("  <x@y>  "), "x@y")

    def test_a_bare_identifier_is_accepted_and_junk_is_not(self):
        self.assertEqual(mid.key("x@y.pl"), "x@y.pl")
        for junk in ("", "   ", "<>", "<a b@c>", "nie ma spacji w ID"):
            with self.subTest(junk=junk):
                self.assertEqual(mid.key(junk), "")

    def test_it_matches_what_the_sender_stored(self):
        generated = mid.new_message_id("roman@z3x.pl")
        self.assertEqual(mid.key(generated.upper()), mid.key(generated))


class KeysInTests(unittest.TestCase):
    def test_references_come_in_header_order_without_repeats(self):
        header = "<A@x.pl>\r\n <b@x.pl> <a@X.pl> <c@x.pl>"
        self.assertEqual(mid.keys_in(header), ["a@x.pl", "b@x.pl", "c@x.pl"])

    def test_missing_or_garbled_headers_give_nothing(self):
        for header in ("", None, "bez nawiasów", "<>", "<a b@c>"):
            with self.subTest(header=header):
                self.assertEqual(mid.keys_in(header), [])  # type: ignore[arg-type]

    def test_a_single_in_reply_to(self):
        self.assertEqual(mid.keys_in("<Odp.123@serwer.pl>"), ["odp.123@serwer.pl"])


class DigestTests(unittest.TestCase):
    def test_it_is_a_fixed_width_hex_string_that_differs_per_identifier(self):
        first, second = mid.digest("a@x.pl"), mid.digest("b@x.pl")
        self.assertRegex(first, r"^[0-9a-f]{40}$")
        self.assertNotEqual(first, second)
        self.assertEqual(first, mid.digest("a@x.pl"))

    def test_very_long_identifiers_still_fit(self):
        self.assertEqual(len(mid.digest("x" * 2000 + "@y.pl")), 40)


if __name__ == "__main__":
    unittest.main()
