"""Własny parser robots.txt zgodny z RFC 9309 (stdlib `robotparser` ma wady: `Allow` po
`Disallow` przegrywa, brak wildcardów, `Crawl-delay` tylko całkowity, User-Agent przez
podciąg) oraz proste wyciąganie adresów z sitemap (regex — bez xml.etree, bo expat 2.2.8 jest stary).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, unquote_to_bytes

from perno.scraper.settings import ROBOTS_MAX_BYTES, USER_AGENT_PRODUCT

PRODUCT_TOKEN = USER_AGENT_PRODUCT.split("/")[0]
MAX_SITEMAPS_IN_ROBOTS = 20
_SAFE = "/?!$&'()*+,;=:@-._~"

# statusy wyniku pobrania robots.txt
OK = "ok"                      # sparsowano (lub HTML → brak reguł)
BRAK = "brak"                  # 4xx → brak ograniczeń
ODMOWA = "odmowa"              # 401/403 → traktujemy jak odmowę (ostrożniej niż RFC)
BACKOFF = "backoff"            # 429 → odczekaj i ponów
NIEDOSTEPNY = "niedostepny"    # 5xx / sieć → ponów, potem błąd
PRZEKIEROWANIE = "przekierowanie"  # redirect na obcy host


def _norm(text: str) -> str:
    return quote(unquote_to_bytes(text), safe=_SAFE)


def _compile(pattern: str):
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.compile(regex + (r"\Z" if anchored else ""), re.S)


@dataclass
class RobotsRules:
    rules: list = field(default_factory=list)  # (allow: bool, długość wzorca, regex)
    crawl_delay: Optional[float] = None
    sitemaps: list = field(default_factory=list)
    has_group: bool = False

    def allowed(self, path_and_query: str) -> bool:
        target = _norm(path_and_query if path_and_query.startswith("/") else "/" + path_and_query)
        if target.split("?")[0] == "/robots.txt":
            return True
        best_len, best_allow = -1, True
        for allow, length, regex in self.rules:
            if regex.match(target) and (length > best_len or (length == best_len and allow)):
                best_len, best_allow = length, allow
        return best_allow


def _parse_delay(value: str) -> Optional[float]:
    try:
        delay = float(value.strip().replace(",", "."))
    except ValueError:
        return None
    return delay if delay >= 0 and delay == delay and delay != float("inf") else None


def parse_robots(text: str, token: str = PRODUCT_TOKEN) -> RobotsRules:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = text.lstrip("﻿")[:ROBOTS_MAX_BYTES]
    token = token.lower()

    groups: list = []  # {"agents": [...], "rules": [...], "delay": float|None}
    sitemaps: list = []
    current = None
    in_rules = False
    for raw in re.split(r"\r\n|\r|\n", text):
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        name, value = name.strip().lower(), value.strip()
        if name == "user-agent":
            if current is None or in_rules:
                current = {"agents": [], "rules": [], "delay": None}
                groups.append(current)
                in_rules = False
            current["agents"].append(value.split("/")[0].strip().lower())
        elif name in ("allow", "disallow"):
            if current is None:
                continue
            in_rules = True
            if value:
                pattern = _norm(value)
                current["rules"].append((name == "allow", len(pattern), _compile(pattern)))
        elif name == "crawl-delay":
            if current is None:
                continue
            in_rules = True
            current["delay"] = _parse_delay(value)
        elif name == "sitemap":
            if re.match(r"https?://", value, re.I) and len(sitemaps) < MAX_SITEMAPS_IN_ROBOTS:
                sitemaps.append(value)

    ours = [g for g in groups if token in g["agents"]]
    chosen = ours or [g for g in groups if "*" in g["agents"]]
    rules = [rule for g in chosen for rule in g["rules"]]
    delays = [g["delay"] for g in chosen if g["delay"] is not None]
    return RobotsRules(rules=rules, crawl_delay=max(delays) if delays else None,
                       sitemaps=sitemaps, has_group=bool(chosen))


@dataclass
class RobotsOutcome:
    status: str
    rules: RobotsRules = field(default_factory=RobotsRules)


def interpret(http_status: int, body: bytes = b"", content_type: str = "") -> RobotsOutcome:
    """Mapowanie odpowiedzi na wynik (po ewentualnym podążeniu za redirectami przez fetcher)."""
    if 200 <= http_status < 300:
        if content_type.lower().startswith(("text/html", "application/xhtml")):
            return RobotsOutcome(OK)  # to nie jest robots.txt (np. strona 404 zwracana jako 200) → brak reguł
        return RobotsOutcome(OK, parse_robots(body))
    if http_status in (401, 403):
        return RobotsOutcome(ODMOWA)
    if http_status == 429:
        return RobotsOutcome(BACKOFF)
    if 400 <= http_status < 500:
        return RobotsOutcome(BRAK)
    return RobotsOutcome(NIEDOSTEPNY)


# ---- sitemapy ----------------------------------------------------------

_LOC_RE = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.I | re.S)


def parse_sitemap(text: str, max_urls: int = 2000) -> tuple:
    """(adresy stron, adresy podrzędnych sitemap). Indeks sitemap rozpoznajemy po <sitemapindex>."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    locs = []
    for match in _LOC_RE.finditer(text):
        loc = html.unescape(match.group(1)).strip()
        if re.match(r"https?://", loc, re.I):
            locs.append(loc)
        if len(locs) >= max_urls:
            break
    if "<sitemapindex" in text[:2000].lower():
        return [], locs
    return locs, []
