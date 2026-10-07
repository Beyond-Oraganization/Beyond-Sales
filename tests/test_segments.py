from __future__ import annotations

import unittest

from perno.domain import segments


class SegmentNameTests(unittest.TestCase):
    def test_slugify_removes_polish_letters_and_collapses_separators(self):
        self.assertEqual(segments.slugify("Szkoły jazdy"), "szkoly-jazdy")
        self.assertEqual(segments.slugify("  Żółć, łódź & spółki!! "), "zolc-lodz-spolki")
        self.assertEqual(segments.slugify(""), "")

    def test_normalize_accepts_known_and_new_segments(self):
        for name in ("eventy", "driverzy", "spoldzielnie", "Kancelarie prawne", "b2b"):
            self.assertTrue(segments.is_valid(segments.normalize(name)), name)

    def test_normalize_rejects_names_that_cannot_be_identifiers(self):
        for name in ("", "a", "123", "!!!", "x" * 40):
            with self.assertRaises(segments.SegmentError, msg=name):
                segments.normalize(name)

    def test_is_valid_is_strict_about_the_final_form(self):
        self.assertTrue(segments.is_valid("kancelarie"))
        for bad in ("Kancelarie", "1abc", "ab c", "abc-", "a", "ą" * 3, None, 5):
            self.assertFalse(segments.is_valid(bad), bad)


if __name__ == "__main__":
    unittest.main()
