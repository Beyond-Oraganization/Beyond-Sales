from __future__ import annotations

import unittest

from perno.domain import brief


class SnippetTests(unittest.TestCase):
    def test_snippets_surround_the_keyword_and_collapse_whitespace(self):
        text = "Witamy w firmie.\n\n" + "Lorem ipsum " * 30 + "Zapisy na kurs trwają do 5 maja.   " + "dolor sit " * 30
        found = brief.snippets(text, ("zapis",), width=30)
        self.assertEqual(len(found), 1)
        self.assertIn("Zapisy na kurs trwają", found[0])
        self.assertNotIn("\n", found[0])
        self.assertLessEqual(len(found[0]), 70)

    def test_snippets_are_limited_and_do_not_overlap(self):
        text = "zapis cena termin online " * 3 + "x" * 400 + " rezerwacja tutaj " + "y" * 400 + " bilet kupisz tu"
        found = brief.snippets(text, ("zapis", "cena", "termin", "rezerw", "bilet"), width=20, limit=2)
        self.assertEqual(len(found), 2)

    def test_control_and_direction_characters_are_stripped(self):
        self.assertEqual(brief.collapse("a\x00b\x1b[31m c\u202ed"), "ab[31m cd")

    def test_no_keyword_gives_no_snippets(self):
        self.assertEqual(brief.snippets("nic tu nie ma", ("zapis",)), [])
        self.assertEqual(brief.snippets("", ("zapis",)), [])


class SnippetBoundaryTests(unittest.TestCase):
    def test_a_keyword_at_the_very_start_is_found_and_kept_whole(self):
        self.assertEqual(brief.snippets("zapis tutaj", ("zapis",), width=3), ["zapis tu"])

    def test_touching_spans_are_both_kept_in_either_order(self):
        self.assertEqual(brief.snippets("zapiscena", ("zapis", "cena"), width=0, limit=5), ["zapis", "cena"])
        self.assertEqual(brief.snippets("zapiscena", ("cena", "zapis"), width=0, limit=5), ["zapis", "cena"])

    def test_overlapping_spans_collapse_into_one(self):
        self.assertEqual(len(brief.snippets("zapis cena termin", ("zapis", "cena", "termin"), width=10, limit=5)), 1)


class SnippetDefaultsTests(unittest.TestCase):
    def test_default_width_and_limit(self):
        far = "x" * 300
        one = far + "zapis" + far
        self.assertEqual(len(brief.snippets(one)[0]), 110 + len("zapis") + 110)
        many = (far + "zapis" + far + "cena" + far + "termin" + far + "bilet" + far)
        self.assertEqual(len(brief.snippets(many)), 3)

    def test_an_earlier_overlapping_span_found_later_in_keyword_order_is_rejected(self):
        text = "aaaaa zapis bbb cena"
        self.assertEqual(len(brief.snippets(text, ("cena", "zapis"), width=10)), 1)


class SignalTests(unittest.TestCase):
    def test_platforms_and_payments_are_detected_case_insensitively(self):
        signals = brief.detect(["Zapisy przez Datasport.pl", "płatność BLIK i Przelewy24", "https://x.pl/koszyk"])
        self.assertEqual(signals.platforms, ("Datasport",))
        self.assertEqual(set(signals.payments), {"Przelewy24", "BLIK", "WooCommerce"})
        self.assertFalse(signals.client)

    def test_payevento_marks_the_firm_as_our_client(self):
        signals = brief.detect(["Bilety: https://payevento.com/event/5/tickets/"])
        self.assertTrue(signals.client)
        self.assertIn("STOP=PayEvento", brief.describe_signals(signals))

    def test_default_signals_are_empty(self):
        self.assertEqual(brief.describe_signals(brief.Signals()), "")
        self.assertNotIn("sygnały", brief.FirmBrief("x", "F", "PL", "pl", 1, "a@b.pl").format(500))

    def test_nothing_found(self):
        signals = brief.detect(["zwykła strona"])
        self.assertEqual(brief.describe_signals(signals), "")


class FormatTests(unittest.TestCase):
    def make(self, **overrides):
        data = dict(row_id="eventy-n001", company="Firma A", place="PL Łódź", language="pl", pages=12,
                    address="biuro@a.pl (własna domena)", phone="+48 111", description="Kursy florystyczne",
                    home="Florystyka | Kursy", template="icebreaker/pl", snippets=[("/cennik", "cena 1980 zł")])
        data.update(overrides)
        return brief.FirmBrief(**data)

    def test_format_has_header_address_and_snippets(self):
        text = self.make().format(2000)
        self.assertTrue(text.startswith("## eventy-n001 | Firma A | PL Łódź | pl | stron: 12\nadres: biuro@a.pl"))
        self.assertIn("- /cennik: „cena 1980 zł”", text)
        self.assertIn("szablon: icebreaker/pl", text)

    def test_snippets_are_dropped_first_when_the_budget_is_tight(self):
        long = [("/a", "x" * 300), ("/b", "y" * 300)]
        text = self.make(snippets=long).format(400)
        self.assertLessEqual(len(text), 400)
        self.assertIn("adres:", text)
        self.assertNotIn("yyy", text)

    def test_fit_cuts_on_a_word_boundary_with_an_ellipsis(self):
        self.assertEqual(brief.fit("jeden dwa trzy cztery", 12), "jeden dwa…")
        self.assertEqual(brief.fit("krótki", 12), "krótki")

    def test_fit_keeps_line_breaks_of_multiline_text_and_collapses_single_lines(self):
        self.assertEqual(brief.fit("a\nb  c", 100), "a\nb  c")
        self.assertEqual(brief.fit("a   b", 100), "a b")

    def test_fit_boundaries(self):
        self.assertEqual(brief.fit("abcdefghij", 5), "abcd…")
        self.assertEqual(brief.fit("abcdefghij", 1), "…")
        self.assertEqual(brief.fit("abcdef ghijklmnop", 10), "abcdef gh…")  # spacja dokładnie na 60%: nie tniemy po słowie
        self.assertEqual(brief.fit("abcde", 5), "abcde")

    def test_long_description_and_home_are_capped(self):
        text = self.make(description="d" * 400, home="h" * 400, snippets=[]).format(5000)
        opis = next(line for line in text.splitlines() if line.startswith("opis: "))
        www = next(line for line in text.splitlines() if line.startswith("www: "))
        self.assertEqual((len(opis) - len("opis: "), len(www) - len("www: ")), (220, 200))
        self.assertTrue(opis.endswith("…") and www.endswith("…"))

    def test_budget_is_exact_for_snippets_and_hard_for_the_header(self):
        bare = self.make(snippets=[]).format(5000)
        line = "\n- /p: „x”"
        with_snippet = self.make(snippets=[("/p", "x")])
        self.assertEqual(with_snippet.format(len(bare) + len(line)), bare + line)
        self.assertEqual(with_snippet.format(len(bare) + len(line) - 1), bare)
        self.assertLessEqual(len(with_snippet.format(30)), 30)

    def test_home_line_skips_empty_parts(self):
        self.assertEqual(brief.home_line("T", "   "), "T")
        self.assertEqual(brief.home_line("Tytuł", None), "Tytuł")
        self.assertEqual(brief.home_line(" A ", "B"), "A | B")


if __name__ == "__main__":
    unittest.main()
