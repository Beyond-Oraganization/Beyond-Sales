"""Szablon icebreaker: jeden na wszystkie segmenty, cztery wersje językowe z tym samym zestawem tokenów."""

from __future__ import annotations

import os
import unittest

from perno.domain import localize, templating
from perno.domain import statuses as st
from perno.mailer import mime_builder

VALUES = {
    "obs": "OBS", "pitch": "PITCH", "channel_summary": "CHANNEL", "season": "", "sender_name": "Roman Patlai",
    "f_chcial": "F_CHCIAL", "f_dop": "F_DOP", "f_musi": "F_MUSI", "f_pyta": "F_PYTA",
    "inline_image_cid": mime_builder.INLINE_IMAGE_CID,
}


def read(language: str) -> str:
    with open(localize.template_path(localize.ICEBREAKER, language), encoding="utf-8") as handle:
        return handle.read()


class IcebreakerTemplateTests(unittest.TestCase):
    def test_every_language_has_a_template(self):
        for language in localize.LANGUAGE_NAMES:
            self.assertTrue(os.path.exists(localize.template_path(localize.ICEBREAKER, language)), language)

    def test_languages_use_the_same_tokens(self):
        polish = set(templating.TOKEN_RE.findall(read("pl")))
        self.assertEqual(polish, set(VALUES))
        foreign = {token for token in polish if not token.startswith("f_")}  # Pan/Pani/Państwo tylko po polsku
        for language in ("en", "de", "es"):
            self.assertEqual(set(templating.TOKEN_RE.findall(read(language))), foreign, language)

    def test_every_language_renders_without_leftover_tokens(self):
        for language in localize.LANGUAGE_NAMES:
            html = templating.render(read(language), VALUES)
            self.assertNotIn("{{", html, language)
            self.assertIn("Roman Patlai", html, language)

    def test_season_paragraph_sits_before_the_closing_question(self):
        for language in localize.LANGUAGE_NAMES:
            template = read(language)
            self.assertLess(template.index("{{season}}"), template.index("{{sender_name}}"), language)
            self.assertLess(template.index("{{channel_summary}}"), template.index("{{season}}"), language)

    def test_templates_use_plain_hyphens(self):
        for language in localize.LANGUAGE_NAMES:
            self.assertNotRegex(read(language), "[—–]", language)

    def test_old_per_segment_templates_are_gone(self):
        for segment in st.SEGMENT_ORDER:
            for language in localize.LANGUAGE_NAMES:
                self.assertFalse(os.path.exists(localize.template_path(segment, language)), f"{segment}.{language}")


if __name__ == "__main__":
    unittest.main()
