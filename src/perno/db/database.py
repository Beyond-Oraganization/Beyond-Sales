"""`Database`: fasada na tabelę `maile` — połączenie, schemat i operacje na wierszach (`MailRepository`)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from perno.db import migrations
from perno.db.engine import build_url, make_engine
from perno.db.errors import DB_ERRORS, DbUnavailable
from perno.db.mail_repository import MailRepository
from perno.db.schema import maile
from perno.settings import Environment

# Kolumny, po których szuka wyszukiwarka w panelu (`db.listing_sql.haystack`).
SEARCH_COLUMNS = ("id", "company", "contact_person", "email", "website", "country", "city", "address", "description")


def search_collation_warning(conn) -> str:
    """Wyszukiwarka w panelu pomija wielkość liter i znaki diakrytyczne dzięki kolacji kolumn MySQL (`*_ai_ci`): z inną
    kolacją „lodz” przestaje znajdować „Łódź”. Dane są nietknięte, ale warto o tym wiedzieć; pusty tekst = wszystko w porządku."""
    try:
        rows = conn.execute(sa.text(
            "SELECT COLUMN_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'maile' AND COLLATION_NAME IS NOT NULL")).all()
    except DB_ERRORS:  # to tylko diagnostyka: brak uprawnień do information_schema nie może zablokować startu panelu
        return ""
    wrong = sorted(f"{name} ({collation})" for name, collation in rows
                   if name in SEARCH_COLUMNS and "_ai_ci" not in collation.lower())
    if not wrong:
        return ""
    return ("kolumny tabeli maile mają kolację bez „_ai_ci”, więc wyszukiwarka w panelu nie pominie ogonków ani wielkości liter: "
            + ", ".join(wrong) + ". Naprawa (po kopii zapasowej): ALTER TABLE maile CONVERT TO CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_0900_ai_ci;")


class Database(MailRepository):
    def __init__(self, engine: Engine, clock: Optional[Callable[[], datetime]] = None):
        super().__init__(engine, clock)

    @classmethod
    def from_config(cls, config: Mapping, env: Optional[Environment] = None) -> "Database":
        url = build_url(config, env)
        try:
            engine = make_engine(url)
        except ModuleNotFoundError as exc:
            raise DbUnavailable(
                "Brak sterownika bazy (PyMySQL). Uruchom: uv sync"
            ) from exc
        return cls(engine)

    @property
    def backend(self) -> str:
        return self.engine.url.get_backend_name()

    def describe(self) -> str:
        url = self.engine.url
        if self.backend == "sqlite":
            return "sqlite (test)"
        where = "socket" if url.query.get("unix_socket") else f"{url.host}:{url.port}"
        return f"{url.database} @ {where}"

    @contextmanager
    def connection(self):
        """Połączenie do odczytu (bez transakcji zapisu)."""
        with self.engine.connect() as conn:
            yield conn

    @contextmanager
    def transaction(self):
        with self.engine.begin() as conn:
            yield conn

    # ---- schemat ------------------------------------------------------

    def ensure_schema(self) -> None:
        migrations.ensure_schema(self.engine)

    def missing_columns(self) -> list:
        return migrations.missing_columns(self.engine)

    def upgrade_schema(self) -> list:
        return migrations.upgrade_schema(self.engine)

    def check_connection(self) -> dict:
        try:
            with self.engine.connect() as conn:
                info: dict[str, Any] = {"db": self.describe(), "backend": self.backend}
                if self.backend == "mysql":
                    info["version"] = conn.execute(sa.text("SELECT VERSION()")).scalar()
                    info["sql_mode"] = conn.execute(sa.text("SELECT @@sql_mode")).scalar()
                else:
                    info["version"] = conn.execute(sa.text("SELECT sqlite_version()")).scalar()
                    info["sql_mode"] = "n/d"
                info["table_exists"] = sa.inspect(conn).has_table("maile")
                info["row_count"] = (
                    conn.execute(sa.select(sa.func.count()).select_from(maile)).scalar()
                    if info["table_exists"] else None
                )
                info["search_warning"] = (
                    search_collation_warning(conn) if self.backend == "mysql" and info["table_exists"] else "")
                return info
        except DB_ERRORS as exc:
            reason = str(getattr(exc, "orig", exc))[:300]
            raise DbUnavailable(f"Nie można połączyć się z bazą ({self.describe()}): {reason}") from exc
