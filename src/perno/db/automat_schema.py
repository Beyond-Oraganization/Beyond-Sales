"""Tabele automatu wysyłki (SQLAlchemy Core).

Osobny `MetaData`, tak jak u skrapera: tabela `maile` i jej migracje są nietknięte (poprzednia wersja programu działa
na uaktualnionej bazie). Celowo bez kluczy obcych (uprawnienia z README nie obejmują REFERENCES).

* `automat_stan` — jeden wiersz (id = 1): zapamiętane ustawienia i stan planu (uzbrojony, wstrzymany, seria błędów).
* `automat_przebiegi` — procesy demona; unikalny `aktywny` gwarantuje, że działa najwyżej jeden.
* `automat_zdarzenia` — dziennik zdarzeń (źródło logu w panelu i zabezpieczenie przed powtarzaniem powiadomień).
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

automat_metadata = sa.MetaData()

_OPTS: dict[str, Any] = dict(mysql_engine="InnoDB", mysql_default_charset="utf8mb4", mysql_collate="utf8mb4_0900_ai_ci")
_BIGPK = sa.BigInteger().with_variant(sa.Integer, "sqlite")  # sqlite: autoincrement wymaga INTEGER

STATE_ROW_ID = 1

automat_stan = sa.Table(
    "automat_stan", automat_metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("ustawienia", sa.Text),  # JSON z `AutomatSettings.to_json()`; NULL = domyślne
    sa.Column("zmieniono_at", sa.DateTime),
    sa.Column("uzbrojony", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("uzbrojony_at", sa.DateTime),
    sa.Column("uzbrojony_do", sa.DateTime),
    sa.Column("wstrzymany", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("powod_pauzy", sa.String(500)),
    sa.Column("bledy_z_rzedu", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("potwierdzone", sa.Text),  # ustawienia widoczne w oknie potwierdzenia Startu (ślad po tym, co zatwierdzono)
    **_OPTS,
)

automat_przebiegi = sa.Table(
    "automat_przebiegi", automat_metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("status", sa.String(16), nullable=False, server_default="dziala"),
    sa.Column("aktywny", sa.SmallInteger),
    sa.Column("start_at", sa.DateTime),
    sa.Column("heartbeat_at", sa.DateTime),
    sa.Column("end_at", sa.DateTime),
    sa.Column("pid", sa.Integer),
    sa.Column("host", sa.String(255)),
    sa.Column("statystyki", sa.Text),  # JSON: stan pętli dla panelu (następny mail, powód czekania…)
    sa.UniqueConstraint("aktywny", name="uq_automat_aktywny"),
    **_OPTS,
)

automat_zdarzenia = sa.Table(
    "automat_zdarzenia", automat_metadata,
    sa.Column("id", _BIGPK, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.DateTime, nullable=False),
    sa.Column("poziom", sa.String(8), nullable=False),
    sa.Column("typ", sa.String(32), nullable=False),
    sa.Column("klucz", sa.String(64)),  # np. data dnia: jedno zdarzenie danego typu na klucz
    sa.Column("tresc", sa.Text, nullable=False),
    sa.Column("powiadomiono_at", sa.DateTime),
    sa.Column("kanaly", sa.String(64)),
    sa.Index("ix_automat_zdarzenia_typ", "typ", "klucz"),
    sa.Index("ix_automat_zdarzenia_ts", "ts"),
    **_OPTS,
)

TABLE_NAMES = tuple(t.name for t in (automat_stan, automat_przebiegi, automat_zdarzenia))
