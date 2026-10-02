"""Reguły wierszy maili — czysta logika, bez bazy."""

from __future__ import annotations

import unittest
from datetime import datetime

from perno.domain import mail_rules as rules
from perno.domain import statuses as st

NOW = datetime(2026, 10, 2, 12, 0, 0)
EARLIER = datetime(2026, 9, 29, 14, 23, 11)


def row(**overrides) -> dict:
    base = {"status": st.STATUS_PENDING, "email": "a@example.com", "body_html": "<p>x</p>", "sent_at": None}
    return {**base, **overrides}


class ManualStatusTests(unittest.TestCase):
    def test_every_status_except_sending_may_be_set_by_hand(self):
        for status in st.MANUAL_STATUSES:
            self.assertIsNone(rules.manual_status_problem(status), status)

    def test_sending_is_reserved_for_the_program(self):
        self.assertIn("Wysyłanie", rules.manual_status_problem(st.STATUS_SENDING))

    def test_unknown_and_non_text_statuses_are_named_in_the_message(self):
        self.assertEqual(rules.manual_status_problem("kosmos"), "Nieprawidłowy status: 'kosmos'.")
        self.assertEqual(rules.manual_status_problem(["blad"]), "Nieprawidłowy status: ['blad'].")
        self.assertEqual(rules.manual_status_problem(None), "Nieprawidłowy status: None.")


class EditRulesTests(unittest.TestCase):
    def test_a_row_being_sent_accepts_only_status_changes(self):
        self.assertTrue(rules.edit_blocked_by_sending(st.STATUS_SENDING, ["subject"]))
        self.assertTrue(rules.edit_blocked_by_sending(st.STATUS_SENDING, ["status", "email"]))
        self.assertFalse(rules.edit_blocked_by_sending(st.STATUS_SENDING, ["status"]))
        self.assertFalse(rules.edit_blocked_by_sending(st.STATUS_PENDING, ["subject"]))

    def test_adding_an_email_promotes_a_row_without_one(self):
        current = row(status=st.STATUS_SKIPPED_NO_EMAIL, email="")
        update = rules.resolve_edit(current, {"email": "nowy@example.com"}, explicit_status=False, now=NOW)
        self.assertEqual(update["status"], st.STATUS_PENDING)

    def test_promotion_needs_a_body_too(self):
        current = row(status=st.STATUS_SKIPPED_NO_EMAIL, email="", body_html=None)
        update = rules.resolve_edit(current, {"email": "nowy@example.com"}, explicit_status=False, now=NOW)
        self.assertEqual(update["status"], st.STATUS_SKIPPED_NO_EMAIL)

    def test_removing_the_email_demotes_a_pending_row(self):
        update = rules.resolve_edit(row(), {"email": None}, explicit_status=False, now=NOW)
        self.assertEqual(update["status"], st.STATUS_SKIPPED_NO_EMAIL)

    def test_other_statuses_are_left_alone_without_an_explicit_change(self):
        current = row(status=st.STATUS_SENT, sent_at=EARLIER)
        update = rules.resolve_edit(current, {"email": None}, explicit_status=False, now=NOW)
        self.assertEqual((update["status"], update["sent_at"]), (st.STATUS_SENT, EARLIER))

    def test_explicit_status_decides_the_sent_at(self):
        sent = rules.resolve_edit(row(), {"status": st.STATUS_SENT}, explicit_status=True, now=NOW)
        self.assertEqual((sent["status"], sent["sent_at"]), (st.STATUS_SENT, NOW))
        replied = rules.resolve_edit(row(status=st.STATUS_SENT, sent_at=EARLIER), {"status": st.STATUS_REPLIED},
                                     explicit_status=True, now=NOW)
        self.assertEqual(replied["sent_at"], EARLIER)  # historia wysyłki przeżywa oznaczenie „Odpowiedział”
        reset = rules.resolve_edit(row(status=st.STATUS_SENT, sent_at=EARLIER), {"status": st.STATUS_PENDING},
                                   explicit_status=True, now=NOW)
        self.assertIsNone(reset["sent_at"])

    def test_result_contains_exactly_the_edited_fields_plus_status_and_sent_at(self):
        update = rules.resolve_edit(row(), {"subject": "Nowy"}, explicit_status=False, now=NOW)
        self.assertEqual(set(update), {"subject", "status", "sent_at"})


class SendabilityTests(unittest.TestCase):
    def test_pending_and_failed_rows_with_email_and_body_may_be_tried(self):
        self.assertIsNone(rules.sendability_problem(row()))
        self.assertIsNone(rules.sendability_problem(row(status=st.STATUS_ERROR)))

    def test_sent_rows_are_refused(self):
        self.assertEqual(rules.sendability_problem(row(status=st.STATUS_SENT)), "Ten mail już został wysłany.")

    def test_statuses_that_keep_the_send_date_are_refused_with_their_label(self):
        for status in (st.STATUS_REPLIED, st.STATUS_INTERESTED, st.STATUS_DO_NOT_CONTACT):
            message = rules.sendability_problem(row(status=status))
            self.assertIn(st.STATUS_META[status][0], message)
            self.assertIn("zmień status ręcznie", message)

    def test_missing_email_or_body_is_refused(self):
        self.assertIn("Brak e-maila lub treści", rules.sendability_problem(row(email="")))
        self.assertIn("Brak e-maila lub treści", rules.sendability_problem(row(body_html="")))
        self.assertIn("Brak e-maila lub treści", rules.sendability_problem(row(body_html=None)))


class QuotaTests(unittest.TestCase):
    def test_quota_never_goes_negative(self):
        self.assertEqual(rules.daily_quota_left(0, 50), 50)
        self.assertEqual(rules.daily_quota_left(49, 50), 1)
        self.assertEqual(rules.daily_quota_left(50, 50), 0)
        self.assertEqual(rules.daily_quota_left(80, 50), 0)


if __name__ == "__main__":
    unittest.main()
