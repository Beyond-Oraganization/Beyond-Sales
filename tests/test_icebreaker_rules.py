from __future__ import annotations

import unittest

from perno.domain import icebreaker_rules as rules

GOOD = {
    "subject": "Konsultacje dla spółdzielni, Katowice", "mode": "Pani",
    "obs": "Szukałem konsultacji prawnych. Zauważyłem, że prowadzi Pani konsultacje online. Czy termin uzgadniany jest telefonicznie?",
    "pitch": "przy konsultacji zdalnej wprowadzić natychmiastową płatność online",
    "channel_summary": "uzgadniać terminów konsultacji telefonicznie",
}


def entry(**overrides):
    return {**GOOD, **overrides}


class NormalizeModeTests(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(rules.normalize_mode("Państwo"), "Panstwo")
        self.assertEqual(rules.normalize_mode("pan"), "Pan")
        self.assertEqual(rules.normalize_mode(None), "Panstwo")
        self.assertEqual(rules.normalize_mode(""), "Panstwo")

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            rules.normalize_mode("Szanowny")


class PolishRulesTests(unittest.TestCase):
    def errors(self, **overrides):
        return rules.check(entry(**overrides), "pl").errors

    def test_a_good_entry_has_no_errors(self):
        found = rules.check(entry(), "pl")
        self.assertEqual((found.errors, found.warnings), ([], []))

    def test_obs_shape(self):
        self.assertTrue(self.errors(obs="Zauważyłem, że coś. Czy tak?"))
        self.assertTrue(self.errors(obs="Szukałem kursów. Czy tak?"))
        self.assertTrue(self.errors(obs="Szukałem kursów. Zauważyłem, że coś."))

    def test_pitch_must_start_with_przy(self):
        self.assertTrue(self.errors(pitch="wprowadzić płatność"))

    def test_channel_summary_must_not_mention_payments(self):
        self.assertTrue(self.errors(channel_summary="rozliczać płatności ręcznie"))

    def test_subject_length(self):
        self.assertTrue(self.errors(subject="Jedno"))
        self.assertTrue(self.errors(subject="a b c d e f g"))
        self.assertTrue(rules.check(entry(subject="Dwa słowa"), "pl").warnings == ["temat ma 2 słów (zalecane 3-4)"])

    def test_subject_word_count_boundaries(self):
        def run(words):
            found = rules.check(entry(subject=" ".join(["słowo"] * words)), "pl")
            return bool(found.errors), bool(found.warnings)

        self.assertEqual([run(n) for n in range(1, 8)],
                         [(True, False), (False, True), (False, False), (False, False), (False, True), (False, True), (True, False)])

    def test_obs_needs_both_the_czy_question_and_the_question_mark(self):
        base = "Szukałem kursów. Zauważyłem, że coś jest."
        self.assertTrue(rules.check(entry(obs=base + " Pytanie?"), "pl").errors)  # „?”, ale bez „Czy”
        self.assertTrue(rules.check(entry(obs=base + " Czy tak."), "pl").errors)  # „Czy”, ale bez „?”
        self.assertEqual(rules.check(entry(obs=base + " Czy tak?"), "pl").errors, [])

    def test_season_must_be_one_sentence(self):
        self.assertTrue(rules.check(entry(season="Raz. Dwa."), "pl").warnings)
        self.assertEqual(rules.check(entry(season="Raz."), "pl").warnings, [])

    def test_the_message_names_the_offending_word(self):
        errors = rules.check(entry(mode="Pan", pitch="przy konsultacji, jeśli Państwo chcą, wprowadzić płatność"), "pl").errors
        self.assertTrue(any("pitch: „Państwo”" in message for message in errors), errors)

    def test_long_dashes_are_refused_everywhere(self):
        for field in ("subject", "obs", "pitch", "channel_summary", "season"):
            value = "przy x – y" if field == "pitch" else GOOD.get(field, "Zdanie — z myślnikiem.") + " –"
            self.assertTrue(any("myślnik" in e for e in self.errors(**{field: value})), field)

    def test_address_form_must_match_the_mode(self):
        self.assertTrue(self.errors(mode="Pan"))  # tekst jest w formie „Pani”
        ok = entry(mode="Panstwo", obs=GOOD["obs"].replace("Pani", "Państwo").replace("prowadzi", "prowadzą"))
        self.assertEqual(rules.check(ok, "pl").errors, [])
        self.assertTrue(self.errors(mode="Panstwo", pitch="przy konsultacji, jeśli Pan woli, wprowadzić płatność"))

    def test_season_warning(self):
        self.assertTrue(rules.check(entry(season="Bez kropki"), "pl").warnings)
        self.assertEqual(rules.check(entry(season="Kolejny turnus w lipcu."), "pl").warnings, [])

    def test_unknown_mode_is_an_error(self):
        self.assertTrue(rules.check(entry(mode="Szanowny"), "pl").errors)


class ForeignLanguageTests(unittest.TestCase):
    def test_only_characters_and_form_are_checked(self):
        found = rules.check({"subject": "x", "mode": "Panstwo", "obs": "I looked for courses", "pitch": "p",
                             "channel_summary": "c"}, "en")
        self.assertEqual(found.errors, [])
        found = rules.check({"subject": "x", "mode": "Panstwo", "obs": "dash — here", "pitch": "p", "channel_summary": "c"}, "en")
        self.assertTrue(found.errors)


if __name__ == "__main__":
    unittest.main()
