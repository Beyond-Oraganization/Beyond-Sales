"""Pętla automatu: decyzje, wysyłka, pauzy, powiadomienia i symulacja kilku dni na atrapach (bez sieci i prawdziwej bazy)."""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta
from email.utils import parseaddr
from unittest import mock

from sqlalchemy import exc as sa_exc

from perno.db.automat_store import AutomatStore, DaemonActive
from perno.domain import automat_events as ev
from perno.domain import automat_rules as ar
from perno.notify import Notifier
from perno.services import sending
from perno.services.automat import AutomatRunner, RunnerDeps
from perno.settings import ConfigError
from perno.system.power import PowerState
from tests.fixtures import FakeClock, FakeSmtp, make_db, put, sample_row

MON_10 = datetime(2026, 10, 5, 10, 0, 0)  # poniedziałek, 10:00


class RecordingChannel:
    secrets: tuple = ()

    def __init__(self, name):
        self.name, self.got = name, []

    def send(self, notification):
        self.got.append(notification)


class FakeGuard:
    def __init__(self):
        self.calls = []

    def set_needed(self, needed):
        self.calls.append(needed)

    def release(self):
        self.calls.append("release")


class FakePinger:
    def __init__(self):
        self.pings, self.fails = 0, 0

    def ping(self):
        self.pings += 1

    def fail(self):
        self.fails += 1


class RunnerCase(unittest.TestCase):
    start_at = MON_10

    def setUp(self):
        self.clock = FakeClock(self.start_at)
        self.db = make_db(self.clock)
        self.store = AutomatStore(self.db)
        self.store.ensure_schema()
        self.smtp = FakeSmtp()
        self.channels = {name: RecordingChannel(name) for name in ("macos", "ntfy", "email")}
        self.guard, self.pinger = FakeGuard(), FakePinger()
        self.battery = PowerState(False, 100)
        self.mailer_error = None
        self.runner = AutomatRunner(RunnerDeps(
            db=self.db, store=self.store, mailers=self._mailers, notifier=Notifier(self.channels), pinger=self.pinger,
            guard=self.guard, power=lambda: self.battery, sleep=lambda s: self.clock.advance(seconds=s),
            jitter=lambda low, high: low), pid=4242)

    def _mailers(self):
        if self.mailer_error:
            raise self.mailer_error
        return [sending.Mailer(lambda: self.smtp, sending.Sender("me@example.com", "Me"))]

    def rows(self, count, **over):
        put(self.db, *[sample_row(n, **over) for n in range(1, count + 1)])

    def arm(self, **settings):
        base = {"daily_cap": 2, "horizon_days": 5, **settings}
        self.store.save_settings(ar.settings_from_mapping(base))
        self.store.arm(ar.horizon_end(self.clock.now, ar.settings_from_mapping(base)), {})

    def events(self, event_type=None):
        return [e for e in self.store.recent_events(500) if event_type in (None, e["typ"])]

    def notified(self, name):
        return [n.title for n in self.channels[name].got]

    def status_of(self, n):
        return (self.db.get(f"eventy-{n:03d}") or {})["status"]


class TickTests(RunnerCase):
    def test_the_daemon_announces_its_version_at_start_and_on_every_beat(self):
        self.rows(1)
        self.runner.start()
        self.assertEqual(self.store.active_run()["statystyki"]["v"], ar.DAEMON_VERSION)  # panel wie od razu, że demon jest aktualny
        self.runner.tick()
        self.assertEqual(self.store.active_run()["statystyki"]["v"], ar.DAEMON_VERSION)

    def test_not_armed_sends_nothing(self):
        self.rows(2)
        self.runner.start()
        self.assertEqual(self.runner.tick(), ar.TICK_SECONDS)
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(self.status_of(1), "do_wyslania")

    def test_sends_one_mail_per_tick_with_the_configured_delay(self):
        self.rows(3)
        self.arm(daily_cap=5)
        self.runner.start()
        wait = self.runner.tick()
        self.assertEqual((len(self.smtp.sent), self.status_of(1), self.status_of(2), wait), (1, "wyslano", "do_wyslania", 45.0))
        self.assertEqual(self.events("sent")[0]["tresc"], "Wysłano: Firma 1 (eventy-001).")

    def test_cap_reached_waits_for_next_day_and_sends_one_summary(self):
        self.rows(5)
        self.arm(daily_cap=2)
        self.runner.start()
        for _ in range(2):
            self.runner.tick()
            self.clock.advance(seconds=45)
        wait = self.runner.tick()
        self.assertEqual(len(self.smtp.sent), 2)
        self.assertEqual(wait, ar.TICK_SECONDS)
        self.runner.tick()
        summaries = self.events(ev.EVENT_DAILY_SUMMARY)
        self.assertEqual(len(summaries), 1)
        self.assertIn("Wysłano dziś: 2 z 2.", summaries[0]["tresc"])
        self.assertIn("W kolejce zostało: 3.", summaries[0]["tresc"])
        for channel in ("macos", "ntfy", "email"):
            self.assertEqual(self.notified(channel).count("Automat: podsumowanie dnia"), 1)
        self.assertEqual(self.notified("ntfy"), ["Automat: podsumowanie dnia"])  # start demona idzie tylko na macOS

    def test_outside_window_waits_and_caps_the_nap(self):
        self.clock.now = datetime(2026, 10, 5, 8, 0)
        self.rows(1)
        self.arm()
        self.runner.start()
        self.assertEqual(self.runner.tick(), ar.TICK_SECONDS)
        self.clock.now = datetime(2026, 10, 5, 8, 59, 50)
        self.assertEqual(self.runner.tick(), 10.0)
        self.assertEqual(self.smtp.sent, [])

    def test_weekend_sends_nothing(self):
        self.clock.now = datetime(2026, 10, 10, 11, 0)
        self.rows(1)
        self.arm()
        self.runner.start()
        self.runner.tick()
        self.assertEqual(self.smtp.sent, [])

    def test_segment_and_country_filters(self):
        put(self.db, sample_row(1, country="DE"), sample_row(2, country="PL"), sample_row(3, segment="driverzy", country="PL"))
        self.arm(daily_cap=10, countries=["PL"], segment="eventy")
        self.runner.start()
        self.runner.tick()
        self.assertEqual([self.status_of(1), self.status_of(2)], ["do_wyslania", "wyslano"])
        self.assertEqual((self.db.get("driverzy-003") or {})["status"], "do_wyslania")

    def test_error_rows_are_not_retried_automatically(self):
        put(self.db, sample_row(1, status="blad"))
        self.arm()
        self.runner.start()
        self.runner.tick()
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(self.events(ev.EVENT_QUEUE_EMPTY)[0]["typ"], ev.EVENT_QUEUE_EMPTY)

    def test_empty_queue_disarms_and_notifies(self):
        self.arm()
        self.runner.start()
        self.runner.tick()
        self.assertFalse(self.store.get_state().armed)
        self.assertEqual(self.notified("ntfy"), ["Automat: kolejka pusta, koniec planu"])

    def test_horizon_end_disarms(self):
        self.rows(3)
        self.arm(horizon_days=1)
        self.runner.start()
        self.clock.now = datetime(2026, 10, 6, 0, 0)
        self.runner.tick()
        self.assertFalse(self.store.get_state().armed)
        self.assertEqual(self.smtp.sent, [])
        self.assertIn("Plan na 1 dni dobiegł końca", self.events(ev.EVENT_HORIZON_END)[0]["tresc"])

    def test_second_daemon_is_refused(self):
        self.runner.start()
        other = AutomatRunner(self.runner.d, pid=1)
        with mock.patch.object(self.store, "recover_dead_runs", return_value=[]), self.assertRaises(DaemonActive):
            other.start()

    def test_summary_warns_when_the_queue_does_not_fit_the_horizon(self):
        self.rows(10)
        self.arm(daily_cap=2, horizon_days=1)
        self.runner.start()
        for _ in range(2):
            self.runner.tick()
            self.clock.advance(seconds=45)
        self.runner.tick()
        text = self.events(ev.EVENT_DAILY_SUMMARY)[0]["tresc"]
        self.assertIn("W kolejce zostało: 8.", text)
        self.assertIn("Przy tym tempie koniec około 09.10.2026 (4 dni wysyłki).", text)
        self.assertIn("nie zmieści całej kolejki", text)

    def test_stuck_claims_are_reported_at_start(self):
        self.rows(1, status="wysylanie")
        self.clock.advance(minutes=30)
        self.runner.start()
        self.assertEqual(self.status_of(1), "blad")
        self.assertEqual(self.notified("email"), ["Automat: sprawdź folder Wysłane"])
        self.assertIn("eventy-001", self.events(ev.EVENT_STUCK_ROW)[0]["tresc"])


class FailureTests(RunnerCase):
    def test_three_timeouts_pause_and_alert(self):
        self.rows(6)
        self.arm(daily_cap=10)
        self.runner.start()
        self.smtp.fail_with = TimeoutError("timed out")
        for _ in range(3):
            self.runner.tick()
            self.clock.advance(seconds=45)
        state = self.store.get_state()
        self.assertTrue(state.paused)
        self.assertIn("3 błędów", state.pause_reason)
        self.assertEqual(self.notified("ntfy"), ["Automat: WSTRZYMANY po błędach"])
        self.assertEqual(self.pinger.fails, 1)
        self.runner.tick()
        self.assertEqual(self.status_of(4), "do_wyslania")  # po pauzie nic nie idzie dalej

    def test_resume_continues_and_success_resets_the_streak(self):
        self.rows(6)
        self.arm(daily_cap=10)
        self.runner.start()
        self.smtp.fail_with = TimeoutError("timed out")
        for _ in range(2):
            self.runner.tick()
        self.assertEqual(self.store.get_state().streak, 2)
        self.smtp.fail_with = None
        self.runner.tick()
        self.assertEqual(self.store.get_state().streak, 0)
        self.assertFalse(self.store.get_state().paused)

    def test_authentication_error_pauses_at_once(self):
        self.rows(2)
        self.arm()
        self.runner.start()
        self.smtp.fail_with = RuntimeError("(535, b'5.7.8 Authentication failed')")
        self.runner.tick()
        self.assertTrue(self.store.get_state().paused)

    def test_recipient_errors_do_not_pause(self):
        self.rows(6)
        self.arm(daily_cap=10)
        self.runner.start()
        self.smtp.fail_with = RuntimeError("{'a@b.pl': (550, b'user unknown')}")
        for _ in range(5):
            self.runner.tick()
        self.assertFalse(self.store.get_state().paused)
        self.assertEqual(len(self.events(ev.EVENT_ROW_ERROR)), 5)

    def test_smtp_config_error_pauses_without_leaking_anything(self):
        self.rows(1)
        self.arm()
        self.runner.start()
        self.mailer_error = ConfigError("W config.env brakuje: SMTP_HOST.")
        self.runner.tick()
        self.assertIn("SMTP_HOST", self.store.get_state().pause_reason)

    def test_unrecorded_send_pauses_and_hides_the_address(self):
        self.rows(2)
        self.arm()
        self.runner.start()
        with mock.patch.object(self.db, "mark_sent", side_effect=sa_exc.OperationalError("x", {}, Exception("y"))), \
                mock.patch("perno.services.sending.time.sleep"):
            self.runner.tick()
        self.assertTrue(self.store.get_state().paused)
        text = self.channels["ntfy"].got[0].message
        self.assertIn("WYSZEDŁ", text)
        self.assertNotIn("@", text)

    def test_database_outage_alerts_once_per_hour_and_survives(self):
        self.runner.start()
        with mock.patch.object(self.db, "sent_today_count", side_effect=sa_exc.OperationalError("x", {}, Exception("y"))):
            self.assertEqual(self.runner._safe_tick(), 60.0)
            self.runner._safe_tick()
        self.assertEqual(self.notified("ntfy"), ["Automat: baza niedostępna"])
        self.clock.advance(hours=2)
        with mock.patch.object(self.db, "sent_today_count", side_effect=sa_exc.OperationalError("x", {}, Exception("y"))):
            self.runner._safe_tick()
        self.assertEqual(len(self.notified("ntfy")), 2)

    def test_unexpected_exception_does_not_kill_the_loop(self):
        self.runner.start()
        with mock.patch.object(self.db, "sent_today_count", side_effect=ValueError("bug")):
            self.assertEqual(self.runner._safe_tick(), 60.0)
        self.assertEqual(self.notified("macos"), ["Automat: nieoczekiwany błąd"])


class AwarenessTests(RunnerCase):
    def test_sleep_gap_is_reported_once(self):
        self.rows(5)
        self.arm(daily_cap=5)
        self.runner.start()
        self.runner.tick()
        self.clock.advance(hours=2)
        self.runner.tick()
        self.runner.tick()
        self.assertEqual(len(self.events(ev.EVENT_RESUMED)), 1)
        self.assertIn("120 min", self.events(ev.EVENT_RESUMED)[0]["tresc"])

    def test_gap_while_not_armed_is_silent(self):
        self.runner.start()
        self.runner.tick()
        self.clock.advance(hours=2)
        self.runner.tick()
        self.assertEqual(self.events(ev.EVENT_RESUMED), [])

    def test_skipped_days_are_reported_once(self):
        self.clock.now = datetime(2026, 10, 5, 8, 0)
        self.rows(5)
        self.arm(daily_cap=5)
        self.runner.start()
        self.clock.now = datetime(2026, 10, 7, 8, 0)  # komputer spał pn i wt
        self.runner.tick()
        self.runner._checked_day = None
        self.runner.tick()
        self.assertEqual(len(self.events(ev.EVENT_SKIPPED_DAY)), 2)
        self.assertEqual(len(self.notified("ntfy")), 1)
        self.assertIn("05.10, 06.10", self.events(ev.EVENT_SKIPPED_DAY)[0]["tresc"])

    def test_on_battery_warning_once_a_day(self):
        self.rows(5)
        self.arm(daily_cap=5)
        self.battery = PowerState(True, 63)
        self.runner.start()
        self.runner.tick()
        self.runner.tick()
        self.assertEqual(len(self.events(ev.EVENT_ON_BATTERY)), 1)
        self.assertIn("63%", self.events(ev.EVENT_ON_BATTERY)[0]["tresc"])

    def test_sleep_guard_and_pinger_follow_the_window(self):
        self.rows(5)
        self.arm(daily_cap=5)
        self.runner.start()
        self.runner.tick()
        self.clock.now = datetime(2026, 10, 5, 17, 0)
        self.runner.tick()
        self.assertEqual(self.guard.calls, [True, False])
        self.assertEqual(self.pinger.pings, 1)

    def test_heartbeat_publishes_state_for_the_panel(self):
        self.rows(1)
        self.runner.start()
        self.runner.tick()
        stats = (self.store.active_run() or {})["statystyki"]
        self.assertEqual((stats["stan"], stats["powod"], stats["dzis"], stats["limit"]), ("idle", "not_armed", 0, 50))


class LoopTests(RunnerCase):
    def test_run_forever_stops_and_releases_everything(self):
        stop = threading.Event()
        self.runner.d.sleep = lambda s: stop.set()
        self.runner.run_forever(stop)
        self.assertIsNone(self.store.active_run())
        self.assertEqual(self.guard.calls[-1], "release")

    def test_run_forever_ticks_before_sleeping(self):
        stop = threading.Event()
        naps = []
        self.runner.d.sleep = lambda s: (naps.append(s), stop.set())
        self.runner.run_forever(stop)
        self.assertEqual(naps, [ar.TICK_SECONDS])
        self.assertEqual((self.store.latest_run() or {})["statystyki"]["powod"], "not_armed")

    def test_sleep_notes_gaps_and_beats(self):
        self.runner.start()
        self.runner.d.sleep = lambda s: self.clock.advance(hours=1)  # „Mac zasnął” w trakcie snu
        self.runner._sleep(60, threading.Event())
        self.assertEqual(self.runner._gap_minutes, 60)

    def test_three_day_simulation_with_a_weekend(self):
        """Pt-pn-wt przy limicie 2 i 5 mailach: 2 + 0 (weekend) + 2 + 1, potem kolejka pusta."""
        self.clock.now = datetime(2026, 10, 9, 8, 30)  # piątek
        self.rows(5)
        self.arm(daily_cap=2, horizon_days=7)
        self.runner.start()
        sent_by_day: dict = {}
        end = datetime(2026, 10, 14, 0, 0)
        while self.clock.now < end and self.store.get_state().armed:
            before = len(self.smtp.sent)
            wait = self.runner.tick()
            if len(self.smtp.sent) > before:
                sent_by_day[self.clock.now.date()] = sent_by_day.get(self.clock.now.date(), 0) + 1
            stats = (self.store.active_run() or {})["statystyki"]
            skip = stats.get("do") and stats["stan"] == "wait"
            self.clock.now = max(self.clock.now + timedelta(seconds=wait),
                                 datetime.fromisoformat(stats["do"]) if skip else self.clock.now)
        self.assertEqual({d.isoformat(): n for d, n in sent_by_day.items()},
                         {"2026-10-09": 2, "2026-10-12": 2, "2026-10-13": 1})
        self.assertEqual(len(self.smtp.sent), 5)
        self.assertEqual(len(self.events(ev.EVENT_QUEUE_EMPTY)), 1)
        self.assertEqual(len(self.events(ev.EVENT_DAILY_SUMMARY)), 2)  # pt i pn; wt kończy pusta kolejka
        self.assertEqual(self.events(ev.EVENT_SKIPPED_DAY), [])


if __name__ == "__main__":
    unittest.main()


class MultiMailboxCase(RunnerCase):
    """Dwie skrzynki z osobnymi atrapami SMTP; `daily_cap` obowiązuje każdą z nich."""

    def setUp(self):
        super().setUp()
        self.smtp_a, self.smtp_b = self.smtp, FakeSmtp()
        self.boxes = [sending.Mailer(lambda: self.smtp_a, sending.Sender("a@example.com", "Me")),
                      sending.Mailer(lambda: self.smtp_b, sending.Sender("b@example.com", "Me"))]
        self.runner.d.mailers = self._boxes

    def _boxes(self):
        if self.mailer_error:
            raise self.mailer_error
        return self.boxes

    def run_ticks(self, count):
        for _ in range(count):
            self.runner.tick()
            self.clock.advance(seconds=45)

    def sent_by(self):
        return len(self.smtp_a.sent), len(self.smtp_b.sent)


class RotationTests(MultiMailboxCase):
    def test_mails_alternate_between_the_mailboxes_and_are_attributed(self):
        self.rows(4)
        self.arm(daily_cap=5)
        self.runner.start()
        self.run_ticks(4)
        self.assertEqual(self.sent_by(), (2, 2))
        self.assertEqual([parseaddr(str(m["From"]))[1] for m in self.smtp_a.sent + self.smtp_b.sent],
                         ["a@example.com", "a@example.com", "b@example.com", "b@example.com"])
        self.assertEqual({k: v.sent for k, v in self.db.sent_by_sender_today().items()}, {"a@example.com": 2, "b@example.com": 2})

    def test_the_cap_is_per_mailbox_and_the_day_total_is_their_sum(self):
        self.rows(6)
        self.arm(daily_cap=2)
        self.runner.start()
        self.run_ticks(6)
        self.assertEqual(self.sent_by(), (2, 2))  # 2 skrzynki × limit 2; reszta czeka do jutra
        beat = self.store.active_run()["statystyki"]
        self.assertEqual((beat["dzis"], beat["limit"], beat["skrzynki"]), (4, 4, 2))
        self.assertEqual(self.runner.tick(), ar.TICK_SECONDS)

    def test_the_daily_summary_shows_the_total_cap_and_each_mailbox(self):
        self.rows(6)
        self.arm(daily_cap=2)
        self.runner.start()
        self.run_ticks(6)
        text = self.events(ev.EVENT_DAILY_SUMMARY)[0]["tresc"]
        self.assertIn("Wysłano dziś: 4 z 4.", text)
        self.assertIn("Skrzynki: nr 1: 2, nr 2: 2.", text)
        for channel in self.channels.values():  # na zewnętrzne kanały nie wychodzą adresy skrzynek
            self.assertTrue(all("@" not in note.message for note in channel.got))

    def test_a_single_mailbox_still_works_exactly_as_before(self):
        self.boxes = self.boxes[:1]
        self.rows(3)
        self.arm(daily_cap=2)
        self.runner.start()
        self.run_ticks(3)
        self.assertEqual(self.sent_by(), (2, 0))
        self.assertEqual(self.store.active_run()["statystyki"]["limit"], 2)


class MailboxFailureTests(MultiMailboxCase):
    AUTH = "(535, b'5.7.8 Authentication failed')"

    def test_a_broken_mailbox_is_switched_off_and_the_other_carries_on_without_a_pause(self):
        self.rows(5)
        self.arm(daily_cap=10)
        self.runner.start()
        self.smtp_a.fail_with = RuntimeError(self.AUTH)
        self.run_ticks(5)
        state = self.store.get_state()
        self.assertFalse(state.paused)
        self.assertEqual(self.sent_by(), (0, 4))  # pierwszy mail zawiódł na a, kolejne cztery poszły z b
        self.assertEqual(self.status_of(1), "blad")
        self.assertEqual(len(self.events(ev.EVENT_MAILBOX_OFF)), 1)
        self.assertIn("Skrzynka nr 1 wyłączona", self.events(ev.EVENT_MAILBOX_OFF)[0]["tresc"])
        self.assertNotIn("@", self.events(ev.EVENT_MAILBOX_OFF)[0]["tresc"])
        self.assertEqual(self.notified("macos").count("Automat: skrzynka wyłączona"), 1)
        self.assertEqual(self.store.active_run()["statystyki"]["skrzynki"], 1)

    def test_the_switched_off_mailbox_comes_back_the_next_day(self):
        self.rows(6)
        self.arm(daily_cap=10, horizon_days=5)
        self.runner.start()
        self.smtp_a.fail_with = RuntimeError(self.AUTH)
        self.run_ticks(2)
        self.smtp_a.fail_with = None
        self.clock.advance(days=1)
        self.clock.now = self.clock.now.replace(hour=10, minute=0)
        self.run_ticks(2)
        self.assertEqual(len(self.smtp_a.sent), 1)

    def test_when_the_last_working_mailbox_fails_the_automat_pauses_as_before(self):
        self.rows(6)
        self.arm(daily_cap=10)
        self.runner.start()
        self.smtp_a.fail_with = RuntimeError(self.AUTH)
        self.smtp_b.fail_with = RuntimeError(self.AUTH)
        self.run_ticks(3)
        state = self.store.get_state()
        self.assertTrue(state.paused)
        self.assertIn("Błąd SMTP wymagający uwagi", state.pause_reason)

    def test_repeated_timeouts_on_one_mailbox_switch_only_that_one_off(self):
        self.rows(12)
        self.arm(daily_cap=20)
        self.runner.start()
        self.smtp_a.fail_with = TimeoutError("timed out")
        self.run_ticks(8)  # a, b, a, b, a (trzecia porażka a z rzędu: wyłączona), potem trzy razy b; b zeruje serię globalną
        self.assertFalse(self.store.get_state().paused)
        self.assertEqual(len(self.events(ev.EVENT_MAILBOX_OFF)), 1)
        self.assertEqual(len(self.smtp_b.sent), 5)

    def test_a_switched_off_mailbox_does_not_use_up_the_cap_of_the_others(self):
        self.rows(10)
        self.arm(daily_cap=3)
        self.runner.start()
        self.run_ticks(4)  # a, b, a, b
        self.assertEqual(self.sent_by(), (2, 2))
        self.runner._suspended = {"b@example.com": self.clock.now + timedelta(hours=5)}
        self.run_ticks(3)
        self.assertEqual(self.sent_by(), (3, 2))  # a ma jeszcze 1 z 3, mimo że razem wysłano już 4
        beat = self.store.active_run()["statystyki"]
        self.assertEqual((beat["stan"], beat["powod"], beat["wylaczone"]), ("wait", "cap_reached", [2]))

    def test_a_connection_failure_switches_the_mailbox_off_for_an_hour_then_retries_it(self):
        self.rows(14)
        self.arm(daily_cap=20)
        self.runner.start()
        self.smtp_a.fail_with = TimeoutError("timed out")
        self.run_ticks(6)
        self.assertEqual(len(self.events(ev.EVENT_MAILBOX_OFF)), 1)
        self.assertIn("na 60 min", self.events(ev.EVENT_MAILBOX_OFF)[0]["tresc"])
        beat = self.store.active_run()["statystyki"]
        self.assertEqual(beat["wylaczone"], [1])
        info = beat["wylaczone_info"]
        self.assertEqual([(box["address"], box["blad"]) for box in info], [("a@example.com", "timed out")])
        self.assertGreater(info[0]["do"], self.clock.now.strftime("%Y-%m-%dT%H:%M:%S"))
        self.smtp_a.fail_with = None
        before = len(self.smtp_a.sent)
        self.run_ticks(4)
        self.assertEqual(len(self.smtp_a.sent), before)  # jeszcze odpoczywa
        self.clock.advance(minutes=60)
        self.run_ticks(4)
        self.assertGreater(len(self.smtp_a.sent), before)
        self.assertEqual(self.store.active_run()["statystyki"]["wylaczone_info"], [])

    def test_a_mailbox_that_still_fails_after_the_break_is_switched_off_again_at_once(self):
        self.rows(20)
        self.arm(daily_cap=20)
        self.runner.start()
        self.smtp_a.fail_with = TimeoutError("timed out")
        self.run_ticks(6)
        self.clock.advance(minutes=60)
        self.run_ticks(4)
        self.assertEqual(len(self.events(ev.EVENT_MAILBOX_OFF)), 2)  # jedna próba, nie trzy
        self.assertFalse(self.store.get_state().paused)

    def test_the_summary_says_which_mailbox_is_off(self):
        self.rows(6)
        self.arm(daily_cap=2)
        self.runner.start()
        self.run_ticks(2)
        self.runner._suspended = {"b@example.com": self.clock.now + timedelta(hours=5)}
        self.run_ticks(4)
        text = self.events(ev.EVENT_DAILY_SUMMARY)[0]["tresc"]
        self.assertIn("Wysłano dziś: 3 z 4.", text)
        self.assertIn("Wyłączone po błędach: skrzynka nr 2.", text)

    def test_recipient_errors_never_switch_a_mailbox_off(self):
        self.rows(8)
        self.arm(daily_cap=20)
        self.runner.start()
        self.smtp_a.fail_with = RuntimeError("{'x@y.pl': (550, b'user unknown')}")
        self.run_ticks(8)
        self.assertEqual(self.events(ev.EVENT_MAILBOX_OFF), [])
        self.assertFalse(self.store.get_state().paused)

    def test_a_successful_send_resets_the_failure_count_of_that_mailbox(self):
        self.rows(10)
        self.arm(daily_cap=20)
        self.runner.start()
        for fail in (True, False, True, False, True, False):  # a: porażka, b, a: porażka … (b zawsze działa)
            self.smtp_a.fail_with = TimeoutError("t") if fail else None
            self.run_ticks(2)
        self.assertEqual(self.events(ev.EVENT_MAILBOX_OFF), [])

    def test_all_mailboxes_off_pauses_with_a_clear_reason(self):
        self.rows(4)
        self.arm(daily_cap=10)
        self.runner.start()
        until = self.clock.now + timedelta(hours=5)
        self.runner._suspended = {"a@example.com": until, "b@example.com": until}
        self.runner.tick()
        self.assertTrue(self.store.get_state().paused)
        self.assertIn("Wszystkie skrzynki", self.store.get_state().pause_reason)
