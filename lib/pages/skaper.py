"""Strona „Skraper": firmy do przeskanowania, sterowanie przebiegiem, podgląd kontaktów i stron.

Importuje wyłącznie `store`/`settings`/`runner` — nigdy `bs4` (brak pakietu nie może zablokować panelu wysyłki;
sprawdzamy go przez `importlib.util.find_spec`). Samo skanowanie działa w osobnym, odłączonym procesie.
"""

from __future__ import annotations

import functools
import importlib.util
import threading

from lib import layout
from lib.app import App, HttpError, Request, Response, json_response
from lib.pages.base import Page
from lib.scraper import runner
from lib.scraper import statuses as st
from lib.scraper.settings import HARD_DELAY_FLOOR, LIMITS, STALE_AFTER, ScanSettings, SettingsError
from lib.scraper.store import DomainTaken, FirmaBusy, InvalidFirma, NotFound, RunActive, Store

MAX_START_IDS = 500


def bs4_available() -> bool:
    """Bez importu `bs4` (i bez ładowania modułu extract): brak pakietu nie może zablokować panelu."""
    return importlib.util.find_spec("bs4") is not None

_STORE_LOCK = threading.Lock()


def get_store(app: App) -> Store:
    """Jeden `Store` na aplikację (jego blokada serializuje sqlite); schemat zakładamy przy pierwszym użyciu."""
    store = getattr(app, "skraper_store", None)
    if store is None:
        with _STORE_LOCK:
            store = getattr(app, "skraper_store", None)
            if store is None:
                store = Store(app.db)
                store.ensure_schema()
                app.skraper_store = store
    return store


def prepare(app: App, log=print) -> list:
    """Wywoływane przy starcie serwera: schemat + odzyskanie przerwanych przebiegów. Zwraca id odzyskanych.
    Awaria skrapera nie może zablokować panelu wysyłki — błędy trafiają tylko do logu."""
    try:
        recovered = get_store(app).recover_dead_runs()
    except Exception as exc:  # noqa: BLE001
        log(f"UWAGA: skraper niedostępny ({type(exc).__name__}: {exc}). Panel wysyłki działa normalnie.")
        return []
    if recovered:
        log(f"Skraper: odzyskano przerwane przebiegi {', '.join(map(str, recovered))} (firmy wróciły do kolejki).")
    return recovered


def shutdown_hint(app: App):
    """Skraper jest odłączony od serwera — przy zamykaniu przypominamy, że może nadal działać."""
    try:
        run = get_store(app).active_run()
    except Exception:  # noqa: BLE001
        return None
    if run is None:
        return None
    return (f"Skraper nadal działa (przebieg #{run['id']}). Zatrzymaj go w zakładce Skraper albo poleceniem: "
            "python3 scrape.py stop")


def guarded(handler):
    """Tłumaczy błędy magazynu na kody HTTP (bez zmian w lib/app.py)."""
    @functools.wraps(handler)
    def wrapper(req: Request, app: App) -> Response:
        try:
            return handler(req, app)
        except (RunActive, FirmaBusy, DomainTaken) as exc:
            raise HttpError(409, str(exc)) from None
        except NotFound as exc:
            raise HttpError(404, str(exc)) from None
        except (InvalidFirma, SettingsError) as exc:
            raise HttpError(400, str(exc)) from None
    return wrapper


def render(app: App) -> str:
    return layout.render_page(
        app, title="Skraper", active_key="skaper",
        body=layout.read_asset("skaper.html"),
        css=layout.read_asset("skaper.css"), js=layout.read_asset("skaper.js"),
    )


def _int(value, name: str, *, minimum: int = 0, maximum: int = 10_000_000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HttpError(400, f"Parametr {name} musi być liczbą całkowitą.") from None
    if not minimum <= number <= maximum:
        raise HttpError(400, f"Parametr {name} poza zakresem ({minimum}–{maximum}).")
    return number


def _settings(app: App, overrides) -> ScanSettings:
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict) or any(isinstance(v, (dict, list)) for v in overrides.values()):
        raise HttpError(400, "Pole ustawienia musi być płaskim obiektem.")
    settings = ScanSettings.from_params(overrides, app.load_config())
    if not settings.contact:
        raise HttpError(400, "Ustaw SKRAPER_CONTACT w config.env (adres e-mail lub URL) — strony muszą wiedzieć, kto je odwiedza.")
    return settings


def api_meta(req: Request, app: App) -> Response:
    store = get_store(app)
    overview = store.status_overview()
    config = app.load_config()
    try:
        defaults = ScanSettings.from_params({}, config)
        settings_error = ""
    except SettingsError as exc:
        defaults, settings_error = ScanSettings(), str(exc)
    return json_response({
        "statuses": [{"code": code, "label": label, "color": color, "count": overview["firmy_wg_statusu"].get(code, 0)}
                     for code, (label, color) in st.FIRMA_META.items()],
        "defaults": {k: v for k, v in defaults.to_dict().items()
                     if k in ("concurrency", "delay", "depth", "max_pages", "max_mb", "max_minutes")},
        "limits": {k: list(v) for k, v in LIMITS.items()},
        "floor": HARD_DELAY_FLOOR,
        "contact": {"ok": bool(defaults.contact), "value": defaults.contact, "source": defaults.contact_source},
        "settings_error": settings_error,
        "bs4_ok": bs4_available(),
        "maile_count": app.db.count_rows(),
        "firmy_count": overview["firmy"],
        "default_statuses": list(st.FIRMA_DOMYSLNIE_DO_SKANU),
        "retryable": list(st.FIRMA_PONAWIALNE),
        "reasons": st.PRZYCZYNY,
    })


@guarded
def api_seed(req: Request, app: App) -> Response:
    return json_response(get_store(app).seed_from_maile())


@guarded
def api_dodaj(req: Request, app: App) -> Response:
    url = req.json.get("url")
    name = req.json.get("nazwa")
    if not isinstance(url, str) or not url.strip():
        raise HttpError(400, "Podaj adres strony firmy.")
    if name is not None and not isinstance(name, str):
        raise HttpError(400, "Pole nazwa musi być tekstem.")
    return json_response(get_store(app).add_manual(url, name))


@guarded
def api_firmy(req: Request, app: App) -> Response:
    status = req.arg("status")
    if status and status not in st.FIRMA_STATUSY:
        raise HttpError(400, f"Nieznany status: {status}.")
    rows, total = get_store(app).list_firmy(
        status=status or None, q=req.arg("q")[:100], offset=_int(req.arg("offset", "0"), "offset"),
        limit=_int(req.arg("limit", "50"), "limit", minimum=1, maximum=500))
    return json_response({"rows": rows, "total": total})


@guarded
def api_firma(req: Request, app: App) -> Response:
    detail = get_store(app).firma_detail(
        _int(req.arg("id"), "id", minimum=1), page=_int(req.arg("page", "0"), "page"), per_page=100)
    if detail is None:
        raise HttpError(404, "Nie znaleziono firmy.")
    return json_response(detail)


@guarded
def api_status(req: Request, app: App) -> Response:
    store = get_store(app)
    run = store.active_run()
    active = run is not None
    run = run or store.latest_run()
    heartbeat_age = None
    if run and run.get("heartbeat_at"):
        from datetime import datetime
        try:
            heartbeat = datetime.strptime(run["heartbeat_at"], "%Y-%m-%dT%H:%M:%S")
            heartbeat_age = max(0.0, (app.db.now() - heartbeat).total_seconds())
        except ValueError:
            pass
    return json_response({
        "aktywny": active, "run": run, "heartbeat_age": heartbeat_age,
        "nieodpowiada": bool(active and heartbeat_age is not None and heartbeat_age > STALE_AFTER),
        "przeglad": store.status_overview(), "log": runner.tail(run.get("log_path")) if run else [],
    })


def _id_list(raw, name: str) -> list:
    if raw is None:
        return None
    if not isinstance(raw, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in raw):
        raise HttpError(400, f"Pole {name} musi być listą liczb całkowitych.")
    if len(raw) > MAX_START_IDS:
        raise HttpError(400, f"Za dużo firm naraz (maksymalnie {MAX_START_IDS}).")
    return sorted(set(raw))


@guarded
def api_start(req: Request, app: App) -> Response:
    store = get_store(app)
    store.recover_dead_runs()
    if not bs4_available():
        raise HttpError(400, "Brak pakietu beautifulsoup4. Uruchom: python3 -m pip install --user -r requirements.txt")
    settings = _settings(app, req.json.get("ustawienia"))
    ids = _id_list(req.json.get("firma_ids"), "firma_ids")
    if ids is not None and len(store.existing_firma_ids(ids)) != len(ids):
        raise HttpError(400, "Niektóre z wybranych firm nie istnieją.")
    statuses = req.json.get("statuses")
    if statuses is None:
        statuses = list(st.FIRMA_DOMYSLNIE_DO_SKANU)
    if not isinstance(statuses, list) or not statuses or any(s not in st.FIRMA_STATUSY for s in statuses):
        raise HttpError(400, "Pole statuses musi być niepustą listą znanych statusów.")

    run_id = store.start_run({"ustawienia": settings.to_dict(), "firma_ids": ids, "statuses": statuses})
    try:
        queued = store.enqueue(ids, None if ids is not None else tuple(statuses))
        if queued == 0:
            store.finish_run(run_id, st.RUN_ZAKONCZONY, {"uwaga": "brak firm do przetworzenia"})
            raise HttpError(400, "Brak firm do przetworzenia — zaimportuj firmy z bazy maili, dodaj adres albo użyj „Ponów”.")
        pid, log_path = runner.spawn_detached(run_id, app.config_path)
    except HttpError:
        raise
    except Exception:
        store.finish_run(run_id, st.RUN_BLAD, {"blad": "nie udało się uruchomić procesu skrapera"})
        raise
    store.attach_run(run_id, pid=pid, log_path=log_path, status=st.RUN_URUCHAMIANIE)
    return json_response({"run_id": run_id, "queued": queued}, 202)


@guarded
def api_stop(req: Request, app: App) -> Response:
    store = get_store(app)
    recovered = store.recover_dead_runs()
    return json_response({"ok": store.request_stop(), "odzyskano": recovered})


@guarded
def api_ponow(req: Request, app: App) -> Response:
    ids = _id_list(req.json.get("firma_ids"), "firma_ids")
    if not ids:
        raise HttpError(400, "Wybierz firmy do ponowienia.")
    return json_response({"zmieniono": get_store(app).requeue(ids)})


@guarded
def api_uzyj_domeny(req: Request, app: App) -> Response:
    return json_response(get_store(app).use_target_domain(_int(req.json.get("firma_id"), "firma_id", minimum=1)))


@guarded
def api_usun_dane(req: Request, app: App) -> Response:
    get_store(app).delete_firma_data(_int(req.json.get("firma_id"), "firma_id", minimum=1))
    return json_response({"ok": True})


PAGE = Page(
    key="skaper", label="Skraper", path="/skraper", render=render, order=30,
    routes={
        ("GET", "/api/skaper/meta"): api_meta,
        ("POST", "/api/skaper/seed"): api_seed,
        ("POST", "/api/skaper/dodaj"): api_dodaj,
        ("GET", "/api/skaper/firmy"): api_firmy,
        ("GET", "/api/skaper/firma"): api_firma,
        ("GET", "/api/skaper/status"): api_status,
        ("POST", "/api/skaper/start"): api_start,
        ("POST", "/api/skaper/stop"): api_stop,
        ("POST", "/api/skaper/ponow"): api_ponow,
        ("POST", "/api/skaper/uzyj-domeny"): api_uzyj_domeny,
        ("POST", "/api/skaper/usun-dane"): api_usun_dane,
    },
)
