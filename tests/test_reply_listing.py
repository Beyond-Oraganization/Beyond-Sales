"""Żądanie listy odpowiedzi i reguły ocen (czysta logika)."""

from __future__ import annotations

import unittest
from datetime import date

from perno.domain import replies as rp
from perno.domain import reply_listing as rl
from perno.domain import reply_rules as rr
from perno.domain import statuses as st


class ParseTests(unittest.TestCase):
    def test_an_empty_request_means_the_default_tab_without_filters(self):
        request = rl.parse_list_request({})
        self.assertEqual((request.flt, request.page, request.size), (rl.ReplyFilter(), 1, 50))
        self.assertEqual(request.flt.tab, rl.TAB_TODO)

    def test_every_tab_is_accepted_and_an_unknown_one_falls_back(self):
        for tab in rl.TABS:
            self.assertEqual(rl.parse_list_request({"tab": tab}).flt.tab, tab)
        for tab in ("nieznana", "", None, 5, ["all"]):
            self.assertEqual(rl.parse_list_request({"tab": tab}).flt.tab, rl.DEFAULT_TAB)

    def test_filters_are_normalised(self):
        request = rl.parse_list_request({"tab": "all", "q": "  Cennik  OFERTA\tcennik ", "mailboxes": ["Roman@Z3X.pl", "roman@z3x.pl"],
                                         "segments": ["eventy", " driverzy "], "countries": ["PL"], "from": "2026-10-01", "to": "2026-10-08",
                                         "page": 3, "size": 20})
        flt = request.flt
        self.assertEqual((flt.words, flt.mailboxes, flt.segments, flt.countries), (("cennik", "oferta"), ("roman@z3x.pl",), ("eventy", "driverzy"), ("PL",)))
        self.assertEqual((flt.since, flt.until, request.page, request.size), (date(2026, 10, 1), date(2026, 10, 8), 3, 20))

    def test_the_search_is_bounded_and_safe(self):
        words = rl.split_words("a" * 100 + " " + " ".join(f"s{n}" for n in range(20)))
        self.assertEqual((len(words), len(words[0])), (rl.MAX_WORDS, rl.MAX_WORD))
        self.assertEqual(rl.split_words("a\x00b\x1b c"), ("a", "b", "c"))
        self.assertEqual(rl.split_words(""), ())
        self.assertEqual(rl.split_words(None), ())

    def test_bad_types_and_ranges_are_request_errors(self):
        for payload in ({"q": 5}, {"mailboxes": "roman"}, {"mailboxes": [1]}, {"segments": [""]}, {"from": "wczoraj"}, {"to": "2026-13-45"},
                        {"from": "2026-10-09", "to": "2026-10-01"}, {"page": 0}, {"page": True}, {"size": 0}, {"size": 201}, {"size": "10"},
                        {"mailboxes": ["x"] * 201}):
            with self.subTest(payload=payload), self.assertRaises(rl.ReplyRequestError):
                rl.parse_list_request(payload)

    def test_boundaries_of_page_and_size_are_inclusive(self):
        request = rl.parse_list_request({"page": 1, "size": 200})
        self.assertEqual((request.page, request.size), (1, 200))
        self.assertEqual(rl.parse_list_request({"page": 10**6}).page, 10**6)
        self.assertEqual(rl.parse_list_request({"from": "2026-10-01", "to": "2026-10-01"}).flt.since, date(2026, 10, 1))

    def test_the_value_objects_are_immutable_and_every_tab_has_a_label(self):
        with self.assertRaises(AttributeError):
            rl.ReplyFilter().tab = "x"  # type: ignore[misc]
        self.assertEqual(set(rl.TAB_LABELS), set(rl.TABS))


class RatingRuleTests(unittest.TestCase):
    def test_valid_ratings_pass_and_empty_means_clear(self):
        for value in rp.RATINGS:
            self.assertEqual(rr.check_rating(value), value)
        for value in (None, ""):
            self.assertIsNone(rr.check_rating(value))

    def test_anything_else_is_refused(self):
        for value in ("super", "POSITIVE", 1, True, ["positive"]):
            with self.subTest(value=value), self.assertRaises(rr.RuleError):
                rr.check_rating(value)

    def test_notes_are_cleaned_and_cut(self):
        self.assertEqual(rr.check_note("  ok\x00 \x07tak  "), "ok tak")
        self.assertEqual(len(rr.check_note("x" * 300)), rr.MAX_NOTE)
        self.assertEqual(rr.check_note(None), "")
        with self.assertRaises(rr.RuleError):
            rr.check_note(5)

    def test_ai_may_not_overwrite_a_human_rating_but_may_replace_its_own(self):
        self.assertFalse(rr.ai_may_rate(rp.BY_HUMAN))
        self.assertTrue(rr.ai_may_rate(rp.BY_AI))
        self.assertTrue(rr.ai_may_rate(None))


class SuggestionTests(unittest.TestCase):
    def test_an_opt_out_suggests_do_not_contact_whatever_the_rating(self):
        for rating in (None, rp.RATING_NEGATIVE, rp.RATING_POSITIVE):
            self.assertEqual(rr.status_suggestion(rating, True, st.STATUS_REPLIED), st.STATUS_DO_NOT_CONTACT)
        self.assertIsNone(rr.status_suggestion(rp.RATING_NEGATIVE, True, st.STATUS_DO_NOT_CONTACT))

    def test_a_positive_rating_suggests_interested_only_from_sent_or_replied(self):
        for status in (st.STATUS_SENT, st.STATUS_REPLIED):
            self.assertEqual(rr.status_suggestion(rp.RATING_POSITIVE, False, status), st.STATUS_INTERESTED)
        for status in (st.STATUS_INTERESTED, st.STATUS_DO_NOT_CONTACT, st.STATUS_PENDING, st.STATUS_BOUNCED):
            self.assertIsNone(rr.status_suggestion(rp.RATING_POSITIVE, False, status))

    def test_neutral_negative_and_unrated_suggest_nothing_without_an_opt_out(self):
        for rating in (None, rp.RATING_NEUTRAL, rp.RATING_NEGATIVE):
            self.assertIsNone(rr.status_suggestion(rating, False, st.STATUS_REPLIED))

    def test_only_three_statuses_can_be_set_from_replies(self):
        for code in rr.SETTABLE_STATUSES:
            self.assertEqual(rr.check_settable_status(code), code)
        for code in (st.STATUS_PENDING, st.STATUS_SENT, st.STATUS_BOUNCED, "wymyslony", None):
            with self.subTest(code=code), self.assertRaises(rr.RuleError):
                rr.check_settable_status(code)


if __name__ == "__main__":
    unittest.main()
