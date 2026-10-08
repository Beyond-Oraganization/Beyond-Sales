"""Definicja strony aplikacji. Nowa strona = nowy moduł z obiektem PAGE + jedna linijka w PAGES."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional


@dataclass(frozen=True)
class Page:
    key: str
    label: str
    path: str
    render: Callable  # (ctx) -> str: pełny dokument HTML (przez layout.render_page)
    routes: Mapping = field(default_factory=dict)  # {("GET"|"POST", "/api/...") : handler(req, ctx) -> Response}
    order: int = 100
    in_nav: bool = True
    badge: Optional[Callable] = None  # (ctx) -> int: licznik przy nazwie w menu (np. nowe odpowiedzi); 0 = bez licznika
