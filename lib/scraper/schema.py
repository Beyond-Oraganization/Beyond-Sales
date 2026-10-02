"""Tabele skrapera (SQLAlchemy Core).

Osobny `MetaData`: `Database.ensure_schema()` i migracja tabeli `maile` są
nietknięte. Celowo bez kluczy obcych (uprawnienia z README nie obejmują
REFERENCES) — powiązania przez `firma_id`, kasowanie jawne w Store.
URL-e i hashe w kolumnach ASCII (bin), ciężka treść w osobnej tabeli.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

skraper_metadata = sa.MetaData()


def _ascii(length: int):
    return sa.String(length).with_variant(mysql.VARCHAR(length, charset="ascii", collation="ascii_bin"), "mysql")


_HASH = sa.String(40).with_variant(mysql.CHAR(40, charset="ascii", collation="ascii_bin"), "mysql")
_TEXT_L = sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql")
_BLOB_L = sa.LargeBinary().with_variant(mysql.MEDIUMBLOB(), "mysql")
_BIGPK = sa.BigInteger().with_variant(sa.Integer, "sqlite")  # sqlite: autoincrement wymaga INTEGER
_OPTS = dict(mysql_engine="InnoDB", mysql_default_charset="utf8mb4", mysql_collate="utf8mb4_0900_ai_ci")

firmy = sa.Table(
    "firmy", skraper_metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("domena", _ascii(253), nullable=False),
    sa.Column("nazwa", sa.String(255)),
    sa.Column("website", _ascii(1000), nullable=False),
    sa.Column("origin", _ascii(300)),
    sa.Column("domena_docelowa", _ascii(253)),
    sa.Column("segment", sa.String(32)),
    sa.Column("zrodlo", sa.String(16), nullable=False, server_default="maile"),
    sa.Column("skan_status", sa.String(24), nullable=False, server_default="nieskanowana"),
    sa.Column("skan_przyczyna", sa.String(40)),
    sa.Column("ostatni_blad", sa.String(500)),
    sa.Column("przebieg_id", sa.Integer),
    sa.Column("robots_status", sa.String(16)),
    sa.Column("robots_txt", _TEXT_L),
    sa.Column("robots_at", sa.DateTime),
    sa.Column("crawl_delay", sa.Float),
    sa.Column("stron_pobranych", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("bajty_pobranych", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("glebokosc_max", sa.SmallInteger),
    sa.Column("skan_start_at", sa.DateTime),
    sa.Column("skan_end_at", sa.DateTime),
    sa.Column("nastepna_proba_po", sa.DateTime),
    sa.Column("updated_at", sa.DateTime),
    sa.UniqueConstraint("domena", name="uq_firmy_domena"),
    sa.Index("ix_firmy_status", "skan_status", "id"),
    **_OPTS,
)

firmy_maile = sa.Table(
    "firmy_maile", skraper_metadata,
    sa.Column("maile_id", sa.String(32), primary_key=True),
    sa.Column("firma_id", sa.Integer, nullable=False),
    sa.Index("ix_firmy_maile_firma", "firma_id"),
    **_OPTS,
)

strony = sa.Table(
    "strony", skraper_metadata,
    sa.Column("id", _BIGPK, primary_key=True, autoincrement=True),
    sa.Column("firma_id", sa.Integer, nullable=False),
    sa.Column("url", _ascii(2000), nullable=False),
    sa.Column("url_hash", _HASH, nullable=False),
    sa.Column("glebokosc", sa.SmallInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("priorytet", sa.Integer, nullable=False, server_default=sa.text("1000")),
    sa.Column("zrodlo", sa.String(8), nullable=False, server_default="link"),
    sa.Column("rodzic_id", sa.BigInteger),
    sa.Column("status", sa.String(20), nullable=False, server_default="oczekuje"),
    sa.Column("http_status", sa.SmallInteger),
    sa.Column("content_type", sa.String(100)),
    sa.Column("kodowanie", sa.String(24)),
    sa.Column("bajty", sa.Integer),
    sa.Column("czas_ms", sa.Integer),
    sa.Column("tytul", sa.String(500)),
    sa.Column("opis", sa.String(1000)),
    sa.Column("content_hash", _HASH),
    sa.Column("blad", sa.String(500)),
    sa.Column("dodano_at", sa.DateTime),
    sa.Column("pobrano_at", sa.DateTime),
    sa.UniqueConstraint("firma_id", "url_hash", name="uq_strony_firma_url"),
    sa.Index("ix_strony_kolejka", "firma_id", "status", "priorytet", "id"),
    sa.Index("ix_strony_hash", "firma_id", "content_hash"),
    **_OPTS,
)

strony_tresc = sa.Table(
    "strony_tresc", skraper_metadata,
    sa.Column("strona_id", _BIGPK, primary_key=True, autoincrement=False),
    sa.Column("tekst", _TEXT_L),
    sa.Column("html_gz", _BLOB_L),
    **_OPTS,
)

kontakty = sa.Table(
    "kontakty", skraper_metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("firma_id", sa.Integer, nullable=False),
    sa.Column("typ", sa.String(16), nullable=False),
    sa.Column("wartosc", sa.String(255), nullable=False),
    sa.Column("zrodlo_url", _ascii(2000)),
    sa.Column("zrodlo_typ", sa.String(12)),
    sa.Column("liczba_stron", sa.Integer, nullable=False, server_default=sa.text("1")),
    sa.Column("w_domenie", sa.Boolean, nullable=False, server_default=sa.text("0")),
    sa.Column("rola", sa.String(12)),
    sa.Column("pierwszy_at", sa.DateTime),
    sa.UniqueConstraint("firma_id", "typ", "wartosc", name="uq_kontakty"),
    sa.Index("ix_kontakty_firma", "firma_id", "typ"),
    **_OPTS,
)

skan_przebiegi = sa.Table(
    "skan_przebiegi", skraper_metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("status", sa.String(16), nullable=False, server_default="uruchamianie"),
    sa.Column("aktywny", sa.SmallInteger),
    sa.Column("stop_requested", sa.Boolean, nullable=False, server_default=sa.text("0")),
    sa.Column("start_at", sa.DateTime),
    sa.Column("end_at", sa.DateTime),
    sa.Column("heartbeat_at", sa.DateTime),
    sa.Column("pid", sa.Integer),
    sa.Column("parametry", sa.Text),
    sa.Column("statystyki", sa.Text),
    sa.Column("log_path", sa.String(255)),
    sa.UniqueConstraint("aktywny", name="uq_przebiegi_aktywny"),
    **_OPTS,
)

TABLE_NAMES = tuple(t.name for t in (firmy, firmy_maile, strony, strony_tresc, kontakty, skan_przebiegi))
