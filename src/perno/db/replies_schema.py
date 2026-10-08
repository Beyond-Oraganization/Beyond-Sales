"""Tabele modułu odpowiedzi (SQLAlchemy Core).

Osobny `MetaData`, tak jak u automatu i skrapera: tabela `maile` i jej migracje są nietknięte (poprzednia wersja programu
działa na uaktualnionej bazie), a tabele zakłada leniwie magazyn (`replies_store`). Celowo bez kluczy obcych (uprawnienia
z README nie obejmują REFERENCES).

* `odp_watki` — wątek = jedna rozmowa z jednego maila kampanii (id = `maile.id` tego maila); ocena, stan odczytu i liczniki.
* `odp_wiadomosci` — każda zapisana wiadomość: przychodząca, nasza (napisana w kliencie poczty albo w panelu) i szkice.
* `odp_oceny` — dziennik ocen wątku (kto, kiedy, jaka): audyt i cofanie.
* `odp_stan` — kursor pobierania na (skrzynka, folder).
* `odp_przebieg` — jeden wiersz (id = 1): blokada przebiegu pobierania i podsumowanie ostatniego.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

replies_metadata = sa.MetaData()

_OPTS: dict[str, Any] = dict(mysql_engine="InnoDB", mysql_default_charset="utf8mb4", mysql_collate="utf8mb4_0900_ai_ci")
_BIGPK = sa.BigInteger().with_variant(sa.Integer, "sqlite")  # sqlite: autoincrement wymaga INTEGER
_BIGINT = sa.BigInteger().with_variant(sa.Integer, "sqlite")
_LongText = sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql")
_ASCII_BIN = sa.String(40).with_variant(mysql.VARCHAR(40, charset="ascii", collation="ascii_bin"), "mysql")  # skrót: wielkość liter ma znaczenie

RUN_ROW_ID = 1

odp_watki = sa.Table(
    "odp_watki", replies_metadata,
    sa.Column("id", sa.String(32), primary_key=True),  # = maile.id maila kampanii, na który odpowiedziano
    sa.Column("company_id", sa.String(32)),
    sa.Column("mailbox", sa.String(320)),  # skrzynka rozmowy (maile.sent_from; mail sprzed wielu skrzynek: główna)
    sa.Column("rating", sa.String(16)),  # positive | neutral | negative; NULL = nieoceniona
    sa.Column("rating_by", sa.String(8)),  # human | ai
    sa.Column("rating_note", sa.String(500)),
    sa.Column("rated_at", sa.DateTime),
    sa.Column("optout", sa.Boolean, nullable=False, server_default=sa.false()),  # prośba o zaprzestanie kontaktu
    sa.Column("read_at", sa.DateTime),
    sa.Column("closed_at", sa.DateTime),
    sa.Column("awaiting_reply", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("weak", sa.Boolean, nullable=False, server_default=sa.false()),  # przypisanie po samej domenie: do sprawdzenia
    sa.Column("other_mailbox", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("human_in_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("last_in_at", sa.DateTime),
    sa.Column("last_out_at", sa.DateTime),
    sa.Column("last_activity_at", sa.DateTime),
    sa.Column("created_at", sa.DateTime),
    sa.Index("ix_odp_watki_activity", "last_activity_at"),
    sa.Index("ix_odp_watki_mailbox", "mailbox"),
    sa.Index("ix_odp_watki_company", "company_id"),
    **_OPTS,
)

odp_wiadomosci = sa.Table(
    "odp_wiadomosci", replies_metadata,
    sa.Column("id", _BIGPK, primary_key=True, autoincrement=True),
    sa.Column("thread_id", sa.String(32)),  # NULL = nieprzypisana
    sa.Column("direction", sa.String(3), nullable=False),  # in | out
    sa.Column("kind", sa.String(8), nullable=False),  # human | auto | bounce
    sa.Column("state", sa.String(10), nullable=False),  # received | draft | sending | sent | failed
    sa.Column("author", sa.String(8)),  # human | ai (kto napisał naszą wiadomość)
    sa.Column("mailbox", sa.String(320)),
    sa.Column("folder", sa.String(255)),
    sa.Column("uidvalidity", _BIGINT),
    sa.Column("uid", _BIGINT),
    sa.Column("message_id", sa.Text),  # klucz Message-ID (bez nawiasów, małymi literami); może być pusty
    sa.Column("message_id_hash", _ASCII_BIN, nullable=False),  # skrót tożsamości wiadomości: unikat chroni przed duplikatami
    sa.Column("in_reply_to", sa.Text),
    sa.Column("references_hdr", sa.Text),
    sa.Column("from_addr", sa.String(320)),
    sa.Column("from_name", sa.String(255)),
    sa.Column("to_addr", sa.String(1000)),
    sa.Column("subject", sa.String(500)),
    sa.Column("date_at", sa.DateTime),
    sa.Column("body_text", _LongText),
    sa.Column("snippet", sa.String(255)),
    sa.Column("attachments", sa.Text),  # JSON z opisem załączników (pliku nie pobieramy)
    sa.Column("match_method", sa.String(8)),
    sa.Column("match_weak", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("bounce_type", sa.String(4)),  # hard | soft
    sa.Column("optout_hint", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("error", sa.Text),
    sa.Column("fetched_at", sa.DateTime),
    sa.UniqueConstraint("message_id_hash", name="uq_odp_wiadomosci_hash"),
    sa.Index("ix_odp_wiadomosci_thread", "thread_id", "date_at"),
    sa.Index("ix_odp_wiadomosci_uid", "mailbox", "folder", "uid"),
    **_OPTS,
)

odp_oceny = sa.Table(
    "odp_oceny", replies_metadata,
    sa.Column("id", _BIGPK, primary_key=True, autoincrement=True),
    sa.Column("thread_id", sa.String(32), nullable=False),
    sa.Column("rating", sa.String(16)),  # NULL = ocenę wyczyszczono
    sa.Column("by", sa.String(8), nullable=False),
    sa.Column("note", sa.String(500)),
    sa.Column("at", sa.DateTime, nullable=False),
    sa.Index("ix_odp_oceny_thread", "thread_id", "at"),
    **_OPTS,
)

odp_stan = sa.Table(
    "odp_stan", replies_metadata,
    sa.Column("mailbox", sa.String(320), primary_key=True),
    sa.Column("folder", sa.String(255), primary_key=True),
    sa.Column("uidvalidity", _BIGINT),
    sa.Column("last_uid", _BIGINT, nullable=False, server_default=sa.text("0")),
    sa.Column("last_fetch_at", sa.DateTime),
    sa.Column("last_error", sa.String(500)),
    sa.Column("error_kind", sa.String(16)),
    **_OPTS,
)

odp_przebieg = sa.Table(
    "odp_przebieg", replies_metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("token", sa.String(36)),  # niepusty = przebieg trwa
    sa.Column("running_since", sa.DateTime),
    sa.Column("last_started_at", sa.DateTime),
    sa.Column("last_finished_at", sa.DateTime),
    sa.Column("last_summary", sa.Text),  # JSON z podsumowaniem ostatniego przebiegu
    **_OPTS,
)

TABLE_NAMES = tuple(t.name for t in (odp_watki, odp_wiadomosci, odp_oceny, odp_stan, odp_przebieg))
