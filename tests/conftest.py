"""Konfiguracja pytest: oznaczanie wolnych testów bez zależności od pytest w plikach testów.

Testy są napisane w `unittest` i mają działać też przez `python -m unittest discover -s tests -t .` (README),
dlatego marker `slow` nadajemy tutaj, a nie dekoratorem w testach. Wolne to testy trwające >2 s: prawdziwe
odliczanie limitów czasu, odłączone podprocesy skrapera i duże pliki eksportu. Szybka warstwa:
`pytest -m "not slow"` (albo `scripts/check.sh --fast`).
"""

from __future__ import annotations

import pytest

SLOW_PREFIXES = (
    "tests/test_scraper_cli.py::RealProcessTests",
    "tests/test_scraper_cli.py::InProcessRunTests",
    "tests/test_scraper_cli.py::RunnerHelperTests::test_spawn_detached_really_starts_a_process_that_the_cli_accepts",
    "tests/test_export.py::LargeTableTests",
    "tests/test_scraper_crawler.py::ConcurrencyTests",
    "tests/test_scraper_crawler.py::ResilienceTests::test_user_stop_requeues_the_firm_and_resume_continues_where_it_stopped",
    "tests/test_scraper_crawler.py::ResilienceTests::test_database_failure_ends_the_run_as_interrupted_and_firms_return_to_the_queue",
    "tests/test_scraper_crawler.py::RobotsAndSitemapTests::test_crawl_delay_is_honoured_between_requests",
    "tests/test_scraper_crawler.py::LimitTests::test_time_limit_per_firm",
    "tests/test_scraper_crawler.py::LimitTests::test_frontier_is_refilled_from_the_database_with_a_tiny_memory_cap",
)


def pytest_collection_modifyitems(items):
    for item in items:
        if item.nodeid.startswith(SLOW_PREFIXES):
            item.add_marker(pytest.mark.slow)
