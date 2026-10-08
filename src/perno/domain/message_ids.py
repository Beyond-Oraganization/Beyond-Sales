"""Nagłówki `Message-ID`: tworzenie własnych identyfikatorów i ich porównywanie (czysta logika, tylko stdlib).

Wysłany mail dostaje własny `Message-ID`, zapisany w bazie bez nawiasów kątowych i małymi literami (`key`). Po nim
dopasowujemy odpowiedzi: `In-Reply-To` i `References` wskazują na identyfikatory wiadomości, na które firma odpowiada.
Małe litery, bo serwery pocztowe potrafią zmienić wielkość liter, a losowy identyfikator i tak jest unikalny.
"""

from __future__ import annotations

import hashlib
import re
from email.utils import make_msgid

_ID = re.compile(r"<([^<>\s]{1,998})>")
MAX_KEY = 255  # tyle mieści kolumna `maile.message_id`; własne identyfikatory są krótsze


def new_message_id(sender_address: str) -> str:
    """Nowy, unikalny `Message-ID` (z nawiasami kątowymi, gotowy do nagłówka) w domenie adresu nadawcy."""
    domain = sender_address.rpartition("@")[2].strip().lower()
    return make_msgid(domain=domain or None)


def key(message_id: str) -> str:
    """Postać porównywalna jednego identyfikatora: bez nawiasów, małymi literami; pusta, gdy tekst nie wygląda na `Message-ID`."""
    found = _ID.search(message_id if "<" in message_id else f"<{message_id.strip()}>")
    return found.group(1).lower() if found else ""


def keys_in(header: str) -> list:
    """Wszystkie identyfikatory z nagłówka `References` albo `In-Reply-To` (kolejność jak w nagłówku, bez powtórzeń)."""
    seen: dict = {}
    for match in _ID.finditer(header or ""):
        seen.setdefault(match.group(1).lower(), None)
    return list(seen)


def digest(message_key: str) -> str:
    """Skrót (SHA-1, 40 znaków) identyfikatora do unikalnego indeksu: `Message-ID` z cudzych maili bywa bardzo długi."""
    return hashlib.sha1(message_key.encode("utf-8", "replace")).hexdigest()  # noqa: S324 — skrót do indeksu, nie bezpieczeństwo


def identity(message_key: str, sender: str, when_iso: str, subject: str, body_head: str) -> str:
    """Tożsamość wiadomości do wykrywania duplikatów: jej `Message-ID`, a gdy go brak (zdarza się), skrót z nadawcy, daty,
    tematu i początku treści, żeby ta sama wiadomość pobrana drugi raz nie powstała podwójnie."""
    if message_key:
        return message_key
    return "brak-id:" + "|".join((sender.strip().lower(), when_iso, subject.strip(), body_head[:200]))
