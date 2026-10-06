"""Adresy jednej firmy: lista, dodawanie i usuwanie. Każda zmiana odbywa się w jednej transakcji pod blokadą firmy
(`company_writes.lock_company`), więc dwa równoległe zapisy tej samej firmy — dodanie adresu, edycja, wysyłka — ustawiają
się w kolejkę, a numery adresów i unikalność adresu w firmie są zawsze spójne."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import Connection, Engine

from perno.db import company_writes as cw
from perno.db.company_sql import company_key, select_mails
from perno.db.errors import InvalidValue, RowNotFound
from perno.db.listing_queries import light_columns, to_light_row
from perno.db.schema import COMPANY_COLUMNS, MAIL_COLUMNS, companies, maile
from perno.domain import company_rules
from perno.domain import statuses as st

TEXT_FIELDS = ("mode", "subject", "body_html", "body_text", "attachments", "inline_images")  # treść kopiowana między adresami
DUPLICATE_MESSAGE = "Ten adres jest już przypisany do tej firmy."


class CompanyMails:
    """Operacje na adresach firmy; część `MailRepository` (korzysta z jego `engine`, `now` i `get`)."""

    engine: Engine

    def now(self) -> datetime:
        raise NotImplementedError

    def get(self, row_id: str, conn: Optional[Connection] = None) -> Optional[dict]:
        raise NotImplementedError

    def company_mails(self, company_id: str) -> list:
        """Adresy firmy jako wiersze listy (bez treści), adres główny pierwszy."""
        no_position_last = sa.case((maile.c.position.is_(None), 1), else_=0)
        stmt = (select_mails(*light_columns()).where(company_key() == company_id)
                .order_by(no_position_last, maile.c.position, maile.c.id))
        with self.engine.connect() as conn:
            return [to_light_row(row) for row in conn.execute(stmt).mappings()]

    def company_of(self, mail_id: str) -> Optional[str]:
        with self.engine.connect() as conn:
            return cw.company_ids_of(conn, [mail_id]).get(mail_id)

    def company_ids(self, mail_ids) -> list:
        """Id firm podanych adresów (bez powtórzeń, rosnąco); id, których nie ma w bazie, są pomijane."""
        with self.engine.connect() as conn:
            return sorted(set(cw.company_ids_of(conn, mail_ids).values()))

    def companies_with_address(self, email: str, except_company: str, limit: int = 5) -> list:
        """Nazwy innych firm, które mają już ten adres (ostrzeżenie przy dodawaniu; bez wielkości liter i spacji)."""
        key = company_rules.email_key(email)
        stmt = (select_mails(companies.c.company).where(
            sa.func.lower(sa.func.trim(maile.c.email)) == key, company_key() != except_company).distinct().limit(limit))
        with self.engine.connect() as conn:
            return [row[0] or "(bez nazwy)" for row in conn.execute(stmt)]

    def add_mail(self, company_id: str, email: str, *, copy_text: bool = True) -> dict:
        """Dodaje adres do firmy i zwraca jego wiersz. Z `copy_text` bierze temat i treść z pierwszego adresu firmy, który
        je ma (status „Do wysłania”); bez tego albo gdy nie ma z czego, adres czeka na treść. Pierwszy adres firmy bez
        adresu (wiersz „Brak e-maila”) dostaje się do tego wiersza, zamiast dopisywać drugi."""
        address = (email or "").strip()
        problem = company_rules.email_problem(address)
        if problem:
            raise InvalidValue(problem)
        try:
            with self.engine.begin() as conn:
                mail_id = self._put_address(conn, company_id, address, copy_text, self.now())
        except sa_exc.IntegrityError as exc:  # druga linia obrony: indeks unikalny (company_id, email)
            raise InvalidValue(DUPLICATE_MESSAGE) from exc
        return self.get(mail_id) or {}

    def _company_rows(self, conn: Connection, company_id: str) -> list:
        columns = [maile.c[name] for name in MAIL_COLUMNS] + [maile.c.position]
        stmt = sa.select(*columns).where(company_key() == company_id).order_by(maile.c.position, maile.c.id)
        return [dict(row) for row in conn.execute(stmt).mappings()]

    def _put_address(self, conn: Connection, company_id: str, address: str, copy_text: bool, now: datetime) -> str:
        cw.lock_company(conn, company_id)
        company = conn.execute(sa.select(*[companies.c[n] for n in COMPANY_COLUMNS]).where(
            companies.c.id == company_id)).mappings().first()
        if company is None:
            raise RowNotFound(f"Nie znaleziono firmy {company_id}.")
        rows = self._company_rows(conn, company_id)
        key = company_rules.email_key(address)
        if any(company_rules.email_key(row["email"]) == key for row in rows):
            raise InvalidValue(DUPLICATE_MESSAGE)
        placeholder = next((row for row in rows if not company_rules.email_key(row["email"])
                            and row["status"] == st.STATUS_SKIPPED_NO_EMAIL), None)
        donor = next((row for row in rows if row["body_html"] and row is not placeholder), None) if copy_text else None
        text = {name: donor[name] for name in TEXT_FIELDS} if donor else {}
        values = {**text, "email": address, "status": st.STATUS_PENDING if text else st.STATUS_AWAITING_COPY,
                  "generated_at": now if text else None, "updated_at": now}
        if placeholder is not None:
            conn.execute(sa.update(maile).where(maile.c.id == placeholder["id"]).values(**values))
            return placeholder["id"]
        mail_id = self._new_mail_id(company_id, rows)
        position = max([row["position"] or 0 for row in rows] + [0]) + 1
        row = {**{name: None for name in MAIL_COLUMNS}, "retry_count": 0, **values, "id": mail_id,
               "company_id": company_id, "position": position, **cw.mirror(dict(company))}
        conn.execute(sa.insert(maile).values(**row))
        return mail_id

    @staticmethod
    def _new_mail_id(company_id: str, rows: list) -> str:
        try:
            return company_rules.next_mail_id(company_id, [row["id"] for row in rows])
        except ValueError as exc:
            raise InvalidValue(str(exc)) from exc

    def delete_mail(self, mail_id: str) -> dict:
        """Usuwa dodatkowy adres firmy, który nigdy nie dostał maila (adres główny i adresy z historią wysyłki zostają)
        i numeruje pozostałe od nowa. Zwraca `{deleted, company_id, mails}`."""
        with self.engine.begin() as conn:
            owner = cw.company_ids_of(conn, [mail_id]).get(mail_id)
            if owner is None:
                raise RowNotFound(f"Nie znaleziono wiersza {mail_id}.")
            cw.lock_company(conn, owner)
            rows = self._company_rows(conn, owner)
            target = next((row for row in rows if row["id"] == mail_id), None)
            if target is None:
                raise RowNotFound(f"Nie znaleziono wiersza {mail_id}.")
            problem = company_rules.deletable_problem(target, anchor=mail_id == owner)
            if problem:
                raise InvalidValue(problem)
            sent = [st.STATUS_SENDING, *st.KEEPS_SENT_AT]
            gone = conn.execute(sa.delete(maile).where(
                maile.c.id == mail_id, maile.c.sent_at.is_(None), maile.c.status.not_in(sent)))
            if gone.rowcount != 1:
                raise InvalidValue("Ten adres właśnie się zmienił (wysyłka albo edycja) — odśwież listę i spróbuj ponownie.")
            self._renumber(conn, owner, [row for row in rows if row["id"] != mail_id])
        return {"deleted": mail_id, "company_id": owner, "mails": self.company_mails(owner)}

    @staticmethod
    def _renumber(conn: Connection, company_id: str, rows: list) -> None:
        """Numery adresów 1…n bez dziur, adres główny pierwszy."""
        ordered = sorted(rows, key=lambda row: (row["id"] != company_id, row["position"] or 0, row["id"]))
        for position, row in enumerate(ordered, start=1):
            if row["position"] != position:
                conn.execute(sa.update(maile).where(maile.c.id == row["id"]).values(position=position))
