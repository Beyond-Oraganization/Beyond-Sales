"""Reguły automatu wysyłki: ustawienia, okno godzin, decyzja tyknięcia, prognoza, błędy, podsumowania."""

from __future__ import annotations

import unittest
from datetime import date, datetime, time

from perno.domain import automat_events as ev
from perno.domain import automat_rules as ar

MON = date(2026, 10, 5)  # poniedziałek


def at(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute)


class SettingsTests(unittest.TestCase):
    def test_defaults_are_valid_and_roundtrip(self):
        s = ar.DEFAULT_SETTINGS
        self.assertEqual(ar.settings_from_mapping(s.to_json()), s)
        self.assertEqual((s.daily_cap, s.weekdays, s.window_start, s.window_end), (50, (0, 1, 2, 3, 4), time(9), time(16)))

    def test_partial_update_keeps_other_fields(self):
        s = ar.settings_from_mapping({"daily_cap": "30", "window_end": "17:30"})
        self.assertEqual((s.daily_cap, s.window_end, s.delay_min), (30, time(17, 30), 45.0))

    def test_rejects_bad_values(self):
        bad = [{"daily_cap": 0}, {"daily_cap": 501}, {"daily_cap": True}, {"daily_cap": 5.5}, {"daily_cap": "x"},
               {"weekdays": []}, {"weekdays": [7]}, {"weekdays": "pn"}, {"window_start": "25:00"},
               {"window_start": "9"}, {"window_start": "16:00"}, {"window_end": "09:00"},
               {"delay_min": 5}, {"delay_min": 200}, {"delay_max": 5000}, {"delay_min": "abc"},
               {"segment": "Inny segment"}, {"countries": ["Polska"]}, {"countries": "PL"}, {"countries": [1]},
               {"horizon_days": 0}, {"horizon_days": 61}, {"max_consecutive_errors": 0}, {"max_consecutive_errors": 21}]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ar.SettingsInvalid):
                ar.settings_from_mapping(raw)

    def test_segment_is_any_valid_label_not_only_the_built_in_ones(self):
        for name in ("both", "eventy", "kancelarie", "szkoly-jazdy"):
            self.assertEqual(ar.settings_from_mapping({"segment": name}).segment, name)

    def test_countries_accept_codes_and_markers_without_duplicates(self):
        s = ar.settings_from_mapping({"countries": ["PL", "DE", "PL", "_foreign", "_none"]})
        self.assertEqual(s.countries, ("PL", "DE", "_foreign", "_none"))

    def test_weekdays_sorted_and_deduplicated(self):
        self.assertEqual(ar.settings_from_mapping({"weekdays": [4, 0, 0, 2]}).weekdays, (0, 2, 4))

    def test_boundaries_of_every_numeric_field(self):
        ok = [{"max_consecutive_errors": 1}, {"max_consecutive_errors": 20}, {"delay_max": ar.MAX_DELAY, "delay_min": 10},
              {"window_end": "23:59"}, {"delay_min": 60, "delay_max": 60}, {"horizon_days": ar.MAX_HORIZON_DAYS}]
        for raw in ok:
            with self.subTest(raw=raw):
                ar.settings_from_mapping(raw)
        for raw in ({"window_end": "24:00"}, {"window_end": "23:60"}, {"delay_min": True}, {"delay_max": None},
                    {"delay_min": [10]}, {"delay_max": 3600.5}, {"delay_min": 9.9}):
            with self.subTest(raw=raw), self.assertRaises(ar.SettingsInvalid):
                ar.settings_from_mapping(raw)

    def test_limits_are_inclusive(self):
        self.assertEqual(ar.settings_from_mapping({"daily_cap": 1}).daily_cap, 1)
        self.assertEqual(ar.settings_from_mapping({"daily_cap": ar.HARD_MAX_DAILY_CAP}).daily_cap, 500)
        self.assertEqual(ar.settings_from_mapping({"delay_min": ar.MIN_DELAY}).delay_min, 10.0)


class CompanyGapSettingTests(unittest.TestCase):
    def test_default_is_one_mail_per_company_per_day(self):
        self.assertEqual(ar.DEFAULT_SETTINGS.company_gap_days, 1)
        self.assertEqual(ar.DEFAULT_SETTINGS.to_json()["company_gap_days"], 1)

    def test_gap_roundtrips_and_zero_means_no_gap(self):
        for value in (0, 1, 3, 30):
            s = ar.settings_from_mapping({"company_gap_days": value})
            self.assertEqual(s.company_gap_days, value)
            self.assertEqual(ar.settings_from_mapping(s.to_json()), s)

    def test_an_old_saved_json_without_the_field_gets_the_default(self):
        old = {k: v for k, v in ar.DEFAULT_SETTINGS.to_json().items() if k != "company_gap_days"}
        self.assertEqual(ar.settings_from_mapping(old).company_gap_days, 1)

    def test_bad_gaps_are_rejected(self):
        for bad in (-1, 31, True, 1.5, "x", None, [1]):
            with self.subTest(bad=bad), self.assertRaises(ar.SettingsInvalid):
                ar.settings_from_mapping({"company_gap_days": bad})

    def test_only_the_gap_differs_when_it_changes(self):
        changed = ar.settings_from_mapping({"company_gap_days": 5})
        self.assertNotEqual(changed, ar.DEFAULT_SETTINGS)
        self.assertEqual({k for k, v in changed.to_json().items() if v != ar.DEFAULT_SETTINGS.to_json()[k]}, {"company_gap_days"})

    def test_queue_filter_skips_held_addresses_and_companies_in_the_gap(self):
        flt = ar.queue_filter(ar.DEFAULT_SETTINGS, ("eventy-001", "eventy-002"))
        self.assertTrue(flt.unheld)
        self.assertEqual(flt.exclude_companies, ("eventy-001", "eventy-002"))
        self.assertEqual((flt.statuses, flt.has_email, flt.has_body), (("do_wyslania",), True, True))
        self.assertEqual(ar.queue_filter(ar.DEFAULT_SETTINGS).exclude_companies, ())


class WindowTests(unittest.TestCase):
    s = ar.DEFAULT_SETTINGS

    def test_in_window_boundaries(self):
        self.assertFalse(ar.in_window(at(5, 8, 59), self.s))
        self.assertTrue(ar.in_window(at(5, 9, 0), self.s))
        self.assertTrue(ar.in_window(at(5, 15, 59), self.s))
        self.assertFalse(ar.in_window(at(5, 16, 0), self.s))

    def test_weekend_is_not_a_send_day(self):
        self.assertFalse(ar.in_window(at(10, 10), self.s))  # sobota
        self.assertFalse(ar.is_send_day(date(2026, 10, 11), self.s))  # niedziela

    def test_next_send_time(self):
        cases = [(at(5, 8), at(5, 9)), (at(5, 12), at(5, 12)), (at(5, 16), at(6, 9)), (at(9, 17), at(12, 9)),
                 (at(10, 12), at(12, 9)), (at(11, 23, 59), at(12, 9))]
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(ar.next_send_time(now, self.s), expected)

    def test_next_send_time_across_dst_change_uses_wall_clock(self):
        # 25.10.2026 cofa zegar (CET); czasy są naiwne-lokalne, więc okno nadal zaczyna się o 9:00 czasu ściennego
        self.assertEqual(ar.next_send_time(at(24, 12), self.s), at(26, 9))
        self.assertEqual(ar.next_send_time(datetime(2026, 3, 28, 12), self.s), datetime(2026, 3, 30, 9))

    def test_single_weekday_waits_up_to_a_week(self):
        s = ar.settings_from_mapping({"weekdays": [2]})  # tylko środa
        self.assertEqual(ar.next_send_time(at(7, 10), s), at(7, 10))
        self.assertEqual(ar.next_send_time(at(7, 16), s), at(14, 9))


class PlanTickTests(unittest.TestCase):
    s = ar.DEFAULT_SETTINGS

    def plan(self, now, **over):
        args = {"armed": True, "paused": False, "armed_until": None, "sent_today": 0, **over}
        return ar.plan_tick(now, self.s, **args)

    def test_not_armed_is_idle(self):
        d = self.plan(at(5, 10), armed=False)
        self.assertEqual((d.action, d.reason), (ar.ACTION_IDLE, ar.REASON_NOT_ARMED))

    def test_paused_is_idle(self):
        d = self.plan(at(5, 10), paused=True)
        self.assertEqual((d.action, d.reason), (ar.ACTION_IDLE, ar.REASON_PAUSED))

    def test_horizon_end_wins_over_everything_but_not_armed(self):
        d = self.plan(at(10, 10), armed_until=at(10, 0), paused=True)
        self.assertEqual((d.action, d.reason), (ar.ACTION_END, ar.REASON_HORIZON))
        self.assertEqual(self.plan(at(10, 10), armed_until=at(10, 0), armed=False).action, ar.ACTION_IDLE)

    def test_send_inside_window_with_quota(self):
        self.assertEqual(self.plan(at(5, 10), sent_today=49).action, ar.ACTION_SEND)

    def test_cap_reached_waits_for_next_day(self):
        d = self.plan(at(5, 10), sent_today=50)
        self.assertEqual((d.action, d.reason, d.until), (ar.ACTION_WAIT, ar.REASON_CAP_REACHED, at(6, 9)))

    def test_cap_reached_on_friday_waits_for_monday(self):
        d = self.plan(at(9, 10), sent_today=60)
        self.assertEqual(d.until, at(12, 9))

    def test_before_and_after_window(self):
        before, after = self.plan(at(5, 8)), self.plan(at(5, 16, 30))
        self.assertEqual((before.reason, before.until), (ar.REASON_OUTSIDE_WINDOW, at(5, 9)))
        self.assertEqual((after.reason, after.until), (ar.REASON_OUTSIDE_WINDOW, at(6, 9)))

    def test_weekend_reason(self):
        d = self.plan(at(10, 10))
        self.assertEqual((d.reason, d.until), (ar.REASON_NOT_SEND_DAY, at(12, 9)))

    def test_no_catch_up_after_a_missed_day(self):
        # wtorek rano po całym dniu przerwy: limit nadal 50, nie 100
        self.assertEqual(self.plan(at(6, 9, 5), sent_today=50).action, ar.ACTION_WAIT)

    def test_horizon_ends_exactly_at_the_boundary(self):
        d = self.plan(at(10, 0), armed_until=at(10, 0))
        self.assertEqual(d.action, ar.ACTION_END)
        self.assertNotEqual(self.plan(at(9, 15, 59), armed_until=at(10, 0)).action, ar.ACTION_END)

    def test_window_end_is_exclusive_for_sending(self):
        self.assertEqual(self.plan(at(5, 15, 59)).action, ar.ACTION_SEND)
        self.assertEqual(self.plan(at(5, 16, 0)).action, ar.ACTION_WAIT)

    def test_horizon_end_is_midnight_after_last_day(self):
        armed = at(5, 14)
        self.assertEqual(ar.horizon_end(armed, self.s), at(10, 0))  # 5 dni: pn-pt
        s1 = ar.settings_from_mapping({"horizon_days": 1})
        self.assertEqual(ar.horizon_end(armed, s1), at(6, 0))


class EstimateTests(unittest.TestCase):
    s = ar.DEFAULT_SETTINGS

    def test_empty_queue(self):
        e = ar.estimate_finish(at(5, 10), self.s, 0, 0)
        self.assertEqual((e.send_days, e.last_day, e.fits_in_horizon, e.plan), (0, None, True, ()))

    def test_spreads_over_days(self):
        e = ar.estimate_finish(at(5, 10), self.s, 120, 0)
        self.assertEqual(e.send_days, 3)
        self.assertEqual(e.last_day, date(2026, 10, 7))
        self.assertEqual(e.plan, ((date(2026, 10, 5), 50), (date(2026, 10, 6), 50), (date(2026, 10, 7), 20)))

    def test_today_counts_what_was_already_sent(self):
        e = ar.estimate_finish(at(5, 10), self.s, 60, 30)
        self.assertEqual(e.plan, ((date(2026, 10, 5), 20), (date(2026, 10, 6), 40)))

    def test_after_window_today_is_skipped(self):
        e = ar.estimate_finish(at(5, 17), self.s, 10, 0)
        self.assertEqual(e.plan, ((date(2026, 10, 6), 10),))

    def test_weekend_is_skipped(self):
        e = ar.estimate_finish(at(9, 10), self.s, 120, 0)
        self.assertEqual([d for d, _ in e.plan], [date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13)])

    def test_fits_in_horizon(self):
        until = ar.horizon_end(at(5, 10), self.s)  # 10.10 00:00
        self.assertTrue(ar.estimate_finish(at(5, 10), self.s, 250, 0, until).fits_in_horizon)
        self.assertFalse(ar.estimate_finish(at(5, 10), self.s, 251, 0, until).fits_in_horizon)

    def test_exact_multiple_does_not_add_an_empty_day(self):
        e = ar.estimate_finish(at(5, 10), self.s, 100, 0)
        self.assertEqual((e.send_days, e.last_day), (2, date(2026, 10, 6)))
        self.assertEqual(e.plan, ((date(2026, 10, 5), 50), (date(2026, 10, 6), 50)))

    def test_single_mail_needs_one_day(self):
        e = ar.estimate_finish(at(5, 10), self.s, 1, 0)
        self.assertEqual((e.send_days, e.plan), (1, ((date(2026, 10, 5), 1),)))

    def test_day_with_one_slot_left_is_used(self):
        e = ar.estimate_finish(at(5, 10), self.s, 5, 49)
        self.assertEqual(e.plan, ((date(2026, 10, 5), 1), (date(2026, 10, 6), 4)))

    def test_window_end_moment_has_no_capacity_today(self):
        e = ar.estimate_finish(at(5, 16, 0), self.s, 5, 0)
        self.assertEqual(e.plan, ((date(2026, 10, 6), 5),))
        e = ar.estimate_finish(at(5, 15, 59), self.s, 5, 0)
        self.assertEqual(e.plan, ((date(2026, 10, 5), 5),))

    def test_last_day_on_horizon_boundary_does_not_fit(self):
        every_day = ar.settings_from_mapping({"weekdays": [0, 1, 2, 3, 4, 5, 6]})
        until = ar.horizon_end(at(5, 10), every_day)  # 10.10 00:00 — pięć dni: 5, 6, 7, 8, 9
        self.assertTrue(ar.estimate_finish(at(5, 10), every_day, 250, 0, until).fits_in_horizon)
        self.assertFalse(ar.estimate_finish(at(5, 10), every_day, 251, 0, until).fits_in_horizon)

    def test_queue_beyond_the_simulation_range_does_not_fit(self):
        e = ar.estimate_finish(at(5, 10), self.s, 1_000_000, 0)
        self.assertFalse(e.fits_in_horizon)
        self.assertGreater(e.send_days, 100)

    def test_plan_is_capped_at_14_days(self):
        e = ar.estimate_finish(at(5, 10), self.s, 50 * 40, 0)
        self.assertEqual(len(e.plan), 14)
        self.assertEqual(e.send_days, 40)


class GroupedEstimateTests(unittest.TestCase):
    """Prognoza z odstępem dla firmy (`estimate_finish_grouped`): kolejka to id firm, po jednym na oczekujący mail."""

    def settings(self, gap: int, **over):
        return ar.settings_from_mapping({"company_gap_days": gap, **over})

    def run_estimate(self, queue, gap=1, sent_today=0, now=None, last_sent=None, until=None, **over):
        return ar.estimate_finish_grouped(now or at(5, 10), self.settings(gap, **over), queue, sent_today, until, last_sent)

    def test_without_a_gap_it_is_the_plain_estimate(self):
        queue = ["a"] * 120
        grouped = self.run_estimate(queue, gap=0)
        plain = ar.estimate_finish(at(5, 10), self.settings(0), 120, 0)
        self.assertEqual(grouped, plain)
        self.assertFalse(grouped.limited_by_gap)

    def test_an_empty_queue_needs_no_days(self):
        e = self.run_estimate([])
        self.assertEqual((e.send_days, e.last_day, e.fits_in_horizon, e.plan, e.limited_by_gap), (0, None, True, (), False))

    def test_one_mail_per_company_per_day_stretches_a_company_with_many_addresses(self):
        e = self.run_estimate(["a", "a", "a", "b"], gap=1, daily_cap=50)
        self.assertEqual(e.plan, ((date(2026, 10, 5), 2), (date(2026, 10, 6), 1), (date(2026, 10, 7), 1)))
        self.assertEqual((e.send_days, e.last_day), (3, date(2026, 10, 7)))
        self.assertTrue(e.limited_by_gap)

    def test_companies_with_one_address_are_not_slowed_down(self):
        queue = [f"firma{n}" for n in range(120)]
        e = self.run_estimate(queue, gap=1)
        self.assertEqual(e.plan, ((date(2026, 10, 5), 50), (date(2026, 10, 6), 50), (date(2026, 10, 7), 20)))
        self.assertFalse(e.limited_by_gap)

    def test_the_daily_limit_still_applies_and_companies_are_taken_in_queue_order(self):
        queue = ["a", "a", "b", "b", "c", "c"]
        e = self.run_estimate(queue, gap=1, daily_cap=2)
        # pn: a, b; wt: a, b; potem zostaje tylko c, które ma dwa maile, więc po jednym dziennie
        self.assertEqual(e.plan, ((date(2026, 10, 5), 2), (date(2026, 10, 6), 2), (date(2026, 10, 7), 1), (date(2026, 10, 8), 1)))

    def test_a_longer_gap_waits_the_whole_gap_in_calendar_days(self):
        every_day = {"weekdays": [0, 1, 2, 3, 4, 5, 6]}
        e = self.run_estimate(["a", "a"], gap=3, **every_day)
        self.assertEqual(e.plan, ((date(2026, 10, 5), 1), (date(2026, 10, 8), 1)))  # pn → czw: trzy dni kalendarzowe

    def test_the_gap_counts_calendar_days_not_send_days(self):
        e = self.run_estimate(["a", "a"], gap=3)  # pn–pt; po poniedziałku najwcześniej czwartek
        self.assertEqual([d for d, _ in e.plan], [date(2026, 10, 5), date(2026, 10, 8)])
        friday = self.run_estimate(["a", "a"], gap=3, now=at(9, 10))  # pt → najwcześniej poniedziałek (3 dni później: nd, pn = 12.10)
        self.assertEqual([d for d, _ in friday.plan], [date(2026, 10, 9), date(2026, 10, 12)])

    def test_a_company_mailed_today_waits_for_tomorrow(self):
        e = self.run_estimate(["a", "b"], gap=1, sent_today=1, last_sent={"a": date(2026, 10, 5)})
        self.assertEqual(e.plan, ((date(2026, 10, 5), 1), (date(2026, 10, 6), 1)))

    def test_a_company_mailed_earlier_in_the_gap_waits_for_the_rest_of_it(self):
        last = {"a": date(2026, 10, 5)}  # poniedziałek; odstęp 3 → najwcześniej czwartek
        e = self.run_estimate(["a", "b"], gap=3, now=at(6, 10), last_sent=last)
        self.assertEqual(e.plan, ((date(2026, 10, 6), 1), (date(2026, 10, 8), 1)))

    def test_a_company_mailed_before_the_gap_is_free_again(self):
        last = {"a": date(2026, 10, 2)}  # piątek; odstęp 3 → w poniedziałek znów wolna
        e = self.run_estimate(["a"], gap=3, now=at(5, 10), last_sent=last)
        self.assertEqual(e.plan, ((date(2026, 10, 5), 1),))

    def test_when_everything_waits_the_empty_days_are_skipped_not_counted(self):
        last = {"a": date(2026, 10, 5)}
        e = self.run_estimate(["a"], gap=3, last_sent=last)
        self.assertEqual((e.send_days, e.plan), (1, ((date(2026, 10, 8), 1),)))

    def test_today_after_the_window_has_no_capacity(self):
        e = self.run_estimate(["a", "a"], gap=1, now=at(5, 17))
        self.assertEqual([d for d, _ in e.plan], [date(2026, 10, 6), date(2026, 10, 7)])

    def test_horizon_fit_follows_the_gap(self):
        until = ar.horizon_end(at(5, 10), self.settings(1))  # 10.10 00:00: dni 5, 6, 7, 8, 9
        self.assertTrue(self.run_estimate(["a"] * 5, until=until).fits_in_horizon)
        self.assertFalse(self.run_estimate(["a"] * 6, until=until).fits_in_horizon)  # szósty mail dopiero 12.10

    def test_a_queue_that_cannot_finish_in_the_simulation_does_not_fit(self):
        e = self.run_estimate(["a"] * 1000, gap=30)
        self.assertFalse(e.fits_in_horizon)

    def test_a_big_queue_is_computed_quickly(self):
        import time

        queue = [f"firma{n % 5000}" for n in range(50_000)]
        started = time.monotonic()
        e = self.run_estimate(queue, gap=1, daily_cap=500, weekdays=[0, 1, 2, 3, 4, 5, 6])
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertGreater(e.send_days, 10)

    def test_the_simulation_covers_exactly_400_days(self):
        every_day = {"weekdays": [0, 1, 2, 3, 4, 5, 6]}
        fits = self.run_estimate(["a"] * 400, gap=1, **every_day)
        self.assertEqual((fits.send_days, fits.fits_in_horizon), (400, True))
        too_long = self.run_estimate(["a"] * 401, gap=1, **every_day)
        self.assertEqual((too_long.send_days, too_long.fits_in_horizon), (400, False))

    def test_the_plan_lists_at_most_14_days(self):
        e = self.run_estimate(["a"] * 15, gap=1, weekdays=[0, 1, 2, 3, 4, 5, 6])
        self.assertEqual((e.send_days, len(e.plan)), (15, 14))

    def test_the_last_day_on_the_horizon_boundary_does_not_fit(self):
        every_day = {"weekdays": [0, 1, 2, 3, 4, 5, 6]}
        until = ar.horizon_end(at(5, 10), self.settings(1, **every_day))  # 10.10 00:00 — dni 5, 6, 7, 8, 9
        self.assertTrue(self.run_estimate(["a"] * 5, until=until, **every_day).fits_in_horizon)
        self.assertFalse(self.run_estimate(["a"] * 6, until=until, **every_day).fits_in_horizon)

    def test_a_gap_that_changes_nothing_is_not_reported_as_limiting(self):
        e = self.run_estimate(["a", "b", "c"], gap=1)
        self.assertFalse(e.limited_by_gap)


class FailureTests(unittest.TestCase):
    def test_classification(self):
        cases = [
            ("(535, b'5.7.8 Authentication failed')", ar.FAILURE_FATAL),
            ("Daily user sending limit exceeded", ar.FAILURE_FATAL),
            ("554 5.7.1 rejected as spam", ar.FAILURE_FATAL),
            ("{'a@b.pl': (550, b'5.1.1 user unknown')}", ar.FAILURE_RECIPIENT),
            ("553 invalid recipient", ar.FAILURE_RECIPIENT),
            ("timed out", ar.FAILURE_OTHER),
            ("[Errno 8] nodename nor servname provided", ar.FAILURE_OTHER),
            ("", ar.FAILURE_OTHER),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(ar.classify_failure(message), expected)

    def test_streak(self):
        self.assertEqual(ar.next_streak(2, sent=True, failure=None), 0)
        self.assertEqual(ar.next_streak(2, sent=False, failure=ar.FAILURE_OTHER), 3)
        self.assertEqual(ar.next_streak(2, sent=False, failure=ar.FAILURE_RECIPIENT), 2)
        self.assertEqual(ar.next_streak(0, sent=False, failure=ar.FAILURE_FATAL), 1)

    def test_pause_reason(self):
        s = ar.DEFAULT_SETTINGS  # 3 błędy z rzędu
        self.assertIsNone(ar.pause_reason(2, ar.FAILURE_OTHER, s))
        self.assertIn("3 błędów", ar.pause_reason(3, ar.FAILURE_OTHER, s) or "")
        self.assertIn("SMTP", ar.pause_reason(1, ar.FAILURE_FATAL, s) or "")
        self.assertIsNone(ar.pause_reason(0, None, s))


class SummaryTests(unittest.TestCase):
    s = ar.DEFAULT_SETTINGS

    def due(self, now, sent, reported=False, armed_at=None):
        return ar.summary_due(now, self.s, sent_today=sent, reported_today=reported, armed_at=armed_at)

    def test_due_when_cap_reached_or_window_over(self):
        self.assertTrue(self.due(at(5, 11), 50))
        self.assertTrue(self.due(at(5, 16), 12))
        self.assertFalse(self.due(at(5, 12), 12))

    def test_not_twice_and_not_on_weekend(self):
        self.assertFalse(self.due(at(5, 17), 50, reported=True))
        self.assertFalse(self.due(at(10, 17), 50))

    def test_one_sent_mail_is_enough_for_the_summary(self):
        self.assertTrue(self.due(at(5, 17), 1))

    def test_arming_at_the_exact_window_start_counts_as_before_the_window(self):
        self.assertTrue(self.due(at(5, 17), 0, armed_at=at(5, 9, 0)))
        self.assertFalse(self.due(at(5, 17), 0, armed_at=at(5, 9, 1)))

    def test_empty_day_only_when_armed_before_window(self):
        self.assertTrue(self.due(at(5, 17), 0, armed_at=at(5, 8)))
        self.assertFalse(self.due(at(5, 17), 0, armed_at=at(5, 12)))
        self.assertFalse(self.due(at(5, 17), 0, armed_at=None))

    def test_skipped_days(self):
        sent = {date(2026, 10, 5): 50, date(2026, 10, 7): 10}
        days = ar.skipped_days(sent, at(5, 8), date(2026, 10, 9), self.s)
        self.assertEqual(days, [date(2026, 10, 6), date(2026, 10, 8)])

    def test_skipped_days_ignore_weekend_and_arming_after_window(self):
        days = ar.skipped_days({}, at(9, 17), date(2026, 10, 13), self.s)  # uzbrojone w pt po oknie
        self.assertEqual(days, [date(2026, 10, 12)])

    def test_arming_after_window_skips_only_that_day(self):
        self.assertEqual(ar.skipped_days({}, at(5, 17), date(2026, 10, 7), self.s), [date(2026, 10, 6)])
        self.assertEqual(ar.skipped_days({}, at(5, 16, 0), date(2026, 10, 7), self.s), [date(2026, 10, 6)])
        self.assertEqual(ar.skipped_days({}, at(5, 15, 59), date(2026, 10, 7), self.s),
                         [date(2026, 10, 5), date(2026, 10, 6)])

    def test_skipped_days_never_include_today(self):
        self.assertEqual(ar.skipped_days({}, at(5, 8), MON, self.s), [])

    def test_sleep_gap(self):
        self.assertFalse(ar.is_sleep_gap(None, at(5, 10)))
        self.assertFalse(ar.is_sleep_gap(at(5, 10, 0), at(5, 10, 2)))
        self.assertFalse(ar.is_sleep_gap(at(5, 10, 0), at(5, 10, 3)))  # dokładnie 180 s to jeszcze norma
        self.assertTrue(ar.is_sleep_gap(at(5, 10, 0), at(5, 10, 4)))


class EventRulesTests(unittest.TestCase):
    def test_every_event_has_known_channels(self):
        for name, (level, channels, gap) in ev.EVENTS.items():
            with self.subTest(event=name):
                self.assertIn(level, (ev.LEVEL_INFO, ev.LEVEL_WARN, ev.LEVEL_CRIT))
                self.assertTrue(set(channels) <= {ev.CHANNEL_MACOS, ev.CHANNEL_NTFY, ev.CHANNEL_EMAIL})
                self.assertGreaterEqual(gap, 0)

    def test_critical_events_reach_the_phone(self):
        for name in (ev.EVENT_PAUSED, ev.EVENT_STUCK_ROW, ev.EVENT_DB_DOWN):
            self.assertIn(ev.CHANNEL_NTFY, ev.channels_for(name))

    def test_unknown_event_is_a_warning_without_channels(self):
        self.assertEqual((ev.level_for("x"), ev.channels_for("x")), (ev.LEVEL_WARN, ()))

    def test_throttling(self):
        now = at(5, 12)
        self.assertTrue(ev.should_notify(ev.EVENT_DB_DOWN, None, now))
        self.assertFalse(ev.should_notify(ev.EVENT_DB_DOWN, at(5, 11, 30), now))
        self.assertTrue(ev.should_notify(ev.EVENT_DB_DOWN, at(5, 10, 59), now))
        self.assertTrue(ev.should_notify(ev.EVENT_DAILY_SUMMARY, at(5, 11, 59), now))  # bez odstępu: zawsze


if __name__ == "__main__":
    unittest.main()
