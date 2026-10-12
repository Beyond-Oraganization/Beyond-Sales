"""Przedziały odstępu między mailami: walidacja, losowanie według prawdopodobieństw, średnia i zapis tekstowy (CLI)."""

from __future__ import annotations

import dataclasses
import random
import unittest
from collections import Counter

from perno.domain import delay_bands as db
from perno.domain.delay_bands import BandsInvalid, DelayBand

EXAMPLE = [{"min": 60, "max": 120, "percent": 50}, {"min": 300, "max": 600, "percent": 40}, {"min": 1200, "max": 3600, "percent": 10}]


def bands(*specs) -> tuple:
    return tuple(DelayBand(low, high, percent) for low, high, percent in specs)


class ValidationTests(unittest.TestCase):
    def test_example_from_the_request_is_accepted_and_roundtrips(self):
        parsed = db.from_json(EXAMPLE)
        self.assertEqual(parsed, bands((60, 120, 50), (300, 600, 40), (1200, 3600, 10)))
        self.assertEqual(db.from_json(db.to_json(parsed)), parsed)

    def test_accepts_numeric_strings_as_sent_by_a_form(self):
        parsed = db.from_json([{"min": "60", "max": "90.5", "percent": "100"}])
        self.assertEqual(parsed, bands((60.0, 90.5, 100.0)))

    def test_any_number_of_bands_up_to_the_technical_cap(self):
        share = 100 / db.MAX_BANDS
        many = [{"min": 10, "max": 20, "percent": share}] * db.MAX_BANDS
        self.assertEqual(len(db.from_json(many)), db.MAX_BANDS)
        with self.assertRaisesRegex(BandsInvalid, "Za dużo"):
            db.from_json(many + [{"min": 10, "max": 20, "percent": 1}])

    def test_percentages_must_total_100(self):
        for percents in ((50, 40), (50, 40, 20), (99,), (60, 60)):
            raw = [{"min": 10, "max": 20, "percent": percent} for percent in percents]
            with self.subTest(percents=percents), self.assertRaisesRegex(BandsInvalid, "razem 100%"):
                db.from_json(raw)

    def test_total_message_says_what_it_is_now(self):
        raw = [{"min": 10, "max": 20, "percent": 30}, {"min": 10, "max": 20, "percent": 45.5}]
        with self.assertRaisesRegex(BandsInvalid, r"teraz: 75\.5%"):
            db.from_json(raw)

    def test_floating_point_drift_in_the_total_is_tolerated(self):
        thirds = [{"min": 10, "max": 20, "percent": percent} for percent in (33.3, 33.3, 33.4)]
        self.assertEqual(len(db.from_json(thirds)), 3)

    def test_rejects_bad_shapes(self):
        one = {"min": 10, "max": 20, "percent": 100}
        bad = [None, [], "1m-2m=100", {"min": 10}, [one, "x"], [{"min": 10, "max": 20}], [{"max": 20, "percent": 100}], 5]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(BandsInvalid):
                db.from_json(raw)

    def test_band_limits_follow_the_global_delay_range(self):
        def one(**over):
            return [{"min": 10, "max": 20, "percent": 100, **over}]
        ok = [one(min=db.MIN_DELAY), one(max=db.MAX_DELAY), one(min=60, max=60), one(min=db.MIN_DELAY, max=db.MAX_DELAY)]
        for raw in ok:
            with self.subTest(raw=raw):
                db.from_json(raw)
        for raw in (one(min=9.9), one(max=3600.5), one(min=30, max=20), one(min=True), one(max=None), one(min="abc"),
                    one(min=[10]), one(max=float("nan")), one(max=float("inf"))):
            with self.subTest(raw=raw), self.assertRaises(BandsInvalid):
                db.from_json(raw)

    def test_percent_must_be_positive_and_at_most_100(self):
        for percent in (0, -5, 100.01, True, None, "abc", float("nan")):
            with self.subTest(percent=percent), self.assertRaises(BandsInvalid):
                db.from_json([{"min": 10, "max": 20, "percent": percent}])

    def test_a_zero_percent_band_is_refused_even_when_the_others_total_100(self):
        raw = [{"min": 10, "max": 20, "percent": 100}, {"min": 30, "max": 40, "percent": 0}]
        with self.assertRaisesRegex(BandsInvalid, "Przedział 2.*większe od 0"):
            db.from_json(raw)

    def test_bands_are_immutable_values(self):
        band = DelayBand(10, 20, 100)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            band.low = 5  # type: ignore[misc]
        self.assertEqual(hash(db.single(10, 20)), hash(db.single(10, 20)))

    def test_error_names_the_offending_band(self):
        raw = [{"min": 60, "max": 120, "percent": 50}, {"min": 600, "max": 300, "percent": 50}]
        with self.assertRaisesRegex(BandsInvalid, "Przedział 2"):
            db.from_json(raw)

    def test_bands_may_overlap(self):
        overlapping = [{"min": 60, "max": 300, "percent": 50}, {"min": 120, "max": 600, "percent": 50}]
        self.assertEqual(len(db.from_json(overlapping)), 2)


class PickTests(unittest.TestCase):
    EXAMPLE = bands((60, 120, 50), (300, 600, 40), (1200, 3600, 10))

    @staticmethod
    def low(low, high):
        return low

    def test_roll_selects_the_band_by_cumulative_probability(self):
        cases = {0.0: 60, 0.499: 60, 0.5: 300, 0.899: 300, 0.9: 1200, 0.999999: 1200}
        for roll, expected in cases.items():
            with self.subTest(roll=roll):
                self.assertEqual(db.pick(self.EXAMPLE, roll, self.low), expected)

    def test_time_comes_from_the_chosen_band_through_the_uniform(self):
        seen = []
        result = db.pick(self.EXAMPLE, 0.6, lambda low, high: seen.append((low, high)) or (low + high) / 2)
        self.assertEqual((seen, result), ([(300, 600)], 450))

    def test_roll_of_exactly_one_falls_back_to_the_last_band(self):
        self.assertEqual(db.pick(self.EXAMPLE, 1.0, self.low), 1200)
        self.assertEqual(db.pick(self.EXAMPLE, 1.0, lambda low, high: (low + high) / 2), 2400)

    def test_single_band_ignores_the_roll(self):
        only = db.single(45, 120)
        self.assertEqual({db.pick(only, roll, self.low) for roll in (0.0, 0.5, 0.99)}, {45})

    def test_probabilities_are_normalised_when_they_drift_from_100(self):
        drifted = bands((10, 20, 33.3), (30, 40, 33.3), (50, 60, 33.3))
        self.assertEqual(db.pick(drifted, 0.9999, self.low), 50)

    def test_frequencies_match_the_configured_probabilities(self):
        rng = random.Random(20261012)
        counts = Counter()
        for _ in range(20000):
            seconds = db.pick(self.EXAMPLE, rng.random(), rng.uniform)
            counts["short" if seconds <= 120 else "medium" if seconds <= 600 else "long"] += 1
        for name, share in (("short", 0.5), ("medium", 0.4), ("long", 0.1)):
            with self.subTest(band=name):
                self.assertAlmostEqual(counts[name] / 20000, share, delta=0.02)

    def test_times_stay_inside_their_bands(self):
        rng = random.Random(7)
        for _ in range(2000):
            seconds = db.pick(self.EXAMPLE, rng.random(), rng.uniform)
            self.assertTrue(any(band.low <= seconds <= band.high for band in self.EXAMPLE), seconds)


class SummaryTests(unittest.TestCase):
    def test_overall_range_spans_all_bands(self):
        self.assertEqual(db.overall_range(bands((300, 600, 40), (60, 120, 50), (1200, 3600, 10))), (60, 3600))

    def test_mean_weights_band_midpoints_by_probability(self):
        mean = db.mean_seconds(bands((60, 120, 50), (300, 600, 40), (1200, 3600, 10)))
        self.assertAlmostEqual(mean, 0.5 * 90 + 0.4 * 450 + 0.1 * 2400)
        self.assertEqual(db.mean_seconds(db.single(45, 120)), 82.5)

    def test_format_duration(self):
        cases = {45: "45 s", 10.5: "10.5 s", 60: "1 min", 90: "1 min 30 s", 120: "2 min", 3600: "60 min"}
        for seconds, text in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(db.format_duration(seconds), text)

    def test_format_band(self):
        self.assertEqual(db.format_band(DelayBand(60, 120, 50)), "1 min – 2 min: 50%")


class TextTests(unittest.TestCase):
    def test_the_example_from_the_request(self):
        parsed = db.parse_text("1m-2m=50,5m-10m=40,20m-60m=10")
        self.assertEqual(parsed, bands((60, 120, 50), (300, 600, 40), (1200, 3600, 10)))

    def test_units_spaces_and_percent_sign(self):
        self.assertEqual(db.parse_text(" 90 - 2min = 25% , 30s-1.5m=75 "), bands((90, 120, 25), (30, 90, 75)))
        self.assertEqual(db.parse_text("45-120=100"), db.single(45, 120))

    def test_rejects_malformed_text(self):
        for text in ("", "   ", ",", "1m-2m", "1m=100", "abc-2m=100", "1h-2h=100", "1m-2m=x", "1m-2m=50", "1m-2m=60,3m-4m=60", "-2m=100"):
            with self.subTest(text=text), self.assertRaises(BandsInvalid):
                db.parse_text(text)

    def test_missing_separators_get_the_format_hint(self):
        for text in ("1m=100", "1m-2m", "abc"):
            with self.subTest(text=text), self.assertRaisesRegex(BandsInvalid, "od-do=procent"):
                db.parse_text(text)

    def test_error_for_bad_time_names_the_band(self):
        with self.assertRaisesRegex(BandsInvalid, "Przedział 2.*1h"):
            db.parse_text("1m-2m=50,1h-2h=50")


if __name__ == "__main__":
    unittest.main()
