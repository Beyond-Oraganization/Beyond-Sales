"""Zbiór danych ze skanu „bez bazy": katalog `data/skrap/<nazwa>/` z plikami CSV i tekstem stron.

  witryny.csv   jedna linia na witrynę: status skanu, tytuł, opis, e-maile, telefony, social, formularz
  strony.csv    jedna linia na odwiedzoną stronę: adres, głębokość, HTTP, tytuł, liczba znaków
  tekst/<domena>.txt  tekst stron (nagłówek `#### adres | tytuł`, potem treść) - stąd czyta `szukaj` i `wyciagnij`
  meta.json     parametry skanu i postęp

Treść pochodzi z cudzych stron, więc jest niezaufana: wartości w CSV są zabezpieczone przed formułami arkusza, a linie
tekstu zaczynające się od `####` dostają wiodącą spację, żeby strona nie mogła podrobić nagłówka innej strony."""

from __future__ import annotations

import csv
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Optional
from urllib.parse import urlsplit

from perno import settings as cfg
from perno.scraper import statuses as st
from perno.scraper.memory_store import SiteResult

DATASETS_DIR = os.path.join(cfg.BASE_DIR, "data", "skrap")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,59}$")
HEADER = "#### "
MAX_PAGE_CHARS = 50_000  # tekst jednej strony w pliku (reszta jest przycinana)
MAX_PER_KIND = {"email": 5, "telefon": 5, "social": 6, "formularz": 2}
SITE_COLUMNS = ("domena", "adres", "status", "powod", "stron", "tytul", "opis", "emaile", "telefony", "social",
                "formularz", "uwaga")
PAGE_COLUMNS = ("domena", "url", "glebokosc", "http", "status", "tytul", "znaki")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_NUMBERISH = re.compile(r"^[+-]?[\d\s().-]+$")
_FILE_UNSAFE = re.compile(r"[^a-z0-9._-]")


class DatasetError(Exception):
    """Błąd zbioru (zła nazwa, brak zbioru, zbiór już istnieje); komunikat jest dla użytkownika."""


@dataclass(frozen=True)
class PageText:
    """Jedna zeskrapowana strona odczytana z pliku tekstowego zbioru."""
    domain: str
    url: str
    title: str
    text: str


def safe_cell(value: object) -> str:
    """Wartość do CSV: bez znaków sterujących i bez początku, który arkusz wziąłby za formułę (=, +, -, @)."""
    text = _CTRL.sub(" ", "" if value is None else str(value)).strip()
    if text[:1] in ("=", "+", "-", "@") and not _NUMBERISH.match(text):
        return "'" + text
    return text


def check_name(name: str) -> str:
    """Zwraca nazwę zbioru albo rzuca `DatasetError`, gdy nie nadaje się na nazwę katalogu."""
    if not NAME_RE.match(name or ""):
        raise DatasetError(f"Nazwa zbioru „{name}” jest niedozwolona: małe litery, cyfry, - i _ (do 60 znaków).")
    return name


def file_stem(domain: str) -> str:
    """Bezpieczna nazwa pliku z domeny (np. `a.pl_8080` dla `a.pl:8080`)."""
    return _FILE_UNSAFE.sub("_", domain.lower())


def _escape_text(text: str) -> str:
    cleaned = _CTRL.sub(" ", text[:MAX_PAGE_CHARS])
    return "\n".join((" " + line) if line.startswith(HEADER.strip()) else line for line in cleaned.split("\n"))


def _pick(result: SiteResult, kind: str) -> list:
    wanted = [c for c in result.contacts if c.kind == kind]
    wanted.sort(key=lambda c: (not c.in_domain, -c.pages))
    return [c.value for c in wanted[:MAX_PER_KIND[kind]]]


class Dataset:
    """Katalog jednego zbioru: zapis wyników witryn w trakcie skanu i odczyt do zapytań."""

    def __init__(self, name: str, root: Optional[str] = None):
        self.name = check_name(name)
        self.dir = os.path.join(root or DATASETS_DIR, name)
        self._lock = threading.Lock()
        self._meta: dict = {}

    # ---- ścieżki -----------------------------------------------------------------------
    @property
    def sites_path(self) -> str:
        return os.path.join(self.dir, "witryny.csv")

    @property
    def pages_path(self) -> str:
        return os.path.join(self.dir, "strony.csv")

    @property
    def text_dir(self) -> str:
        return os.path.join(self.dir, "tekst")

    @property
    def meta_path(self) -> str:
        return os.path.join(self.dir, "meta.json")

    def exists(self) -> bool:
        return os.path.isfile(self.meta_path)

    def require(self) -> "Dataset":
        if not self.exists():
            raise DatasetError(f"Nie ma zbioru „{self.name}”. Lista zbiorów: ai.py zbiory")
        return self

    def output_path(self, requested: Optional[str], default: str) -> str:
        """Plik wynikowy: sama nazwa trafia do katalogu zbioru, ścieżka ze znakiem / jest brana dosłownie."""
        target = requested or default
        return target if os.path.dirname(target) else os.path.join(self.dir, target)

    # ---- zapis -------------------------------------------------------------------------
    def create(self, meta: dict, *, overwrite: bool = False) -> None:
        if self.exists() and not overwrite:
            raise DatasetError(f"Zbiór „{self.name}” już istnieje. Wybierz inną nazwę albo dodaj --nadpisz.")
        os.makedirs(self.text_dir, exist_ok=True)
        for stale in os.listdir(self.text_dir):
            if stale.endswith(".txt"):
                os.remove(os.path.join(self.text_dir, stale))
        self._write_csv(self.sites_path, SITE_COLUMNS, [], mode="w")
        self._write_csv(self.pages_path, PAGE_COLUMNS, [], mode="w")
        self._meta = {"nazwa": self.name, "utworzono": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "status": "w toku",
                      "gotowe": 0, **meta}
        self._save_meta()

    def append_site(self, result: SiteResult) -> None:
        pages = result.fetched
        if pages:
            os.makedirs(self.text_dir, exist_ok=True)
            with open(os.path.join(self.text_dir, file_stem(result.domain) + ".txt"), "w", encoding="utf-8") as handle:
                for page in pages:
                    title = " ".join(page.title.split())
                    handle.write(f"{HEADER}{page.url} | {title}\n{_escape_text(page.text)}\n\n")
        site_row = self._site_row(result)
        page_rows = [[result.domain, page.url, page.depth, page.http_status or "", page.status, page.title,
                      len(page.text)] for page in result.pages]
        with self._lock:
            self._write_csv(self.sites_path, SITE_COLUMNS, [site_row], mode="a")
            self._write_csv(self.pages_path, PAGE_COLUMNS, page_rows, mode="a")
            self._meta["gotowe"] = self._meta.get("gotowe", 0) + 1
            self._save_meta()

    def finish(self, **extra: object) -> None:
        with self._lock:
            self._meta.update({"status": "gotowy", **extra})
            self._save_meta()

    @staticmethod
    def _site_row(result: SiteResult) -> list:
        fetched = result.fetched
        first = fetched[0] if fetched else None
        note = result.message or (f"przekierowanie na {result.target}" if result.target else "")
        if result.pending and result.status == st.FIRMA_LIMIT:
            note = (note + "; " if note else "") + f"nieodwiedzone adresy: {result.pending}"
        return [result.domain, result.start_url, result.status, result.reason, len(fetched),
                first.title if first else "", first.description if first else "",
                ";".join(_pick(result, "email")), ";".join(_pick(result, "telefon")), ";".join(_pick(result, "social")),
                ";".join(_pick(result, "formularz")), note]

    @staticmethod
    def _write_csv(path: str, header: tuple, rows: list, *, mode: str) -> None:
        with open(path, mode, newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if mode == "w":
                writer.writerow(header)
            writer.writerows([[safe_cell(cell) for cell in row] for row in rows])

    def _save_meta(self) -> None:
        tmp = self.meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._meta, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, self.meta_path)

    # ---- odczyt ---------------------------------------------------------------------------
    def meta(self) -> dict:
        with open(self.require().meta_path, encoding="utf-8") as handle:
            return json.load(handle)

    def sites(self) -> list:
        with open(self.require().sites_path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def pages(self, domain: Optional[str] = None) -> Iterator[PageText]:
        """Strony z plików tekstowych (w kolejności domen z witryny.csv); `domain` zawęża do jednej witryny."""
        for site in self.sites():
            if domain and site["domena"] != domain:
                continue
            yield from self._read_text(site["domena"])

    def _read_text(self, domain: str) -> Iterator[PageText]:
        path = os.path.join(self.text_dir, file_stem(domain) + ".txt")
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as handle:
            raw = "\n" + handle.read()
        for block in raw.split("\n" + HEADER)[1:]:
            head, _, body = block.partition("\n")
            url, _, title = head.partition(" | ")
            yield PageText(domain, url.strip(), title.strip(), body.strip("\n"))


def list_datasets(root: Optional[str] = None) -> list:
    """Metadane wszystkich zbiorów w katalogu `data/skrap/`, alfabetycznie."""
    base = root or DATASETS_DIR
    found = []
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        dataset = Dataset(name, base) if NAME_RE.match(name) else None
        if dataset and dataset.exists():
            found.append(dataset.meta())
    return found


def short_url(url: str) -> str:
    """Ścieżka i zapytanie adresu (bez schematu i hosta) - do zwięzłych wypisów."""
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
