"""Raport odpowiedzi na sqlite: zestawienia z SQL, okres i zawężenia, czas reakcji, pokrycie odbioru oraz eksport CSV i Markdown."""

from __future__ import annotations

import csv
import io
import unittest
from datetime import datetime, timedelta

import sqlalchemy as sa

from perno.db.replies_store import ReplyStore
from perno.db.schema import maile
from perno.domain import replies as rp
from perno.domain import reply_listing as rl
from perno.services import replies_report as rep
from tests.fixtures import FakeClock, make_db, put, sample_row

CONFIG = {"SMTP_HOST": "s", "SMTP_PORT": "465", "SMTP_USER": "roman@z3x.pl", "SMTP_PASSWORD": "p", "IMAP_HOST": "i",
          "SMTP2_HOST": "s", "SMTP2_PORT": "465", "SMTP2_USER": "drugi@z3x.pl", "SMTP2_PASSWORD": "p2"}
BOTH = {**CONFIG, "IMAP2_HOST": "i2"}
D = datetime(2026, 9, 29, 13, 0)


def inbound(thread, *, kind="human", when, text="Odpowiedź", bounce="", mailbox="roman@z3x.pl", optout=False):
    return rp.NewMessage(direction="in", kind=kind, mailbox=mailbox, identity=f"{thread}-{kind}-{when.isoformat()}", message_id=f"{thread}-{kind}-{when.isoformat()}",
                         thread_id=thread, company_id=thread, date_at=when, subject="Re: x", from_addr="x@y.pl", body_text=text, snippet=text, bounce_type=bounce,
                         optout_hint=optout, thread_mailbox=mailbox, match_method="address")


class ReportCase(unittest.TestCase):
    def setUp(self):
        self.db = make_db(FakeClock(datetime(2026, 10, 8, 10, 0)))
        self.store = ReplyStore(self.db)
        self.store.ensure_schema()
        sent = "2026-09-29T13:00:00"
        put(self.db,
            sample_row(1, company="A", country="PL", status="odpowiedzial", sent_at=sent),
            sample_row(2, company="B", country="DE", status="odpowiedzial", sent_at="2026-09-30T09:00:00"),
            sample_row(3, company="C", country="PL", status="odpowiedzial", sent_at=sent),
            sample_row(4, company="D", country="PL", status="zwrot", sent_at=sent),
            sample_row(5, company="E", country="PL", status="wyslano", sent_at=sent),
            sample_row(6, company="F", country="PL", status="wyslano", sent_at=sent),
            sample_row(7, company="G", country="PL", status="wyslano", sent_at="2026-09-01T10:00:00"),
            sample_row(8, company="H", country="PL", status="do_wyslania"),
            sample_row(1, segment="driverzy", company="I", country="PL", status="odpowiedzial", sent_at=sent))
        with self.db.engine.begin() as conn:
            conn.execute(sa.update(maile).values(sent_from="roman@z3x.pl"))
            conn.execute(sa.update(maile).where(maile.c.id == "eventy-002").values(sent_from="drugi@z3x.pl"))
            conn.execute(sa.update(maile).where(maile.c.id == "eventy-007").values(sent_from=None))
        s = self.store
        s.save_message(inbound("eventy-001", when=D + timedelta(hours=5)), D)
        s.save_message(inbound("eventy-002", when=D + timedelta(days=1, hours=2), mailbox="drugi@z3x.pl"), D)
        s.save_message(inbound("eventy-003", when=D + timedelta(days=4), optout=True), D)
        s.save_message(inbound("driverzy-001", when=D + timedelta(days=10)), D)
        s.save_message(inbound("eventy-004", kind="bounce", when=D + timedelta(minutes=5), bounce="hard"), D)
        s.save_message(inbound("eventy-005", kind="auto", when=D + timedelta(hours=1)), D)
        s.save_message(inbound("eventy-006", kind="bounce", when=D + timedelta(hours=1), bounce="soft"), D)
        s.set_rating("eventy-001", "positive", "human", "", D)
        s.set_rating("eventy-002", "neutral", "human", "", D)
        s.set_rating("eventy-003", "negative", "ai", "", D)
        with self.db.engine.begin() as conn:
            from perno.db.replies_schema import odp_watki
            conn.execute(sa.update(odp_watki).where(odp_watki.c.id == "eventy-002").values(awaiting_reply=False))

    def report(self, config=CONFIG, **payload):
        return rep.build_report(self.store, config, payload, datetime(2026, 10, 8, 10, 0))

    def group(self, report, name):
        return {row["key"]: row for row in report["groups"][name]}


class TotalsTests(ReportCase):
    def test_the_headline_numbers(self):
        total = self.report()["total"]
        self.assertEqual((total["sent"], total["bounced"], total["delivered"], total["replied"]), (8, 1, 7, 4))
        self.assertEqual((total["positive"], total["neutral"], total["negative"], total["unrated"], total["optout"], total["awaiting"]), (1, 1, 1, 1, 1, 3))
        self.assertAlmostEqual(total["reply_rate"], 4 / 7)
        self.assertAlmostEqual(total["positive_rate"], 1 / 3)
        self.assertTrue(total["small_sample"])

    def test_extra_counts_companies_auto_replies_soft_bounces_and_the_oldest_waiting_reply(self):
        report = self.report()
        self.assertEqual((report["replied_companies"], report["auto_threads"], report["soft_bounces"]), (4, 1, 1))
        self.assertEqual(report["awaiting_since"], (D + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S"))

    def test_an_empty_database_gives_a_zero_report_without_errors(self):
        empty = ReplyStore(make_db())
        empty.ensure_schema()
        report = rep.build_report(empty, CONFIG, {}, datetime(2026, 10, 8))
        self.assertEqual((report["total"]["sent"], report["total"]["reply_rate"], report["latency"]["n"], report["awaiting_since"]), (0, None, 0, None))
        self.assertEqual(report["period"], "od początku – dziś")

    def test_unsent_mails_and_non_human_threads_are_not_replies(self):
        report = self.report()
        self.assertEqual(report["total"]["sent"], 8)  # „do wysłania” nie wchodzi
        self.assertEqual(self.group(report, "segment")["driverzy"]["replied"], 1)


class GroupTests(ReportCase):
    def test_by_segment_country_and_mailbox(self):
        report = self.report()
        seg = self.group(report, "segment")
        self.assertEqual((seg["eventy"]["sent"], seg["eventy"]["replied"], seg["driverzy"]["sent"], seg["driverzy"]["replied"]), (7, 3, 1, 1))
        country = self.group(report, "country")
        self.assertEqual((country["PL"]["sent"], country["DE"]["sent"], country["DE"]["neutral"]), (7, 1, 1))
        box = self.group(report, "mailbox")
        self.assertEqual((box["roman@z3x.pl"]["sent"], box["drugi@z3x.pl"]["sent"]), (7, 1))  # mail bez skrzynki to skrzynka główna

    def test_by_day_in_chronological_order(self):
        days = [(row["key"], row["sent"]) for row in self.report()["groups"]["day"]]
        self.assertEqual(days, [("2026-09-01", 1), ("2026-09-29", 6), ("2026-09-30", 1)])

    def test_the_biggest_groups_come_first(self):
        self.assertEqual([row["key"] for row in self.report()["groups"]["segment"]], ["eventy", "driverzy"])


class FilterTests(ReportCase):
    def test_the_period_is_the_send_day_inclusive(self):
        report = self.report(**{"from": "2026-09-29", "to": "2026-09-29"})
        self.assertEqual(report["total"]["sent"], 6)
        self.assertEqual(report["period"], "2026-09-29 – 2026-09-29")
        self.assertEqual(self.report(**{"from": "2026-09-30"})["total"]["sent"], 1)
        self.assertEqual(self.report(**{"to": "2026-09-01"})["total"]["sent"], 1)
        self.assertEqual(self.report(**{"from": "2026-10-01"})["total"]["sent"], 0)

    def test_segment_country_and_mailbox_narrow_everything_including_groups(self):
        self.assertEqual(self.report(segments=["driverzy"])["total"]["sent"], 1)
        self.assertEqual(self.report(countries=["DE"])["total"]["replied"], 1)
        only = self.report(mailboxes=["Drugi@Z3X.pl"])
        self.assertEqual((only["total"]["sent"], list(self.group(only, "mailbox"))), (1, ["drugi@z3x.pl"]))
        self.assertEqual(self.report(mailboxes=["roman@z3x.pl"])["total"]["sent"], 7)

    def test_bad_filters_are_request_errors(self):
        for payload in ({"from": "x"}, {"from": "2026-10-02", "to": "2026-10-01"}, {"segments": "eventy"}):
            with self.subTest(payload=payload), self.assertRaises(rl.ReplyRequestError):
                self.report(**payload)


class LatencyAndCoverageTests(ReportCase):
    def test_time_to_the_first_human_reply(self):
        latency = self.report()["latency"]
        self.assertEqual(latency["n"], 4)
        self.assertEqual(latency["median_hours"], 51.0)  # 5 h, 6 h, 96 h, 240 h
        self.assertEqual(latency["buckets"], {"do 1 dnia": 2, "2–3 dni": 0, "4–7 dni": 1, "powyżej 7 dni": 1})

    def test_a_mailbox_without_imap_makes_the_report_say_the_rate_is_understated(self):
        report = self.report(CONFIG)
        cov = report["coverage"]
        self.assertEqual((cov["complete"], cov["blind_mailboxes"], cov["blind_mails"]), (False, ["drugi@z3x.pl"], 1))
        self.assertTrue(any("zaniżony" in text and "drugi@z3x.pl" in text for text in report["warnings"]))

    def test_with_imap_on_both_mailboxes_there_is_no_such_warning_and_an_error_brings_it_back(self):
        self.assertTrue(self.report(BOTH)["coverage"]["complete"])
        self.assertFalse(any("zaniżony" in text for text in self.report(BOTH)["warnings"]))
        self.store.record_error("drugi@z3x.pl", "Authentication failed", "auth", datetime(2026, 10, 8))
        self.assertEqual(self.report(BOTH)["coverage"]["blind_mailboxes"], ["drugi@z3x.pl"])

    def test_a_small_sample_and_auto_replies_are_mentioned(self):
        warnings = self.report(BOTH)["warnings"]
        self.assertTrue(any("Mała próba" in text for text in warnings))
        self.assertTrue(any("Autoodpowiedzi nie są liczone" in text for text in warnings))


class FileTests(ReportCase):
    def test_markdown_has_numbers_and_no_addresses_or_message_text(self):
        text = rep.markdown(self.report(BOTH))
        self.assertIn("Wysłano maili: 8, dostarczono: 7 (twarde zwroty: 1)", text)
        self.assertIn("## Segment", text)
        self.assertIn("roman@z3x.pl", text)  # nasze skrzynki są w zestawieniu, cudzych adresów nie ma
        for private in ("x@y.pl", "Odpowiedź", "Re: x"):
            self.assertNotIn(private, text)

    def test_csv_is_semicolon_separated_with_a_bom_and_a_header(self):
        text = rep.csv_text(self.report(BOTH))
        self.assertTrue(text.startswith("﻿"))
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
        self.assertEqual(rows[0][:4], ["zestawienie", "grupa", "wysłano", "twarde zwroty"])
        self.assertEqual(rows[1][:6], ["razem", "razem", "8", "1", "7", "4"])
        self.assertIn(["Segment", "eventy", "7", "1", "6", "3"], [r[:6] for r in rows])

    def test_csv_cells_that_could_be_formulas_are_neutralised(self):
        with self.db.engine.begin() as conn:
            from perno.db.schema import companies
            conn.execute(sa.update(companies).where(companies.c.id == "driverzy-001").values(segment="=HYPERLINK(\"http://x\")"))
        text = rep.csv_text(self.report(BOTH))
        self.assertIn("'=HYPERLINK", text)
        self.assertNotIn(";=HYPERLINK", text)


if __name__ == "__main__":
    unittest.main()
