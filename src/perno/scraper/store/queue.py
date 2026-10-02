"""Kolejka firm i dzierżawy: kolejkowanie, zajęcie następnej firmy, zwolnienie, zapis robots.txt."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import sqlalchemy as sa

from perno.scraper import statuses as st
from perno.scraper.schema import firmy
from perno.scraper.store.core import StoreCore, ascii_or_none, clean_text

ROBOTS_MAX_STORED = 512 * 1024


class QueueStore(StoreCore):
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

    def save_robots(self, firma_id: int, run_id: int, status: str, text: Optional[str], crawl_delay: Optional[float]) -> None:
        now = self.db.now()

        def work(conn):
            self._check_lease(conn, firma_id, run_id)
            conn.execute(sa.update(firmy).where(firmy.c.id == firma_id).values(
                robots_status=status, robots_txt=clean_text(text, ROBOTS_MAX_STORED), robots_at=now,
                crawl_delay=crawl_delay, updated_at=now))

        self._run(work)
