"""Strona „Leady”: firmy z pozytywną albo neutralną odpowiedzią, z kontaktem, e-mailem i opisem działalności.

Handlery tylko czytają żądanie i wołają `services.leads`; wyjątki mapuje `web/errors.py`. Pola leadów pochodzą ze stron firm i z
odpowiedzi osób trzecich, więc trafiają do przeglądarki jako tekst (`textContent`), nigdy jako HTML.
"""

from __future__ import annotations

from perno.services import leads
from perno.web import layout
from perno.web.context import AppContext
from perno.web.http import HttpError, Request, Response, json_response
from perno.web.page import Page


def render(ctx: AppContext) -> str:
    return layout.render_page(
        ctx, title="Leady", active_key="leady", body=layout.read_asset("leady.html"),
        css=layout.read_asset("leady.css"), js=layout.read_asset("leady.js"),
    )


def badge(ctx: AppContext) -> int:
    """Licznik w menu: aktywne leady, którym czegoś brakuje, i firmy czekające na zasiew (nic nie zapisuje)."""
    return ctx.leads_store.get().pending_total()


def _store(ctx: AppContext):
    return ctx.leads_store.get()


def _text(req: Request, name: str) -> str:
    value = req.json.get(name)
    if not isinstance(value, str) or not value:
        raise HttpError(400, f"Pole {name} jest wymagane (tekst).")
    return value


def api_list(req: Request, ctx: AppContext) -> Response:
    return json_response(leads.list_leads(_store(ctx), req.json, ctx.db.now()))


def api_lead(req: Request, ctx: AppContext) -> Response:
    return json_response(leads.lead_view(_store(ctx), _text(req, "id")))


def api_save(req: Request, ctx: AppContext) -> Response:
    return json_response(leads.save(_store(ctx), _text(req, "id"), req.json.get("changes"), ctx.db.now()))


def api_approve(req: Request, ctx: AppContext) -> Response:
    return json_response(leads.approve(_store(ctx), _text(req, "id"), req.json.get("fields"), ctx.db.now()))


def api_restore(req: Request, ctx: AppContext) -> Response:
    return json_response(leads.restore(_store(ctx), _text(req, "id"), req.json.get("previous"), ctx.db.now()))


def api_export(req: Request, ctx: AppContext) -> Response:
    """Leady wybranej karty i filtrów jako plik CSV."""
    now = ctx.db.now()
    body = leads.export_csv(_store(ctx), req.json, now).encode("utf-8")
    return Response(200, body=body, content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="leady_{now:%Y%m%d}.csv"'})


PAGE = Page(
    key="leady", label="Leady", path="/leady", render=render, order=13, badge=badge, badge_label="do uzupełnienia",
    routes={
        ("POST", "/api/leady/lista"): api_list,
        ("POST", "/api/leady/lead"): api_lead,
        ("POST", "/api/leady/zapisz"): api_save,
        ("POST", "/api/leady/zatwierdz"): api_approve,
        ("POST", "/api/leady/cofnij"): api_restore,
        ("POST", "/api/leady/eksport"): api_export,
    },
)
