"""Skanowanie jednej firmy: robots.txt, sitemapy, kolejka adresów, odwiedzanie stron i zamknięcie dzierżawy.

Każda ścieżka wyjścia z firmy to `Finish` (status + powód). Elementy niezapisane (przerwane żądanie, stop z powodu
odmowy) zostają w bazie jako `oczekuje`, więc „Ponów"/wznowienie podejmuje dokładnie od tego miejsca.
"""

from __future__ import annotations

import traceback
from datetime import timedelta
from typing import Optional

from sqlalchemy import exc as sa_exc

from perno.db import DB_ERRORS
from perno.scraper import robots
from perno.scraper import statuses as st
from perno.scraper.company import Company
from perno.scraper.page_fetch import Got, PageFetcher
from perno.scraper.page_pipeline import PagePipeline
from perno.scraper.run_context import RunContext
from perno.scraper.settings import FRONTIER_MEMORY_CAP, MAX_DISCOVERED_PER_FIRM
from perno.scraper.store import LeaseLost, LinkRecord, PageRecord
from perno.scraper.urls import normalize_url, path_query, priority, url_hash

ROBOTS_TTL = timedelta(hours=24)
MAX_SITEMAP_FETCHES = 6
MAX_SITEMAP_URLS = 2000
MIN_USEFUL_TEXT = 100


class Finish(Exception):
    """Zakończenie firmy (każda ścieżka wyjścia z `_crawl`)."""

    def __init__(self, status: str, reason: Optional[str] = None, message: str = "",
                 target: Optional[str] = None, hint: float = 0.0):
        super().__init__(message or reason or status)
        self.status, self.reason, self.message, self.target, self.hint = status, reason, message, target, hint


class CompanyCrawler:
    def __init__(self, rc: RunContext):
        self.rc = rc
        self.fetch = PageFetcher(rc)
        self.pipeline = PagePipeline(rc)

    def crawl_company(self, firma: dict) -> None:
        company = Company(firma, self.rc.settings, self.rc.clock, self.rc.rng, self.rc.clock.now() + self.rc.settings.max_minutes * 60.0)
        self.rc.stats.firm_started(company.domena)
        self.rc.log(f"[{company.domena}] start (strony={company.pages}, bajty={company.bytes})")
        outcome = Finish(st.FIRMA_BLAD, "siec", "nieoczekiwany koniec")
        try:
            self._crawl(company)
            outcome = Finish(st.FIRMA_ZAKONCZONA)
        except Finish as done:
            outcome = done
        except LeaseLost:
            self.rc.log(f"[{company.domena}] dzierżawa utracona — porzucam bez zapisu")
            self.rc.stats.firm_done(company.domena, "dzierzawa_utracona")
            return
        except (DB_ERRORS + (sa_exc.DBAPIError,)):
            self.rc.stats.firm_done(company.domena, "baza")
            raise
        except Exception as exc:  # noqa: BLE001 — jedna firma nie może zabić przebiegu
            self.rc.log(f"[{company.domena}] BŁĄD: {type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}")
            outcome = Finish(st.FIRMA_BLAD, "siec", f"{type(exc).__name__}: {exc}")
        retry_after = None
        if outcome.hint and outcome.status == st.FIRMA_ZABLOKOWANA:
            retry_after = self.rc.store.db.now() + timedelta(seconds=min(outcome.hint, 7 * 86400))
        elif outcome.reason == "http_429":
            retry_after = self.rc.store.db.now() + timedelta(hours=1)
        with self.rc.stats.timed("baza_inne"):
            released = self.rc.store.release_firma(company.id, self.rc.run_id, outcome.status, outcome.reason,
                                                   outcome.message or None, outcome.target, retry_after)
        self.rc.stats.firm_done(company.domena, outcome.status if released else "dzierzawa_utracona")
        why = f" ({outcome.reason})" if outcome.reason else ""
        self.rc.log(f"[{company.domena}] koniec: {outcome.status}{why} strony={company.pages} {outcome.message}".rstrip())

    def _crawl(self, company: Company) -> None:
        self._load_robots(company)
        self._prepare_frontier(company)
        while True:
            if self.rc.stop.is_set():
                raise Finish(st.FIRMA_W_KOLEJCE, "stop_uzytkownika")
            item = self._next_item(company)
            if item is None:
                return
            if company.pages >= self.rc.settings.max_pages:
                raise Finish(st.FIRMA_LIMIT, "limit_stron")
            if company.bytes >= self.rc.settings.max_bytes:
                raise Finish(st.FIRMA_LIMIT, "limit_bajtow")
            if self.rc.clock.now() >= company.deadline:
                raise Finish(st.FIRMA_LIMIT, "limit_czasu")
            self._visit(company, item)

    def _load_robots(self, company: Company) -> None:
        firma = company.firma
        now = self.rc.store.db.now()
        fetched_at = firma.get("robots_at")
        cached = (firma.get("robots_status") in (robots.OK, robots.BRAK) and fetched_at is not None
                  and timedelta(0) <= now - fetched_at < ROBOTS_TTL)
        if cached:
            company.rules = robots.parse_robots(firma.get("robots_txt") or "")
            company.rules.crawl_delay = firma.get("crawl_delay")
        else:
            got = self.fetch.get(company, company.origin + "/robots.txt", "robots", is_start=True)
            self._raise_for_non_ok(company, got, robots_mode=True)
            res = got.response
            outcome = robots.interpret(res.status or 0, res.body, res.content_type)
            if res.error or outcome.status in (robots.NIEDOSTEPNY, robots.BACKOFF):
                raise Finish(st.FIRMA_BLAD, "robots_niedostepny", f"robots.txt: {res.error or res.status}")
            if outcome.status == robots.ODMOWA:
                raise Finish(st.FIRMA_ZABLOKOWANA, "robots_odmowa")
            company.rules = outcome.rules
            text = res.body.decode("utf-8", errors="replace") if outcome.status == robots.OK else ""
            with self.rc.stats.timed("baza_inne"):
                self.rc.store.save_robots(company.id, self.rc.run_id, outcome.status, text, company.rules.crawl_delay)
        company.gate.set_robots_delay(company.rules.crawl_delay, after_request=not cached)
        if company.rules.crawl_delay:
            self.rc.log(f"[{company.domena}] robots.txt: Crawl-delay {company.rules.crawl_delay:g}s — respektuję")

    def _raise_for_non_ok(self, company: Company, got: Got, *, robots_mode: bool = False, lenient: bool = False) -> None:
        """Zamienia wynik `_get` w `Finish`. `lenient` (sitemapy): przekierowanie/zły adres nie kończy firmy."""
        if got.kind == "ok":
            return
        if lenient and got.kind in ("foreign", "bad", "robots_skip", "known"):
            return
        if got.kind == "interrupted":
            raise Finish(st.FIRMA_W_KOLEJCE, "stop_uzytkownika")
        if got.kind == "limit_czasu":
            raise Finish(st.FIRMA_LIMIT, "limit_czasu")
        if got.kind == "foreign":
            raise Finish(st.FIRMA_POMINIETA, "przekierowanie", f"przekierowanie na {got.target}", target=got.target)
        if got.kind == "stop":
            reason = "robots_odmowa" if (robots_mode and got.reason == "http_403") else got.reason
            raise Finish(st.PRZYCZYNA_STATUS.get(reason, st.FIRMA_BLAD), reason, got.message, hint=got.hint)
        raise Finish(st.FIRMA_BLAD, "robots_niedostepny" if robots_mode else "siec", got.message or got.kind)

    def _discover_sitemaps(self, company: Company) -> list:
        rules = company.require_rules()
        listed = []
        for raw in rules.sitemaps[:5]:
            url = normalize_url(raw)
            if url and company.same_site(url) and not url.endswith(".gz") and url not in listed:
                listed.append(url)
        default = normalize_url(company.origin + "/sitemap.xml")
        if default and default not in listed:
            listed.append(default)
        queue = [(url, 0) for url in listed]
        found: list = []
        fetched = 0
        while queue and fetched < MAX_SITEMAP_FETCHES and len(found) < MAX_SITEMAP_URLS:
            url, level = queue.pop(0)
            if not rules.allowed(path_query(url)):
                continue
            fetched += 1
            got = self.fetch.get(company, url, "sitemap", is_start=False)
            self._raise_for_non_ok(company, got, lenient=True)
            res = got.response
            if got.kind != "ok" or res.status != 200 or res.error or not res.body:
                continue
            pages, children = robots.parse_sitemap(res.body, max_urls=MAX_SITEMAP_URLS - len(found))
            found.extend(pages)
            if level < 1:
                for child in children:
                    normalized = normalize_url(child)
                    if normalized and company.same_site(normalized) and not normalized.endswith(".gz"):
                        queue.append((normalized, level + 1))
        return found

    def _prepare_frontier(self, company: Company) -> None:
        with self.rc.stats.timed("baza_inne"):
            known = self.rc.store.known_urls(company.id)
        for url, digest in known:
            company.seen.add(digest)
            company.trap.admit(url)  # odbudowa liczników pułapek po wznowieniu
        with self.rc.stats.timed("baza_inne"):
            company.content = self.rc.store.content_hashes(company.id)
        if known:
            return
        start = normalize_url(company.website)
        if not start:
            raise Finish(st.FIRMA_POMINIETA, "zly_url", company.website)
        company.seen.add(url_hash(start))
        company.trap.admit(start)
        links = [LinkRecord(start, url_hash(start), 0, 0, "start")]
        for raw in self._discover_sitemaps(company):
            url = normalize_url(raw)
            if not url or not company.same_site(url) or url_hash(url) in company.seen:
                continue
            if len(company.seen) >= MAX_DISCOVERED_PER_FIRM:
                break
            ok, _ = company.trap.admit(url)
            if not ok:
                continue
            company.seen.add(url_hash(url))
            links.append(LinkRecord(url, url_hash(url), 1,
                                    priority(url, 1, from_sitemap=True, start_prefix=company.start_prefix), "sitemap"))
        with self.rc.stats.timed("baza_inne"):
            self.rc.store.add_discovered(company.id, self.rc.run_id, links)
        self.rc.log(f"[{company.domena}] kolejka startowa: {len(links)} adresów (w tym sitemapa: {len(links) - 1})")

    def _next_item(self, company: Company) -> Optional[dict]:
        item = company.pop()
        if item is not None:
            return item
        with self.rc.stats.timed("baza_inne"):
            frontier = self.rc.store.load_frontier(company.id, FRONTIER_MEMORY_CAP)
        for row in frontier:
            company.push({"url": row["url"], "url_hash": row["url_hash"], "depth": row["glebokosc"],
                      "priorytet": row["priorytet"], "source": row["zrodlo"]})
        return company.pop()

    def _visit(self, company: Company, item: dict) -> None:
        is_start = item["source"] == "start"
        if self._skip_unfetched(company, item, is_start):
            return
        got = self.fetch.get(company, item["url"], "html", is_start)
        self._raise_for_failed_page(got, is_start)
        first_hop = got.hops[0][1] if got.hops else None
        if got.kind in ("foreign", "robots_skip", "known", "bad"):
            note = {"foreign": f"obca domena: {got.target}", "robots_skip": "przekierowanie na adres zabroniony w robots.txt",
                    "known": f"→ {got.url}", "bad": got.message}[got.kind]
            self._save(company, self.pipeline.plain(item, st.STRONA_PRZEKIEROWANIE, http_status=first_hop, bytes=0, error=note[:500]))
            return
        if got.hops:
            self._save(company, self.pipeline.plain(item, st.STRONA_PRZEKIEROWANIE, http_status=first_hop, bytes=0, error=f"→ {got.url}"[:500]))
        self._store_page(company, item, got, is_start)

    def _skip_unfetched(self, company: Company, item: dict, is_start: bool) -> bool:
        """Strony, których nie pobieramy (zabronione w robots.txt, za głęboko): zapisuje wpis i zwraca True."""
        if not company.require_rules().allowed(path_query(item["url"])):
            if is_start:
                raise Finish(st.FIRMA_ZABLOKOWANA, "robots_disallow", "robots.txt zabrania strony głównej")
            self._save(company, self.pipeline.plain(item, st.STRONA_POMINIETO_ROBOTS, counts=False, error="robots.txt"))
            return True
        if item["depth"] > self.rc.settings.depth:
            self._save(company, self.pipeline.plain(item, st.STRONA_POMINIETO_LIMIT, counts=False, error="głębokość"))
            return True
        return False

    @staticmethod
    def _raise_for_failed_page(got: Got, is_start: bool) -> None:
        """Zamienia nieudane pobranie w `Finish` (stop, limit czasu; dla strony głównej także przekierowania na obcą
        domenę albo zabroniony adres). Pozostałe wyniki wracają do wywołującego."""
        if got.kind == "interrupted":
            raise Finish(st.FIRMA_W_KOLEJCE, "stop_uzytkownika")
        if got.kind == "limit_czasu":
            raise Finish(st.FIRMA_LIMIT, "limit_czasu")
        if got.kind == "stop":
            raise Finish(st.PRZYCZYNA_STATUS.get(got.reason, st.FIRMA_BLAD), got.reason, got.message, hint=got.hint)
        if is_start and got.kind in ("foreign", "robots_skip", "bad"):
            if got.kind == "foreign":
                raise Finish(st.FIRMA_POMINIETA, "przekierowanie", f"przekierowanie na {got.target}", target=got.target)
            if got.kind == "robots_skip":
                raise Finish(st.FIRMA_ZABLOKOWANA, "robots_disallow", "przekierowanie strony głównej na zabroniony adres")
            raise Finish(st.FIRMA_BLAD, "brak_strony_glownej", got.message)

    def _store_page(self, company: Company, item: dict, got: Got, is_start: bool) -> None:
        """Klasyfikuje pobraną stronę, zapisuje ją i dokłada nowe linki do kolejki firmy."""
        res = got.response
        with self.rc.stats.timed("analiza"):
            rec = self.pipeline.classify(company, item, got.url, res)
        if is_start and rec.status not in (st.STRONA_POBRANO, st.STRONA_DUPLIKAT):
            reason = self.pipeline.failure_reason(res)
            raise Finish(st.PRZYCZYNA_STATUS.get(reason, st.FIRMA_BLAD), reason,
                         rec.error or f"strona główna: {res.status or res.error}")
        self._save(company, rec)
        company.trap.record(got.url, rec.status == st.STRONA_POBRANO and len(rec.text or "") >= MIN_USEFUL_TEXT)
        for link in rec.links:
            company.push({"url": link.url, "url_hash": link.url_hash, "depth": link.depth, "priorytet": link.priority,
                          "source": link.source})

    def _save(self, company: Company, rec: PageRecord) -> int:
        with self.rc.stats.timed("zapis"):
            page_id = self.rc.store.save_result(company.id, self.rc.run_id, rec)
        if rec.counts:
            company.pages += 1
            company.bytes += int(rec.bytes or 0)
            self.rc.stats.add(strony=1 if rec.status == st.STRONA_POBRANO else 0, bajty=int(rec.bytes or 0))
        if rec.status == st.STRONA_POBRANO:
            company.pobrano += 1
        if rec.contacts:
            self.rc.stats.add(kontakty=len(rec.contacts))
        return page_id
