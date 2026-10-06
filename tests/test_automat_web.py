"""Zakładka „Automat” przez aplikację (bez gniazd): trasy, potwierdzenie Startu, błędy i wygląd strony."""

from __future__ import annotations

import unittest

from perno.system.power import PowerState
from tests.fixtures import put, sample_row
from tests.test_web import WebCase


class AutomatWebCase(WebCase):
    def setUp(self):
        super().setUp()
        from perno.services import automat_control as ctl
        self.app.ctx.automat_probes = ctl.Probes(
            launchd=ctl.Cached(lambda: {"installed": False, "loaded": False, "pid": None}),
            power=ctl.Cached(lambda: PowerState(False, 100)))

    def start(self, **payload):
        status = self.data(self.call("GET", "/api/automat/status"))
        body = {"potwierdzam": True, "stamp": status["stamp"], **payload}
        return self.call("POST", "/api/automat/start", body)


class PageTests(AutomatWebCase):
    def test_page_is_in_the_menu_and_renders(self):
        resp = self.call("GET", "/automat")
        html = resp.body.decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn('href="/automat" aria-current="page">Automat</a>', html)
        self.assertIn("Sprawdziłem limity", html)
        self.assertIn('id="confirmDlg"', html)


class StatusTests(AutomatWebCase):
    def test_status_of_a_fresh_install(self):
        data = self.data(self.call("GET", "/api/automat/status"))
        self.assertEqual((data["armed"], data["stamp"], data["sent_today"], data["daemon"]["running"]),
                         (False, "", 0, False))
        self.assertEqual(data["settings"]["daily_cap"], 50)
        self.assertIn("macos", data["channels"])

    def test_preview_counts_the_queue(self):
        put(self.database, sample_row(1, country="DE"), sample_row(2, country="PL"))
        data = self.data(self.call("GET", "/api/automat/preview"))
        self.assertEqual((data["queue"], data["foreign"]), (2, 1))


class LimitsTests(AutomatWebCase):
    def test_limits_are_saved_and_come_back(self):
        resp = self.call("POST", "/api/automat/limits", {"daily_cap": 12, "weekdays": [0, 1], "countries": ["PL"]})
        self.assertEqual(resp.status, 200)
        data = self.data(self.call("GET", "/api/automat/status"))
        self.assertEqual((data["settings"]["daily_cap"], data["settings"]["weekdays"], data["settings"]["countries"]),
                         (12, [0, 1], ["PL"]))
        self.assertNotEqual(data["stamp"], "")

    def test_invalid_limits_are_a_400_with_a_message(self):
        resp = self.call("POST", "/api/automat/limits", {"daily_cap": 0})
        self.assertEqual(resp.status, 400)
        self.assertIn("dzienny limit", self.data(resp)["error"])

    def test_non_object_is_rejected_by_the_core(self):
        self.assertEqual(self.call("POST", "/api/automat/limits", raw=b"[1]").status, 400)

    def test_get_is_not_allowed_on_post_routes(self):
        self.assertEqual(self.call("GET", "/api/automat/limits").status, 404)


class StartTests(AutomatWebCase):
    def test_start_needs_the_acknowledgement(self):
        put(self.database, sample_row(1))
        resp = self.call("POST", "/api/automat/start", {"stamp": ""})
        self.assertEqual(resp.status, 400)
        self.assertIn("Sprawdziłem limity", self.data(resp)["error"])
        self.assertFalse(self.data(self.call("GET", "/api/automat/status"))["armed"])

    def test_acknowledgement_must_be_exactly_true(self):
        put(self.database, sample_row(1))
        for value in ("tak", 1, "true"):
            with self.subTest(value=value):
                resp = self.call("POST", "/api/automat/start", {"potwierdzam": value, "stamp": ""})
                self.assertEqual(resp.status, 400)

    def test_start_arms_the_plan(self):
        put(self.database, sample_row(1))
        self.call("POST", "/api/automat/limits", {"daily_cap": 5, "horizon_days": 2})
        resp = self.start()
        self.assertEqual(resp.status, 200)
        status = self.data(self.call("GET", "/api/automat/status"))
        self.assertTrue(status["armed"])
        self.assertTrue(status["armed_until"].endswith("T00:00:00"))
        self.assertTrue(any("Plan uzbrojony" in e["tresc"] for e in status["events"]))

    def test_stale_confirmation_is_a_409(self):
        put(self.database, sample_row(1))
        self.call("POST", "/api/automat/limits", {"daily_cap": 5})
        resp = self.call("POST", "/api/automat/start", {"potwierdzam": True, "stamp": "2020-01-01T00:00:00"})
        self.assertEqual(resp.status, 409)

    def test_stamp_must_be_text(self):
        resp = self.call("POST", "/api/automat/start", {"potwierdzam": True, "stamp": 5})
        self.assertEqual(resp.status, 400)

    def test_empty_queue_is_refused(self):
        resp = self.start()
        self.assertEqual(resp.status, 400)
        self.assertIn("Kolejka jest pusta", self.data(resp)["error"])

    def test_missing_smtp_is_a_400(self):
        from tests.fixtures import make_app
        put(self.database, sample_row(1))
        self.app = make_app(self.database, allowed_hosts=set(self.app.allowed_hosts))
        resp = self.start()
        self.assertEqual(resp.status, 400)

    def test_editing_limits_while_armed_disarms(self):
        put(self.database, sample_row(1))
        self.start()
        resp = self.call("POST", "/api/automat/limits", {"daily_cap": 7})
        self.assertTrue(self.data(resp)["disarmed"])
        self.assertFalse(self.data(self.call("GET", "/api/automat/status"))["armed"])

    def test_pause_resume_stop(self):
        put(self.database, sample_row(1))
        self.start()
        self.call("POST", "/api/automat/pause", {})
        status = self.data(self.call("GET", "/api/automat/status"))
        self.assertEqual((status["paused"], status["pause_reason"]), (True, "Wstrzymano ręcznie."))
        self.call("POST", "/api/automat/resume", {})
        self.assertFalse(self.data(self.call("GET", "/api/automat/status"))["paused"])
        self.assertTrue(self.data(self.call("POST", "/api/automat/stop", {}))["ok"])
        self.assertFalse(self.data(self.call("POST", "/api/automat/stop", {}))["ok"])


if __name__ == "__main__":
    unittest.main()


class DaemonWebTests(AutomatWebCase):
    def setUp(self):
        super().setUp()
        from unittest import mock
        self.control = mock.patch.multiple("perno.services.automat_control", install_daemon=mock.DEFAULT,
                                           restart_daemon=mock.DEFAULT, remove_daemon=mock.DEFAULT)
        self.mocks = self.control.start()
        self.mocks["remove_daemon"].return_value = True
        self.addCleanup(self.control.stop)

    def test_install_restart_remove(self):
        self.assertEqual(self.call("POST", "/api/automat/daemon", {"action": "install"}).status, 200)
        self.assertEqual(self.call("POST", "/api/automat/daemon", {"action": "restart"}).status, 200)
        self.assertEqual(self.data(self.call("POST", "/api/automat/daemon", {"action": "remove"})), {"ok": True})
        self.mocks["install_daemon"].assert_called_once()
        self.mocks["restart_daemon"].assert_called_once()

    def test_unknown_action_is_a_400(self):
        for payload in ({}, {"action": "kill"}, {"action": 5}):
            with self.subTest(payload=payload):
                self.assertEqual(self.call("POST", "/api/automat/daemon", payload).status, 400)

    def test_launchd_and_request_errors_are_400_with_a_message(self):
        from perno.services.automat_control import DaemonRequestError
        from perno.system.launchd import LaunchdError
        self.mocks["install_daemon"].side_effect = LaunchdError("launchctl bootstrap nie powiódł się: odmowa")
        resp = self.call("POST", "/api/automat/daemon", {"action": "install"})
        self.assertEqual(resp.status, 400)
        self.assertIn("odmowa", self.data(resp)["error"])
        self.mocks["restart_daemon"].side_effect = DaemonRequestError("Demon nie jest zainstalowany")
        self.assertEqual(self.call("POST", "/api/automat/daemon", {"action": "restart"}).status, 400)

    def test_doctor_lists_checks_and_the_wake_command(self):
        data = self.data(self.call("GET", "/api/automat/doctor"))
        self.assertTrue(all(c["level"] in ("OK", "UWAGA") for c in data["checks"]))
        self.assertTrue(any("baza:" in c["text"] for c in data["checks"]))
        self.assertEqual(data["pmset"], "sudo pmset repeat wakeorpoweron MTWRF 08:55:00")

    def test_test_notify_reports_channels(self):
        data = self.data(self.call("POST", "/api/automat/test-notify", {}))
        self.assertEqual(set(data), {"sent", "failed", "skipped"})
