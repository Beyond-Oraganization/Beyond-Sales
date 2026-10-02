"""Usługa sterowania skanowaniem: start przebiegu, raport stanu, odzyskiwanie i ustawienia z żądania."""

from __future__ import annotations

import unittest
from datetime import datetime

from perno.scraper import statuses as st
from perno.scraper.settings import SettingsError
from perno.scraper.store import Store
from perno.services import scan_control
from tests.fixtures import FakeClock, make_db

NOW = datetime(2026, 9, 30, 12, 0, 0)
CONFIG = {"SKRAPER_CONTACT": "ops@example.com"}


class SettingsForRunTests(unittest.TestCase):
    def test_request_values_win_over_the_configuration(self):
        settings = scan_control.settings_for_run({**CONFIG, "SKRAPER_MAX_PAGES": "40"}, {"max_pages": 10})
        self.assertEqual((settings.max_pages, settings.contact), (10, "ops@example.com"))

    def test_missing_overrides_mean_defaults_from_the_configuration(self):
        self.assertEqual(scan_control.settings_for_run({**CONFIG, "SKRAPER_MAX_PAGES": "40"}, None).max_pages, 40)

    def test_overrides_must_be_a_flat_object(self):
        for bad in ([1], "tekst", {"depth": {"a": 1}}, {"depth": [3]}):
            with self.assertRaisesRegex(SettingsError, "płaskim obiektem"):
                scan_control.settings_for_run(CONFIG, bad)

    def test_a_contact_is_required_for_the_user_agent(self):
        with self.assertRaisesRegex(SettingsError, "SKRAPER_CONTACT"):
            scan_control.settings_for_run({}, {})


class HeartbeatAgeTests(unittest.TestCase):
    def test_missing_or_unreadable_pulse_has_no_age(self):
        self.assertIsNone(scan_control.heartbeat_age(None, NOW))
        self.assertIsNone(scan_control.heartbeat_age({}, NOW))
        self.assertIsNone(scan_control.heartbeat_age({"heartbeat_at": "wczoraj"}, NOW))

    def test_age_in_seconds_never_goes_negative(self):
        self.assertEqual(scan_control.heartbeat_age({"heartbeat_at": "2026-09-30T11:59:30"}, NOW), 30.0)
        self.assertEqual(scan_control.heartbeat_age({"heartbeat_at": "2026-09-30T12:05:00"}, NOW), 0.0)  # cofnięcie zegara


class StartScanTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.db = make_db(self.clock)
        self.store = Store(self.db)
        self.store.ensure_schema()
        self.settings = scan_control.settings_for_run(CONFIG, {})
        self.spawned = []

    def spawn(self, run_id, config_path):
        self.spawned.append((run_id, config_path))
        return 4242, "/dev/null/log"

    def add_firm(self, url="https://a.example.com/"):
        return self.store.add_manual(url)["id"]

    def start(self, **kw):
        kw.setdefault("firma_ids", None)
        kw.setdefault("statuses", list(st.FIRMA_DOMYSLNIE_DO_SKANU))
        return scan_control.start_scan(self.store, self.settings, config_path="config.env", spawn=self.spawn, **kw)

    def test_queues_the_firms_spawns_the_process_and_attaches_it(self):
        self.add_firm()
        started = self.start()
        self.assertEqual((started.queued, self.spawned), (1, [(started.run_id, "config.env")]))
        run = self.store.get_run(started.run_id)
        self.assertEqual((run["pid"], run["log_path"], run["status"]), (4242, "/dev/null/log", st.RUN_URUCHAMIANIE))
        self.assertEqual(run["parametry"]["statuses"], list(st.FIRMA_DOMYSLNIE_DO_SKANU))

    def test_unknown_firm_ids_are_refused_before_a_run_exists(self):
        self.add_firm()
        with self.assertRaises(scan_control.UnknownFirms):
            self.start(firma_ids=[1, 999])
        self.assertIsNone(self.store.latest_run())
        self.assertEqual(self.spawned, [])

    def test_nothing_to_scan_closes_the_run_so_the_next_start_is_not_blocked(self):
        with self.assertRaises(scan_control.NothingToScan):
            self.start()
        self.assertIsNone(self.store.active_run())
        self.assertEqual(self.store.latest_run()["status"], st.RUN_ZAKONCZONY)
        self.assertEqual(self.spawned, [])

    def test_a_failing_spawn_closes_the_run_as_an_error_and_propagates(self):
        self.add_firm()

        def broken(run_id, config_path):
            raise OSError("brak pliku")

        with self.assertRaises(OSError):
            scan_control.start_scan(self.store, self.settings, firma_ids=None, statuses=list(st.FIRMA_DOMYSLNIE_DO_SKANU),
                                    config_path="config.env", spawn=broken)
        self.assertIsNone(self.store.active_run())
        self.assertEqual(self.store.latest_run()["status"], st.RUN_BLAD)

    def test_only_the_chosen_firms_are_queued(self):
        first, _second = self.add_firm(), self.add_firm("https://b.example.com/")
        self.assertEqual(self.start(firma_ids=[first]).queued, 1)


class StatusAndStartupTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.db = make_db(self.clock)
        self.store = Store(self.db)
        self.store.ensure_schema()

    def test_status_report_without_any_run(self):
        report = scan_control.status_report(self.store, self.db.now(), tail=lambda path: ["x"])
        self.assertEqual((report["aktywny"], report["run"], report["log"], report["heartbeat_age"]), (False, None, [], None))

    def test_status_report_of_an_active_run_tails_its_log_and_flags_a_silent_process(self):
        run_id = self.store.start_run({}, pid=1, log_path="/log")
        self.clock.advance(seconds=500)
        report = scan_control.status_report(self.store, self.db.now(), tail=lambda path: [f"log:{path}"])
        self.assertTrue(report["aktywny"])
        self.assertEqual((report["run"]["id"], report["log"]), (run_id, ["log:/log"]))
        self.assertTrue(report["nieodpowiada"])  # puls starszy niż STALE_AFTER

    def test_recover_on_start_reports_and_swallows_failures(self):
        lines = []

        def broken():
            raise RuntimeError("baza nie wstała")

        self.assertEqual(scan_control.recover_on_start(broken, lines.append), [])
        self.assertIn("Panel wysyłki działa normalnie", lines[0])

    def test_shutdown_hint_only_while_a_run_is_active(self):
        self.assertIsNone(scan_control.shutdown_hint(lambda: self.store))
        run_id = self.store.start_run({}, pid=1)
        self.assertIn(f"przebieg #{run_id}", scan_control.shutdown_hint(lambda: self.store))

        def broken():
            raise RuntimeError("baza nie wstała")

        self.assertIsNone(scan_control.shutdown_hint(broken))


if __name__ == "__main__":
    unittest.main()
