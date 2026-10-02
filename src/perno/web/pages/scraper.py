"""Strona „Skraper": firmy do przeskanowania, sterowanie przebiegiem, podgląd kontaktów i stron.

Handlery tylko czytają żądanie i wołają `ctx.scan_store` oraz `services.scan_control`; nigdy nie importują `bs4`
(brak pakietu nie może zablokować panelu wysyłki). Samo skanowanie działa w osobnym, odłączonym procesie.
"""

from __future__ import annotations

from typing import Optional

from perno.scraper import statuses as st
from perno.services import scan_control
from perno.web import layout
from perno.web.context import AppContext
from perno.web.http import HttpError, Request, Response, json_response
from perno.web.page import Page

MAX_START_IDS = 500


def render(ctx: AppContext) -> str:
    return layout.render_page(
        ctx, title="Skraper", active_key="skaper",
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


def api_meta(req: Request, ctx: AppContext) -> Response:
    return json_response(scan_control.meta_report(ctx.scan_store.get(), ctx.config.load(), ctx.db.count_rows()))


def api_seed(req: Request, ctx: AppContext) -> Response:
    return json_response(ctx.scan_store.get().seed_from_maile())


def api_add_company(req: Request, ctx: AppContext) -> Response:
    url = req.json.get("url")
    name = req.json.get("nazwa")
    if not isinstance(url, str) or not url.strip():
        raise HttpError(400, "Podaj adres strony firmy.")
    if name is not None and not isinstance(name, str):
        raise HttpError(400, "Pole nazwa musi być tekstem.")
    return json_response(ctx.scan_store.get().add_manual(url, name))


def api_companies(req: Request, ctx: AppContext) -> Response:
    status = req.arg("status")
    if status and status not in st.FIRMA_STATUSY:
        raise HttpError(400, f"Nieznany status: {status}.")
    rows, total = ctx.scan_store.get().list_firmy(
        status=status or None, q=req.arg("q")[:100], offset=_int(req.arg("offset", "0"), "offset"),
        limit=_int(req.arg("limit", "50"), "limit", minimum=1, maximum=500))
    return json_response({"rows": rows, "total": total})


def api_company(req: Request, ctx: AppContext) -> Response:
    detail = ctx.scan_store.get().firma_detail(
        _int(req.arg("id"), "id", minimum=1), page=_int(req.arg("page", "0"), "page"), per_page=100)
    if detail is None:
        raise HttpError(404, "Nie znaleziono firmy.")
    return json_response(detail)


def api_status(req: Request, ctx: AppContext) -> Response:
    return json_response(scan_control.status_report(ctx.scan_store.get(), ctx.db.now()))


def _id_list(raw, name: str) -> Optional[list]:
    if raw is None:
        return None
    if not isinstance(raw, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in raw):
        raise HttpError(400, f"Pole {name} musi być listą liczb całkowitych.")
    if len(raw) > MAX_START_IDS:
        raise HttpError(400, f"Za dużo firm naraz (maksymalnie {MAX_START_IDS}).")
    return sorted(set(raw))


def _statuses(raw) -> list:
    """Statusy firm do przeskanowania z żądania (brak pola = domyślne)."""
    if raw is None:
        return list(st.FIRMA_DOMYSLNIE_DO_SKANU)
    if not isinstance(raw, list) or not raw or any(s not in st.FIRMA_STATUSY for s in raw):
        raise HttpError(400, "Pole statuses musi być niepustą listą znanych statusów.")
    return raw


def api_start(req: Request, ctx: AppContext) -> Response:
    store = ctx.scan_store.get()
    store.recover_dead_runs()
    scan_control.ensure_parser_available()
    settings = scan_control.settings_for_run(ctx.config.load(), req.json.get("ustawienia"))
    ids = _id_list(req.json.get("firma_ids"), "firma_ids")
    statuses = _statuses(req.json.get("statuses"))
    started = scan_control.start_scan(store, settings, firma_ids=ids, statuses=statuses, config_path=ctx.config.path)
    return json_response({"run_id": started.run_id, "queued": started.queued}, 202)


def api_stop(req: Request, ctx: AppContext) -> Response:
    store = ctx.scan_store.get()
    recovered = store.recover_dead_runs()
    return json_response({"ok": store.request_stop(), "odzyskano": recovered})


def api_requeue(req: Request, ctx: AppContext) -> Response:
    ids = _id_list(req.json.get("firma_ids"), "firma_ids")
    if not ids:
        raise HttpError(400, "Wybierz firmy do ponowienia.")
    return json_response({"zmieniono": ctx.scan_store.get().requeue(ids)})


def api_use_target_domain(req: Request, ctx: AppContext) -> Response:
    return json_response(ctx.scan_store.get().use_target_domain(_int(req.json.get("firma_id"), "firma_id", minimum=1)))


def api_delete_data(req: Request, ctx: AppContext) -> Response:
    ctx.scan_store.get().delete_firma_data(_int(req.json.get("firma_id"), "firma_id", minimum=1))
    return json_response({"ok": True})


PAGE = Page(
    key="skaper", label="Skraper", path="/skraper", render=render, order=30,
    routes={
        ("GET", "/api/skaper/meta"): api_meta,
        ("POST", "/api/skaper/seed"): api_seed,
        ("POST", "/api/skaper/dodaj"): api_add_company,
        ("GET", "/api/skaper/firmy"): api_companies,
        ("GET", "/api/skaper/firma"): api_company,
        ("GET", "/api/skaper/status"): api_status,
        ("POST", "/api/skaper/start"): api_start,
        ("POST", "/api/skaper/stop"): api_stop,
        ("POST", "/api/skaper/ponow"): api_requeue,
        ("POST", "/api/skaper/uzyj-domeny"): api_use_target_domain,
        ("POST", "/api/skaper/usun-dane"): api_delete_data,
    },
)
