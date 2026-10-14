"""Podstrony z instrukcjami i dokumentacją (AI_INSTRUCTIONS, README, zasady maili i projektu).

Treść to pliki projektu czytane z dysku (`services.docs`), więc poprawka w pliku jest widoczna po odświeżeniu strony. W menu jest
jedna pozycja „Pomoc” (pierwszy dokument); pozostałe podstrony podświetlają ją i przełącza się je zakładkami nad treścią.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from string import Template

from perno import settings
from perno.services import docs
from perno.web import layout
from perno.web.context import AppContext
from perno.web.http import HttpError
from perno.web.page import Page

MENU_KEY = docs.INSTRUCTIONS_KEY  # wszystkie podstrony podświetlają w menu tę jedną pozycję
MENU_LABEL = "Pomoc"
MENU_ORDER = 50


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def tabs_html(active_key: str) -> str:
    """Zakładki przełączające między dokumentami."""
    links = []
    for spec in docs.DOCS:
        current = ' aria-current="page"' if spec.key == active_key else ""
        links.append(f'<a class="doc-tab" href="{_esc(spec.path)}"{current}>{_esc(spec.title)}</a>')
    return f'<nav class="doc-tabs seg" aria-label="Dokumenty">{"".join(links)}</nav>'


def toc_html(document: docs.Document) -> str:
    """Spis treści: nagłówki drugiego i trzeciego poziomu, trzeci poziom z wcięciem."""
    return "".join(
        f'<a class="doc-toc-link{" is-sub" if heading.level == 3 else ""}" href="#{_esc(heading.slug)}">{_esc(heading.text)}</a>'
        for heading in document.rendered.toc())


def _meta(document: docs.Document) -> str:
    return f"<span>{document.sections_label}</span><span>{document.minutes_label} czytania</span>"


def render_document(ctx: AppContext, spec: docs.DocSpec) -> str:
    document = docs.load(spec, settings.BASE_DIR)
    if document is None:
        raise HttpError(404, f"Nie znaleziono pliku {spec.file}.")
    body = Template(layout.read_asset("docs.html")).substitute(
        title=_esc(spec.title), blurb=_esc(spec.blurb), file=_esc(spec.file), meta=_meta(document), tabs=tabs_html(spec.key),
        reference=_esc(docs.attach_reference(spec, settings.BASE_DIR, settings.PERNO_DIR)), toc=toc_html(document),
        article=document.rendered.html)
    return layout.render_page(ctx, title=spec.title, active_key=MENU_KEY, body=body,
                              css=layout.read_asset("docs.css"), js=layout.read_asset("docs.js"))


def _renderer(spec: docs.DocSpec) -> Callable[[AppContext], str]:
    return lambda ctx: render_document(ctx, spec)


def _page(spec: docs.DocSpec) -> Page:
    in_menu = spec.key == MENU_KEY
    return Page(key=spec.key, label=MENU_LABEL if in_menu else spec.title, path=spec.path, render=_renderer(spec),
                order=MENU_ORDER, in_nav=in_menu)


PAGES = [_page(spec) for spec in docs.DOCS]
