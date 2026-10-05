"""Sterowanie automatem: zapis limitów, uzbrojenie z potwierdzeniem, podgląd kolejki i stan dla panelu."""

from __future__ import annotations

import unittest
from datetime import datetime

from perno.db.automat_store import AutomatStore
from perno.domain import automat_events as ev
from perno.domain import automat_rules as ar
from perno.notify import Notifier
from perno.services import automat_control as ctl
from perno.services import sending
from perno.services.automat_log import EventLog
from perno.settings import ConfigError
from perno.system.power import PowerState
from tests.fixtures import FakeClock, FakeSmtp, make_db, put, sample_row


class Channel:
    secrets: tuple = ()

    def __init__(self, name):
        self.name, self.got = name, []

    def send(self, notification):
        self.got.append(notification)


class ControlCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 10, 5, 10, 0, 0))
        self.db = make_db(self.clock)
        self.store = AutomatStore(self.db)
        self.store.ensure_schema()
        self.macos = Channel("macos")
        self.log = EventLog(self.store, Notifier({"macos": self.macos}), self.db.now)
        self.mailer = lambda: sending.Mailer(lambda: FakeSmtp(), sending.Sender("me@example.com"))

    def save(self, **raw):
        return ctl.save_settings(self.store, self.log, raw)

    def arm(self, **over):
        state = self.store.get_state()
        args = {"acknowledged": True, "stamp": ctl.stamp_of(state.changed_at), **over}
        return ctl.arm(self.db, self.store, self.log, self.mailer, **args)


class SettingsTests(ControlCase):
    def test_save_merges_with_stored_values_and_returns_the_stamp(self):
        self.save(daily_cap=20)
        result = self.save(window_end="15:00")
        self.assertEqual((result["settings"]["daily_cap"], result["settings"]["window_end"]), (20, "15:00"))
        self.assertEqual(result["stamp"], "2026-10-05T10:00:00")
        self.assertFalse(result["disarmed"])

    def test_invalid_settings_change_nothing(self):
        self.save(daily_cap=20)
        with self.assertRaises(ar.SettingsInvalid):
            self.save(daily_cap=0)
        self.assertEqual(self.store.get_state().settings.daily_cap, 20)

    def test_saving_while_armed_disarms_and_says_so(self):
        put(self.db, sample_row(1))
        self.save(daily_cap=5)
        self.arm()
        result = self.save(daily_cap=6)
        self.assertTrue(result["disarmed"])
        self.assertFalse(self.store.get_state().armed)
        self.assertIn("rozbrojono", self.store.recent_events()[0]["tresc"])


class ArmTests(ControlCase):
    def test_requires_acknowledgement(self):
        put(self.db, sample_row(1))
        with self.assertRaisesRegex(ctl.ArmRefused, "Sprawdziłem limity"):
            self.arm(acknowledged=False)
        self.assertFalse(self.store.get_state().armed)

    def test_stale_stamp_is_refused(self):
        put(self.db, sample_row(1))
        self.save(daily_cap=5)
        with self.assertRaises(ctl.StaleConfirmation):
            self.arm(stamp="2026-10-04T10:00:00")
        with self.assertRaises(ctl.StaleConfirmation):
            self.arm(stamp="")  # ktoś zapisał limity po wyświetleniu okna z domyślnymi

    def test_defaults_can_be_confirmed_with_empty_stamp(self):
        put(self.db, sample_row(1))
        state = self.arm(stamp="")
        self.assertTrue(state.armed)

    def test_arms_until_the_end_of_the_horizon_and_logs_it(self):
        put(self.db, sample_row(1), sample_row(2))
        self.save(daily_cap=7, horizon_days=3)
        state = self.arm()
        self.assertEqual((state.armed, state.armed_until), (True, datetime(2026, 10, 8, 0, 0)))
        self.assertEqual(self.macos.got[0].message, "Plan uzbrojony: do 7 maili dziennie, w kolejce 2.")

    def test_refuses_empty_queue_and_missing_smtp(self):
        with self.assertRaisesRegex(ctl.ArmRefused, "Kolejka jest pusta"):
            self.arm()
        put(self.db, sample_row(1))

        def broken():
            raise ConfigError("Brak SMTP_HOST")

        self.mailer = broken
        with self.assertRaises(ConfigError):
            self.arm()
        self.assertFalse(self.store.get_state().armed)

    def test_queue_respects_segment_and_country_settings(self):
        put(self.db, sample_row(1, country="DE"))
        self.save(countries=["PL"])
        with self.assertRaises(ctl.ArmRefused):
            self.arm()

    def test_disarm_pause_resume(self):
        put(self.db, sample_row(1))
        self.arm()
        ctl.pause(self.store, self.log)
        self.assertEqual((self.store.get_state().paused, self.store.get_state().pause_reason), (True, "Wstrzymano ręcznie."))
        ctl.resume(self.store, self.log)
        self.assertFalse(self.store.get_state().paused)
        self.assertTrue(ctl.disarm(self.store, self.log))
        self.assertFalse(ctl.disarm(self.store, self.log))
        types = [e["typ"] for e in self.store.recent_events()]
        self.assertEqual(types.count(ev.EVENT_DISARMED), 1)


class PreviewTests(ControlCase):
    def test_counts_queue_foreign_and_unknown_country(self):
        put(self.db, sample_row(1, country="PL"), sample_row(2, country="DE"), sample_row(3, country="DE"),
            sample_row(4), sample_row(5, status="wyslano"), sample_row(6, status="blad"), sample_row(7, email=""))
        data = ctl.preview(self.db, self.store, self.clock.now)
        self.assertEqual((data["queue"], data["foreign"], data["no_country"]), (4, 2, 1))
        self.assertEqual(dict(data["countries"]), {"PL": 1, "DE": 2, "_none": 1})

    def test_selected_countries_narrow_the_queue(self):
        put(self.db, sample_row(1, country="PL"), sample_row(2, country="DE"), sample_row(3, country="FR"))
        self.save(countries=["PL", "DE"])
        data = ctl.preview(self.db, self.store, self.clock.now)
        self.assertEqual((data["queue"], data["foreign"]), (2, 1))
        self.save(countries=["_foreign"])
        self.assertEqual(ctl.preview(self.db, self.store, self.clock.now)["queue"], 2)

    def test_forecast_over_days_and_horizon(self):
        put(self.db, *[sample_row(n) for n in range(1, 8)])
        self.save(daily_cap=3, horizon_days=2)
        data = ctl.preview(self.db, self.store, self.clock.now)
        self.assertEqual((data["send_days"], data["last_day"], data["fits_in_horizon"]), (3, "2026-10-07", False))
        self.assertEqual(data["plan"], [["2026-10-05", 3], ["2026-10-06", 3], ["2026-10-07", 1]])

    def test_empty_queue(self):
        data = ctl.preview(self.db, self.store, self.clock.now)
        self.assertEqual((data["queue"], data["send_days"], data["last_day"], data["plan"]), (0, 0, None, []))


class StatusTests(ControlCase):
    def probes(self):
        return ctl.Probes(launchd=ctl.Cached(lambda: {"installed": True, "loaded": True, "pid": 7}),
                          power=ctl.Cached(lambda: PowerState(True, 63)))

    def test_report_without_daemon(self):
        report = ctl.status_report(self.db, self.store, self.clock.now, self.probes(), channels=["macos"])
        self.assertFalse(report["daemon"]["running"])
        self.assertEqual((report["armed"], report["stamp"], report["sent_today"]), (False, "", 0))
        self.assertEqual(report["power"], {"on_battery": True, "percent": 63})
        self.assertEqual(report["launchd"]["pid"], 7)

    def test_report_with_daemon_heartbeat_age(self):
        run = self.store.start_run(99)
        self.store.heartbeat(run, {"stan": "wait"})
        self.clock.advance(seconds=42)
        report = ctl.status_report(self.db, self.store, self.clock.now, self.probes(), channels=[])
        self.assertEqual((report["daemon"]["running"], report["daemon"]["heartbeat_age"], report["daemon"]["stats"]),
                         (True, 42.0, {"stan": "wait"}))


class CachedTests(unittest.TestCase):
    def test_refreshes_only_after_ttl(self):
        calls, now = [], [0.0]
        cached = ctl.Cached(lambda: calls.append(1) or len(calls), ttl=30, clock=lambda: now[0])
        self.assertEqual((cached.get(), cached.get()), (1, 1))
        now[0] = 29.9
        self.assertEqual(cached.get(), 1)
        now[0] = 30
        self.assertEqual(cached.get(), 2)


class EventLogTests(ControlCase):
    def test_throttled_event_is_stored_but_not_notified_twice(self):
        self.log.emit(ev.EVENT_ON_BATTERY, "a")
        self.log.emit(ev.EVENT_ON_BATTERY, "b")
        self.assertEqual(len(self.macos.got), 1)
        self.assertEqual(len(self.store.recent_events()), 2)

    def test_notify_false_and_channelless_events_are_silent(self):
        self.log.emit(ev.EVENT_ARMED, "x", notify=False)
        self.log.emit(ev.EVENT_SENT, "y")
        self.assertEqual(self.macos.got, [])

    def test_build_event_log_reads_notification_settings(self):
        log = ctl.build_event_log(self.store, self.db, {"NTFY_TOPIC": "bardzo-tajny-temat", "NOTIFY_MACOS": "false"})
        self.assertEqual(set(log.notifier.channels), {"ntfy"})
        bad = ctl.build_event_log(self.store, self.db, {"NTFY_TOPIC": "x"})  # niepoprawny temat: zostaje domyślny macOS
        self.assertEqual(set(bad.notifier.channels), {"macos"})
        full = ctl.build_event_log(self.store, self.db, {
            "SMTP_HOST": "h", "SMTP_PORT": "465", "SMTP_USER": "me@example.com", "SMTP_PASSWORD": "p", "NOTIFY_EMAIL": "ja@example.com"},
            smtp_factory=lambda: FakeSmtp())
        self.assertIn("email", full.notifier.channels)


if __name__ == "__main__":
    unittest.main()
