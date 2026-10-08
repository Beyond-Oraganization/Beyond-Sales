"""domain/text_search.py: dopasowanie fraz po złożeniu liter, fragmenty, wartości pól i specyfikacje kolumn (czysta logika)."""

from __future__ import annotations

import unittest

from perno.domain import text_search as ts
from perno.domain.search import fold


def spans(text, *terms):
    return [(s.start, s.end) for s in ts.phrase_spans(text, [fold(t) for t in terms])]


class PhraseSpanTests(unittest.TestCase):
    def test_finds_every_occurrence_without_overlap_and_in_order(self):
        self.assertEqual(spans("aaaa", "aa"), [(0, 2), (2, 4)])
        self.assertEqual(spans("ab ba ab", "ab"), [(0, 2), (6, 8)])

    def test_several_terms_are_merged_and_sorted(self):
        self.assertEqual(spans("xx bb aa", "aa", "bb"), [(3, 5), (6, 8)])

    def test_no_terms_or_empty_term_finds_nothing(self):
        self.assertEqual(ts.phrase_spans("abc", []), [])
        self.assertEqual(ts.phrase_spans("abc", [""]), [])

    def test_whitespace_runs_match_a_single_space_phrase(self):
        text = "rezerwacja\nonline"
        self.assertEqual(spans(text, "rezerwacja online"), [(0, len(text))])

    def test_positions_point_into_the_original_text(self):
        text = "Zażółć gęślą jaźń"
        found = ts.phrase_spans(text, [fold("gesla")])
        self.assertEqual(text[found[0].start:found[0].end], "gęślą")
        self.assertEqual(found[0].term, "gesla")

    def test_regex_spans_skip_empty_matches_and_sort(self):
        patterns = ts.compile_patterns([r"b+", r"x*"])
        self.assertEqual([(s.start, s.end) for s in ts.regex_spans("abbc", patterns)], [(1, 3)])
        with self.assertRaises(ts.SpecError):
            ts.compile_patterns(["("])


class SnippetTests(unittest.TestCase):
    TEXT = "0123456789" * 10  # 100 znaków

    def span(self, start, end, term="t"):
        return ts.Span(start, end, term)

    def test_context_is_cut_at_both_sides_with_ellipses(self):
        [one] = ts.snippets(self.TEXT, [self.span(50, 52)], width=5)
        self.assertEqual(one.text, "…" + self.TEXT[45:57] + "…")

    def test_no_ellipsis_when_text_edge_is_reached(self):
        [one] = ts.snippets(self.TEXT, [self.span(0, 2)], width=5)
        self.assertEqual(one.text, self.TEXT[:7] + "…")
        [two] = ts.snippets(self.TEXT, [self.span(98, 100)], width=5)
        self.assertEqual(two.text, "…" + self.TEXT[93:])
        [whole] = ts.snippets("krótki tekst", [self.span(0, 6)], width=50)
        self.assertEqual(whole.text, "krótki tekst")

    def test_touching_or_overlapping_windows_merge_but_distant_ones_do_not(self):
        merged = ts.snippets(self.TEXT, [self.span(20, 22), self.span(30, 32)], width=5)  # [15,27) i [25,37)
        self.assertEqual(len(merged), 1)
        touching = ts.snippets(self.TEXT, [self.span(20, 22), self.span(32, 34)], width=5)  # [15,27) i [27,39)
        self.assertEqual(len(touching), 1)
        apart = ts.snippets(self.TEXT, [self.span(20, 22), self.span(40, 42)], width=5)
        self.assertEqual(len(apart), 2)
        self.assertEqual(apart[0].term, "t")

    def test_merged_window_covers_both_hits_and_keeps_the_first_term(self):
        [merged] = ts.snippets(self.TEXT, [self.span(20, 22, "pierwsza"), self.span(30, 32, "druga")], width=5)
        self.assertEqual(merged.text, "…" + self.TEXT[15:37] + "…")
        self.assertEqual(merged.term, "pierwsza")

    def test_one_cut_character_still_gets_an_ellipsis(self):
        [one] = ts.snippets(self.TEXT, [self.span(6, 7)], width=5)  # okno zaczyna się na znaku 1
        self.assertTrue(one.text.startswith("…"))
        [full] = ts.snippets(self.TEXT, [self.span(5, 6)], width=5)  # okno od 0: nic nie ucięto
        self.assertFalse(full.text.startswith("…"))

    def test_default_limit_is_three_snippets(self):
        hits = [self.span(position, position + 1) for position in (5, 25, 45, 65, 85)]
        self.assertEqual(len(ts.snippets(self.TEXT, hits, width=2)), 3)

    def test_limit_counts_snippets_not_hits(self):
        found = ts.snippets(self.TEXT, [self.span(10, 11), self.span(40, 41), self.span(80, 81)], width=2, limit=2)
        self.assertEqual(len(found), 2)

    def test_whitespace_in_snippet_is_squashed(self):
        [one] = ts.snippets("a\n\n  b  X  c\td", [self.span(8, 9)], width=6)
        self.assertNotIn("\n", one.text)
        self.assertNotIn("  ", one.text)


class ValueTests(unittest.TestCase):
    def test_group_one_is_the_value_and_without_group_the_whole_match(self):
        with_group = ts.compile_patterns([r"nr (\d+)"])[0]
        without = ts.compile_patterns([r"\d+"])[0]
        self.assertEqual(ts.extract_values("Nr 12, nr 7", with_group), ["12", "7"])
        self.assertEqual(ts.extract_values("a 12 b 7", without), ["12", "7"])

    def test_values_are_distinct_limited_and_trimmed(self):
        pattern = ts.compile_patterns([r"\d+"])[0]
        self.assertEqual(ts.extract_values("1 1 2 3 4 5", pattern), ["1", "2", "3"])
        self.assertEqual(ts.extract_values("1 2 3 4", pattern, limit=2), ["1", "2"])
        long = ts.compile_patterns([r"x+"])[0]
        self.assertEqual(len(ts.extract_values("x" * 500, long)[0]), ts.MAX_VALUE_LEN)

    def test_has_phrase_ignores_case_and_accents(self):
        self.assertTrue(ts.has_phrase("Rezerwacja ONLINE", [fold("rezerwacja online")]))
        self.assertFalse(ts.has_phrase("brak", [fold("rezerwacja")]))
        self.assertFalse(ts.has_phrase("cokolwiek", []))

    def test_presets_cover_registry_numbers(self):
        regon = ts.compile_patterns([ts.PRESETS["regon"]])[0]
        krs = ts.compile_patterns([ts.PRESETS["krs"]])[0]
        self.assertEqual(ts.extract_values("REGON: 123456789; REGON 12345678901234", regon), ["123456789", "12345678901234"])
        self.assertEqual(ts.extract_values("KRS 0000123456", krs), ["0000123456"])
        self.assertEqual(ts.extract_values("KRS 123", krs), [])


class SpecTests(unittest.TestCase):
    def test_name_rules_have_exact_boundaries(self):
        self.assertEqual(ts.parse_name("a" + "b" * 30 + "=x", "t")[0], "a" + "b" * 30)  # 31 znaków
        with self.assertRaises(ts.SpecError):
            ts.parse_name("a" + "b" * 31 + "=x", "t")  # 32 znaki
        self.assertEqual(ts.parse_name(" nazwa_1 = wartość ", "t"), ("nazwa_1", "wartość"))
        for bad in ("1abc=x", "a-b=x", "nazwa=", "nazwa= ", "nazwa"):
            with self.assertRaises(ts.SpecError):
                ts.parse_name(bad, "t")

    def test_phrases_are_folded_deduplicated_and_required(self):
        self.assertEqual(ts.parse_phrases("Łódź| lodz |Booksy||"), ("lodz", "booksy"))
        with self.assertRaises(ts.SpecError):
            ts.parse_phrases("| |")


if __name__ == "__main__":
    unittest.main()
