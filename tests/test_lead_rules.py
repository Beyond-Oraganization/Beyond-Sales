"""Reguły leadów: oczyszczanie i kształt wartości, uprawnienia AI i człowieka, pochodzenie pól, kompletność i parsowanie listy."""

from __future__ import annotations

import unittest

from perno.domain import lead_listing as ll
from perno.domain import lead_rules as lr
from perno.domain.reply_rules import RuleError


class CleanTextTests(unittest.TestCase):
    def test_whitespace_and_control_characters_are_collapsed_and_none_is_empty(self):
        self.assertEqual(lr.clean_text(lr.CONTACT, "  Anna \t Nowak\x00,\n dyrektor "), "Anna Nowak , dyrektor")
        self.assertEqual(lr.clean_text(lr.ABOUT, None), "")

    def test_only_the_note_keeps_line_breaks(self):
        self.assertEqual(lr.clean_text(lr.NOTE, "a  b\r\n\r\n c  "), "a b\n\nc")
        self.assertNotIn("\n", lr.clean_text(lr.ABOUT, "a\nb"))

    def test_a_value_over_the_limit_is_refused_not_cut(self):
        self.assertEqual(len(lr.clean_text(lr.ABOUT, "x" * lr.LIMITS[lr.ABOUT])), lr.LIMITS[lr.ABOUT])
        with self.assertRaisesRegex(RuleError, "najwyżej 400"):
            lr.clean_text(lr.ABOUT, "x" * 401)

    def test_every_field_has_its_exact_limit(self):
        for field, limit in lr.LIMITS.items():
            with self.subTest(field=field):
                self.assertEqual(len(lr.clean_text(field, "x" * limit)), limit)
                with self.assertRaisesRegex(RuleError, f"najwyżej {limit} znaków"):
                    lr.clean_text(field, "x" * (limit + 1))
        self.assertEqual(lr.LIMITS, {"name": 255, "website": 500, "contact": 255, "phone": 64, "email": 320, "about": 400, "note": 2000})

    def test_a_non_text_value_is_refused(self):
        for bad in (5, ["a"], {"a": 1}, True):
            with self.assertRaisesRegex(RuleError, "musi być tekstem"):
                lr.clean_text(lr.NAME, bad)


class ShapeTests(unittest.TestCase):
    def test_email_is_lowercased_and_a_bad_shape_refused(self):
        self.assertEqual(lr.check_email("Anna.Nowak@Firma.PL"), "anna.nowak@firma.pl")
        self.assertEqual(lr.check_email(""), "")
        for bad in ("anna", "anna@", "@firma.pl", "a b@firma.pl", "a@firma", "a@b@c.pl", "<a@firma.pl>", "a@firma.pl, b@firma.pl"):
            with self.assertRaises(RuleError, msg=bad):
                lr.check_email(bad)

    def test_phone_needs_seven_digits_and_only_phone_characters(self):
        self.assertEqual(lr.check_phone("+48 600 100 200"), "+48 600 100 200")
        self.assertEqual(lr.check_phone("(012) 345-67-89"), "(012) 345-67-89")
        self.assertEqual(lr.check_phone(""), "")
        self.assertEqual(lr.check_phone("1234567"), "1234567", "siedem cyfr wystarcza")
        with self.assertRaises(RuleError):
            lr.check_phone("123456")
        for bad in ("12345", "tel. 600100200", "600100200x5", "+++"):
            with self.assertRaises(RuleError, msg=bad):
                lr.check_phone(bad)

    def test_website_gets_a_scheme_and_only_http_with_a_domain_passes(self):
        self.assertEqual(lr.check_website("firma.pl/oferta"), "https://firma.pl/oferta")
        self.assertEqual(lr.check_website("HTTP://Firma.PL:8080/a?b=1"), "HTTP://Firma.PL:8080/a?b=1")
        self.assertEqual(lr.check_website(""), "")
        for bad in ("javascript:alert(1)", "ftp://firma.pl", "foo", "https://a b.pl", "https://user@firma.pl", "https://localhost"):
            with self.assertRaises(RuleError, msg=bad):
                lr.check_website(bad)

    def test_stage_must_be_known(self):
        self.assertEqual(lr.check_stage("won"), "won")
        for bad in ("", None, "WON", "inny"):
            with self.assertRaises(RuleError):
                lr.check_stage(bad)


class ChangesTests(unittest.TestCase):
    def test_a_human_may_change_every_editable_field(self):
        changes = {name: "x@firma.pl" if name == lr.EMAIL else "https://firma.pl" if name == lr.WEBSITE else "+48 600 100 200" if name == lr.PHONE
                   else "talking" if name == lr.STAGE else "tekst" for name in lr.EDITABLE_FIELDS}
        self.assertEqual(set(lr.check_changes(changes, by=lr.BY_HUMAN)), set(lr.EDITABLE_FIELDS))

    def test_the_ai_may_write_only_its_fields(self):
        self.assertEqual(lr.check_changes({"about": "Wynajem limuzyn.", "email": "A@B.PL"}, by=lr.BY_AI), {"about": "Wynajem limuzyn.", "email": "a@b.pl"})
        for forbidden in ("name", "website", "note", "stage"):
            with self.assertRaisesRegex(RuleError, "niedozwolone"):
                lr.check_changes({forbidden: "x"}, by=lr.BY_AI)

    def test_unknown_fields_and_empty_changes_are_errors(self):
        for bad in ({}, None, [], {"zzz": "x"}, "about"):
            with self.assertRaises(RuleError):
                lr.check_changes(bad, by=lr.BY_HUMAN)


class ProvenanceTests(unittest.TestCase):
    def test_src_round_trips_and_ignores_damage(self):
        self.assertEqual(lr.parse_src(lr.dump_src({"about": "ai", "email": "human"})), {"about": "ai", "email": "human"})
        self.assertIsNone(lr.dump_src({}))
        for damaged in (None, "", "{", "[]", '{"about": "robot", "zzz": "ai"}'):
            self.assertEqual(lr.parse_src(damaged), {})

    def test_the_ai_does_not_overwrite_a_human_but_fills_blanks_and_its_own(self):
        self.assertTrue(lr.ai_may_write({}, "email"))
        self.assertTrue(lr.ai_may_write({"email": "ai"}, "email"))
        self.assertFalse(lr.ai_may_write({"email": "human"}, "email"))
        self.assertFalse(lr.ai_may_write({}, "note"))
        self.assertFalse(lr.ai_may_write({}, "name"))

    def test_a_write_sets_the_author_and_clearing_returns_the_field_to_the_base(self):
        merged = lr.merge_src({"about": "ai"}, {"about": "", "email": "a@b.pl", "stage": "won"}, lr.BY_HUMAN)
        self.assertEqual(merged, {"email": "human"})
        self.assertEqual(lr.merge_src({}, {"contact": "Anna"}, lr.BY_AI), {"contact": "ai"})

    def test_src_is_stored_as_compact_sorted_json(self):
        self.assertEqual(lr.dump_src({"contact": "human", "about": "ai"}), '{"about":"ai","contact":"human"}')

    def test_pending_lists_ai_fields_in_the_canonical_order(self):
        self.assertEqual(lr.pending_fields({"about": "ai", "email": "human", "contact": "ai"}), ["contact", "about"])


class CompletenessTests(unittest.TestCase):
    def test_a_lead_is_complete_with_the_five_required_fields_and_the_phone_does_not_count(self):
        full = {"name": "A", "website": "https://a.pl", "contact": "Anna", "email": "a@a.pl", "about": "x", "phone": ""}
        self.assertEqual((lr.completeness(full), lr.missing_fields(full)), (5, []))
        self.assertEqual(lr.missing_fields({**full, "contact": " ", "about": None}), ["contact", "about"])
        self.assertEqual(lr.completeness({}), 0)

    def test_a_sender_name_is_a_contact_only_when_it_looks_like_a_person(self):
        self.assertTrue(lr.looks_like_person("Anna Nowak", "MyLimo"))
        for bad in ("", "anna@mylimo.pl", "Biuro24", "mylimo", "  MyLimo ", "x" * 81):
            self.assertFalse(lr.looks_like_person(bad, "MyLimo"), bad)
        self.assertTrue(lr.looks_like_person("a" * 80, "MyLimo"))
        self.assertFalse(lr.looks_like_person("a" * 81, "MyLimo"))
        self.assertFalse(lr.looks_like_person("BusCharter", "BusCharter Śląsk"), "nazwa będąca częścią nazwy firmy to marka")
        self.assertFalse(lr.looks_like_person("MyLimo Wynajem Limuzyn Sp. z o.o.", "MyLimo"), "nazwa rozszerzająca nazwę firmy to marka")
        self.assertTrue(lr.looks_like_person("Piotr Taxi", "Taxi Kraków Express"))


class ListingRequestTests(unittest.TestCase):
    def test_defaults_and_unknown_values_fall_back(self):
        request = ll.parse_list_request({"tab": "zzz", "sort": "zzz"})
        self.assertEqual((request.flt.tab, request.flt.sort, request.page, request.size), (ll.TAB_ALL, ll.SORT_RECENT, 1, ll.DEFAULT_PAGE_SIZE))

    def test_words_segments_and_stages_are_validated(self):
        request = ll.parse_list_request({"tab": "missing", "q": "  Anna  Nowak ", "segments": ["eventy"], "stages": ["won"], "sort": "name",
                                         "page": 2, "size": 10})
        self.assertEqual((request.flt.tab, request.flt.words, request.flt.segments, request.flt.stages, request.flt.sort, request.page, request.size),
                         ("missing", ("anna", "nowak"), ("eventy",), ("won",), "name", 2, 10))

    def test_bad_input_is_a_request_error_not_a_silent_no_filter(self):
        for payload in ({"stages": ["zły"]}, {"stages": "won"}, {"segments": [1]}, {"q": 5}, {"page": 0}, {"size": 10**6}, {"page": True}):
            with self.assertRaises(ValueError, msg=str(payload)):
                ll.parse_list_request(payload)
