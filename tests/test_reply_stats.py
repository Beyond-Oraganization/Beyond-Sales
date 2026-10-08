"""Statystyki odpowiedzi: odsetki, wiersze z SQL, czas do odpowiedzi, pokrycie odbioru oraz zapis do Markdown i CSV (czysta logika)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from perno.domain import reply_stats as rs

T0 = datetime(2026, 9, 29, 13, 0)


def row(key="razem", **kw):
    return rs.StatsRow(key, **kw)


class RatioTests(unittest.TestCase):
    def test_a_ratio_is_none_without_a_denominator_and_a_real_zero_is_zero(self):
        self.assertIsNone(rs.ratio(5, 0))
        self.assertIsNone(rs.ratio(0, -1))
        self.assertEqual(rs.ratio(0, 10), 0.0)
        self.assertEqual(rs.ratio(1, 4), 0.25)

    def test_percent_formatting_uses_a_comma_and_a_dash_for_no_data(self):
        self.assertEqual(rs.percent(0.125), "12,5%")
        self.assertEqual(rs.percent(0.0), "0,0%")
        self.assertEqual(rs.percent(1.0), "100,0%")
        self.assertEqual(rs.percent(None), "—")


class StatsRowTests(unittest.TestCase):
    def test_delivered_is_sent_without_hard_bounces_and_never_negative(self):
        self.assertEqual(row(sent=100, bounced=8).delivered, 92)
        self.assertEqual(row(sent=3, bounced=9).delivered, 0)

    def test_the_reply_rate_is_replies_over_delivered(self):
        self.assertAlmostEqual(row(sent=100, bounced=8, replied=9).reply_rate, 9 / 92)
        self.assertIsNone(row(sent=5, bounced=5, replied=0).reply_rate)

    def test_the_positive_share_counts_only_rated_replies(self):
        self.assertEqual(row(positive=2, neutral=2, negative=4, unrated=50).positive_rate, 0.25)
        self.assertIsNone(row(unrated=5).positive_rate)

    def test_a_small_sample_is_flagged_below_twenty_delivered(self):
        self.assertTrue(row(sent=19).small_sample)
        self.assertFalse(row(sent=20).small_sample)
        self.assertTrue(row(sent=25, bounced=6).small_sample)

    def test_json_carries_the_derived_numbers(self):
        data = row("PL", sent=40, bounced=4, replied=9, positive=3, neutral=3, negative=3).to_json()
        self.assertEqual((data["delivered"], data["reply_rate"], data["positive_rate"], data["small_sample"]), (36, 0.25, 1 / 3, False))

    def test_an_empty_row_is_all_zeros(self):
        empty = rs.StatsRow("x")
        self.assertEqual((empty.sent, empty.bounced, empty.replied, empty.positive, empty.neutral, empty.negative, empty.unrated, empty.optout,
                          empty.awaiting, empty.delivered), (0,) * 10)
        self.assertIsNone(empty.reply_rate)

    def test_rows_are_immutable(self):
        with self.assertRaises(AttributeError):
            row().sent = 1  # type: ignore[misc]


class RowFromSqlTests(unittest.TestCase):
    def test_nulls_decimals_and_missing_keys_become_integers_and_a_placeholder_key(self):
        parsed = rs.row_from_sql(None, {"sent": Decimal("12"), "bounced": None, "replied": 3.0, "positive": "2"})
        self.assertEqual((parsed.key, parsed.sent, parsed.bounced, parsed.replied, parsed.positive, parsed.neutral), ("brak", 12, 0, 3, 2, 0))
        self.assertEqual(rs.row_from_sql("", {}).key, "brak")
        self.assertEqual(rs.row_from_sql("PL", {}).key, "PL")
        self.assertEqual(rs.row_from_sql(0, {}).key, "0")


class LatencyTests(unittest.TestCase):
    def pairs(self, *hours):
        return [(T0, T0 + timedelta(hours=h)) for h in hours]

    def test_median_and_buckets(self):
        stats = rs.latency_stats(self.pairs(2, 20, 30, 70, 100, 400))
        self.assertEqual(stats["n"], 6)
        self.assertEqual(stats["median_hours"], 50.0)
        self.assertEqual(stats["buckets"], {"do 1 dnia": 2, "2–3 dni": 2, "4–7 dni": 1, "powyżej 7 dni": 1})

    def test_bucket_edges_belong_to_the_lower_bucket(self):
        stats = rs.latency_stats(self.pairs(24, 72, 168, 168.01))
        self.assertEqual(list(stats["buckets"].values()), [1, 1, 1, 1])
        self.assertEqual(stats["buckets"]["2–3 dni"], 1)

    def test_just_over_three_days_is_the_next_bucket(self):
        self.assertEqual(rs.latency_stats(self.pairs(72.01))["buckets"]["4–7 dni"], 1)
        self.assertEqual(rs.latency_stats(self.pairs(24.01))["buckets"]["2–3 dni"], 1)

    def test_a_reply_dated_before_the_send_counts_as_zero_not_negative(self):
        stats = rs.latency_stats([(T0, T0 - timedelta(hours=1))])
        self.assertEqual((stats["median_hours"], stats["buckets"]["do 1 dnia"]), (0.0, 1))

    def test_missing_or_non_date_values_are_skipped_and_nothing_gives_none(self):
        stats = rs.latency_stats([(None, T0), (T0, None), ("x", "y"), (T0, T0 + timedelta(hours=5))])
        self.assertEqual((stats["n"], stats["median_hours"]), (1, 5.0))
        self.assertEqual(rs.latency_stats([]), {"n": 0, "median_hours": None, "buckets": {label: 0 for label, _ in rs.LATENCY_BUCKETS}})

    def test_the_median_is_rounded_to_one_decimal(self):
        self.assertEqual(rs.latency_stats(self.pairs(1.04, 1.06))["median_hours"], 1.1)


class CoverageTests(unittest.TestCase):
    def test_every_used_mailbox_receiving_without_errors_is_complete(self):
        cov = rs.coverage(["a@x.pl", "b@x.pl"], ["a@x.pl", "B@x.pl"], [], {"a@x.pl": 10, "b@x.pl": 5})
        self.assertEqual((cov["complete"], cov["receiving"], cov["blind_mails"], cov["blind_mailboxes"]), (True, 2, 0, []))

    def test_mailboxes_without_imap_or_with_an_error_are_blind_and_counted(self):
        cov = rs.coverage(["a@x.pl", "b@x.pl", "c@x.pl"], ["a@x.pl", "b@x.pl"], ["B@x.pl"], {"a@x.pl": 10, "b@x.pl": 5, "c@x.pl": 7})
        self.assertEqual((cov["complete"], cov["receiving"], cov["blind_mails"], cov["blind_mailboxes"]), (False, 1, 12, ["b@x.pl", "c@x.pl"]))

    def test_a_blind_mailbox_that_sent_nothing_does_not_matter(self):
        cov = rs.coverage(["a@x.pl", "b@x.pl"], ["a@x.pl"], [], {"a@x.pl": 4, "b@x.pl": 0})
        self.assertTrue(cov["complete"])

    def test_nothing_configured_is_a_complete_empty_picture(self):
        cov = rs.coverage([], [], [], {})
        self.assertEqual((cov["complete"], cov["configured"], cov["receiving"], cov["blind_mails"]), (True, 0, 0, 0))

    def test_the_adjusted_rate_ignores_mails_from_blind_mailboxes(self):
        total = row(sent=100, bounced=10, replied=9)
        self.assertAlmostEqual(rs.adjust_for_coverage(total, 40), 9 / 50)
        self.assertAlmostEqual(rs.adjust_for_coverage(total, 40, 4), 9 / 54)
        self.assertIsNone(rs.adjust_for_coverage(row(sent=5, replied=0), 5))


REPORT = {
    "period": "2026-09-29 – dziś", "replied_companies": 3, "auto_threads": 1, "soft_bounces": 2, "warnings": ["Uwaga testowa."],
    "total": row(sent=40, bounced=4, replied=9, positive=3, neutral=3, negative=2, unrated=1, optout=1, awaiting=2).to_json(),
    "latency": {"n": 9, "median_hours": 30.5, "buckets": {}},
    "groups": {"segment": [row("eventy", sent=30, bounced=3, replied=8, positive=3, neutral=3, negative=2).to_json(), row("=cmd()", sent=10, bounced=1, replied=1).to_json()],
               "country": [], "mailbox": [], "day": []},
}


class WritingTests(unittest.TestCase):
    def test_markdown_has_the_summary_warnings_and_tables_with_small_sample_marks(self):
        text = rs.to_markdown(REPORT)
        for needle in ("# Raport odpowiedzi (2026-09-29 – dziś)", "Wysłano maili: 40, dostarczono: 36 (twarde zwroty: 4)", "Odpowiedzi człowieka: 9 (25,0% dostarczonych), firm: 3",
                       "Oceny: pozytywne 3, neutralne 3, negatywne 2, nieocenione 1", "Prośby o wypisanie: 1, czeka na naszą odpowiedź: 2", "Autoodpowiedzi: 1, zwroty czasowe: 2",
                       "mediana 30.5 h (n=9)", "## Uwagi", "- Uwaga testowa.", "## Segment", "| eventy | 30 | 27 | 8 | 29,6% | 3 | 3 | 2 |", "| =cmd() (mała próba) |"):
            self.assertIn(needle, text)
        self.assertNotIn("## Kraj", text)  # puste zestawienie nie ma sekcji
        self.assertTrue(text.endswith("\n"))

    def test_markdown_without_latency_or_warnings_omits_those_lines(self):
        quiet = {**REPORT, "warnings": [], "latency": {"n": 0, "median_hours": None, "buckets": {}}}
        text = rs.to_markdown(quiet)
        self.assertNotIn("Czas do pierwszej odpowiedzi", text)
        self.assertNotIn("## Uwagi", text)

    def test_csv_rows_start_with_the_header_then_the_total_then_every_group(self):
        rows = rs.to_csv_rows(REPORT)
        self.assertEqual(rows[0], list(rs.CSV_HEADER))
        self.assertEqual(rows[1][:7], ["razem", "razem", 40, 4, 36, 9, "25,0%"])
        self.assertEqual([r[:2] for r in rows[2:]], [["Segment", "eventy"], ["Segment", "=cmd()"]])
        self.assertEqual(rows[3][-1], "tak")
        self.assertEqual(rows[1][-1], "nie")


if __name__ == "__main__":
    unittest.main()
