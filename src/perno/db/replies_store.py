"""Magazyn modułu odpowiedzi: kursor pobierania, blokada przebiegu, wiadomości i wątki.

Na tym samym silniku co `Database` i z tą samą obsługą transakcji co magazyny automatu i skrapera (`StoreCore`), ale z własnymi
tabelami (`replies_schema`). Daty to naiwne czasy lokalne z `db.now()` (konwencja projektu). Nie zna reguł dopasowania ani
statusów maili: dostaje gotowe `NewMessage` i zapisuje je razem z aktualizacją wątku w jednej transakcji.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc

from perno.db.company_sql import column_for, company_key, select_mails
from perno.db.replies_schema import (
    RUN_ROW_ID,
    odp_przebieg,
    odp_stan,
    odp_watki,
    odp_wiadomosci,
    replies_metadata,
)
from perno.db.schema import maile
from perno.domain import message_ids
from perno.domain import replies as rp
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
        conn.execute(sa.update(odp_watki).where(odp_watki.c.id == msg.thread_id).values(**values))

    # ---- liczniki (do `ai.py odpowiedzi stan` i panelu) -------------------------------------------------------

    def counts(self) -> dict:
        """Liczby do podsumowania: wątki z odpowiedzią człowieka, czekające na nas, nieocenione, zwroty, autoodpowiedzi, nieprzypisane."""
        human = odp_watki.c.human_in_count > 0
        with self._read() as conn:
            def count(stmt) -> int:
                return int(conn.execute(stmt).scalar() or 0)

            threads = sa.select(sa.func.count()).select_from(odp_watki)
            messages = sa.select(sa.func.count()).select_from(odp_wiadomosci)
            return {
                "threads": count(threads.where(human)),
                "awaiting_reply": count(threads.where(human, odp_watki.c.awaiting_reply.is_(True))),
                "unrated": count(threads.where(human, odp_watki.c.rating.is_(None))),
                "unread": count(threads.where(human, odp_watki.c.read_at.is_(None))),
                "bounces": count(messages.where(odp_wiadomosci.c.kind == rp.KIND_BOUNCE)),
                "auto": count(messages.where(odp_wiadomosci.c.kind == rp.KIND_AUTO)),
                "unassigned": count(messages.where(odp_wiadomosci.c.thread_id.is_(None))),
                "messages": count(messages),
            }
