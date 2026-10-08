"""Wiele skrzynek nadawczych w panelu: konfiguracja, stan, rotacja przy wysyłce ręcznej i limity Automatu."""

from __future__ import annotations

import os
import unittest
from datetime import timedelta
from email.utils import parseaddr

from perno import settings as cfg
from tests.fixtures import put, sample_row
from tests.test_web import SECRET, WebCase

SECOND = {"SMTP2_HOST": "smtp2.invalid", "SMTP2_PORT": "465", "SMTP2_USER": "second@example.com", "SMTP2_PASSWORD": "drugie-hasło"}


class TwoMailboxCase(WebCase):
    def setUp(self):
        super().setUp()
        self.config_path = self.app.ctx.config.path
        with open(self.config_path, "a", encoding="utf-8") as f:
            f.write("".join(f"{key}={value}\n" for key, value in SECOND.items()))

    def read_config(self):
        with open(self.config_path, encoding="utf-8") as handle:
            return handle.read()

    def senders(self):
        return [parseaddr(str(message["From"]))[1] for message in self.smtp.sent]


class StatusTests(TwoMailboxCase):
    def test_status_lists_every_mailbox_without_any_password(self):
        resp = self.call("GET", "/api/status")
        data = self.data(resp)
        self.assertEqual(data["mailboxes"], [{"address": "demo@example.com", "sent_today": 0},
                                             {"address": "second@example.com", "sent_today": 0}])
        for secret in (SECRET, SECOND["SMTP2_PASSWORD"]):
            self.assertNotIn(secret, resp.body.decode("utf-8"))


class SingleMailboxTests(WebCase):
    def test_one_mailbox_is_reported_as_a_list_of_one_and_the_old_fields_stay(self):
        data = self.data(self.call("GET", "/api/status"))
        self.assertEqual(data["mailboxes"], [{"address": "demo@example.com", "sent_today": 0}])
        self.assertEqual((data["smtp_configured"], data["smtp_user"]), (True, "demo@example.com"))

    def test_the_automat_capacity_equals_the_daily_cap(self):
        self.call("POST", "/api/automat/limits", {"daily_cap": 30})
        data = self.data(self.call("GET", "/api/automat/status"))
        self.assertEqual((data["capacity"], len(data["mailboxes"])), (30, 1))


class SendTests(TwoMailboxCase):
    def setUp(self):
        super().setUp()
        put(self.database, *[sample_row(n) for n in range(1, 6)])

    def send(self, n, cap=None):
        payload = {"id": f"eventy-{n:03d}"}
        if cap:
            payload["daily_cap"] = cap
        return self.call("POST", "/api/send-one", payload)

    def test_manual_sends_alternate_between_the_mailboxes(self):
        for n in (1, 2, 3, 4):
            self.assertEqual(self.send(n).status, 200)
        self.assertEqual(self.senders(), ["demo@example.com", "second@example.com", "demo@example.com", "second@example.com"])
        counts = {box["address"]: box["sent_today"] for box in self.data(self.call("GET", "/api/status"))["mailboxes"]}
        self.assertEqual(counts, {"demo@example.com": 2, "second@example.com": 2})

    def test_the_limit_is_per_mailbox_and_the_error_says_so(self):
        self.assertEqual([self.send(n, cap=1).status for n in (1, 2)], [200, 200])
        resp = self.send(3, cap=1)
        self.assertEqual(resp.status, 429)
        self.assertIn("każdej skrzynce", self.data(resp)["error"])

    def test_a_broken_second_mailbox_stops_sending_with_a_clear_error(self):
        with open(self.config_path, "a", encoding="utf-8") as f:
            f.write("SMTP3_USER=trzecia@example.com\n")
        resp = self.send(1)
        self.assertEqual(resp.status, 400)
        self.assertIn("SMTP3_HOST", self.data(resp)["error"])
        self.assertEqual(self.smtp.sent, [])


class ConfigTests(TwoMailboxCase):
    def fields(self):
        data = self.data(self.call("GET", "/api/config"))
        return {f["key"]: f for g in data["groups"] for f in g["fields"]}

    def test_the_second_mailbox_is_visible_and_its_password_only_flagged(self):
        fields = self.fields()
        self.assertEqual(fields["SMTP2_USER"]["value"], "second@example.com")
        self.assertEqual((fields["SMTP2_PASSWORD"]["set"], "value" in fields["SMTP2_PASSWORD"]), (True, False))

    def test_a_third_mailbox_can_be_added_and_removed_from_the_panel(self):
        third = {"SMTP3_HOST": "smtp3.invalid", "SMTP3_PORT": "587", "SMTP3_USER": "third@example.com", "SMTP3_PASSWORD": "trzecie"}
        self.assertEqual(self.call("POST", "/api/config", {"values": third}).status, 200)
        self.assertEqual(len(self.data(self.call("GET", "/api/status"))["mailboxes"]), 3)
        removal = {"values": {"SMTP3_HOST": "", "SMTP3_PORT": "", "SMTP3_USER": ""}, "clear": ["SMTP3_PASSWORD"]}
        self.assertEqual(self.call("POST", "/api/config", removal).status, 200)
        self.assertEqual(len(self.data(self.call("GET", "/api/status"))["mailboxes"]), 2)
        self.assertEqual([k for k in cfg.load_config(self.config_path) if k.startswith("SMTP3")], [])

    def test_half_a_mailbox_or_a_duplicate_address_is_a_400_and_changes_nothing(self):
        before = self.read_config()
        half = self.call("POST", "/api/config", {"values": {"SMTP3_HOST": "smtp3.invalid", "SMTP3_USER": "third@example.com"}})
        duplicate = self.call("POST", "/api/config", {"values": {"SMTP2_USER": "DEMO@example.com"}})
        self.assertEqual((half.status, duplicate.status), (400, 400))
        self.assertIn("SMTP3_PORT", self.data(half)["error"])
        self.assertIn("ten sam adres", self.data(duplicate)["error"])
        self.assertEqual(self.read_config(), before)

    def test_connection_check_covers_every_mailbox(self):
        data = self.data(self.call("POST", "/api/config/test-smtp", {}))
        self.assertTrue(data["ok"])
        self.assertEqual([r["address"] for r in data["results"]], ["demo@example.com", "second@example.com"])


class AutomatPanelTests(TwoMailboxCase):
    def test_status_reports_the_combined_capacity_and_usage_per_mailbox(self):
        self.call("POST", "/api/automat/limits", {"daily_cap": 30})
        data = self.data(self.call("GET", "/api/automat/status"))
        self.assertEqual((data["capacity"], data["settings"]["daily_cap"]), (60, 30))
        self.assertEqual([box["address"] for box in data["mailboxes"]], ["demo@example.com", "second@example.com"])
        self.assertNotIn(SECOND["SMTP2_PASSWORD"], repr(data))

    def test_the_forecast_uses_the_combined_capacity(self):
        put(self.database, *[sample_row(n) for n in range(1, 41)])
        self.call("POST", "/api/automat/limits", {"daily_cap": 10, "weekdays": [0, 1, 2, 3, 4, 5, 6], "company_gap_days": 0})
        two = self.data(self.call("GET", "/api/automat/preview"))
        os.unlink(self.config_path)
        one = self.data(self.call("GET", "/api/automat/preview"))
        self.assertEqual(two["send_days"], 2)  # 40 maili przy 2 × 10 dziennie
        self.assertEqual(one["send_days"], 4)

    def test_arming_checks_every_mailbox(self):
        put(self.database, sample_row(1))
        stamp = self.data(self.call("GET", "/api/automat/status"))["stamp"]
        with open(self.config_path, "a", encoding="utf-8") as f:
            f.write("SMTP3_USER=trzecia@example.com\n")
        resp = self.call("POST", "/api/automat/start", {"potwierdzam": True, "stamp": stamp})
        self.assertEqual(resp.status, 400)
        self.assertIn("SMTP3_HOST", self.data(resp)["error"])


class BrokenSmtp:
    """Atrapa serwera, który odrzuca logowanie (komunikat zawiera hasło, żeby sprawdzić jego wycięcie)."""

    def __enter__(self):
        raise ConnectionError(f"535 odrzucono hasło {SECOND['SMTP2_PASSWORD']}\nkoniec")

    def __exit__(self, *exc):
        return False


class MailboxStatusTests(TwoMailboxCase):
    def boxes(self, data=None):
        data = data or self.data(self.call("GET", "/api/config"))
        return {box["slot"]: box for box in data["mailboxes"]}

    def beat_off(self, **entry):
        store = self.app.ctx.automat_store.get()
        run_id = store.start_run(1)
        store.heartbeat(run_id, {"wylaczone_info": [{"address": "second@example.com", **entry}]})

    def test_untested_mailboxes_are_unknown(self):
        boxes = self.boxes()
        self.assertEqual({slot: box["state"] for slot, box in boxes.items()}, {1: "unknown", 2: "unknown"})
        self.assertEqual(boxes[2]["address"], "second@example.com")

    def test_testing_one_mailbox_checks_only_that_one_and_remembers_the_result(self):
        data = self.data(self.call("POST", "/api/config/test-smtp", {"slot": 1}))
        self.assertEqual([r["address"] for r in data["results"]], ["demo@example.com"])
        self.assertEqual({slot: box["state"] for slot, box in self.boxes(data).items()}, {1: "ok", 2: "unknown"})
        self.assertEqual(self.boxes()[1]["state"], "ok")

    def test_a_failed_test_shows_the_error_without_the_password(self):
        self.app.ctx.smtp_factory = BrokenSmtp
        resp = self.call("POST", "/api/config/test-smtp", {"slot": 2})
        data = self.data(resp)
        box = self.boxes(data)[2]
        self.assertEqual((data["ok"], box["state"]), (False, "error"))
        self.assertIn("ConnectionError: 535", box["error"])
        self.assertNotIn(SECOND["SMTP2_PASSWORD"], resp.body.decode("utf-8"))

    def test_testing_every_mailbox_updates_every_state(self):
        data = self.data(self.call("POST", "/api/config/test-smtp", {}))
        self.assertEqual({slot: box["state"] for slot, box in self.boxes(data).items()}, {1: "ok", 2: "ok"})

    def test_a_mailbox_switched_off_by_the_daemon_shows_the_reason_and_stays_off_after_a_good_test(self):
        until = (self.database.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        self.beat_off(blad="Connection unexpectedly closed", do=until)
        box = self.boxes()[2]
        self.assertEqual((box["state"], box["error"], box["until"]), ("off", "Connection unexpectedly closed", until))
        data = self.data(self.call("POST", "/api/config/test-smtp", {"slot": 2}))
        self.assertEqual(self.boxes(data)[2]["state"], "off")
        self.assertTrue(self.boxes(data)[2]["test"]["ok"])

    def test_an_expired_switch_off_is_ignored(self):
        past = (self.database.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
        self.beat_off(blad="x", do=past)
        self.assertEqual(self.boxes()[2]["state"], "unknown")

    def test_the_mailboxes_endpoint_alone_returns_the_states(self):
        data = self.data(self.call("GET", "/api/config/mailboxes"))
        self.assertEqual([box["slot"] for box in data["mailboxes"]], [1, 2])

    def test_a_bad_slot_is_rejected(self):
        for slot in ("2", True, 7, 0):
            self.assertEqual(self.call("POST", "/api/config/test-smtp", {"slot": slot}).status, 400, slot)


if __name__ == "__main__":
    unittest.main()
