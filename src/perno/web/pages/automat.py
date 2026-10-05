"""Strona „Automat”: limity, stan planu i demona, prognoza kolejki, dziennik zdarzeń oraz Start z potwierdzeniem.

Handlery tylko czytają żądanie i wołają `services.automat_control`. Samo wysyłanie robi osobny demon
(`perno-automat run`, uruchamiany przez launchd), więc zamknięcie tej karty niczego nie przerywa.
"""

from __future__ import annotations

from perno.services import automat_control
from perno.web import layout
from perno.web.context import AppContext
from perno.web.http import HttpError, Request, Response, json_response
from perno.web.page import Page


def render(ctx: AppContext) -> str:
    return layout.render_page(
        ctx, title="Automat", active_key="automat",
        body=layout.read_asset("automat.html"),
        css=layout.read_asset("automat.css"), js=layout.read_asset("automat.js"),
    )


def api_status(req: Request, ctx: AppContext) -> Response:
    return json_response(automat_control.status_report(
        ctx.db, ctx.automat_store.get(), ctx.db.now(), ctx.automat_probes, channels=ctx.notify_channels()))


def api_preview(req: Request, ctx: AppContext) -> Response:
    return json_response(automat_control.preview(ctx.db, ctx.automat_store.get(), ctx.db.now()))


def api_limits(req: Request, ctx: AppContext) -> Response:
    return json_response(automat_control.save_settings(ctx.automat_store.get(), ctx.automat_log(), req.json))


def api_start(req: Request, ctx: AppContext) -> Response:
    stamp = req.json.get("stamp", "")
    if not isinstance(stamp, str):
        raise HttpError(400, "Pole stamp musi być tekstem.")
    state = automat_control.arm(ctx.db, ctx.automat_store.get(), ctx.automat_log(), ctx.mailer,
                                acknowledged=req.json.get("potwierdzam") is True, stamp=stamp)
    return json_response({"ok": True, "armed_until": automat_control.stamp_of(state.armed_until)})


def api_stop(req: Request, ctx: AppContext) -> Response:
    return json_response({"ok": automat_control.disarm(ctx.automat_store.get(), ctx.automat_log())})


def api_pause(req: Request, ctx: AppContext) -> Response:
    automat_control.pause(ctx.automat_store.get(), ctx.automat_log())
    return json_response({"ok": True})


def api_resume(req: Request, ctx: AppContext) -> Response:
    automat_control.resume(ctx.automat_store.get(), ctx.automat_log())
    return json_response({"ok": True})


PAGE = Page(
    key="automat", label="Automat", path="/automat", render=render, order=15,
    routes={
        ("GET", "/api/automat/status"): api_status,
        ("GET", "/api/automat/preview"): api_preview,
        ("POST", "/api/automat/limits"): api_limits,
        ("POST", "/api/automat/start"): api_start,
        ("POST", "/api/automat/stop"): api_stop,
        ("POST", "/api/automat/pause"): api_pause,
        ("POST", "/api/automat/resume"): api_resume,
    },
)
