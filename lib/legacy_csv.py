"""Dawny format Maile.csv (19 pól) — używany tylko przez migrację i `--export-legacy`.

Dane o firmie (kraj, miasto, adres, opis) powstały później i w tym formacie ich nie ma."""

from __future__ import annotations

import csv
import os

from lib.db import LEGACY_COLUMNS


def read_rows(path: str) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_rows(path: str, rows: list) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LEGACY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in LEGACY_COLUMNS})
    os.replace(tmp_path, path)
