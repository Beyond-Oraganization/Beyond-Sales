"""Strona „Wysyłka": tabela maili z edycją, zaznaczaniem i wysyłką."""

from __future__ import annotations

from typing import Optional

from perno.services import bulk, generation, listing, sending
from perno.settings import smtp_configured
from perno.web import layout
from perno.web.context import AppContext
from perno.web.http import HttpError, Request, Response, json_response
from perno.web.page import Page


def render(ctx: AppContext) -> str:
    return layout.render_page(
        ctx, title="Wysyłka", active_key="wysylka",
        body=layout.read_asset("wysylka.html"),
        css=layout.read_asset("wysylka.css"),
        js=layout.read_asset("bulk.js") + "\n" + layout.read_asset("wysylka.js"), kit=True,
    )


def api_maile(req: Request, ctx: AppContext) -> Response:
    """`?light=1` zwraca wiersze bez treści maili (z `has_body`) — treść pobiera się osobno przez `/api/maile/row`."""
    return json_response({"rows": ctx.db.fetch_all(light=req.arg("light") == "1")})


def api_list(req: Request, ctx: AppContext) -> Response:
    """Jedna strona listy z licznikami: `{state, page, size, ids?, counts?, totals?, presets?}` (patrz `services.listing`)."""
    return json_response(listing.list_view(ctx.db, listing.parse_list_request(req.json)))


def api_list_ids(req: Request, ctx: AppContext) -> Response:
    """Id wierszy bieżącego widoku w jego kolejności (do „zaznacz wszystkie pasujące”, „pierwsze N”, „gotowe do wysłania”)."""
    return json_response(listing.view_ids(ctx.db, req.json))


def api_selection(req: Request, ctx: AppContext) -> Response:
    """Podsumowanie zaznaczenia: ile firm, ile gotowych do wysyłki, ile z zagranicy, ile poza bieżącym filtrem."""
    return json_response(listing.selection_summary(ctx.db, req.json))


def api_selected_emails(req: Request, ctx: AppContext) -> Response:
    return json_response(listing.selected_emails(ctx.db, req.json))


def api_maile_row(req: Request, ctx: AppContext) -> Response:
    row_id = req.arg("id")
    if not row_id:
        raise HttpError(400, "Brak parametru id.")
    row = ctx.db.get(row_id)
    if row is None:
        raise HttpError(404, f"Nie znaleziono wiersza {row_id}.")
    return json_response({"row": row})


def api_status(req: Request, ctx: AppContext) -> Response:
    config = ctx.config.load()
    return json_response({
        "smtp_configured": smtp_configured(config),
        "smtp_user": config.get("SMTP_USER", ""),
        "sent_today": ctx.db.sent_today_count(),
        "row_count": ctx.db.count_rows(),
        "db": ctx.db.describe(),
    })


def _row_id(req: Request) -> str:
    row_id = req.json.get("id")
    if not isinstance(row_id, str) or not row_id:
        raise HttpError(400, "Brak pola id.")
    return row_id


def api_update_row(req: Request, ctx: AppContext) -> Response:
    row_id = _row_id(req)
    changes = {k: v for k, v in req.json.items() if k != "id"}
    for name, value in changes.items():
        if not isinstance(value, str):
            raise HttpError(400, f"Pole {name} musi być tekstem.")
    return json_response({"row": ctx.db.apply_edit(row_id, changes)})


def _daily_cap(payload: dict) -> Optional[int]:
    """Dzienny limit z żądania; wartość nie-całkowita albo ≤ 0 oznacza „bez limitu” (jak dotąd)."""
    value = payload.get("daily_cap")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def api_send_one(req: Request, ctx: AppContext) -> Response:
    result = sending.send_one(ctx.db, _row_id(req), ctx.mailer, daily_cap=_daily_cap(req.json))
    if result.outcome == "skipped":
        raise HttpError(409, result.message, row=result.row)
    return json_response({"row": result.row, "outcome": result.outcome, "message": result.message})


def api_regenerate(req: Request, ctx: AppContext) -> Response:
    sender_name = ctx.config.load().get("SENDER_NAME", "")
    result = generation.refresh_segments(ctx.db, ("eventy", "driverzy"), sender_name)
    return json_response({**result, "rows": ctx.db.fetch_all(light=req.json.get("light") is True)})


def api_bulk_update(req: Request, ctx: AppContext) -> Response:
    """Zbiorcza zmiana statusu i/lub danych firm. `dry_run: true` tylko pokazuje, co by się stało; zapis, który
    wymaga potwierdzenia ostrzeżeń (`acknowledge`), a go nie dostał, kończy się 409 i niczego nie zmienia."""
    request = bulk.parse_request(req.json)
    if req.json.get("dry_run") is True:
        return json_response({"dry_run": True, **bulk.preview(ctx.db, request).to_json()})
    return json_response({"dry_run": False, **bulk.apply(ctx.db, request, ctx.undo_log)})


def api_bulk_undo(req: Request, ctx: AppContext) -> Response:
    operation = req.json.get("operation")
    if operation is not None and not isinstance(operation, str):
        raise HttpError(400, "Pole operation musi być tekstem.")
    result = bulk.undo(ctx.db, ctx.undo_log, operation)
    return json_response(result)


def api_bulk_log(req: Request, ctx: AppContext) -> Response:
    return json_response({"operations": ctx.undo_log.listing(), "limit": bulk.UNDO_LIMIT})


PAGE = Page(
    key="wysylka", label="Wysyłka", path="/", render=render, order=10,
    routes={
        ("GET", "/api/maile"): api_maile,
        ("GET", "/api/maile/row"): api_maile_row,
        ("POST", "/api/mailing/list"): api_list,
        ("POST", "/api/mailing/ids"): api_list_ids,
        ("POST", "/api/mailing/selection"): api_selection,
        ("POST", "/api/mailing/emails"): api_selected_emails,
        ("POST", "/api/bulk-update"): api_bulk_update,
        ("POST", "/api/bulk-undo"): api_bulk_undo,
        ("GET", "/api/bulk-log"): api_bulk_log,
        ("GET", "/api/status"): api_status,
        ("POST", "/api/update-row"): api_update_row,
        ("POST", "/api/send-one"): api_send_one,
        ("POST", "/api/regenerate"): api_regenerate,
    },
)
