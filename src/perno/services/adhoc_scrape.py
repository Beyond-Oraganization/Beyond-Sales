"""Skan „bez bazy": zeskrapuj podane strony do plików (`scrape_dataset`), tym samym uprzejmym crawlerem co skan firm.

Nic nie jest zapisywane w bazie ani w kolejce skrapera. Crawler dostaje `MemoryStore`, a wyniki każdej witryny trafiają
do katalogu zbioru zaraz po jej zakończeniu (przerwanie skanu nie traci ukończonych witryn). Uprzejmość (odstęp ≥ 1 s,
robots.txt, limity na IP, zatrzymanie po odmowie serwera) jest dokładnie ta sama co przy skanie firm."""

from __future__ import annotations

import csv
import math
import os
import random
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional, Sequence, cast

from perno.scraper import runner
from perno.scraper import statuses as st
from perno.scraper.crawler import Crawler
from perno.scraper.fetcher import Fetcher
from perno.scraper.memory_store import MemoryStore
from perno.scraper.settings import ScanSettings
from perno.scraper.store import Store
from perno.scraper.urls import site_key, validate_start_url
from perno.services import scan_control
from perno.services.scrape_dataset import Dataset, DatasetError

MAX_SITES = 200
DEFAULT_PROFILE = "kontekst"
DEFAULT_MAX_PAGES = 25
DEFAULT_CONCURRENCY = 8
URL_COLUMNS = ("website", "url", "adres", "strona")
SECONDS_PER_PAGE = 4  # zapas na odstęp 2,5 s + jitter + czas odpowiedzi, do wyliczenia limitu czasu na witrynę


@dataclass(frozen=True)
class ScrapeOptions:
    """Parametry skanu z linii poleceń: nazwa zbioru, limity na witrynę, równoległość, nadpisanie."""
    name: str
    max_pages: int = DEFAULT_MAX_PAGES
    depth: Optional[int] = None
    concurrency: int = DEFAULT_CONCURRENCY
    overwrite: bool = False


@dataclass
class Targets:
    """Adresy startowe (po jednym na domenę) i odrzucone wpisy z powodem."""
    sites: list = field(default_factory=list)  # (domena, adres)
    rejected: list = field(default_factory=list)  # (wpis, powód)


@dataclass
class ScrapeReport:
    """Wynik skanu: wiersze witryny.csv, czas, odrzucone adresy i błędy zapisu."""
    name: str
    path: str
    rows: list  # wiersze witryny.csv (słowniki)
    interrupted: bool
    elapsed: float
    rejected: list
    errors: list

    def count(self, *statuses: str) -> int:
        return sum(1 for row in self.rows if row["status"] in statuses)


def read_targets(urls: Sequence[str], file: Optional[str] = None) -> Targets:
    """Adresy z argumentów i z pliku (`.csv` z kolumną website/url/adres/strona albo tekst: jeden adres w linii, `#` to komentarz).
    Z jednej domeny bierzemy pierwszy adres - reszty serwisu crawler dojdzie sam."""
    raw = list(urls)
    if file:
        raw.extend(_read_file_entries(file))
    targets = Targets()
    seen: set = set()
    for entry in raw:
        url, reason = validate_start_url(entry)
        domain = site_key(url) if url else None
        if not url or not domain:
            targets.rejected.append((entry, reason or "zly_url"))
        elif domain in seen:
            targets.rejected.append((entry, "duplikat domeny"))
        else:
            seen.add(domain)
            targets.sites.append((domain, url))
    if not targets.sites:
        raise DatasetError("Brak poprawnych adresów do zeskrapowania" + (f" (odrzucone: {targets.rejected[:3]})" if targets.rejected else "."))
    if len(targets.sites) > MAX_SITES:
        raise DatasetError(f"Za dużo domen naraz: {len(targets.sites)} (limit {MAX_SITES}). Podziel listę na części.")
    return targets


def _read_file_entries(path: str) -> list:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        if path.lower().endswith(".csv"):
            reader = csv.DictReader(handle)
            column = next((name for name in (reader.fieldnames or []) if name.strip().lower() in URL_COLUMNS), None)
            if column is None:
                raise DatasetError(f"Plik CSV musi mieć kolumnę {'/'.join(URL_COLUMNS)} (jest: {reader.fieldnames}).")
            return [(row[column] or "").strip() for row in reader if (row[column] or "").strip()]
        return [line.strip() for line in handle if line.strip() and not line.lstrip().startswith("#")]


def build_settings(config: Mapping, options: ScrapeOptions) -> ScanSettings:
    """Ustawienia skanu: profil „kontekst" z podanym limitem stron; czas na witrynę rośnie razem z liczbą stron."""
    params: dict = {"profile": DEFAULT_PROFILE, "max_pages": options.max_pages, "store_html": "nie",
                    "concurrency": options.concurrency,
                    "max_minutes": max(10, math.ceil(options.max_pages * SECONDS_PER_PAGE / 60) + 1)}
    if options.depth is not None:
        params["depth"] = options.depth
    return scan_control.settings_for_run(config, params)


@dataclass
class Seams:
    """Szwy testów: podmienione ustawienia, fetcher, argumenty crawlera i sygnał stopu."""
    settings: Optional[ScanSettings] = None
    fetcher: Optional[Fetcher] = None
    crawler_kwargs: dict = field(default_factory=dict)
    stop_event: Optional[threading.Event] = None


def run_scrape(targets: Targets, config: Mapping, options: ScrapeOptions, *, root: Optional[str] = None,
               seams: Optional[Seams] = None) -> ScrapeReport:
    """Skanuje wszystkie witryny i zapisuje zbiór (bez bazy)."""
    seams = seams or Seams()
    scan_control.ensure_parser_available()
    settings = seams.settings or build_settings(config, options)
    dataset = Dataset(options.name, root)
    dataset.create({"adresy": [domain for domain, _ in targets.sites], "ustawienia": _public_settings(settings),
                    "domen": len(targets.sites)}, overwrite=options.overwrite)
    firms = [{"id": index, "domena": domain, "website": url, "origin": _origin(url), "stron_pobranych": 0,
              "bajty_pobranych": 0, "robots_status": None, "robots_at": None, "robots_txt": None, "crawl_delay": None,
              "domena_docelowa": None} for index, (domain, url) in enumerate(targets.sites, start=1)]
    store = MemoryStore(firms, dataset.append_site)
    stop = seams.stop_event or threading.Event()
    logger = runner.RunLogger(os.path.join(dataset.dir, "skraper.log"), echo=False)
    restore = runner.install_signal_handlers(stop, logger)
    started = datetime.now()
    try:
        crawler = Crawler(cast(Store, store), settings, 1, fetcher=seams.fetcher, log=logger, stop_event=stop,
                          rng=random.Random(), **seams.crawler_kwargs)
        outcome = crawler.run()
    finally:
        restore()
        logger.close()
    elapsed = (datetime.now() - started).total_seconds()
    dataset.finish(zakonczono=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), czas_s=round(elapsed),
                   przerwany=outcome["status"] != st.RUN_ZAKONCZONY)
    return ScrapeReport(options.name, dataset.dir, dataset.sites(), outcome["status"] != st.RUN_ZAKONCZONY, elapsed,
                        targets.rejected, store.sink_errors)


def _origin(url: str) -> str:
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{rest.split('/', 1)[0]}"


def _public_settings(settings: ScanSettings) -> dict:
    keys = ("profile", "concurrency", "delay", "depth", "max_pages", "max_mb", "max_minutes")
    return {key: getattr(settings, key) for key in keys}


def report_lines(report: ScrapeReport) -> list:
    """Krótki raport dla użytkownika (kilka linii, niezależnie od liczby witryn)."""
    total = len(report.rows)
    pages = sum(int(row["stron"] or 0) for row in report.rows)
    done = report.count(st.FIRMA_ZAKONCZONA, st.FIRMA_LIMIT)
    contacts = {label: sum(1 for row in report.rows if row[column]) for label, column in
                (("e-mail", "emaile"), ("telefon", "telefony"), ("social", "social"), ("formularz", "formularz"))}
    lines = [f"{'PRZERWANO' if report.interrupted else 'ZESKRAPOWANO'} zbiór „{report.name}”: witryn {done}/{total} ok, "
             f"stron {pages}, czas {report.elapsed:.0f} s",
             f"  katalog: {report.path}",
             "  kontakty: " + ", ".join(f"{label} {count} witryn" for label, count in contacts.items())]
    problems = [row for row in report.rows if row["status"] not in (st.FIRMA_ZAKONCZONA, st.FIRMA_LIMIT)]
    if problems:
        lines.append(f"  problemy ({len(problems)}): " + "; ".join(_problem(row) for row in problems[:12])
                     + ("; …" if len(problems) > 12 else ""))
    if report.rejected:
        lines.append(f"  odrzucone adresy ({len(report.rejected)}): " + "; ".join(f"{e} ({why})" for e, why in report.rejected[:8]))
    lines.extend(f"  BŁĄD zapisu: {error}" for error in report.errors)
    return lines


def _problem(row: dict) -> str:
    detail = row["powod"] or row["uwaga"]
    return f"{row['domena']} ({row['status']}{': ' + detail if detail else ''})"
