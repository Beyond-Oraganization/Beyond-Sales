"""Magazyn automatu: ustawienia, stan planu, przebiegi demona i dziennik zdarzeń (sqlite w pamięci)."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

import sqlalchemy as sa

from perno.db.automat_schema import automat_stan
from perno.db.automat_store import AutomatStore, DaemonActive
from perno.domain import automat_rules as ar
from tests.fixtures import FakeClock, make_db, put, sample_row


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 10, 5, 12, 0, 0))
        self.db = make_db(self.clock)
        self.store = AutomatStore(self.db)
        self.store.ensure_schema()


class StateTests(StoreCase):
    def test_default_state(self):
        state = self.store.get_state()
        self.assertEqual(state.settings, ar.DEFAULT_SETTINGS)
        self.assertEqual((state.armed, state.paused, state.streak, state.changed_at), (False, False, 0, None))

    def test_ensure_schema_is_idempotent_and_keeps_state(self):
        self.store.save_settings(ar.settings_from_mapping({"daily_cap": 20}))
        self.store.ensure_schema()
        self.assertEqual(self.store.get_state().settings.daily_cap, 20)

    def test_settings_are_remembered_by_a_new_store_instance(self):
        wanted = ar.settings_from_mapping({"daily_cap": 33, "countries": ["PL"], "window_end": "15:00"})
        self.store.save_settings(wanted)
        other = AutomatStore(self.db)
        self.assertEqual(other.get_state().settings, wanted)
        self.assertEqual(other.get_state().changed_at, self.clock.now)

    def test_saving_while_armed_disarms_and_reports_it(self):
        self.store.arm(self.clock.now + timedelta(days=3), {"daily_cap": 50})
        self.assertTrue(self.store.get_state().armed)
        disarmed = self.store.save_settings(ar.settings_from_mapping({"daily_cap": 10}))
        state = self.store.get_state()
        self.assertTrue(disarmed)
        self.assertFalse(state.armed)
        self.assertEqual(state.settings.daily_cap, 10)

    def test_saving_while_not_armed_does_not_disarm(self):
        self.assertFalse(self.store.save_settings(ar.DEFAULT_SETTINGS))

    def test_arm_records_times_and_clears_pause(self):
        self.store.pause("coś")
        until = self.clock.now + timedelta(days=2)
        self.store.arm(until, {"x": 1})
        state = self.store.get_state()
        self.assertEqual((state.armed, state.armed_at, state.armed_until, state.paused, state.streak),
                         (True, self.clock.now, until, False, 0))

    def test_pause_and_resume(self):
        self.store.arm(self.clock.now + timedelta(days=1), {})
        self.store.set_streak(2)
        self.store.pause("trzy błędy")
        state = self.store.get_state()
        self.assertEqual((state.paused, state.pause_reason, state.armed, state.streak), (True, "trzy błędy", True, 2))
        self.store.resume()
        state = self.store.get_state()
        self.assertEqual((state.paused, state.pause_reason, state.streak), (False, "", 0))

    def test_disarm_clears_everything(self):
        self.store.arm(self.clock.now + timedelta(days=1), {})
        self.store.pause("x")
        self.assertTrue(self.store.disarm())
        state = self.store.get_state()
        self.assertEqual((state.armed, state.paused, state.pause_reason), (False, False, ""))
        self.assertFalse(self.store.disarm())

    def test_unreadable_settings_fall_back_to_defaults_without_crashing(self):
        with self.db.engine.begin() as conn:
            conn.execute(sa.update(automat_stan).values(ustawienia="{nie json"))
        state = self.store.get_state()
        self.assertEqual(state.settings, ar.DEFAULT_SETTINGS)
        self.assertFalse(state.settings_ok)

    def test_pause_reason_is_truncated(self):
        self.store.pause("x" * 900)
        self.assertEqual(len(self.store.get_state().pause_reason), 500)


class RunTests(StoreCase):
    def test_only_one_daemon_at_a_time(self):
        first = self.store.start_run(111)
        with self.assertRaises(DaemonActive):
            self.store.start_run(222)
        self.store.finish_run(first)
        self.assertIsNotNone(self.store.start_run(222))

    def test_heartbeat_and_stats(self):
        run = self.store.start_run(111)
        self.clock.advance(seconds=30)
        self.store.heartbeat(run, {"stan": "czeka", "do": "09:00"})
        active = self.store.active_run() or {}
        self.assertEqual(active["statystyki"], {"stan": "czeka", "do": "09:00"})
        self.assertEqual(active["heartbeat_at"], "2026-10-05T12:00:30")

    def test_recovers_run_of_dead_process(self):
        run = self.store.start_run(111)
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: False), [run])
        self.assertIsNone(self.store.active_run())
        latest = self.store.latest_run() or {}
        self.assertEqual(latest["status"], "przerwany")

    def test_keeps_run_of_living_process_even_with_old_heartbeat_under_limit(self):
        self.store.start_run(111)
        self.clock.advance(minutes=30)  # Mac spał, ale proces ten sam
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: True), [])
        self.assertIsNotNone(self.store.active_run())

    def test_recovers_run_with_very_old_heartbeat_even_if_pid_alive(self):
        run = self.store.start_run(111)
        self.clock.advance(hours=3)
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: True), [run])

    def test_new_daemon_can_start_after_recovery(self):
        self.store.start_run(111)
        self.store.recover_dead_runs(is_alive=lambda pid: False)
        self.assertIsNotNone(self.store.start_run(222))


class EventTests(StoreCase):
    def test_add_and_list_newest_first(self):
        self.store.add_event("info", "armed", "pierwsze")
        self.clock.advance(minutes=1)
        self.store.add_event("warn", "paused", "drugie")
        events = self.store.recent_events()
        self.assertEqual([e["tresc"] for e in events], ["drugie", "pierwsze"])
        self.assertEqual(events[0]["ts"], "2026-10-05T12:01:00")

    def test_limit(self):
        for n in range(5):
            self.store.add_event("info", "armed", str(n))
        self.assertEqual(len(self.store.recent_events(3)), 3)

    def test_has_event_by_type_and_key(self):
        self.store.add_event("info", "daily_summary", "x", key="2026-10-05")
        self.assertTrue(self.store.has_event("daily_summary", "2026-10-05"))
        self.assertFalse(self.store.has_event("daily_summary", "2026-10-06"))
        self.assertFalse(self.store.has_event("skipped_day", "2026-10-05"))

    def test_mark_notified_and_last_notified(self):
        self.assertIsNone(self.store.last_notified_at("paused"))
        event = self.store.add_event("crit", "paused", "x")
        self.assertIsNone(self.store.last_notified_at("paused"))
        self.store.mark_notified(event, ["macos", "ntfy"])
        self.assertEqual(self.store.last_notified_at("paused"), self.clock.now)
        self.assertEqual(self.store.recent_events()[0]["kanaly"], "macos,ntfy")


class SentCountsTests(StoreCase):
    def test_counts_per_day_for_statuses_that_keep_sent_at(self):
        put(self.db,
            sample_row(1, status="wyslano", sent_at="2026-10-05T09:10:00"),
            sample_row(2, status="odpowiedzial", sent_at="2026-10-05T10:00:00"),
            sample_row(3, status="wyslano", sent_at="2026-10-07T11:00:00"),
            sample_row(4, status="blad"),
            sample_row(5, status="wyslano", sent_at="2026-10-09T23:59:59"),
            sample_row(6, status="wyslano", sent_at="2026-10-10T00:00:00"))
        counts = self.db.sent_counts_by_day(date(2026, 10, 5), date(2026, 10, 9))
        self.assertEqual(counts, {date(2026, 10, 5): 2, date(2026, 10, 7): 1, date(2026, 10, 9): 1})
        self.assertEqual(self.db.sent_counts_by_day(date(2026, 10, 6), date(2026, 10, 8)), {date(2026, 10, 7): 1})


if __name__ == "__main__":
    unittest.main()
