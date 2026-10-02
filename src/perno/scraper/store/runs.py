"""Przebiegi skanowania: start, puls (heartbeat), zatrzymanie, zakończenie i odzyskiwanie po awarii."""

from __future__ import annotations

import json
import os
import socket
from typing import Any, Callable, Optional

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc

from perno.scraper import statuses as st
from perno.scraper.schema import firmy, skan_przebiegi
from perno.scraper.settings import STALE_AFTER
from perno.scraper.store.core import StoreCore, iso
from perno.scraper.store.errors import RunActive


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


# Heartbeat starszy niż 2 h = martwy, nawet jeśli pid „żyje" (np. ponownie użyty numer). Celowo > 1 h: daty są naiwne-lokalne,
# więc przejście na czas letni daje pozorną różnicę równą godzinie i nie może uśmiercić żyjącego przebiegu.
HARD_STALE_SECONDS = 7200


class RunStore(StoreCore):
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
        values: dict[str, Any] = {"heartbeat_at": now}
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
        data = {k: iso(v) for k, v in dict(row).items()}
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
