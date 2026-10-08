from __future__ import annotations

import os
import unittest

from perno.domain import forms, localize, templating

VALUES = {
    "sent_date": "29 września", "recap": "Pytałem, czy zapis idzie tylko przez telefon.",
    "angle": "Zapis z płatnością online zamyka decyzję od razu.", "f_dop": forms.conjugate("Pan")["dop"],
    "sender_name": "Roman Patlai",
}


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class FollowupTemplateTests(unittest.TestCase):
    def test_every_language_has_a_template_that_renders(self):
        for language in localize.LANGUAGE_NAMES:
            with self.subTest(language=language):
                path = localize.template_path("followup", language)
                self.assertTrue(os.path.exists(path), path)
                html = templating.render(read(path), VALUES)
                self.assertNotIn("{{", html)
                for value in ("29 września", "Roman Patlai", VALUES["angle"], VALUES["recap"]):
                    self.assertIn(value, html)

    def test_polish_template_uses_the_address_form(self):
        html = templating.render(read(localize.template_path("followup", "pl")), VALUES)
        self.assertIn("do Pana", html)

    def test_unknown_token_is_rejected(self):
        with self.assertRaises(templating.MissingTokenError):
            templating.render(read(localize.template_path("followup", "pl")), {"sent_date": "1 stycznia"})
