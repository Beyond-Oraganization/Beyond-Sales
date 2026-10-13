"""Reguły leadów (czysta logika): pola, etapy, walidacja wartości i pochodzenie pól (AI albo człowiek).

Lead to firma, która odpowiedziała pozytywnie lub neutralnie (`LEAD_RATINGS`). Jego pola wypełnia głównie AI (`ai.py leady`),
a człowiek poprawia je w panelu. Reguła jest ta sama co przy ocenach odpowiedzi: AI nie nadpisuje pola, które wpisał człowiek.
Pochodzenie pól trzyma słownik `src` ({pole: "ai" | "human"}; brak klucza = wartość z bazy albo z zasiewu).
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from perno.domain import replies as rp
from perno.domain.reply_rules import RuleError

LEAD_RATINGS = (rp.RATING_POSITIVE, rp.RATING_NEUTRAL)  # oceny, od których firma jest leadem

NAME, WEBSITE, CONTACT, PHONE, EMAIL, ABOUT, NOTE, STAGE = ("name", "website", "contact", "phone", "email", "about", "note", "stage")
TEXT_FIELDS = (NAME, WEBSITE, CONTACT, PHONE, EMAIL, ABOUT, NOTE)
EDITABLE_FIELDS = (*TEXT_FIELDS, STAGE)  # co zmienia człowiek w panelu
AI_FIELDS = (CONTACT, PHONE, EMAIL, ABOUT)  # co wolno wpisać AI; reszta (firma, strona, notatka, etap) jest od człowieka albo z zasiewu
REQUIRED_FIELDS = (NAME, WEBSITE, CONTACT, EMAIL, ABOUT)  # lead jest kompletny, gdy ma je wszystkie (telefon jest dodatkiem)
LABELS = {NAME: "Firma", WEBSITE: "Strona", CONTACT: "Kontakt", PHONE: "Telefon", EMAIL: "E-mail", ABOUT: "Czym się zajmuje",
          NOTE: "Notatka", STAGE: "Etap"}
LIMITS = {NAME: 255, WEBSITE: 500, CONTACT: 255, PHONE: 64, EMAIL: 320, ABOUT: 400, NOTE: 2000}
MULTILINE = (NOTE,)  # tylko notatka może mieć kilka linii

STAGE_NEW, STAGE_TALKING, STAGE_MEETING, STAGE_WON, STAGE_LOST = "new", "talking", "meeting", "won", "lost"
STAGES = (STAGE_NEW, STAGE_TALKING, STAGE_MEETING, STAGE_WON, STAGE_LOST)
STAGE_LABELS = {STAGE_NEW: "Nowy", STAGE_TALKING: "W rozmowie", STAGE_MEETING: "Umówione", STAGE_WON: "Wygrany", STAGE_LOST: "Odpadł"}

BY_HUMAN, BY_AI = rp.BY_HUMAN, rp.BY_AI

_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_BLANKS = re.compile(r"[ \t\f\v ]+")
_EMAIL = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]{2,}$")
_PHONE_CHARS = re.compile(r"^\+?[\d\s().\-/]+$")
_HOST = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")
_URL = re.compile(r"^https?://([^/?#\s@]+)(?:[/?#]\S*)?$", re.IGNORECASE)


def clean_text(field: str, value: Any) -> str:
    """Tekst pola do zapisu: bez znaków sterujących, białe znaki zwinięte (notatka zachowuje podziały linii). Nie-tekst to błąd,
    za długi tekst też (ucinanie po cichu zgubiłoby dane)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RuleError(f"Pole {LABELS[field]} musi być tekstem.")
    text = _CONTROL.sub(" ", value.replace("\r\n", "\n").replace("\r", "\n"))
    if field in MULTILINE:
        text = "\n".join(_BLANKS.sub(" ", line).strip() for line in text.split("\n")).strip()
    else:
        text = _BLANKS.sub(" ", text.replace("\n", " ")).strip()
    if len(text) > LIMITS[field]:
        raise RuleError(f"Pole {LABELS[field]} może mieć najwyżej {LIMITS[field]} znaków (jest {len(text)}).")
    return text


def check_email(value: str) -> str:
    """Adres e-mail małymi literami albo pusty tekst; zły kształt to błąd."""
    if value and not _EMAIL.match(value):
        raise RuleError("E-mail ma niepoprawny kształt.")
    return value.lower()


def check_phone(value: str) -> str:
    """Telefon: cyfry, spacje, `+ ( ) . - /`, co najmniej 7 cyfr (albo pusty)."""
    if value and (not _PHONE_CHARS.match(value) or sum(ch.isdigit() for ch in value) < 7):
        raise RuleError("Telefon ma niepoprawny kształt (cyfry, spacje i znaki + ( ) - ., co najmniej 7 cyfr).")
    return value


def check_website(value: str) -> str:
    """Adres strony jako http(s) z nazwą domeny albo pusty tekst (bez schematu dopisujemy `https://`)."""
    if not value:
        return ""
    candidate = value if "://" in value else "https://" + value
    match = _URL.match(candidate)
    if not match or not _HOST.match(match.group(1).lower().split(":")[0]):
        raise RuleError("Strona musi być adresem http(s) z nazwą domeny.")
    return candidate


def check_stage(value: Any) -> str:
    """Etap leada: jeden z `STAGES`."""
    if value not in STAGES:
        raise RuleError(f"Etap musi być jednym z: {', '.join(STAGES)}.")
    return str(value)


def check_field(field: str, value: Any) -> str:
    """Wartość jednego pola po oczyszczeniu i sprawdzeniu kształtu (puste pole jest dozwolone)."""
    if field == STAGE:
        return check_stage(value)
    text = clean_text(field, value)
    if field == EMAIL:
        return check_email(text)
    if field == PHONE:
        return check_phone(text)
    if field == WEBSITE:
        return check_website(text)
    return text


def check_changes(changes: Any, *, by: str) -> dict:
    """Zmiany pól do zapisu: słownik {pole: wartość}. Człowiek zmienia `EDITABLE_FIELDS`, AI tylko `AI_FIELDS`; nieznane pole,
    zły typ albo zły kształt wartości to błąd. Puste zmiany też (nic do zapisania)."""
    if not isinstance(changes, Mapping) or not changes:
        raise RuleError("Brak zmian do zapisania.")
    allowed = AI_FIELDS if by == BY_AI else EDITABLE_FIELDS
    unknown = [str(name) for name in changes if name not in allowed]
    if unknown:
        raise RuleError(f"Pola niedozwolone: {', '.join(unknown)}.")
    return {name: check_field(name, value) for name, value in changes.items()}


def parse_src(text: Optional[str]) -> dict:
    """Pochodzenie pól z kolumny `src`; uszkodzony zapis traktujemy jak brak informacji (pola z bazy)."""
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: by for name, by in data.items() if name in TEXT_FIELDS and by in (BY_AI, BY_HUMAN)}


def dump_src(src: Mapping) -> Optional[str]:
    """Pochodzenie pól do kolumny `src` (None, gdy nic nie trzeba zapisywać)."""
    clean = {name: src[name] for name in TEXT_FIELDS if src.get(name) in (BY_AI, BY_HUMAN)}
    return json.dumps(clean, ensure_ascii=True, separators=(",", ":"), sort_keys=True) if clean else None


def ai_may_write(src: Mapping, field: str) -> bool:
    """AI uzupełnia puste pole i poprawia własne wcześniejsze wpisy, ale nie rusza tego, co wpisał albo zatwierdził człowiek."""
    return field in AI_FIELDS and src.get(field) != BY_HUMAN


def merge_src(src: Mapping, changes: Mapping, by: str) -> dict:
    """Pochodzenie po zapisie `changes` (pole → nowa wartość). Wpis ustawia autora; pole wyczyszczone wraca do „z bazy” (bez klucza),
    więc AI może je znów uzupełnić."""
    merged = dict(src)
    for name, value in changes.items():
        if name not in TEXT_FIELDS:
            continue
        if value:
            merged[name] = by
        else:
            merged.pop(name, None)
    return merged


def pending_fields(src: Mapping) -> list:
    """Pola wpisane przez AI, których człowiek jeszcze nie zatwierdził."""
    return [name for name in TEXT_FIELDS if src.get(name) == BY_AI]


def missing_fields(values: Mapping) -> list:
    """Które z pól wymaganych są puste (kolejność jak w `REQUIRED_FIELDS`)."""
    return [name for name in REQUIRED_FIELDS if not str(values.get(name) or "").strip()]


def completeness(values: Mapping) -> int:
    """Ile z `REQUIRED_FIELDS` jest wypełnionych (0-5)."""
    return len(REQUIRED_FIELDS) - len(missing_fields(values))


def looks_like_person(display_name: str, company: str) -> bool:
    """Czy nazwa nadawcy z nagłówka nadaje się na kontakt: niepusta, bez adresu i cyfr, krótka i niebędąca nazwą firmy (ani jej częścią,
    ani jej rozszerzeniem: „BusCharter” przy firmie „BusCharter Śląsk” to marka, nie osoba)."""
    name = " ".join((display_name or "").split())
    if not name or len(name) > 80 or "@" in name or any(ch.isdigit() for ch in name):
        return False
    brand = " ".join((company or "").split()).casefold()
    return bool(name.casefold()) and not (brand and (name.casefold() in brand or brand in name.casefold()))
