"""Odczyty dla panelu i CLI: przegląd statusów, lista firm, szczegóły firmy oraz dane do uzupełniania maili."""

from __future__ import annotations

from typing import Optional

import sqlalchemy as sa

from perno.db import companies, maile
from perno.scraper import statuses as st
from perno.scraper.schema import firmy, firmy_maile, kontakty, strony, strony_tresc
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

    # ---- dane do uzupełniania maili (services.enrichment) -----------------------------------
    def enrichment_candidates(self, mail_statuses: tuple, scan_statuses: tuple, min_pages: int = 0) -> list:
        """Wiersze `maile` w podanych statusach, których firma ma skan w jednym z `scan_statuses` i pobrane
        co najmniej `min_pages` stron — razem z danymi firmy ze skanera (`firma_id`, `skan_status`, `stron_pobranych`)."""
        with self._read() as conn:
            rows = conn.execute(
                sa.select(maile.c.id, companies.c.segment, companies.c.company, companies.c.website, companies.c.country,
                          companies.c.city, companies.c.address, companies.c.description, maile.c.email, maile.c.status,
                          firmy.c.id.label("firma_id"), firmy.c.skan_status, firmy.c.stron_pobranych)
                .select_from(maile.join(companies, companies.c.id == maile.c.company_id)
                             .join(firmy_maile, firmy_maile.c.maile_id == maile.c.id)
                             .join(firmy, firmy.c.id == firmy_maile.c.firma_id))
                .where(firmy.c.skan_status.in_(scan_statuses), maile.c.status.in_(mail_statuses),
                       firmy.c.stron_pobranych >= min_pages)
                .order_by(maile.c.id)
            ).mappings().all()
        return [dict(r) for r in rows]

    def scan_counts_by_segment(self) -> dict:
        """`{(segment, status skanu): liczba firm}`. Segment bierzemy z firmy skrapera, a gdy go nie ma (firma dodana
        ręcznie), z powiązanej firmy w tabeli maile; bez żadnego pusty tekst."""
        segment = sa.func.coalesce(firmy.c.segment, sa.func.min(companies.c.segment))
        stmt = (sa.select(segment, firmy.c.skan_status, sa.func.count(sa.distinct(firmy.c.id)))
                .select_from(firmy.outerjoin(firmy_maile, firmy_maile.c.firma_id == firmy.c.id)
                             .outerjoin(maile, maile.c.id == firmy_maile.c.maile_id)
                             .outerjoin(companies, companies.c.id == maile.c.company_id))
                .group_by(firmy.c.id, firmy.c.segment, firmy.c.skan_status))
        counts: dict = {}
        with self._read() as conn:
            for name, status, _n in conn.execute(stmt):
                counts[(name or "", status)] = counts.get((name or "", status), 0) + 1
        return counts

    def brief_pages(self, firma_id: int, limit: int) -> list:
        """`[(url, tytuł, opis, tekst)]` najpłytszych pobranych stron firmy, najwyżej `limit` (do skrótu dla autora treści)."""
        with self._read() as conn:
            return [tuple(row) for row in conn.execute(
                sa.select(strony.c.url, strony.c.tytul, strony.c.opis, strony_tresc.c.tekst)
                .select_from(strony.outerjoin(strony_tresc, strony_tresc.c.strona_id == strony.c.id))
                .where(strony.c.firma_id == firma_id, strony.c.status == st.STRONA_POBRANO)
                .order_by(strony.c.glebokosc, strony.c.id).limit(max(1, limit))
            ).all()]

    def scraped_emails(self, firma_id: int) -> list:
        """E-maile znalezione na stronach firmy: w domenie firmy pierwsze, potem wg liczby stron, na których wystąpiły."""
        with self._read() as conn:
            return [dict(r) for r in conn.execute(
                sa.select(kontakty.c.wartosc, kontakty.c.w_domenie, kontakty.c.rola, kontakty.c.zrodlo_url,
                          kontakty.c.liczba_stron)
                .where(kontakty.c.firma_id == firma_id, kontakty.c.typ == "email")
                .order_by(kontakty.c.w_domenie.desc(), kontakty.c.liczba_stron.desc())
            ).mappings()]

    def contact_values(self, firma_id: int, kind: str, limit: int = 8) -> list:
        """Wartości kontaktów danego typu (np. „telefon”, „formularz”) — najwyżej `limit`."""
        with self._read() as conn:
            return [row[0] for row in conn.execute(sa.select(kontakty.c.wartosc).where(
                kontakty.c.firma_id == firma_id, kontakty.c.typ == kind).limit(limit))]

    def fetched_pages(self, firma_id: int) -> list:
        """[(url, tytuł, opis, tekst)] pobranych stron firmy: najpłytsze pierwsze (tekst bywa pusty)."""
        with self._read() as conn:
            return [tuple(row) for row in conn.execute(
                sa.select(strony.c.url, strony.c.tytul, strony.c.opis, strony_tresc.c.tekst)
                .select_from(strony.outerjoin(strony_tresc, strony_tresc.c.strona_id == strony.c.id))
                .where(strony.c.firma_id == firma_id, strony.c.status == st.STRONA_POBRANO)
                .order_by(strony.c.glebokosc, strony.c.id)
            ).all()]
