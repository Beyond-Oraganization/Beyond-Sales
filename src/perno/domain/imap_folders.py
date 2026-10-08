"""Foldery skrzynki IMAP: rozpoznanie ról (Odebrane, Wysłane, Spam…) i wybór folderów do przeszukania (czysta logika).

Serwery nazywają foldery różnie („Sent”, „Wysłane”, „INBOX.Sent”, „Elementy wysłane”), ale większość podaje flagę
SPECIAL-USE (`\\Sent`, `\\Junk`). Flaga ma pierwszeństwo, nazwa jest tylko zapasem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

ROLE_INBOX = "inbox"
ROLE_SENT = "sent"
ROLE_JUNK = "junk"
ROLE_OTHER = ""

_FLAG_ROLES = {"\\sent": ROLE_SENT, "\\junk": ROLE_JUNK}
_NAME_ROLES = (
    (ROLE_SENT, ("sent", "sent items", "sent mail", "wysłane", "wyslane", "elementy wysłane", "gesendet", "gesendete elemente", "enviados")),
    (ROLE_JUNK, ("junk", "spam", "junk e-mail", "junk email", "bulk mail", "wiadomości-śmieci", "niechciane", "correo no deseado")),
)
_NO_SELECT = "\\noselect"


@dataclass(frozen=True)
class Folder:
    """Folder skrzynki: nazwa (po zdekodowaniu), flagi z serwera i rozpoznana rola."""

    name: str
    flags: tuple = ()
    role: str = ROLE_OTHER


def _leaf(name: str, delimiter: str) -> str:
    return name.rsplit(delimiter, 1)[-1] if delimiter and delimiter in name else name


def classify(name: str, flags: Iterable[str] = (), delimiter: str = ".") -> str:
    """Rola folderu: z flagi SPECIAL-USE, a gdy jej brak, z nazwy (ostatni człon ścieżki, wielkość liter bez znaczenia)."""
    lowered = [flag.lower() for flag in flags]
    for flag, role in _FLAG_ROLES.items():
        if flag in lowered:
            return role
    if name.upper() == "INBOX":
        return ROLE_INBOX
    leaf = _leaf(name, delimiter).strip().lower()
    return next((role for role, names in _NAME_ROLES if leaf in names), ROLE_OTHER)


_LIST_LINE = re.compile(r'\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)', re.DOTALL)


def parse_list_line(line: str) -> Optional[tuple]:
    """Jedna linia odpowiedzi LIST → (nazwa zakodowana, flagi, separator); None, gdy linia ma inną postać."""
    match = _LIST_LINE.fullmatch(line.strip())
    if match is None:
        return None
    delimiter = match["delim"]
    delimiter = "" if delimiter.upper() == "NIL" else delimiter[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    name = match["name"].strip()
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return name, tuple(match["flags"].split()), delimiter


def default_folders(folders: Sequence[Folder]) -> list:
    """Foldery, które przeszukujemy domyślnie: Odebrane, Wysłane (tam trafiają odpowiedzi pisane w zwykłym kliencie poczty)
    i Spam (odpowiedzi czasem tam lądują). Pomija foldery bez możliwości otwarcia i powtórzenia ról."""
    chosen: dict = {}
    for folder in folders:
        if _NO_SELECT in (flag.lower() for flag in folder.flags):
            continue
        if folder.role in (ROLE_INBOX, ROLE_SENT, ROLE_JUNK) and folder.role not in chosen:
            chosen[folder.role] = folder.name
    return [chosen[role] for role in (ROLE_INBOX, ROLE_SENT, ROLE_JUNK) if role in chosen]


def parse_selection(raw: str) -> list:
    """Lista folderów z ustawienia `REPLIES_FOLDERS` (nazwy rozdzielone przecinkami; puste = wykryj automatycznie)."""
    seen: dict = {}
    for part in raw.split(","):
        name = part.strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)
