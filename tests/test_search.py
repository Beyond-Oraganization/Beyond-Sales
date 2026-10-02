"""Składanie liter, rozbijanie zapytania i polska kolejność: zgodność z dawną implementacją w przeglądarce."""

from __future__ import annotations

import json
import os
import unittest

from perno.domain.search import SearchTerms, fold, parse_query, pl_sort_key

HERE = os.path.dirname(os.path.abspath(__file__))


def load_vectors() -> dict:
    with open(os.path.join(HERE, "search_cases.json"), encoding="utf-8") as handle:
        return json.load(handle)


class VectorTests(unittest.TestCase):
    """Oczekiwania wygenerowano z `filters.js` (fold, parseQuery) przed przeniesieniem wyszukiwarki na serwer."""

    vectors = load_vectors()

    def test_fold_matches_the_browser_implementation(self):
        for case in self.vectors["fold"]:
            with self.subTest(case["input"]):
                self.assertEqual(fold(case["input"]), case["expect"])

    def test_query_parsing_matches_the_browser_implementation(self):
        for case in self.vectors["query"]:
            with self.subTest(case["input"]):
                parsed = parse_query(case["input"])
                self.assertEqual((list(parsed.include), list(parsed.exclude)),
                                 (case["expect"]["include"], case["expect"]["exclude"]))

    def test_the_vectors_cover_the_tricky_inputs(self):
        inputs = [case["input"] for case in self.vectors["query"]]
        for needle in ('"taxi ber', "-", '""  -""', None):  # niedomknięty cudzysłów, sam minus, puste frazy, brak tekstu
            self.assertIn(needle, inputs)
        self.assertGreaterEqual(len(self.vectors["fold"]), 30)


class BehaviourTests(unittest.TestCase):
    def test_fold_examples(self):
        self.assertEqual(fold("Zażółć gęślą jaźń"), "zazolc gesla jazn")
        self.assertEqual(fold("  Ærø   Øl "), "aero ol")
        self.assertEqual(fold(None), "")

    def test_query_terms_and_field(self):
        terms = parse_query('"Taxi Berlin" -pies', "email")
        self.assertEqual(terms, SearchTerms(("taxi berlin",), ("pies",), "email"))
        self.assertTrue(terms)
        self.assertFalse(parse_query("  "))
        self.assertEqual(parse_query("x", "nie-ma-takiego").field, "all")

    def test_polish_order_matches_the_polish_collation_of_the_browser_and_mysql(self):
        words = ["Łotwa", "Litwa", "Malta", "Zambia", "Źródło", "Żuraw", "Ząb", "Ćma", "Czad", "Cypr", "Austria", "Ąkol"]
        expected = ["Austria", "Ąkol", "Cypr", "Czad", "Ćma", "Litwa", "Łotwa", "Malta", "Zambia", "Ząb", "Źródło", "Żuraw"]
        self.assertEqual(sorted(words, key=pl_sort_key), expected)

    def test_polish_order_ignores_case_and_foreign_accents_but_keeps_digits_first(self):
        self.assertEqual(pl_sort_key("ŁÓDŹ"), pl_sort_key("łódź"))
        self.assertEqual(pl_sort_key("Müller"), pl_sort_key("muller"))
        self.assertEqual(pl_sort_key("Straße"), pl_sort_key("strasse"))
        self.assertLess(pl_sort_key("5 Firm"), pl_sort_key("Alfa"))
        self.assertLess(pl_sort_key("a1@x.pl"), pl_sort_key("a12@x.cz"))  # znak specjalny przed cyfrą, jak w MySQL
        self.assertLess(pl_sort_key("x-y"), pl_sort_key("x1"))
        self.assertLess(pl_sort_key(""), pl_sort_key("a"))
        self.assertEqual(pl_sort_key(None), ())
        # liczby w tekście porównujemy znak po znaku (jak MySQL), nie jako liczby jak przeglądarka
        self.assertLess(pl_sort_key("Firma 10"), pl_sort_key("Firma 2"))


if __name__ == "__main__":
    unittest.main()
