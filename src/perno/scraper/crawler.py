"""Crawler: pula wątków, jedna firma = jeden wątek (z natury najwyżej jedno żądanie naraz do hosta).

Równoległość istnieje wyłącznie MIĘDZY firmami. Każde żądanie przechodzi przez: odstęp hosta (`HostGate`) →
limit na IP (`IpLimiter`) → bramkę globalną (`GlobalGate`) → fetcher. Odmowa serwera (429/403/bot-wall) =
wycofanie i zatrzymanie firmy, nigdy obejście. Strona jest zapisywana w jednej transakcji z kontrolą dzierżawy.

Elementy niezapisane (przerwane żądanie, stop z powodu odmowy) zostają w bazie jako `oczekuje`, więc
„Ponów"/wznowienie podejmuje dokładnie od tego miejsca.
"""

from __future__ import annotations

import hashlib
import heapq
import random
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Optional
from urllib.parse import urlsplit

from sqlalchemy import exc as sa_exc

from perno.db import DB_ERRORS
from perno.scraper import contacts as contacts_mod
from perno.scraper import extract, robots
from perno.scraper import statuses as st
from perno.scraper.fetcher import Fetcher, FetchError, FetchResult, decode_html
from perno.scraper.politeness import GlobalGate, HostGate, IpLimiter, RealClock, detect_bot_wall, looks_like_cdn
from perno.scraper.settings import (
    FRONTIER_MEMORY_CAP,
    HEARTBEAT_EVERY,
    MAX_DISCOVERED_PER_FIRM,
    MAX_REDIRECTS,
    ScanSettings,
)
from perno.scraper.store import LeaseLost, LinkRecord, PageRecord, Store, compress_html
from perno.scraper.urls import TrapGuard, host_key, normalize_url, page_class, priority, site_key, url_hash

ROBOTS_TTL = timedelta(hours=24)
REDIRECT_CODES = (301, 302, 303, 307, 308)
MAX_RETRIES_PER_URL = 6
MAX_SITEMAP_FETCHES = 6
MAX_SITEMAP_URLS = 2000
DB_GRACE_SECONDS = 60.0
MIN_USEFUL_TEXT = 100
SHORT_TEXT_FOR_HASH = 200
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.I | re.S)


class Finish(Exception):
    """Zakończenie firmy (każda ścieżka wyjścia z `_crawl`)."""

    def __init__(self, status: str, reason: Optional[str] = None, message: str = "",
                 target: Optional[str] = None, hint: float = 0.0):
        super().__init__(message or reason or status)
        self.status, self.reason, self.message, self.target, self.hint = status, reason, message, target, hint


@dataclass
class Got:
    kind: str  # ok | stop | interrupted | limit_czasu | foreign | robots_skip | known | bad
    fetch: Optional[FetchResult] = None
    url: str = ""
    hops: list = field(default_factory=list)  # [(url, status)] przeskoki przekierowań
    reason: str = ""
    hint: float = 0.0
    target: str = ""
    message: str = ""


class Stats:
    def __init__(self, clock):
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

    def push(self, item: dict) -> None:
        self.counter += 1
        heapq.heappush(self.heap, (item["priorytet"], self.counter, item))

    def pop(self) -> Optional[dict]:
        return heapq.heappop(self.heap)[2] if self.heap else None


def _path_query(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def _visible_len(body: bytes) -> int:
    text = body[:60_000].decode("utf-8", errors="ignore")
    return len(re.sub(r"\s+", " ", _TAG_RE.sub(" ", text)).strip())


class Crawler:
    def __init__(self, store: Store, settings: ScanSettings, run_id: int, *, fetcher: Optional[Fetcher] = None,
                 clock=None, rng: Optional[random.Random] = None, log: Optional[Callable] = None,
                 only_ids: Optional[list] = None, stop_event: Optional[threading.Event] = None,
                 stagger: float = 0.25, heartbeat_every: float = HEARTBEAT_EVERY,
                 ip_limiter: Optional[IpLimiter] = None, global_gate: Optional[GlobalGate] = None,
                 verbose: bool = False):
        self.store = store
        self.settings = settings
        self.run_id = run_id
        self.clock = clock or RealClock()
        self.rng = rng or random.Random()
        self.fetcher = fetcher or Fetcher(settings)
        self.log_fn = log or (lambda message: None)
        self.verbose = verbose
        self.only_ids = only_ids
        self.stop = stop_event or threading.Event()
        self.stagger = stagger
        self.heartbeat_every = heartbeat_every
        self.ip_limiter = ip_limiter or IpLimiter(cap=settings.ip_cap, clock=self.clock)
        self.global_gate = global_gate or GlobalGate(settings.max_rps, clock=self.clock)
        self.parse_slots = threading.BoundedSemaphore(4)
        self.stats = Stats(self.clock)
        self.stop_reason = ""
        self.db_failed = False
        self._failure_lock = threading.Lock()
        self.run_deadline = self.clock.now() + settings.run_max_hours * 3600.0

    def log(self, message: str) -> None:
        self.log_fn(message)

    # ---- przebieg ------------------------------------------------------------------
    def run(self) -> dict:
        workers = []
        count = max(1, self.settings.concurrency)
        for index in range(count):
            thread = threading.Thread(target=self._worker, name=f"crawl-{index}", daemon=True)
            thread.start()
            workers.append(thread)
            if self.stagger and index < count - 1 and self.stop.wait(self.stagger):
                break
        self._coordinate(workers)
        status = st.RUN_PRZERWANY if self.db_failed else (st.RUN_ZATRZYMANY if self.stop.is_set() else st.RUN_ZAKONCZONY)
        return {"status": status, "reason": self.stop_reason, "stats": self.stats.snapshot()}

    def _coordinate(self, workers: list) -> None:
        last_beat = 0.0
        failing_since: Optional[float] = None
        last_summary = time.monotonic()
        while any(worker.is_alive() for worker in workers):
            time.sleep(0.05)
            now = time.monotonic()
            if self.clock.now() >= self.run_deadline and not self.stop.is_set():
                self.stop_reason = "limit_przebiegu"
                self.log("Osiągnięto maksymalny czas przebiegu — zatrzymuję.")
                self.stop.set()
            if now - last_beat < self.heartbeat_every:
                continue
            last_beat = now
            try:
                requested = self.store.heartbeat(self.run_id, self.stats.snapshot())
                failing_since = None
                if requested and not self.stop.is_set():
                    self.stop_reason = self.stop_reason or "stop_uzytkownika"
                    self.log("Otrzymano prośbę o zatrzymanie.")
                    self.stop.set()
            except (DB_ERRORS + (sa_exc.DBAPIError,)) as exc:
                failing_since = failing_since or now
                if now - failing_since > DB_GRACE_SECONDS:
                    self._db_failure(exc)
            if now - last_summary >= 30:
                last_summary = now
                snap = self.stats.snapshot()
                self.log(f"[podsumowanie] strony={snap['strony']} zapytania={snap['zapytania']} aktywne={snap['aktywne']} "
                         f"strony/min={snap['strony_na_min']}")
        try:
            self.store.heartbeat(self.run_id, self.stats.snapshot())
        except (DB_ERRORS + (sa_exc.DBAPIError,)):
            pass

    def _db_failure(self, exc: Exception) -> None:
        with self._failure_lock:
            if self.db_failed:
                return
            self.db_failed = True
            self.stop_reason = "baza"
            self.log(f"Utracono połączenie z bazą ({type(exc).__name__}) — kończę bez zapisu; firmy wrócą do kolejki po odzyskaniu.")
            self.stop.set()

    def _worker(self) -> None:
        while not self.stop.is_set():
            try:
                firma = self.store.claim_next_firma(self.run_id, self.only_ids)
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

    # ---- jedna firma ---------------------------------------------------------------
    def crawl_company(self, firma: dict) -> None:
        ctx = Company(firma, self.settings, self.clock, self.rng, self.clock.now() + self.settings.max_minutes * 60.0)
        self.stats.firm_started(ctx.domena)
        self.log(f"[{ctx.domena}] start (strony={ctx.pages}, bajty={ctx.bytes})")
        outcome = Finish(st.FIRMA_BLAD, "siec", "nieoczekiwany koniec")
        try:
            self._crawl(ctx)
            outcome = Finish(st.FIRMA_ZAKONCZONA)
        except Finish as done:
            outcome = done
        except LeaseLost:
            self.log(f"[{ctx.domena}] dzierżawa utracona — porzucam bez zapisu")
            self.stats.firm_done(ctx.domena, "dzierzawa_utracona")
            return
        except (DB_ERRORS + (sa_exc.DBAPIError,)):
            self.stats.firm_done(ctx.domena, "baza")
            raise
        except Exception as exc:  # noqa: BLE001 — jedna firma nie może zabić przebiegu
            self.log(f"[{ctx.domena}] BŁĄD: {type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}")
            outcome = Finish(st.FIRMA_BLAD, "siec", f"{type(exc).__name__}: {exc}")
        retry_after = None
        if outcome.hint and outcome.status == st.FIRMA_ZABLOKOWANA:
            retry_after = self.store.db.now() + timedelta(seconds=min(outcome.hint, 7 * 86400))
        elif outcome.reason == "http_429":
            retry_after = self.store.db.now() + timedelta(hours=1)
        released = self.store.release_firma(ctx.id, self.run_id, outcome.status, outcome.reason, outcome.message or None,
                                            outcome.target, retry_after)
        self.stats.firm_done(ctx.domena, outcome.status if released else "dzierzawa_utracona")
        why = f" ({outcome.reason})" if outcome.reason else ""
        self.log(f"[{ctx.domena}] koniec: {outcome.status}{why} strony={ctx.pages} {outcome.message}".rstrip())

    def _crawl(self, ctx: Company) -> None:
        self._load_robots(ctx)
        self._prepare_frontier(ctx)
        while True:
            if self.stop.is_set():
                raise Finish(st.FIRMA_W_KOLEJCE, "stop_uzytkownika")
            item = self._next_item(ctx)
            if item is None:
                return
            if ctx.pages >= self.settings.max_pages:
                raise Finish(st.FIRMA_LIMIT, "limit_stron")
            if ctx.bytes >= self.settings.max_bytes:
                raise Finish(st.FIRMA_LIMIT, "limit_bajtow")
            if self.clock.now() >= ctx.deadline:
                raise Finish(st.FIRMA_LIMIT, "limit_czasu")
            self._visit(ctx, item)

    # ---- robots i sitemapy -------------------------------------------------------------
    def _load_robots(self, ctx: Company) -> None:
        firma = ctx.firma
        now = self.store.db.now()
        fetched_at = firma.get("robots_at")
        cached = (firma.get("robots_status") in (robots.OK, robots.BRAK) and fetched_at is not None
                  and timedelta(0) <= now - fetched_at < ROBOTS_TTL)
        if cached:
            ctx.rules = robots.parse_robots(firma.get("robots_txt") or "")
            ctx.rules.crawl_delay = firma.get("crawl_delay")
        else:
            got = self._get(ctx, ctx.origin + "/robots.txt", "robots", is_start=True)
            self._raise_for_non_ok(ctx, got, robots_mode=True)
            res = got.fetch
            outcome = robots.interpret(res.status or 0, res.body, res.content_type)
            if res.error or outcome.status in (robots.NIEDOSTEPNY, robots.BACKOFF):
                raise Finish(st.FIRMA_BLAD, "robots_niedostepny", f"robots.txt: {res.error or res.status}")
            if outcome.status == robots.ODMOWA:
                raise Finish(st.FIRMA_ZABLOKOWANA, "robots_odmowa")
            ctx.rules = outcome.rules
            text = res.body.decode("utf-8", errors="replace") if outcome.status == robots.OK else ""
            self.store.save_robots(ctx.id, self.run_id, outcome.status, text, ctx.rules.crawl_delay)
        ctx.gate.set_robots_delay(ctx.rules.crawl_delay, after_request=not cached)
        if ctx.rules.crawl_delay:
            self.log(f"[{ctx.domena}] robots.txt: Crawl-delay {ctx.rules.crawl_delay:g}s — respektuję")

    def _raise_for_non_ok(self, ctx: Company, got: Got, *, robots_mode: bool = False, lenient: bool = False) -> None:
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

    def _discover_sitemaps(self, ctx: Company) -> list:
        listed = []
        for raw in ctx.rules.sitemaps[:5]:
            url = normalize_url(raw)
            if url and self._same_site(ctx, url) and not url.endswith(".gz") and url not in listed:
                listed.append(url)
        default = normalize_url(ctx.origin + "/sitemap.xml")
        if default and default not in listed:
            listed.append(default)
        queue = [(url, 0) for url in listed]
        found: list = []
        fetched = 0
        while queue and fetched < MAX_SITEMAP_FETCHES and len(found) < MAX_SITEMAP_URLS:
            url, level = queue.pop(0)
            if not ctx.rules.allowed(_path_query(url)):
                continue
            fetched += 1
            got = self._get(ctx, url, "sitemap", is_start=False)
            self._raise_for_non_ok(ctx, got, lenient=True)
            res = got.fetch
            if got.kind != "ok" or res.status != 200 or res.error or not res.body:
                continue
            pages, children = robots.parse_sitemap(res.body, max_urls=MAX_SITEMAP_URLS - len(found))
            found.extend(pages)
            if level < 1:
                for child in children:
                    normalized = normalize_url(child)
                    if normalized and self._same_site(ctx, normalized) and not normalized.endswith(".gz"):
                        queue.append((normalized, level + 1))
        return found

    # ---- kolejka -------------------------------------------------------------------
    def _prepare_frontier(self, ctx: Company) -> None:
        known = self.store.known_urls(ctx.id)
        for url, digest in known:
            ctx.seen.add(digest)
            ctx.trap.admit(url)  # odbudowa liczników pułapek po wznowieniu
        ctx.content = self.store.content_hashes(ctx.id)
        if known:
            return
        start = normalize_url(ctx.website)
        if not start:
            raise Finish(st.FIRMA_POMINIETA, "zly_url", ctx.website)
        ctx.seen.add(url_hash(start))
        ctx.trap.admit(start)
        links = [LinkRecord(start, url_hash(start), 0, 0, "start")]
        for raw in self._discover_sitemaps(ctx):
            url = normalize_url(raw)
            if not url or not self._same_site(ctx, url) or url_hash(url) in ctx.seen:
                continue
            if len(ctx.seen) >= MAX_DISCOVERED_PER_FIRM:
                break
            ok, _ = ctx.trap.admit(url)
            if not ok:
                continue
            ctx.seen.add(url_hash(url))
            links.append(LinkRecord(url, url_hash(url), 1,
                                    priority(url, 1, from_sitemap=True, start_prefix=ctx.start_prefix), "sitemap"))
        self.store.add_discovered(ctx.id, self.run_id, links)
        self.log(f"[{ctx.domena}] kolejka startowa: {len(links)} adresów (w tym sitemapa: {len(links) - 1})")

    def _next_item(self, ctx: Company) -> Optional[dict]:
        item = ctx.pop()
        if item is not None:
            return item
        for row in self.store.load_frontier(ctx.id, FRONTIER_MEMORY_CAP):
            ctx.push({"url": row["url"], "url_hash": row["url_hash"], "depth": row["glebokosc"],
                      "priorytet": row["priorytet"], "source": row["zrodlo"]})
        return ctx.pop()

    def _same_site(self, ctx: Company, url: str) -> bool:
        return site_key(url) == ctx.domena

    # ---- pobranie (żądanie + polityka + przekierowania) -------------------------------------
    def _get(self, ctx: Company, url: str, kind: str, is_start: bool) -> Got:
        current = url
        visited = {url}
        hops: list = []
        retries = 0
        while True:
            if self.stop.is_set():
                return Got("interrupted")
            if not ctx.gate.wait_turn(self.stop, ctx.deadline):
                return Got("interrupted") if self.stop.is_set() else Got("limit_czasu")
            parts = urlsplit(current)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            ip = None
            try:
                ip = self.fetcher.resolve(parts.hostname or "", port)[0]
                res = None
            except FetchError as exc:
                res = FetchResult(url=current, error=exc.kind)
            if res is None:
                budget = max(0.0, ctx.deadline - self.clock.now())
                if not self.ip_limiter.acquire(ip, self.stop, timeout=budget):
                    return Got("interrupted") if self.stop.is_set() else Got("limit_czasu")
                try:
                    if not self.global_gate.wait(self.stop):
                        return Got("interrupted")
                    res = self.fetcher.fetch_once(current, kind, ip=ip)
                finally:
                    self.ip_limiter.release(ip)
            self.stats.add(zapytania=1)
            if self.verbose:
                self.log(f"[{ctx.domena}] {kind} {current} -> {res.status or res.error} ({res.elapsed:.2f}s)")
            if ip and looks_like_cdn(res.headers):
                self.ip_limiter.lift(ip)
            if res.error == "ssrf":
                return Got("stop", fetch=res, url=current, reason="ssrf", message=st.PRZYCZYNY["ssrf"], hops=hops)
            verdict = ctx.gate.on_response(
                status=res.status, elapsed=res.elapsed, retry_after=res.headers.get("retry-after"), is_start=is_start,
                bot_wall=self._bot_wall(res), error=res.error if res.error in ("dns", "timeout", "siec", "tls") else None,
                retries_used=retries)
            if verdict.action == "stop":
                return Got("stop", fetch=res, url=current, reason=verdict.reason, hint=verdict.retry_hint, hops=hops,
                           message=st.PRZYCZYNY.get(verdict.reason, ""))
            if verdict.action == "retry":
                retries += 1
                self.log(f"[{ctx.domena}] wycofanie: {res.status or res.error}, ponowię za {verdict.wait:.0f}s")
                if retries > MAX_RETRIES_PER_URL:
                    return Got("stop", fetch=res, url=current, reason="siec", hops=hops, message=st.PRZYCZYNY["siec"])
                continue
            if res.status in REDIRECT_CODES and res.location:
                hops.append((current, res.status))
                if len(hops) > MAX_REDIRECTS:
                    return Got("bad", fetch=res, url=current, hops=hops, message="za wiele przekierowań")
                target = normalize_url(res.location, base=current)
                if target is None:
                    return Got("bad", fetch=res, url=current, hops=hops, message="nieprawidłowy adres przekierowania")
                if target in visited:
                    return Got("bad", fetch=res, url=current, hops=hops, message="pętla przekierowań")
                visited.add(target)
                if not self._same_site(ctx, target):
                    return Got("foreign", fetch=res, url=target, hops=hops,
                               target=host_key(urlsplit(target).hostname or "") or "")
                if kind == "html":
                    if ctx.rules is not None and not ctx.rules.allowed(_path_query(target)):
                        return Got("robots_skip", fetch=res, url=target, hops=hops)
                    digest = url_hash(target)
                    if digest in ctx.seen:
                        return Got("known", fetch=res, url=target, hops=hops)
                    admitted, why = ctx.trap.admit(target)
                    if not admitted:
                        return Got("bad", fetch=res, url=target, hops=hops, message=f"przekierowanie na odrzucony adres ({why})")
                    ctx.seen.add(digest)
                current, retries = target, 0
                continue
            return Got("ok", fetch=res, url=current, hops=hops)

    @staticmethod
    def _bot_wall(res: FetchResult) -> bool:
        if res.status in (403, 429, 503):
            return detect_bot_wall(res.status, res.headers, res.body)
        if res.status == 200 and res.body and len(res.body) < 20_000:
            return detect_bot_wall(200, res.headers, res.body, _visible_len(res.body))
        return False

    # ---- obsługa jednej strony -------------------------------------------------------------
    def _save(self, ctx: Company, rec: PageRecord) -> int:
        page_id = self.store.save_result(ctx.id, self.run_id, rec)
        if rec.counts:
            ctx.pages += 1
            ctx.bytes += int(rec.bytes or 0)
            self.stats.add(strony=1 if rec.status == st.STRONA_POBRANO else 0, bajty=int(rec.bytes or 0))
        if rec.status == st.STRONA_POBRANO:
            ctx.pobrano += 1
        if rec.contacts:
            self.stats.add(kontakty=len(rec.contacts))
        return page_id

    @staticmethod
    def _plain(item: dict, status: str, url: Optional[str] = None, **kwargs) -> PageRecord:
        target = url or item["url"]
        return PageRecord(url=target, url_hash=url_hash(target), depth=item["depth"], priority=item["priorytet"],
                          source=item["source"], status=status, **kwargs)

    def _visit(self, ctx: Company, item: dict) -> None:
        is_start = item["source"] == "start"
        if not ctx.rules.allowed(_path_query(item["url"])):
            if is_start:
                raise Finish(st.FIRMA_ZABLOKOWANA, "robots_disallow", "robots.txt zabrania strony głównej")
            self._save(ctx, self._plain(item, st.STRONA_POMINIETO_ROBOTS, counts=False, error="robots.txt"))
            return
        if item["depth"] > self.settings.depth:
            self._save(ctx, self._plain(item, st.STRONA_POMINIETO_LIMIT, counts=False, error="głębokość"))
            return

        got = self._get(ctx, item["url"], "html", is_start)
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

        first_hop = got.hops[0][1] if got.hops else None
        if got.kind in ("foreign", "robots_skip", "known", "bad"):
            note = {"foreign": f"obca domena: {got.target}", "robots_skip": "przekierowanie na adres zabroniony w robots.txt",
                    "known": f"→ {got.url}", "bad": got.message}[got.kind]
            self._save(ctx, self._plain(item, st.STRONA_PRZEKIEROWANIE, http_status=first_hop, bytes=0, error=note[:500]))
            return

        res = got.fetch
        final_item = item
        if got.hops:
            self._save(ctx, self._plain(item, st.STRONA_PRZEKIEROWANIE, http_status=first_hop, bytes=0, error=f"→ {got.url}"[:500]))
            final_item = {**item}
        rec = self._classify(ctx, final_item, got.url, res)
        if is_start and rec.status != st.STRONA_POBRANO and rec.status != st.STRONA_DUPLIKAT:
            raise Finish(st.PRZYCZYNA_STATUS.get(self._reason_for(res), st.FIRMA_BLAD), self._reason_for(res),
                         rec.error or f"strona główna: {res.status or res.error}")
        self._save(ctx, rec)
        ctx.trap.record(got.url, rec.status == st.STRONA_POBRANO and len(rec.text or "") >= MIN_USEFUL_TEXT)
        for link in rec.links:
            ctx.push({"url": link.url, "url_hash": link.url_hash, "depth": link.depth, "priorytet": link.priority,
                      "source": link.source})

    @staticmethod
    def _reason_for(res: FetchResult) -> str:
        if res.error in ("timeout", "tls", "siec", "dns"):
            return res.error
        if res.status and res.status >= 500:
            return "http_5xx"
        if res.status in (401, 403, 406, 451):
            return "http_403"
        return "brak_strony_glownej"

    def _classify(self, ctx: Company, item: dict, url: str, res: FetchResult) -> PageRecord:
        elapsed = int(res.elapsed * 1000)
        base = dict(http_status=res.status, content_type=res.content_type or None, elapsed_ms=elapsed)
        if res.error == "niehtml":
            return self._plain(item, st.STRONA_NIEHTML, url, bytes=0, **base)
        if res.error == "za_duze":
            return self._plain(item, st.STRONA_ZA_DUZE, url, bytes=0, error="strona przekracza limit rozmiaru", **base)
        if res.error:
            return self._plain(item, st.STRONA_BLAD, url, bytes=0, error=f"błąd pobierania: {res.error}", **base)
        if res.status is None:
            return self._plain(item, st.STRONA_BLAD, url, bytes=0, error="brak odpowiedzi", **base)
        if res.status in (401, 403, 406, 451):
            return self._plain(item, st.STRONA_ZABLOKOWANO, url, bytes=0, error=f"HTTP {res.status}", **base)
        if res.status >= 400 or not (200 <= res.status < 300):
            return self._plain(item, st.STRONA_BLAD, url, bytes=0, error=f"HTTP {res.status}", **base)
        if not res.body:
            return self._plain(item, st.STRONA_BLAD, url, bytes=0, error="pusta odpowiedź", **base)

        with self.parse_slots:
            html_text, encoding = decode_html(res.body, res.headers.get("content-type", ""))
            data = extract.parse_html(html_text, url)
        if data.error:
            return self._plain(item, st.STRONA_BLAD, url, bytes=len(res.body), error=f"parsowanie: {data.error}",
                               encoding=encoding, **base)
        basis = data.text if len(data.text) >= SHORT_TEXT_FOR_HASH else html_text
        digest = hashlib.sha1(basis.encode("utf-8", "ignore"), usedforsecurity=False).hexdigest()
        common = dict(bytes=len(res.body), encoding=encoding, title=data.title or None,
                      description=data.description or None, content_hash=digest, **base)
        if digest in ctx.content:
            return self._plain(item, st.STRONA_DUPLIKAT, url, error=f"duplikat: {ctx.content[digest]}"[:500], **common)
        ctx.content[digest] = url

        follow = not extract.nofollow_requested(data.meta_robots, res.headers.get("x-robots-tag", ""))
        links = self._new_links(ctx, item, url, data, follow)
        found = contacts_mod.extract_contacts(
            data, url, ctx.host, [ctx.firma.get("domena_docelowa") or ""], international=ctx.international)
        keep_html = self.settings.store_html == "all" or (
            self.settings.store_html == "klucz" and (item["source"] == "start" or bool(page_class(urlsplit(url).path))))
        return self._plain(item, st.STRONA_POBRANO, url, text=data.text, links=links, contacts=found,
                           html_gz=compress_html(res.body) if keep_html else None, **common)

    def _new_links(self, ctx: Company, item: dict, page_url: str, data, follow: bool) -> list:
        candidates = []
        if follow:
            candidates = [(link.href, link.where in ("nav", "footer", "header"), link.text)
                          for link in data.links if not link.nofollow]
        if data.canonical:
            candidates.append((data.canonical, False, ""))
        next_depth = item["depth"] + 1
        out = []
        for href, in_nav, text in candidates:
            if next_depth > self.settings.depth:
                break
            url = normalize_url(href, base=page_url)
            if not url or not self._same_site(ctx, url):
                continue
            digest = url_hash(url)
            if digest in ctx.seen:
                continue
            if len(ctx.seen) >= MAX_DISCOVERED_PER_FIRM:
                break
            admitted, _ = ctx.trap.admit(url)
            if not admitted:
                continue
            ctx.seen.add(digest)
            out.append(LinkRecord(url, digest, next_depth,
                                  priority(url, next_depth, in_nav=in_nav, anchor_text=text, start_prefix=ctx.start_prefix)))
        return out
