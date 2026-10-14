"""Markdown instrukcji i dokumentacji jako bezpieczny HTML podstrony panelu.

Surowy HTML z pliku jest escapowany, obrazki wyłączone, a linki sprawdzane: odnośnik do innego pliku z dokumentacji prowadzi
do jego podstrony, adres http(s) otwiera się w nowej karcie, a każdy inny (np. ścieżka do pliku, którego panel nie serwuje)
zostaje zwykłym tekstem. Nagłówki dostają `id` wg reguły GitHuba (małe litery, polskie znaki zostają, interpunkcja znika),
dzięki czemu odnośniki `(#1-wyszukaj-…)` z samych plików działają bez zmian.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from markdown_it import MarkdownIt
from markdown_it.token import Token

TOC_LEVELS = (2, 3)  # poziomy nagłówków w spisie treści
ANCHOR_LEVELS = (2, 3, 4)  # poziomy nagłówków z `id` i odnośnikiem „#”
_ANCHOR_HTML = '<a class="doc-anchor" href="#{slug}" aria-label="Odnośnik do tej sekcji">#</a>'
_WORD = re.compile(r"\w+")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


@dataclass(frozen=True)
class Heading:
    """Nagłówek dokumentu: poziom (1-6), tekst bez znaczników i `id` do odnośników."""

    level: int
    text: str
    slug: str


@dataclass(frozen=True)
class Rendered:
    """Wynik renderowania: HTML treści (bez pierwszego nagłówka h1), jego tekst, spis treści i liczba słów."""

    html: str
    title: str
    headings: tuple[Heading, ...]
    words: int

    def toc(self) -> tuple[Heading, ...]:
        """Nagłówki do spisu treści (poziomy h2 i h3)."""
        return tuple(h for h in self.headings if h.level in TOC_LEVELS)


def slugify(text: str) -> str:
    """`id` nagłówka jak na GitHubie: małe litery, bez znaków innych niż litery, cyfry, `_` i `-`, spacje na myślniki."""
    folded = unicodedata.normalize("NFC", text).strip().lower()
    return re.sub(r"[^\w\- ]", "", folded).replace(" ", "-")


def _unique_slug(slug: str, used: dict[str, int]) -> str:
    """Powtórzony nagłówek dostaje `-1`, `-2`… (jak na GitHubie), żeby `id` były różne."""
    count = used.get(slug, 0)
    used[slug] = count + 1
    return slug if count == 0 else f"{slug}-{count}"


def _plain_text(inline: Token) -> str:
    """Tekst nagłówka bez znaczników: tekst i kod, bez łamań linii."""
    return "".join(c.content for c in inline.children or [] if c.type in ("text", "code_inline")).strip()


def classify_link(href: str, doc_routes: Mapping[str, str]) -> tuple[str, str]:
    """`(rodzaj, adres)` odnośnika: `anchor` (w tym pliku), `doc` (podstrona innego dokumentu), `external` albo `none`
    (nie prowadzi nigdzie, gdzie panel mógłby zaprowadzić: zostaje tekstem)."""
    if href.startswith("#"):
        return "anchor", unicodedata.normalize("NFC", href)
    if _SCHEME.match(href):
        return ("external", href) if href.lower().startswith(("http://", "https://")) else ("none", "")
    name, _, fragment = href.partition("#")
    route = doc_routes.get(name.rsplit("/", 1)[-1])
    if route is None:
        return "none", ""
    return "doc", route + (("#" + unicodedata.normalize("NFC", fragment)) if fragment else "")


def _rewrite_links(inline: Token, doc_routes: Mapping[str, str]) -> None:
    """Dostosowuje odnośniki w jednym akapicie; nieobsługiwane zamienia na `<span>` (razem ze znacznikiem zamykającym)."""
    plain = False
    for child in inline.children or []:
        if child.type == "link_open":
            kind, target = classify_link(str(child.attrGet("href") or ""), doc_routes)
            plain = kind == "none"
            if plain:
                child.tag, child.attrs = "span", {}
            else:
                child.attrSet("href", target)
                if kind == "external":
                    child.attrSet("target", "_blank")
                    child.attrSet("rel", "noopener noreferrer")
        elif child.type == "link_close" and plain:
            child.tag, plain = "span", False


def _add_anchor(inline: Token, slug: str) -> None:
    """Dopisuje do nagłówka odnośnik „#” (pojawia się po najechaniu); `slug` ma tylko znaki bezpieczne, ale i tak jest escapowany."""
    anchor = Token("html_inline", "", 0)
    anchor.content = _ANCHOR_HTML.format(slug=html.escape(slug, quote=True))
    (inline.children if inline.children is not None else []).append(anchor)


def _process_heading(tokens: list[Token], at: int, env: dict[str, Any]) -> bool:
    """Obsługuje nagłówek od `at`: pierwszy h1 trafia do `env["title"]` i znika z treści (zwraca True), reszta dostaje `id`."""
    opening, inline = tokens[at], tokens[at + 1]
    level, text = int(opening.tag[1:]), _plain_text(inline)
    if level == 1 and not env["title"]:
        env["title"] = text
        return True
    if level in ANCHOR_LEVELS:
        slug = _unique_slug(slugify(text), env["used"])
        opening.attrSet("id", slug)
        _add_anchor(inline, slug)
        env["headings"].append(Heading(level, text, slug))
    return False


def _core_rule(state: Any) -> None:
    """Reguła po parsowaniu bloków i tekstu: nagłówki (id, spis), odnośniki i liczba słów."""
    env, kept, at = state.env, [], 0
    tokens = state.tokens
    while at < len(tokens):
        token = tokens[at]
        if token.type == "heading_open" and _process_heading(tokens, at, env):
            at += 3  # heading_open, inline, heading_close
            continue
        if token.type == "inline":
            _rewrite_links(token, env["doc_routes"])
            env["words"] += len(_WORD.findall(token.content))
        elif token.type in ("fence", "code_block"):
            env["words"] += len(_WORD.findall(token.content))
        kept.append(token)
        at += 1
    state.tokens[:] = kept


def _wrap_table_open(*_args: Any) -> str:
    return '<div class="table-wrap doc-table"><table>\n'


def _wrap_table_close(*_args: Any) -> str:
    return "</table></div>\n"


def _wrap_fence(self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
    """Blok kodu z przyciskiem „Kopiuj”; jest ukryty, dopóki skrypt strony go nie odsłoni (bez skryptu nic by nie robił)."""
    return ('<div class="doc-code">' + self.fence(tokens, idx, options, env)
            + '<button type="button" class="btn doc-copy" data-copy-code hidden aria-label="Kopiuj blok kodu">Kopiuj</button></div>\n')


def _build_parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")
    parser.core.ruler.push("perno_docs", _core_rule)
    parser.add_render_rule("table_open", _wrap_table_open)
    parser.add_render_rule("table_close", _wrap_table_close)
    parser.add_render_rule("fence", _wrap_fence)
    return parser


_PARSER = _build_parser()  # bezstanowy, więc wspólny dla wątków serwera; stan renderowania jest w `env`


def render(text: str, doc_routes: Optional[Mapping[str, str]] = None) -> Rendered:
    """Renderuje Markdown. `doc_routes`: `{nazwa pliku: trasa w panelu}` dla odnośników między dokumentami."""
    env: dict[str, Any] = {"title": "", "headings": [], "used": {}, "words": 0, "doc_routes": doc_routes or {}}
    body = _PARSER.render(text, env)
    return Rendered(body, env["title"], tuple(env["headings"]), env["words"])
