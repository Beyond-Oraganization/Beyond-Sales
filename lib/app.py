"""Rdzeń aplikacji webowej: żądanie/odpowiedź, zabezpieczenia i routing stron.

Strony nie dotykają `BaseHTTPRequestHandler` — dostają `Request` i zwracają
`Response`. Dzięki temu całą logikę (w tym zabezpieczenia) da się testować
bez gniazd sieciowych; `server.py` to tylko cienki adapter HTTP.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Mapping, Optional

from lib import config as cfg
from lib import db as dbmod
from lib.config import ConfigError, load_config

MAX_BODY = 2 * 1024 * 1024

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


@dataclass
class Request:
    method: str
    path: str
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    content_length: Optional[int] = None
    json: dict = field(default_factory=dict)

    def arg(self, name: str, default: str = "") -> str:
        values = self.query.get(name)
        return values[0] if values else default

    def args(self, name: str) -> list:
        return list(self.query.get(name, []))


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    headers: dict = field(default_factory=dict)
    stream: Optional[BinaryIO] = None
    length: Optional[int] = None


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def json_response(payload, status: int = 200) -> Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(status, body, "application/json; charset=utf-8")


def html_response(html: str, status: int = 200) -> Response:
    return Response(status, html.encode("utf-8"), "text/html; charset=utf-8")


def error_response(status: int, message: str, **extra) -> Response:
    return json_response({"error": message, **extra}, status)


class App:
    def __init__(self, db: dbmod.Database, pages: list, *, config_path: str = cfg.DEFAULT_CONFIG,
                 smtp_factory: Optional[Callable] = None, index_html: Optional[str] = cfg.INDEX_HTML,
                 allowed_hosts: Optional[set] = None):
        self.db = db
        self.pages = pages
        self.config_path = config_path
        self.smtp_factory = smtp_factory
        self.index_html = index_html
        self.allowed_hosts = allowed_hosts or set()
        self.routes = self._build_routes(pages)

    @staticmethod
    def _build_routes(pages: list) -> dict:
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

    def load_config(self) -> dict:
        return load_config(self.config_path)

    def set_port(self, port: int) -> None:
        self.allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}

    def refresh_dashboard(self) -> None:
        if not self.index_html:
            return
        from lib import dashboard
        try:
            dashboard.generate_dashboard(self.db.fetch_all(), self.index_html)
        except OSError as exc:
            print(f"Nie udało się odświeżyć index.html: {exc}", file=sys.stderr)

    def dispatch(self, req: Request) -> Response:
        try:
            resp = self._dispatch(req)
        except HttpError as exc:
            resp = error_response(exc.status, exc.message, **exc.extra)
        except dbmod.RowNotFound as exc:
            resp = error_response(404, str(exc))
        except dbmod.RowLocked as exc:
            resp = error_response(409, str(exc), row=exc.row)
        except dbmod.EmptyDatabase as exc:
            resp = error_response(409, str(exc))
        except dbmod.InvalidValue as exc:
            resp = error_response(400, str(exc))
        except ConfigError as exc:
            resp = error_response(400, str(exc))
        except (dbmod.DbUnavailable, *dbmod.DB_ERRORS):
            traceback.print_exc(file=sys.stderr)
            resp = error_response(503, "Baza danych jest niedostępna. Sprawdź, czy MySQL działa (python3 -m lib.db --check).")
        except Exception:
            traceback.print_exc(file=sys.stderr)
            resp = error_response(500, "Wewnętrzny błąd serwera (szczegóły w terminalu, w którym działa server.py).")
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
        return handler(req, self)


def _page_handler(page) -> Callable:
    def handler(req: Request, app: App) -> Response:
        return html_response(page.render(app))
    return handler
