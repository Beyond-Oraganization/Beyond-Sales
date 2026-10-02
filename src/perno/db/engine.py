"""Adres bazy i silnik SQLAlchemy (MySQL na co dzień, sqlite w testach i na bazie demo)."""

from __future__ import annotations

from typing import Any, Mapping, Optional

import sqlalchemy as sa
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import StaticPool

from perno.db import sqlite_functions
from perno.settings import ConfigError, Environment


def build_url(config: Mapping, env: Optional[Environment] = None) -> URL:
    override = (env if env is not None else Environment.from_environ()).db_url
    if override:
        return sa.engine.make_url(override)
    missing = [k for k in ("DB_NAME", "DB_USER", "DB_PASSWORD") if not config.get(k)]
    if missing:
        raise ConfigError(f"W config.env brakuje danych dostępu do bazy: {', '.join(missing)}.")
    query = {"charset": "utf8mb4"}
    if config.get("DB_SOCKET"):
        query["unix_socket"] = config["DB_SOCKET"]
    try:
        port = int(config.get("DB_PORT") or 3306)
    except ValueError:
        raise ConfigError("DB_PORT w config.env nie jest liczbą.") from None
    return URL.create(
        "mysql+pymysql", username=config["DB_USER"], password=config["DB_PASSWORD"],
        host=config.get("DB_HOST") or "127.0.0.1", port=port, database=config["DB_NAME"], query=query,
    )


def make_engine(url: URL) -> Engine:
    if url.get_backend_name() == "sqlite":
        kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
        if url.database in (None, "", ":memory:"):
            kwargs["poolclass"] = StaticPool
        engine = sa.create_engine(url, **kwargs)
        sa.event.listen(engine, "connect", sqlite_functions.register)  # perno_fold i kolacja polska dla listy w panelu
        return engine
    return sa.create_engine(
        url, pool_pre_ping=True, pool_recycle=1800,
        connect_args={"connect_timeout": 5, "read_timeout": 60, "write_timeout": 60},
    )
