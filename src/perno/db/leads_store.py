"""Magazyn leadów: zasiew z ocenionych odpowiedzi, lista, edycja pól z pochodzeniem (AI albo człowiek) i cofanie.

Lead to wiersz `odp_leady` o kluczu firmy. Powstaje przy `sync` dla każdej firmy, która ma pozytywny albo neutralny wątek z
odpowiedzią człowieka; późniejsza zmiana oceny nie kasuje wiersza (lead staje się „wycofany”, dane zostają). Edycje leadów
nie zapisują się do `podmioty` (nazwa firmy trafia do szablonów maili). Reguła „AI nie nadpisuje człowieka” jest sprawdzana
w transakcji pod blokadą wiersza, tak jak przy ocenie wątku (`ReplyStore.set_rating`).
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

import sqlalchemy as sa

from perno.db import leads_queries as lq
from perno.db.replies_schema import odp_leady, replies_metadata
from perno.domain import lead_listing as ll
from perno.domain import lead_rules as lr
from perno.domain import replies as rp
from perno.scraper.store.core import StoreCore

_CHUNK = 500
_RANK_RATING = {lq.RANK_POSITIVE: rp.RATING_POSITIVE, lq.RANK_NEUTRAL: rp.RATING_NEUTRAL}


def _chunks(items: list) -> Iterable[list]:
    for start in range(0, len(items), _CHUNK):
        yield items[start:start + _CHUNK]


def _stored(name: str, value: Any) -> Any:
    """Wartość do kolumny: puste pole to NULL (etap nigdy nie jest pusty)."""
    return value if name == lr.STAGE else (value or None)


class LeadStore(StoreCore):
    """Operacje na `odp_leady`; jeden obiekt na proces (sqlite serializuje się blokadą rdzenia)."""

    def ensure_schema(self) -> None:
        """Zakłada brakujące tabele modułu odpowiedzi (w tym `odp_leady`); istniejących nie rusza."""
        with self._lock or nullcontext():
            replies_metadata.create_all(self.engine)

    # ---- zasiew ---------------------------------------------------------------------------------------------------

    def sync(self, now: datetime) -> int:
        """Zakłada leady dla firm z kwalifikującym się wątkiem, które ich jeszcze nie mają. Idempotentne; zwraca, ile założono.
        Nazwa i strona idą z `podmioty`, e-mail z adresu nadawcy ostatniej odpowiedzi, kontakt z nazwy nadawcy (gdy wygląda na osobę)."""
        created = 0
        with self._tx() as conn:
            ids = [row["company_id"] for row in lq.unsynced(conn)]
            for chunk in _chunks(ids):
                names, fallback = lq.company_names(conn, chunk), lq.first_addresses(conn, chunk)
                latest: dict = {}
                for message in lq.incoming_messages(conn, chunk, newest_first=True):
                    latest.setdefault(message["company_id"], message)
                rows = [self._seed(cid, names.get(cid, ("", "")), latest.get(cid), fallback.get(cid, ""), now) for cid in chunk]
                self._insert_noop(conn, odp_leady, rows, noop_column="company_id")
                created += len(rows)
        return created

    @staticmethod
    def _seed(company_id: str, company: tuple, message: Optional[dict], fallback_email: str, now: datetime) -> dict:
        name, website = company
        sender = (message or {}).get("from_addr") or fallback_email
        try:
            email = lr.check_email(lr.clean_text(lr.EMAIL, sender))
        except lr.RuleError:
            email = ""
        person = (message or {}).get("from_name") or ""
        contact = " ".join(person.split()) if lr.looks_like_person(person, name) else ""
        return {"company_id": company_id, "name": name[:lr.LIMITS[lr.NAME]] or None, "website": website[:lr.LIMITS[lr.WEBSITE]] or None,
                "contact": contact[:lr.LIMITS[lr.CONTACT]] or None, "email": email or None, "stage": lr.STAGE_NEW,
                "created_at": now, "updated_at": now}

    # ---- lista i widok --------------------------------------------------------------------------------------------

    def list_page(self, flt: ll.LeadFilter, page: int, size: int) -> tuple:
        """`(wiersze, suma)` strony wybranej karty; wiersze mają pochodzenie pól, braki i wątek reprezentujący leada."""
        with self._read() as conn:
            rows, total = lq.list_leads(conn, flt, page, size)
            threads = lq.threads_of(conn, [row["id"] for row in rows])
        return self._decorate(rows, threads), total

    def tab_counts(self, flt: ll.LeadFilter) -> dict:
        with self._read() as conn:
            return lq.tab_counts(conn, flt)

    def segments(self) -> list:
        with self._read() as conn:
            return lq.segments(conn)

    def get(self, company_id: str) -> Optional[dict]:
        """Lead z wszystkimi wątkami z odpowiedzią człowieka (None, gdy leada nie ma)."""
        with self._read() as conn:
            row = lq.lead_row(conn, company_id)
            threads = lq.threads_of(conn, [company_id]) if row else []
        if row is None:
            return None
        lead = self._decorate([row], threads)[0]
        lead["threads"] = threads
        return lead

    @staticmethod
    def _decorate(rows: list, threads: list) -> list:
        """Dopisuje do wierszy listy: pochodzenie pól, braki, ocenę i wątek reprezentujący (kwalifikujący się najświeższy)."""
        by_company: dict = {}
        for thread in threads:
            by_company.setdefault(thread["company_id"], []).append(thread)
        for row in rows:
            mine = by_company.get(row["id"], [])
            lead_threads = [t for t in mine if t["rating"] in lr.LEAD_RATINGS]
            rep = (lead_threads or mine or [None])[0]
            src = lr.parse_src(row["src"])
            row.update(src=src, pending=lr.pending_fields(src), missing=lr.missing_fields(row), active=bool(row["active"]),
                       optout=bool(row["optout"]), do_not_contact=bool(row["do_not_contact"]),
                       rating=_RANK_RATING.get(row.pop("best_rank"), rep["rating"] if rep else None),
                       thread_id=rep["id"] if rep else None, rating_by=rep["rating_by"] if rep else None,
                       thread_ids=[t["id"] for t in mine], snippet=rep["last_snippet"] if rep else None,
                       last_from=rep["last_from"] if rep else None, awaiting_reply=bool(rep and rep["awaiting_reply"]))
        return rows

    # ---- edycja ---------------------------------------------------------------------------------------------------

    def update_fields(self, company_id: str, changes: Mapping, *, by: str, now: datetime) -> Optional[dict]:
        """Zapisuje pola leada. AI pomija pola z wpisem człowieka (`skipped`). Zwraca `{previous, skipped}`, a `previous`
        ({pole: {value, src}}) wystarcza do `restore_fields`. None, gdy leada nie ma."""
        with self._tx() as conn:
            row = self._locked(conn, company_id)
            if row is None:
                return None
            src = lr.parse_src(row["src"])
            applied = {name: value for name, value in changes.items() if by != lr.BY_AI or lr.ai_may_write(src, name)}
            skipped = [name for name in changes if name not in applied]
            if applied:
                values = {name: _stored(name, value) for name, value in applied.items()}
                conn.execute(sa.update(odp_leady).where(odp_leady.c.company_id == company_id).values(
                    **values, src=lr.dump_src(lr.merge_src(src, applied, by)), updated_at=now, updated_by=by))
            return {"previous": {name: {"value": row[name], "src": src.get(name)} for name in applied}, "skipped": skipped}

    def restore_fields(self, company_id: str, previous: Mapping, now: datetime) -> bool:
        """Przywraca wartości i pochodzenie pól sprzed zapisu (przycisk „Cofnij”); False, gdy leada nie ma."""
        with self._tx() as conn:
            row = self._locked(conn, company_id)
            if row is None:
                return False
            src = lr.parse_src(row["src"])
            values = {}
            for name, old in previous.items():
                values[name] = _stored(name, old.get("value"))
                if old.get("src") in (lr.BY_AI, lr.BY_HUMAN):
                    src[name] = old["src"]
                else:
                    src.pop(name, None)
            conn.execute(sa.update(odp_leady).where(odp_leady.c.company_id == company_id).values(
                **values, src=lr.dump_src(src), updated_at=now, updated_by=lr.BY_HUMAN))
            return True

    def approve(self, company_id: str, fields: Iterable[str], now: datetime) -> Optional[dict]:
        """Człowiek zatwierdza pola wpisane przez AI: pochodzenie zmienia się na `human`, wartość zostaje. Zwraca `{previous, approved}`
        (jak `update_fields`) albo None, gdy leada nie ma."""
        with self._tx() as conn:
            row = self._locked(conn, company_id)
            if row is None:
                return None
            src = lr.parse_src(row["src"])
            approved = [name for name in dict.fromkeys(fields) if src.get(name) == lr.BY_AI]
            if approved:
                conn.execute(sa.update(odp_leady).where(odp_leady.c.company_id == company_id).values(
                    src=lr.dump_src({**src, **{name: lr.BY_HUMAN for name in approved}}), updated_at=now, updated_by=lr.BY_HUMAN))
            return {"previous": {name: {"value": row[name], "src": lr.BY_AI} for name in approved}, "approved": approved}

    @staticmethod
    def _locked(conn, company_id: str) -> Optional[Mapping]:
        return conn.execute(sa.select(odp_leady).where(odp_leady.c.company_id == company_id).with_for_update()).mappings().first()

    # ---- licznik, statystyki i dane dla Claude Code ---------------------------------------------------------------

    def pending_total(self) -> int:
        """Aktywne leady z brakami i firmy jeszcze bez leada (licznik w menu; nic nie zapisuje)."""
        with self._read() as conn:
            return lq.pending_total(conn)

    def stats(self) -> dict:
        """Liczby kart, firmy czekające na zasiew i licznik braków (do `ai.py leady stan`)."""
        with self._read() as conn:
            counts = lq.tab_counts(conn, ll.LeadFilter())
            return {**counts, "unsynced": len(lq.unsynced(conn)), "pending": lq.pending_total(conn)}

    def lead_ids(self, after: str = "", *, only_missing: bool = True) -> list:
        with self._read() as conn:
            return lq.lead_ids(conn, after, only_missing=only_missing)

    def resolve(self, entry_id: str) -> Optional[dict]:
        """Id z pliku AI to id leada (firmy) albo któregoś z jego wątków. Zwraca `{company_id, active}` albo None, gdy to nie lead."""
        with self._read() as conn:
            row = lq.lead_row(conn, entry_id)
            if row is not None:
                return {"company_id": entry_id, "active": bool(row["active"])}
            thread = lq.thread_company(conn, entry_id)
            if thread is None or not thread["qualifies"]:
                return None
            lead = lq.lead_row(conn, thread["company_id"])
        return {"company_id": thread["company_id"], "active": True} if lead is not None else None

    def evidence(self, company_id: str) -> dict:
        """Wiadomości człowieka (od najstarszej) i adresy firmy: z nich AI może brać kontakt i z nich sprawdzamy, że go nie zmyśliło."""
        with self._read() as conn:
            return {"messages": lq.incoming_messages(conn, [company_id]), "addresses": lq.company_addresses(conn, company_id)}
