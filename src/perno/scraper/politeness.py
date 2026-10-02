"""Uprzejmość: odstępy i backoff na host, limit na IP, bramka globalna, „bot wall", Retry-After.

Zegar i `sleep` są wstrzykiwane (testy używają zegara fałszywego). Wszystkie czasy to
`monotonic` — nigdy zegar ścienny (zmiana czasu nie może zaburzyć odstępów).
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping, Optional, Protocol

from perno.scraper.settings import ScanSettings

MAX_PENALTY = 6
MAX_DELAY = 120.0
RTT_CAP = 30.0
RTT_ALPHA = 0.3
RETRY_AFTER_LIMIT = 600.0
NO_HEADER_BACKOFF = (30.0, 60.0, 120.0)
START_RETRY_WAIT = 60.0
SERVER_ERROR_WAITS = (30.0, 90.0)
NETWORK_ERROR_WAITS = (15.0, 45.0)
OK_STREAK_FOR_RELIEF = 10
RECENT_WINDOW = 50

BOT_WALL_MARKERS = (
    "just a moment", "challenge-platform", "attention required", "incapsula incident",
    "sucuri website firewall", "pardon our interruption",
)
_CDN_HEADER_NAMES = ("cf-ray", "x-amz-cf-id", "x-vercel-id", "x-azure-ref", "x-wix-request-id", "x-sucuri-id")
_CDN_SERVER_VALUES = ("cloudflare", "shopify")


class Clock(Protocol):
    """Zegar wstrzykiwany do bramek i crawlera (prawdziwy albo sterowany w testach)."""

    def now(self) -> float: ...

    def sleep(self, seconds: float, stop_event: Optional[threading.Event] = None) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float, stop_event: Optional[threading.Event] = None) -> None:
        if seconds <= 0:
            return
        if stop_event is not None:
            stop_event.wait(seconds)
        else:
            time.sleep(seconds)


# ---- nagłówki / treść ----------------------------------------------------

def parse_retry_after(value: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    """Sekundy z `Retry-After` (liczba albo data HTTP); śmieci → None, ujemne → 0."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if seconds == seconds and seconds != float("inf") else None
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


def looks_like_cdn(headers: Mapping) -> bool:
    lowered = {str(k).lower(): str(v).lower() for k, v in headers.items()}
    if any(name in lowered for name in _CDN_HEADER_NAMES):
        return True
    if any(name.startswith("x-akamai") for name in lowered):
        return True
    if any(lowered.get("server", "").startswith(v) for v in _CDN_SERVER_VALUES):
        return True
    return "x-served-by" in lowered and "x-cache" in lowered


def detect_bot_wall(status: int, headers: Mapping, body, visible_text_len: Optional[int] = None) -> bool:
    """Blokada dla botów (np. wyzwanie Cloudflare). Samo `g-recaptcha` NIE jest markerem."""
    lowered = {str(k).lower(): str(v).lower() for k, v in headers.items()}
    text = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else (body or "")
    head = text[:20_000].lower()
    has_marker = any(marker in head for marker in BOT_WALL_MARKERS)
    if status in (403, 429, 503):
        return lowered.get("cf-mitigated") == "challenge" or has_marker
    if status == 200:
        small = len(text) < 20_000
        little_text = visible_text_len is not None and visible_text_len < 400
        return small and little_text and has_marker
    return False


# ---- host --------------------------------------------------------------

@dataclass
class Verdict:
    action: str          # "ok" | "retry" | "stop"
    wait: float = 0.0    # ile odczekać przed powtórzeniem (action == "retry")
    reason: str = ""     # kod z statuses.PRZYCZYNY przy action == "stop"
    retry_hint: float = 0.0  # Retry-After w sekundach (do nastepna_proba_po)


class HostGate:
    def __init__(self, settings: ScanSettings, clock=None, rng: Optional[random.Random] = None,
                 robots_delay: Optional[float] = None):
        self.settings = settings
        self.clock = clock or RealClock()
        self.rng = rng or random.Random()
        self.robots_delay = robots_delay or 0.0
        self.ready_at = 0.0
        self.penalty = 0
        self.rtt_ewma: Optional[float] = None
        self.ok_streak = 0
        self.consec_429 = 0
        self.recent_429 = deque(maxlen=RECENT_WINDOW)
        self.consec_403 = 0
        self.consec_botwall = 0
        self.consec_5xx = 0
        self.consec_net = 0
        self.start_retried = False

    # ---- odstęp ----------------------------------------------------------
    def base_delay(self) -> float:
        rtt_part = min(3 * self.rtt_ewma, RTT_CAP) if self.rtt_ewma is not None else 0.0
        return max(self.settings.floor, self.settings.delay, self.robots_delay, rtt_part)

    def next_delay(self) -> float:
        base = self.base_delay()
        delay = min(base * (2 ** self.penalty), MAX_DELAY)
        delay = max(delay, self.robots_delay, self.settings.floor)
        return delay + self.rng.uniform(0, 0.5 * base)  # jitter tylko dodaje; losowany raz na żądanie

    def set_robots_delay(self, delay: Optional[float], after_request: bool = False) -> None:
        """Ustawia `Crawl-delay` z robots.txt. `after_request`: robots.txt właśnie pobraliśmy, więc następne żądanie
        do tego hosta też musi poczekać cały `delay` (odstęp zaplanowany przy odpowiedzi nie znał jeszcze tej wartości)."""
        self.robots_delay = delay or 0.0
        if after_request and self.robots_delay > 0:
            self.ready_at = max(self.ready_at, self.clock.now() + self.robots_delay)

    def wait_turn(self, stop_event: Optional[threading.Event] = None, deadline: Optional[float] = None) -> bool:
        """Czeka na swoją kolej. False, gdy stop albo czekanie wyszłoby poza `deadline` (zegar monotoniczny)."""
        wait = self.ready_at - self.clock.now()
        if wait > 0:
            if deadline is not None and self.clock.now() + wait > deadline:
                return False
            self.clock.sleep(wait, stop_event)
        return not (stop_event is not None and stop_event.is_set())

    def _schedule(self, wait: float) -> None:
        self.ready_at = self.clock.now() + wait

    # ---- reakcje ---------------------------------------------------------
    def on_response(self, *, status: Optional[int] = None, elapsed: float = 0.0,
                    retry_after: Optional[str] = None, is_start: bool = False,
                    bot_wall: bool = False, error: Optional[str] = None,
                    retries_used: int = 0) -> Verdict:
        """`is_start` dotyczy strony startowej i robots.txt. `error`: dns|timeout|siec|tls."""
        if error:
            return self._on_error(error, is_start, retries_used)
        if bot_wall:
            return self._on_blocked(is_start, botwall=True)
        if status in (429, 503):
            return self._on_throttled(retry_after)
        if status in (401, 403, 406, 451):
            return self._on_blocked(is_start, botwall=False)
        if status in (500, 502, 504):
            return self._on_server_error(retries_used)
        return self._on_success(elapsed)

    def _on_success(self, elapsed: float) -> Verdict:
        self.rtt_ewma = elapsed if self.rtt_ewma is None else RTT_ALPHA * elapsed + (1 - RTT_ALPHA) * self.rtt_ewma
        self.ok_streak += 1
        if self.ok_streak >= OK_STREAK_FOR_RELIEF and self.penalty > 0:
            self.penalty -= 1
            self.ok_streak = 0
        self.consec_429 = self.consec_403 = self.consec_botwall = self.consec_5xx = self.consec_net = 0
        self.start_retried = False
        self.recent_429.append(False)
        self._schedule(self.next_delay())
        return Verdict("ok")

    def _on_throttled(self, retry_after: Optional[str]) -> Verdict:
        self.consec_429 += 1
        self.recent_429.append(True)
        self.ok_streak = 0
        self.penalty = min(self.penalty + 1, MAX_PENALTY)
        seconds = parse_retry_after(retry_after)
        if seconds is not None and seconds > RETRY_AFTER_LIMIT:
            return Verdict("stop", reason="retry_after", retry_hint=seconds)
        if self.consec_429 >= 3 or sum(self.recent_429) >= 5:
            return Verdict("stop", reason="http_429", retry_hint=seconds or 0.0)
        if seconds is not None:
            wait = seconds * (1 + self.rng.uniform(0, 0.1))
        else:
            wait = min(NO_HEADER_BACKOFF[min(self.consec_429 - 1, len(NO_HEADER_BACKOFF) - 1)], 300.0)
        self._schedule(max(wait, self.next_delay()))
        return Verdict("retry", wait=wait)

    def _on_blocked(self, is_start: bool, botwall: bool) -> Verdict:
        self.ok_streak = 0
        self.penalty = min(self.penalty + 1, MAX_PENALTY)
        reason = "bot_wall" if botwall else "http_403"
        if is_start:
            if not self.start_retried:
                self.start_retried = True
                self._schedule(START_RETRY_WAIT)
                return Verdict("retry", wait=START_RETRY_WAIT)
            return Verdict("stop", reason=reason)
        if botwall:
            self.consec_botwall += 1
            if self.consec_botwall >= 2:
                return Verdict("stop", reason="bot_wall")
        else:
            self.consec_403 += 1
            if self.consec_403 >= 3:
                return Verdict("stop", reason="http_403")
        self._schedule(self.next_delay())
        return Verdict("ok")

    def _on_server_error(self, retries_used: int) -> Verdict:
        self.ok_streak = 0
        if retries_used < len(SERVER_ERROR_WAITS):
            wait = SERVER_ERROR_WAITS[retries_used]
            self._schedule(wait)
            return Verdict("retry", wait=wait)
        self.consec_5xx += 1
        if self.consec_5xx >= 5:
            return Verdict("stop", reason="http_5xx")
        self._schedule(self.next_delay())
        return Verdict("ok")

    def _on_error(self, error: str, is_start: bool, retries_used: int) -> Verdict:
        self.ok_streak = 0
        if error == "dns" and is_start:
            return Verdict("stop", reason="dns")
        reason = error if error in ("timeout", "tls") else "siec"
        if retries_used < len(NETWORK_ERROR_WAITS):
            wait = NETWORK_ERROR_WAITS[retries_used]
            self._schedule(wait)
            return Verdict("retry", wait=wait)
        self.consec_net += 1
        if self.consec_net >= 4:
            return Verdict("stop", reason=reason)
        self._schedule(self.next_delay())
        return Verdict("ok")


# ---- IP i bramka globalna --------------------------------------------------

class IpLimiter:
    """Ogranicza liczbę żądań w toku do jednego adresu IP (hosting współdzielony)."""

    def __init__(self, cap: int = 2, cdn_cap: int = 8, min_spacing: float = 0.5, clock=None):
        self.default_cap = cap
        self.cdn_cap = max(cap, cdn_cap)
        self.min_spacing = min_spacing
        self.clock = clock or RealClock()
        self._cond = threading.Condition()
        self._inflight: dict = {}
        self._caps: dict = {}
        self._last_start: dict = {}

    def cap_for(self, ip: str) -> int:
        return self._caps.get(ip, self.default_cap)

    def lift(self, ip: str) -> None:
        """Nagłówki CDN: ten adres obsługuje wiele witryn — podnieś limit (do cdn_cap, nie do nieskończoności)."""
        with self._cond:
            self._caps[ip] = self.cdn_cap
            self._cond.notify_all()

    def acquire(self, ip: str, stop_event: Optional[threading.Event] = None,
                timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else self.clock.now() + timeout
        with self._cond:
            while self._inflight.get(ip, 0) >= self.cap_for(ip):
                if stop_event is not None and stop_event.is_set():
                    return False
                if deadline is not None and self.clock.now() >= deadline:
                    return False
                self._cond.wait(0.5)
            self._inflight[ip] = self._inflight.get(ip, 0) + 1
            start_at = max(self.clock.now(), self._last_start.get(ip, 0.0) + self.min_spacing)
            self._last_start[ip] = start_at
        pause = start_at - self.clock.now()
        if pause > 0:
            self.clock.sleep(pause, stop_event)
        if stop_event is not None and stop_event.is_set():
            self.release(ip)
            return False
        return True

    def release(self, ip: str) -> None:
        with self._cond:
            self._inflight[ip] = max(0, self._inflight.get(ip, 0) - 1)
            self._cond.notify_all()

    def inflight(self, ip: str) -> int:
        with self._cond:
            return self._inflight.get(ip, 0)


class GlobalGate:
    """Pas bezpieczeństwa: najwyżej `max_rps` startów żądań na sekundę w całym procesie."""

    def __init__(self, max_rps: float = 15.0, clock=None):
        self.interval = 1.0 / max_rps
        self.clock = clock or RealClock()
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, stop_event: Optional[threading.Event] = None) -> bool:
        with self._lock:
            start_at = max(self.clock.now(), self._next)
            self._next = start_at + self.interval
        pause = start_at - self.clock.now()
        if pause > 0:
            self.clock.sleep(pause, stop_event)
        return not (stop_event is not None and stop_event.is_set())
