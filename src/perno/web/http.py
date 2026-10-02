"""Prymitywy HTTP niezależne od serwera: żądanie, odpowiedź i błąd z kodem.

Strony nie dotykają `BaseHTTPRequestHandler` — dostają `Request` i zwracają `Response`, więc całą logikę
(w tym zabezpieczenia) da się testować bez gniazd sieciowych.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import BinaryIO, Optional

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
