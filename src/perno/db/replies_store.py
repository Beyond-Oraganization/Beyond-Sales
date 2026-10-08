"""Magazyn modułu odpowiedzi: kursor pobierania, blokada przebiegu, wiadomości i wątki.

Na tym samym silniku co `Database` i z tą samą obsługą transakcji co magazyny automatu i skrapera (`StoreCore`), ale z własnymi
tabelami (`replies_schema`). Daty to naiwne czasy lokalne z `db.now()` (konwencja projektu). Nie zna reguł dopasowania ani
statusów maili: dostaje gotowe `NewMessage` i zapisuje je razem z aktualizacją wątku w jednej transakcji.
"""

from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc

from perno.db import replies_queries as rq
from perno.db import replies_report as rr
from perno.db.company_sql import column_for, company_key, select_mails
from perno.db.replies_schema import (
    RUN_ROW_ID,
    odp_oceny,
    odp_przebieg,
    odp_stan,
    odp_watki,
    odp_wiadomosci,
    replies_metadata,
)
from perno.db.schema import maile
from perno.domain import message_ids, reply_rules
from perno.domain import replies as rp
from perno.domain import reply_listing as rl
from perno.domain import statuses as st
from perno.scraper.store.core import StoreCore, clean_text

RUN_STALE = timedelta(minutes=10)  # przebieg bez zakończenia dłużej niż tyle uznajemy za martwy (proces padł)


@dataclass(frozen=True)
class Cursor:
    """Gdzie skończyło się pobieranie folderu: `uidvalidity` i ostatni przetworzony UID."""

    uidvalidity: int
    last_uid: int


class ReplyStore(StoreCore):
    """Operacje na tabelach odpowiedzi; jeden obiekt na proces (sqlite serializuje się blokadą rdzenia)."""

    def ensure_schema(self) -> None:
        """Zakłada tabele i jedyny wiersz przebiegu (bez nadpisywania istniejącego)."""
        with self._lock or nullcontext():
            replies_metadata.create_all(self.engine)
        with self._tx() as conn:
            self._insert_noop(conn, odp_przebieg, [{"id": RUN_ROW_ID}])

    # ---- kursor pobierania ------------------------------------------------------------------------

    def get_cursor(self, mailbox: str, folder: str) -> Optional[Cursor]:
        with self._read() as conn:
            row = conn.execute(sa.select(odp_stan.c.uidvalidity, odp_stan.c.last_uid)
                               .where(odp_stan.c.mailbox == mailbox, odp_stan.c.folder == folder)).first()
        return Cursor(int(row[0] or 0), int(row[1] or 0)) if row else None

    def save_cursor(self, mailbox: str, folder: str, cursor: Cursor, now: datetime) -> None:
        """Zapamiętuje postęp folderu i kasuje błąd tej skrzynki (pobranie się udało)."""
        values = {"uidvalidity": cursor.uidvalidity, "last_uid": cursor.last_uid, "last_fetch_at": now,
                  "last_error": None, "error_kind": None}
        with self._tx() as conn:
            self._upsert_state(conn, mailbox, folder, values)

    def record_error(self, mailbox: str, message: str, kind: str, now: datetime) -> None:
        """Zapisuje błąd pobierania skrzynki (kursory folderów zostają, więc po naprawie idziemy dalej od tego miejsca)."""
        with self._tx() as conn:
            self._upsert_state(conn, mailbox, "", {"last_fetch_at": now, "last_error": clean_text(message, 500), "error_kind": kind[:16]})

    @staticmethod
    def _upsert_state(conn, mailbox: str, folder: str, values: dict) -> None:
        found = conn.execute(sa.update(odp_stan).where(odp_stan.c.mailbox == mailbox, odp_stan.c.folder == folder).values(**values))
        if found.rowcount == 0:
            conn.execute(sa.insert(odp_stan).values(mailbox=mailbox, folder=folder, **values))

    def mailbox_states(self) -> list:
        """Stan pobierania każdej skrzynki: kiedy ostatnio, czy z błędem (wiersz folderu „” trzyma błąd całej skrzynki)."""
        with self._read() as conn:
            rows = conn.execute(sa.select(odp_stan).order_by(odp_stan.c.mailbox, odp_stan.c.folder)).mappings().all()
        states: dict = {}
        for row in rows:
            entry = states.setdefault(row["mailbox"], {"mailbox": row["mailbox"], "last_fetch_at": None, "error": "", "kind": "", "folders": {}})
            if row["folder"] == "":
                entry["error"], entry["kind"] = row["last_error"] or "", row["error_kind"] or ""
            else:
                entry["folders"][row["folder"]] = {"uidvalidity": row["uidvalidity"], "last_uid": row["last_uid"]}
            if row["last_fetch_at"] and (entry["last_fetch_at"] is None or row["last_fetch_at"] > entry["last_fetch_at"]):
                entry["last_fetch_at"] = row["last_fetch_at"]
        return list(states.values())

    # ---- blokada przebiegu ---------------------------------------------------------------------------

    def acquire_run(self, token: str, now: datetime) -> bool:
        """Zajmuje blokadę przebiegu pobierania (False: inny trwa). Przebieg niezakończony od `RUN_STALE` uznajemy za martwy."""
        with self._tx() as conn:
            result = conn.execute(
                sa.update(odp_przebieg).where(odp_przebieg.c.id == RUN_ROW_ID,
                                              sa.or_(odp_przebieg.c.token.is_(None), odp_przebieg.c.running_since < now - RUN_STALE))
                .values(token=token, running_since=now, last_started_at=now))
            return result.rowcount == 1

    def finish_run(self, token: str, now: datetime, summary: dict) -> None:
        """Zwalnia blokadę (tylko właściciel) i zapisuje podsumowanie przebiegu."""
        with self._tx() as conn:
            conn.execute(sa.update(odp_przebieg).where(odp_przebieg.c.id == RUN_ROW_ID, odp_przebieg.c.token == token)
                         .values(token=None, running_since=None, last_finished_at=now,
                                 last_summary=json.dumps(summary, ensure_ascii=True, default=str)))

    def last_run(self) -> dict:
        """Stan ostatniego przebiegu: czy trwa, kiedy się zaczął i skończył, podsumowanie."""
        with self._read() as conn:
            row = conn.execute(sa.select(odp_przebieg).where(odp_przebieg.c.id == RUN_ROW_ID)).mappings().first()
        if row is None:
            return {"running": False, "started_at": None, "finished_at": None, "summary": {}}
        try:
            summary = json.loads(row["last_summary"]) if row["last_summary"] else {}
        except ValueError:
            summary = {}
        return {"running": row["token"] is not None, "started_at": row["last_started_at"],
                "finished_at": row["last_finished_at"], "summary": summary}

    # ---- wysłane maile (do indeksu dopasowania) ----------------------------------------------------------------

    def sent_mails(self, since: Optional[datetime] = None) -> list:
        """Wysłane maile kampanii (wiersze do `sent_mail_from_row`): tylko te, które faktycznie wyszły, od danej daty."""
        stmt = select_mails(maile.c.id, company_key().label("company_id"), maile.c.email, maile.c.subject, maile.c.sent_at,
                            maile.c.sent_from, maile.c.message_id, column_for("website").label("website")
                            ).where(maile.c.status.in_(st.KEEPS_SENT_AT), maile.c.sent_at.is_not(None))
        if since is not None:
            stmt = stmt.where(maile.c.sent_at >= since)
        with self._read() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    def first_sent_at(self) -> Optional[datetime]:
        """Kiedy wyszedł pierwszy mail kampanii: od tego dnia szukamy odpowiedzi."""
        with self._read() as conn:
            return conn.execute(sa.select(sa.func.min(maile.c.sent_at)).where(maile.c.status.in_(st.KEEPS_SENT_AT))).scalar()

    # ---- wiadomości i wątki --------------------------------------------------------------------------------

    def known_threads(self, keys: Iterable[str]) -> dict:
        """Klucz `Message-ID` → id wątku dla zapisanych już wiadomości (przychodzących i naszych) o podanych kluczach."""
        by_hash = {message_ids.digest(key): key for key in keys if key}
        if not by_hash:
            return {}
        found: dict = {}
        hashes = list(by_hash)
        with self._read() as conn:
            for start in range(0, len(hashes), 500):
                rows = conn.execute(sa.select(odp_wiadomosci.c.message_id_hash, odp_wiadomosci.c.thread_id).where(
                    odp_wiadomosci.c.message_id_hash.in_(hashes[start:start + 500]), odp_wiadomosci.c.thread_id.is_not(None)))
                found.update({by_hash[digest]: thread for digest, thread in rows})
        return found

    def has_identity(self, identity: str) -> bool:
        """Czy wiadomość o tej tożsamości (`message_ids.identity`) jest już zapisana: do liczenia duplikatów bez zapisu."""
        with self._read() as conn:
            return conn.execute(sa.select(odp_wiadomosci.c.id).where(
                odp_wiadomosci.c.message_id_hash == message_ids.digest(identity))).first() is not None

    def all_known_threads(self) -> dict:
        """Klucz `Message-ID` → id wątku dla wszystkich zapisanych wiadomości z identyfikatorem (migawka na czas przebiegu:
        odpowiedź na naszą odpowiedź trafia dzięki niej do tego samego wątku, bez zapytań z wątków roboczych)."""
        with self._read() as conn:
            rows = conn.execute(sa.select(odp_wiadomosci.c.message_id, odp_wiadomosci.c.thread_id).where(
                odp_wiadomosci.c.message_id.is_not(None), odp_wiadomosci.c.thread_id.is_not(None)))
            return {key: thread for key, thread in rows}

    def save_message(self, msg: rp.NewMessage, now: datetime) -> Optional[int]:
        """Zapisuje wiadomość i aktualizuje jej wątek w jednej transakcji. None = to duplikat (już ją mamy)."""
        digest = message_ids.digest(msg.identity)
        try:
            with self._tx() as conn:
                if conn.execute(sa.select(odp_wiadomosci.c.id).where(odp_wiadomosci.c.message_id_hash == digest)).first():
                    return None
                result = conn.execute(sa.insert(odp_wiadomosci).values(**self._message_values(msg, digest, now)))
                if msg.thread_id and msg.state != rp.STATE_DRAFT:
                    self._touch_thread(conn, msg, now)
                return int(result.inserted_primary_key[0])
        except sa_exc.IntegrityError:  # drugi zapis tej samej wiadomości w tej samej chwili: pierwszy wygrał
            return None

    @staticmethod
    def _message_values(msg: rp.NewMessage, digest: str, now: datetime) -> dict:
        return {
            "thread_id": msg.thread_id, "direction": msg.direction, "kind": msg.kind, "state": msg.state, "author": msg.author,
            "mailbox": msg.mailbox, "folder": clean_text(msg.folder, 255), "uidvalidity": msg.uidvalidity or None, "uid": msg.uid or None,
            "message_id": clean_text(msg.message_id) or None, "message_id_hash": digest, "in_reply_to": clean_text(msg.in_reply_to) or None,
            "references_hdr": clean_text(msg.references) or None, "from_addr": clean_text(msg.from_addr, 320),
            "from_name": clean_text(msg.from_name, 255), "to_addr": clean_text(msg.to_addr, 1000), "subject": clean_text(msg.subject, 500),
            "date_at": msg.date_at, "body_text": clean_text(msg.body_text), "snippet": clean_text(msg.snippet, 255),
            "attachments": msg.attachments or None, "match_method": msg.match_method or None, "match_weak": msg.match_weak,
            "bounce_type": msg.bounce_type or None, "optout_hint": msg.optout_hint, "fetched_at": now,
        }

    @staticmethod
    def _touch_thread(conn, msg: rp.NewMessage, now: datetime) -> None:
        """Zakłada wątek przy pierwszej wiadomości i przelicza jego liczniki, stan odpowiedzi i odczytu."""
        row = conn.execute(sa.select(odp_watki).where(odp_watki.c.id == msg.thread_id).with_for_update()).mappings().first()
        when = msg.date_at or now
        if row is None:
            conn.execute(sa.insert(odp_watki).values(
                id=msg.thread_id, company_id=msg.company_id, mailbox=msg.thread_mailbox or msg.mailbox, created_at=now,
                weak=msg.match_weak, other_mailbox=msg.other_mailbox, last_activity_at=when))
            row = conn.execute(sa.select(odp_watki).where(odp_watki.c.id == msg.thread_id)).mappings().one()
        values: dict = {"last_activity_at": max(filter(None, (row["last_activity_at"], when)))}
        if msg.match_method in (rp.MATCH_HEADER, rp.MATCH_ADDRESS, rp.MATCH_MANUAL) and row["weak"]:
            values["weak"] = False  # pewniejsza wiadomość potwierdziła przypisanie
        human = msg.kind == rp.KIND_HUMAN
        if human and msg.direction == rp.DIRECTION_IN:
            values.update(human_in_count=int(row["human_in_count"] or 0) + 1, awaiting_reply=True, read_at=None, closed_at=None,
                          last_in_at=max(filter(None, (row["last_in_at"], when))),
                          optout=bool(row["optout"]) or msg.optout_hint)
        elif human and msg.direction == rp.DIRECTION_OUT:
            values["last_out_at"] = max(filter(None, (row["last_out_at"], when)))
            if row["last_in_at"] is None or when >= row["last_in_at"]:
                values["awaiting_reply"] = False  # odpisaliśmy po ostatniej wiadomości firmy
        if human and (row["last_activity_at"] is None or when >= row["last_activity_at"]):
            values.update(last_snippet=clean_text(msg.snippet, 255), last_direction=msg.direction,
                          last_from=clean_text(msg.from_name or msg.from_addr, 255))
        conn.execute(sa.update(odp_watki).where(odp_watki.c.id == msg.thread_id).values(**values))

    # ---- liczniki (do `ai.py odpowiedzi stan` i panelu) -------------------------------------------------------

    def counts(self) -> dict:
        """Liczby do podsumowania: wątki z odpowiedzią człowieka, czekające na nas, nieocenione, zwroty, autoodpowiedzi, nieprzypisane."""
        human = odp_watki.c.human_in_count > 0
        with self._read() as conn:
            def count(stmt) -> int:
                return int(conn.execute(stmt).scalar() or 0)

            messages = sa.select(sa.func.count()).select_from(odp_wiadomosci)
            return {
                "threads": rq.count_threads(conn, human),
                "awaiting_reply": rq.count_threads(conn, human, odp_watki.c.awaiting_reply.is_(True)),
                "unrated": rq.count_threads(conn, human, odp_watki.c.rating.is_(None)),
                "unread": rq.count_threads(conn, human, odp_watki.c.read_at.is_(None)),
                "bounces": count(messages.where(odp_wiadomosci.c.kind == rp.KIND_BOUNCE)),
                "auto": count(messages.where(odp_wiadomosci.c.kind == rp.KIND_AUTO)),
                "unassigned": count(messages.where(odp_wiadomosci.c.thread_id.is_(None))),
                "messages": count(messages),
            }

    # ---- lista i widok wątku (panel) ---------------------------------------------------------------------

    def list_page(self, flt: rl.ReplyFilter, page: int, size: int) -> tuple:
        """`(wiersze, suma)` strony wybranej zakładki: wątki albo (dla „Nieprzypisanych”) wiadomości bez wątku."""
        with self._read() as conn:
            if flt.tab == rl.TAB_UNASSIGNED:
                return rq.list_unassigned(conn, flt, page, size)
            return rq.list_threads(conn, flt, page, size)

    def tab_counts(self, flt: rl.ReplyFilter) -> dict:
        """Liczby wątków w zakładkach przy zawężeniach z `flt` (zakładka z `flt` jest pomijana)."""
        with self._read() as conn:
            return rq.tab_counts(conn, flt)

    def unread_total(self) -> int:
        """Ile wątków czeka na nas nieprzeczytanych (licznik w menu)."""
        with self._read() as conn:
            return rq.unread_total(conn)

    def thread_detail(self, thread_id: str) -> Optional[dict]:
        """Wszystko do widoku wątku: wiersz listy, mail kampanii, wiadomości i historia ocen (None, gdy wątku nie ma)."""
        with self._read() as conn:
            thread = rq.thread_row(conn, thread_id)
            if thread is None:
                return None
            return {"thread": thread, "origin": rq.origin_mail(conn, thread_id), "messages": rq.thread_messages(conn, thread_id),
                    "ratings": rq.rating_log(conn, thread_id)}

    def origin_info(self, mail_id: str) -> Optional[dict]:
        """Mail kampanii o tym id: firma, status i skrzynka, z której wyszedł (None, gdy go nie ma)."""
        with self._read() as conn:
            row = conn.execute(select_mails(maile.c.id, company_key().label("company_id"), maile.c.status, maile.c.sent_from)
                               .where(maile.c.id == mail_id)).mappings().first()
        return dict(row) if row else None

    def unassigned_message(self, message_id: int) -> Optional[dict]:
        with self._read() as conn:
            return rq.unassigned_message(conn, message_id)

    def search_sent_mails(self, words: tuple, limit: int = 20) -> list:
        """Wysłane maile pasujące do słów (firma, adres, temat): do ręcznego przypisania nieprzypisanej wiadomości."""
        with self._read() as conn:
            return rq.search_sent_mails(conn, words, limit)

    def thread_exists(self, thread_id: str) -> bool:
        with self._read() as conn:
            return conn.execute(sa.select(odp_watki.c.id).where(odp_watki.c.id == thread_id)).first() is not None

    # ---- ocena i stan wątku ----------------------------------------------------------------------------------

    def set_rating(self, thread_id: str, rating: Optional[str], by: str, note: str, now: datetime, *, force: bool = False) -> Optional[dict]:
        """Zapisuje ocenę wątku i dopisuje ją do dziennika. Zwraca poprzednią ocenę (do „Cofnij”), a None, gdy wątku nie ma albo
        AI próbowałoby nadpisać ocenę człowieka (`force`: przywrócenie oceny po „Cofnij”, bez tej reguły)."""
        with self._tx() as conn:
            row = conn.execute(sa.select(odp_watki.c.rating, odp_watki.c.rating_by, odp_watki.c.rating_note)
                               .where(odp_watki.c.id == thread_id).with_for_update()).mappings().first()
            if row is None or (by == rp.BY_AI and not force and not reply_rules.ai_may_rate(row["rating_by"])):
                return None
            conn.execute(sa.update(odp_watki).where(odp_watki.c.id == thread_id).values(
                rating=rating, rating_by=by if rating else None, rating_note=note[:500] or None if rating else None,
                rated_at=now if rating else None))
            conn.execute(sa.insert(odp_oceny).values(thread_id=thread_id, rating=rating, by=by, note=note[:500] or None, at=now))
            return dict(row)

    def update_thread(self, thread_id: str, **values) -> bool:
        """Zmienia pola stanu wątku (`read_at`, `closed_at`, `optout`); zwraca, czy wątek istnieje."""
        allowed = {key: value for key, value in values.items() if key in ("read_at", "closed_at", "optout")}
        with self._tx() as conn:
            return conn.execute(sa.update(odp_watki).where(odp_watki.c.id == thread_id).values(**allowed)).rowcount == 1

    def assign_message(self, message_id: int, thread_id: str, company_id: str, mailbox: str, now: datetime) -> bool:
        """Przypisuje wiadomość bez wątku do wątku (ręcznie): wątek powstaje, jeśli go nie było, i przelicza swoje liczniki.
        Zwraca False, gdy wiadomości nie ma albo ma już wątek."""
        with self._tx() as conn:
            row = conn.execute(sa.select(odp_wiadomosci).where(odp_wiadomosci.c.id == message_id, odp_wiadomosci.c.thread_id.is_(None))
                               .with_for_update()).mappings().first()
            if row is None:
                return False
            conn.execute(sa.update(odp_wiadomosci).where(odp_wiadomosci.c.id == message_id).values(thread_id=thread_id, match_method=rp.MATCH_MANUAL))
            self._touch_thread(conn, rp.NewMessage(
                direction=row["direction"], kind=row["kind"], mailbox=row["mailbox"] or "", identity="", from_addr=row["from_addr"] or "",
                from_name=row["from_name"] or "", snippet=row["snippet"] or "", date_at=row["date_at"], thread_id=thread_id,
                company_id=company_id, match_method=rp.MATCH_MANUAL, optout_hint=bool(row["optout_hint"]), thread_mailbox=mailbox), now)
            return True

    # ---- szkice i wysyłka naszych odpowiedzi -----------------------------------------------------------------

    _EDITABLE = (rp.STATE_DRAFT, rp.STATE_FAILED)

    def get_draft(self, thread_id: str) -> Optional[dict]:
        """Szkic odpowiedzi w wątku (jeden na wątek): treść, autor, adresat, temat, błąd ostatniej próby wysłania."""
        m = odp_wiadomosci.c
        with self._read() as conn:
            row = conn.execute(sa.select(m.id, m.state, m.author, m.mailbox, m.to_addr, m.subject, m.body_text, m.error, m.fetched_at)
                               .where(m.thread_id == thread_id, m.direction == rp.DIRECTION_OUT, m.state.in_(self._EDITABLE + (rp.STATE_SENDING,)))
                               .order_by(m.id.desc())).mappings().first()
        return dict(row) if row else None

    def save_draft(self, thread_id: str, *, body: str, subject: str, to_addr: str, mailbox: str, author: str, now: datetime,
                   keep_human: bool = False) -> Optional[int]:
        """Zapisuje szkic wątku (nowy albo zastępuje poprzedni). `keep_human`: szkic napisany przez człowieka nie jest nadpisywany
        (tak zapisuje AI): wtedy None. Szkic w trakcie wysyłania nie jest zmieniany (też None)."""
        m = odp_wiadomosci.c
        with self._tx() as conn:
            existing = conn.execute(sa.select(m.id, m.state, m.author).where(
                m.thread_id == thread_id, m.direction == rp.DIRECTION_OUT, m.state.in_(self._EDITABLE + (rp.STATE_SENDING,)))
                .order_by(m.id.desc()).with_for_update()).mappings().first()
            if existing is not None and (existing["state"] == rp.STATE_SENDING or (keep_human and existing["author"] == rp.BY_HUMAN)):
                return None
            values = {"body_text": clean_text(body), "subject": clean_text(subject, 500), "to_addr": clean_text(to_addr, 1000),
                      "mailbox": mailbox, "author": author, "state": rp.STATE_DRAFT, "error": None, "fetched_at": now,
                      "snippet": clean_text(" ".join(body.split()), 255)}
            if existing is not None:
                conn.execute(sa.update(odp_wiadomosci).where(m.id == existing["id"]).values(**values))
                return int(existing["id"])
            identity = f"szkic:{uuid.uuid4().hex}"
            result = conn.execute(sa.insert(odp_wiadomosci).values(
                thread_id=thread_id, direction=rp.DIRECTION_OUT, kind=rp.KIND_HUMAN, message_id_hash=message_ids.digest(identity),
                date_at=now, **values))
            return int(result.inserted_primary_key[0])

    def discard_draft(self, thread_id: str) -> bool:
        """Usuwa szkic wątku (nie ten w trakcie wysyłania); zwraca, czy coś usunięto."""
        m = odp_wiadomosci.c
        with self._tx() as conn:
            return conn.execute(sa.delete(odp_wiadomosci).where(
                m.thread_id == thread_id, m.direction == rp.DIRECTION_OUT, m.state.in_(self._EDITABLE))).rowcount > 0

    def claim_draft(self, draft_id: int) -> bool:
        """Przejmuje szkic do wysyłki (szkic → wysyłanie); False, gdy ktoś był pierwszy albo szkic już poszedł."""
        m = odp_wiadomosci.c
        with self._tx() as conn:
            return conn.execute(sa.update(odp_wiadomosci).where(m.id == draft_id, m.state.in_(self._EDITABLE))
                                .values(state=rp.STATE_SENDING, error=None)).rowcount == 1

    def release_draft(self, draft_id: int, error: str) -> None:
        """Wysyłka się nie udała: szkic wraca do edycji z komunikatem o błędzie (nic nie wyszło)."""
        m = odp_wiadomosci.c
        with self._tx() as conn:
            conn.execute(sa.update(odp_wiadomosci).where(m.id == draft_id, m.state == rp.STATE_SENDING)
                         .values(state=rp.STATE_FAILED, error=clean_text(error, 1000)))

    def finish_sent(self, draft_id: int, msg: rp.NewMessage, now: datetime) -> bool:
        """Szkic poszedł: zapisuje wysłaną wiadomość (identyfikator, treść, czas) i przelicza wątek (odpisaliśmy)."""
        m = odp_wiadomosci.c
        with self._tx() as conn:
            changed = conn.execute(sa.update(odp_wiadomosci).where(m.id == draft_id, m.state == rp.STATE_SENDING).values(
                state=rp.STATE_SENT, message_id=msg.message_id, message_id_hash=message_ids.digest(msg.identity), date_at=msg.date_at,
                body_text=clean_text(msg.body_text), subject=clean_text(msg.subject, 500), to_addr=clean_text(msg.to_addr, 1000),
                in_reply_to=clean_text(msg.in_reply_to) or None, references_hdr=clean_text(msg.references) or None,
                snippet=clean_text(msg.snippet, 255), mailbox=msg.mailbox, error=None, fetched_at=now)).rowcount == 1
            if changed and msg.thread_id:
                self._touch_thread(conn, msg, now)
            return changed

    # ---- raport -----------------------------------------------------------------------------------------------

    def report_data(self, flt: rl.ReportFilter, primary: str, groups: Iterable[str]) -> dict:
        """Surowe liczby raportu: całość, zestawienia po wymiarach, pary do czasu reakcji i użycie skrzynek."""
        with self._read() as conn:
            return {"total": rr.totals(conn, flt, primary), "groups": {group: rr.grouped(conn, flt, group, primary) for group in groups},
                    "latency": rr.latency_pairs(conn, flt, primary), "usage": rr.mailbox_usage(conn, flt, primary), "first_day": rr.first_sent_day(conn)}

    # ---- dla Claude Code (ai.py odpowiedzi) -------------------------------------------------------------------

    def thread_ids(self, after: str = "", rating: Optional[str] = None) -> list:
        """Id wątków z odpowiedzią człowieka, rosnąco, od id większego niż `after` (kolejne paczki skrótu). `rating=None` to
        wątki bez oceny, inaczej wątki z tą oceną."""
        with self._read() as conn:
            return rq.thread_ids(conn, after, rating)

    def thread_state(self, thread_id: str) -> Optional[dict]:
        """Stan wątku do walidacji poleceń AI: czy ma odpowiedź człowieka, kto go ocenił i czy jest prośba o brak kontaktu."""
        with self._read() as conn:
            row = conn.execute(sa.select(odp_watki.c.human_in_count, odp_watki.c.rating, odp_watki.c.rating_by, odp_watki.c.optout)
                               .where(odp_watki.c.id == thread_id)).mappings().first()
        return dict(row) if row else None
