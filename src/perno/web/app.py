"""Rdzeń aplikacji webowej: zabezpieczenia i routing stron.

Strony dostają `Request` i zwracają `Response` (patrz `perno.web.http`), a `cli/server.py` to tylko cienki adapter
HTTP. Wyjątki zamienia na kody HTTP `perno.web.errors`.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional, Sequence

from perno.db import DB_ERRORS, DbUnavailable
from perno.web import errors
from perno.web.context import AppContext
from perno.web.http import (
    MAX_BODY,
    SECURITY_HEADERS,
    HttpError,
    Request,
    Response,
    error_response,
    html_response,
)
from perno.web.page import Page

logger = logging.getLogger(__name__)


class App:
    """Routing i zabezpieczenia; zależności stron (baza, konfiguracja…) leżą w `ctx` i trafiają do handlerów."""

    def __init__(self, ctx: AppContext, *, allowed_hosts: Optional[set] = None):
        self.ctx = ctx
        self.allowed_hosts = allowed_hosts or set()
        self.routes = self._build_routes(ctx.pages)

    @staticmethod
    def _build_routes(pages: Sequence[Page]) -> dict:
        routes, keys = {}, set()
        for page in pages:
            if page.key in keys:
                raise ValueError(f"Zduplikowany klucz strony: {page.key}")
            keys.add(page.key)
            if not page.path.startswith("/"):
                raise ValueError(f"Ścieżka strony musi zaczynać się od '/': {page.path}")
            page_routes = {("GET", page.path): _page_handler(page), **page.routes}
            for route, handler in page_routes.items():
                if route in routes:
                    raise ValueError(f"Zduplikowana trasa: {route[0]} {route[1]}")
                routes[route] = handler
        return routes

    def set_port(self, port: int) -> None:
        self.allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}

    def dispatch(self, req: Request) -> Response:
        try:
            resp = self._dispatch(req)
        except (DbUnavailable, *DB_ERRORS):
            logger.exception("Baza danych niedostępna (%s %s)", req.method, req.path)
            resp = error_response(503, "Baza danych jest niedostępna. Sprawdź, czy MySQL działa (python3 -m perno.db --check).")
        except Exception as exc:
            known = errors.to_response(exc)
            if known is None:
                logger.exception("Nieoczekiwany błąd w obsłudze żądania %s %s", req.method, req.path)
            resp = known or error_response(500, "Wewnętrzny błąd serwera (szczegóły w terminalu, w którym działa server.py).")
        resp.headers = {**SECURITY_HEADERS, **resp.headers}
        return resp

    def _dispatch(self, req: Request) -> Response:
        host = (req.headers.get("host") or "").lower()
        if host not in self.allowed_hosts:
            raise HttpError(403, "Niedozwolony nagłówek Host.")
        origin = req.headers.get("origin")
        if origin is not None and origin != f"http://{host}":
            raise HttpError(403, "Niedozwolone źródło żądania (Origin).")
        if req.method not in ("GET", "POST"):
            raise HttpError(405, "Metoda niedozwolona.")

        if req.method == "POST":
            size = req.content_length if req.content_length is not None else len(req.body)
            if size > MAX_BODY:
                raise HttpError(413, "Zbyt duże żądanie.")
            media_type = (req.headers.get("content-type") or "").split(";")[0].strip().lower()
            if media_type != "application/json":
                raise HttpError(415, "Wymagany Content-Type: application/json.")
            try:
                parsed = json.loads(req.body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                raise HttpError(400, "Nieprawidłowy JSON.") from None
            if not isinstance(parsed, dict):
                raise HttpError(400, "Treść żądania musi być obiektem JSON.")
            req.json = parsed

        handler = self.routes.get((req.method, req.path.rstrip("/") or "/"))
        if handler is None:
            raise HttpError(404, "Nie znaleziono.")
        return handler(req, self.ctx)


def _page_handler(page) -> Callable:
    def handler(req: Request, ctx: AppContext) -> Response:
        return html_response(page.render(ctx))
    return handler
