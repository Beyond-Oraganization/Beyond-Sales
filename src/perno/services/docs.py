"""Dokumenty projektu pokazywane w panelu jako podstrony oraz polecenia dla AI wyciągnięte z instrukcji.

Pliki czytamy z dysku przy każdym wejściu na stronę i renderujemy ponownie tylko wtedy, gdy plik się zmienił (czas i rozmiar),
więc poprawka w `README.md` albo `AI_INSTRUCTIONS.md` jest widoczna od razu, bez restartu panelu.
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Optional

from perno.services import markdown_render

WORDS_PER_MINUTE = 200  # tempo czytania do szacunku „ok. N min”
INSTRUCTIONS_KEY = "instrukcje"  # dokument, z którego pochodzą polecenia dla AI


@dataclass(frozen=True)
class DocSpec:
    """Dokument w panelu: klucz strony, trasa, plik względem katalogu projektu i opis na kafelku."""

    key: str
    path: str
    file: str
    title: str
    blurb: str


DOCS: tuple[DocSpec, ...] = (
    DocSpec("instrukcje", "/instrukcje", "AI_INSTRUCTIONS.md", "Instrukcje dla AI",
            "Polecenia dla Claude Code: szukanie firm, treść maili, odpowiedzi i leady, razem z regułami, których AI nie łamie."),
    DocSpec("dokumentacja", "/dokumentacja", "README.md", "Dokumentacja",
            "Instalacja, baza danych, panel, skraper, Automat wysyłki, konfiguracja i testy."),
    DocSpec("zasady-maili", "/zasady-maili", "templates/icebreaker.md", "Zasady pisania maili",
            "Jak napisać pola pierwszego maila: temat, obserwację, propozycję i formę grzecznościową."),
    DocSpec("zasady-projektu", "/zasady-projektu", "CLAUDE.md", "Zasady projektu",
            "Reguły pracy nad kodem: warstwy, bezpieczeństwo wysyłki, testy i styl."),
)


def spec_for(key: str) -> DocSpec:
    """Dokument o danym kluczu (KeyError, gdy go nie ma w rejestrze)."""
    return next(spec for spec in DOCS if spec.key == key)


def attach_reference(spec: DocSpec, base_dir: str, root_dir: str) -> str:
    """`@katalog/plik.md`: tak dołącza się plik do rozmowy z Claude Code (ścieżka względem katalogu, w którym działa Claude Code)."""
    return "@" + os.path.relpath(os.path.join(base_dir, spec.file), root_dir).replace(os.sep, "/")


def doc_routes() -> dict[str, str]:
    """`{nazwa pliku: trasa}`: do zamiany odnośników między dokumentami na podstrony panelu."""
    return {os.path.basename(spec.file): spec.path for spec in DOCS}


def plural_pl(count: int, one: str, few: str, many: str) -> str:
    """Liczba z rzeczownikiem w polskiej odmianie: 1 sekcja, 2 sekcje, 5 sekcji, 12 sekcji, 22 sekcje."""
    if count == 1:
        return f"{count} {one}"
    few_form = 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14
    return f"{count} {few if few_form else many}"


@dataclass(frozen=True)
class Document:
    """Wczytany dokument: opis, wynik renderowania i surowy tekst (z niego czytamy polecenia)."""

    spec: DocSpec
    rendered: markdown_render.Rendered
    text: str

    @property
    def sections(self) -> int:
        """Liczba sekcji (nagłówków drugiego poziomu)."""
        return sum(1 for heading in self.rendered.headings if heading.level == 2)

    @property
    def minutes(self) -> int:
        """Szacowany czas czytania w minutach (co najmniej 1)."""
        return max(1, round(self.rendered.words / WORDS_PER_MINUTE))

    @property
    def sections_label(self) -> str:
        """„12 sekcji”: liczba sekcji w polskiej odmianie."""
        return plural_pl(self.sections, "sekcja", "sekcje", "sekcji")

    @property
    def minutes_label(self) -> str:
        """„ok. 25 min”: szacowany czas czytania."""
        return f"ok. {self.minutes} min"


_CACHE: dict[str, tuple[tuple[int, int], Document]] = {}
_LOCK = threading.Lock()


def load(spec: DocSpec, base_dir: str) -> Optional[Document]:
    """Dokument z katalogu projektu albo None, gdy pliku nie ma lub nie da się go odczytać."""
    path = os.path.join(base_dir, spec.file)
    try:
        stat = os.stat(path)
    except OSError:
        return None
    signature = (stat.st_mtime_ns, stat.st_size)
    with _LOCK:
        cached = _CACHE.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    document = Document(spec, markdown_render.render(text, doc_routes()), text)
    with _LOCK:
        _CACHE[path] = (signature, document)
    return document


@dataclass(frozen=True)
class Command:
    """Polecenie dla AI z tabeli w instrukcji: tekst z miejscami do uzupełnienia w `[nawiasach]` i sekcja z opisem."""

    text: str
    section: str
    anchor: str


# wiersz tabeli poleceń: | `POLECENIE [parametr]` / `INNE` | [numer sekcji](#id-nagłówka) |
_COMMAND_ROW = re.compile(r"^\|(?P<cells>[^|]*`[^|]*)\|\s*\[(?P<number>\d+)\]\(#(?P<anchor>[^)\s]+)\)\s*\|$")
_CODE_SPAN = re.compile(r"`([^`]+)`")


def commands(markdown: str) -> tuple[Command, ...]:
    """Polecenia z tabeli „Komenda użytkownika | Sekcja” na początku instrukcji; komórka z kilkoma kodami (`A` / `B`) daje kilka poleceń."""
    found: list[Command] = []
    for line in markdown.splitlines():
        row = _COMMAND_ROW.match(line.strip())
        if row is None:
            continue
        anchor = unicodedata.normalize("NFC", row["anchor"])
        found.extend(Command(text.strip(), row["number"], anchor) for text in _CODE_SPAN.findall(row["cells"]))
    return tuple(found)
