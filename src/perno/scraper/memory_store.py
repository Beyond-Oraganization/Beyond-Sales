"""Magazyn w pamięci dla skanu „bez bazy": ten sam crawler (robots.txt, uprzejmość, limity, priorytety stron), ale wyniki
nie trafiają do żadnej bazy, tylko do wywołania zwrotnego `on_site` po zakończeniu każdej witryny.

Implementuje wyłącznie metody `Store`, których używają `Scheduler` i `CompanyCrawler` (kolejka firm, robots, frontier,
zapis strony, zwolnienie dzierżawy). Nie importuje sqlalchemy ani nie otwiera połączeń: niczego nie da się tu zapisać
do bazy z założenia. Pamięć witryny jest zwalniana, gdy tylko trafi do `on_site`."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from perno.scraper import statuses as st
from perno.scraper.store.records import PageRecord


@dataclass
class PageEntry:
    id: int
    url: str
    depth: int
    priority: int
    source: str
    status: str = st.STRONA_OCZEKUJE
    http_status: Optional[int] = None
    title: str = ""
    description: str = ""
    bytes: int = 0
    error: str = ""
    text: str = ""


@dataclass
class ContactEntry:
    kind: str
    value: str
    in_domain: bool
    role: str
    source_url: str
    pages: int = 1


@dataclass
class SiteResult:
    """Wynik jednej witryny: kończy się przy zwolnieniu dzierżawy, więc zawiera już ostateczny status."""
    domain: str
    start_url: str
    status: str
    reason: str = ""
    message: str = ""
    target: str = ""
    robots: str = ""
    pages: list = field(default_factory=list)  # PageEntry odwiedzone (bez oczekujących)
    pending: int = 0  # adresy znalezione, ale nieodwiedzone (limit stron / czasu)
    contacts: list = field(default_factory=list)  # ContactEntry

    @property
    def fetched(self) -> list:
        return [page for page in self.pages if page.status == st.STRONA_POBRANO]


class _LocalClock:
    @staticmethod
    def now() -> datetime:
        return datetime.now()


class _Site:
    def __init__(self, firm: dict):
        self.firm = firm
        self.pages: dict = {}  # url_hash -> PageEntry, w kolejności odkrycia
        self.contacts: dict = {}  # (typ, wartość) -> ContactEntry
        self.robots = ""


class MemoryStore:
    """Podmienia `Store` w skanie jednorazowym. `firms`: słowniki jak wiersze tabeli firm (id, domena, website…)."""

    def __init__(self, firms: list, on_site: Callable[[SiteResult], None]):
        self.db = _LocalClock()
        self._queue = deque(firms)
        self._sites: dict = {}
        self._on_site = on_site
        self._lock = threading.Lock()
        self._page_seq = 0
        self.progress: dict = {}
        self.sink_errors: list = []

    # ---- kolejka firm (Scheduler) -------------------------------------------------
    def claim_next_firma(self, run_id: int, only_ids: Optional[list] = None) -> Optional[dict]:
        with self._lock:
            if not self._queue:
                return None
            firm = self._queue.popleft()
            self._sites[firm["id"]] = _Site(firm)
            return firm

    def heartbeat(self, run_id: int, stats: dict) -> bool:
        self.progress = stats
        return False  # zatrzymanie przychodzi przez stop_event (Ctrl-C), nie przez bazę

    def release_firma(self, firma_id: int, run_id: int, status: str, przyczyna: Optional[str] = None,
                      blad: Optional[str] = None, domena_docelowa: Optional[str] = None,
                      nastepna_proba_po: Optional[datetime] = None) -> bool:
        with self._lock:
            site = self._sites.pop(firma_id, None)
        if site is None:
            return False
        pages = [page for page in site.pages.values() if page.status != st.STRONA_OCZEKUJE]
        result = SiteResult(
            domain=site.firm["domena"], start_url=site.firm["website"], status=status, reason=przyczyna or "",
            message=blad or "", target=domena_docelowa or "", robots=site.robots, pages=pages,
            pending=len(site.pages) - len(pages), contacts=list(site.contacts.values()))
        try:
            self._on_site(result)
        except OSError as exc:  # dysk pełny / brak uprawnień: nie zabijamy wątku, raport pokaże błąd
            self.sink_errors.append(f"{result.domain}: {exc}")
        return True

    # ---- robots.txt -----------------------------------------------------------------
    def save_robots(self, firma_id: int, run_id: int, status: str, text: Optional[str], crawl_delay: Optional[float]) -> None:
        self._sites[firma_id].robots = status

    # ---- frontier i strony -------------------------------------------------------------
    def known_urls(self, firma_id: int) -> list:
        return [(page.url, digest) for digest, page in self._sites[firma_id].pages.items()]

    def content_hashes(self, firma_id: int) -> dict:
        return {}

    def add_discovered(self, firma_id: int, run_id: int, links: list, parent_id: Optional[int] = None) -> None:
        site = self._sites[firma_id]
        for link in links:
            self._discover(site, link)

    def load_frontier(self, firma_id: int, limit: int) -> list:
        pending = [(page.priority, page.id, digest, page) for digest, page in self._sites[firma_id].pages.items()
                   if page.status == st.STRONA_OCZEKUJE]
        pending.sort(key=lambda item: (item[0], item[1]))
        return [{"url": page.url, "url_hash": digest, "glebokosc": page.depth, "priorytet": page.priority,
                 "zrodlo": page.source} for _, _, digest, page in pending[:limit]]

    def save_result(self, firma_id: int, run_id: int, rec: PageRecord) -> int:
        site = self._sites[firma_id]
        page = site.pages.get(rec.url_hash)
        if page is None:
            page = self._new_page(rec.url, rec.depth, rec.priority, rec.source)
            site.pages[rec.url_hash] = page
        page.status, page.http_status, page.bytes = rec.status, rec.http_status, int(rec.bytes or 0)
        page.title, page.description, page.error = rec.title or "", rec.description or "", rec.error or ""
        if rec.text is not None:
            page.text = rec.text
        for link in rec.links:
            self._discover(site, link)
        for contact in rec.contacts:
            self._add_contact(site, contact, rec.url)
        return page.id

    # ---- pomocnicze ---------------------------------------------------------------------
    def _new_page(self, url: str, depth: int, priority: int, source: str) -> PageEntry:
        with self._lock:
            self._page_seq += 1
            return PageEntry(self._page_seq, url, int(depth), int(priority), source)

    def _discover(self, site: _Site, link) -> None:
        if link.url_hash not in site.pages:
            site.pages[link.url_hash] = self._new_page(link.url, link.depth, link.priority, link.source)

    @staticmethod
    def _add_contact(site: _Site, contact, source_url: str) -> None:
        key = (contact.typ, contact.wartosc)
        known = site.contacts.get(key)
        if known is not None:
            known.pages += 1
        else:
            site.contacts[key] = ContactEntry(contact.typ, contact.wartosc, bool(contact.w_domenie), contact.rola or "",
                                              source_url)
