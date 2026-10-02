"""Planista przebiegu: pula wątków (jedna firma = jeden wątek), puls (heartbeat), stop i utrata bazy.

Równoległość istnieje wyłącznie MIĘDZY firmami; co robi wątek z jedną firmą, wie `CompanyCrawler`.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from sqlalchemy import exc as sa_exc

from perno.db import DB_ERRORS
from perno.scraper import statuses as st
from perno.scraper.run_context import RunContext
from perno.scraper.settings import HEARTBEAT_EVERY

DB_GRACE_SECONDS = 60.0


class Scheduler:
    def __init__(self, rc: RunContext, crawl_company: Callable[[dict], None], *, only_ids: Optional[list] = None,
                 stagger: float = 0.25, heartbeat_every: float = HEARTBEAT_EVERY):
        self.rc = rc
        self.crawl_company = crawl_company
        self.only_ids = only_ids
        self.stagger = stagger
        self.heartbeat_every = heartbeat_every
        self.stop_reason = ""
        self.db_failed = False
        self._failure_lock = threading.Lock()
        self.run_deadline = rc.clock.now() + rc.settings.run_max_hours * 3600.0

    def run(self) -> dict:
        workers = []
        count = max(1, self.rc.settings.concurrency)
        for index in range(count):
            thread = threading.Thread(target=self._worker, name=f"crawl-{index}", daemon=True)
            thread.start()
            workers.append(thread)
            if self.stagger and index < count - 1 and self.rc.stop.wait(self.stagger):
                break
        self._coordinate(workers)
        status = st.RUN_PRZERWANY if self.db_failed else (st.RUN_ZATRZYMANY if self.rc.stop.is_set() else st.RUN_ZAKONCZONY)
        return {"status": status, "reason": self.stop_reason, "stats": self.rc.stats.snapshot()}

    def _coordinate(self, workers: list) -> None:
        last_beat = 0.0
        failing_since: Optional[float] = None
        last_summary = time.monotonic()
        while any(worker.is_alive() for worker in workers):
            time.sleep(0.05)
            now = time.monotonic()
            if self.rc.clock.now() >= self.run_deadline and not self.rc.stop.is_set():
                self.stop_reason = "limit_przebiegu"
                self.rc.log("Osiągnięto maksymalny czas przebiegu — zatrzymuję.")
                self.rc.stop.set()
            if now - last_beat < self.heartbeat_every:
                continue
            last_beat = now
            try:
                requested = self.rc.store.heartbeat(self.rc.run_id, self.rc.stats.snapshot())
                failing_since = None
                if requested and not self.rc.stop.is_set():
                    self.stop_reason = self.stop_reason or "stop_uzytkownika"
                    self.rc.log("Otrzymano prośbę o zatrzymanie.")
                    self.rc.stop.set()
            except (DB_ERRORS + (sa_exc.DBAPIError,)) as exc:
                failing_since = failing_since or now
                if now - failing_since > DB_GRACE_SECONDS:
                    self._db_failure(exc)
            if now - last_summary >= 30:
                last_summary = now
                snap = self.rc.stats.snapshot()
                self.rc.log(f"[podsumowanie] strony={snap['strony']} zapytania={snap['zapytania']} aktywne={snap['aktywne']} "
                         f"strony/min={snap['strony_na_min']}")
        try:
            self.rc.store.heartbeat(self.rc.run_id, self.rc.stats.snapshot())
        except (DB_ERRORS + (sa_exc.DBAPIError,)):
            pass

    def _db_failure(self, exc: Exception) -> None:
        with self._failure_lock:
            if self.db_failed:
                return
            self.db_failed = True
            self.stop_reason = "baza"
            self.rc.log(f"Utracono połączenie z bazą ({type(exc).__name__}) — kończę bez zapisu; firmy wrócą do kolejki po odzyskaniu.")
            self.rc.stop.set()

    def _worker(self) -> None:
        while not self.rc.stop.is_set():
            try:
                with self.rc.stats.timed("baza_inne"):
                    firma = self.rc.store.claim_next_firma(self.rc.run_id, self.only_ids)
            except (DB_ERRORS + (sa_exc.DBAPIError,)) as exc:
                self._db_failure(exc)
                return
            if firma is None:
                return
            try:
                self.crawl_company(firma)
            except (DB_ERRORS + (sa_exc.DBAPIError,)) as exc:
                self._db_failure(exc)
                return
