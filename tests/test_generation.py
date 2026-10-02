from __future__ import annotations

import unittest
from datetime import datetime

from perno import settings as config
from perno.domain import statuses as st
from perno.mailer import mime_builder
from perno.services import generation

NOW = datetime(2026, 9, 30, 12, 0, 0)
CID = mime_builder.INLINE_IMAGE_CID


def seed(n=1, **overrides):
    row = {
        "row_number": str(n), "company_display_name": f"Firma {n}", "contact_person_guess": "",
        "email": f"f{n}@example.com", "website": "https://example.com/", "tag": "t", "mode": "Panstwo",
        "subject": f"Temat {n}", "obs": "Szukałem kursów.", "pitch": "wprowadzić płatność",
        "channel_summary": "zbierać zgłoszenia", "already_customer": "false", "needs_manual_copy": "false",
    }
    row.update(overrides)
    return row


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class RenderRowTests(unittest.TestCase):
    def test_both_templates_render_for_both_segments(self):
        for segment, path in (("eventy", config.EVENTY_TEMPLATE_PATH), ("driverzy", config.DRIVERZY_TEMPLATE_PATH)):
            subject, html, text, inline = generation.render_row(segment, seed(), read(path), "Roman Patlai")
            self.assertEqual(subject, "Temat 1")
            self.assertIn("Szukałem kursów.", html)
            self.assertIn("Roman Patlai", text)
            self.assertNotIn("{{", html)

    def test_inline_image_detected_from_rendered_html(self):
        with_image = "<p>{{obs}}</p><img src=\"cid:{{inline_image_cid}}\">"
        without_image = "<p>{{obs}} {{f_chcial}} {{pitch}} {{channel_summary}} {{f_dop}} {{f_musi}} {{f_pyta}} {{sender_name}}</p>"
        self.assertEqual(generation.render_row("eventy", seed(), with_image, "X")[3], CID)
        self.assertEqual(generation.render_row("driverzy", seed(), without_image, "X")[3], "")

    def test_missing_sender_name_falls_back_to_placeholder(self):
        _, _, text, _ = generation.render_row("eventy", seed(), "<p>{{sender_name}} {{obs}}</p>", "")
        self.assertIn("[Imię i nazwisko]", text)


class GenerateMissingTests(unittest.TestCase):
    TEMPLATE = "<p>{{obs}}</p>{{f_chcial}}{{pitch}}{{channel_summary}}{{f_dop}}{{f_musi}}{{f_pyta}}{{sender_name}}"

    def run_gen(self, index, seeds, regenerate=False):
        return generation.generate_missing(index, "eventy", seeds, self.TEMPLATE, "Roman", regenerate, NOW)

    def test_new_rows_get_status_from_data(self):
        changed, stats = self.run_gen({}, [
            seed(1), seed(2, email=""), seed(3, already_customer="true"), seed(4, needs_manual_copy="true"),
        ])
        by_id = {r["id"]: r for r in changed}
        self.assertEqual(by_id["eventy-001"]["status"], st.STATUS_PENDING)
        self.assertEqual(by_id["eventy-002"]["status"], st.STATUS_SKIPPED_NO_EMAIL)
        self.assertEqual(by_id["eventy-003"]["status"], st.STATUS_SKIPPED_CUSTOMER)
        self.assertEqual(by_id["eventy-004"]["status"], st.STATUS_AWAITING_COPY)
        self.assertEqual(by_id["eventy-003"]["body_html"], "")
        self.assertEqual(stats, {"created": 4, "regenerated": 0})
        self.assertEqual(by_id["eventy-001"]["generated_at"], "2026-09-30T12:00:00")

    def test_existing_rows_are_skipped_unless_regenerating(self):
        index = {"eventy-001": {"status": st.STATUS_PENDING}, "eventy-002": {"status": st.STATUS_ERROR}}
        changed, stats = self.run_gen(index, [seed(1), seed(2), seed(3)])
        self.assertEqual([r["id"] for r in changed], ["eventy-003"])
        changed, stats = self.run_gen(index, [seed(1), seed(2), seed(3)], regenerate=True)
        self.assertEqual(stats, {"created": 1, "regenerated": 2})

    def test_sent_and_sending_rows_are_never_regenerated(self):
        index = {"eventy-001": {"status": st.STATUS_SENT}, "eventy-002": {"status": st.STATUS_SENDING}}
        changed, _ = self.run_gen(index, [seed(1), seed(2)], regenerate=True)
        self.assertEqual(changed, [])

    def test_rows_with_a_human_decision_are_never_regenerated(self):
        index = {"eventy-001": {"status": st.STATUS_REPLIED}, "eventy-002": {"status": st.STATUS_INTERESTED},
                 "eventy-003": {"status": st.STATUS_DO_NOT_CONTACT}}
        changed, stats = self.run_gen(index, [seed(1), seed(2), seed(3)], regenerate=True)
        self.assertEqual((changed, stats), ([], {"created": 0, "regenerated": 0}))

    def test_seed_without_row_number_is_ignored(self):
        changed, _ = self.run_gen({}, [seed(1, row_number="")])
        self.assertEqual(changed, [])


if __name__ == "__main__":
    unittest.main()
