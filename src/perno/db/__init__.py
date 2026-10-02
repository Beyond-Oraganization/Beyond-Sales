"""Warstwa danych: tabela `maile` w MySQL (SQLAlchemy Core + PyMySQL).

Zamiast "wczytaj cały plik → zmień → przepisz" (jak w dawnym Maile.csv) każda
operacja to celowane zapytanie, więc CLI i panel mogą pracować jednocześnie.
Kontrakt z panelem (JSON) jest bez zmian: 19 pól tekstowych jak w starym CSV,
NULL → '' i daty jako ISO 'RRRR-MM-DDTGG:MM:SS' (to robi `to_api`).

Uruchomienie samodzielne:  python3 -m perno.db --check
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Iterator, Mapping, Optional

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import StaticPool

from perno.domain import countries
from perno.domain import statuses as st
from perno.settings import ConfigError

# Dawny format Maile.csv (19 pól) — migracja i `--export-legacy` trzymają się go dokładnie.
LEGACY_COLUMNS = (
    "id", "segment", "row_number", "company", "contact_person", "email", "website",
    "tag", "mode", "subject", "body_html", "body_text", "status", "generated_at",
    "sent_at", "attachments", "inline_images", "error", "retry_count",
)
# Dane o firmie: skąd pochodzi (kraj jako kod ISO), gdzie (miasto, adres) i czym się zajmuje.
PROFILE_COLUMNS = ("country", "city", "address", "description")
COLUMNS = LEGACY_COLUMNS + PROFILE_COLUMNS
# Lista bez treści maili: tego potrzebuje panel do tabeli i filtrów (treść pobiera dopiero po rozwinięciu wiersza).
LIGHT_COLUMNS = tuple(name for name in COLUMNS if name not in ("body_html", "body_text"))

DB_ERRORS = (sa_exc.OperationalError, sa_exc.InterfaceError)

STALE_CLAIM_MESSAGE = "Przerwano w trakcie wysyłki — sprawdź folder Wysłane, zanim ponowisz."

_INT_FIELDS = ("row_number", "retry_count")
_DT_FIELDS = ("generated_at", "sent_at")
_REQUIRED = ("id", "segment", "status")
_MAX_LEN = {
    "id": 32, "segment": 32, "company": 255, "contact_person": 255, "email": 320,
    "website": 1000, "tag": 255, "mode": 16, "subject": 500, "status": 32,
    "inline_images": 255, "body_html": 1_000_000, "body_text": 1_000_000,
    "attachments": 60_000, "error": 60_000, "city": 120, "address": 500, "description": 5_000,
}
_EDITABLE = ("email", "subject", "body_html", "body_text", "contact_person", "status",
             "country", "city", "address", "description")
_GENERATED = (
    "segment", "row_number", "company", "contact_person", "email", "website", "tag",
    "mode", "subject", "body_html", "body_text", "status", "generated_at",
    "attachments", "inline_images",
)

metadata = sa.MetaData()
_LongText = sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql")

maile = sa.Table(
    "maile", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("segment", sa.String(32), nullable=False),
    sa.Column("row_number", sa.Integer, nullable=True),
    sa.Column("company", sa.String(255)),
    sa.Column("contact_person", sa.String(255)),
    sa.Column("email", sa.String(320)),
    sa.Column("website", sa.String(1000)),
    sa.Column("tag", sa.String(255)),
    sa.Column("mode", sa.String(16)),
    sa.Column("subject", sa.String(500)),
    sa.Column("body_html", _LongText),
    sa.Column("body_text", _LongText),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("generated_at", sa.DateTime),
    sa.Column("sent_at", sa.DateTime),
    sa.Column("attachments", sa.Text),
    sa.Column("inline_images", sa.String(255)),
    sa.Column("error", sa.Text),
    sa.Column("retry_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("updated_at", sa.DateTime),
    # Dane o firmie — na końcu, żeby kolejność kolumn była taka sama w świeżej i w uaktualnionej bazie.
    sa.Column("country", sa.String(2)),
    sa.Column("city", sa.String(120)),
    sa.Column("address", sa.String(500)),
    sa.Column("description", sa.Text),
    sa.Index("ix_maile_status_sent_at", "status", "sent_at"),
    sa.Index("ix_maile_segment_row", "segment", "row_number"),
    sa.Index("ix_maile_email", "email"),
    mysql_engine="InnoDB", mysql_default_charset="utf8mb4", mysql_collate="utf8mb4_0900_ai_ci",
)


class DbError(Exception):
    pass


class DbUnavailable(DbError):
    pass


class RowNotFound(DbError):
    pass


class InvalidValue(DbError):
    pass


class EmptyDatabase(DbError):
    pass


class RowLocked(DbError):
    def __init__(self, message: str, row: Optional[dict] = None):
        super().__init__(message)
        self.row = row


NO_COUNTRY = "_none"  # w filtrze krajów: firmy, którym nie przypisano kraju
ID_CHUNK = 500  # tyle id naraz w jednym zapytaniu `IN (…)` (limit zmiennych sqlite, rozmiar zapytania MySQL)


@dataclass(frozen=True)
class RowFilter:
    """Które wiersze wybrać. Puste pole = bez ograniczenia; wszystkie warunki łączą się przez AND."""
    segments: tuple = ()
    statuses: tuple = ()
    countries: tuple = ()  # kody ISO; NO_COUNTRY = bez kraju (można łączyć z kodami)
    has_email: Optional[bool] = None
    has_body: Optional[bool] = None
    sent_from: Optional[date] = None  # dni kalendarzowe, oba końce włącznie
    sent_to: Optional[date] = None
    # Jawna lista wierszy. None = bez ograniczenia, () = żaden. Wynik idzie w kolejności `ids` (nie w domyślnej),
    # więc plik może wyglądać tak jak lista na ekranie; id, których nie ma w bazie, są pomijane.
    ids: Optional[tuple] = None


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return str(value)


def to_api(row: Mapping) -> dict:
    return {name: _fmt(row[name]) for name in COLUMNS}


def _parse_dt(value: str, name: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise InvalidValue(f"Pole {name}: nieprawidłowa data {value!r} (oczekiwano RRRR-MM-DDTGG:MM:SS).")


def from_api(data: Mapping, *, partial: bool = False) -> dict:
    out = {}
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


def build_url(config: Mapping, environ: Mapping = os.environ) -> URL:
    override = environ.get("PERNO_DB_URL")
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
        kwargs = {"connect_args": {"check_same_thread": False}}
        if url.database in (None, "", ":memory:"):
            kwargs["poolclass"] = StaticPool
        return sa.create_engine(url, **kwargs)
    return sa.create_engine(
        url, pool_pre_ping=True, pool_recycle=1800,
        connect_args={"connect_timeout": 5, "read_timeout": 60, "write_timeout": 60},
    )


def add_column_statements(dialect, columns) -> list:
    """Polecenia ALTER TABLE dla brakujących kolumn. MySQL: jedno polecenie na wszystkie kolumny (atomowo);
    sqlite przyjmuje tylko jedną kolumnę na polecenie. Obsługujemy wyłącznie kolumny opcjonalne."""
    quote = dialect.identifier_preparer.quote
    parts = []
    for column in columns:
        if not column.nullable or column.primary_key:
            raise DbError(f"Kolumny {column.name} nie da się dodać automatycznie (wymagana wartość) — zrób to ręcznie.")
        parts.append(f"ADD COLUMN {quote(column.name)} {column.type.compile(dialect=dialect)} NULL")
    table = quote(maile.name)
    if dialect.name == "sqlite":
        return [f"ALTER TABLE {table} {part}" for part in parts]
    return [f"ALTER TABLE {table} " + ", ".join(parts)]


def _ordering():
    segment_rank = sa.case(
        *[(maile.c.segment == name, i) for i, name in enumerate(st.SEGMENT_ORDER)],
        else_=len(st.SEGMENT_ORDER),
    )
    no_number_last = sa.case((maile.c.row_number.is_(None), 1), else_=0)  # NULL sortuje się różnie zależnie od bazy
    return [segment_rank, no_number_last, maile.c.row_number, maile.c.id]


def _filled(column):
    return sa.and_(column.is_not(None), column != "")


def _blank(column):
    return sa.or_(column.is_(None), column == "")


def _day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


def _filter_clauses(flt: RowFilter) -> list:
    """Warunki WHERE dla wszystkich pól filtra poza `ids` (te obsługują `count_rows` i `iter_rows` partiami)."""
    clauses = []
    if flt.segments:
        clauses.append(maile.c.segment.in_(flt.segments))
    if flt.statuses:
        clauses.append(maile.c.status.in_(flt.statuses))
    if flt.countries:
        options = []
        codes = [code for code in flt.countries if code != NO_COUNTRY]
        if codes:
            options.append(maile.c.country.in_(codes))
        if NO_COUNTRY in flt.countries:
            options.append(_blank(maile.c.country))
        clauses.append(sa.or_(*options))
    if flt.has_email is not None:
        trimmed = sa.func.trim(maile.c.email)  # adres złożony ze spacji to brak adresu (tak samo liczy lista w panelu)
        clauses.append(sa.and_(maile.c.email.is_not(None), trimmed != "") if flt.has_email
                       else sa.or_(maile.c.email.is_(None), trimmed == ""))
    if flt.has_body is not None:
        clauses.append(_filled(maile.c.body_html) if flt.has_body else _blank(maile.c.body_html))
    if flt.sent_from is not None:
        clauses.append(maile.c.sent_at >= _day_start(flt.sent_from))
    if flt.sent_to is not None:
        clauses.append(maile.c.sent_at < _day_start(flt.sent_to) + timedelta(days=1))
    return clauses


def _id_chunks(ids) -> Iterator[list]:
    unique = list(dict.fromkeys(ids))  # duplikaty znikają, kolejność pierwszych wystąpień zostaje
    for start in range(0, len(unique), ID_CHUNK):
        yield unique[start:start + ID_CHUNK]


def _default_clock() -> datetime:
    return datetime.now()


class Database:
    def __init__(self, engine: Engine, clock: Optional[Callable[[], datetime]] = None):
        self.engine = engine
        self._clock = clock or _default_clock

    @classmethod
    def from_config(cls, config: Mapping) -> "Database":
        url = build_url(config)
        try:
            engine = make_engine(url)
        except ModuleNotFoundError as exc:
            raise DbUnavailable(
                "Brak sterownika bazy (PyMySQL). Uruchom: uv sync"
            ) from exc
        return cls(engine)

    def now(self) -> datetime:
        return self._clock().replace(microsecond=0)

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
    def transaction(self):
        with self.engine.begin() as conn:
            yield conn

    def ensure_schema(self) -> None:
        metadata.create_all(self.engine)
        self.upgrade_schema()

    def missing_columns(self) -> list:
        """Kolumny z definicji tabeli, których nie ma jeszcze w istniejącej tabeli `maile` (pusta lista, gdy tabeli brak)."""
        inspector = sa.inspect(self.engine)
        if not inspector.has_table(maile.name):
            return []
        present = {column["name"] for column in inspector.get_columns(maile.name)}
        return [column for column in maile.columns if column.name not in present]

    def upgrade_schema(self) -> list:
        """Dodaje do istniejącej tabeli brakujące kolumny (idempotentnie, bez dotykania danych). Zwraca nazwy dodanych.

        `create_all` nie zmienia tabel, które już istnieją, więc uaktualnienie wykonuje to jawnie. Wszystkie nowe
        kolumny są opcjonalne (NULL), więc istniejące wiersze zostają bez zmian, a starsza wersja programu nadal działa."""
        missing = self.missing_columns()
        if not missing:
            return []
        statements = add_column_statements(self.engine.dialect, missing)
        try:
            with self.engine.begin() as conn:
                for statement in statements:
                    conn.execute(sa.text(statement))
        except sa_exc.DBAPIError:
            if self.missing_columns():  # inny proces mógł dodać kolumny w międzyczasie — wtedy to nie błąd
                raise
        return [column.name for column in missing]

    def check_connection(self) -> dict:
        try:
            with self.engine.connect() as conn:
                info = {"db": self.describe(), "backend": self.backend}
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
                return info
        except DB_ERRORS as exc:
            reason = str(getattr(exc, "orig", exc))[:300]
            raise DbUnavailable(f"Nie można połączyć się z bazą ({self.describe()}): {reason}") from exc

    # ---- odczyt -------------------------------------------------------

    def fetch_all(self, conn=None, light: bool = False) -> list:
        """Wszystkie wiersze w kolejności panelu. `light` pomija treść maili (te dwie kolumny to większość danych),
        a w zamian dodaje `has_body` — wystarczy do list, filtrów i zaznaczania."""
        if conn is None:
            with self.engine.connect() as own:
                return self.fetch_all(own, light)
        if light:
            has_body = sa.case((sa.and_(maile.c.body_html.is_not(None), maile.c.body_html != ""), 1), else_=0)
            stmt = sa.select(*[maile.c[name] for name in LIGHT_COLUMNS], has_body.label("has_body")).order_by(*_ordering())
            rows = []
            for r in conn.execute(stmt).mappings():
                row = {name: _fmt(r[name]) for name in LIGHT_COLUMNS}
                row["has_body"] = bool(r["has_body"])
                rows.append(row)
            return rows
        stmt = sa.select(maile).order_by(*_ordering())
        return [to_api(r) for r in conn.execute(stmt).mappings()]

    def fetch_index(self) -> dict:
        stmt = sa.select(maile.c.id, maile.c.status, maile.c.sent_at, maile.c.retry_count)
        with self.engine.connect() as conn:
            return {r.id: {"status": r.status, "sent_at": r.sent_at, "retry_count": r.retry_count}
                    for r in conn.execute(stmt)}

    def get(self, row_id: str, conn=None) -> Optional[dict]:
        if conn is None:
            with self.engine.connect() as own:
                return self.get(row_id, own)
        row = conn.execute(sa.select(maile).where(maile.c.id == row_id)).mappings().first()
        return to_api(row) if row else None

    def select_candidates(
        self, *, segment: str = "both", row_id: str = "", company: str = "",
        statuses: tuple = (st.STATUS_PENDING,), limit: Optional[int] = 20, countries: tuple = (),
    ) -> list:
        stmt = sa.select(maile).where(maile.c.status.in_(statuses))
        if segment and segment != "both":
            stmt = stmt.where(maile.c.segment == segment)
        if countries:
            stmt = stmt.where(*_filter_clauses(RowFilter(countries=tuple(countries))))
        if row_id:
            stmt = stmt.where(maile.c.id == row_id)
        if company:
            stmt = stmt.where(sa.func.lower(maile.c.company).contains(company.lower(), autoescape=True))
        stmt = stmt.order_by(maile.c.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        with self.engine.connect() as conn:
            return [to_api(r) for r in conn.execute(stmt).mappings()]

    def status_counts(self) -> dict:
        stmt = sa.select(maile.c.status, sa.func.count()).group_by(maile.c.status)
        with self.engine.connect() as conn:
            return {status: n for status, n in conn.execute(stmt)}

    def distinct_segments(self) -> list:
        with self.engine.connect() as conn:
            found = [r[0] for r in conn.execute(sa.select(maile.c.segment).distinct())]
        rank = {name: i for i, name in enumerate(st.SEGMENT_ORDER)}
        return sorted(found, key=lambda s: (rank.get(s, len(rank)), s))

    def sent_today_count(self, now: Optional[datetime] = None) -> int:
        now = now or self.now()
        start = datetime(now.year, now.month, now.day)
        stmt = (
            sa.select(sa.func.count()).select_from(maile)
            .where(maile.c.status.in_(st.KEEPS_SENT_AT), maile.c.sent_at >= start,
                   maile.c.sent_at < start + timedelta(days=1))
        )
        with self.engine.connect() as conn:
            return conn.execute(stmt).scalar() or 0

    def count_rows(self, flt: RowFilter = RowFilter()) -> int:
        clauses = _filter_clauses(flt)
        count = sa.select(sa.func.count()).select_from(maile)
        with self.engine.connect() as conn:
            if flt.ids is None:
                return conn.execute(count.where(*clauses)).scalar() or 0
            return sum(conn.execute(count.where(*clauses, maile.c.id.in_(part))).scalar() or 0
                       for part in _id_chunks(flt.ids))

    def iter_rows(self, flt: RowFilter = RowFilter(), columns: Optional[tuple] = None,
                  chunk: int = 500) -> Iterator[dict]:
        names = tuple(columns or COLUMNS)
        clauses = _filter_clauses(flt)
        if flt.ids is not None:
            yield from self._iter_by_ids(flt.ids, names, clauses)
            return
        stmt = sa.select(*[maile.c[name] for name in names]).where(*clauses).order_by(*_ordering())
        with self.engine.connect() as conn:
            result = conn.execution_options(stream_results=True, yield_per=chunk).execute(stmt)
            for row in result.mappings():
                yield dict(row)

    def _iter_by_ids(self, ids, names: tuple, clauses: list) -> Iterator[dict]:
        """Wiersze o wskazanych id w kolejności `ids`, pobierane partiami po ID_CHUNK (pamięć ograniczona).
        Wszystkie partie czytane są w jednej transakcji, więc plik pochodzi z jednego spójnego stanu bazy."""
        fetch = names if "id" in names else ("id",) + names
        stmt = sa.select(*[maile.c[name] for name in fetch])
        with self.engine.connect() as conn:
            for part in _id_chunks(ids):
                found = {row["id"]: row for row in conn.execute(stmt.where(*clauses, maile.c.id.in_(part))).mappings()}
                for row_id in part:
                    row = found.get(row_id)
                    if row is not None:
                        yield {name: row[name] for name in names}

    # ---- zapis --------------------------------------------------------

    def insert_rows(self, conn, rows: list) -> int:
        stamp = self.now()
        prepared = []
        for row in rows:
            values = from_api(row)
            values["updated_at"] = stamp
            prepared.append(values)
        if prepared:
            conn.execute(sa.insert(maile), prepared)
        return len(prepared)

    def upsert_generated(self, rows: list) -> dict:
        stats = {"inserted": 0, "updated": 0, "skipped": 0}
        if not rows:
            return stats
        stamp = self.now()
        protected = st.PROTECTED_FROM_REGENERATION
        with self.engine.begin() as conn:
            existing = {
                r.id: r.status for r in conn.execute(
                    sa.select(maile.c.id, maile.c.status).where(maile.c.id.in_([r["id"] for r in rows]))
                )
            }
            for row in rows:
                values = from_api(row)
                if row["id"] not in existing:
                    values["updated_at"] = stamp
                    values["sent_at"] = None
                    values["error"] = None
                    conn.execute(sa.insert(maile).values(**values))
                    stats["inserted"] += 1
                    continue
                if existing[row["id"]] in protected:
                    stats["skipped"] += 1
                    continue
                changes = {name: values[name] for name in _GENERATED}
                changes.update(error=None, updated_at=stamp)
                result = conn.execute(
                    sa.update(maile)
                    .where(maile.c.id == row["id"], maile.c.status.not_in(protected))
                    .values(**changes)
                )
                stats["updated" if result.rowcount else "skipped"] += 1
        return stats

    def apply_edit(self, row_id: str, changes: Mapping) -> dict:
        explicit_status = "status" in changes
        editable = {k: changes[k] for k in _EDITABLE if k in changes}
        if explicit_status:
            if editable["status"] not in st.VALID_STATUSES:
                raise InvalidValue(f"Nieprawidłowy status: {editable['status']!r}.")
            if editable["status"] == st.STATUS_SENDING:
                raise InvalidValue("Statusu „Wysyłanie…” nie można ustawić ręcznie.")
        if "email" in editable and isinstance(editable["email"], str):
            editable["email"] = editable["email"].strip()
        values = from_api(editable, partial=True)
        now = self.now()

        with self.engine.begin() as conn:
            current = conn.execute(
                sa.select(maile).where(maile.c.id == row_id).with_for_update()
            ).mappings().first()
            if current is None:
                raise RowNotFound(f"Nie znaleziono wiersza {row_id}.")
            current = dict(current)
            if current["status"] == st.STATUS_SENDING and any(k != "status" for k in editable):
                raise RowLocked("Ten mail jest właśnie wysyłany — poczekaj na wynik.", to_api(current))

            merged = {**current, **values}
            if not explicit_status:
                if merged["status"] == st.STATUS_SKIPPED_NO_EMAIL and merged["email"] and merged["body_html"]:
                    merged["status"] = st.STATUS_PENDING
                elif merged["status"] == st.STATUS_PENDING and not merged["email"]:
                    merged["status"] = st.STATUS_SKIPPED_NO_EMAIL
            else:
                merged["sent_at"] = st.resolve_sent_at(merged["status"], merged["sent_at"], now)

            update = {k: merged[k] for k in list(values) + ["status", "sent_at"]}
            update["updated_at"] = now
            conn.execute(sa.update(maile).where(maile.c.id == row_id).values(**update))
            return self.get(row_id, conn)

    def claim_for_sending(self, row_id: str, allowed_from: tuple = (st.STATUS_PENDING, st.STATUS_ERROR)) -> Optional[dict]:
        with self.engine.begin() as conn:
            result = conn.execute(
                sa.update(maile)
                .where(maile.c.id == row_id, maile.c.status.in_(allowed_from),
                       maile.c.email.is_not(None), maile.c.body_html.is_not(None))
                .values(status=st.STATUS_SENDING, updated_at=self.now())
            )
            if result.rowcount != 1:
                return None
            return self.get(row_id, conn)

    def mark_sent(self, row_id: str) -> dict:
        now = self.now()
        with self.engine.begin() as conn:
            conn.execute(
                sa.update(maile).where(maile.c.id == row_id)
                .values(status=st.STATUS_SENT, sent_at=now, error=None, updated_at=now)
            )
            return self.get(row_id, conn)

    def mark_error(self, row_id: str, message: str) -> dict:
        with self.engine.begin() as conn:
            conn.execute(
                sa.update(maile)
                .where(maile.c.id == row_id, maile.c.status == st.STATUS_SENDING)
                .values(status=st.STATUS_ERROR, error=message[:2000],
                        retry_count=maile.c.retry_count + 1, updated_at=self.now())
            )
            return self.get(row_id, conn)

    def recover_stale_claims(self, older_than: timedelta = timedelta(minutes=10)) -> list:
        now = self.now()
        with self.engine.begin() as conn:
            stale = [
                r.id for r in conn.execute(
                    sa.select(maile.c.id)
                    .where(maile.c.status == st.STATUS_SENDING, maile.c.updated_at < now - older_than)
                    .with_for_update()
                )
            ]
            if stale:
                conn.execute(
                    sa.update(maile)
                    .where(maile.c.id.in_(stale), maile.c.status == st.STATUS_SENDING)
                    .values(status=st.STATUS_ERROR, error=STALE_CLAIM_MESSAGE, updated_at=now)
                )
        return stale


def main() -> None:
    from perno.settings import DEFAULT_CONFIG, load_config

    parser = argparse.ArgumentParser(description="Diagnostyka bazy (bez wypisywania haseł).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Sprawdź połączenie i stan tabeli.")
    group.add_argument("--init", action="store_true", help="Utwórz tabelę maile, jeśli nie istnieje (idempotentne).")
    group.add_argument("--upgrade", action="store_true",
                       help="Dodaj do istniejącej tabeli maile brakujące kolumny (kraj, miasto, adres, opis); dane bez zmian.")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()
    try:
        db = Database.from_config(load_config(args.config))
        if args.init:
            db.check_connection()
            db.ensure_schema()
            print("Tabela maile jest gotowa.")
        if args.upgrade:
            if not db.check_connection()["table_exists"]:
                raise DbError("Tabela maile nie istnieje — użyj najpierw --init albo migrate_csv_to_sql.py.")
            added = db.upgrade_schema()
            print(f"Dodano kolumny: {', '.join(added)}." if added else "Schemat jest aktualny — nic do dodania.")
        info = db.check_connection()
    except (ConfigError, DbError) as exc:
        sys.exit(f"BŁĄD: {exc}")
    print(f"Połączono: {info['db']}  ({info['backend']} {info['version']})")
    print(f"sql_mode: {info['sql_mode']}")
    if info["backend"] == "mysql" and "STRICT" not in (info["sql_mode"] or ""):
        print("UWAGA: tryb SQL nie jest ścisły (brak STRICT_TRANS_TABLES) — zbyt długie dane mogą być cicho ucinane.")
    print(f"Tabela maile: {'jest, wierszy: ' + str(info['row_count']) if info['table_exists'] else 'jeszcze nie istnieje'}")


if __name__ == "__main__":
    main()
