"""Rotacja skrzynek nadawczych: czysta logika wyboru, bez bazy i SMTP."""

import unittest
from datetime import datetime

from perno.domain import mailboxes as mb
from perno.domain.mailboxes import DayUse

A, B, C = "a@example.com", "b@example.com", "c@example.com"
T = datetime(2026, 10, 7, 10, 0, 0)


class DayUseTests(unittest.TestCase):
    def test_usage_records_are_immutable_and_default_to_nothing_sent(self):
        use = DayUse()
        self.assertEqual((use.sent, use.last_sent), (0, None))
        with self.assertRaises(AttributeError):
            use.sent = 1  # type: ignore[misc]


class PrefixTests(unittest.TestCase):
    def test_first_mailbox_keeps_the_legacy_keys(self):
        self.assertEqual([mb.smtp_prefix(n) for n in (1, 2, 3, 10)], ["SMTP", "SMTP2", "SMTP3", "SMTP10"])


class ChooseTests(unittest.TestCase):
    def test_an_idle_day_starts_with_the_first_mailbox(self):
        self.assertEqual(mb.choose_mailbox([A, B, C], {}, 5), A)

    def test_mailboxes_take_turns(self):
        use: dict = {}
        order = []
        for minute in range(6):
            address = mb.choose_mailbox([A, B, C], use, None)
            order.append(address)
            today = use.get(address, DayUse())
            use[address] = DayUse(today.sent + 1, T.replace(minute=minute))
        self.assertEqual(order, [A, B, C, A, B, C])

    def test_the_least_used_mailbox_wins_even_out_of_turn(self):
        use = {A: DayUse(3, T), B: DayUse(1, T.replace(hour=9)), C: DayUse(2, T)}
        self.assertEqual(mb.choose_mailbox([A, B, C], use, None), B)

    def test_a_tie_goes_to_the_mailbox_that_sent_longest_ago(self):
        use = {A: DayUse(2, T.replace(minute=30)), B: DayUse(2, T.replace(minute=10)), C: DayUse(2, T.replace(minute=20))}
        self.assertEqual(mb.choose_mailbox([A, B, C], use, None), B)

    def test_the_daily_cap_is_per_mailbox(self):
        use = {A: DayUse(2, T), B: DayUse(1, T)}
        self.assertEqual(mb.choose_mailbox([A, B], use, 2), B)
        self.assertIsNone(mb.choose_mailbox([A, B], {A: DayUse(2, T), B: DayUse(2, T)}, 2))

    def test_no_cap_means_no_limit(self):
        self.assertEqual(mb.choose_mailbox([A], {A: DayUse(9999, T)}, None), A)

    def test_unavailable_mailboxes_are_skipped_regardless_of_case(self):
        self.assertEqual(mb.choose_mailbox([A, B], {}, 5, unavailable={"A@Example.com"}), B)
        self.assertIsNone(mb.choose_mailbox([A], {}, 5, unavailable=[A]))

    def test_usage_keys_are_case_insensitive(self):
        self.assertEqual(mb.choose_mailbox(["A@Example.com", B], {"a@example.com": DayUse(1, T)}, 5), B)

    def test_no_mailboxes_means_no_choice(self):
        self.assertIsNone(mb.choose_mailbox([], {}, 5))


class LegacyTests(unittest.TestCase):
    def test_mails_without_a_mailbox_count_for_the_primary_one(self):
        merged = mb.attribute_legacy({"": DayUse(3, T), "a@example.com": DayUse(1, T.replace(hour=9)), "b@example.com": DayUse(2, T)}, "A@Example.com")
        self.assertEqual(merged, {"a@example.com": DayUse(4, T), "b@example.com": DayUse(2, T)})

    def test_nothing_to_attribute_leaves_usage_alone(self):
        use = {"b@example.com": DayUse(2, T)}
        self.assertEqual(mb.attribute_legacy(use, A), use)

    def test_the_input_is_not_mutated(self):
        use = {"": DayUse(1, T)}
        mb.attribute_legacy(use, A)
        self.assertIn("", use)


class MailboxStateTests(unittest.TestCase):
    OFF = {"blad": "Connection unexpectedly closed", "do": "2026-10-08T14:00:00"}
    GOOD = {"ok": True, "message": "ok", "at": "2026-10-08T13:00:00"}
    BAD = {"ok": False, "message": "535 odrzucono", "at": "2026-10-08T13:00:00"}

    def test_nothing_known_is_unknown(self):
        self.assertEqual(mb.mailbox_state(None, None, 0)["state"], mb.STATE_UNKNOWN)

    def test_a_good_test_or_todays_sends_mean_it_works(self):
        self.assertEqual(mb.mailbox_state(None, self.GOOD, 0)["state"], mb.STATE_OK)
        self.assertEqual(mb.mailbox_state(None, None, 3)["state"], mb.STATE_OK)

    def test_a_failed_test_shows_its_error(self):
        state = mb.mailbox_state(None, self.BAD, 3)
        self.assertEqual((state["state"], state["error"]), (mb.STATE_ERROR, "535 odrzucono"))

    def test_the_daemon_switch_off_outranks_a_good_test(self):
        state = mb.mailbox_state(self.OFF, self.GOOD, 5)
        self.assertEqual((state["state"], state["error"], state["until"]), (mb.STATE_OFF, self.OFF["blad"], self.OFF["do"]))
        self.assertEqual(state["test"], self.GOOD)

    def test_a_switch_off_without_a_reason_has_no_error(self):
        self.assertIsNone(mb.mailbox_state({"do": "x"}, None, 0)["error"])


if __name__ == "__main__":
    unittest.main()
