from __future__ import annotations

import unittest

from perno.domain import countries
from perno.domain.countries import choices, country_name, flag, normalize_country


class NormalizeTests(unittest.TestCase):
    def test_codes_in_any_case(self):
        for raw in ("PL", "pl", " Pl ", "pL"):
            self.assertEqual(normalize_country(raw), "PL", raw)

    def test_polish_and_english_names(self):
        table = {
            "Polska": "PL", "polska": "PL", "Poland": "PL", "Wielka Brytania": "GB", "United Kingdom": "GB",
            "Niemcy": "DE", "Germany": "DE", "Austria": "AT", "Irlandia": "IE", "Ireland": "IE",
            "Szwajcaria": "CH", "Czechy": "CZ", "Czech Republic": "CZ", "Holandia": "NL", "Netherlands": "NL",
            "Słowacja": "SK", "Łotwa": "LV", "Węgry": "HU", "Bośnia i Hercegowina": "BA", "Włochy": "IT",
        }
        for raw, code in table.items():
            self.assertEqual(normalize_country(raw), code, raw)

    def test_aliases_and_diacritics_do_not_matter(self):
        table = {"UK": "GB", "u.k.": "GB", "Anglia": "GB", "Szkocja": "GB", "Scotland": "GB", "Irlandia Północna": "GB",
                 "USA": "US", "Stany Zjednoczone": "US", "polen": "PL", "POLSKA ": "PL", "luksemburg": "LU",
                 "Czarnogora": "ME", "Bulgaria": "BG", "Bułgaria": "BG"}
        for raw, code in table.items():
            self.assertEqual(normalize_country(raw), code, raw)

    def test_unknown_empty_and_non_countries_are_rejected(self):
        for raw in (None, "", "   ", "XX", "ZZ", "Atlantyda", "Polska (Warszawa)", "P", "POL", "12", "UKR", 5):
            self.assertIsNone(normalize_country(raw), repr(raw))

    def test_uk_is_never_stored_as_a_code(self):
        self.assertNotIn("UK", countries.COUNTRY_NAMES)
        self.assertEqual(normalize_country("UK"), "GB")

    def test_every_listed_country_roundtrips_through_its_names(self):
        for code, polish, english in countries._COUNTRIES:
            self.assertEqual(normalize_country(code), code)
            self.assertEqual(normalize_country(polish), code, polish)
            self.assertEqual(normalize_country(english), code, english)

    def test_codes_and_names_are_unique(self):
        codes = [code for code, _, _ in countries._COUNTRIES]
        names = [countries._fold(pl) for _, pl, _ in countries._COUNTRIES]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(len(names), len(set(names)))


class DisplayTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(country_name("PL"), "Polska")
        self.assertEqual(country_name("gb"), "Wielka Brytania")
        self.assertEqual(country_name(""), "")
        self.assertEqual(country_name(None), "")
        self.assertEqual(country_name("XX"), "")

    def test_flags_are_regional_indicator_pairs(self):
        self.assertEqual(flag("PL"), "\U0001F1F5\U0001F1F1")
        self.assertEqual(flag("de"), "\U0001F1E9\U0001F1EA")
        self.assertEqual(flag("XX"), "")
        self.assertEqual(flag(None), "")

    def test_choices_put_poland_first_and_sort_the_rest_in_polish_order(self):
        items = choices()
        self.assertEqual(items[0], ("PL", "Polska", flag("PL")))
        names = [name for _, name, _ in items[1:]]
        folded = [countries._fold(name) for name in names]
        self.assertEqual(folded, sorted(folded))
        self.assertEqual(len(items), len(countries.COUNTRY_NAMES))
        self.assertIn(("GB", "Wielka Brytania", flag("GB")), items)


if __name__ == "__main__":
    unittest.main()
