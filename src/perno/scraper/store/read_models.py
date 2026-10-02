"""Odczyty dla panelu i CLI: przegląd statusów, lista firm i szczegóły jednej firmy."""

from __future__ import annotations

from typing import Optional

import sqlalchemy as sa

from perno.scraper import statuses as st
from perno.scraper.schema import firmy, kontakty, strony
from perno.scraper.store.core import StoreCore, iso

ROBOTS_VIEW_CHARS = 4096


class ReadModels(StoreCore):
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
            rows = [{k: iso(v) for k, v in dict(r).items()} for r in conn.execute(stmt).mappings()]
            total = conn.execute(sa.select(sa.func.count()).select_from(firmy).where(*where)).scalar() or 0
        return rows, total

    def firma_detail(self, firma_id: int, page: int = 0, per_page: int = 100) -> Optional[dict]:
        per_page = max(1, min(500, per_page))
        with self._read() as conn:
            row = conn.execute(sa.select(firmy).where(firmy.c.id == firma_id)).mappings().first()
            if not row:
                return None
            firm = {k: iso(v) for k, v in dict(row).items()}
            robots = firm.get("robots_txt") or ""
            firm["robots_txt"] = robots[:ROBOTS_VIEW_CHARS]
            firm["robots_obcięty"] = len(robots) > ROBOTS_VIEW_CHARS
            page_cols = [strony.c.id, strony.c.url, strony.c.glebokosc, strony.c.priorytet, strony.c.zrodlo, strony.c.status,
                         strony.c.http_status, strony.c.content_type, strony.c.bajty, strony.c.czas_ms, strony.c.tytul,
                         strony.c.blad, strony.c.pobrano_at]
            pages = [{k: iso(v) for k, v in dict(r).items()} for r in conn.execute(
                sa.select(*page_cols).where(strony.c.firma_id == firma_id).order_by(strony.c.id)
                .offset(max(0, page) * per_page).limit(per_page)).mappings()]
            total = conn.execute(sa.select(sa.func.count()).select_from(strony).where(strony.c.firma_id == firma_id)).scalar() or 0
            people = [{k: iso(v) for k, v in dict(r).items()} for r in conn.execute(
                sa.select(kontakty).where(kontakty.c.firma_id == firma_id)
                .order_by(kontakty.c.typ, kontakty.c.liczba_stron.desc(), kontakty.c.id)).mappings()]
        return {"firma": firm, "strony": pages, "strony_razem": total, "kontakty": people}
