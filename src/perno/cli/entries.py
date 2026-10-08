"""Wczytywanie plików JSON z wpisami dla poleceń `ai.py` (maile, oceny, szkice): lista obiektów albo jeden obiekt."""

from __future__ import annotations

import json

from perno.db import DbError


def read_entries(path: str) -> list:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise DbError("Plik musi zawierać listę obiektów (jeden na wpis).")
    return data
