"""Sterowanie skanowaniem: start przebiegu (kolejka + odłączony proces), raport stanu i odzyskiwanie po awarii.

Wspólne dla zakładki „Skraper" i terminala. Nigdy nie importuje `bs4` ani crawlera (brak pakietu nie może
zablokować panelu wysyłki); samo skanowanie działa w osobnym, odłączonym procesie (`perno.scraper.runner`).
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Optional, Sequence

from perno.scraper import runner
from perno.scraper import statuses as st
from perno.scraper.settings import HARD_DELAY_FLOOR, LIMITS, STALE_AFTER, ScanSettings, SettingsError
from perno.scraper.store import Store

_RUN_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
_DEFAULT_FIELDS = ("concurrency", "delay", "depth", "max_pages", "max_mb", "max_minutes")


class ScanRequestError(Exception):
    """Żądanie startu skanowania nie może zostać wykonane (komunikat jest dla użytkownika)."""


class ParserMissing(ScanRequestError):
    """Brak pakietu parsera HTML (beautifulsoup4), bez którego skraper nie wystartuje."""
    pass


class UnknownFirms(ScanRequestError):
    """Wskazano firmy, których nie ma w bazie skrapera."""
    pass


class NothingToScan(ScanRequestError):
    """Żadna firma nie kwalifikuje się do skanu przy tych ustawieniach."""
    pass


@dataclass(frozen=True)
class StartedScan:
    """Uruchomiony przebieg: jego id i liczba firm w kolejce."""
    run_id: int
    queued: int


def parser_available() -> bool:
    """Bez importu `bs4` (i bez ładowania modułu extract): brak pakietu nie może zablokować panelu."""
    return importlib.util.find_spec("bs4") is not None


def ensure_parser_available() -> None:
    """Rzuca `ParserMissing`, gdy brakuje beautifulsoup4 (sprawdzane bez importu pakietu)."""
    if not parser_available():
        raise ParserMissing("Brak pakietu beautifulsoup4. Uruchom: uv sync")


def settings_for_run(config: Mapping, overrides: object) -> ScanSettings:
    """Ustawienia przebiegu: wartości z żądania (płaski obiekt) > config.env > domyślne; wymaga kontaktu do User-Agent."""
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict) or any(isinstance(v, (dict, list)) for v in overrides.values()):
        raise SettingsError("Pole ustawienia musi być płaskim obiektem.")
    settings = ScanSettings.from_params(overrides, config)
    if not settings.contact:
        raise SettingsError("Ustaw SKRAPER_CONTACT w config.env (adres e-mail lub URL) — strony muszą wiedzieć, kto je odwiedza.")
    return settings


def start_scan(store: Store, settings: ScanSettings, *, firma_ids: Optional[list], statuses: Sequence[str],
               config_path: str, spawn: Optional[Callable] = None) -> StartedScan:
    """Zakłada przebieg, kolejkuje firmy i uruchamia odłączony proces. Gdy coś pójdzie nie tak po założeniu
    przebiegu, zamyka go ze statusem błędu, żeby nie blokował następnego startu."""
    if firma_ids is not None and len(store.existing_firma_ids(firma_ids)) != len(firma_ids):
        raise UnknownFirms("Niektóre z wybranych firm nie istnieją.")
    run_id = store.start_run({"ustawienia": settings.to_dict(), "firma_ids": firma_ids, "statuses": list(statuses)})
    try:
        queued = store.enqueue(firma_ids, None if firma_ids is not None else tuple(statuses))
        if queued == 0:
            store.finish_run(run_id, st.RUN_ZAKONCZONY, {"uwaga": "brak firm do przetworzenia"})
            raise NothingToScan("Brak firm do przetworzenia — zaimportuj firmy z bazy maili, dodaj adres albo użyj „Ponów”.")
        pid, log_path = (spawn or runner.spawn_detached)(run_id, config_path)
    except ScanRequestError:
        raise
    except Exception:
        store.finish_run(run_id, st.RUN_BLAD, {"blad": "nie udało się uruchomić procesu skrapera"})
        raise
    store.attach_run(run_id, pid=pid, log_path=log_path, status=st.RUN_URUCHAMIANIE)
    return StartedScan(run_id, queued)


def heartbeat_age(run: Optional[Mapping], now: datetime) -> Optional[float]:
    """Ile sekund temu przebieg dał znak życia (None, gdy nie ma pulsu albo ma nieczytelną datę)."""
    if not run or not run.get("heartbeat_at"):
        return None
    try:
        heartbeat = datetime.strptime(run["heartbeat_at"], _RUN_TIME_FORMAT)
    except ValueError:
        return None
    return max(0.0, (now - heartbeat).total_seconds())


def status_report(store: Store, now: datetime, *, tail: Optional[Callable] = None) -> dict:
    """Stan skanowania dla panelu: aktywny (albo ostatni) przebieg, wiek pulsu, przegląd firm i końcówka logu."""
    run = store.active_run()
    active = run is not None
    run = run or store.latest_run()
    age = heartbeat_age(run, now)
    return {
        "aktywny": active, "run": run, "heartbeat_age": age,
        "nieodpowiada": bool(active and age is not None and age > STALE_AFTER),
        "przeglad": store.status_overview(), "log": (tail or runner.tail)(run.get("log_path")) if run else [],
    }


def meta_report(store: Store, config: Mapping, maile_count: int) -> dict:
    """Dane startowe zakładki: statusy z licznikami, domyślne ustawienia, limity i stan konfiguracji."""
    overview = store.status_overview()
    try:
        defaults = ScanSettings.from_params({}, config)
        settings_error = ""
    except SettingsError as exc:
        defaults, settings_error = ScanSettings(), str(exc)
    return {
        "statuses": [{"code": code, "label": label, "color": color, "count": overview["firmy_wg_statusu"].get(code, 0)}
                     for code, (label, color) in st.FIRMA_META.items()],
        "defaults": {k: v for k, v in defaults.to_dict().items() if k in _DEFAULT_FIELDS},
        "limits": {k: list(v) for k, v in LIMITS.items()},
        "floor": HARD_DELAY_FLOOR,
        "contact": {"ok": bool(defaults.contact), "value": defaults.contact, "source": defaults.contact_source},
        "settings_error": settings_error,
        "bs4_ok": parser_available(),
        "maile_count": maile_count,
        "firmy_count": overview["firmy"],
        "default_statuses": list(st.FIRMA_DOMYSLNIE_DO_SKANU),
        "retryable": list(st.FIRMA_PONAWIALNE),
        "reasons": st.PRZYCZYNY,
    }


def recover_on_start(open_store: Callable[[], Store], log: Callable = print) -> list:
    """Przy starcie serwera: schemat skrapera + odzyskanie przerwanych przebiegów. Zwraca id odzyskanych.
    Awaria skrapera nie może zablokować panelu wysyłki — błędy trafiają tylko do logu."""
    try:
        recovered = open_store().recover_dead_runs()
    except Exception as exc:  # noqa: BLE001 — granica startu: skraper jest dodatkiem, panel wysyłki ma działać bez niego
        log(f"UWAGA: skraper niedostępny ({type(exc).__name__}: {exc}). Panel wysyłki działa normalnie.")
        return []
    if recovered:
        log(f"Skraper: odzyskano przerwane przebiegi {', '.join(map(str, recovered))} (firmy wróciły do kolejki).")
    return recovered


def shutdown_hint(open_store: Callable[[], Store]) -> Optional[str]:
    """Skraper jest odłączony od serwera — przy zamykaniu przypominamy, że może nadal działać."""
    try:
        run = open_store().active_run()
    except Exception:  # noqa: BLE001 — podpowiedź przy zamykaniu nie może przeszkodzić w zamknięciu
        return None
    if run is None:
        return None
    return (f"Skraper nadal działa (przebieg #{run['id']}). Zatrzymaj go w zakładce Skraper albo poleceniem: "
            "python3 scrape.py stop")
