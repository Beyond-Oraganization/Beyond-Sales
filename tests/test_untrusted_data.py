"""Dane z internetu i z bazy są niezaufane: log skrapera i eksport CSV."""

from __future__ import annotations

import os
import tempfile
import unittest

from perno.scraper import runner
from perno.services import export


class LogSanitizingTests(unittest.TestCase):
    def test_control_characters_become_spaces(self):
        self.assertEqual(runner.sanitize_log_message("a\x1b[31mczerwony\rb\x00c\x9bd"), "a [31mczerwony b c d")

    def test_tabs_and_polish_letters_survive(self):
        self.assertEqual(runner.sanitize_log_message("zażółć\tgęślą jaźń"), "zażółć\tgęślą jaźń")

    def test_continuation_lines_are_indented_so_they_cannot_forge_a_log_entry(self):
        forged = "tytuł strony\n12:00:00 [firma.pl] koniec: zakonczona strony=999"
        self.assertEqual(runner.sanitize_log_message(forged), "tytuł strony\n    12:00:00 [firma.pl] koniec: zakonczona strony=999")

    def test_logger_writes_only_clean_entries_to_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run.log")
            logger = runner.RunLogger(path, echo=False)
            logger("wynik: \x1b]0;pwned\x07\nHH:MM:SS fałszywy wpis\rnadpisanie")
            logger("zwykły wpis")
            logger.close()
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertNotIn("\r", text)
        for line in text.splitlines():
            self.assertTrue(line[2] == ":" or line.startswith("    "), line)  # wpis z godziną albo wcięta kontynuacja


class CsvGuardTests(unittest.TestCase):
    def cell(self, value):
        return export._csv_cell("subject", value)

    def test_every_formula_starter_gets_an_apostrophe(self):
        for starter in export.CSV_DANGEROUS:
            self.assertEqual(self.cell(f"{starter}cmd|' /C calc'!A0"), f"'{starter}cmd|' /C calc'!A0", repr(starter))

    def test_fullwidth_and_newline_variants_are_covered(self):
        for value in ("＝1+1", "＋48 600", "－5", "＠x", "\n=1+1"):
            self.assertTrue(self.cell(value).startswith("'"), repr(value))

    def test_harmless_values_are_left_alone(self):
        for value in ("Zażółć gęślą", "1+1", "a=b", "  =spacja", "", "email@firma.pl"):
            self.assertEqual(self.cell(value), value, repr(value))

    def test_non_text_values_pass_through(self):
        self.assertEqual(self.cell(5), 5)
        self.assertEqual(self.cell(None), "")


if __name__ == "__main__":
    unittest.main()
