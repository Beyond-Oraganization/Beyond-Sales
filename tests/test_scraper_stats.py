"""Statystyki przebiegu skrapera: liczniki, czasy etapów i migawka dla panelu."""

from __future__ import annotations

import threading
import time
import unittest

from perno.scraper.run_context import Stats
from tests.fixtures import FakeTime


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeTime()
        self.stats = Stats(self.clock)

    def test_counters_and_firm_lifecycle(self):
        self.stats.add(zapytania=3, strony=2, bajty=100)
        self.stats.add(zapytania=1)
        self.stats.firm_started("a.pl")
        self.stats.firm_started("b.pl")
        self.stats.firm_done("a.pl", "zakonczona")
        snap = self.stats.snapshot()
        self.assertEqual((snap["zapytania"], snap["strony"], snap["bajty"], snap["firmy_start"]), (4, 2, 100, 2))
        self.assertEqual((snap["aktywne"], snap["aktywne_domeny"], snap["firmy_wynik"]), (1, ["b.pl"], {"zakonczona": 1}))

    def test_pages_per_minute_follow_the_injected_clock(self):
        self.stats.add(strony=30)
        self.clock.sleep(60)
        snap = self.stats.snapshot()
        self.assertEqual((snap["uplynelo_s"], snap["strony_na_min"]), (60.0, 30.0))

    def test_timed_blocks_accumulate_real_time_per_stage(self):
        with self.stats.timed("pobieranie"):
            time.sleep(0.02)
        with self.stats.timed("pobieranie"):
            time.sleep(0.02)
        with self.stats.timed("zapis"):
            pass
        stages = self.stats.snapshot()["czasy_s"]
        self.assertGreaterEqual(stages["pobieranie"], 0.04)
        self.assertLess(stages["pobieranie"], 1.0)
        self.assertEqual(sorted(stages), ["pobieranie", "zapis"])

    def test_a_block_that_raises_is_still_timed(self):
        with self.assertRaises(ValueError), self.stats.timed("analiza"):
            raise ValueError("błąd w bloku")
        self.assertIn("analiza", self.stats.snapshot()["czasy_s"])

    def test_concurrent_additions_are_not_lost(self):
        def work():
            for _ in range(500):
                self.stats.add_time("zapis", 0.001)
                self.stats.add(strony=1)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        snap = self.stats.snapshot()
        self.assertEqual(snap["strony"], 4000)
        self.assertAlmostEqual(snap["czasy_s"]["zapis"], 4.0, places=1)

    def test_snapshot_without_timings_has_an_empty_breakdown(self):
        self.assertEqual(self.stats.snapshot()["czasy_s"], {})


if __name__ == "__main__":
    unittest.main()
