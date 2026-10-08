from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest import mock

import sqlalchemy as sa
import sqlalchemy.exc as sa_exc

from perno import settings as cfg
from perno.db import RowNotFound
from perno.domain import message_ids
from perno.domain import statuses as st
from perno.mailer import mime_builder
from perno.services import sending
from perno.settings import ConfigError, Environment
from tests.fixtures import FakeClock, FakeSmtp, make_db, put, sample_row

SENDER = sending.Sender("roman@z3x.pl", "Roman")
CID_BODY = f'<p>Cześć</p><img src="cid:{mime_builder.INLINE_IMAGE_CID}">'


class NoRealSmtpMixin:
    def setUp(self):
        boom = RuntimeError("test próbował otworzyć prawdziwe połączenie SMTP")
        patches = [mock.patch("smtplib.SMTP", side_effect=boom), mock.patch("smtplib.SMTP_SSL", side_effect=boom)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class SendRowTests(NoRealSmtpMixin, unittest.TestCase):
    def send(self, database, row_id, smtp, **kw):
        return sending.send_row(database, row_id, sending.Mailer(lambda: smtp, SENDER), **kw)

    def test_success_marks_sent_and_builds_message(self):
        database = make_db()
        put(database, sample_row(1, email="cel@example.com", subject="Temat X"))
        smtp = FakeSmtp()
        result = self.send(database, "eventy-001", smtp)
        self.assertEqual(result.outcome, "sent")
        self.assertEqual(database.get("eventy-001")["status"], st.STATUS_SENT)
        message = smtp.sent[0]
        self.assertEqual((message["To"], message["Subject"]), ("cel@example.com", "Temat X"))
        self.assertIn("Roman", message["From"])

    def test_the_sent_message_carries_its_own_message_id_and_date_and_the_database_remembers_the_id(self):
        database = make_db()
        put(database, sample_row(1, email="cel@example.com"))
        smtp = FakeSmtp()
        self.send(database, "eventy-001", smtp)
        message = smtp.sent[0]
        header = str(message["Message-ID"])
        self.assertRegex(header, r"^<[^<>\s]+@z3x\.pl>$")
        self.assertTrue(message["Date"])
        row = database.get("eventy-001")
        self.assertIsNone(row.get("message_id"))  # to pole nie wychodzi do API, jak sent_from
        stored = database.engine.connect().execute(
            sa.text("SELECT message_id FROM maile WHERE id = 'eventy-001'")).scalar()
        self.assertEqual(stored, message_ids.key(header))
        self.assertEqual(stored, stored.lower())

    def test_two_mails_get_different_message_ids(self):
        database = make_db()
        put(database, sample_row(1), sample_row(2))
        smtp = FakeSmtp()
        for n in (1, 2):
            self.send(database, f"eventy-00{n}", smtp)
        self.assertNotEqual(smtp.sent[0]["Message-ID"], smtp.sent[1]["Message-ID"])

    def test_a_failed_send_stores_no_message_id(self):
        database = make_db()
        put(database, sample_row(1))
        self.send(database, "eventy-001", FakeSmtp(fail_with=RuntimeError("serwer odmówił")))
        stored = database.engine.connect().execute(sa.text("SELECT message_id FROM maile WHERE id = 'eventy-001'")).scalar()
        self.assertIsNone(stored)

    def test_the_test_message_without_an_id_leaves_the_header_to_the_server(self):
        message = sending.build_message_for_row(sample_row(1, email="cel@example.com"), "cel@example.com", SENDER)
        self.assertIsNone(message["Message-ID"])
        self.assertIsNone(message["Date"])

    @unittest.skipUnless(os.path.exists(cfg.INLINE_IMAGE_SRC), "brak obrazka źródłowego")
    def test_inline_image_follows_the_body_not_the_column(self):
        database = make_db()
        put(database,
            sample_row(1, body_html=CID_BODY, inline_images=""),
            sample_row(2, body_html="<p>bez obrazka</p>", inline_images=mime_builder.INLINE_IMAGE_CID))
        with_image, without_image = FakeSmtp(), FakeSmtp()
        self.send(database, "eventy-001", with_image)
        self.send(database, "eventy-002", without_image)
        cids = lambda m: [p["Content-ID"] for p in m.walk() if p.get("Content-ID")]
        self.assertEqual(cids(with_image.sent[0]), [f"<{mime_builder.INLINE_IMAGE_CID}>"])
        self.assertEqual(cids(without_image.sent[0]), [])

    def test_failure_marks_error_and_counts_retry(self):
        database = make_db()
        put(database, sample_row(1))
        result = self.send(database, "eventy-001", FakeSmtp(fail_with=OSError("serwer padł")))
        row = database.get("eventy-001")
        self.assertEqual((result.outcome, row["status"], row["error"], row["retry_count"]),
                         ("error", st.STATUS_ERROR, "serwer padł", "1"))

    def test_double_click_sends_only_once(self):
        database = make_db()
        put(database, sample_row(1))
        smtp = FakeSmtp()
        first = self.send(database, "eventy-001", smtp)
        second = self.send(database, "eventy-001", smtp)
        self.assertEqual((first.outcome, second.outcome), ("sent", "skipped"))
        self.assertEqual(len(smtp.sent), 1)

    def test_rows_that_must_not_be_sent_are_skipped(self):
        database = make_db()
        put(database,
            sample_row(1, email=""),
            sample_row(2, status="wyslano", sent_at="2026-09-29T14:00:00"),
            sample_row(3, status="pominieto_klient"))
        smtp = FakeSmtp()
        for n in (1, 2, 3):
            self.assertEqual(self.send(database, f"eventy-{n:03d}", smtp).outcome, "skipped")
        self.assertEqual(smtp.sent, [])

    def test_cli_mode_does_not_retry_error_rows(self):
        database = make_db()
        put(database, sample_row(1, status="blad", error="x"))
        smtp = FakeSmtp()
        self.assertEqual(self.send(database, "eventy-001", smtp, allowed_from=(st.STATUS_PENDING,)).outcome, "skipped")
        self.assertEqual(self.send(database, "eventy-001", smtp).outcome, "sent")

    def test_ctrl_c_marks_error_and_reraises(self):
        database = make_db()
        put(database, sample_row(1))
        with self.assertRaises(KeyboardInterrupt):
            self.send(database, "eventy-001", FakeSmtp(fail_with=KeyboardInterrupt()))
        row = database.get("eventy-001")
        self.assertEqual(row["status"], st.STATUS_ERROR)
        self.assertIn("Przerwano", row["error"])

    def test_failed_bookkeeping_after_successful_send_is_reported_loudly(self):
        database = make_db()
        put(database, sample_row(1, email="cel@example.com"))
        smtp = FakeSmtp()
        broken = sa_exc.OperationalError("UPDATE", {}, Exception("baza zniknęła"))
        with mock.patch.object(database, "mark_sent", side_effect=broken), \
                mock.patch("time.sleep"), self.assertLogs("perno.services.sending", level="ERROR") as logs:
            result = self.send(database, "eventy-001", smtp)
        self.assertEqual(result.outcome, "sent_unrecorded")
        self.assertEqual(len(smtp.sent), 1)
        self.assertIn("WYSŁANO", logs.output[0])
        self.assertEqual(database.get("eventy-001")["status"], st.STATUS_SENDING)

    def test_bookkeeping_is_retried_three_times_with_a_growing_pause(self):
        database = make_db()
        put(database, sample_row(1, email="cel@example.com"))
        broken = sa_exc.OperationalError("UPDATE", {}, Exception("baza zniknęła"))
        with mock.patch.object(database, "mark_sent", side_effect=broken) as mark_sent, \
                mock.patch("time.sleep") as sleep, self.assertLogs("perno.services.sending", level="ERROR"):
            self.send(database, "eventy-001", FakeSmtp())
        self.assertEqual(mark_sent.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0, 1.5])

    def test_bookkeeping_that_succeeds_on_a_retry_is_a_normal_send(self):
        database = make_db()
        put(database, sample_row(1, email="cel@example.com"))
        broken = sa_exc.OperationalError("UPDATE", {}, Exception("chwilowa awaria"))
        real_mark_sent, calls = database.mark_sent, []

        def flaky(row_id, sent_from=None, message_id=None):
            calls.append(row_id)
            if len(calls) == 1:
                raise broken
            return real_mark_sent(row_id, sent_from, message_id)

        with mock.patch.object(database, "mark_sent", side_effect=flaky), mock.patch("time.sleep") as sleep:
            result = self.send(database, "eventy-001", FakeSmtp())
        self.assertEqual(result.outcome, "sent")
        self.assertEqual(database.get("eventy-001")["status"], st.STATUS_SENT)
        self.assertEqual(sleep.call_count, 1)


FULL_CONFIG = {"SMTP_HOST": "smtp.invalid", "SMTP_PORT": "465", "SMTP_USER": "demo@example.com",
               "SMTP_PASSWORD": "fake", "SENDER_NAME": "Demo"}


class BuildMailerTests(NoRealSmtpMixin, unittest.TestCase):
    def test_sender_comes_from_the_configuration(self):
        mailer = sending.build_mailer(FULL_CONFIG, __file__, smtp_factory=lambda: None)
        self.assertEqual(mailer.sender, sending.Sender("demo@example.com", "Demo"))

    def test_injected_factory_bypasses_the_real_smtp_gate(self):
        env = Environment.from_environ({"PERNO_DB_URL": "sqlite://"})
        fake = object
        self.assertIs(sending.build_mailer(FULL_CONFIG, __file__, smtp_factory=fake, env=env).smtp_factory, fake)
        with self.assertRaises(ConfigError):
            sending.build_mailer(FULL_CONFIG, __file__, env=env)

    def test_incomplete_configuration_is_refused(self):
        with self.assertRaisesRegex(ConfigError, "SMTP_PASSWORD"):
            sending.build_mailer({**FULL_CONFIG, "SMTP_PASSWORD": ""}, __file__)
        with self.assertRaisesRegex(ConfigError, "Brak config.env"):
            sending.build_mailer({}, "/nie/ma/takiego/config.env")


class SendOneTests(NoRealSmtpMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.database = make_db()
        self.smtp = FakeSmtp()
        self.mailer_calls = 0

    def mailer(self):
        self.mailer_calls += 1
        return [sending.Mailer(lambda: self.smtp, SENDER)]

    def send(self, row_id, **kw):
        return sending.send_one(self.database, row_id, self.mailer, **kw)

    def test_sends_and_records(self):
        put(self.database, sample_row(1))
        self.assertEqual(self.send("eventy-001").outcome, "sent")
        self.assertEqual(len(self.smtp.sent), 1)

    def test_unknown_row_is_reported_before_anything_else(self):
        with self.assertRaises(RowNotFound):
            self.send("brak")
        self.assertEqual(self.mailer_calls, 0)

    def test_row_problems_win_over_configuration_problems(self):
        put(self.database, sample_row(1, status="wyslano", sent_at="2026-09-29T10:00:00"))
        def broken():
            raise ConfigError("zła konfiguracja")

        with self.assertRaisesRegex(sending.NotSendable, "już został wysłany"):
            sending.send_one(self.database, "eventy-001", broken)

    def test_configuration_problem_stops_before_claiming_the_row(self):
        put(self.database, sample_row(1))

        def broken():
            raise ConfigError("brak hasła")

        with self.assertRaises(ConfigError):
            sending.send_one(self.database, "eventy-001", broken)
        self.assertEqual(self.database.get("eventy-001")["status"], st.STATUS_PENDING)

    def test_daily_cap_counts_what_was_already_sent_today(self):
        put(self.database, sample_row(1, status="wyslano", sent_at="2026-09-30T08:00:00"), sample_row(2))
        with self.assertRaises(sending.DailyCapReached):  # FakeClock stoi na 2026-09-30
            self.send("eventy-002", daily_cap=1)
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(self.send("eventy-002", daily_cap=2).outcome, "sent")

    def test_no_cap_means_no_limit(self):
        put(self.database, sample_row(1))
        self.assertEqual(self.send("eventy-001", daily_cap=None).outcome, "sent")


class SendBatchTests(NoRealSmtpMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.database = make_db()
        put(self.database, sample_row(1), sample_row(2), sample_row(3))
        self.smtp = FakeSmtp()
        self.connections = 0

        def factory():
            self.connections += 1
            return self.smtp

        self.mailer = sending.Mailer(factory, SENDER)
        self.rows = self.database.select_candidates(limit=None)

    def test_sends_everything_over_one_connection_with_pauses_between_mails(self):
        pauses, seen = [], []
        results = sending.send_batch(self.database, self.mailer, self.rows, delay=(3.0, 9.0),
                                     on_result=lambda row, result: seen.append((row["id"], result.outcome)),
                                     sleep=pauses.append, jitter=lambda low, high: (low + high) / 2)
        self.assertEqual([result.outcome for _, result in results], ["sent"] * 3)
        self.assertEqual(seen, [("eventy-001", "sent"), ("eventy-002", "sent"), ("eventy-003", "sent")])
        self.assertEqual(pauses, [6.0, 6.0])  # między mailami, nie po ostatnim
        self.assertEqual(self.connections, 1)
        self.assertEqual(len(self.smtp.sent), 3)

    def test_only_pending_rows_are_sent_and_the_rest_is_skipped(self):
        self.database.apply_edit("eventy-002", {"status": "blad"})
        results = sending.send_batch(self.database, self.mailer, self.rows, sleep=lambda s: None)
        self.assertEqual([result.outcome for _, result in results], ["sent", "skipped", "sent"])

    def test_ctrl_c_propagates_and_the_row_in_flight_becomes_an_error(self):
        self.smtp.fail_with = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            sending.send_batch(self.database, self.mailer, self.rows, sleep=lambda s: None)
        self.assertEqual(self.database.get("eventy-001")["status"], st.STATUS_ERROR)
        self.assertEqual(self.database.get("eventy-002")["status"], st.STATUS_PENDING)


class RealSmtpGateTests(NoRealSmtpMixin, unittest.TestCase):
    CONFIG = {"SMTP_HOST": "h", "SMTP_PORT": "465", "SMTP_USER": "u", "SMTP_PASSWORD": "p"}

    def test_blocked_when_a_test_database_is_configured(self):
        env = Environment.from_environ({"PERNO_DB_URL": "sqlite://"})
        self.assertFalse(env.real_smtp_allowed)
        with self.assertRaises(ConfigError):
            sending.smtp_factory_from_config(self.CONFIG, env)

    def test_allowed_with_explicit_override_or_without_test_database(self):
        env = Environment.from_environ({"PERNO_DB_URL": "sqlite://", "PERNO_ALLOW_REAL_SMTP": "1"})
        self.assertTrue(env.real_smtp_allowed)
        self.assertTrue(callable(sending.smtp_factory_from_config(self.CONFIG, env)))
        self.assertTrue(Environment.from_environ({}).real_smtp_allowed)

    def test_process_environment_is_the_default(self):
        with mock.patch.dict(os.environ, {"PERNO_DB_URL": "sqlite://"}):
            os.environ.pop("PERNO_ALLOW_REAL_SMTP", None)
            with self.assertRaises(ConfigError):
                sending.smtp_factory_from_config(self.CONFIG)

    def test_incomplete_or_malformed_smtp_settings_are_rejected_up_front(self):
        env = Environment()
        with self.assertRaisesRegex(ConfigError, "SMTP_PASSWORD"):
            sending.smtp_factory_from_config({**self.CONFIG, "SMTP_PASSWORD": ""}, env)
        with self.assertRaisesRegex(ConfigError, "SMTP_PORT"):
            sending.smtp_factory_from_config({**self.CONFIG, "SMTP_PORT": "abc"}, env)


if __name__ == "__main__":
    unittest.main()


TWO_BOXES_CONFIG = {**FULL_CONFIG, "SMTP2_HOST": "smtp2.invalid", "SMTP2_PORT": "587", "SMTP2_USER": "second@example.com",
                    "SMTP2_PASSWORD": "fake2", "SMTP2_USE_SSL": "false"}


class BuildMailersTests(NoRealSmtpMixin, unittest.TestCase):
    def test_every_mailbox_gets_its_own_sender_and_the_shared_name(self):
        mailers = sending.build_mailers(TWO_BOXES_CONFIG, __file__, smtp_factory=lambda: None)
        self.assertEqual([m.sender for m in mailers],
                         [sending.Sender("demo@example.com", "Demo"), sending.Sender("second@example.com", "Demo")])

    def test_each_mailbox_connects_with_its_own_settings(self):
        created = []

        class Client:
            def __init__(self, *args):
                created.append(args)

        with mock.patch("perno.mailer.smtp_client.SmtpClient", Client):
            first, second = sending.build_mailers(TWO_BOXES_CONFIG, __file__, env=Environment())
            first.smtp_factory()
            second.smtp_factory()
        self.assertEqual(created, [("smtp.invalid", 465, "demo@example.com", "fake", True),
                                   ("smtp2.invalid", 587, "second@example.com", "fake2", False)])

    def test_one_mailbox_config_gives_one_mailer_like_before(self):
        self.assertEqual(len(sending.build_mailers(FULL_CONFIG, __file__, smtp_factory=lambda: None)), 1)

    def test_incomplete_or_duplicate_mailboxes_are_a_config_error(self):
        with self.assertRaises(ConfigError):
            sending.build_mailers({**FULL_CONFIG, "SMTP2_USER": "x@example.com"}, __file__, smtp_factory=lambda: None)
        with self.assertRaises(ConfigError):
            sending.build_mailers({**TWO_BOXES_CONFIG, "SMTP2_USER": "demo@example.com"}, __file__, smtp_factory=lambda: None)

    def test_a_test_database_still_blocks_real_smtp_for_every_mailbox(self):
        with self.assertRaisesRegex(ConfigError, "PERNO_DB_URL"):
            sending.build_mailers(TWO_BOXES_CONFIG, __file__, env=Environment(db_url="sqlite://"))


class RotationTests(NoRealSmtpMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock(datetime(2026, 10, 7, 10, 0, 0))
        self.database = make_db(self.clock)
        self.smtps = {"a@example.com": FakeSmtp(), "b@example.com": FakeSmtp(), "c@example.com": FakeSmtp()}
        self.mailers = [sending.Mailer(lambda smtp=smtp: smtp, sending.Sender(address)) for address, smtp in self.smtps.items()]
        put(self.database, *[sample_row(n) for n in range(1, 8)])

    def send(self, n, cap=None):
        self.clock.advance(seconds=60)
        return sending.send_one(self.database, f"eventy-{n:03d}", lambda: self.mailers, daily_cap=cap)

    def sent_by(self):
        return {address: len(smtp.sent) for address, smtp in self.smtps.items()}

    def test_mails_leave_mailbox_after_mailbox_and_the_sender_is_recorded(self):
        for n in range(1, 7):
            self.send(n)
        self.assertEqual(self.sent_by(), {"a@example.com": 2, "b@example.com": 2, "c@example.com": 2})
        order = [str(smtp.sent[0]["From"]) for smtp in self.smtps.values()]
        self.assertEqual(order, ["a@example.com", "b@example.com", "c@example.com"])
        use = self.database.sent_by_sender_today()
        self.assertEqual({key: value.sent for key, value in use.items()}, {"a@example.com": 2, "b@example.com": 2, "c@example.com": 2})

    def test_the_daily_cap_applies_to_each_mailbox_separately(self):
        for n in range(1, 7):
            self.send(n, cap=2)
        with self.assertRaisesRegex(sending.DailyCapReached, "każdej skrzynce"):
            self.send(7, cap=2)
        self.assertEqual(self.database.get("eventy-007")["status"], st.STATUS_PENDING)

    def test_a_single_mailbox_keeps_the_old_cap_message_and_semantics(self):
        self.mailers = self.mailers[:1]
        self.send(1, cap=1)
        with self.assertRaises(sending.DailyCapReached) as caught:
            self.send(2, cap=1)
        self.assertNotIn("skrzynce", str(caught.exception))

    def test_mails_sent_before_mailboxes_existed_count_for_the_primary_one(self):
        put(self.database, sample_row(20, status="wyslano", sent_at="2026-10-07T09:00:00"))
        self.send(1, cap=1)  # stara wysyłka bez skrzynki należy do głównej (a), która wyczerpała limit 1, więc mail idzie z b
        self.assertEqual(self.sent_by(), {"a@example.com": 0, "b@example.com": 1, "c@example.com": 0})

    def test_the_rotation_survives_a_restart_because_it_lives_in_the_database(self):
        self.send(1)
        self.send(2)
        self.mailers = [sending.Mailer(lambda smtp=smtp: smtp, m.sender) for smtp, m in zip(self.smtps.values(), self.mailers)]  # „nowy proces”
        self.send(3)
        self.assertEqual(self.sent_by(), {"a@example.com": 1, "b@example.com": 1, "c@example.com": 1})

    def test_usage_report_lists_mailboxes_in_configuration_order(self):
        self.send(1)
        self.assertEqual(sending.mailbox_usage(self.database, list(self.smtps)),
                         [{"address": "a@example.com", "sent_today": 1}, {"address": "b@example.com", "sent_today": 0},
                          {"address": "c@example.com", "sent_today": 0}])
        self.assertEqual(sending.mailbox_usage(self.database, []), [])

    def test_yesterdays_mails_do_not_count(self):
        self.send(1)
        self.clock.advance(days=1)
        self.assertEqual(sending.pick_mailer(self.database, self.mailers, 1).sender.address, "a@example.com")

    def test_pick_mailer_skips_unavailable_ones_and_knows_when_nothing_is_left(self):
        self.assertEqual(sending.pick_mailer(self.database, self.mailers, 5, unavailable={"a@example.com"}).sender.address, "b@example.com")
        self.assertIsNone(sending.pick_mailer(self.database, self.mailers, 5, unavailable=set(self.smtps)))
        self.assertIsNone(sending.pick_mailer(self.database, [], 5))


class SentFromTests(unittest.TestCase):
    def test_mark_sent_records_the_mailbox_and_the_api_row_does_not_expose_it(self):
        database = make_db()
        put(database, sample_row(1))
        row = database.mark_sent("eventy-001", "a@example.com")
        self.assertNotIn("sent_from", row)
        self.assertEqual(database.sent_by_sender_today()["a@example.com"].sent, 1)

    def test_addresses_differing_only_in_case_are_one_mailbox(self):
        database = make_db()
        put(database, sample_row(1), sample_row(2))
        database.mark_sent("eventy-001", "Roman@Z3X.pl")
        database.mark_sent("eventy-002", "roman@z3x.pl")
        self.assertEqual({k: v.sent for k, v in database.sent_by_sender_today().items()}, {"roman@z3x.pl": 2})


class MimeBuilderHeaderTests(unittest.TestCase):
    def build(self):
        return mime_builder.build_message(from_addr="a@x.pl", from_name="A", to_addr="b@y.pl", subject="T",
                                          html_body="<p>x</p>", text_body="x")

    def test_threading_headers_are_added_only_when_given(self):
        plain = mime_builder.add_thread_headers(self.build())
        for name in ("Message-ID", "Date", "In-Reply-To", "References"):
            self.assertIsNone(plain[name], name)
        full = mime_builder.add_thread_headers(
            self.build(), message_id="<id@x.pl>", date="Thu, 08 Oct 2026 10:00:00 +0200", in_reply_to="<prev@y.pl>",
            references="<first@x.pl> <prev@y.pl>")
        self.assertEqual((full["Message-ID"], full["In-Reply-To"], full["References"]), ("<id@x.pl>", "<prev@y.pl>", "<first@x.pl> <prev@y.pl>"))
        self.assertEqual(full["Date"], "Thu, 08 Oct 2026 10:00:00 +0200")
