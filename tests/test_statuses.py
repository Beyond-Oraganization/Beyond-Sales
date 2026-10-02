from __future__ import annotations

import unittest
from datetime import datetime

from lib import statuses as st

NOW = datetime(2026, 9, 30, 12, 0, 0)
SENT_AT = datetime(2026, 9, 29, 14, 23, 11)
NEW_STATUSES = (st.STATUS_REPLIED, st.STATUS_INTERESTED, st.STATUS_DO_NOT_CONTACT)


class MetaTests(unittest.TestCase):
    def test_new_statuses_have_labels_and_colors(self):
        self.assertEqual(st.STATUS_META["odpowiedzial"], ("Odpowiedział", "#0e7490"))
        self.assertEqual(st.STATUS_META["zainteresowany"], ("Zainteresowany", "#be185d"))
        self.assertEqual(st.STATUS_META["nie_kontaktowac"], ("Nie kontaktować", "#7f1d1d"))

    def test_order_follows_the_lifecycle_of_a_firm(self):
        self.assertEqual(list(st.STATUS_META), [
            "do_wyslania", "wysylanie", "wyslano", "odpowiedzial", "zainteresowany",
            "blad", "pominieto_brak_email", "pominieto_klient", "oczekuje_na_tresc", "nie_kontaktowac",
        ])

    def test_new_statuses_can_be_picked_by_hand_but_never_sent_from(self):
        for status in NEW_STATUSES:
            self.assertIn(status, st.VALID_STATUSES)
            self.assertIn(status, st.MANUAL_STATUSES)
            self.assertNotIn(status, st.SENDABLE_STATUSES)
        self.assertNotIn(st.STATUS_SENDING, st.MANUAL_STATUSES)

    def test_groups_are_consistent(self):
        self.assertTrue(set(st.KEEPS_SENT_AT) <= st.VALID_STATUSES)
        self.assertTrue(set(st.KEEPS_SENT_AT) <= set(st.PROTECTED_FROM_REGENERATION))
        self.assertIn(st.STATUS_SENDING, st.PROTECTED_FROM_REGENERATION)
        for status in st.SENDABLE_STATUSES:
            self.assertNotIn(status, st.PROTECTED_FROM_REGENERATION)


class ResolveSentAtTests(unittest.TestCase):
    def test_sent_keeps_an_existing_date_and_stamps_a_missing_one(self):
        self.assertEqual(st.resolve_sent_at(st.STATUS_SENT, SENT_AT, NOW), SENT_AT)
        self.assertEqual(st.resolve_sent_at(st.STATUS_SENT, None, NOW), NOW)

    def test_statuses_after_contact_keep_the_date_whatever_it_is(self):
        for status in NEW_STATUSES:
            self.assertEqual(st.resolve_sent_at(status, SENT_AT, NOW), SENT_AT)
            self.assertIsNone(st.resolve_sent_at(status, None, NOW))  # nie zmyślamy daty, której nie znamy

    def test_every_other_status_clears_the_date(self):
        others = set(st.VALID_STATUSES) - set(st.KEEPS_SENT_AT)
        self.assertIn(st.STATUS_PENDING, others)
        for status in others:
            self.assertIsNone(st.resolve_sent_at(status, SENT_AT, NOW), status)


if __name__ == "__main__":
    unittest.main()
