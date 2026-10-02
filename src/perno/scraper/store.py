"""DAL skrapera — osobna klasa na tym samym silniku co `Database` (nie rozbudowujemy src/perno/db/__init__.py).

Najważniejsze gwarancje:
  * jedna firma = jedna dzierżawa (`claim_next_firma`, UPDATE z warunkiem na status);
  * zapis strony to JEDNA transakcja, a każda taka transakcja sprawdza dzierżawę (`przebieg_id`) —
    „zombie" po uśpieniu Maca dostaje `LeaseLost` zamiast nadpisywać dane następnego przebiegu;
  * co najwyżej jeden aktywny przebieg (`skan_przebiegi.aktywny` UNIQUE);
  * odzyskiwanie po awarii znosi zmianę czasu (ujemna różnica heartbeatu nie oznacza „stary").

Daty to naiwne czasy lokalne z `db.now()` (konwencja projektu). Dla sqlite (testy/demo) wszystkie
operacje są serializowane blokadą procesu; MySQL korzysta z blokad wierszy.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect

from perno.db import Database, DbError
from perno.scraper import schema
from perno.scraper import statuses as st
from perno.scraper.schema import firmy, firmy_maile, kontakty, skan_przebiegi, strony, strony_tresc
from perno.scraper.settings import STALE_AFTER
from perno.scraper.urls import site_key, validate_start_url

# Heartbeat starszy niż 2 h = martwy, nawet jeśli pid „żyje" (np. ponownie użyty numer). Celowo > 1 h: daty są naiwne-lokalne,
# więc przejście na czas letni daje pozorną różnicę równą godzinie i nie może uśmiercić żyjącego przebiegu.
HARD_STALE_SECONDS = 7200
ROBOTS_MAX_STORED = 512 * 1024
ROBOTS_VIEW_CHARS = 4096
INSERT_CHUNK = 50
RETRY_CODES = frozenset({1205, 1213, 2006, 2013})  # lock wait timeout, deadlock, MySQL gone away / lost
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ---- błędy -------------------------------------------------------------------

class StoreError(DbError):
    pass


class RunActive(StoreError):
    pass


class LeaseLost(StoreError):
    pass


class FirmaBusy(StoreError):
    pass


class DomainTaken(StoreError):
    pass


class InvalidFirma(StoreError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or st.PRZYCZYNY.get(code, code))
        self.code = code


class NotFound(StoreError):
    pass


# ---- dane przekazywane przez crawler -------------------------------------------

@dataclass
class LinkRecord:
    url: str
    url_hash: str
    depth: int
    priority: int
    source: str = "link"


@dataclass
class PageRecord:
    url: str
    url_hash: str
    depth: int
    priority: int
    source: str
    status: str
    parent_id: Optional[int] = None
    http_status: Optional[int] = None
    content_type: Optional[str] = None
    encoding: Optional[str] = None
    bytes: int = 0
    elapsed_ms: Optional[int] = None
    title: Optional[str] = None
    description: Optional[str] = None
    content_hash: Optional[str] = None
    error: Optional[str] = None
    text: Optional[str] = None
    html_gz: Optional[bytes] = None
    links: list = field(default_factory=list)  # LinkRecord
    contacts: list = field(default_factory=list)  # contacts.Contact
    counts: bool = True  # strona kosztowała żądanie sieciowe → liczy się do limitów firmy


# ---- pomocnicze -------------------------------------------------------------------

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


def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%S") if isinstance(value, datetime) else value


def pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # proces istnieje, tylko nie nasz
    except OSError:
        return False
    return True


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


class Store:
    def __init__(self, db: Database):
        self.db = db
        self.engine = db.engine
        self.backend = db.backend
        if self.backend not in ("mysql", "sqlite"):
            raise StoreError(f"Skraper obsługuje MySQL i sqlite (testy), nie {self.backend}.")
        _prepare_sqlite(self.engine)
        self._lock = threading.RLock() if self.backend == "sqlite" else None

    # ---- transakcje ------------------------------------------------------------
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
        args = getattr(getattr(exc, "orig", None), "args", ())
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
            if self.backend == "mysql":
                stmt = mysql_dialect.insert(table).values(chunk)
                stmt = stmt.on_duplicate_key_update(**{noop_column: table.c[noop_column]})
            else:
                stmt = sqlite_dialect.insert(table).values(chunk).on_conflict_do_nothing()
            conn.execute(stmt)

    def ensure_schema(self) -> None:
        if self._lock:
            with self._lock:
                schema.skraper_metadata.create_all(self.engine)
        else:
            schema.skraper_metadata.create_all(self.engine)

    # ---- firmy: zasilanie --------------------------------------------------------
    def seed_from_maile(self) -> dict:
        """Jedna firma = jedna domena (site_key). Idempotentne; wiersze `maile` bez poprawnego www/platformy są pomijane."""
        skipped: dict = {}
        plan: dict = {}
        links: list = []
        for row in self.db.iter_rows(columns=("id", "company", "website", "segment")):
            url, reason = validate_start_url(row.get("website") or "")
            domena = site_key(url) if url else None
            if not domena or not ascii_or_none(domena, 253):
                reason = reason or "zly_url"
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            plan.setdefault(domena, {"url": url, "nazwa": row.get("company"), "segment": row.get("segment")})
            links.append((row["id"], domena))
        added = self._add_domains(plan, "maile")
        with self._tx() as conn:
            ids = self._ids_for(conn, list(plan))
            self._insert_noop(conn, firmy_maile, [{"maile_id": mid, "firma_id": ids[d]} for mid, d in links if d in ids],
                              "maile_id")
            total = conn.execute(sa.select(sa.func.count()).select_from(firmy)).scalar() or 0
        return {"dodano": added, "pominieto": sum(skipped.values()), "powody": skipped, "razem": total}

    def _add_domains(self, plan: dict, zrodlo: str) -> int:
        if not plan:
            return 0
        now = self.db.now()
        with self._tx() as conn:
            existing = self._ids_for(conn, list(plan))
            fresh = []
            for domena, info in plan.items():
                if domena in existing:
                    continue
                fresh.append({
                    "domena": domena, "nazwa": clean_text(info.get("nazwa"), 255) or None, "website": info["url"][:1000],
                    "origin": self._origin(info["url"]), "segment": clean_text(info.get("segment"), 32) or None,
                    "zrodlo": zrodlo, "skan_status": st.FIRMA_NIESKANOWANA, "stron_pobranych": 0, "bajty_pobranych": 0,
                    "updated_at": now,
                })
            self._insert_noop(conn, firmy, fresh, "id")
        return len(fresh)

    @staticmethod
    def _origin(url: str) -> str:
        match = re.match(r"^(https?://[^/?#]+)", url)
        return match.group(1)[:300] if match else ""

    @staticmethod
    def _ids_for(conn, domains: list) -> dict:
        found: dict = {}
        for start in range(0, len(domains), 200):
            chunk = domains[start:start + 200]
            for row in conn.execute(sa.select(firmy.c.domena, firmy.c.id).where(firmy.c.domena.in_(chunk))):
                found[row.domena] = row.id
        return found

    def add_manual(self, url: str, nazwa: Optional[str] = None) -> dict:
        normalized, reason = validate_start_url(url)
        domena = site_key(normalized) if normalized else None
        if not normalized or not domena:
            raise InvalidFirma(reason or "zly_url")
        added = self._add_domains({domena: {"url": normalized, "nazwa": nazwa}}, "reczna")
        with self._read() as conn:
            firma_id = conn.execute(sa.select(firmy.c.id).where(firmy.c.domena == domena)).scalar()
        return {"id": firma_id, "domena": domena, "nowa": bool(added)}

    # ---- firmy: odczyt ---------------------------------------------------------------
    def get_firma(self, firma_id: int) -> Optional[dict]:
        with self._read() as conn:
            row = conn.execute(sa.select(firmy).where(firmy.c.id == firma_id)).mappings().first()
        return dict(row) if row else None

    def find_firma(self, ref: str) -> Optional[dict]:
        ref = (ref or "").strip()
        with self._read() as conn:
            if ref.isdigit():
                row = conn.execute(sa.select(firmy).where(firmy.c.id == int(ref))).mappings().first()
            else:
                url, _ = validate_start_url(ref)
                key = site_key(url) if url else None
                row = conn.execute(sa.select(firmy).where(firmy.c.domena == key)).mappings().first() if key else None
        return dict(row) if row else None

    def status_overview(self) -> dict:
        with self._read() as conn:
            counts = {row.skan_status: row.n for row in conn.execute(
                sa.select(firmy.c.skan_status, sa.func.count().label("n")).group_by(firmy.c.skan_status))}
            totals = conn.execute(sa.select(
                sa.func.coalesce(sa.func.sum(firmy.c.stron_pobranych), 0),
                sa.func.coalesce(sa.func.sum(firmy.c.bajty_pobranych), 0))).first()
            contact_counts = {row.typ: row.n for row in conn.execute(
                sa.select(kontakty.c.typ, sa.func.count().label("n")).group_by(kontakty.c.typ))}
        return {"firmy_wg_statusu": counts, "firmy": sum(counts.values()), "kolejka": counts.get(st.FIRMA_W_KOLEJCE, 0),
                "skanowane": counts.get(st.FIRMA_SKANOWANIE, 0), "stron": int(totals[0]), "bajty": int(totals[1]),
                "kontakty": contact_counts}

    def list_firmy(self, status: Optional[str] = None, q: str = "", offset: int = 0, limit: int = 50) -> tuple:
        def count_of(typ: str):
            return sa.select(sa.func.count()).where(kontakty.c.firma_id == firmy.c.id, kontakty.c.typ == typ).scalar_subquery()

        cols = [firmy.c.id, firmy.c.domena, firmy.c.nazwa, firmy.c.website, firmy.c.segment, firmy.c.skan_status,
                firmy.c.skan_przyczyna, firmy.c.ostatni_blad, firmy.c.stron_pobranych, firmy.c.bajty_pobranych,
                firmy.c.glebokosc_max, firmy.c.skan_end_at, firmy.c.domena_docelowa, firmy.c.nastepna_proba_po]
        where = []
        if status:
            where.append(firmy.c.skan_status == status)
        if q.strip():
            like = "%" + q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(sa.or_(sa.func.lower(firmy.c.domena).like(like, escape="\\"),
                                sa.func.lower(sa.func.coalesce(firmy.c.nazwa, "")).like(like, escape="\\")))
        stmt = (sa.select(*cols, count_of("email").label("emaile"), count_of("telefon").label("telefony"))
                .where(*where).order_by(firmy.c.id).offset(max(0, offset)).limit(max(1, min(500, limit))))
        with self._read() as conn:
            rows = [{k: _iso(v) for k, v in dict(r).items()} for r in conn.execute(stmt).mappings()]
            total = conn.execute(sa.select(sa.func.count()).select_from(firmy).where(*where)).scalar() or 0
        return rows, total

    def firma_detail(self, firma_id: int, page: int = 0, per_page: int = 100) -> Optional[dict]:
        per_page = max(1, min(500, per_page))
        with self._read() as conn:
            row = conn.execute(sa.select(firmy).where(firmy.c.id == firma_id)).mappings().first()
            if not row:
                return None
            firm = {k: _iso(v) for k, v in dict(row).items()}
            robots = firm.get("robots_txt") or ""
            firm["robots_txt"] = robots[:ROBOTS_VIEW_CHARS]
            firm["robots_obcięty"] = len(robots) > ROBOTS_VIEW_CHARS
            page_cols = [strony.c.id, strony.c.url, strony.c.glebokosc, strony.c.priorytet, strony.c.zrodlo, strony.c.status,
                         strony.c.http_status, strony.c.content_type, strony.c.bajty, strony.c.czas_ms, strony.c.tytul,
                         strony.c.blad, strony.c.pobrano_at]
            pages = [{k: _iso(v) for k, v in dict(r).items()} for r in conn.execute(
                sa.select(*page_cols).where(strony.c.firma_id == firma_id).order_by(strony.c.id)
                .offset(max(0, page) * per_page).limit(per_page)).mappings()]
            total = conn.execute(sa.select(sa.func.count()).select_from(strony).where(strony.c.firma_id == firma_id)).scalar() or 0
            people = [{k: _iso(v) for k, v in dict(r).items()} for r in conn.execute(
                sa.select(kontakty).where(kontakty.c.firma_id == firma_id)
                .order_by(kontakty.c.typ, kontakty.c.liczba_stron.desc(), kontakty.c.id)).mappings()]
        return {"firma": firm, "strony": pages, "strony_razem": total, "kontakty": people}

    # ---- przebiegi -----------------------------------------------------------------
    def start_run(self, params: dict, *, pid: Optional[int] = None, log_path: Optional[str] = None) -> int:
        now = self.db.now()
        params = {**params, "host": socket.gethostname()}  # pid ma sens tylko na tym komputerze (patrz recover_dead_runs)
        try:
            with self._tx() as conn:
                result = conn.execute(sa.insert(skan_przebiegi).values(
                    status=st.RUN_URUCHAMIANIE, aktywny=1, stop_requested=False, start_at=now, heartbeat_at=now, pid=pid,
                    parametry=json.dumps(params, ensure_ascii=True, default=str), log_path=log_path))
                return int(result.inserted_primary_key[0])
        except sa_exc.IntegrityError as exc:
            raise RunActive("Skraper już działa — poczekaj na koniec przebiegu albo go zatrzymaj.") from exc

    def attach_run(self, run_id: int, *, pid: Optional[int] = None, log_path: Optional[str] = None,
                   status: str = st.RUN_DZIALA) -> bool:
        now = self.db.now()
        values = {"status": status, "heartbeat_at": now}
        if pid is not None:
            values["pid"] = pid
        if log_path is not None:
            values["log_path"] = log_path
        with self._tx() as conn:
            result = conn.execute(sa.update(skan_przebiegi)
                                  .where(skan_przebiegi.c.id == run_id, skan_przebiegi.c.aktywny == 1).values(**values))
            return result.rowcount == 1

    def heartbeat(self, run_id: int, stats: Optional[dict] = None) -> bool:
        """Zapisuje puls i statystyki; zwraca True, gdy użytkownik poprosił o zatrzymanie (lub przebieg nie jest już aktywny)."""
        now = self.db.now()
        values = {"heartbeat_at": now}
        if stats is not None:
            values["statystyki"] = json.dumps(stats, ensure_ascii=True, default=str)
        with self._tx() as conn:
            conn.execute(sa.update(skan_przebiegi)
                         .where(skan_przebiegi.c.id == run_id, skan_przebiegi.c.aktywny == 1).values(**values))
            # Stan odczytujemy SELECT-em, nie z rowcount: UPDATE bez zmiany wartości (dwa pulsy w tej samej sekundzie)
            # potrafi zwrócić 0 zależnie od flagi FOUND_ROWS sterownika, a to nie może zatrzymać crawlera.
            row = conn.execute(sa.select(skan_przebiegi.c.stop_requested, skan_przebiegi.c.aktywny)
                               .where(skan_przebiegi.c.id == run_id)).first()
        return row is None or not row.aktywny or bool(row.stop_requested)

    def request_stop(self) -> bool:
        with self._tx() as conn:
            active = conn.execute(sa.select(skan_przebiegi.c.id).where(skan_przebiegi.c.aktywny == 1)).first()
            if active is None:
                return False
            conn.execute(sa.update(skan_przebiegi).where(skan_przebiegi.c.aktywny == 1).values(stop_requested=True))
            return True

    def finish_run(self, run_id: int, status: str, stats: Optional[dict] = None) -> None:
        now = self.db.now()
        values = {"status": status, "aktywny": None, "end_at": now}
        if stats is not None:
            values["statystyki"] = json.dumps(stats, ensure_ascii=True, default=str)
        with self._tx() as conn:
            conn.execute(sa.update(skan_przebiegi).where(skan_przebiegi.c.id == run_id).values(**values))
            # Firmy, których ten przebieg nie zdążył zamknąć (np. po utracie bazy), wracają do kolejki — inaczej
            # zostałyby w „skanowanie" na stałe, bo odzyskiwanie patrzy wyłącznie na przebiegi aktywne.
            conn.execute(sa.update(firmy)
                         .where(firmy.c.przebieg_id == run_id, firmy.c.skan_status == st.FIRMA_SKANOWANIE)
                         .values(skan_status=st.FIRMA_W_KOLEJCE, skan_przyczyna=None, updated_at=now,
                                 ostatni_blad="Przebieg zakończył się przed ukończeniem tej firmy — wróciła do kolejki."))

    def get_run(self, run_id: int) -> Optional[dict]:
        with self._read() as conn:
            row = conn.execute(sa.select(skan_przebiegi).where(skan_przebiegi.c.id == run_id)).mappings().first()
        return self._run_dict(row)

    def active_run(self) -> Optional[dict]:
        with self._read() as conn:
            row = conn.execute(sa.select(skan_przebiegi).where(skan_przebiegi.c.aktywny == 1)).mappings().first()
        return self._run_dict(row)

    def latest_run(self) -> Optional[dict]:
        with self._read() as conn:
            row = conn.execute(sa.select(skan_przebiegi).order_by(skan_przebiegi.c.id.desc()).limit(1)).mappings().first()
        return self._run_dict(row)

    @staticmethod
    def _run_dict(row) -> Optional[dict]:
        if not row:
            return None
        data = {k: _iso(v) for k, v in dict(row).items()}
        for key in ("parametry", "statystyki"):
            try:
                data[key] = json.loads(data[key]) if data.get(key) else {}
            except ValueError:
                data[key] = {}
        return data

    def recover_dead_runs(self, *, stale_after: float = STALE_AFTER, hard_after: float = HARD_STALE_SECONDS,
                          is_alive: Callable = pid_alive) -> list:
        """Przebieg uznajemy za martwy, gdy: (a) jego proces (zapisany pid, ten sam komputer) już nie istnieje —
        to dowód wystarczający, nie czekamy na heartbeat; albo (b) heartbeat jest nieświeży ORAZ proces nie żyje
        (albo brak pid, np. start się nie udał); albo (c) heartbeat starszy niż `hard_after` mimo „żyjącego" pid
        (np. ponownie użyty numer). Ujemna różnica (cofnięcie zegara po zmianie czasu) sama nie oznacza „nieświeży"."""
        now = self.db.now()
        recovered: list = []
        with self._read() as conn:
            active = conn.execute(sa.select(skan_przebiegi.c.id, skan_przebiegi.c.pid, skan_przebiegi.c.heartbeat_at,
                                            skan_przebiegi.c.parametry).where(skan_przebiegi.c.aktywny == 1)).all()
        here = socket.gethostname()
        for run in active:
            try:
                run_host = (json.loads(run.parametry) if run.parametry else {}).get("host")
            except ValueError:
                run_host = None
            age = None if run.heartbeat_at is None else (now - run.heartbeat_at).total_seconds()
            stale = age is None or age > stale_after
            local = run_host in (None, here)
            if run.pid and local and not is_alive(run.pid):
                dead = True
            else:
                dead = stale and (age is None or age > hard_after or not (run.pid and local and is_alive(run.pid)))
            if not dead:
                continue
            with self._tx() as conn:
                result = conn.execute(sa.update(skan_przebiegi)
                                      .where(skan_przebiegi.c.id == run.id, skan_przebiegi.c.aktywny == 1)
                                      .values(status=st.RUN_PRZERWANY, aktywny=None, end_at=now))
                if result.rowcount == 1:
                    conn.execute(sa.update(firmy)
                                 .where(firmy.c.przebieg_id == run.id, firmy.c.skan_status == st.FIRMA_SKANOWANIE)
                                 .values(skan_status=st.FIRMA_W_KOLEJCE, skan_przyczyna=None,
                                         ostatni_blad="Poprzedni przebieg został przerwany — wznowiono w kolejce.",
                                         updated_at=now))
                    recovered.append(run.id)
        return recovered

    # ---- kolejka i dzierżawa firm ---------------------------------------------------------
    def enqueue(self, firma_ids: Optional[list] = None, statuses: Optional[tuple] = None) -> int:
        """Ustawia `w_kolejce`. Z `firma_ids`: te firmy (poza aktualnie skanowanymi); bez: wg `statuses`
        (domyślnie nieskanowane i z błędem). Nigdy nie rusza firm w trakcie skanowania."""
        now = self.db.now()
        where = [firmy.c.skan_status != st.FIRMA_SKANOWANIE]
        if firma_ids is not None:
            if not firma_ids:
                return 0
            where.append(firmy.c.id.in_([int(i) for i in firma_ids]))
        else:
            where.append(firmy.c.skan_status.in_(statuses or st.FIRMA_DOMYSLNIE_DO_SKANU))
        with self._tx() as conn:
            result = conn.execute(sa.update(firmy).where(*where).values(
                skan_status=st.FIRMA_W_KOLEJCE, skan_przyczyna=None, ostatni_blad=None, nastepna_proba_po=None,
                skan_end_at=None, updated_at=now))
            return result.rowcount

    def existing_firma_ids(self, firma_ids: list) -> set:
        found: set = set()
        ids = [int(i) for i in firma_ids]
        with self._read() as conn:
            for start in range(0, len(ids), 200):
                found.update(r.id for r in conn.execute(sa.select(firmy.c.id).where(firmy.c.id.in_(ids[start:start + 200]))))
        return found

    def count_runnable(self, firma_ids: Optional[list] = None, statuses: Optional[tuple] = None) -> int:
        """Ile firm zajęłby przebieg (jak `enqueue`, ale bez zmian w bazie) — do `--dry-run`."""
        where = [firmy.c.skan_status != st.FIRMA_SKANOWANIE]
        if firma_ids is not None:
            if not firma_ids:
                return 0
            where.append(firmy.c.id.in_([int(i) for i in firma_ids]))
        else:
            where.append(firmy.c.skan_status.in_(statuses or st.FIRMA_DOMYSLNIE_DO_SKANU))
        with self._read() as conn:
            return conn.execute(sa.select(sa.func.count()).select_from(firmy).where(*where)).scalar() or 0

    def requeue(self, firma_ids: list) -> int:
        """„Ponów": tylko firmy w statusach ponawialnych."""
        if not firma_ids:
            return 0
        now = self.db.now()
        with self._tx() as conn:
            result = conn.execute(sa.update(firmy).where(
                firmy.c.id.in_([int(i) for i in firma_ids]), firmy.c.skan_status.in_(st.FIRMA_PONAWIALNE)
            ).values(skan_status=st.FIRMA_W_KOLEJCE, skan_przyczyna=None, ostatni_blad=None, nastepna_proba_po=None,
                     skan_end_at=None, updated_at=now))
            return result.rowcount

    def claim_next_firma(self, run_id: int, only_ids: Optional[list] = None) -> Optional[dict]:
        now = self.db.now()
        for _ in range(5):
            def attempt(conn):
                where = [firmy.c.skan_status == st.FIRMA_W_KOLEJCE,
                         sa.or_(firmy.c.nastepna_proba_po.is_(None), firmy.c.nastepna_proba_po <= now)]
                if only_ids is not None:
                    where.append(firmy.c.id.in_([int(i) for i in only_ids]))
                ids = [r.id for r in conn.execute(sa.select(firmy.c.id).where(*where).order_by(firmy.c.id).limit(10))]
                if not ids:
                    return "empty"
                for firma_id in ids:
                    result = conn.execute(sa.update(firmy)
                                          .where(firmy.c.id == firma_id, firmy.c.skan_status == st.FIRMA_W_KOLEJCE)
                                          .values(skan_status=st.FIRMA_SKANOWANIE, przebieg_id=run_id, skan_start_at=now,
                                                  skan_end_at=None, skan_przyczyna=None, ostatni_blad=None, updated_at=now))
                    if result.rowcount == 1:
                        return dict(conn.execute(sa.select(firmy).where(firmy.c.id == firma_id)).mappings().first())
                return None  # wszystkie dziesięć zajęły inne wątki — pobierz kolejne

            outcome = self._run(attempt)
            if outcome == "empty":
                return None
            if outcome:
                return outcome
        return None

    def release_firma(self, firma_id: int, run_id: int, status: str, przyczyna: Optional[str] = None,
                      blad: Optional[str] = None, domena_docelowa: Optional[str] = None,
                      nastepna_proba_po: Optional[datetime] = None) -> bool:
        """Zamyka dzierżawę. False = dzierżawa utracona (ktoś inny przejął firmę) — nic nie zapisano."""
        now = self.db.now()
        values = {"skan_status": status, "skan_przyczyna": przyczyna, "ostatni_blad": clean_text(blad, 500),
                  "skan_end_at": now, "updated_at": now, "nastepna_proba_po": nastepna_proba_po}
        if domena_docelowa:
            values["domena_docelowa"] = ascii_or_none(domena_docelowa, 253)
        if status == st.FIRMA_W_KOLEJCE:
            values["skan_end_at"] = None
        with self._tx() as conn:
            result = conn.execute(sa.update(firmy).where(
                firmy.c.id == firma_id, firmy.c.przebieg_id == run_id, firmy.c.skan_status == st.FIRMA_SKANOWANIE
            ).values(**values))
            return result.rowcount == 1

    @staticmethod
    def _check_lease(conn, firma_id: int, run_id: int) -> None:
        """SELECT … FOR UPDATE: weryfikuje dzierżawę i serializuje zapisy firmy (portowalne, w odróżnieniu od rowcount)."""
        found = conn.execute(sa.select(firmy.c.id).where(
            firmy.c.id == firma_id, firmy.c.przebieg_id == run_id, firmy.c.skan_status == st.FIRMA_SKANOWANIE
        ).with_for_update()).first()
        if found is None:
            raise LeaseLost(f"Firma {firma_id} nie jest już dzierżawiona przez przebieg {run_id}.")

    def save_robots(self, firma_id: int, run_id: int, status: str, text: Optional[str], crawl_delay: Optional[float]) -> None:
        now = self.db.now()

        def work(conn):
            self._check_lease(conn, firma_id, run_id)
            conn.execute(sa.update(firmy).where(firmy.c.id == firma_id).values(
                robots_status=status, robots_txt=clean_text(text, ROBOTS_MAX_STORED), robots_at=now,
                crawl_delay=crawl_delay, updated_at=now))

        self._run(work)

    # ---- frontier -----------------------------------------------------------------
    def load_frontier(self, firma_id: int, limit: int) -> list:
        with self._read() as conn:
            rows = conn.execute(sa.select(strony.c.id, strony.c.url, strony.c.url_hash, strony.c.glebokosc,
                                          strony.c.priorytet, strony.c.zrodlo)
                                .where(strony.c.firma_id == firma_id, strony.c.status == st.STRONA_OCZEKUJE)
                                .order_by(strony.c.priorytet, strony.c.id).limit(limit)).mappings().all()
        return [dict(r) for r in rows]

    def pending_count(self, firma_id: int) -> int:
        with self._read() as conn:
            return conn.execute(sa.select(sa.func.count()).select_from(strony).where(
                strony.c.firma_id == firma_id, strony.c.status == st.STRONA_OCZEKUJE)).scalar() or 0

    def known_urls(self, firma_id: int) -> list:
        """[(url, url_hash)] wszystkich znanych stron firmy — do odbudowy liczników pułapek po wznowieniu."""
        with self._read() as conn:
            return [(r.url, r.url_hash) for r in conn.execute(
                sa.select(strony.c.url, strony.c.url_hash).where(strony.c.firma_id == firma_id).order_by(strony.c.id))]

    def known_url_hashes(self, firma_id: int) -> set:
        with self._read() as conn:
            return {r.url_hash for r in conn.execute(sa.select(strony.c.url_hash).where(strony.c.firma_id == firma_id))}

    def content_hashes(self, firma_id: int) -> dict:
        with self._read() as conn:
            rows = conn.execute(sa.select(strony.c.content_hash, sa.func.min(strony.c.id)).where(
                strony.c.firma_id == firma_id, strony.c.content_hash.is_not(None),
                strony.c.status == st.STRONA_POBRANO).group_by(strony.c.content_hash)).all()
        return {r[0]: r[1] for r in rows}

    def add_discovered(self, firma_id: int, run_id: int, links: list, parent_id: Optional[int] = None) -> None:
        """Dodaje URL-e do kolejki (sitemapa, strona startowa) pod kontrolą dzierżawy."""
        now = self.db.now()

        def work(conn):
            self._check_lease(conn, firma_id, run_id)
            self._insert_noop(conn, strony, self._link_rows(firma_id, links, parent_id, now), "id")

        self._run(work)

    @staticmethod
    def _link_rows(firma_id: int, links: list, parent_id: Optional[int], now: datetime) -> list:
        rows = []
        for link in links:
            url = ascii_or_none(link.url, 2000)
            if not url:
                continue
            rows.append({"firma_id": firma_id, "url": url, "url_hash": link.url_hash, "glebokosc": int(link.depth),
                         "priorytet": int(link.priority), "zrodlo": link.source[:8], "rodzic_id": parent_id,
                         "status": st.STRONA_OCZEKUJE, "dodano_at": now})
        return rows

    def _upsert_contact(self, conn, row: dict) -> None:
        """Pierwszy zapis kontaktu zostaje (źródło, rola); każdy kolejny tylko podbija `liczba_stron`."""
        if self.backend == "mysql":
            stmt = mysql_dialect.insert(kontakty).values(**row).on_duplicate_key_update(
                liczba_stron=kontakty.c.liczba_stron + 1)
        else:
            stmt = sqlite_dialect.insert(kontakty).values(**row).on_conflict_do_update(
                index_elements=["firma_id", "typ", "wartosc"], set_={"liczba_stron": kontakty.c.liczba_stron + 1})
        conn.execute(stmt)

    # ---- zapis wyniku strony -----------------------------------------------------------
    def save_result(self, firma_id: int, run_id: int, rec: PageRecord) -> int:
        """Jedna transakcja: wiersz strony, treść, nowe linki, kontakty i liczniki firmy. `LeaseLost` = nic nie zapisano."""
        now = self.db.now()
        page_url = ascii_or_none(rec.url, 2000)
        if not page_url:
            raise StoreError("URL strony nie jest czystym ASCII (normalizacja zawiodła).")

        def work(conn) -> int:
            self._check_lease(conn, firma_id, run_id)
            values = {
                "status": rec.status, "http_status": rec.http_status,
                "content_type": clean_text(rec.content_type, 100), "kodowanie": clean_text(rec.encoding, 24),
                "bajty": int(rec.bytes or 0), "czas_ms": rec.elapsed_ms, "tytul": clean_text(rec.title, 500),
                "opis": clean_text(rec.description, 1000), "content_hash": rec.content_hash,
                "blad": clean_text(rec.error, 500), "pobrano_at": now if rec.counts else None,
            }
            existing = conn.execute(sa.select(strony.c.id).where(
                strony.c.firma_id == firma_id, strony.c.url_hash == rec.url_hash)).scalar()
            if existing is not None:
                conn.execute(sa.update(strony).where(strony.c.id == existing).values(**values))
                strona_id = int(existing)
            else:
                result = conn.execute(sa.insert(strony).values(
                    firma_id=firma_id, url=page_url, url_hash=rec.url_hash, glebokosc=int(rec.depth),
                    priorytet=int(rec.priority), zrodlo=rec.source[:8], rodzic_id=rec.parent_id, dodano_at=now, **values))
                strona_id = int(result.inserted_primary_key[0])

            if rec.text is not None or rec.html_gz is not None:
                conn.execute(sa.delete(strony_tresc).where(strony_tresc.c.strona_id == strona_id))
                conn.execute(sa.insert(strony_tresc).values(
                    strona_id=strona_id, tekst=clean_text(rec.text) if rec.text is not None else None, html_gz=rec.html_gz))

            self._insert_noop(conn, strony, self._link_rows(firma_id, rec.links, strona_id, now), "id")

            for contact in rec.contacts:
                value = clean_text(contact.wartosc, 255)
                if not value:
                    continue
                self._upsert_contact(conn, {
                    "firma_id": firma_id, "typ": contact.typ, "wartosc": value, "zrodlo_url": page_url,
                    "zrodlo_typ": contact.zrodlo_typ, "liczba_stron": 1, "w_domenie": bool(contact.w_domenie),
                    "rola": contact.rola or None, "pierwszy_at": now})

            depth = int(rec.depth)
            counters = {"updated_at": now}
            if rec.counts:
                counters["stron_pobranych"] = firmy.c.stron_pobranych + 1
                counters["bajty_pobranych"] = firmy.c.bajty_pobranych + int(rec.bytes or 0)
                counters["glebokosc_max"] = sa.case((firmy.c.glebokosc_max.is_(None), depth),
                                                    (firmy.c.glebokosc_max < depth, depth), else_=firmy.c.glebokosc_max)
            conn.execute(sa.update(firmy).where(firmy.c.id == firma_id).values(**counters))
            return strona_id

        return self._run(work)

    # ---- zmiany administracyjne -----------------------------------------------------------
    def delete_firma_data(self, firma_id: int) -> None:
        """Usuwa strony, treść i kontakty firmy i resetuje jej stan do „nieskanowana"."""
        now = self.db.now()
        with self._tx() as conn:
            status = conn.execute(sa.select(firmy.c.skan_status).where(firmy.c.id == firma_id).with_for_update()).scalar()
            if status is None:
                raise NotFound(f"Brak firmy {firma_id}.")
            if status == st.FIRMA_SKANOWANIE:
                raise FirmaBusy("Firma jest teraz skanowana — zatrzymaj skraper, zanim usuniesz jej dane.")
            self._wipe(conn, firma_id)
            conn.execute(sa.update(firmy).where(firmy.c.id == firma_id).values(**self._reset_values(now)))

    @staticmethod
    def _wipe(conn, firma_id: int) -> None:
        conn.execute(sa.delete(strony_tresc).where(
            strony_tresc.c.strona_id.in_(sa.select(strony.c.id).where(strony.c.firma_id == firma_id))))
        conn.execute(sa.delete(kontakty).where(kontakty.c.firma_id == firma_id))
        conn.execute(sa.delete(strony).where(strony.c.firma_id == firma_id))

    @staticmethod
    def _reset_values(now: datetime) -> dict:
        return dict(skan_status=st.FIRMA_NIESKANOWANA, skan_przyczyna=None, ostatni_blad=None, przebieg_id=None,
                    robots_status=None, robots_txt=None, robots_at=None, crawl_delay=None, stron_pobranych=0,
                    bajty_pobranych=0, glebokosc_max=None, skan_start_at=None, skan_end_at=None,
                    nastepna_proba_po=None, updated_at=now)

    def use_target_domain(self, firma_id: int) -> dict:
        """Przyjmuje `domena_docelowa` (po przekierowaniu na obcą domenę) jako domenę firmy i kolejkuje ją."""
        now = self.db.now()
        with self._tx() as conn:
            row = conn.execute(sa.select(firmy.c.skan_status, firmy.c.domena_docelowa, firmy.c.domena)
                               .where(firmy.c.id == firma_id).with_for_update()).first()
            if row is None:
                raise NotFound(f"Brak firmy {firma_id}.")
            if row.skan_status == st.FIRMA_SKANOWANIE:
                raise FirmaBusy("Firma jest teraz skanowana.")
            if not row.domena_docelowa:
                raise InvalidFirma("brak_www", "Firma nie ma zapisanej domeny docelowej.")
            url, reason = validate_start_url(row.domena_docelowa)
            key = site_key(url) if url else None
            if not url or not key:
                raise InvalidFirma(reason or "zly_url")
            taken = conn.execute(sa.select(firmy.c.id).where(firmy.c.domena == key, firmy.c.id != firma_id)).scalar()
            if taken is not None:
                raise DomainTaken(st.PRZYCZYNY["domena_docelowa_zajeta"])
            self._wipe(conn, firma_id)
            values = self._reset_values(now)
            values.update(domena=key, website=url[:1000], origin=self._origin(url), domena_docelowa=None,
                          skan_status=st.FIRMA_W_KOLEJCE)
            conn.execute(sa.update(firmy).where(firmy.c.id == firma_id).values(**values))
        return {"id": firma_id, "domena": key}

    def purge_older_than(self, days: int) -> int:
        """Usuwa dane skanu firm zakończonych przed `days` dniami (RODO: minimalizacja przechowywania)."""
        cutoff = self.db.now() - timedelta(days=days)
        with self._read() as conn:
            ids = [r.id for r in conn.execute(sa.select(firmy.c.id).where(
                firmy.c.skan_end_at.is_not(None), firmy.c.skan_end_at < cutoff,
                firmy.c.skan_status != st.FIRMA_SKANOWANIE))]
        purged = 0
        for firma_id in ids:
            try:
                self.delete_firma_data(firma_id)
                purged += 1
            except FirmaBusy:
                continue
        return purged

    def purge_html(self, days: int) -> int:
        """Kasuje tylko zapisany surowy HTML (tekst i kontakty zostają) ze stron pobranych przed `days` dniami."""
        cutoff = self.db.now() - timedelta(days=days)
        with self._tx() as conn:
            old = sa.select(strony.c.id).where(strony.c.pobrano_at.is_not(None), strony.c.pobrano_at < cutoff)
            result = conn.execute(sa.update(strony_tresc).where(
                strony_tresc.c.html_gz.is_not(None), strony_tresc.c.strona_id.in_(old)).values(html_gz=None))
            return result.rowcount
