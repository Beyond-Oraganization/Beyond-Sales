"""Strona główna „Beyond Sales”: przepływ pracy z licznikami, polecenia dla AI z instrukcji i kafelki dokumentacji.

Strona jest renderowana w całości po stronie serwera (działa bez skryptu; skrypt tylko kopiuje polecenia). Liczniki czyta
`read_figures`: awaria jednego z nich (np. baza chwilowo niedostępna) pokazuje „—” w tym miejscu i nie psuje strony.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from string import Template
from typing import Optional

from perno import settings
from perno.domain import statuses as st
from perno.domain.rowfilter import ALL_ROWS
from perno.services import docs
from perno.web import layout
from perno.web.context import AppContext
from perno.web.page import Page

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"(\[[^\]]+\])")  # miejsca do uzupełnienia w poleceniu: [ilość], [typ]…
NO_VALUE = "—"


@dataclass(frozen=True)
class Figure:
    """Licznik na kafelku kroku: klucz w `read_figures` i opis tego, co liczy."""

    key: str
    label: str


@dataclass(frozen=True)
class Step:
    """Krok przepływu pracy: opis, zakładka panelu, w której się go wykonuje, i liczniki."""

    title: str
    text: str
    path: str
    action: str
    figures: tuple[Figure, ...]


STEPS = (
    Step("Znajdź firmy", "Skraper zbiera strony firm, a AI wyszukuje nowe i sprawdza, czy pasują do oferty.",
         "/skraper", "Skraper", (Figure("companies", "firm w bazie"),)),
    Step("Napisz maile", "AI czyta zeskanowane strony i pisze krótki, konkretny pierwszy mail dla każdej firmy.",
         "/wysylka", "Wysyłka", (Figure("awaiting_copy", "czeka na treść"),)),
    Step("Wyślij", "Sprawdzasz treść i zatwierdzasz. Automat wysyła w ustawionym tempie, z rotacją skrzynek.",
         "/automat", "Automat", (Figure("to_send", "do wysłania"), Figure("sent_today", "wysłano dziś"))),
    Step("Czytaj odpowiedzi", "Odpowiedzi ze wszystkich skrzynek w jednym miejscu; AI je ocenia i szykuje szkice.",
         "/odpowiedzi", "Odpowiedzi", (Figure("unread", "nieprzeczytanych"),)),
    Step("Prowadź leady", "Firmy, które odpisały pozytywnie lub neutralnie, z osobą kontaktową i opisem.",
         "/leady", "Leady", (Figure("leads", "do uzupełnienia"),)),
)


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _attempt[T](name: str, read: Callable[[], T]) -> Optional[T]:
    """Wynik odczytu albo None, gdy się nie powiódł (błąd trafia do logu z nazwą licznika)."""
    try:
        return read()
    except Exception:
        logger.warning("Licznik „%s” na stronie głównej nie powiódł się", name, exc_info=True)
        return None


def read_figures(ctx: AppContext) -> dict[str, Optional[int]]:
    """Liczniki kroków; None przy liczniku, którego nie udało się odczytać."""
    statuses = _attempt("statusy maili", ctx.db.status_counts)

    def of_status(code: str) -> Optional[int]:
        return None if statuses is None else int(statuses.get(code, 0))

    return {
        "companies": _attempt("firmy", lambda: ctx.db.count_companies(ALL_ROWS)),
        "awaiting_copy": of_status(st.STATUS_AWAITING_COPY),
        "to_send": of_status(st.STATUS_PENDING),
        "sent_today": _attempt("wysłano dziś", ctx.db.sent_today_count),
        "unread": _attempt("odpowiedzi", lambda: ctx.replies_store.get().unread_total()),
        "leads": _attempt("leady", lambda: ctx.leads_store.get().pending_total()),
    }


def _number(value: Optional[int]) -> str:
    """Liczba ze spacją nierozdzielającą co trzy cyfry (jak w polskich tekstach) albo „—”."""
    return NO_VALUE if value is None else f"{value:,}".replace(",", " ")


def figure_html(figure: Figure, value: Optional[int]) -> str:
    """Jeden licznik: duża liczba i podpis; zero jest przygaszone, brak odczytu to „—”."""
    tone = " is-zero" if value == 0 else (" is-none" if value is None else "")
    return f'<span class="hm-fig{tone}"><b>{_number(value)}</b><span>{_esc(figure.label)}</span></span>'


def step_html(number: int, step: Step, values: Mapping[str, Optional[int]]) -> str:
    """Kafelek kroku: cały jest odnośnikiem do zakładki."""
    figures = "".join(figure_html(figure, values.get(figure.key)) for figure in step.figures)
    return (f'<li><a class="hm-step" href="{_esc(step.path)}"><span class="hm-num" aria-hidden="true">{number}</span>'
            f'<strong class="hm-step-title">{_esc(step.title)}</strong><span class="hm-step-text">{_esc(step.text)}</span>'
            f'<span class="hm-figs">{figures}</span><span class="hm-step-go">{_esc(step.action)}</span></a></li>')


def command_html(command: docs.Command, route: str) -> str:
    """Wiersz polecenia: klik kopiuje je, osobny odnośnik prowadzi do opisu w instrukcji; miejsca na parametry są wyróżnione."""
    parts = _PLACEHOLDER.split(command.text)
    label = "".join(f'<span class="hm-ph">{_esc(part)}</span>' if at % 2 else _esc(part) for at, part in enumerate(parts))
    return (f'<li class="hm-cmd"><button type="button" class="hm-cmd-copy" data-copy="{_esc(command.text)}" '
            f'data-copied="Skopiowano polecenie."><code>{label}</code></button>'
            f'<a class="hm-cmd-sec" href="{_esc(route)}#{_esc(command.anchor)}" '
            f'aria-label="Opis polecenia w instrukcji, sekcja {_esc(command.section)}">§{_esc(command.section)}</a></li>')


def doc_html(spec: docs.DocSpec, document: Optional[docs.Document]) -> str:
    """Kafelek dokumentu z nazwą pliku, opisem i rozmiarem (sekcje, czas czytania)."""
    meta = "nie znaleziono pliku" if document is None else f"{document.sections_label} · {document.minutes_label}"
    return (f'<li><a class="hm-doc" href="{_esc(spec.path)}"><code class="hm-doc-file">{_esc(spec.file)}</code>'
            f'<strong class="hm-doc-title">{_esc(spec.title)}</strong><span class="hm-doc-text">{_esc(spec.blurb)}</span>'
            f'<span class="hm-doc-meta">{_esc(meta)}</span></a></li>')


def render(ctx: AppContext) -> str:
    base_dir = settings.BASE_DIR
    loaded = {spec.key: docs.load(spec, base_dir) for spec in docs.DOCS}
    instructions = loaded[docs.INSTRUCTIONS_KEY]
    spec = docs.spec_for(docs.INSTRUCTIONS_KEY)
    commands = docs.commands(instructions.text) if instructions else ()
    values = read_figures(ctx)
    body = Template(layout.read_asset("home.html")).substitute(
        steps="".join(step_html(at, step, values) for at, step in enumerate(STEPS, 1)),
        commands="".join(command_html(command, spec.path) for command in commands)
        or '<li class="hm-empty">Nie znaleziono tabeli poleceń w pliku z instrukcjami.</li>',
        reference=_esc(docs.attach_reference(spec, base_dir, settings.PERNO_DIR)),
        docs="".join(doc_html(item, loaded[item.key]) for item in docs.DOCS),
        brand=_esc(layout.BRAND), instructions_path=_esc(spec.path))
    return layout.render_page(ctx, title="Strona główna", active_key=layout.HOME_KEY, body=body,
                              css=layout.read_asset("home.css"), js=layout.read_asset("home.js"))


PAGE = Page(key=layout.HOME_KEY, label="Start", path="/", render=render, order=0, in_nav=False)
