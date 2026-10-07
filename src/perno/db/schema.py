"""Definicja tabeli `maile` (SQLAlchemy Core) i nazwy jej kolumn."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# Dawny format Maile.csv (19 pól) — migracja i `--export-legacy` trzymają się go dokładnie.
LEGACY_COLUMNS = (
    "id", "segment", "row_number", "company", "contact_person", "email", "website",
    "tag", "mode", "subject", "body_html", "body_text", "status", "generated_at",
    "sent_at", "attachments", "inline_images", "error", "retry_count",
)
# Dane o firmie: skąd pochodzi (kraj jako kod ISO), gdzie (miasto, adres) i czym się zajmuje.
PROFILE_COLUMNS = ("country", "city", "address", "description")
COLUMNS = LEGACY_COLUMNS + PROFILE_COLUMNS
# Firma ma wiele adresów: pola firmy leżą w `podmioty` (jedno miejsce prawdy), pola adresu, treści i wysyłki w `maile`.
# Stare kolumny firmowe w `maile` zostają jako lustro (zapisywane razem z `podmioty` dla wszystkich adresów firmy), bo
# poprzednia wersja programu — panel albo demon Automatu — nadal je czyta i musi działać na uaktualnionej bazie.
COMPANY_COLUMNS = ("segment", "row_number", "company", "website", "tag", "country", "city", "address", "description")
MAIL_COLUMNS = tuple(name for name in COLUMNS if name not in COMPANY_COLUMNS)
# Łącze adresu z firmą (NULL do czasu `--adopt`): id firmy i numer adresu w firmie (1 = adres główny).
LINK_COLUMNS = ("company_id", "position")
# Pola wyliczane w odczycie i widoczne w API obok `COLUMNS`: firma i numer adresu, ile adresów ma firma oraz status innego
# adresu, który wstrzymuje ten (`domain.statuses.COMPANY_STOPS`; pusty = brak blokady).
EXTRA_COLUMNS = ("company_id", "position", "mail_count", "hold")


# Indeks, po którym szukamy innych adresów firmy. Podzapytania o adresy firmy (liczba adresów, blokada, odstęp) wymuszają go
# podpowiedzią dla MySQL: zaraz po masowym wczytaniu statystyki tabeli bywają nieaktualne i optymalizator wybiera wtedy
# pełny przegląd tabeli dla każdego wiersza (50 tys. firm: minuty zamiast ułamka sekundy).
COMPANY_STATUS_INDEX = "ix_maile_company_status"


def legacy_key(name: str) -> str:
    """Klucz w kodzie dla starej kolumny firmowej w `maile` (nazwa w SQL się nie zmienia). Dzięki temu `maile.c.country`
    rzuca błąd zamiast po cichu czytać lustro: pola firmy czytamy z `podmioty`, a lustro tylko zapisujemy."""
    return "legacy_" + name
# Lista bez treści maili: tego potrzebuje panel do tabeli i filtrów (treść pobiera dopiero po rozwinięciu wiersza).
LIGHT_COLUMNS = tuple(name for name in COLUMNS if name not in ("body_html", "body_text"))

metadata = sa.MetaData()
_LongText = sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql")

maile = sa.Table(
    "maile", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("segment", sa.String(32), nullable=False, key=legacy_key("segment")),
    sa.Column("row_number", sa.Integer, nullable=True, key=legacy_key("row_number")),
    sa.Column("company", sa.String(255), key=legacy_key("company")),
    sa.Column("contact_person", sa.String(255)),
    sa.Column("email", sa.String(320)),
    sa.Column("website", sa.String(1000), key=legacy_key("website")),
    sa.Column("tag", sa.String(255), key=legacy_key("tag")),
    sa.Column("mode", sa.String(16)),
    sa.Column("subject", sa.String(500)),
    sa.Column("body_html", _LongText),
    sa.Column("body_text", _LongText),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("generated_at", sa.DateTime),
    sa.Column("sent_at", sa.DateTime),
    sa.Column("attachments", sa.Text),
    sa.Column("inline_images", sa.String(255)),
    sa.Column("error", sa.Text),
    sa.Column("retry_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("updated_at", sa.DateTime),
    # Dane o firmie — na końcu, żeby kolejność kolumn była taka sama w świeżej i w uaktualnionej bazie.
    sa.Column("country", sa.String(2), key=legacy_key("country")),
    sa.Column("city", sa.String(120), key=legacy_key("city")),
    sa.Column("address", sa.String(500), key=legacy_key("address")),
    sa.Column("description", sa.Text, key=legacy_key("description")),
    # Łącze z firmą — też na końcu, z tego samego powodu. Bez klucza obcego: konto bazy nie ma uprawnienia REFERENCES.
    sa.Column("company_id", sa.String(32)),
    sa.Column("position", sa.Integer),
    sa.Index("ix_maile_status_sent_at", "status", "sent_at"),
    sa.Index("ix_maile_segment_row", legacy_key("segment"), legacy_key("row_number")),
    sa.Index("ix_maile_email", "email"),
    sa.Index(COMPANY_STATUS_INDEX, "company_id", "status"),
    # Ten sam adres dwa razy w jednej firmie jest błędem; NULL (wiersz bez adresu albo jeszcze bez firmy) nie koliduje.
    sa.Index("uq_maile_company_email", "company_id", "email", unique=True),
    mysql_engine="InnoDB", mysql_default_charset="utf8mb4", mysql_collate="utf8mb4_0900_ai_ci",
)

# Firma (podmiot) — nazwa różni się od `firmy` skrapera (tam jedna firma = jedna domena, z własnym numerem). `id` to dawne
# id pierwszego maila firmy, dlatego adres główny ma to samo id co firma.
companies = sa.Table(
    "podmioty", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("segment", sa.String(32), nullable=False),
    sa.Column("row_number", sa.Integer, nullable=True),
    sa.Column("company", sa.String(255)),
    sa.Column("website", sa.String(1000)),
    sa.Column("tag", sa.String(255)),
    sa.Column("country", sa.String(2)),
    sa.Column("city", sa.String(120)),
    sa.Column("address", sa.String(500)),
    sa.Column("description", sa.Text),
    sa.Column("updated_at", sa.DateTime),
    sa.Index("ix_podmioty_segment_row", "segment", "row_number"),
    sa.Index("ix_podmioty_country", "country"),
    mysql_engine="InnoDB", mysql_default_charset="utf8mb4", mysql_collate="utf8mb4_0900_ai_ci",
)
