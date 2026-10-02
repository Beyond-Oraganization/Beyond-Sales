"""Wspólne pomoce testów: baza sqlite w pamięci, sterowany zegar, przykładowe wiersze."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from typing import Callable, Optional, Sequence

import sqlalchemy as sa

from perno import db as dbmod
from perno.settings import ConfigFile
from perno.web.app import App
from perno.web.context import AppContext
from perno.web.pages import PAGES

NO_CONFIG = os.path.join(tempfile.gettempdir(), "perno-testy-brak-config.env")  # nie istnieje: testy nie czytają config.env


class FakeClock:
    def __init__(self, now: datetime = datetime(2026, 9, 30, 12, 0, 0)):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def make_db(clock=None, url: str = "sqlite://") -> dbmod.Database:
    engine = dbmod.make_engine(sa.engine.make_url(url))
    database = dbmod.Database(engine, clock=clock or FakeClock())
    database.ensure_schema()
    return database


def make_app(database: dbmod.Database, pages: Optional[Sequence] = None, *, config_path: Optional[str] = None,
             smtp_factory: Optional[Callable] = None, allowed_hosts: Optional[set] = None) -> App:
    """Aplikacja z kontekstem złożonym jak w `cli/server.py`; domyślnie bez pliku konfiguracji (nie czyta config.env)."""
    ctx = AppContext(db=database, pages=PAGES if pages is None else pages,
                     config=ConfigFile(config_path or NO_CONFIG), smtp_factory=smtp_factory)
    return App(ctx, allowed_hosts=allowed_hosts)


def sample_row(n: int = 1, segment: str = "eventy", status: str = "do_wyslania",
               email: str = "firma@example.com", **overrides) -> dict:
    row = {
        "id": f"{segment}-{n:03d}", "segment": segment, "row_number": str(n),
        "company": f"Firma {n}", "contact_person": "", "email": email,
        "website": f"https://firma{n}.example.com/", "tag": "test", "mode": "Panstwo",
        "subject": f"Temat {n}", "body_html": f"<p>Treść {n}</p>", "body_text": f"Treść {n}",
        "status": status, "generated_at": "2026-09-29T10:00:00", "sent_at": "",
        "attachments": "", "inline_images": "", "error": "", "retry_count": "0",
        "country": "", "city": "", "address": "", "description": "",
    }
    row.update(overrides)
    return row


def put(database: dbmod.Database, *rows: dict) -> None:
    with database.transaction() as conn:
        database.insert_rows(conn, list(rows))


class FakeSmtp:
    """Zapamiętuje wiadomości; nigdy nie łączy się z siecią."""

    def __init__(self, fail_with=None):
        self.sent = []
        self.fail_with = fail_with

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, message):
        if self.fail_with:
            raise self.fail_with
        self.sent.append(message)


class FakeTime:
    """Zegar monotoniczny dla testów uprzejmości: `sleep` tylko przesuwa czas."""

    def __init__(self, start: float = 1000.0):
        self.t = start
        self.sleeps = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float, stop_event=None) -> None:
        if seconds > 0:
            self.sleeps.append(seconds)
            self.t += seconds


class ZeroRng:
    """Jitter zawsze 0 — deterministyczne odstępy w testach arytmetyki."""

    def uniform(self, low, high):
        return low


OLD_TABLE_DDL = """
CREATE TABLE maile (
    id VARCHAR(32) NOT NULL PRIMARY KEY, segment VARCHAR(32) NOT NULL, row_number INTEGER, company VARCHAR(255),
    contact_person VARCHAR(255), email VARCHAR(320), website VARCHAR(1000), tag VARCHAR(255), mode VARCHAR(16),
    subject VARCHAR(500), body_html TEXT, body_text TEXT, status VARCHAR(32) NOT NULL, generated_at DATETIME,
    sent_at DATETIME, attachments TEXT, inline_images VARCHAR(255), error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0, updated_at DATETIME
)
"""


def old_database(rows: int = 2, url: str = "sqlite://", clock=None) -> dbmod.Database:
    """Baza w kształcie sprzed dodania danych o firmie: tabela z 20 kolumnami i kilkoma wysłanymi wierszami."""
    engine = dbmod.make_engine(sa.engine.make_url(url))
    with engine.begin() as conn:
        conn.execute(sa.text(OLD_TABLE_DDL))
        for n in range(1, rows + 1):
            conn.execute(sa.text(
                "INSERT INTO maile (id, segment, row_number, company, email, website, status, sent_at, retry_count, body_html) "
                "VALUES (:id, 'eventy', :n, :company, :email, :website, 'wyslano', '2026-09-29 10:00:00', 0, :body)"),
                {"id": f"eventy-{n:03d}", "n": n, "company": f"Firma {n}", "email": f"f{n}@example.com",
                 "website": f"https://f{n}.example.com/", "body": f"<p>Treść {n}</p>"})
    return dbmod.Database(engine, clock=clock or FakeClock())
