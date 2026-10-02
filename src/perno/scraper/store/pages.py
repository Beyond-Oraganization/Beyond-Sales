"""Strony firmy: frontier (adresy do pobrania), zapis wyniku jednej strony w jednej transakcji i kontakty."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.sql.dml import Insert

from perno.scraper import statuses as st
from perno.scraper.schema import firmy, kontakty, strony, strony_tresc
from perno.scraper.store.core import StoreCore, ascii_or_none, clean_text
from perno.scraper.store.errors import StoreError
from perno.scraper.store.records import PageRecord


class PageStore(StoreCore):
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
        stmt: Insert
        if self.backend == "mysql":
            stmt = mysql_dialect.insert(kontakty).values(**row).on_duplicate_key_update(
                liczba_stron=kontakty.c.liczba_stron + 1)
        else:
            stmt = sqlite_dialect.insert(kontakty).values(**row).on_conflict_do_update(
                index_elements=["firma_id", "typ", "wartosc"], set_={"liczba_stron": kontakty.c.liczba_stron + 1})
        conn.execute(stmt)

    def save_result(self, firma_id: int, run_id: int, rec: PageRecord) -> int:
        """Jedna transakcja: wiersz strony, treść, nowe linki, kontakty i liczniki firmy. `LeaseLost` = nic nie zapisano."""
        now = self.db.now()
        page_url = ascii_or_none(rec.url, 2000)
        if not page_url:
            raise StoreError("URL strony nie jest czystym ASCII (normalizacja zawiodła).")

        def work(conn) -> int:
            self._check_lease(conn, firma_id, run_id)
            page_id = self._write_page(conn, firma_id, rec, page_url, now)
            self._write_content(conn, page_id, rec)
            self._insert_noop(conn, strony, self._link_rows(firma_id, rec.links, page_id, now), "id")
            self._write_contacts(conn, firma_id, rec, page_url, now)
            self._bump_counters(conn, firma_id, rec, now)
            return page_id

        return self._run(work)

    @staticmethod
    def _write_page(conn, firma_id: int, rec: PageRecord, page_url: str, now: datetime) -> int:
        """Wiersz strony (wstawienie albo aktualizacja po `url_hash`); zwraca id strony."""
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
            return int(existing)
        result = conn.execute(sa.insert(strony).values(
            firma_id=firma_id, url=page_url, url_hash=rec.url_hash, glebokosc=int(rec.depth),
            priorytet=int(rec.priority), zrodlo=rec.source[:8], rodzic_id=rec.parent_id, dodano_at=now, **values))
        return int(result.inserted_primary_key[0])

    @staticmethod
    def _write_content(conn, page_id: int, rec: PageRecord) -> None:
        if rec.text is not None or rec.html_gz is not None:
            conn.execute(sa.delete(strony_tresc).where(strony_tresc.c.strona_id == page_id))
            conn.execute(sa.insert(strony_tresc).values(
                strona_id=page_id, tekst=clean_text(rec.text) if rec.text is not None else None, html_gz=rec.html_gz))

    def _write_contacts(self, conn, firma_id: int, rec: PageRecord, page_url: str, now: datetime) -> None:
        for contact in rec.contacts:
            value = clean_text(contact.wartosc, 255)
            if not value:
                continue
            self._upsert_contact(conn, {
                "firma_id": firma_id, "typ": contact.typ, "wartosc": value, "zrodlo_url": page_url,
                "zrodlo_typ": contact.zrodlo_typ, "liczba_stron": 1, "w_domenie": bool(contact.w_domenie),
                "rola": contact.rola or None, "pierwszy_at": now})

    @staticmethod
    def _bump_counters(conn, firma_id: int, rec: PageRecord, now: datetime) -> None:
        """Liczniki firmy rosną tylko dla stron, które kosztowały żądanie sieciowe (`rec.counts`)."""
        depth = int(rec.depth)
        counters: dict[str, Any] = {"updated_at": now}
        if rec.counts:
            counters["stron_pobranych"] = firmy.c.stron_pobranych + 1
            counters["bajty_pobranych"] = firmy.c.bajty_pobranych + int(rec.bytes or 0)
            counters["glebokosc_max"] = sa.case((firmy.c.glebokosc_max.is_(None), depth),
                                                (firmy.c.glebokosc_max < depth, depth), else_=firmy.c.glebokosc_max)
        conn.execute(sa.update(firmy).where(firmy.c.id == firma_id).values(**counters))
