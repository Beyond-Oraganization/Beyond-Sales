"""Stan jednej dzierżawionej firmy podczas skanowania (żyje tylko w wątku, który ją skanuje)."""

from __future__ import annotations

import heapq
from typing import Optional
from urllib.parse import urlsplit

from perno.scraper import robots
from perno.scraper.politeness import HostGate
from perno.scraper.settings import ScanSettings
from perno.scraper.urls import TrapGuard, host_key, site_key


class Company:
    """Stan jednej dzierżawionej firmy; żyje tylko w wątku, który ją skanuje."""

    def __init__(self, firma: dict, settings: ScanSettings, clock, rng, deadline: float):
        self.firma = firma
        self.id = firma["id"]
        self.domena = firma["domena"]
        self.host = host_key(self.domena.split(":")[0]) or self.domena
        self.website = firma["website"]
        self.origin = firma.get("origin") or self.website.split("/", 3)[0] + "//" + urlsplit(self.website).netloc
        self.gate = HostGate(settings, clock=clock, rng=rng)
        self.deadline = deadline
        self.rules: Optional[robots.RobotsRules] = None
        self.trap = TrapGuard()
        self.seen: set = set()
        self.content: dict = {}
        self.heap: list = []
        self.counter = 0
        self.pages = int(firma.get("stron_pobranych") or 0)
        self.bytes = int(firma.get("bajty_pobranych") or 0)
        start_path = urlsplit(self.website).path or "/"
        self.start_prefix = start_path if start_path != "/" else "/"
        self.international = not self.host.endswith(".pl")
        self.pobrano = 0

    def require_rules(self) -> robots.RobotsRules:
        """Reguły robots.txt; wołać dopiero po `_load_robots` (wcześniej ich nie ma)."""
        if self.rules is None:
            raise RuntimeError("reguły robots.txt nie zostały jeszcze wczytane")
        return self.rules

    def same_site(self, url: str) -> bool:
        return site_key(url) == self.domena

    def push(self, item: dict) -> None:
        self.counter += 1
        heapq.heappush(self.heap, (item["priorytet"], self.counter, item))

    def pop(self) -> Optional[dict]:
        return heapq.heappop(self.heap)[2] if self.heap else None
