"""Strona „Konfiguracja”: ustawienia z config.env (poczta, powiadomienia, skraper) bez otwierania pliku w edytorze.

Handlery tylko czytają żądanie i wołają `services.config_editor`. Wartości sekretów nigdy nie wracają do przeglądarki.
"""

from __future__ import annotations

from perno.services import config_editor
from perno.web import layout
from perno.web.context import AppContext
from perno.web.http import HttpError, Request, Response, json_response
from perno.web.page import Page


def render(ctx: AppContext) -> str:
    return layout.render_page(
        ctx, title="Konfiguracja", active_key="konfiguracja",
        body=layout.read_asset("konfiguracja.html"),
        css=layout.read_asset("konfiguracja.css"), js=layout.read_asset("konfiguracja.js"),
    )


def _description(ctx: AppContext) -> dict:
    return config_editor.describe(ctx.config.load(), file_exists=ctx.config.exists())


def api_get(req: Request, ctx: AppContext) -> Response:
    return json_response(_description(ctx))


def api_save(req: Request, ctx: AppContext) -> Response:
    values, clear = req.json.get("values", {}), req.json.get("clear", [])
    if not isinstance(values, dict) or not isinstance(clear, list):
        raise HttpError(400, "Pola values (obiekt) i clear (lista) są wymagane.")
    result = config_editor.apply_changes(ctx.config.path, values, clear)
    return json_response({**result, **_description(ctx)})


def api_test_smtp(req: Request, ctx: AppContext) -> Response:
    return json_response(config_editor.test_smtp(ctx.mailer, ctx.config.load().get("SMTP_PASSWORD", "")))


PAGE = Page(
    key="konfiguracja", label="Konfiguracja", path="/konfiguracja", render=render, order=40,
    routes={
        ("GET", "/api/config"): api_get,
        ("POST", "/api/config"): api_save,
        ("POST", "/api/config/test-smtp"): api_test_smtp,
    },
)
