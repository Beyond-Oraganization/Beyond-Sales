"""Pobranie jednej strony: odstęp hosta → limit na IP → bramka globalna → fetcher, z obsługą przekierowań i odmów.

Wynik to `Got` (rodzaj + odpowiedź); decyzje o losie firmy podejmuje `CompanyCrawler`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from perno.scraper import statuses as st
from perno.scraper.company import Company
from perno.scraper.fetcher import FetchError, FetchResult
from perno.scraper.politeness import detect_bot_wall, looks_like_cdn
from perno.scraper.run_context import RunContext
from perno.scraper.settings import MAX_REDIRECTS
from perno.scraper.urls import host_key, normalize_url, path_query, url_hash

REDIRECT_CODES = (301, 302, 303, 307, 308)
MAX_RETRIES_PER_URL = 6
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.I | re.S)


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

    @property
    def response(self) -> FetchResult:
        """Odpowiedź dla wyniku, który ją ma (kind „ok”/przekierowania); brak to błąd w logice crawlera."""
        if self.fetch is None:
            raise RuntimeError(f"wynik pobrania ({self.kind}) nie ma odpowiedzi")
        return self.fetch


def _visible_len(body: bytes) -> int:
    text = body[:60_000].decode("utf-8", errors="ignore")
    return len(re.sub(r"\s+", " ", _TAG_RE.sub(" ", text)).strip())


class PageFetcher:
    def __init__(self, rc: RunContext):
        self.rc = rc

    def get(self, company: Company, url: str, kind: str, is_start: bool) -> Got:
        current = url
        visited = {url}
        hops: list = []
        retries = 0
        while True:
            outcome = self._request(company, current, kind)
            if isinstance(outcome, Got):
                return outcome
            res, ip = outcome
            self.rc.stats.add(zapytania=1)
            if self.rc.verbose:
                self.rc.log(f"[{company.domena}] {kind} {current} -> {res.status or res.error} ({res.elapsed:.2f}s)")
            if ip and looks_like_cdn(res.headers):
                self.rc.ip_limiter.lift(ip)
            if res.error == "ssrf":
                return Got("stop", fetch=res, url=current, reason="ssrf", message=st.PRZYCZYNY["ssrf"], hops=hops)
            verdict = company.gate.on_response(
                status=res.status, elapsed=res.elapsed, retry_after=res.headers.get("retry-after"), is_start=is_start,
                bot_wall=self._bot_wall(res), error=res.error if res.error in ("dns", "timeout", "siec", "tls") else None,
                retries_used=retries)
            if verdict.action == "stop":
                return Got("stop", fetch=res, url=current, reason=verdict.reason, hint=verdict.retry_hint, hops=hops,
                           message=st.PRZYCZYNY.get(verdict.reason, ""))
            if verdict.action == "retry":
                retries += 1
                self.rc.log(f"[{company.domena}] wycofanie: {res.status or res.error}, ponowię za {verdict.wait:.0f}s")
                if retries > MAX_RETRIES_PER_URL:
                    return Got("stop", fetch=res, url=current, reason="siec", hops=hops, message=st.PRZYCZYNY["siec"])
                continue
            if res.status in REDIRECT_CODES and res.location:
                hops.append((current, res.status))
                step = self._follow_redirect(company, res, current, kind, visited, hops)
                if isinstance(step, Got):
                    return step
                current, retries = step, 0
                continue
            return Got("ok", fetch=res, url=current, hops=hops)

    def _request(self, company: Company, url: str, kind: str):
        """Jedno żądanie przez wszystkie bramki: odstęp hosta → DNS → limit na IP → bramka globalna → fetcher.
        Zwraca (odpowiedź, ip) albo gotowy `Got`, gdy przebieg zatrzymano lub skończył się czas firmy.
        Czasy etapów (oczekiwanie, dns, pobieranie) trafiają do statystyk przebiegu."""
        rc = self.rc
        if rc.stop.is_set():
            return Got("interrupted")
        with rc.stats.timed("oczekiwanie"):
            turn = company.gate.wait_turn(rc.stop, company.deadline)
        if not turn:
            return Got("interrupted") if rc.stop.is_set() else Got("limit_czasu")
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            with rc.stats.timed("dns"):
                ip = rc.fetcher.resolve(parts.hostname or "", port)[0]
        except FetchError as exc:
            return FetchResult(url=url, error=exc.kind), ""
        budget = max(0.0, company.deadline - rc.clock.now())
        with rc.stats.timed("oczekiwanie"):
            acquired = rc.ip_limiter.acquire(ip, rc.stop, timeout=budget)
        if not acquired:
            return Got("interrupted") if rc.stop.is_set() else Got("limit_czasu")
        try:
            with rc.stats.timed("oczekiwanie"):
                allowed = rc.global_gate.wait(rc.stop)
            if not allowed:
                return Got("interrupted")
            with rc.stats.timed("pobieranie"):
                return rc.fetcher.fetch_once(url, kind, ip=ip), ip
        finally:
            rc.ip_limiter.release(ip)

    def _follow_redirect(self, company: Company, res: FetchResult, current: str, kind: str, visited: set, hops: list):
        """Następny adres po przekierowaniu albo `Got`, gdy podążanie nie ma sensu (pętla, obca domena, robots…)."""
        if len(hops) > MAX_REDIRECTS:
            return Got("bad", fetch=res, url=current, hops=hops, message="za wiele przekierowań")
        target = normalize_url(res.location or "", base=current)  # wołający gwarantuje niepusty Location
        if target is None:
            return Got("bad", fetch=res, url=current, hops=hops, message="nieprawidłowy adres przekierowania")
        if target in visited:
            return Got("bad", fetch=res, url=current, hops=hops, message="pętla przekierowań")
        visited.add(target)
        if not company.same_site(target):
            return Got("foreign", fetch=res, url=target, hops=hops,
                       target=host_key(urlsplit(target).hostname or "") or "")
        if kind == "html":
            if company.rules is not None and not company.rules.allowed(path_query(target)):
                return Got("robots_skip", fetch=res, url=target, hops=hops)
            digest = url_hash(target)
            if digest in company.seen:
                return Got("known", fetch=res, url=target, hops=hops)
            admitted, why = company.trap.admit(target)
            if not admitted:
                return Got("bad", fetch=res, url=target, hops=hops, message=f"przekierowanie na odrzucony adres ({why})")
            company.seen.add(digest)
        return target

    @staticmethod
    def _bot_wall(res: FetchResult) -> bool:
        if res.status in (403, 429, 503):
            return detect_bot_wall(res.status, res.headers, res.body)
        if res.status == 200 and res.body and len(res.body) < 20_000:
            return detect_bot_wall(200, res.headers, res.body, _visible_len(res.body))
        return False
