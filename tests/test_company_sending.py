"""Wysyłka do firmy z wieloma adresami: blokada przez status innego adresu, jedna wysyłka naraz, jeden adres na przebieg."""

from __future__ import annotations

import io
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

from perno import db as dbmod
from perno import settings as cfg
from perno.cli import send as send_cli
from perno.domain import statuses as st
from perno.domain.rowfilter import RowFilter
from perno.services import sending
from tests.fixtures import FakeSmtp, make_db, put, sample_row


def company_with_siblings(database, count: int = 2, **overrides):
    put(database, sample_row(1, **overrides))
    return [database.add_mail("eventy-001", f"adres{n}@firma.pl")["id"] for n in range(2, count + 1)]


class ClaimGuardTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        self.sibling, = company_with_siblings(self.database)

    def test_an_address_cannot_be_claimed_while_another_one_of_the_company_is_being_sent(self):
        self.assertIsNotNone(self.database.claim_for_sending("eventy-001"))
        self.assertIsNone(self.database.claim_for_sending(self.sibling))
        self.assertEqual(self.database.get(self.sibling)["status"], st.STATUS_PENDING)

    def test_the_sibling_can_be_claimed_after_the_first_one_is_done(self):
        self.database.claim_for_sending("eventy-001")
        self.database.mark_sent("eventy-001")
        self.assertIsNotNone(self.database.claim_for_sending(self.sibling))

    def test_a_failed_sibling_does_not_block(self):
        self.database.claim_for_sending("eventy-001")
        self.database.mark_error("eventy-001", "SMTP")
        self.assertIsNotNone(self.database.claim_for_sending(self.sibling))

    def test_every_stop_status_of_a_sibling_blocks_the_claim(self):
        for status in st.COMPANY_STOPS:
            self.database.apply_edit("eventy-001", {"status": status})
            self.assertIsNone(self.database.claim_for_sending(self.sibling), status)
        self.database.apply_edit("eventy-001", {"status": st.STATUS_SENT})
        self.assertIsNotNone(self.database.claim_for_sending(self.sibling))

    def test_another_company_is_not_affected(self):
        put(self.database, sample_row(2))
        self.database.claim_for_sending("eventy-001")
        self.assertIsNotNone(self.database.claim_for_sending("eventy-002"))

    def test_race_between_siblings_has_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = make_db(url=f"sqlite:///{os.path.join(tmp, 'race.db')}")
            put(database, sample_row(1))
            ids = ["eventy-001", database.add_mail("eventy-001", "b@firma.pl")["id"], database.add_mail("eventy-001", "c@firma.pl")["id"]]
            winners, barrier = [], threading.Barrier(9)

            def worker(row_id):
                barrier.wait()
                if database.claim_for_sending(row_id):
                    winners.append(row_id)

            threads = [threading.Thread(target=worker, args=(ids[n % 3],)) for n in range(9)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(len(winners), 1)
            self.assertEqual(self.sending_count(database), 1)

    @staticmethod
    def sending_count(database) -> int:
        return database.count_rows(RowFilter(statuses=(st.STATUS_SENDING,)))


class SendRowTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        self.sibling, = company_with_siblings(self.database)
        self.smtp = FakeSmtp()
        self.mailer = sending.Mailer(lambda: self.smtp, sending.Sender("ja@example.com", "Ja"))

    def test_a_held_address_is_skipped_with_the_reason_and_nothing_is_sent(self):
        self.database.apply_edit("eventy-001", {"status": st.STATUS_REPLIED})
        result = sending.send_row(self.database, self.sibling, self.mailer)
        self.assertEqual(result.outcome, "skipped")
        self.assertIn("Odpowiedział", result.message)
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(self.database.get(self.sibling)["status"], st.STATUS_PENDING)

    def test_an_address_skipped_because_a_sibling_is_in_flight_says_so(self):
        self.database.claim_for_sending("eventy-001")
        result = sending.send_row(self.database, self.sibling, self.mailer)
        self.assertEqual(result.outcome, "skipped")
        self.assertIn("jest właśnie wysyłany", result.message)

    def test_send_one_refuses_a_held_address_before_touching_smtp(self):
        self.database.apply_edit("eventy-001", {"status": st.STATUS_DO_NOT_CONTACT})
        with self.assertRaisesRegex(sending.NotSendable, "Nie kontaktować"):
            sending.send_one(self.database, self.sibling, lambda: [self.mailer])
        self.assertEqual(self.smtp.sent, [])

    def test_send_one_still_sends_an_unheld_sibling(self):
        sending.send_one(self.database, "eventy-001", lambda: [self.mailer])
        result = sending.send_one(self.database, self.sibling, lambda: [self.mailer])
        self.assertEqual(result.outcome, "sent")
        self.assertEqual([m["To"] for m in self.smtp.sent], ["firma@example.com", "adres2@firma.pl"])

    def test_the_batch_skips_a_sibling_whose_company_just_replied(self):
        rows = [self.database.get("eventy-001"), self.database.get(self.sibling)]
        results = []

        def on_result(row, result):
            results.append((row["id"], result.outcome))
            if row["id"] == "eventy-001":  # w trakcie przebiegu ktoś oznacza firmę jako „Odpowiedział”
                self.database.apply_edit("eventy-001", {"status": st.STATUS_REPLIED})

        sending.send_batch(self.database, self.mailer, rows, delay=(0, 0), on_result=on_result, sleep=lambda s: None)
        self.assertEqual(results, [("eventy-001", "sent"), (self.sibling, "skipped")])
        self.assertEqual(len(self.smtp.sent), 1)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        put(self.database, sample_row(2))
        self.sibling, = company_with_siblings(self.database)
        put(self.database, sample_row(3))

    def ids(self, **kwargs) -> list:
        return self.database.list_ids(RowFilter(**kwargs), "default")

    def test_ready_and_unheld_leave_out_addresses_held_by_a_sibling(self):
        self.database.apply_edit("eventy-001", {"status": st.STATUS_REPLIED})
        self.assertEqual(self.ids(ready=True), ["eventy-002", "eventy-003"])
        self.assertEqual(self.ids(unheld=True), ["eventy-001", "eventy-002", "eventy-003"])  # własny status nie wstrzymuje
        self.assertEqual(self.ids(), ["eventy-001", self.sibling, "eventy-002", "eventy-003"])  # bez filtra jest na liście

    def test_without_a_stop_status_every_address_is_ready(self):
        self.assertEqual(self.ids(ready=True), ["eventy-001", self.sibling, "eventy-002", "eventy-003"])
        self.assertEqual(self.ids(unheld=True), self.ids())

    def test_excluded_companies_drop_all_their_addresses(self):
        self.assertEqual(self.ids(exclude_companies=("eventy-001",)), ["eventy-002", "eventy-003"])
        self.assertEqual(self.ids(exclude_companies=("eventy-001", "eventy-003")), ["eventy-002"])
        self.assertEqual(self.ids(exclude_companies=()), ["eventy-001", self.sibling, "eventy-002", "eventy-003"])

    def test_candidates_can_be_limited_to_one_address_per_company(self):
        both = [r["id"] for r in self.database.select_candidates(limit=None)]
        self.assertEqual(both, ["eventy-001", self.sibling, "eventy-002", "eventy-003"])
        first = [r["id"] for r in self.database.select_candidates(limit=None, per_company=True)]
        self.assertEqual(first, ["eventy-001", "eventy-002", "eventy-003"])
        self.database.apply_edit("eventy-001", {"status": st.STATUS_SENT})
        again = [r["id"] for r in self.database.select_candidates(limit=None, per_company=True)]
        self.assertEqual(again, [self.sibling, "eventy-002", "eventy-003"])  # pierwszy adres firmy, który czeka na wysłanie

    def test_candidates_can_leave_out_held_addresses(self):
        self.database.apply_edit("eventy-001", {"status": st.STATUS_REPLIED})
        unheld = [r["id"] for r in self.database.select_candidates(limit=None, unheld=True)]
        self.assertEqual(unheld, ["eventy-002", "eventy-003"])
        held_too = [r["id"] for r in self.database.select_candidates(limit=None)]
        self.assertEqual(held_too, [self.sibling, "eventy-002", "eventy-003"])


class CliSendTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = os.path.join(tmp.name, "fake.env")
        with open(config, "w", encoding="utf-8") as handle:
            handle.write("SMTP_HOST=smtp.invalid\nSMTP_PORT=465\nSMTP_USER=demo@example.com\nSMTP_PASSWORD=fake\nSENDER_NAME=Demo\n")
        self.config = config
        for patcher in (mock.patch.dict(os.environ, {"PERNO_DB_URL": f"sqlite:///{os.path.join(tmp.name, 'd.db')}",
                                                     "PERNO_ALLOW_REAL_SMTP": "1"}),
                        mock.patch.object(cfg, "LOG_DIR", os.path.join(tmp.name, "logs")),
                        mock.patch.object(cfg, "INDEX_HTML", os.path.join(tmp.name, "index.html")),
                        mock.patch("perno.mailer.smtp_client.SmtpClient", FakeClient)):
            patcher.start()
            self.addCleanup(patcher.stop)
        FakeClient.sent = []
        self.database = dbmod.Database.from_config({})
        self.database.ensure_schema()
        put(self.database, sample_row(1, email="a@firma.pl", company="Zzzfirma 1"),
            sample_row(2, email="b@inna.pl", company="Zzzfirma 2"))
        self.database.add_mail("eventy-001", "druga@firma.pl")

    def run_cli(self, *argv):
        args = send_cli.build_parser().parse_args(["--config", self.config, "--yes", "--delay-min", "0", "--delay-max", "0",
                                                   "--limit", "50", "--company", "Zzzfirma", *argv])
        with redirect_stdout(io.StringIO()) as out:
            code = send_cli.run(args)
        return code, out.getvalue()

    def test_by_default_one_address_per_company_goes_out_per_run(self):
        self.run_cli()
        self.assertEqual(sorted(m["To"] for m in FakeClient.sent), ["a@firma.pl", "b@inna.pl"])
        self.assertEqual(self.database.get("eventy-001~2")["status"], st.STATUS_PENDING)
        self.run_cli()  # następny przebieg wysyła drugi adres firmy
        self.assertEqual(sorted(m["To"] for m in FakeClient.sent), ["a@firma.pl", "b@inna.pl", "druga@firma.pl"])

    def test_all_addresses_sends_every_address_in_one_run(self):
        self.run_cli("--all-addresses")
        self.assertEqual(sorted(m["To"] for m in FakeClient.sent), ["a@firma.pl", "b@inna.pl", "druga@firma.pl"])

    def test_held_addresses_are_never_sent_even_with_all_addresses(self):
        self.database.apply_edit("eventy-001", {"status": st.STATUS_REPLIED})
        self.run_cli("--all-addresses")
        self.assertEqual([m["To"] for m in FakeClient.sent], ["b@inna.pl"])


class FakeClient:
    sent: list = []

    def __init__(self, host, port, user, password, use_ssl):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, message):
        FakeClient.sent.append(message)


if __name__ == "__main__":
    unittest.main()
