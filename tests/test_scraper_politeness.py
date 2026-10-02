from __future__ import annotations

import random
import threading
import time
import unittest
from datetime import datetime, timezone

from perno.scraper import politeness as pol
from perno.scraper.politeness import GlobalGate, HostGate, IpLimiter
from perno.scraper.settings import ScanSettings
from tests.fixtures import FakeTime, ZeroRng


def gate(clock=None, rng=None, robots_delay=None, **overrides):
    return HostGate(ScanSettings.for_tests(**{"delay": 1.0, **overrides}), clock or FakeTime(),
                    rng or ZeroRng(), robots_delay)


class RetryAfterTests(unittest.TestCase):
    NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_numbers_dates_and_garbage(self):
        table = {"120": 120.0, "0": 0.0, "-5": 0.0, " 7 ": 7.0, "1.5": 1.5, "abc": None, "": None, None: None,
                 "nan": None, "inf": None, "Thu, 01 Oct 2026 12:02:00 GMT": 120.0,
                 "Thu, 01 Oct 2026 11:00:00 GMT": 0.0, "Thu, 99 Foo 2026": None}
        for raw, expected in table.items():
            self.assertEqual(pol.parse_retry_after(raw, self.NOW), expected, repr(raw))


class CdnAndBotWallTests(unittest.TestCase):
    def test_cdn_detection(self):
        for headers in ({"CF-Ray": "abc"}, {"Server": "cloudflare"}, {"server": "Shopify"},
                        {"X-Akamai-Transformed": "9"}, {"X-Served-By": "cache-x", "X-Cache": "HIT"},
                        {"x-vercel-id": "1"}, {"X-Amz-Cf-Id": "1"}, {"x-wix-request-id": "1"}):
            self.assertTrue(pol.looks_like_cdn(headers), headers)
        for headers in ({}, {"Server": "nginx"}, {"X-Served-By": "only-this"}, {"Server": "Apache"}):
            self.assertFalse(pol.looks_like_cdn(headers), headers)

    def test_bot_wall_positive_cases(self):
        self.assertTrue(pol.detect_bot_wall(403, {"cf-mitigated": "challenge"}, ""))
        self.assertTrue(pol.detect_bot_wall(403, {}, "<title>Just a moment...</title>"))
        self.assertTrue(pol.detect_bot_wall(503, {}, b"<html>Attention Required! | Cloudflare</html>"))
        self.assertTrue(pol.detect_bot_wall(429, {}, "Pardon Our Interruption"))
        self.assertTrue(pol.detect_bot_wall(200, {}, "<div id='challenge-platform'></div>", visible_text_len=30))

    def test_bot_wall_negative_cases(self):
        self.assertFalse(pol.detect_bot_wall(403, {}, "<h1>Forbidden</h1>"))
        self.assertFalse(pol.detect_bot_wall(404, {}, "Just a moment"))
        recaptcha_form = "<form><div class='g-recaptcha'></div></form>" + "tekst " * 300
        self.assertFalse(pol.detect_bot_wall(200, {}, recaptcha_form, visible_text_len=1800))
        self.assertFalse(pol.detect_bot_wall(200, {}, "<div class='g-recaptcha'></div>", visible_text_len=20))
        self.assertFalse(pol.detect_bot_wall(200, {}, "just a moment " + "x" * 30_000, visible_text_len=100))
        self.assertFalse(pol.detect_bot_wall(200, {}, "just a moment", visible_text_len=900))


class DelayMathTests(unittest.TestCase):
    def test_floor_holds_over_many_random_samples(self):
        settings = ScanSettings.from_params({"delay": 0.1})
        host = HostGate(settings, FakeTime(), random.Random(1))
        samples = [host.next_delay() for _ in range(1000)]
        self.assertGreaterEqual(min(samples), 1.0)
        self.assertLessEqual(max(samples), 1.5)

    def test_default_delay_and_jitter_range(self):
        host = HostGate(ScanSettings.from_params(), FakeTime(), random.Random(2))
        samples = [host.next_delay() for _ in range(500)]
        self.assertGreaterEqual(min(samples), 2.5)
        self.assertLessEqual(max(samples), 3.75)
        self.assertGreater(max(samples) - min(samples), 0.5)

    def test_robots_crawl_delay_including_fractional_and_large(self):
        self.assertEqual(gate(robots_delay=2.5, delay=1.0).next_delay(), 2.5)
        self.assertEqual(gate(robots_delay=10, delay=1.0).next_delay(), 10)
        big = gate(robots_delay=200, delay=1.0)
        big.penalty = 3
        self.assertGreaterEqual(big.next_delay(), 200)  # nigdy poniżej Crawl-delay, mimo limitu 120 s

    def test_crawl_delay_learned_after_a_request_applies_to_the_very_next_request(self):
        clock = FakeTime()
        host = gate(clock=clock, rng=ZeroRng(), delay=0.0)
        host.on_response(status=200, elapsed=0.01, is_start=True)  # np. pobranie robots.txt
        self.assertLess(host.ready_at - clock.now(), 0.1)
        host.set_robots_delay(2.5, after_request=True)
        self.assertGreaterEqual(host.ready_at - clock.now(), 2.5)
        host.wait_turn()
        self.assertEqual(clock.sleeps[-1], 2.5)

    def test_cached_crawl_delay_does_not_delay_the_first_request_of_a_run(self):
        clock = FakeTime()
        host = gate(clock=clock, rng=ZeroRng(), delay=0.0)
        host.set_robots_delay(30, after_request=False)
        self.assertEqual(host.ready_at, 0.0)
        host.wait_turn()
        self.assertEqual(clock.sleeps, [])

    def test_slow_servers_get_three_times_their_response_time(self):
        host = gate(delay=1.0)
        for _ in range(20):
            host.on_response(status=200, elapsed=2.0)
        self.assertAlmostEqual(host.base_delay(), 6.0, places=1)
        slow = gate(delay=1.0)
        slow.on_response(status=200, elapsed=100.0)
        self.assertEqual(slow.base_delay(), 30.0)

    def test_penalty_doubles_is_capped_and_relaxes(self):
        host = gate(delay=2.0)
        self.assertEqual(host.next_delay(), 2.0)
        host.penalty = 1
        self.assertEqual(host.next_delay(), 4.0)
        host.penalty = 5
        self.assertEqual(host.next_delay(), 64.0)
        host.penalty = 6
        self.assertEqual(host.next_delay(), 120.0)
        host.penalty = 2
        for _ in range(pol.OK_STREAK_FOR_RELIEF):
            host.on_response(status=200, elapsed=0.1)
        self.assertEqual(host.penalty, 1)

    def test_delay_is_counted_from_the_end_of_the_previous_response(self):
        clock = FakeTime()
        host = gate(clock, delay=3.0)
        self.assertTrue(host.wait_turn())  # pierwsze żądanie bez czekania
        self.assertEqual(clock.sleeps, [])
        clock.t += 0.7  # czas trwania żądania
        host.on_response(status=200, elapsed=0.7)
        self.assertAlmostEqual(host.ready_at, clock.t + 3.0)
        host.wait_turn()
        self.assertAlmostEqual(clock.sleeps[-1], 3.0)

    def test_wait_turn_respects_deadline_and_stop(self):
        clock = FakeTime()
        host = gate(clock, delay=5.0)
        host.on_response(status=200, elapsed=0.1)
        self.assertFalse(host.wait_turn(deadline=clock.now() + 2.0))
        self.assertEqual(clock.sleeps, [])
        stop = threading.Event()
        stop.set()
        self.assertFalse(host.wait_turn(stop_event=stop))


class StateMachineTests(unittest.TestCase):
    def test_success_statuses_are_ok(self):
        host = gate()
        for status in (200, 301, 404, 410, 204):
            self.assertEqual(host.on_response(status=status, elapsed=0.1).action, "ok", status)

    def test_429_with_retry_after_retries_then_stops_on_third(self):
        host = gate()
        first = host.on_response(status=429, retry_after="5")
        second = host.on_response(status=429, retry_after="5")
        third = host.on_response(status=429, retry_after="5")
        self.assertEqual((first.action, second.action, third.action), ("retry", "retry", "stop"))
        self.assertTrue(5.0 <= first.wait <= 5.5)
        self.assertEqual(third.reason, "http_429")

    def test_retry_after_longer_than_ten_minutes_stops_with_hint(self):
        verdict = gate().on_response(status=429, retry_after="601")
        self.assertEqual((verdict.action, verdict.reason, verdict.retry_hint), ("stop", "retry_after", 601.0))
        self.assertEqual(gate().on_response(status=503, retry_after="600").action, "retry")

    def test_429_without_header_backs_off_30_60_then_stops(self):
        host = gate()
        waits = [host.on_response(status=429), host.on_response(status=429)]
        self.assertEqual([v.wait for v in waits], [30.0, 60.0])
        self.assertEqual(host.on_response(status=429).action, "stop")

    def test_503_behaves_like_429_not_like_5xx(self):
        host = gate()
        self.assertEqual(host.on_response(status=503).action, "retry")
        self.assertEqual(host.consec_5xx, 0)

    def test_success_resets_the_consecutive_429_counter(self):
        host = gate()
        host.on_response(status=429)
        host.on_response(status=429)
        host.on_response(status=200, elapsed=0.1)
        self.assertEqual(host.on_response(status=429).action, "retry")

    def test_five_throttles_among_fifty_requests_stop_even_if_not_consecutive(self):
        host = gate()
        verdicts = []
        for _ in range(5):
            verdicts.append(host.on_response(status=429, retry_after="1"))
            host.on_response(status=200, elapsed=0.1)
        self.assertEqual([v.action for v in verdicts], ["retry"] * 4 + ["stop"])

    def test_403_class_on_the_start_page_retries_once_then_stops(self):
        for status in (401, 403, 406, 451):
            host = gate()
            first = host.on_response(status=status, is_start=True)
            second = host.on_response(status=status, is_start=True)
            self.assertEqual((first.action, first.wait, second.action, second.reason), ("retry", 60.0, "stop", "http_403"), status)

    def test_403_on_other_pages_stops_after_three_in_a_row(self):
        host = gate()
        actions = [host.on_response(status=403).action for _ in range(3)]
        self.assertEqual(actions, ["ok", "ok", "stop"])
        recover = gate()
        recover.on_response(status=403)
        recover.on_response(status=403)
        recover.on_response(status=200, elapsed=0.1)
        self.assertEqual(recover.on_response(status=403).action, "ok")

    def test_bot_wall_start_retries_once_other_pages_stop_on_second(self):
        start = gate()
        self.assertEqual(start.on_response(status=403, is_start=True, bot_wall=True).action, "retry")
        self.assertEqual(start.on_response(status=403, is_start=True, bot_wall=True).reason, "bot_wall")
        later = gate()
        self.assertEqual(later.on_response(status=200, bot_wall=True).action, "ok")
        self.assertEqual(later.on_response(status=200, bot_wall=True).reason, "bot_wall")

    def test_server_errors_retry_twice_then_count_toward_a_stop(self):
        host = gate()
        self.assertEqual(host.on_response(status=500, retries_used=0).wait, 30.0)
        self.assertEqual(host.on_response(status=502, retries_used=1).wait, 90.0)
        for _ in range(4):
            self.assertEqual(host.on_response(status=504, retries_used=2).action, "ok")
        self.assertEqual(host.on_response(status=500, retries_used=2).reason, "http_5xx")
        recovered = gate()
        for _ in range(4):
            recovered.on_response(status=500, retries_used=2)
        recovered.on_response(status=200, elapsed=0.1)
        self.assertEqual(recovered.on_response(status=500, retries_used=2).action, "ok")

    def test_network_errors_retry_then_stop_after_four_final_failures(self):
        host = gate()
        self.assertEqual(host.on_response(error="timeout", retries_used=0).wait, 15.0)
        self.assertEqual(host.on_response(error="timeout", retries_used=1).wait, 45.0)
        for _ in range(3):
            self.assertEqual(host.on_response(error="timeout", retries_used=2).action, "ok")
        self.assertEqual(host.on_response(error="timeout", retries_used=2).reason, "timeout")
        tls = gate()
        for _ in range(3):
            tls.on_response(error="tls", retries_used=2)
        self.assertEqual(tls.on_response(error="tls", retries_used=2).reason, "tls")
        other = gate()
        for _ in range(3):
            other.on_response(error="boom", retries_used=2)
        self.assertEqual(other.on_response(error="boom", retries_used=2).reason, "siec")

    def test_dns_failure_stops_immediately_only_on_start(self):
        self.assertEqual(gate().on_response(error="dns", is_start=True).reason, "dns")
        self.assertEqual(gate().on_response(error="dns", is_start=False, retries_used=0).action, "retry")

    def test_backoff_pushes_ready_at_forward(self):
        clock = FakeTime()
        host = gate(clock, delay=1.0)
        host.on_response(status=429)
        self.assertGreaterEqual(host.ready_at - clock.now(), 30.0)


class IpLimiterTests(unittest.TestCase):
    def test_cap_blocks_until_release(self):
        limiter = IpLimiter(cap=2, min_spacing=0)
        self.assertTrue(limiter.acquire("1.1.1.1"))
        self.assertTrue(limiter.acquire("1.1.1.1"))
        self.assertTrue(limiter.acquire("2.2.2.2"))
        got = threading.Event()
        worker = threading.Thread(target=lambda: (limiter.acquire("1.1.1.1"), got.set()))
        worker.start()
        self.assertFalse(got.wait(0.2))
        limiter.release("1.1.1.1")
        self.assertTrue(got.wait(2))
        worker.join(2)
        self.assertEqual(limiter.inflight("1.1.1.1"), 2)

    def test_lift_raises_the_cap_and_wakes_waiters(self):
        limiter = IpLimiter(cap=1, cdn_cap=8, min_spacing=0)
        limiter.acquire("1.1.1.1")
        got = threading.Event()
        worker = threading.Thread(target=lambda: (limiter.acquire("1.1.1.1"), got.set()))
        worker.start()
        self.assertFalse(got.wait(0.2))
        limiter.lift("1.1.1.1")
        self.assertTrue(got.wait(2))
        worker.join(2)
        self.assertEqual(limiter.cap_for("1.1.1.1"), 8)
        self.assertEqual(limiter.cap_for("9.9.9.9"), 1)

    def test_stop_event_and_timeout_abort_waiting(self):
        limiter = IpLimiter(cap=1, min_spacing=0)
        limiter.acquire("1.1.1.1")
        stop = threading.Event()
        stop.set()
        self.assertFalse(limiter.acquire("1.1.1.1", stop_event=stop))
        started = time.monotonic()
        self.assertFalse(limiter.acquire("1.1.1.1", timeout=0.2))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(limiter.inflight("1.1.1.1"), 1)

    def test_minimum_spacing_between_request_starts(self):
        clock = FakeTime()
        limiter = IpLimiter(cap=5, min_spacing=0.5, clock=clock)
        starts = []
        for _ in range(4):
            limiter.acquire("1.1.1.1")
            starts.append(clock.now())
            limiter.release("1.1.1.1")
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertTrue(all(gap >= 0.5 - 1e-9 for gap in gaps), gaps)

    def test_release_never_goes_negative(self):
        limiter = IpLimiter(cap=1, min_spacing=0)
        limiter.release("1.1.1.1")
        self.assertEqual(limiter.inflight("1.1.1.1"), 0)


class GlobalGateTests(unittest.TestCase):
    def test_spacing_between_starts(self):
        clock = FakeTime()
        gate_ = GlobalGate(max_rps=10, clock=clock)
        start = clock.now()
        for _ in range(5):
            self.assertTrue(gate_.wait())
        self.assertAlmostEqual(clock.now() - start, 0.4, places=6)

    def test_stop_event(self):
        stop = threading.Event()
        stop.set()
        self.assertFalse(GlobalGate(max_rps=10, clock=FakeTime()).wait(stop))


if __name__ == "__main__":
    unittest.main()
