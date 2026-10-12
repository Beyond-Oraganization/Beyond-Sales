"""Automat a firmy z wieloma adresami: odstęp dla firmy, czekanie zamiast rozbrajania, blokada po odpowiedzi, podgląd."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from perno.domain import automat_events as ev
from perno.domain import automat_rules as ar
from perno.domain import statuses as st
from perno.services import automat_control as ctl
from tests.fixtures import put, sample_row
from tests.test_automat_control import ControlCase
from tests.test_automat_runner import RunnerCase


class GapCase(RunnerCase):
    """Firma A ma trzy adresy (a1, a2, a3), firma B jeden (b); kolejka idzie firmami: a1, a2, a3, b."""

    def setUp(self):
        super().setUp()
        put(self.db, sample_row(1, email="a1@firma.pl", company="Firma A"), sample_row(2, email="b@inna.pl", company="Firma B"))
        self.db.add_mail("eventy-001", "a2@firma.pl")
        self.db.add_mail("eventy-001", "a3@firma.pl")

    def run_days(self, last_day: datetime) -> dict:
        """Wysyłka dzień po dniu do `last_day` (jak symulacja trzech dni): `{dzień: [adresy w kolejności wysyłki]}`."""
        self.runner.start()
        sent_by_day: dict = {}
        seen = 0
        while self.clock.now < last_day and self.store.get_state().armed:
            wait = self.runner.tick()
            for message in self.smtp.sent[seen:]:
                sent_by_day.setdefault(self.clock.now.date().isoformat(), []).append(message["To"])
            seen = len(self.smtp.sent)
            stats = (self.store.active_run() or {})["statystyki"]
            jump = datetime.fromisoformat(stats["do"]) if stats.get("do") and stats["stan"] == "wait" else self.clock.now
            self.clock.now = max(self.clock.now + timedelta(seconds=wait), jump)
        return sent_by_day


class CompanyGapSimulationTests(GapCase):
    def test_one_mail_per_company_per_day_and_the_plan_waits_instead_of_disarming(self):
        self.arm(daily_cap=10, horizon_days=7, company_gap_days=1)
        days = self.run_days(datetime(2026, 10, 10, 0, 0))
        self.assertEqual(days, {"2026-10-05": ["a1@firma.pl", "b@inna.pl"], "2026-10-06": ["a2@firma.pl"],
                                "2026-10-07": ["a3@firma.pl"]})
        self.assertEqual(len(self.events(ev.EVENT_QUEUE_EMPTY)), 1)  # dopiero po ostatnim mailu, nie w dniu z odstępem
        self.assertEqual(len(self.events(ev.EVENT_GAP_WAIT)), 2)  # pn i wt: jeden wpis na dzień
        self.assertFalse(self.store.get_state().armed)

    def test_without_a_gap_every_address_goes_out_the_same_day(self):
        self.arm(daily_cap=10, horizon_days=7, company_gap_days=0)
        days = self.run_days(datetime(2026, 10, 10, 0, 0))
        self.assertEqual(days, {"2026-10-05": ["a1@firma.pl", "a2@firma.pl", "a3@firma.pl", "b@inna.pl"]})
        self.assertEqual(self.events(ev.EVENT_GAP_WAIT), [])

    def test_a_three_day_gap_spaces_the_addresses_of_a_company(self):
        self.arm(daily_cap=10, horizon_days=14, company_gap_days=3, weekdays=[0, 1, 2, 3, 4, 5, 6])
        days = self.run_days(datetime(2026, 10, 20, 0, 0))
        self.assertEqual(days, {"2026-10-05": ["a1@firma.pl", "b@inna.pl"], "2026-10-08": ["a2@firma.pl"],
                                "2026-10-11": ["a3@firma.pl"]})

    def test_the_gap_wait_shows_up_in_the_heartbeat_for_the_panel(self):
        self.arm(daily_cap=10, company_gap_days=1)
        self.runner.start()
        for _ in range(3):  # a1, b, potem nic do wysłania
            self.runner.tick()
        stats = self.store.active_run()["statystyki"]
        self.assertEqual((stats["stan"], stats["powod"], stats["v"]), ("wait", ar.REASON_COMPANY_GAP, ar.DAEMON_VERSION))
        self.assertEqual(stats["do"], "2026-10-06T09:00:00")
        self.assertTrue(stats["delay_bands"])  # także puls oczekiwania na odstęp zna przedziały odstępu między mailami

    def test_a_reply_from_one_address_stops_the_others_and_ends_the_plan_with_an_empty_queue(self):
        self.arm(daily_cap=10, horizon_days=7, company_gap_days=1)
        self.runner.start()
        for _ in range(3):
            self.runner.tick()
        self.assertEqual([m["To"] for m in self.smtp.sent], ["a1@firma.pl", "b@inna.pl"])
        self.db.apply_edit("eventy-001", {"status": st.STATUS_REPLIED})
        self.clock.now = datetime(2026, 10, 6, 9, 0)
        self.runner.tick()
        self.assertEqual(len(self.smtp.sent), 2)  # a2 i a3 nigdy nie wyjdą
        self.assertEqual(len(self.events(ev.EVENT_QUEUE_EMPTY)), 1)
        self.assertEqual(self.db.get("eventy-001~2")["status"], st.STATUS_PENDING)
        self.assertEqual(self.db.get("eventy-001~2")["hold"], st.STATUS_REPLIED)

    def test_a_company_with_a_hold_does_not_block_the_other_companies(self):
        self.db.apply_edit("eventy-001", {"status": st.STATUS_DO_NOT_CONTACT})
        self.arm(daily_cap=10, horizon_days=7, company_gap_days=1)
        days = self.run_days(datetime(2026, 10, 10, 0, 0))
        self.assertEqual(days, {"2026-10-05": ["b@inna.pl"]})

    def test_the_log_and_notifications_never_contain_an_address(self):
        self.arm(daily_cap=10, horizon_days=7, company_gap_days=1)
        self.run_days(datetime(2026, 10, 10, 0, 0))
        texts = [event["tresc"] for event in self.events()]
        texts += [n.message for channel in self.channels.values() for n in channel.got]
        self.assertTrue(texts)
        self.assertFalse([text for text in texts if "@" in text], texts)

    def test_the_daily_summary_mentions_companies_and_the_gap(self):
        self.arm(daily_cap=2, horizon_days=7, company_gap_days=1)
        self.runner.start()
        for _ in range(3):
            self.runner.tick()
        self.clock.now = datetime(2026, 10, 5, 16, 5)
        self.runner.tick()
        summary = self.events(ev.EVENT_DAILY_SUMMARY)[0]["tresc"]
        self.assertIn("Wysłano dziś: 2 z 2.", summary)
        self.assertIn("W kolejce zostało: 2.", summary)
        self.assertIn("Dotyczy firm: 1.", summary)
        self.assertIn("Czeka na odstęp dla firmy: 2.", summary)
        self.assertIn("Odstęp dla firmy (1 dn.) wydłuża plan.", summary)


class SingleAddressSummaryTests(RunnerCase):
    def test_a_summary_without_multi_address_companies_stays_as_before(self):
        self.rows(3)
        self.arm(daily_cap=2, horizon_days=7)
        self.runner.start()
        for _ in range(3):
            self.runner.tick()
        self.clock.now = datetime(2026, 10, 5, 16, 5)
        self.runner.tick()
        summary = self.events(ev.EVENT_DAILY_SUMMARY)[0]["tresc"]
        self.assertIn("W kolejce zostało: 1.", summary)
        self.assertNotIn("Dotyczy firm", summary)
        self.assertNotIn("odstęp", summary)


class PreviewAndArmTests(ControlCase):
    def setUp(self):
        super().setUp()
        put(self.db, sample_row(1, email="a1@firma.pl"), sample_row(2, email="b@inna.pl"), sample_row(3, email="c@trzecia.pl"))
        self.db.add_mail("eventy-001", "a2@firma.pl")
        self.db.add_mail("eventy-001", "a3@firma.pl")

    def sent_today(self, mail_id: str) -> None:
        self.db.claim_for_sending(mail_id)
        self.db.mark_sent(mail_id)

    def test_preview_counts_mails_companies_held_and_deferred(self):
        self.sent_today("eventy-001")
        self.db.apply_edit("eventy-003", {"status": st.STATUS_REPLIED})  # wiersz firmy C: własny status, brak sióstr
        view = ctl.preview(self.db, self.store, self.clock.now)
        self.assertEqual((view["queue"], view["companies"], view["deferred_today"], view["held"], view["gap_days"]),
                         (3, 2, 2, 0, 1))
        self.assertTrue(view["limited_by_gap"])
        self.assertEqual([count for _, count in view["plan"]], [1, 1, 1])  # dziś b, jutro a2, pojutrze a3

    def test_preview_counts_addresses_held_by_a_sibling(self):
        self.db.apply_edit("eventy-001", {"status": st.STATUS_INTERESTED})
        view = ctl.preview(self.db, self.store, self.clock.now)
        self.assertEqual((view["queue"], view["held"], view["companies"]), (2, 2, 2))

    def test_without_a_gap_nothing_is_deferred_or_limited(self):
        self.save(company_gap_days=0)
        self.sent_today("eventy-001")
        view = ctl.preview(self.db, self.store, self.clock.now)
        self.assertEqual((view["deferred_today"], view["gap_days"], view["limited_by_gap"]), (0, 0, False))

    def test_a_plan_can_be_armed_even_when_every_company_in_the_queue_was_mailed_today(self):
        self.save(segment="eventy", daily_cap=5)
        for mail in ("eventy-001", "eventy-002", "eventy-003"):
            self.sent_today(mail)
        self.assertEqual(ctl.preview(self.db, self.store, self.clock.now)["deferred_today"], 2)
        state = self.arm()
        self.assertTrue(state.armed)  # kolejka (2 adresy A) czeka na jutro, ale plan wolno uzbroić
        self.assertIn("w kolejce 2", self.macos.got[0].message)

    def test_changing_only_the_gap_disarms_a_running_plan_and_invalidates_the_confirmation(self):
        confirmed = ctl.stamp_of(self.store.get_state().changed_at)
        self.save(daily_cap=5)
        self.arm()
        result = self.save(company_gap_days=3)
        self.assertTrue(result["disarmed"])
        self.assertFalse(self.store.get_state().armed)
        with self.assertRaises(ctl.StaleConfirmation):
            ctl.arm(self.db, self.store, self.log, self.mailer, acknowledged=True, stamp=confirmed)

    def test_an_empty_queue_has_no_companies_held_or_deferred(self):
        self.save(segment="driverzy")
        view = ctl.preview(self.db, self.store, self.clock.now)
        self.assertEqual((view["queue"], view["companies"], view["held"], view["deferred_today"]), (0, 0, 0, 0))
        self.assertEqual(view["plan"], [])

    def test_the_settings_report_the_gap(self):
        self.save(company_gap_days=4)
        self.assertEqual(self.store.get_state().settings.company_gap_days, 4)


if __name__ == "__main__":
    unittest.main()
