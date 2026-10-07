"""Reguły treści maila icebreaker (zasady z templates/icebreaker.md) sprawdzane przed zapisem do kolejki.

Błąd zatrzymuje wiersz, ostrzeżenie tylko się wypisuje. Pełna kontrola dotyczy maili po polsku (szablon polski ma
formy Pan/Pani/Państwo); dla pozostałych języków sprawdzamy tylko znaki i formę adresu."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

MODES = ("Pan", "Pani", "Panstwo")
_MODE_ALIASES = {"państwo": "Panstwo", "panstwo": "Panstwo", "pan": "Pan", "pani": "Pani"}
_DASHES = re.compile("[—–]")
# Formy, których nie wolno użyć w tekście napisanym do danego adresata (inne niż jego własna forma).
_FOREIGN_FORMS = {
    "Pan": re.compile(r"\b(Pani\w*|Państw\w*)\b"),
    "Pani": re.compile(r"\b(Pan|Pana|Panu|Panem|Państw\w*)\b"),
    "Panstwo": re.compile(r"\b(Pan|Pana|Panu|Panem|Pani\w*)\b"),
}
SUBJECT_WORDS_ERROR = (2, 6)
SUBJECT_WORDS_WARNING = (3, 4)


@dataclass
class Findings:
    """Wynik kontroli jednego wpisu: błędy (blokują zapis) i ostrzeżenia."""
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def normalize_mode(raw: object) -> str:
    """„Państwo”/„panstwo” → „Panstwo”; brak wartości daje „Panstwo” (jak w `wypelnij.py`); nieznana wartość → ValueError."""
    text = str(raw or "").strip()
    if not text:
        return "Panstwo"
    mode = _MODE_ALIASES.get(text.lower())
    if mode is None:
        raise ValueError(f"mode „{text}” — dozwolone: Pan, Pani, Panstwo")
    return mode


def _words(text: str) -> int:
    return len(text.split())


def _check_polish(entry: Mapping, found: Findings) -> None:
    obs, pitch, channel = (str(entry.get(name) or "").strip() for name in ("obs", "pitch", "channel_summary"))
    if not obs.startswith("Szukałem "):
        found.errors.append("obs ma zaczynać się od „Szukałem …”")
    if "Zauważyłem, że" not in obs:
        found.errors.append("obs ma zawierać „Zauważyłem, że …”")
    if not obs.endswith("?") or " Czy " not in obs:
        found.errors.append("obs ma kończyć się pytaniem „Czy …?”")
    if not pitch.startswith("przy "):
        found.errors.append("pitch ma zaczynać się od „przy …” (miejscownik) i kończyć bezokolicznikiem")
    if re.search(r"płatno|platno", channel, re.IGNORECASE):
        found.errors.append("channel_summary bez płatności - szablon sam dopisuje „i osobno rozliczać płatności”")
    low, high = SUBJECT_WORDS_ERROR
    count = _words(str(entry.get("subject") or ""))
    if not low <= count <= high:
        found.errors.append(f"temat ma {count} słów - ma mieć 3-4")
    elif not SUBJECT_WORDS_WARNING[0] <= count <= SUBJECT_WORDS_WARNING[1]:
        found.warnings.append(f"temat ma {count} słów (zalecane 3-4)")
    season = str(entry.get("season") or "").strip()
    if season and (not season.endswith(".") or season.count(".") > 1):
        found.warnings.append("season ma być jednym zdaniem zakończonym kropką")


def check(entry: Mapping, language: str) -> Findings:
    """Błędy i ostrzeżenia dla wpisu `entry` (pola jak w tresci.json) przy mailu w języku `language`."""
    found = Findings()
    try:
        mode = normalize_mode(entry.get("mode"))
    except ValueError as exc:
        found.errors.append(str(exc))
        return found
    texts = {name: str(entry.get(name) or "") for name in ("subject", "obs", "pitch", "channel_summary", "season")}
    for name, text in texts.items():
        if _DASHES.search(text):
            found.errors.append(f"{name}: długi myślnik (— –) - użyj zwykłego „-”")
    if language == "pl":
        _check_polish(entry, found)
        for name, text in texts.items():
            match = _FOREIGN_FORMS[mode].search(text)
            if match:
                found.errors.append(f"{name}: „{match.group(0)}” nie pasuje do formy {mode} (jedna forma w całym mailu)")
    return found
