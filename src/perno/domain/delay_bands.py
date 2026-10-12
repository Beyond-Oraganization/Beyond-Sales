"""Przedziały odstępu między mailami: lista (od, do, prawdopodobieństwo) i losowanie czasu oczekiwania.

Automat po każdym mailu losuje przedział według jego prawdopodobieństwa (np. 1–2 min w 50% przypadków, 5–10 min w 40%, 20–60 min
w 10%), a w wybranym przedziale losuje czas jednostajnie. Czysta logika bez zegara i generatora: losowość wchodzi jako
argumenty, więc testy sprawdzają dokładne wyniki. Błędy walidacji mają komunikat po polsku dla użytkownika.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

MIN_DELAY = 10.0  # sekund; poniżej tego odstęp przestaje przypominać człowieka
MAX_DELAY = 3600.0
MAX_BANDS = 100  # sufit techniczny (JSON w bazie, formularz), nie ograniczenie użytkowe
PERCENT_TOTAL = 100.0
PERCENT_TOLERANCE = 0.01  # tyle może brakować do 100 (np. 33,3 + 33,3 + 33,4 w liczbach zmiennoprzecinkowych)

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(s|sek|m|min)?")
_UNIT_SECONDS = {None: 1.0, "s": 1.0, "sek": 1.0, "m": 60.0, "min": 60.0}


class BandsInvalid(ValueError):
    """Przedziały odstępu są nieprawidłowe; komunikat po polsku jest dla użytkownika."""


@dataclass(frozen=True)
class DelayBand:
    """Przedział `[low, high]` sekund, wybierany w `percent` procentach wysyłek."""
    low: float
    high: float
    percent: float

    def to_json(self) -> dict:
        return {"min": self.low, "max": self.high, "percent": self.percent}


def single(low: float, high: float) -> tuple:
    """Jeden przedział wybierany zawsze (dawne ustawienie „odstęp min–max”)."""
    return (DelayBand(low, high, PERCENT_TOTAL),)


def seconds_in_range(raw: object, label: str) -> float:
    """Liczba sekund z zakresu dozwolonych odstępów (odrzuca wartości nieliczbowe, wartości logiczne, NaN i nieskończoność)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise BandsInvalid(f"{label} musi być liczbą sekund.")
    try:
        number = float(raw)
    except ValueError:
        raise BandsInvalid(f"{label} musi być liczbą sekund.") from None
    if not MIN_DELAY <= number <= MAX_DELAY:
        raise BandsInvalid(f"{label} poza zakresem ({MIN_DELAY:g}–{MAX_DELAY:g} s).")
    return number


def _percent(raw: object, label: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise BandsInvalid(f"{label}: prawdopodobieństwo musi być liczbą procent.")
    try:
        number = float(raw)
    except ValueError:
        raise BandsInvalid(f"{label}: prawdopodobieństwo musi być liczbą procent.") from None
    if not 0 < number <= PERCENT_TOTAL:  # fałsz także dla NaN
        raise BandsInvalid(f"{label}: prawdopodobieństwo musi być większe od 0 i nie większe niż 100%.")
    return number


def _band(raw: object, number: int) -> DelayBand:
    label = f"Przedział {number}"
    if not isinstance(raw, Mapping) or not {"min", "max", "percent"} <= raw.keys():
        raise BandsInvalid(f"{label}: podaj „od”, „do” i prawdopodobieństwo.")
    low = seconds_in_range(raw["min"], f"{label}: „od”")
    high = seconds_in_range(raw["max"], f"{label}: „do”")
    if low > high:
        raise BandsInvalid(f"{label}: „od” nie może być większe niż „do”.")
    return DelayBand(low, high, _percent(raw["percent"], label))


def from_json(raw: object) -> tuple:
    """Przedziały z listy obiektów `{"min", "max", "percent"}` (sekundy i procenty). Rzuca `BandsInvalid`, gdy lista jest
    pusta, ma zły kształt, przedział jest poza zakresem albo prawdopodobieństwa nie dają razem 100%."""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise BandsInvalid("Dodaj co najmniej jeden przedział odstępu między mailami.")
    if len(raw) > MAX_BANDS:
        raise BandsInvalid(f"Za dużo przedziałów (najwyżej {MAX_BANDS}).")
    bands = tuple(_band(item, number) for number, item in enumerate(raw, 1))
    total = sum(band.percent for band in bands)
    if abs(total - PERCENT_TOTAL) > PERCENT_TOLERANCE:
        raise BandsInvalid(f"Prawdopodobieństwa przedziałów muszą dawać razem 100% (teraz: {total:g}%).")
    return bands


def to_json(bands: Sequence[DelayBand]) -> list:
    """Przedziały jako lista obiektów `{"min", "max", "percent"}` (zapis w bazie i w API)."""
    return [band.to_json() for band in bands]


def pick(bands: Sequence[DelayBand], roll: float, uniform: Callable[[float, float], float]) -> float:
    """Losowy czas oczekiwania w sekundach. `roll` z [0, 1) wybiera przedział proporcjonalnie do prawdopodobieństw (0 = pierwszy
    przedział, tuż poniżej 1 = ostatni), a `uniform(low, high)` losuje czas w tym przedziale."""
    point = roll * sum(band.percent for band in bands)
    upto = 0.0
    for band in bands:
        upto += band.percent
        if point < upto:
            return uniform(band.low, band.high)
    return uniform(bands[-1].low, bands[-1].high)  # roll == 1 albo zaokrąglenie na końcu sumy


def overall_range(bands: Sequence[DelayBand]) -> tuple:
    """Najmniejsze „od” i największe „do” ze wszystkich przedziałów (dawne pola `delay_min` i `delay_max`)."""
    return min(band.low for band in bands), max(band.high for band in bands)


def mean_seconds(bands: Sequence[DelayBand]) -> float:
    """Oczekiwany odstęp między mailami: średnia środków przedziałów ważona prawdopodobieństwami."""
    total = sum(band.percent for band in bands)
    return sum(band.percent * (band.low + band.high) / 2 for band in bands) / total


# ---- zapis tekstowy (CLI) ---------------------------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    """„45 s”, „2 min”, „1 min 30 s” — czas w zapisie dla człowieka."""
    if seconds < 60:
        return f"{seconds:g} s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min" + (f" {rest} s" if rest else "")


def format_band(band: DelayBand) -> str:
    """Jeden przedział dla człowieka: „1 min – 2 min: 50%”."""
    return f"{format_duration(band.low)} – {format_duration(band.high)}: {band.percent:g}%"


def _duration(text: str, label: str) -> float:
    match = _DURATION_RE.fullmatch(text.strip().lower())
    if not match:
        raise BandsInvalid(f"{label}: nie rozumiem czasu {text.strip()!r} (użyj np. 90, 90s, 2m, 1.5min).")
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def parse_text(text: str) -> tuple:
    """Przedziały z zapisu `od-do=procent` rozdzielonego przecinkami, np. `1m-2m=50,5m-10m=40,20m-60m=10`. Czas to liczba
    sekund z opcjonalnym `s` albo `m`/`min` (minuty), procent może mieć `%`. Waliduje jak `from_json`."""
    raw = []
    for number, part in enumerate((chunk for chunk in text.split(",") if chunk.strip()), 1):
        label = f"Przedział {number}"
        span, sign, percent = part.partition("=")
        low, dash, high = span.partition("-")
        if not sign or not dash:
            raise BandsInvalid(f"{label}: zapis ma postać od-do=procent (np. 1m-2m=50), a podano {part.strip()!r}.")
        raw.append({"min": _duration(low, label), "max": _duration(high, label), "percent": percent.strip().rstrip("%").strip()})
    return from_json(raw)

