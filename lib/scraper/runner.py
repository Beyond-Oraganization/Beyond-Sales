"""Uruchamianie skrapera: log, sygnały, limit deskryptorów, odłączony start procesu, plan „na sucho".

Skraper działa w OSOBNYM procesie (`scrape.py run --run-id N`), odłączonym od serwera panelu: crawl przeżywa
restart serwera, a limity uprzejmości są liczone w jednym miejscu (dwa procesy po cichu podwoiłyby limity —
dlatego jednocześnie działa co najwyżej jeden przebieg, patrz `skan_przebiegi.aktywny`).
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime
from typing import Callable, Optional

from lib import config as cfg
from lib.db import DB_ERRORS
from lib.scraper import statuses as st
from lib.scraper.settings import HARD_DELAY_FLOOR, ScanSettings
from lib.scraper.store import Store

NOFILE_TARGET = 4096
ASSUMED_RTT = 0.7  # s — założenie do szacunku w --dry-run (mierzone w trakcie realnego przebiegu)
TYPICAL_PAGES_PER_FIRM = 60


def log_dir() -> str:
    """Katalog logów skrapera: `logs/` (testy ustawiają PERNO_LOG_DIR na katalog tymczasowy)."""
    return os.environ.get("PERNO_LOG_DIR") or cfg.LOG_DIR


def log_path_for(run_id: int) -> str:
    os.makedirs(log_dir(), exist_ok=True)
    return os.path.join(log_dir(), f"skraper_{run_id}_{datetime.now():%Y%m%d_%H%M%S}.log")


class RunLogger:
    """Bezpieczny wątkowo logger linia-po-linii (stdout i/lub plik)."""

    def __init__(self, path: Optional[str] = None, echo: bool = True):
        self.path = path
        self.echo = echo
        self._lock = threading.Lock()
        self._file = open(path, "a", encoding="utf-8", buffering=1) if path else None

    def __call__(self, message: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {message}"
        with self._lock:
            if self.echo:
                print(line, flush=True)
            if self._file:
                self._file.write(line + "\n")

    def close(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None


def raise_nofile_limit(target: int = NOFILE_TARGET) -> int:
    """Podnosi miękki limit otwartych plików (macOS domyślnie 256). Zwraca obowiązujący limit."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = target if hard == resource.RLIM_INFINITY else min(hard, target)
        if soft < wanted:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
            soft = wanted
        return soft
    except (ImportError, ValueError, OSError):
        return 0


def install_signal_handlers(stop_event: threading.Event, log: Callable) -> Callable:
    """Pierwszy SIGTERM/SIGINT = łagodne zatrzymanie (bieżące żądania dokończą się), drugi = natychmiast.
    Tylko w głównym wątku; zwraca funkcję przywracającą poprzednie handlery."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    state = {"count": 0}

    def handler(signum, frame):
        state["count"] += 1
        if state["count"] == 1:
            log("Sygnał zatrzymania — kończę bieżące żądania i zapisuję postęp (drugi sygnał przerwie natychmiast).")
            stop_event.set()
        else:
            log("Drugi sygnał — kończę natychmiast.")
            os._exit(130)

    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGTERM, signal.SIGINT)}

    def restore() -> None:
        for sig, old in previous.items():
            signal.signal(sig, old)

    return restore


def execute_run(store: Store, settings: ScanSettings, run_id: int, *, only_ids: Optional[list], log: Callable,
                log_path: Optional[str] = None, stop_event: Optional[threading.Event] = None,
                crawler_factory: Optional[Callable] = None, **crawler_kwargs) -> dict:
    """Dołącza proces do przebiegu, crawluje i zamyka przebieg. Zwraca wynik crawlera (status, powód, statystyki)."""
    if crawler_factory is None:
        from lib.scraper.crawler import Crawler  # leniwie: panel (spawn_detached/tail) nie ładuje crawlera ani parsera
        crawler_factory = Crawler
    store.attach_run(run_id, pid=os.getpid(), log_path=log_path)
    log(f"Przebieg #{run_id}: równoległość={settings.concurrency}, odstęp={settings.delay:g}s (min {settings.floor:g}s), "
        f"głębokość={settings.depth}, limit={settings.max_pages} stron / {settings.max_mb} MB / {settings.max_minutes} min na firmę")
    log(f"Tożsamość bota: {settings.user_agent()}")
    crawler = crawler_factory(store, settings, run_id, log=log, only_ids=only_ids, stop_event=stop_event, **crawler_kwargs)
    try:
        result = crawler.run()
    except BaseException as exc:  # noqa: BLE001 — przebieg musi zostać zamknięty nawet po wyjątku
        log(f"BŁĄD KRYTYCZNY: {type(exc).__name__}: {exc}")
        _finish(store, run_id, st.RUN_BLAD, {"blad": f"{type(exc).__name__}: {exc}"}, log)
        raise
    _finish(store, run_id, result["status"], result["stats"], log)
    stats = result["stats"]
    log(f"Koniec przebiegu #{run_id}: {result['status']} — strony={stats['strony']}, zapytania={stats['zapytania']}, "
        f"firmy={stats['firmy_wynik']}")
    return result


def _finish(store: Store, run_id: int, status: str, stats: dict, log: Callable) -> None:
    try:
        store.finish_run(run_id, status, stats)
    except DB_ERRORS as exc:
        log(f"Nie udało się zapisać zakończenia przebiegu ({type(exc).__name__}) — zostanie odzyskany przy następnym starcie.")


def spawn_detached(run_id: int, config_path: str) -> tuple:
    """Startuje `scrape.py run --run-id N` jako odłączony proces. Zwraca (pid, ścieżka logu).
    argv to lista (bez powłoki); stdout/stderr trafiają do pliku logu, który UI czyta."""
    path = log_path_for(run_id)
    # `--config` jest opcją globalną scrape.py, więc musi stać PRZED podpoleceniem `run`.
    argv = [sys.executable, "-u", os.path.join(cfg.BASE_DIR, "scrape.py"), "--config", config_path,
            "run", "--run-id", str(int(run_id))]
    with open(path, "ab") as logfile:
        proc = subprocess.Popen(argv, cwd=cfg.BASE_DIR, stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
    threading.Thread(target=proc.wait, name=f"reap-{proc.pid}", daemon=True).start()  # bez zombie
    return proc.pid, path


def tail(path: Optional[str], lines: int = 40, max_bytes: int = 64 * 1024) -> list:
    """Ostatnie linie logu. Ścieżka musi leżeć w katalogu logów (brak path traversal)."""
    if not path:
        return []
    real = os.path.realpath(path)
    base = os.path.realpath(log_dir())
    if os.path.commonpath([real, base]) != base or not os.path.isfile(real):
        return []
    with open(real, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        data = handle.read().decode("utf-8", errors="replace")
    return data.splitlines()[-lines:]


def plan_summary(settings: ScanSettings, firms: int) -> list:
    """Arytmetyka uprzejmości do `--dry-run` (bez sieci). To szacunek, nie obietnica."""
    base = max(settings.floor, settings.delay, HARD_DELAY_FLOOR if settings.floor else 0.0)
    cycle = base * 1.25 + ASSUMED_RTT  # jitter dodaje średnio +25%
    per_host_rate = 1.0 / cycle
    parallel = max(1, min(settings.concurrency, firms)) if firms else 0
    throughput = min(parallel * per_host_rate, settings.max_rps)
    firm_seconds = min(TYPICAL_PAGES_PER_FIRM * cycle, settings.max_minutes * 60.0)
    worst_seconds = settings.max_minutes * 60.0
    waves = math.ceil(firms / parallel) if parallel else 0
    return [
        f"Firm do przetworzenia: {firms}; równolegle: {parallel} (po jednej domenie na wątek).",
        f"Na jedną domenę: najwyżej 1 żądanie naraz, odstęp co najmniej {base:g} s + jitter (≈{cycle:.1f} s na cykl, "
        f"≈{per_host_rate:.2f} żądań/s na domenę).",
        f"Łącznie: ≈{throughput:.1f} żądań/s (bramka globalna {settings.max_rps:g}/s, limit na adres IP {settings.ip_cap}).",
        f"Limity na firmę: {settings.max_pages} stron, {settings.max_mb} MB, {settings.max_minutes} min, głębokość {settings.depth}.",
        f"Szacunek czasu: typowo ≈{waves * firm_seconds / 60:.0f} min (≈{TYPICAL_PAGES_PER_FIRM} stron na firmę), "
        f"najwyżej ≈{waves * worst_seconds / 60:.0f} min.",
        "robots.txt jest zawsze respektowany (w tym Crawl-delay); odmowa serwera (403/429/blokada dla botów) zatrzymuje firmę.",
    ]
