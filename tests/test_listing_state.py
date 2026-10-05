"""Stan listy z przeglądarki → filtr i sortowanie: wszystko, czego serwer nie rozumie, znaczy „bez ograniczenia”."""

from __future__ import annotations

import unittest
from datetime import date

from perno.domain.listing import (
    DEFAULT_SORT,
    MAX_LIST_ITEMS,
    MAX_QUERY_LENGTH,
    MAX_SEARCH_TERMS,
    SORT_KEYS,
    parse_state,
    without_facet,
)
from perno.domain.rowfilter import ALL_ROWS, RowFilter
from perno.domain.search import SearchTerms


class StateParsingTests(unittest.TestCase):
    def test_anything_that_is_not_an_object_is_an_empty_state(self):
        for payload in (None, [], "q=x", 5, True):
            query = parse_state(payload)
            self.assertEqual((query.flt, query.sort), (ALL_ROWS, DEFAULT_SORT), payload)

    def test_a_complete_state_maps_to_one_filter(self):
        query = parse_state({
            "q": '"taxi berlin" -pies', "qFields": "company", "statuses": ["wyslano", "blad"], "segments": ["eventy"],
            "countries": ["PL", "_foreign"], "email": "yes", "body": "no", "city": "yes", "address": "no", "sent": "yes",
            "origin": "import", "sentFrom": "2026-09-01", "sentTo": "2026-09-30", "generatedFrom": "2026-08-01",
            "generatedTo": "2026-08-31", "sort": "-sent_at"})
        self.assertEqual(query.sort, "-sent_at")
        self.assertEqual(query.flt, RowFilter(
            segments=("eventy",), statuses=("wyslano", "blad"), countries=("PL", "_foreign"), has_email=True, has_body=False,
            has_city=True, has_address=False, has_sent=True, origin="import", sent_from=date(2026, 9, 1),
            sent_to=date(2026, 9, 30), generated_from=date(2026, 8, 1), generated_to=date(2026, 8, 31),
            search=SearchTerms(("taxi berlin",), ("pies",), "company")))

    def test_lists_keep_only_distinct_short_non_empty_strings_up_to_the_limit(self):
        flt = parse_state({"statuses": ["a", "a", "", 5, None, "x" * 65, "b", "y" * 64]}).flt
        self.assertEqual(flt.statuses, ("a", "b", "y" * 64))  # 64 znaki jeszcze przechodzą, 65 już nie
        many = [f"s{n}" for n in range(MAX_LIST_ITEMS + 20)]
        self.assertEqual(len(parse_state({"segments": many}).flt.segments), MAX_LIST_ITEMS)
        self.assertEqual(parse_state({"statuses": "blad", "countries": {"PL": 1}}).flt, ALL_ROWS)

    def test_three_way_fields_accept_only_yes_and_no(self):
        flt = parse_state({"email": "maybe", "body": 1, "city": True, "address": None, "sent": "YES"}).flt
        self.assertEqual((flt.has_email, flt.has_body, flt.has_city, flt.has_address, flt.has_sent), (None,) * 5)

    def test_days_must_be_real_calendar_dates(self):
        flt = parse_state({"sentFrom": "2026-13-45", "sentTo": "2026-9-1", "generatedFrom": 20260901, "generatedTo": "2026-02-29"}).flt
        self.assertEqual((flt.sent_from, flt.sent_to, flt.generated_from, flt.generated_to), (None,) * 4)
        self.assertEqual(parse_state({"sentFrom": "2024-02-29"}).flt.sent_from, date(2024, 2, 29))

    def test_search_is_cut_to_the_maximum_length_and_blank_text_is_no_search(self):
        self.assertIsNone(parse_state({"q": "   "}).flt.search)
        self.assertIsNone(parse_state({"q": '"" -""'}).flt.search)
        search = parse_state({"q": "a" * (MAX_QUERY_LENGTH + 50)}).flt.search
        self.assertEqual(len(search.include[0]), MAX_QUERY_LENGTH)
        self.assertEqual(parse_state({"q": "x", "qFields": "nie-ma"}).flt.search.field, "all")
        self.assertIsNone(parse_state({"q": 7}).flt.search)

    def test_a_search_with_many_words_keeps_only_the_first_ones(self):
        words = " ".join(f"s{n}" for n in range(MAX_SEARCH_TERMS + 15))
        search = parse_state({"q": words}).flt.search
        self.assertEqual(len(search.include), MAX_SEARCH_TERMS)
        self.assertEqual(search.include[0], "s0")
        excluded = parse_state({"q": " ".join(f"-s{n}" for n in range(MAX_SEARCH_TERMS + 15))}).flt.search
        self.assertEqual((len(excluded.include), len(excluded.exclude)), (0, MAX_SEARCH_TERMS))
        exact = parse_state({"q": " ".join(f"s{n}" for n in range(MAX_SEARCH_TERMS))}).flt.search
        self.assertEqual(len(exact.include), MAX_SEARCH_TERMS)

    def test_unknown_origin_and_sort_fall_back_to_the_defaults(self):
        self.assertEqual(parse_state({"origin": "zewnetrzne"}).flt.origin, "")
        for sort in ("nie_ma", "-nie_ma", "COMPANY", "company;drop", "", "-", 7, ["company"]):
            self.assertEqual(parse_state({"sort": sort}).sort, DEFAULT_SORT, sort)
        for key in SORT_KEYS:
            self.assertEqual(parse_state({"sort": key}).sort, key)
            self.assertEqual(parse_state({"sort": "-" + key}).sort, "-" + key)

    def test_a_counter_ignores_only_its_own_filter(self):
        flt = parse_state({"statuses": ["blad"], "segments": ["eventy"], "countries": ["PL"], "email": "yes"}).flt
        self.assertEqual((without_facet(flt, "statuses").statuses, without_facet(flt, "statuses").segments), ((), ("eventy",)))
        self.assertEqual(without_facet(flt, "segments").segments, ())
        self.assertEqual((without_facet(flt, "countries").countries, without_facet(flt, "countries").has_email), ((), True))
        with self.assertRaises(ValueError):
            without_facet(flt, "email")


if __name__ == "__main__":
    unittest.main()
