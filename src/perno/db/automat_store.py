"""Magazyn automatu wysyłki: zapamiętane ustawienia, stan planu, przebiegi demona i dziennik zdarzeń.

Na tym samym silniku co `Database` i z tą samą obsługą transakcji co magazyn skrapera (`StoreCore`), ale z własnymi
tabelami (`automat_schema`). Daty to naiwne czasy lokalne z `db.now()` (konwencja projektu).
"""

from __future__ import annotations

import json
import socket
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc

from perno.db.automat_schema import (
    STATE_ROW_ID,
    automat_metadata,
    automat_przebiegi,
    automat_stan,
    automat_zdarzenia,
)
from perno.domain.automat_rules import DEFAULT_SETTINGS, AutomatSettings, SettingsInvalid, settings_from_mapping
from perno.scraper.store.core import StoreCore, iso
from perno.scraper.store.runs import pid_alive

RUN_RUNNING = "dziala"
RUN_FINISHED = "zakonczony"
RUN_DEAD = "przerwany"
HARD_STALE_SECONDS = 7200  # heartbeat starszy niż 2 h = martwy nawet przy „żyjącym” pid (ponownie użyty numer)


class DaemonActive(Exception):
    """Demon automatu już działa (jest tylko jeden naraz)."""


@dataclass(frozen=True)
class AutomatState:
    """Odczyt wiersza `automat_stan`: zapamiętane ustawienia i stan planu."""
    settings: AutomatSettings
    changed_at: Optional[datetime]
    armed: bool
    armed_at: Optional[datetime]
    armed_until: Optional[datetime]
    paused: bool
    pause_reason: str
    streak: int
    settings_ok: bool = True  # False: zapisane ustawienia były nieczytelne, użyto domyślnych


class AutomatStore(StoreCore):
    """Operacje na tabelach automatu; jeden obiekt na proces (sqlite serializuje się blokadą rdzenia)."""

    def ensure_schema(self) -> None:
        """Zakłada tabele i jedyny wiersz stanu (bez nadpisywania istniejącego)."""
        with self._lock or nullcontext():
            automat_metadata.create_all(self.engine)
        with self._tx() as conn:
            self._insert_noop(conn, automat_stan, [{"id": STATE_ROW_ID}])

    # ---- stan i ustawienia ----------------------------------------------------------------------------------------

    def get_state(self) -> AutomatState:
        """Aktualne ustawienia i stan planu."""
        with self._read() as conn:
            row = conn.execute(sa.select(automat_stan).where(automat_stan.c.id == STATE_ROW_ID)).mappings().first()
        if row is None:
            return AutomatState(DEFAULT_SETTINGS, None, False, None, None, False, "", 0)
        settings, ok = DEFAULT_SETTINGS, True
        if row["ustawienia"]:
            try:
                settings = settings_from_mapping(json.loads(row["ustawienia"]))
            except (ValueError, TypeError, SettingsInvalid):
                ok = False  # nieczytelne ustawienia nie mogą zatrzymać demona ani panelu; domyślne są ostrożne
        return AutomatState(settings, row["zmieniono_at"], bool(row["uzbrojony"]), row["uzbrojony_at"],
                            row["uzbrojony_do"], bool(row["wstrzymany"]), row["powod_pauzy"] or "",
                            int(row["bledy_z_rzedu"] or 0), ok)

    def save_settings(self, settings: AutomatSettings) -> bool:
        """Zapisuje ustawienia. Zmiana przy uzbrojonym planie rozbraja go (trzeba potwierdzić od nowa); zwraca, czy rozbroił."""
        now = self.db.now()
        with self._tx() as conn:
            armed = conn.execute(sa.select(automat_stan.c.uzbrojony)
                                 .where(automat_stan.c.id == STATE_ROW_ID).with_for_update()).scalar()
            values: dict = {"ustawienia": json.dumps(settings.to_json(), ensure_ascii=True), "zmieniono_at": now}
            if armed:
                values.update(uzbrojony=False, wstrzymany=False, powod_pauzy=None, bledy_z_rzedu=0)
            conn.execute(sa.update(automat_stan).where(automat_stan.c.id == STATE_ROW_ID).values(**values))
        return bool(armed)

    def arm(self, until: datetime, confirmed: dict) -> None:
        """Uzbraja plan do `until` i zapisuje, jakie ustawienia człowiek zatwierdził."""
        with self._tx() as conn:
            conn.execute(sa.update(automat_stan).where(automat_stan.c.id == STATE_ROW_ID).values(
                uzbrojony=True, uzbrojony_at=self.db.now(), uzbrojony_do=until, wstrzymany=False, powod_pauzy=None,
                bledy_z_rzedu=0, potwierdzone=json.dumps(confirmed, ensure_ascii=True, default=str)))

    def disarm(self) -> bool:
        """Rozbraja plan; zwraca, czy był uzbrojony."""
        with self._tx() as conn:
            was = conn.execute(sa.select(automat_stan.c.uzbrojony)
                               .where(automat_stan.c.id == STATE_ROW_ID).with_for_update()).scalar()
            conn.execute(sa.update(automat_stan).where(automat_stan.c.id == STATE_ROW_ID).values(
                uzbrojony=False, wstrzymany=False, powod_pauzy=None, bledy_z_rzedu=0))
        return bool(was)

    def pause(self, reason: str) -> None:
        """Wstrzymuje wysyłkę (plan zostaje uzbrojony); powód widzi użytkownik w panelu."""
        with self._tx() as conn:
            conn.execute(sa.update(automat_stan).where(automat_stan.c.id == STATE_ROW_ID)
                         .values(wstrzymany=True, powod_pauzy=reason[:500]))

    def resume(self) -> None:
        """Wznawia wstrzymany plan i zeruje serię błędów."""
        with self._tx() as conn:
            conn.execute(sa.update(automat_stan).where(automat_stan.c.id == STATE_ROW_ID)
                         .values(wstrzymany=False, powod_pauzy=None, bledy_z_rzedu=0))

    def set_streak(self, streak: int) -> None:
        """Zapamiętuje serię kolejnych błędów (przeżywa restart demona)."""
        with self._tx() as conn:
            conn.execute(sa.update(automat_stan).where(automat_stan.c.id == STATE_ROW_ID).values(bledy_z_rzedu=streak))

    # ---- przebiegi demona ------------------------------------------------------------------------------------------

    def start_run(self, pid: int) -> int:
        """Rejestruje działający proces demona; rzuca `DaemonActive`, gdy inny już działa."""
        now = self.db.now()
        try:
            with self._tx() as conn:
                result = conn.execute(sa.insert(automat_przebiegi).values(
                    status=RUN_RUNNING, aktywny=1, start_at=now, heartbeat_at=now, pid=pid, host=socket.gethostname()))
                return int(result.inserted_primary_key[0])
        except sa_exc.IntegrityError as exc:
            raise DaemonActive("Demon automatu już działa.") from exc

    def heartbeat(self, run_id: int, stats: Optional[dict] = None) -> None:
        """Zapisuje znak życia i (opcjonalnie) stan pętli do pokazania w panelu."""
        values: dict = {"heartbeat_at": self.db.now()}
        if stats is not None:
            values["statystyki"] = json.dumps(stats, ensure_ascii=True, default=str)
        with self._tx() as conn:
            conn.execute(sa.update(automat_przebiegi)
                         .where(automat_przebiegi.c.id == run_id, automat_przebiegi.c.aktywny == 1).values(**values))

    def finish_run(self, run_id: int, status: str = RUN_FINISHED) -> None:
        """Zamyka przebieg (zwalnia miejsce dla następnego demona)."""
        with self._tx() as conn:
            conn.execute(sa.update(automat_przebiegi).where(automat_przebiegi.c.id == run_id)
                         .values(status=status, aktywny=None, end_at=self.db.now()))

    def active_run(self) -> Optional[dict]:
        """Działający demon (albo None)."""
        with self._read() as conn:
            row = conn.execute(sa.select(automat_przebiegi)
                               .where(automat_przebiegi.c.aktywny == 1)).mappings().first()
        return self._run_dict(row)

    def latest_run(self) -> Optional[dict]:
        """Ostatni przebieg (działający lub zakończony)."""
        with self._read() as conn:
            row = conn.execute(sa.select(automat_przebiegi)
                               .order_by(automat_przebiegi.c.id.desc()).limit(1)).mappings().first()
        return self._run_dict(row)

    @staticmethod
    def _run_dict(row) -> Optional[dict]:
        if not row:
            return None
        data = {key: iso(value) for key, value in dict(row).items()}
        try:
            data["statystyki"] = json.loads(data["statystyki"]) if data.get("statystyki") else {}
        except ValueError:
            data["statystyki"] = {}
        return data

    def recover_dead_runs(self, *, hard_after: float = HARD_STALE_SECONDS,
                          is_alive: Callable = pid_alive) -> list:
        """Zamyka przebiegi, których proces już nie istnieje (ten sam komputer) albo których puls jest bardzo stary."""
        now = self.db.now()
        with self._read() as conn:
            active = conn.execute(sa.select(automat_przebiegi.c.id, automat_przebiegi.c.pid,
                                            automat_przebiegi.c.heartbeat_at, automat_przebiegi.c.host)
                                  .where(automat_przebiegi.c.aktywny == 1)).all()
        recovered = []
        for run in active:
            local = run.host in (None, socket.gethostname())
            age = None if run.heartbeat_at is None else (now - run.heartbeat_at).total_seconds()
            dead = (local and run.pid and not is_alive(run.pid)) or age is None or age > hard_after
            if not dead:
                continue
            with self._tx() as conn:
                result = conn.execute(sa.update(automat_przebiegi)
                                      .where(automat_przebiegi.c.id == run.id, automat_przebiegi.c.aktywny == 1)
                                      .values(status=RUN_DEAD, aktywny=None, end_at=now))
                if result.rowcount == 1:
                    recovered.append(run.id)
        return recovered

    # ---- dziennik zdarzeń ------------------------------------------------------------------------------------------

    def add_event(self, level: str, event_type: str, text: str, key: Optional[str] = None) -> int:
        """Zapisuje zdarzenie i zwraca jego id."""
        with self._tx() as conn:
            result = conn.execute(sa.insert(automat_zdarzenia).values(
                ts=self.db.now(), poziom=level, typ=event_type, klucz=key, tresc=text[:4000]))
            return int(result.inserted_primary_key[0])

    def mark_notified(self, event_id: int, channels: list) -> None:
        """Zaznacza, którymi kanałami wysłano powiadomienie o zdarzeniu."""
        with self._tx() as conn:
            conn.execute(sa.update(automat_zdarzenia).where(automat_zdarzenia.c.id == event_id)
                         .values(powiadomiono_at=self.db.now(), kanaly=",".join(channels)[:64]))

    def last_notified_at(self, event_type: str) -> Optional[datetime]:
        """Kiedy ostatnio powiadomiono o zdarzeniu tego typu."""
        with self._read() as conn:
            return conn.execute(sa.select(sa.func.max(automat_zdarzenia.c.powiadomiono_at))
                                .where(automat_zdarzenia.c.typ == event_type)).scalar()

    def count_events(self, event_type: str, since: Optional[datetime] = None) -> int:
        """Ile zdarzeń tego typu zapisano (od `since` włącznie, jeśli podano)."""
        stmt = sa.select(sa.func.count()).select_from(automat_zdarzenia).where(automat_zdarzenia.c.typ == event_type)
        if since is not None:
            stmt = stmt.where(automat_zdarzenia.c.ts >= since)
        with self._read() as conn:
            return int(conn.execute(stmt).scalar() or 0)

    def has_event(self, event_type: str, key: str) -> bool:
        """Czy zdarzenie tego typu z tym kluczem już zapisano (np. podsumowanie danego dnia)."""
        with self._read() as conn:
            return conn.execute(sa.select(automat_zdarzenia.c.id).where(
                automat_zdarzenia.c.typ == event_type, automat_zdarzenia.c.klucz == key).limit(1)).first() is not None

    def recent_events(self, limit: int = 50) -> list:
        """Najnowsze zdarzenia (najnowsze pierwsze) do dziennika w panelu."""
        stmt = sa.select(automat_zdarzenia).order_by(automat_zdarzenia.c.id.desc()).limit(limit)
        with self._read() as conn:
            return [{key: iso(value) for key, value in dict(row).items()} for row in conn.execute(stmt).mappings()]
