"""Operacje na wierszach tabeli `maile`: odczyt (pełny i lekki), filtrowanie, edycja, generowanie i cykl wysyłki.

Zamiast „wczytaj cały plik → zmień → przepisz" (jak w dawnym Maile.csv) każda operacja to celowane zapytanie,
więc CLI i panel mogą pracować jednocześnie. Reguły biznesowe (które statusy wolno ustawić, kiedy wiersz wraca na
listę wysyłki) są w `perno.domain.mail_rules`; tu zostaje wyłącznie ich wykonanie na bazie.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Callable, Iterable, Iterator, Mapping, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from perno.db.codec import from_api, to_api
from perno.db.errors import InvalidValue, RowLocked, RowNotFound
from perno.db.listing_queries import ListingQueries, has_body_expression, light_columns, to_light_row
from perno.db.queries import ID_CHUNK, filter_clauses, id_chunks, ordering
from perno.db.schema import COLUMNS, maile
from perno.domain import mail_rules
from perno.domain import statuses as st
from perno.domain.listing import DEFAULT_SORT
from perno.domain.rowfilter import ALL_ROWS, RowFilter

STALE_CLAIM_MESSAGE = "Przerwano w trakcie wysyłki — sprawdź folder Wysłane, zanim ponowisz."

EDITABLE = ("email", "subject", "body_html", "body_text", "contact_person", "status",
            "country", "city", "address", "description")
GENERATED = (
    "segment", "row_number", "company", "contact_person", "email", "website", "tag",
    "mode", "subject", "body_html", "body_text", "status", "generated_at",
    "attachments", "inline_images",
)


def _default_clock() -> datetime:
    return datetime.now()


class MailRepository(ListingQueries):
    def __init__(self, engine: Engine, clock: Optional[Callable[[], datetime]] = None):
        self.engine = engine
        self._clock = clock or _default_clock

    def now(self) -> datetime:
        return self._clock().replace(microsecond=0)

    # ---- odczyt -------------------------------------------------------

    def fetch_all(self, conn=None, light: bool = False) -> list:
        """Wszystkie wiersze w kolejności panelu. `light` pomija treść maili (te dwie kolumny to większość danych),
        a w zamian dodaje `has_body` — wystarczy do list, filtrów i zaznaczania."""
        if conn is None:
            with self.engine.connect() as own:
                return self.fetch_all(own, light)
        if light:
            stmt = sa.select(*light_columns()).order_by(*ordering())
            return [to_light_row(r) for r in conn.execute(stmt).mappings()]
        stmt = sa.select(maile).order_by(*ordering())
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
            stmt = stmt.where(*filter_clauses(RowFilter(countries=tuple(countries))))
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

    def count_rows(self, flt: RowFilter = ALL_ROWS) -> int:
        clauses = filter_clauses(flt)
        count = sa.select(sa.func.count()).select_from(maile)
        with self.engine.connect() as conn:
            if flt.ids is None:
                return conn.execute(count.where(*clauses)).scalar() or 0
            return sum(conn.execute(count.where(*clauses, maile.c.id.in_(part))).scalar() or 0
                       for part in id_chunks(flt.ids))

    def iter_rows(self, flt: RowFilter = ALL_ROWS, columns: Optional[tuple] = None,
                  chunk: int = 500, sort: str = DEFAULT_SORT) -> Iterator[dict]:
        """Wiersze po filtrze, strumieniowo, w kolejności `sort` (domyślnie kolejność panelu). Z `flt.ids` idą w kolejności
        tych id (wołający układa je tak, jak ma wyglądać plik), a `sort` nie ma wtedy znaczenia."""
        names = tuple(columns or COLUMNS)
        clauses = filter_clauses(flt)
        if flt.ids is not None:
            yield from self._iter_by_ids(flt.ids, names, clauses)
            return
        stmt = sa.select(*[maile.c[name] for name in names]).where(*clauses).order_by(*self._order(sort))
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
            for part in id_chunks(ids):
                found = {row["id"]: row for row in conn.execute(stmt.where(*clauses, maile.c.id.in_(part))).mappings()}
                for row_id in part:
                    row = found.get(row_id)
                    if row is not None:
                        yield {name: row[name] for name in names}

    def load_for_bulk(self, conn, ids: Iterable[str], *, lock: bool) -> dict:
        """Wiersze o podanych id (tylko pola potrzebne do analizy zmiany zbiorczej — bez treści maili, za to z
        `has_body`), czytane partiami w kolejności id. Przy `lock` zakładają blokady wierszy (SELECT … FOR UPDATE)
        zawsze w tej samej kolejności, więc dwie jednoczesne operacje nie zakleszczą się nawzajem."""
        has_body = has_body_expression().label("has_body")
        columns = [maile.c.id, maile.c.status, maile.c.sent_at, maile.c.email, maile.c.country, maile.c.city,
                   maile.c.address, has_body]
        found = {}
        ordered = sorted(ids)
        for start in range(0, len(ordered), ID_CHUNK):
            stmt = sa.select(*columns).where(maile.c.id.in_(ordered[start:start + ID_CHUNK])).order_by(maile.c.id)
            if lock:
                stmt = stmt.with_for_update()
            for row in conn.execute(stmt).mappings():
                found[row["id"]] = dict(row)
        return found

    def id_website_pairs(self, conn) -> list:
        """`[{id, website}]` wszystkich wierszy — do rozpoznawania firm po domenie przy imporcie."""
        return [dict(r) for r in conn.execute(sa.select(maile.c.id, maile.c.website)).mappings()]

    def segment_ids(self, conn, segment: str) -> list:
        return [r.id for r in conn.execute(sa.select(maile.c.id).where(maile.c.segment == segment))]

    def profile_rows(self, conn) -> list:
        """`[{id, website, country, city, address}]` — dane o firmie do uzupełniania kraju, miasta i adresu."""
        return [dict(r) for r in conn.execute(
            sa.select(maile.c.id, maile.c.website, maile.c.country, maile.c.city, maile.c.address)).mappings()]

    @contextmanager
    def dump_table(self):
        """`(nazwy kolumn, wiersze)` całej tabeli w kolejności id — także z kolumnami, których definicja w kodzie jeszcze
        nie zna (baza sprzed uaktualnienia schematu). Kolumny mają typy z definicji tabeli, nieznane idą jako tekst."""
        with self.engine.connect() as conn:
            present = [column["name"] for column in sa.inspect(conn).get_columns(maile.name)]
            selected = [maile.c[name] if name in maile.c else sa.column(name) for name in present]
            result = conn.execute(sa.select(*selected).select_from(maile).order_by(maile.c.id))
            yield list(result.keys()), result

    # ---- zapis --------------------------------------------------------

    def update_rows(self, conn, updates: Iterable[tuple], now: datetime) -> None:
        """Zapisuje `[(id, {pole: wartość})]` w transakcji wywołującego i odnotowuje czas zmiany (`updated_at`).
        Wiersze z identycznymi zmianami idą jednym `UPDATE … WHERE id IN (…)` (partiami po `ID_CHUNK`): zmiana statusu
        20 tys. firm to kilkadziesiąt zapytań zamiast 20 tys., czyli sekundy zamiast kilkunastu sekund na MySQL."""
        groups: dict[tuple, list] = {}
        for row_id, values in updates:
            groups.setdefault(tuple(sorted(values.items())), []).append(row_id)
        for changes, ids in groups.items():
            for start in range(0, len(ids), ID_CHUNK):
                part = ids[start:start + ID_CHUNK]
                conn.execute(sa.update(maile).where(maile.c.id.in_(part)).values(**dict(changes), updated_at=now))

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
                changes = {name: values[name] for name in GENERATED}
                changes.update(error=None, updated_at=stamp)
                result = conn.execute(
                    sa.update(maile)
                    .where(maile.c.id == row["id"], maile.c.status.not_in(protected))
                    .values(**changes)
                )
                stats["updated" if result.rowcount else "skipped"] += 1
        return stats

    def apply_enrichment(self, updates: Iterable[tuple], allowed_statuses: tuple) -> int:
        """Zapisuje treść przygotowaną z danych skanu: `[(id, {pole: wartość})]`. Wiersz dostaje status „Do wysłania”
        i świeżą datę wygenerowania, ale tylko wtedy, gdy nadal jest w jednym z `allowed_statuses` (w międzyczasie
        mógł zostać wysłany albo zmieniony ręcznie — wtedy go nie ruszamy). Zwraca liczbę zapisanych wierszy."""
        now = self.now()
        written = 0
        with self.engine.begin() as conn:
            for row_id, values in updates:
                values = {**values, "status": st.STATUS_PENDING, "generated_at": now, "updated_at": now,
                          "error": None, "sent_at": None}
                result = conn.execute(sa.update(maile).where(
                    maile.c.id == row_id, maile.c.status.in_(allowed_statuses)).values(**values))
                written += result.rowcount
        return written

    def apply_edit(self, row_id: str, changes: Mapping) -> dict:
        explicit_status = "status" in changes
        editable = {k: changes[k] for k in EDITABLE if k in changes}
        if explicit_status:
            problem = mail_rules.manual_status_problem(editable["status"])
            if problem:
                raise InvalidValue(problem)
        if "email" in editable and isinstance(editable["email"], str):
            editable["email"] = editable["email"].strip()
        values = from_api(editable, partial=True)
        now = self.now()

        with self.engine.begin() as conn:
            found = conn.execute(
                sa.select(maile).where(maile.c.id == row_id).with_for_update()
            ).mappings().first()
            if found is None:
                raise RowNotFound(f"Nie znaleziono wiersza {row_id}.")
            current = dict(found)
            if mail_rules.edit_blocked_by_sending(current["status"], editable):
                raise RowLocked("Ten mail jest właśnie wysyłany — poczekaj na wynik.", to_api(current))

            update = mail_rules.resolve_edit(current, values, explicit_status=explicit_status, now=now)
            update["updated_at"] = now
            conn.execute(sa.update(maile).where(maile.c.id == row_id).values(**update))
            return to_api(conn.execute(sa.select(maile).where(maile.c.id == row_id)).mappings().one())

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

    def mark_sent(self, row_id: str) -> Optional[dict]:
        now = self.now()
        with self.engine.begin() as conn:
            conn.execute(
                sa.update(maile).where(maile.c.id == row_id)
                .values(status=st.STATUS_SENT, sent_at=now, error=None, updated_at=now)
            )
            return self.get(row_id, conn)

    def mark_error(self, row_id: str, message: str) -> Optional[dict]:
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
