"""Firmy: zasilanie z tabeli maile, dodawanie ręczne, odczyt pojedynczej firmy oraz zmiany administracyjne
(usunięcie danych, przyjęcie domeny docelowej)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

import sqlalchemy as sa

from perno.scraper import statuses as st
from perno.scraper.schema import firmy, firmy_maile, kontakty, strony, strony_tresc
from perno.scraper.store.core import StoreCore, ascii_or_none, clean_text
from perno.scraper.store.errors import DomainTaken, FirmaBusy, InvalidFirma, NotFound
from perno.scraper.urls import site_key, validate_start_url


class CompanyStore(StoreCore):
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
