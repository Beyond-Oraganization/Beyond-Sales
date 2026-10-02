#!/usr/bin/env python3
"""Generuje i wysyła maile sprzedażowe (Eventy + Driverzy ICP3) na podstawie bazy MySQL.

Jedna komenda, bezpieczna paczka: uruchomienie bez argumentów dogenerowuje
brakujące wiersze w bazie, pyta o potwierdzenie i wysyła kolejną paczkę
z opóźnieniami; status każdego maila zapisuje się w bazie zaraz po wysyłce.

Zanim wyślesz cokolwiek naprawdę:
  1. python3 -m perno.db --check                    (połączenie z bazą)
  2. python3 build_seed_data.py
  3. python3 send_emails.py --dry-run --segment both --limit 200
  4. uzupełnij hasła w config.env (SMTP_PASSWORD, DB_PASSWORD)
  5. python3 send_emails.py --test-send TWOJ@ADRES --id eventy-003
  6. dopiero potem: python3 send_emails.py --limit 3   (na realnych firmach)
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from collections import Counter
from contextlib import nullcontext
from datetime import datetime

from perno import settings as cfg
from perno.db import Database, DbError
from perno.domain import countries
from perno.domain import statuses as st
from perno.domain.rowfilter import NO_COUNTRY, RowFilter
from perno.services import generation, sending
from perno.settings import ConfigError, load_config, require_smtp


def write_log(lines: list) -> str:
    os.makedirs(cfg.LOG_DIR, exist_ok=True)
    path = os.path.join(cfg.LOG_DIR, f"send_emails_{datetime.now():%Y%m%d_%H%M%S}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def parse_countries(value: str) -> tuple:
    """`PL,GB` / `Polska, Niemcy` / `none` (firmy bez kraju) → krotka kodów ISO (z NO_COUNTRY dla „none")."""
    codes = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        code = NO_COUNTRY if part.lower() in ("none", "brak") else countries.normalize_country(part)
        if code is None:
            raise argparse.ArgumentTypeError(
                f"nieznany kraj {part!r} — podaj kod ISO (PL, GB, DE…), nazwę (Polska, Niemcy…) albo none (firmy bez kraju)")
        codes.append(code)
    if not codes:
        raise argparse.ArgumentTypeError("podaj przynajmniej jeden kraj, np. --country PL,GB")
    return tuple(dict.fromkeys(codes))


def country_summary(rows: list) -> tuple:
    """(opis rozbicia po krajach, liczba maili do firm spoza Polski) — do pytania o potwierdzenie."""
    counts = Counter(row.get("country") or "" for row in rows)
    # Polska pierwsza, firmy bez kraju na końcu, reszta od największej grupy
    order = lambda item: (0 if item[0] == "PL" else 2 if item[0] == "" else 1, -item[1], item[0])
    parts = [f"{countries.country_name(code) or code or 'bez kraju'} {n}" for code, n in sorted(counts.items(), key=order)]
    foreign = sum(n for code, n in counts.items() if code and code != "PL")
    return ", ".join(parts), foreign


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--segment", choices=["eventy", "driverzy", "both"], default="both")
    parser.add_argument("--limit", type=int, default=20, help="Ile nowych maili wysłać w tym przebiegu.")
    parser.add_argument("--daily-cap", type=int, default=50, help="Maks. wysłanych łącznie dzisiaj.")
    parser.add_argument("--delay-min", type=float, default=45, help="Min. opóźnienie między mailami (s).")
    parser.add_argument("--delay-max", type=float, default=120, help="Maks. opóźnienie między mailami (s).")
    parser.add_argument("--dry-run", action="store_true", help="Tylko generuje treści, nic nie wysyła.")
    parser.add_argument("--regenerate-pending", action="store_true",
                        help="Przelicza jeszcze niewysłane maile z aktualnych szablonów/danych.")
    parser.add_argument("--test-send", metavar="EMAIL", help="Wysyła jeden wybrany mail testowo na ten adres.")
    parser.add_argument("--id", help="Ogranicz do jednego wiersza po id (np. eventy-003).")
    parser.add_argument("--country", type=parse_countries, default=(), metavar="KRAJE",
                        help="Ogranicz do firm z tych krajów: kody ISO lub nazwy po przecinku (PL,GB), "
                             "none = firmy bez kraju.")
    parser.add_argument("--company", help="Ogranicz do wierszy, których nazwa zawiera ten tekst.")
    parser.add_argument("--yes", action="store_true", help="Pomiń potwierdzenie przed realną wysyłką.")
    parser.add_argument("--allow-empty-db", action="store_true",
                        help="Zezwól na generowanie do PUSTEJ bazy (tylko świeża instalacja bez historii).")
    parser.add_argument("--config", default=cfg.DEFAULT_CONFIG)
    return parser


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not args.dry_run:
        require_smtp(config, args.config)
    sender_name = config.get("SENDER_NAME", "")

    db = Database.from_config(config)
    if not db.check_connection()["table_exists"]:
        raise DbError("Tabela maile nie istnieje. Uruchom migrate_csv_to_sql.py (lub: python3 -m perno.db --init).")
    db.upgrade_schema()  # idempotentne: dodaje brakujące, opcjonalne kolumny (kraj, miasto, adres, opis)

    recovered = db.recover_stale_claims()
    if recovered:
        print(f"UWAGA: zawieszone wysyłki oznaczono jako błąd (sprawdź folder Wysłane): {', '.join(recovered)}")

    segments = ("eventy", "driverzy") if args.segment == "both" else (args.segment,)
    result = generation.refresh_segments(
        db, segments, sender_name, args.regenerate_pending, allow_empty=args.allow_empty_db)
    for segment, stats in result.items():
        print(f"{segment}: {stats['created']} nowych, {stats['regenerated']} przeliczonych.")

    if args.dry_run:
        if args.country:
            pending = db.count_rows(RowFilter(statuses=(st.STATUS_PENDING,), countries=args.country))
            scope = " w wybranych krajach"
        else:
            pending, scope = db.status_counts().get(st.STATUS_PENDING, 0), ""
        print(f"\n--dry-run: nic nie wysłano. {pending} maili{scope} ma status 'do_wyslania'. "
              "Otwórz panel (python3 server.py), żeby je przejrzeć.")
        return 0

    smtp_factory = sending.smtp_factory_from_config(config)

    if args.test_send:
        if not (args.id or args.company):
            sys.exit("Użyj --id lub --company, żeby wskazać, który mail wysłać testowo.")
        found = [r for r in db.select_candidates(
            row_id=args.id or "", company=args.company or "", countries=args.country,
            statuses=tuple(st.VALID_STATUSES), limit=None) if r["body_html"]]
        if not found:
            sys.exit("Nie znaleziono wygenerowanego maila pasującego do filtra.")
        if len(found) > 1:
            sys.exit(f"Filtr pasuje do {len(found)} wierszy — doprecyzuj --id.")
        row = found[0]
        message = sending.build_message_for_row(row, args.test_send, config["SMTP_USER"], sender_name)
        with smtp_factory() as client:
            client.send(message)
        print(f"Wysłano testowo na {args.test_send} (treść z {row['id']} / {row['company']}). "
              "Status tego wiersza NIE został zmieniony.")
        return 0

    sent_today = db.sent_today_count()
    limit = min(args.limit, max(0, args.daily_cap - sent_today))
    to_send = db.select_candidates(
        segment=args.segment, row_id=args.id or "", company=args.company or "", countries=args.country,
        statuses=(st.STATUS_PENDING,), limit=limit) if limit > 0 else []

    if not to_send:
        print(f"\nBrak maili do wysłania (dzisiaj już wysłano {sent_today}, limit dzienny {args.daily_cap}).")
        return 0

    print(f"\nDo wysłania teraz: {len(to_send)} maili (dzisiaj już wysłano {sent_today}/{args.daily_cap}).")
    summary, foreign = country_summary(to_send)
    print(f"Kraje: {summary}.")
    if foreign:
        print(f"UWAGA: {foreign} z tych maili jedzie do firm spoza Polski, a szablony są po polsku. "
              "Sprawdź treść albo zawęź wysyłkę do Polski: --country PL.")
    if not args.yes:
        answer = input(f"Wysyłam {len(to_send)} maili z konta {config['SMTP_USER']}. Kontynuować? [y/N] ")
        if answer.strip().lower() not in ("y", "yes", "tak"):
            print("Przerwano.")
            return 0

    labels = {"sent": "OK   ", "sent_unrecorded": "OK?  ", "error": "BLAD ", "skipped": "POMIN"}
    log_lines = []
    try:
        with smtp_factory() as client:
            for i, row in enumerate(to_send):
                outcome = sending.send_row(
                    db, row["id"], smtp_factory=lambda: nullcontext(client), config=config,
                    sender_name=sender_name, allowed_from=(st.STATUS_PENDING,))
                line = f"{labels[outcome.outcome]} {row['id']}  {row['company']}  <{row['email']}>"
                if outcome.message:
                    line += f"  -- {outcome.message}"
                print(line)
                log_lines.append(line)
                if i < len(to_send) - 1:
                    time.sleep(random.uniform(args.delay_min, args.delay_max))
    except KeyboardInterrupt:
        print("\nPrzerwano (Ctrl+C). Wiersz, który był wysyłany, oznaczono jako błąd — sprawdź folder Wysłane.")
        return 130

    log_path = write_log(log_lines)
    ok_count = sum(1 for line in log_lines if line.startswith("OK"))
    print(f"\nGotowe: {ok_count}/{len(to_send)} wysłanych. Log: {log_path}")
    return 0


def main() -> None:
    args = build_parser().parse_args()
    try:
        sys.exit(run(args))
    except (ConfigError, DbError) as exc:
        sys.exit(f"BŁĄD: {exc}")


if __name__ == "__main__":
    main()
