"""Współdzielone usługi jednego przebiegu skanowania: magazyn, ustawienia, zegar, fetcher, bramki uprzejmości,
sygnał stopu i statystyki. Przekazywane do planisty i do komponentów skanowania firmy."""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from perno.scraper.fetcher import Fetcher
from perno.scraper.politeness import Clock, GlobalGate, IpLimiter, RealClock
from perno.scraper.settings import ScanSettings
from perno.scraper.store import Store


class Stats:
    def __init__(self, clock: Clock) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self.started = clock.now()
        self.counters = {"zapytania": 0, "strony": 0, "bajty": 0, "kontakty": 0, "firmy_start": 0}
        self.results: dict = {}
        self.active: set = set()

    def add(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                self.counters[key] = self.counters.get(key, 0) + value

    def firm_started(self, domena: str) -> None:
        with self._lock:
            self.counters["firmy_start"] += 1
            self.active.add(domena)

    def firm_done(self, domena: str, status: str) -> None:
        with self._lock:
            self.active.discard(domena)
            self.results[status] = self.results.get(status, 0) + 1

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = max(0.001, self._clock.now() - self.started)
            return {**self.counters, "firmy_wynik": dict(self.results), "aktywne": len(self.active),
                    "aktywne_domeny": sorted(self.active)[:50], "uplynelo_s": round(elapsed, 1),
                    "strony_na_min": round(self.counters["strony"] * 60.0 / elapsed, 1)}


@dataclass
class RunContext:
    store: Store
    settings: ScanSettings
    run_id: int
    fetcher: Fetcher
    clock: Clock
    rng: random.Random
    ip_limiter: IpLimiter
    global_gate: GlobalGate
    stop: threading.Event
    stats: Stats
    log: Callable[[str], None]
    verbose: bool = False
    parse_slots: threading.BoundedSemaphore = field(default_factory=lambda: threading.BoundedSemaphore(4))

    @classmethod
    def create(cls, store: Store, settings: ScanSettings, run_id: int, *, fetcher: Optional[Fetcher] = None,
               clock: Optional[Clock] = None, rng: Optional[random.Random] = None, log: Optional[Callable] = None,
               stop_event: Optional[threading.Event] = None, ip_limiter: Optional[IpLimiter] = None,
               global_gate: Optional[GlobalGate] = None, verbose: bool = False) -> "RunContext":
        """Uzupełnia domyślne usługi (prawdziwy zegar, fetcher, limity IP i globalny)."""
        clock = clock or RealClock()
        return cls(
            store=store, settings=settings, run_id=run_id, fetcher=fetcher or Fetcher(settings), clock=clock,
            rng=rng or random.Random(), ip_limiter=ip_limiter or IpLimiter(cap=settings.ip_cap, clock=clock),
            global_gate=global_gate or GlobalGate(settings.max_rps, clock=clock), stop=stop_event or threading.Event(),
            stats=Stats(clock), log=log or (lambda message: None), verbose=verbose)
