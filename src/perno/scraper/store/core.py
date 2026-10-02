"""Wspólny rdzeń magazynu skrapera: silnik, blokada sqlite, transakcje z ponowieniami i pomocnicze czyszczenie tekstu."""

from __future__ import annotations

import gzip
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Optional

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.sql.dml import Insert

from perno.db import Database
from perno.scraper import schema
from perno.scraper import statuses as st
from perno.scraper.schema import firmy
from perno.scraper.store.errors import LeaseLost, StoreError

INSERT_CHUNK = 50
RETRY_CODES = frozenset({1205, 1213, 2006, 2013})  # lock wait timeout, deadlock, MySQL gone away / lost
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value, limit: Optional[int] = None) -> Optional[str]:
    """Bezpieczny tekst do utf8mb4: bez NUL/znaków sterujących C0 i samotnych surogatów, przycięty po znakach."""
    if value is None:
        return None
    text = str(value).encode("utf-8", "ignore").decode("utf-8", "ignore")
    text = _CTRL_RE.sub("", text)
    return text[:limit] if limit else text


def ascii_or_none(value, limit: int) -> Optional[str]:
    if value is None:
        return None
    value = str(value)
    return value if value.isascii() and len(value) <= limit and "\x00" not in value else None


def compress_html(raw: bytes) -> bytes:
    return gzip.compress(raw, 6, mtime=0)


def iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%S") if isinstance(value, datetime) else value


def _prepare_sqlite(engine) -> None:
    """Testy/demo na sqlite: czekaj na zwolnienie blokady zamiast od razu zgłaszać „database is locked"."""
    if engine.url.get_backend_name() != "sqlite" or getattr(engine, "_perno_sqlite_ready", False):
        return
    in_memory = engine.url.database in (None, "", ":memory:")

    @sa.event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):  # pragma: no cover - wywoływane przez SQLAlchemy
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        if not in_memory:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    engine._perno_sqlite_ready = True
    if not in_memory:
        engine.dispose()  # przebuduj pulę, żeby istniejące połączenia nie ominęły PRAGMA (pamięć: dispose skasowałby bazę)


class StoreCore:
    def __init__(self, db: Database):
        self.db = db
        self.engine = db.engine
        self.backend = db.backend
        if self.backend not in ("mysql", "sqlite"):
            raise StoreError(f"Skraper obsługuje MySQL i sqlite (testy), nie {self.backend}.")
        _prepare_sqlite(self.engine)
        self._lock = threading.RLock() if self.backend == "sqlite" else None

    @contextmanager
    def _tx(self):
        if self._lock:
            self._lock.acquire()
        try:
            with self.engine.begin() as conn:
                yield conn
        finally:
            if self._lock:
                self._lock.release()

    @contextmanager
    def _read(self):
        if self._lock:
            self._lock.acquire()
        try:
            with self.engine.connect() as conn:
                yield conn
        finally:
            if self._lock:
                self._lock.release()

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        args: tuple = getattr(getattr(exc, "orig", None), "args", ())
        return bool(args) and args[0] in RETRY_CODES

    def _run(self, fn: Callable, retries: int = 3):
        """Wykonaj `fn(conn)` w transakcji; deadlock/utratę połączenia MySQL ponawiamy (nowa transakcja)."""
        for attempt in range(retries + 1):
            try:
                with self._tx() as conn:
                    return fn(conn)
            except sa_exc.DBAPIError as exc:
                if attempt < retries and self._retryable(exc):
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise

    def _insert_noop(self, conn, table, rows: list, noop_column: str = "id") -> None:
        """INSERT … ON CONFLICT DO NOTHING / ON DUPLICATE KEY UPDATE x=x (nie INSERT IGNORE — ten w trybie
        ścisłym maskuje także inne błędy)."""
        for start in range(0, len(rows), INSERT_CHUNK):
            chunk = rows[start:start + INSERT_CHUNK]
            stmt: Insert
            if self.backend == "mysql":
                stmt = mysql_dialect.insert(table).values(chunk).on_duplicate_key_update(
                    **{noop_column: table.c[noop_column]})
            else:
                stmt = sqlite_dialect.insert(table).values(chunk).on_conflict_do_nothing()
            conn.execute(stmt)

    def ensure_schema(self) -> None:
        if self._lock:
            with self._lock:
                schema.skraper_metadata.create_all(self.engine)
        else:
            schema.skraper_metadata.create_all(self.engine)

    @staticmethod
    def _check_lease(conn, firma_id: int, run_id: int) -> None:
        """SELECT … FOR UPDATE: weryfikuje dzierżawę i serializuje zapisy firmy (portowalne, w odróżnieniu od rowcount)."""
        found = conn.execute(sa.select(firmy.c.id).where(
            firmy.c.id == firma_id, firmy.c.przebieg_id == run_id, firmy.c.skan_status == st.FIRMA_SKANOWANIE
        ).with_for_update()).first()
        if found is None:
            raise LeaseLost(f"Firma {firma_id} nie jest już dzierżawiona przez przebieg {run_id}.")
