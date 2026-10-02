"""Obróbka pobranej strony: klasyfikacja wyniku, ekstrakcja treści i kontaktów, nowe linki do kolejki."""

from __future__ import annotations

import hashlib
from typing import Optional
from urllib.parse import urlsplit

from perno.scraper import contacts as contacts_mod
from perno.scraper import extract
from perno.scraper import statuses as st
from perno.scraper.company import Company
from perno.scraper.fetcher import FetchResult, decode_html
from perno.scraper.run_context import RunContext
from perno.scraper.settings import MAX_DISCOVERED_PER_FIRM
from perno.scraper.store import LinkRecord, PageRecord, compress_html
from perno.scraper.urls import normalize_url, page_class, priority, url_hash

SHORT_TEXT_FOR_HASH = 200


class PagePipeline:
    def __init__(self, rc: RunContext):
        self.rc = rc

    @staticmethod
    def plain(item: dict, status: str, url: Optional[str] = None, **kwargs) -> PageRecord:
        target = url or item["url"]
        return PageRecord(url=target, url_hash=url_hash(target), depth=item["depth"], priority=item["priorytet"],
                          source=item["source"], status=status, **kwargs)

    @staticmethod
    def failure_reason(res: FetchResult) -> str:
        if res.error in ("timeout", "tls", "siec", "dns"):
            return res.error
        if res.status and res.status >= 500:
            return "http_5xx"
        if res.status in (401, 403, 406, 451):
            return "http_403"
        return "brak_strony_glownej"

    def classify(self, company: Company, item: dict, url: str, res: FetchResult) -> PageRecord:
        elapsed = int(res.elapsed * 1000)
        base = dict(http_status=res.status, content_type=res.content_type or None, elapsed_ms=elapsed)
        if res.error == "niehtml":
            return self.plain(item, st.STRONA_NIEHTML, url, bytes=0, **base)
        if res.error == "za_duze":
            return self.plain(item, st.STRONA_ZA_DUZE, url, bytes=0, error="strona przekracza limit rozmiaru", **base)
        if res.error:
            return self.plain(item, st.STRONA_BLAD, url, bytes=0, error=f"błąd pobierania: {res.error}", **base)
        if res.status is None:
            return self.plain(item, st.STRONA_BLAD, url, bytes=0, error="brak odpowiedzi", **base)
        if res.status in (401, 403, 406, 451):
            return self.plain(item, st.STRONA_ZABLOKOWANO, url, bytes=0, error=f"HTTP {res.status}", **base)
        if res.status >= 400 or not (200 <= res.status < 300):
            return self.plain(item, st.STRONA_BLAD, url, bytes=0, error=f"HTTP {res.status}", **base)
        if not res.body:
            return self.plain(item, st.STRONA_BLAD, url, bytes=0, error="pusta odpowiedź", **base)

        with self.rc.parse_slots:
            html_text, encoding = decode_html(res.body, res.headers.get("content-type", ""))
            data = extract.parse_html(html_text, url)
        if data.error:
            return self.plain(item, st.STRONA_BLAD, url, bytes=len(res.body), error=f"parsowanie: {data.error}",
                               encoding=encoding, **base)
        basis = data.text if len(data.text) >= SHORT_TEXT_FOR_HASH else html_text
        digest = hashlib.sha1(basis.encode("utf-8", "ignore"), usedforsecurity=False).hexdigest()
        common = dict(bytes=len(res.body), encoding=encoding, title=data.title or None,
                      description=data.description or None, content_hash=digest, **base)
        if digest in company.content:
            return self.plain(item, st.STRONA_DUPLIKAT, url, error=f"duplikat: {company.content[digest]}"[:500], **common)
        company.content[digest] = url

        follow = not extract.nofollow_requested(data.meta_robots, res.headers.get("x-robots-tag", ""))
        links = self.new_links(company, item, url, data, follow)
        found = contacts_mod.extract_contacts(
            data, url, company.host, [company.firma.get("domena_docelowa") or ""], international=company.international)
        keep_html = self.rc.settings.store_html == "all" or (
            self.rc.settings.store_html == "klucz" and (item["source"] == "start" or bool(page_class(urlsplit(url).path))))
        return self.plain(item, st.STRONA_POBRANO, url, text=data.text, links=links, contacts=found,
                           html_gz=compress_html(res.body) if keep_html else None, **common)

    def new_links(self, company: Company, item: dict, page_url: str, data, follow: bool) -> list:
        candidates = []
        if follow:
            candidates = [(link.href, link.where in ("nav", "footer", "header"), link.text)
                          for link in data.links if not link.nofollow]
        if data.canonical:
            candidates.append((data.canonical, False, ""))
        next_depth = item["depth"] + 1
        out = []
        for href, in_nav, text in candidates:
            if next_depth > self.rc.settings.depth:
                break
            url = normalize_url(href, base=page_url)
            if not url or not company.same_site(url):
                continue
            digest = url_hash(url)
            if digest in company.seen:
                continue
            if len(company.seen) >= MAX_DISCOVERED_PER_FIRM:
                break
            admitted, _ = company.trap.admit(url)
            if not admitted:
                continue
            company.seen.add(digest)
            out.append(LinkRecord(url, digest, next_depth,
                                  priority(url, next_depth, in_nav=in_nav, anchor_text=text, start_prefix=company.start_prefix)))
        return out
