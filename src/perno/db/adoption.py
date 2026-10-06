"""Przypisanie wierszy `maile` do firm (`podmioty`): krok danych po uaktualnieniu schematu.

Dotychczas wiersz `maile` był naraz firmą i adresem. „Przejęcie” zakłada firmę z każdego takiego wiersza (firma dostaje id
wiersza, więc adres główny ma to samo id co firma) i ustawia w wierszu `company_id` oraz `position = 1`. Jest
idempotentne i bezpieczne dla starego kodu: nie zmienia żadnej dotychczasowej kolumny, a stary panel czy demon, który
później wstawi wiersz bez firmy (sierotę), zostawi go do przejęcia przy następnym starcie nowego programu.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from perno.db.errors import DbError
from perno.db.queries import ID_CHUNK
from perno.db.schema import COMPANY_COLUMNS, companies, maile

LOCK_NAME = "perno_adopt"
LOCK_WAIT_SECONDS = 30


class NotAdopted(DbError):
    """Baza ma maile, ale nie ma jeszcze firm: przejęcie trzeba wykonać jawnie (z kopią zapasową)."""


@dataclass(frozen=True)
class AdoptionState:
    """Stan przejęcia: ile jest firm i maili oraz ile maili nie ma jeszcze firmy."""
    companies: int
    mails: int
    orphans: int


@dataclass(frozen=True)
class AdoptionReport:
    companies_added: int
    rows_linked: int


def _count(conn: Connection, table, *where) -> int:
    return conn.execute(sa.select(sa.func.count()).select_from(table).where(*where)).scalar() or 0


def state(conn: Connection) -> AdoptionState:
    return AdoptionState(
        companies=_count(conn, companies), mails=_count(conn, maile),
        orphans=_count(conn, maile, maile.c.company_id.is_(None)))


def require_adopted(current: AdoptionState) -> None:
    """Odmawia pracy na bazie, która ma maile, a nie ma firm: nowy program niczego by na niej nie zobaczył."""
    if current.companies == 0 and current.mails > 0:
        raise NotAdopted(
            "Baza ma maile, ale jeszcze nie ma firm (tabela podmioty jest pusta). Zatrzymaj panel i demona Automatu, "
            "a potem uruchom: .venv/bin/python -m perno.db --adopt")


@contextmanager
def _exclusive(engine: Engine) -> Iterator[None]:
    """Blokada na czas przejęcia (MySQL `GET_LOCK`): dwa programy startujące naraz nie wstawią tej samej firmy dwa razy.
    sqlite nie potrzebuje jej, bo zapis i tak jest szeregowany."""
    if engine.dialect.name != "mysql":
        yield
        return
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
        taken = lock.execute(sa.text("SELECT GET_LOCK(:name, :wait)"), {"name": LOCK_NAME, "wait": LOCK_WAIT_SECONDS}).scalar()
        if taken != 1:
            raise DbError("Inny program właśnie przypisuje maile do firm — spróbuj za chwilę.")
        try:
            yield
        finally:
            lock.execute(sa.text("SELECT RELEASE_LOCK(:name)"), {"name": LOCK_NAME})


def _insert_missing_companies(conn: Connection, now: datetime) -> int:
    """Firma z każdego wiersza-kotwicy (bez firmy albo wskazującego samego siebie), którego firmy jeszcze nie ma.
    `INSERT … SELECT` z innej tabeli, więc MySQL nie zgłasza błędu 1093 jak przy podzapytaniu do tej samej tabeli."""
    anchor = sa.or_(maile.c.company_id.is_(None), maile.c.company_id == maile.c.id)
    absent = ~sa.exists().where(companies.c.id == maile.c.id)
    source = sa.select(maile.c.id, *[maile.c[name] for name in COMPANY_COLUMNS], sa.literal(now)).where(anchor, absent)
    result = conn.execute(sa.insert(companies).from_select(
        ["id", *COMPANY_COLUMNS, "updated_at"], source))
    return result.rowcount or 0


def _link_orphans(conn: Connection) -> int:
    """`company_id = id`, `position = 1` dla wierszy bez firmy, partiami po `ID_CHUNK` (UPDATE … LIMIT nie działa na sqlite)."""
    ids = [row[0] for row in conn.execute(sa.select(maile.c.id).where(maile.c.company_id.is_(None)).order_by(maile.c.id))]
    for start in range(0, len(ids), ID_CHUNK):
        part = ids[start:start + ID_CHUNK]
        conn.execute(sa.update(maile).where(maile.c.id.in_(part), maile.c.company_id.is_(None))
                     .values(company_id=maile.c.id, position=1))
    return len(ids)


def adopt(engine: Engine, now: datetime) -> AdoptionReport:
    """Zakłada brakujące firmy i przypisuje do nich wiersze bez firmy; w jednej transakcji, pod blokadą."""
    with _exclusive(engine):
        connection = engine.connect()
        if engine.dialect.name == "mysql":  # bez blokad następnego klucza na `maile`: demon może w tym czasie zajmować wiersze
            connection = connection.execution_options(isolation_level="READ COMMITTED")
        with connection as conn, conn.begin():
            return AdoptionReport(_insert_missing_companies(conn, now), _link_orphans(conn))


@dataclass(frozen=True)
class Diagnostics:
    """Wynik `--check`: niezgodności między `maile` a `podmioty` (wszystkie powinny być zerami)."""
    state: AdoptionState
    companies_without_mail: int  # firma bez żadnego wiersza maila (niezmiennik: każda firma ma ≥ 1 wiersz)
    mails_without_company: int  # wiersz wskazuje firmę, której nie ma
    mirror_drift: int  # wiersze, w których stare kolumny firmowe różnią się od firmy (stary kod czyta nieaktualne dane)

    @property
    def clean(self) -> bool:
        return not (self.state.orphans or self.companies_without_mail or self.mails_without_company or self.mirror_drift)


def diagnose(conn: Connection) -> Diagnostics:
    has_mail = sa.exists().where(maile.c.company_id == companies.c.id)
    has_company = sa.exists().where(companies.c.id == maile.c.company_id)
    drift = sa.or_(*[maile.c[name].is_distinct_from(companies.c[name]) for name in COMPANY_COLUMNS])
    drifted = conn.execute(
        sa.select(sa.func.count()).select_from(maile.join(companies, companies.c.id == maile.c.company_id)).where(drift)
    ).scalar() or 0
    return Diagnostics(
        state=state(conn),
        companies_without_mail=_count(conn, companies, ~has_mail),
        mails_without_company=_count(conn, maile, maile.c.company_id.is_not(None), ~has_company),
        mirror_drift=drifted,
    )
