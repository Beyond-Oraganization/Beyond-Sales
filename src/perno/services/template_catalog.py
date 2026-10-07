"""Katalog szablonów maili z folderu templates/: nazwy, języki i pola, które musi dostarczyć autor treści.

Szablon to `nazwa.html` (polski) oraz `nazwa.xx.html` (inne języki). Pola do napisania wynikają z tokenów `{{…}}`
w szablonie, więc nowy plik wrzucony do katalogu jest od razu widoczny, a lista pól zawsze zgadza się z szablonem.
Opis zasad pisania pól leży obok jako `nazwa.md` (jedno miejsce prawdy dla autora treści)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from perno.domain import localize, templating

SYSTEM_TOKENS = frozenset({"sender_name", "inline_image_cid"})  # podstawia program, nie autor treści
OPTIONAL_FIELDS = frozenset({"season"})  # bez tej wartości akapit znika z maila
WRITABLE_FIELDS = frozenset({"obs", "pitch", "channel_summary", "season"})  # pola, które umie złożyć `generation.render_row`
_FILE = re.compile(r"^(?P<name>[a-z0-9_]+?)(?:\.(?P<lang>[a-z]{2}))?\.html$")


class UnknownTemplate(KeyError):
    """Nie ma takiego szablonu w katalogu templates/."""


@dataclass(frozen=True)
class TemplateInfo:
    """Szablon: nazwa, języki, pola do napisania (`fields` to pola obowiązkowe, `optional` pozostałe) i plik z zasadami."""
    name: str
    languages: tuple
    fields: tuple
    optional: tuple
    guide: Optional[str]

    @property
    def writable(self) -> bool:
        """Czy `ai.py zapisz` umie złożyć z niego mail pierwszego kontaktu (tylko znane pola autora)."""
        return set(self.fields) | set(self.optional) <= WRITABLE_FIELDS


def templates_dir() -> str:
    """Katalog szablonów projektu (templates/)."""
    return os.path.dirname(localize.template_path("x", "pl"))


def author_tokens(template: str) -> set:
    """Tokeny szablonu, które wypełnia autor treści (bez systemowych i bez form Pan/Pani/Państwo `f_*`)."""
    return {token for token in templating.TOKEN_RE.findall(template)
            if token not in SYSTEM_TOKENS and not token.startswith("f_")}


def discover(directory: Optional[str] = None) -> list:
    """Szablony z katalogu, posortowane po nazwie; pola liczone z wersji polskiej (albo pierwszej dostępnej)."""
    directory = directory or templates_dir()
    found: dict = {}
    for file_name in sorted(os.listdir(directory)):
        match = _FILE.match(file_name)
        if not match or (match["lang"] and match["lang"] not in localize.LANGUAGE_NAMES):
            continue
        found.setdefault(match["name"], {})[match["lang"] or "pl"] = os.path.join(directory, file_name)
    return [_info(name, files, directory) for name, files in sorted(found.items())]


def _info(name: str, files: dict, directory: str) -> TemplateInfo:
    reference = files.get("pl") or next(iter(files.values()))
    with open(reference, encoding="utf-8") as handle:
        tokens = author_tokens(handle.read())
    guide = os.path.join(directory, f"{name}.md")
    languages = tuple(code for code in localize.LANGUAGE_NAMES if code in files)
    return TemplateInfo(name, languages, tuple(sorted(tokens - OPTIONAL_FIELDS)),
                        tuple(sorted(tokens & OPTIONAL_FIELDS)), guide if os.path.exists(guide) else None)


def get(name: str, directory: Optional[str] = None) -> TemplateInfo:
    """Szablon o danej nazwie albo `UnknownTemplate`."""
    for info in discover(directory):
        if info.name == name:
            return info
    raise UnknownTemplate(name)


def read_guide(name: str, directory: Optional[str] = None) -> str:
    """Treść pliku z zasadami szablonu albo pusty tekst, gdy go nie ma."""
    info = get(name, directory)
    if not info.guide:
        return ""
    with open(info.guide, encoding="utf-8") as handle:
        return handle.read()
