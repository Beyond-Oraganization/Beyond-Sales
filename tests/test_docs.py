"""Usługa dokumentów: rejestr, wczytywanie z odświeżaniem po zmianie pliku, odmiana liczb i polecenia z tabeli w instrukcji."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from perno import settings
from perno.services import docs

SPEC = docs.DocSpec("probny", "/probny", "PROBNY.md", "Próbny", "Opis")


class RegistryTests(unittest.TestCase):
    def test_keys_paths_and_files_are_unique_and_the_routes_are_pages(self):
        for field in ("key", "path", "file"):
            values = [getattr(spec, field) for spec in docs.DOCS]
            self.assertEqual(len(values), len(set(values)), field)
        self.assertTrue(all(spec.path.startswith("/") for spec in docs.DOCS))

    def test_routes_map_file_names_to_pages(self):
        self.assertEqual(docs.doc_routes()["AI_INSTRUCTIONS.md"], "/instrukcje")
        self.assertEqual(docs.doc_routes()["icebreaker.md"], "/zasady-maili")

    def test_spec_for_finds_a_document_and_rejects_an_unknown_key(self):
        self.assertEqual(docs.spec_for("dokumentacja").file, "README.md")
        with self.assertRaises(StopIteration):
            docs.spec_for("nie-ma")

    def test_the_instructions_document_is_registered_first(self):
        self.assertEqual(docs.DOCS[0].key, docs.INSTRUCTIONS_KEY)

    def test_attach_reference_is_relative_to_the_folder_claude_code_runs_in(self):
        spec = docs.spec_for("instrukcje")
        self.assertEqual(docs.attach_reference(spec, "/home/u/Perno/Wysylka", "/home/u/Perno"), "@Wysylka/AI_INSTRUCTIONS.md")
        icebreaker = docs.spec_for("zasady-maili")
        self.assertEqual(docs.attach_reference(icebreaker, "/home/u/Perno/Wysylka", "/home/u/Perno"), "@Wysylka/templates/icebreaker.md")


class PluralTests(unittest.TestCase):
    def test_polish_plural_forms(self):
        cases = {1: "sekcja", 2: "sekcje", 4: "sekcje", 5: "sekcji", 11: "sekcji", 12: "sekcji", 14: "sekcji",
                 21: "sekcji", 22: "sekcje", 25: "sekcji", 112: "sekcji", 122: "sekcje", 0: "sekcji"}
        for count, word in cases.items():
            with self.subTest(count=count):
                self.assertEqual(docs.plural_pl(count, "sekcja", "sekcje", "sekcji"), f"{count} {word}")


class LoadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, SPEC.file)

    def write(self, text: str, mtime: float | None = None) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        if mtime is not None:
            os.utime(self.path, (mtime, mtime))

    def test_a_missing_file_gives_none(self):
        self.assertIsNone(docs.load(SPEC, self.dir))

    def test_a_document_is_rendered_with_sections_and_a_reading_time(self):
        self.write("# Tytuł\n\n## Jeden\n\n" + "słowo " * 450 + "\n\n## Dwa\n")
        document = docs.load(SPEC, self.dir)
        self.assertEqual(document.rendered.title, "Tytuł")
        self.assertEqual((document.sections, document.sections_label), (2, "2 sekcje"))
        self.assertEqual((document.minutes, document.minutes_label), (2, "ok. 2 min"))

    def test_reading_time_is_at_least_one_minute(self):
        self.write("# T\n\nkrótko\n")
        self.assertEqual(docs.load(SPEC, self.dir).minutes, 1)

    def test_an_unchanged_file_is_served_from_the_cache(self):
        self.write("## A\n", mtime=1_700_000_000)
        first = docs.load(SPEC, self.dir)
        with mock.patch("perno.services.docs.markdown_render.render", side_effect=AssertionError("renderowanie niepotrzebne")):
            self.assertIs(docs.load(SPEC, self.dir), first)

    def test_a_changed_file_is_rendered_again(self):
        self.write("## A\n", mtime=1_700_000_000)
        before = docs.load(SPEC, self.dir)
        self.write("## A\n\n## B\n", mtime=1_700_000_100)
        after = docs.load(SPEC, self.dir)
        self.assertIsNot(after, before)
        self.assertEqual(after.sections, 2)

    def test_a_file_that_is_not_utf8_gives_none(self):
        with open(self.path, "wb") as handle:
            handle.write(b"\xff\xfe## A\n")
        self.assertIsNone(docs.load(SPEC, self.dir))

    def test_the_real_files_of_the_project_all_load(self):
        for spec in docs.DOCS:
            with self.subTest(file=spec.file):
                document = docs.load(spec, settings.BASE_DIR)
                self.assertIsNotNone(document)
                self.assertGreaterEqual(document.sections, 1)


class CommandsTests(unittest.TestCase):
    TABLE = (
        "| Komenda użytkownika | Sekcja |\n|---|---|\n"
        "| `WYSZUKAJ [ilość] firm typu [typ]` | [1](#1-wyszukaj-ilość-firm-typu-typ) |\n"
        "| `ZATRZYMAJ SKAN` / `WZNÓW SKAN` | [3](#3-pokaż-stan) |\n"
        "| tekst bez kodu | [4](#4-x) |\n"
        "| `BEZ ODNOŚNIKA` | brak |\n"
    )

    def test_commands_come_from_the_table_and_a_cell_with_two_codes_gives_two_commands(self):
        found = docs.commands(self.TABLE)
        self.assertEqual([(c.text, c.section) for c in found],
                         [("WYSZUKAJ [ilość] firm typu [typ]", "1"), ("ZATRZYMAJ SKAN", "3"), ("WZNÓW SKAN", "3")])
        self.assertEqual(found[1].anchor, "3-pokaż-stan")

    def test_text_outside_the_table_is_ignored(self):
        self.assertEqual(docs.commands("Zwykły akapit z `kodem` i [1](#a).\n"), ())

    def test_the_real_instructions_list_every_command_and_each_points_at_a_heading(self):
        document = docs.load(docs.spec_for(docs.INSTRUCTIONS_KEY), settings.BASE_DIR)
        found = docs.commands(document.text)
        self.assertGreaterEqual(len(found), 13)
        texts = [c.text for c in found]
        for expected in ("POKAŻ STAN", "ZATRZYMAJ SKAN", "WZNÓW SKAN", "POBIERZ ODPOWIEDZI", "POKAŻ LEADY"):
            self.assertIn(expected, texts)
        ids = {h.slug for h in document.rendered.headings}
        for command in found:
            self.assertIn(command.anchor, ids, command.text)
            self.assertTrue(command.anchor.startswith(command.section + "-"), command.text)


if __name__ == "__main__":
    unittest.main()
