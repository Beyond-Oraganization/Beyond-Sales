"""Zależności stron panelu składane w jednym miejscu (`cli/server.py`) i przekazywane handlerom.

Zastępuje ukryte atrybuty doklejane do aplikacji w trakcie pracy (`getattr(app, "bulk_log")` i podobne) oraz globalne
blokady modułowe: stan należy do kontekstu, a każda leniwa wartość ma własną blokadę.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from perno.db import Database
from perno.scraper.store import Store
from perno.services.bulk import UndoLog
from perno.settings import ConfigFile
from perno.web.page import Page


class Lazy[T]:
    """Wartość tworzona przy pierwszym udanym użyciu. Gdy fabryka rzuci wyjątek, nic nie jest zapamiętane,
    więc następne użycie próbuje ponownie (np. po przywróceniu bazy)."""

    def __init__(self, factory: Callable[[], T]):
        self._factory = factory
        self._lock = threading.Lock()
        self._ready = False
        self._value: Optional[T] = None

    def get(self) -> T:
        if not self._ready:
            with self._lock:
                if not self._ready:
                    self._value = self._factory()
                    self._ready = True
        return self._value  # type: ignore[return-value]


def open_scan_store(db: Database) -> Store:
    """Magazyn skrapera na bazie panelu; jeden na aplikację (jego blokada serializuje sqlite), schemat zakładany od razu."""
    store = Store(db)
    store.ensure_schema()
    return store


@dataclass
class AppContext:
    db: Database
    pages: Sequence[Page]
    config: ConfigFile = field(default_factory=ConfigFile)
    undo_log: UndoLog = field(default_factory=UndoLog)  # jeden dziennik cofania na aplikację (w pamięci serwera)
    smtp_factory: Optional[Callable] = None  # wstrzykiwana atrapa w testach; None = z config.env
    scan_store: Lazy[Store] = field(init=False)

    def __post_init__(self) -> None:
        self.scan_store = Lazy(lambda: open_scan_store(self.db))
