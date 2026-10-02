#!/usr/bin/env python3
"""Benchmark skrapera na lokalnym „internecie" (bez prawdziwej sieci): ile stron na sekundę daje sam nasz potok
(pobranie, analiza, zapis do bazy) i gdzie ucieka czas.

    .venv/bin/python scripts/bench_scraper.py                       # 60 firm x 40 stron, opóźnienia 50-300 ms
    .venv/bin/python scripts/bench_scraper.py --firms 200 --pages 30 --threads 50

Uwaga: odstępy uprzejmości (podłoga 1 s, 2,5 s) są tu WYŁĄCZONE (`ScanSettings.for_tests`), bo mierzymy narzut
naszego kodu na lokalnym serwerze. Prawdziwy skan jest ograniczony odstępem na domenę, więc tempo w produkcji to
mniej więcej `równoległość / (odstęp + czas odpowiedzi)`; tu widać, czy nasz kod nie jest przy tym wąskim gardłem.
Serwer testowy działa w tym samym procesie, więc zużycie CPU zawiera także jego pracę.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
import time

import sqlalchemy as sa

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from perno.db import Database, make_engine
from perno.scraper.crawler import Crawler
from perno.scraper.fetcher import Fetcher
from perno.scraper.politeness import IpLimiter
from perno.scraper.settings import ScanSettings
from perno.scraper.store import Store
from tests.fake_sites import FakeInternet, Page, html, local_resolver


def build_site(rng: random.Random, pages: int, latency: tuple) -> dict:
    """Drzewo 10-krotne: strona startowa linkuje do p1..p10, każda pi do kolejnych dziesięciu; część linków prowadzi
    przez przekierowanie, część do błędu 404, a część podstron to duplikaty treści."""
    def delay() -> float:
        return rng.uniform(*latency)

    site = {}
    for i in range(1, pages):
        children = [f"/p{j}" for j in range(10 * i - 9, 10 * i + 1) if j < pages]
        links = ["/"] + [f"/stare/{j[2:]}" if rng.random() < 0.05 else j for j in children]
        if rng.random() < 0.02:
            links.append("/nie-ma-takiej")
        body = html(*links, title=f"Strona {i}", extra=f"<p>unikalny tekst {i} " + "słowo " * rng.randint(20, 200) + "</p>")
        if rng.random() < 0.03:
            body = html("/", title="Duplikat", extra="<p>ta sama treść</p>")
        site[f"/p{i}"] = Page(body, delay=delay(), encoding="gzip" if rng.random() < 0.5 else None)
        site[f"/stare/{i}"] = Page("", status=301, location=f"/p{i}", content_type=None)
    site["/"] = Page(html(*[f"/p{j}" for j in range(1, min(pages, 11))] + ["/kontakt"], title="Start") , delay=delay())
    site["/kontakt"] = Page(html("/", title="Kontakt", extra='<a href="mailto:biuro@firma.test">biuro@firma.test</a> 22 123 45 67'),
                            delay=delay())
    return site


def main() -> int:
    """Uruchamia benchmark i drukuje tempo oraz podział czasu na etapy."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--firms", type=int, default=60)
    parser.add_argument("--pages", type=int, default=40, help="stron na firmę (górna granica; budżet firmy też to ogranicza)")
    parser.add_argument("--threads", type=int, default=30, help="równoległość (liczba firm naraz)")
    parser.add_argument("--latency-min", type=float, default=0.05, help="najkrótsze opóźnienie odpowiedzi serwera (s)")
    parser.add_argument("--latency-max", type=float, default=0.3, help="najdłuższe opóźnienie odpowiedzi serwera (s)")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    internet = FakeInternet().start()
    workdir = tempfile.mkdtemp(prefix="perno-bench-")
    try:
        db = Database(make_engine(sa.engine.make_url(f"sqlite:///{os.path.join(workdir, 'bench.db')}")))
        store = Store(db)
        store.ensure_schema()
        for number in range(args.firms):
            host = f"s{number:04d}.test"
            internet.site(host, build_site(rng, args.pages, (args.latency_min, args.latency_max)))
            store.add_manual(internet.url(host, "/"), f"Firma {number}")
        settings = ScanSettings.for_tests(concurrency=args.threads, max_pages=args.pages, depth=6, ip_cap=args.threads,
                                          max_rps=50.0)
        store.enqueue()
        run_id = store.start_run({})
        fetcher = Fetcher(settings, resolver=local_resolver, allow_private=True)
        crawler = Crawler(store, settings, run_id, fetcher=fetcher, stagger=0, heartbeat_every=1.0,
                          ip_limiter=IpLimiter(cap=args.threads, min_spacing=0.0))
        cpu, wall = time.process_time(), time.monotonic()
        result = crawler.run()
        wall, cpu = time.monotonic() - wall, time.process_time() - cpu
        store.finish_run(run_id, result["status"], result["stats"])
        stats = result["stats"]

        print(f"Firm: {args.firms}, wątków: {args.threads}, opóźnienia serwera {args.latency_min}-{args.latency_max} s")
        print(f"Wynik: {result['status']}; czas {wall:.1f} s; CPU procesu {cpu:.1f} s (z serwerem testowym)")
        print(f"Strony: {stats['strony']} ({stats['strony'] / wall:.1f}/s), żądania: {stats['zapytania']} "
              f"({stats['zapytania'] / wall:.1f}/s, {stats['zapytania'] / max(1, stats['strony']):.2f} na stronę), "
              f"bajty: {stats['bajty'] / 1e6:.1f} MB, kontakty: {stats['kontakty']}")
        thread_time = wall * args.threads
        print("Czas wątków wg etapów (suma po wątkach; wątek bywa jednocześnie w kilku etapach tylko pozornie):")
        for stage, seconds in sorted(stats["czasy_s"].items(), key=lambda item: -item[1]):
            print(f"  {stage:<12} {seconds:>8.1f} s  {100 * seconds / thread_time:>5.1f}% czasu wątków")
        return 0
    finally:
        internet.stop()


if __name__ == "__main__":
    sys.exit(main())
