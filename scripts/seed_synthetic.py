#!/usr/bin/env python3
"""Syntetyczne dane do testów skali: tabela `maile` (i opcjonalnie firmy skrapera) w bazie TESTOWEJ.

    scripts/scratch_mysql.sh start
    .venv/bin/python scripts/seed_synthetic.py --db-url "$(scripts/scratch_mysql.sh url)" --rows 50000

Baza musi mieć nazwę kończącą się na `_test` (dla sqlite: nazwa pliku bez rozszerzenia, np. `/tmp/perno_demo_test.db`):
skrypt kasuje i odtwarza w niej tabele `maile` i skrapera, więc nigdy nie wskazuj nim bazy produkcyjnej.
Panel na takiej bazie demo: `PERNO_DB_URL=sqlite:////tmp/perno_demo_test.db .venv/bin/python server.py --no-browser`. Dane są losowe, ale powtarzalne (`--seed`) i mają rozmiary zbliżone do prawdziwych:
treść HTML ok. 2,6 KB, tekst ok. 1,6 KB, polskie litery w nazwach (żeby sprawdzać wyszukiwanie bez ogonków), rozkład
statusów i krajów jak w kampanii (większość „Do wysłania", dużo „Wysłano", reszta rzadsza).
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime, timedelta

import sqlalchemy as sa

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from perno.db import Database, maile, make_engine
from perno.domain import statuses as st
from perno.scraper import schema as scraper_schema
from perno.scraper.store import Store

BATCH = 500
SEGMENTS = (("eventy", 0.6), ("driverzy", 0.4))
STATUS_WEIGHTS = (
    (st.STATUS_PENDING, 55), (st.STATUS_SENT, 28), (st.STATUS_ERROR, 3), (st.STATUS_SKIPPED_NO_EMAIL, 5),
    (st.STATUS_REPLIED, 3), (st.STATUS_INTERESTED, 1.5), (st.STATUS_DO_NOT_CONTACT, 1), (st.STATUS_SKIPPED_CUSTOMER, 1),
    (st.STATUS_AWAITING_COPY, 2.5),
)
COUNTRY_WEIGHTS = (("PL", 65), ("DE", 8), ("GB", 7), ("CZ", 4), ("ES", 4), ("FR", 3), ("IT", 3), ("NL", 2), ("", 4))
CITIES = {"PL": ("Łódź", "Gdańsk", "Kraków", "Wrocław", "Poznań", "Szczecin", "Białystok", "Lublin", "Katowice", "Bydgoszcz"),
          "DE": ("Berlin", "München", "Köln", "Hamburg"), "GB": ("London", "Manchester", "Leeds"),
          "CZ": ("Praha", "Brno"), "ES": ("Madrid", "Barcelona"), "FR": ("Paris", "Lyon"), "IT": ("Roma", "Milano"),
          "NL": ("Amsterdam", "Utrecht"), "": ("",)}
WORDS = ("Żuraw", "Śląska", "Łączność", "Event", "Studio", "Gala", "Trans", "Limo", "Zjazd", "Konferencja", "Sąsiad",
         "Ćma", "Księżyc", "Dźwięk", "Światło", "Pałac", "Wesele", "Kongres", "Premium", "Express", "Bus", "Van",
         "Chauffeur", "Ruch", "Szlak", "Zakątek", "Grupa", "Partner", "Serwis", "Agencja")
SENTENCE = ("Dzień dobry, piszę w sprawie współpracy przy obsłudze Państwa wydarzeń i transferów. Przygotowaliśmy "
            "ofertę dopasowaną do firmy, która łączy elastyczne terminy, jasne ceny i sprawdzony zespół. ")


def weighted(rng: random.Random, pairs):
    """Losuje wartość z listy par (wartość, waga)."""
    values, weights = zip(*pairs)
    return rng.choices(values, weights=weights)[0]


def make_row(rng: random.Random, number: int, base: datetime) -> dict:
    """Jeden syntetyczny wiersz w formacie API (jak `tests.fixtures.sample_row`, ale z realnymi rozmiarami)."""
    segment = weighted(rng, SEGMENTS)
    status = weighted(rng, STATUS_WEIGHTS)
    country = weighted(rng, COUNTRY_WEIGHTS)
    name = f"{rng.choice(WORDS)} {rng.choice(WORDS)} {number}"
    slug = f"firma{number}.example.pl"
    generated = base - timedelta(days=rng.randint(0, 30), seconds=rng.randint(0, 86399))
    sent = ""
    if status in st.KEEPS_SENT_AT:
        sent = (generated + timedelta(days=rng.randint(0, 5), seconds=rng.randint(0, 86399))).strftime("%Y-%m-%dT%H:%M:%S")
    html = "".join(f"<p>{SENTENCE}{name}.</p>" for _ in range(rng.randint(6, 9)))
    return {
        "id": f"{segment}-{number:05d}", "segment": segment, "row_number": str(number), "company": name,
        "contact_person": rng.choice(("", "", "Anna Kowalska", "Jan Nowak")),
        "email": "" if status == st.STATUS_SKIPPED_NO_EMAIL else f"kontakt@{slug}", "website": f"https://{slug}/",
        "tag": "synt", "mode": "Panstwo", "subject": f"Współpraca z {name}", "body_html": html,
        "body_text": " ".join(f"{SENTENCE}{name}." for _ in range(rng.randint(3, 5))), "status": status,
        "generated_at": generated.strftime("%Y-%m-%dT%H:%M:%S"), "sent_at": sent, "attachments": "", "inline_images": "",
        "error": "SMTP: odrzucono" if status == st.STATUS_ERROR else "", "retry_count": "0", "country": country,
        "city": rng.choice(CITIES[country]), "address": "", "description": "",
    }


def main() -> int:
    """Zakłada tabele w bazie testowej i wstawia `--rows` wierszy (oraz firmy skrapera z `--scraper-firms`)."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-url", required=True, help="adres bazy testowej; nazwa bazy musi kończyć się na _test")
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--scraper-firms", action="store_true", help="po wstawieniu maili zaimportuj z nich firmy skrapera")
    args = parser.parse_args()

    url = sa.engine.make_url(args.db_url)
    name = url.database or ""
    if url.get_backend_name() == "sqlite":
        name = os.path.splitext(os.path.basename(name))[0]  # plik demo: np. /tmp/perno_demo_test.db
    if not name.endswith("_test"):
        raise SystemExit("Nazwa bazy (albo pliku sqlite) musi kończyć się na _test: skrypt kasuje w niej tabele.")
    database = Database(make_engine(url))
    scraper_schema.skraper_metadata.drop_all(database.engine)
    maile.drop(database.engine, checkfirst=True)
    database.ensure_schema()
    rng, base, started = random.Random(args.seed), datetime(2026, 10, 2, 12, 0, 0), time.monotonic()
    for first in range(1, args.rows + 1, BATCH):
        rows = [make_row(rng, number, base) for number in range(first, min(first + BATCH, args.rows + 1))]
        with database.transaction() as conn:
            database.insert_rows(conn, rows)
        if first % (BATCH * 20) == 1:
            print(f"  ... {first + len(rows) - 1}/{args.rows} ({time.monotonic() - started:.0f} s)", flush=True)
    print(f"Wstawiono {args.rows} wierszy `maile` w {time.monotonic() - started:.0f} s ({database.describe()}).")
    if args.scraper_firms:
        store = Store(database)
        store.ensure_schema()
        print("Firmy skrapera:", store.seed_from_maile())
    return 0


if __name__ == "__main__":
    sys.exit(main())
