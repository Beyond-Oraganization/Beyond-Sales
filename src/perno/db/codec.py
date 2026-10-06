"""Zamiana wiersza bazy na JSON panelu i z powrotem.

Kontrakt z panelem (JSON): pola tekstowe jak w starym CSV, NULL → '' i daty jako ISO 'RRRR-MM-DDTGG:MM:SS'.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from perno.db.errors import InvalidValue
from perno.db.schema import COLUMNS, EXTRA_COLUMNS
from perno.domain import countries

_INT_FIELDS = ("row_number", "retry_count")
_DT_FIELDS = ("generated_at", "sent_at")
_REQUIRED = ("id", "segment", "status")
_MAX_LEN = {
    "id": 32, "segment": 32, "company": 255, "contact_person": 255, "email": 320,
    "website": 1000, "tag": 255, "mode": 16, "subject": 500, "status": 32,
    "inline_images": 255, "body_html": 1_000_000, "body_text": 1_000_000,
    "attachments": 60_000, "error": 60_000, "city": 120, "address": 500, "description": 5_000,
}


def format_value(value) -> str:
    """Wartość z bazy jako tekst panelu: NULL → '', data → ISO bez ułamków."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return str(value)


def to_api(row: Mapping) -> dict:
    """Wiersz API: pola `COLUMNS` oraz pola wyliczane (`EXTRA_COLUMNS`), jeśli odczyt je dołączył."""
    api = {name: format_value(row[name]) for name in COLUMNS}
    api.update({name: format_value(row[name]) for name in EXTRA_COLUMNS if name in row})
    return api


def _parse_dt(value: str, name: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise InvalidValue(f"Pole {name}: nieprawidłowa data {value!r} (oczekiwano RRRR-MM-DDTGG:MM:SS).")


def from_api(data: Mapping, *, partial: bool = False) -> dict:
    out: dict[str, Any] = {}
    for name in COLUMNS:
        if name not in data:
            if partial:
                continue
            value = ""
        else:
            value = data[name]
        if value is None:
            value = ""
        if not isinstance(value, str):
            value = str(value)

        if value == "":
            if name in _REQUIRED:
                raise InvalidValue(f"Pole {name} jest wymagane.")
            out[name] = 0 if name == "retry_count" else None
            continue

        if name == "country":
            code = countries.normalize_country(value)
            if code is None:
                raise InvalidValue(
                    f"Pole country: nieznany kraj {value[:40]!r} — podaj nazwę (np. „Polska”) albo kod ISO (np. PL).")
            out[name] = code
            continue

        limit = _MAX_LEN.get(name)
        if limit is not None and len(value) > limit:
            raise InvalidValue(f"Pole {name} jest za długie (maks. {limit} znaków).")
        if name in _INT_FIELDS:
            try:
                out[name] = int(value)
            except ValueError:
                raise InvalidValue(f"Pole {name}: {value!r} nie jest liczbą.") from None
        elif name in _DT_FIELDS:
            out[name] = _parse_dt(value, name)
        else:
            out[name] = value
    return out
