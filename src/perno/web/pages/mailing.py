"""Strona „Wysyłka": tabela maili z edycją, zaznaczaniem i wysyłką."""

from __future__ import annotations

from perno.domain import statuses as st
from perno.services import bulk, generation, sending
from perno.settings import require_smtp, smtp_configured
from perno.web import layout
from perno.web.app import HttpError, Request, Response, json_response
from perno.web.context import AppContext
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


def api_send_one(req: Request, ctx: AppContext) -> Response:
    row_id = _row_id(req)
    row = ctx.db.get(row_id)
    if row is None:
        raise HttpError(404, f"Nie znaleziono wiersza {row_id}.")
    if row["status"] == st.STATUS_SENT:
        raise HttpError(400, "Ten mail już został wysłany.")
    if row["status"] in st.KEEPS_SENT_AT:
        raise HttpError(400, f"Ten wiersz ma status „{st.STATUS_META[row['status']][0]}” — nie wysyłam. "
                             "Jeśli to pomyłka, zmień status ręcznie.")
    if not row["email"] or not row["body_html"]:
        raise HttpError(400, "Brak e-maila lub treści — uzupełnij przed wysyłką.")

    config = require_smtp(ctx.config.load(), ctx.config.path)
    daily_cap = req.json.get("daily_cap")
    if isinstance(daily_cap, int) and not isinstance(daily_cap, bool) and daily_cap > 0:
        if ctx.db.sent_today_count() >= daily_cap:
            raise HttpError(429, f"Dzienny limit wysyłki ({daily_cap}) jest wyczerpany.")

    smtp_factory = ctx.smtp_factory or sending.smtp_factory_from_config(config)
    result = sending.send_row(
        ctx.db, row_id, smtp_factory=smtp_factory, config=config,
        sender_name=config.get("SENDER_NAME", ""),
        allowed_from=(st.STATUS_PENDING, st.STATUS_ERROR),
    )
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
    try:
        result = bulk.apply(ctx.db, request, ctx.undo_log)
    except bulk.AckRequired as exc:
        raise HttpError(409, str(exc), **exc.plan.to_json()) from None
    return json_response({"dry_run": False, **result})


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
        ("POST", "/api/bulk-update"): api_bulk_update,
        ("POST", "/api/bulk-undo"): api_bulk_undo,
        ("GET", "/api/bulk-log"): api_bulk_log,
        ("GET", "/api/status"): api_status,
        ("POST", "/api/update-row"): api_update_row,
        ("POST", "/api/send-one"): api_send_one,
        ("POST", "/api/regenerate"): api_regenerate,
    },
)
