"""Crawler: fasada składająca przebieg skanowania z planisty, skanera firm i usług przebiegu.

Równoległość istnieje wyłącznie MIĘDZY firmami (jedna firma = jeden wątek, więc najwyżej jedno żądanie naraz do
hosta). Każde żądanie przechodzi przez: odstęp hosta (`HostGate`) → limit na IP (`IpLimiter`) → bramkę globalną
(`GlobalGate`) → fetcher. Odmowa serwera (429/403/bot-wall) = wycofanie i zatrzymanie firmy, nigdy obejście.

Moduły: `run_context` (współdzielone usługi), `scheduler` (wątki, puls, stop), `company_crawl` (jedna firma),
`page_fetch` (żądanie z polityką), `page_pipeline` (obróbka strony), `company` (stan firmy).
"""

from __future__ import annotations

import random
import threading
from typing import Callable, Optional

from perno.scraper.company_crawl import CompanyCrawler
from perno.scraper.fetcher import Fetcher
from perno.scraper.politeness import GlobalGate, IpLimiter
from perno.scraper.run_context import RunContext
from perno.scraper.scheduler import Scheduler
from perno.scraper.settings import HEARTBEAT_EVERY, ScanSettings
from perno.scraper.store import Store


class Crawler:
    def __init__(self, store: Store, settings: ScanSettings, run_id: int, *, fetcher: Optional[Fetcher] = None,
                 clock=None, rng: Optional[random.Random] = None, log: Optional[Callable] = None,
                 only_ids: Optional[list] = None, stop_event: Optional[threading.Event] = None,
                 stagger: float = 0.25, heartbeat_every: float = HEARTBEAT_EVERY,
                 ip_limiter: Optional[IpLimiter] = None, global_gate: Optional[GlobalGate] = None,
                 verbose: bool = False):
        self.rc = RunContext.create(
            store, settings, run_id, fetcher=fetcher, clock=clock, rng=rng, log=log, stop_event=stop_event,
            ip_limiter=ip_limiter, global_gate=global_gate, verbose=verbose)
        self.company_crawler = CompanyCrawler(self.rc)
        self.scheduler = Scheduler(self.rc, self.company_crawler.crawl_company, only_ids=only_ids, stagger=stagger,
                                   heartbeat_every=heartbeat_every)

    def run(self) -> dict:
        """Skanuje firmy do końca kolejki albo do stopu; zwraca {status, reason, stats}."""
        return self.scheduler.run()
