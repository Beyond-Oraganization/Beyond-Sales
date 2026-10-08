"""Strona „Odpowiedzi”: skrzynka odpowiedzi firm w układzie lista | wątek (jak w poczcie), z oceną i statusem.

Handlery tylko czytają żądanie i wołają `services.replies` / `services.replies_sync`; wyjątki mapuje `web/errors.py`. Treść
wiadomości jest niezaufana: trafia do przeglądarki jako tekst i tylko tak jest wyświetlana (`textContent`).
"""

from __future__ import annotations

from perno.services import replies, replies_report, replies_send, replies_sync
from perno.settings import ConfigError
from perno.web import layout
from perno.web.context import AppContext
from perno.web.http import HttpError, Request, Response, json_response
from perno.web.page import Page


def render(ctx: AppContext) -> str:
    return layout.render_page(
        ctx, title="Odpowiedzi", active_key="odpowiedzi", body=layout.read_asset("odpowiedzi.html"),
        css=layout.read_asset("odpowiedzi.css"), js=layout.read_asset("odpowiedzi.js"),
    )


def badge(ctx: AppContext) -> int:
    """Licznik w menu: odpowiedzi czekające na nas i jeszcze nieprzeczytane."""
    return ctx.replies_store.get().unread_total()


def _store(ctx: AppContext):
    return ctx.replies_store.get()


def _text(req: Request, name: str) -> str:
    value = req.json.get(name)
    if not isinstance(value, str) or not value:
        raise HttpError(400, f"Pole {name} jest wymagane (tekst).")
    return value


def api_list(req: Request, ctx: AppContext) -> Response:
    return json_response(replies.list_replies(_store(ctx), req.json))


def api_thread(req: Request, ctx: AppContext) -> Response:
    return json_response(replies.thread_view(_store(ctx), _text(req, "id"), ctx.config.load()))


def _message_id(req: Request) -> int:
    value = req.json.get("id")
    if isinstance(value, bool) or not isinstance(value, int):
        raise HttpError(400, "Pole id musi być liczbą całkowitą.")
    return value


def api_message(req: Request, ctx: AppContext) -> Response:
    return json_response(replies.unassigned_view(_store(ctx), _message_id(req)))


def api_candidates(req: Request, ctx: AppContext) -> Response:
    return json_response(replies.candidates(_store(ctx), req.json.get("q")))


def api_rate(req: Request, ctx: AppContext) -> Response:
    restore = req.json.get("restore_by")
    by = restore if restore in ("human", "ai") else "human"
    result = replies.rate(_store(ctx), _text(req, "id"), req.json.get("rating"), note=req.json.get("note", ""), by=by,
                          now=ctx.db.now(), restore=bool(restore))
    return json_response(result)


def api_draft(req: Request, ctx: AppContext) -> Response:
    result = replies_send.save_draft(_store(ctx), ctx.config.load(), _text(req, "id"), req.json.get("body"), author="human", now=ctx.db.now())
    return json_response(result)


def api_draft_discard(req: Request, ctx: AppContext) -> Response:
    return json_response({"discarded": _store(ctx).discard_draft(_text(req, "id"))})


def _optional_imap(ctx: AppContext):
    """Fabryka sesji IMAP do kopii w Wysłanych albo None, gdy odbiór jest tu zablokowany (baza demo)."""
    try:
        return ctx.imap_connector()
    except ConfigError:
        return None


def api_send(req: Request, ctx: AppContext) -> Response:
    acks = req.json.get("ack", [])
    if not isinstance(acks, list) or not all(isinstance(code, str) for code in acks):
        raise HttpError(400, "Pole ack musi być listą kodów.")
    options = replies_send.SendOptions(acks=tuple(acks), quote=req.json.get("quote", True) is not False, close=req.json.get("close") is True,
                                       imap_factory=_optional_imap(ctx))
    try:
        mailers = ctx.mailers()
    except ConfigError as exc:
        raise HttpError(400, str(exc)) from exc
    return json_response(replies_send.send_reply(_store(ctx), ctx.config.load(), mailers, _text(req, "id"), options, ctx.db.now()))


def api_report(req: Request, ctx: AppContext) -> Response:
    return json_response(replies_report.build_report(_store(ctx), ctx.config.load(), req.json, ctx.db.now()))


def api_report_file(req: Request, ctx: AppContext) -> Response:
    """Raport jako plik (`format`: csv albo md); filtry jak w widoku."""
    kind = req.json.get("format")
    if kind not in ("csv", "md"):
        raise HttpError(400, "Pole format musi mieć wartość csv albo md.")
    report = replies_report.build_report(_store(ctx), ctx.config.load(), req.json, ctx.db.now())
    body = (replies_report.csv_text(report) if kind == "csv" else replies_report.markdown(report)).encode("utf-8")
    stamp = ctx.db.now().strftime("%Y%m%d")
    return Response(200, body=body, content_type="text/csv; charset=utf-8" if kind == "csv" else "text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="raport_odpowiedzi_{stamp}.{kind}"'})


def api_state(req: Request, ctx: AppContext) -> Response:
    return json_response(replies.set_state(_store(ctx), _text(req, "id"), _text(req, "action"), ctx.db.now()))


def api_status(req: Request, ctx: AppContext) -> Response:
    return json_response(replies.set_mail_status(ctx.db, _store(ctx), _text(req, "id"), req.json.get("status")))


def api_assign(req: Request, ctx: AppContext) -> Response:
    message_id = req.json.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        raise HttpError(400, "Pole message_id musi być liczbą całkowitą.")
    primary = (ctx.config.load().get("SMTP_USER") or "").lower()
    return json_response(replies.assign(_store(ctx), message_id, _text(req, "mail_id"), primary, ctx.db.now()))


def api_overview(req: Request, ctx: AppContext) -> Response:
    return json_response(replies.panel_state(_store(ctx), ctx.config.load(), ctx.reply_runner.running))


def api_fetch(req: Request, ctx: AppContext) -> Response:
    """Startuje pobieranie w tle (202); gdy jedno już trwa, odpowiada, że nie wystartowało."""
    config = ctx.config.load()
    try:
        factory, options = ctx.imap_connector(), replies_sync.options_from_config(config, only=str(req.json.get("mailbox") or ""))
    except ConfigError as exc:
        raise HttpError(400, str(exc)) from exc
    store = _store(ctx)
    started = ctx.reply_runner.start(lambda: replies_sync.fetch_replies(ctx.db, store, config, factory, options=options))
    return json_response({"started": started, "reason": "" if started else "Pobieranie już trwa."}, 202 if started else 200)


PAGE = Page(
    key="odpowiedzi", label="Odpowiedzi", path="/odpowiedzi", render=render, order=12, badge=badge,
    routes={
        ("POST", "/api/odpowiedzi/lista"): api_list,
        ("POST", "/api/odpowiedzi/watek"): api_thread,
        ("POST", "/api/odpowiedzi/wiadomosc"): api_message,
        ("POST", "/api/odpowiedzi/kandydaci"): api_candidates,
        ("POST", "/api/odpowiedzi/ocena"): api_rate,
        ("POST", "/api/odpowiedzi/szkic"): api_draft,
        ("POST", "/api/odpowiedzi/szkic-usun"): api_draft_discard,
        ("POST", "/api/odpowiedzi/wyslij"): api_send,
        ("POST", "/api/odpowiedzi/raport"): api_report,
        ("POST", "/api/odpowiedzi/raport-plik"): api_report_file,
        ("POST", "/api/odpowiedzi/akcja"): api_state,
        ("POST", "/api/odpowiedzi/status"): api_status,
        ("POST", "/api/odpowiedzi/przypisz"): api_assign,
        ("GET", "/api/odpowiedzi/stan"): api_overview,
        ("POST", "/api/odpowiedzi/pobierz"): api_fetch,
    },
)
