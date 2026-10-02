"""Magazyn skrapera (DAL) — osobny od `Database`, ale na tym samym silniku.

Najważniejsze gwarancje:
  * jedna firma = jedna dzierżawa (`claim_next_firma`, UPDATE z warunkiem na status);
  * zapis strony to JEDNA transakcja, a każda taka transakcja sprawdza dzierżawę (`przebieg_id`) —
    „zombie" po uśpieniu Maca dostaje `LeaseLost` zamiast nadpisywać dane następnego przebiegu;
  * co najwyżej jeden aktywny przebieg (`skan_przebiegi.aktywny` UNIQUE);
  * odzyskiwanie po awarii znosi zmianę czasu (ujemna różnica heartbeatu nie oznacza „stary").

Daty to naiwne czasy lokalne z `db.now()` (konwencja projektu). Dla sqlite (testy/demo) wszystkie
operacje są serializowane blokadą procesu; MySQL korzysta z blokad wierszy."""

from __future__ import annotations

from perno.scraper.store.companies import CompanyStore
from perno.scraper.store.core import INSERT_CHUNK, StoreCore, ascii_or_none, clean_text, compress_html
from perno.scraper.store.errors import (
    DomainTaken,
    FirmaBusy,
    InvalidFirma,
    LeaseLost,
    NotFound,
    RunActive,
    StoreError,
)
from perno.scraper.store.pages import PageStore
from perno.scraper.store.queue import QueueStore
from perno.scraper.store.read_models import ReadModels
from perno.scraper.store.records import LinkRecord, PageRecord
from perno.scraper.store.retention import RetentionStore
from perno.scraper.store.runs import RunStore, pid_alive


class Store(RetentionStore, CompanyStore, ReadModels, RunStore, QueueStore, PageStore):
    """Fasada magazynu: wszystkie operacje na tabelach skrapera. Role są rozdzielone na moduły tego pakietu
    (`companies`, `runs`, `queue`, `pages`, `read_models`, `retention`), a wspólny rdzeń to `StoreCore`."""


__all__ = [
    "INSERT_CHUNK", "DomainTaken", "FirmaBusy", "InvalidFirma", "LeaseLost", "LinkRecord", "NotFound", "PageRecord",
    "RunActive", "Store", "StoreCore", "StoreError", "ascii_or_none", "clean_text", "compress_html", "pid_alive",
]
