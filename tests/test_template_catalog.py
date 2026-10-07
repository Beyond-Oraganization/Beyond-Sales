from __future__ import annotations

import os
import tempfile
import unittest

from perno.services import template_catalog as catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def put(self, name, text="<p>{{obs}}</p>"):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_languages_and_fields_come_from_the_files(self):
        self.put("intro.html", "<p>{{obs}} {{pitch}} {{season}} {{f_dop}} {{sender_name}} {{inline_image_cid}}</p>")
        self.put("intro.en.html", "<p>{{obs}} {{pitch}} {{season}} {{sender_name}}</p>")
        self.put("intro.xx.html", "<p>{{obs}}</p>")  # nieznany język: pomijany
        self.put("notes.txt", "to nie szablon")
        (info,) = catalog.discover(self.dir)
        self.assertEqual((info.name, info.languages), ("intro", ("pl", "en")))
        self.assertEqual((info.fields, info.optional), (("obs", "pitch"), ("season",)))
        self.assertTrue(info.writable)

    def test_a_new_template_is_visible_without_code_changes(self):
        self.put("a.html")
        self.assertEqual([i.name for i in catalog.discover(self.dir)], ["a"])
        self.put("b.html")
        self.assertEqual([i.name for i in catalog.discover(self.dir)], ["a", "b"])

    def test_templates_with_other_author_fields_are_not_writable_by_ai_zapisz(self):
        self.put("followup.html", "<p>{{sent_date}} {{recap}} {{angle}}</p>")
        (info,) = catalog.discover(self.dir)
        self.assertEqual(info.fields, ("angle", "recap", "sent_date"))
        self.assertFalse(info.writable)

    def test_guide_is_found_next_to_the_template(self):
        self.put("x.html")
        self.assertIsNone(catalog.get("x", self.dir).guide)
        self.assertEqual(catalog.read_guide("x", self.dir), "")
        self.put("x.md", "# zasady")
        self.assertEqual(catalog.read_guide("x", self.dir), "# zasady")

    def test_unknown_template(self):
        with self.assertRaises(catalog.UnknownTemplate):
            catalog.get("nie-ma", self.dir)

    def test_the_real_templates_directory_has_a_writable_icebreaker_in_every_language(self):
        info = catalog.get("icebreaker")
        self.assertEqual(info.languages, ("pl", "en", "de", "es"))
        self.assertEqual(info.fields, ("channel_summary", "obs", "pitch"))
        self.assertEqual(info.optional, ("season",))
        self.assertTrue(info.writable and info.guide)


if __name__ == "__main__":
    unittest.main()
