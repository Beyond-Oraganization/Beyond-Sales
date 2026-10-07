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
import sys
from collections import Counter
from datetime import datetime

from perno import settings as cfg
from perno.db import Database, DbError
from perno.domain import countries, mail_rules
from perno.domain import statuses as st
from perno.domain.rowfilter import NO_COUNTRY, RowFilter
from perno.logging_setup import configure_logging
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
    parser.add_argument("--all-addresses", action="store_true",
                        help="Wysyłaj do wszystkich adresów firmy w tym przebiegu (domyślnie najwyżej jeden adres na firmę).")
    parser.add_argument("--yes", action="store_true", help="Pomiń potwierdzenie przed realną wysyłką.")
    parser.add_argument("--allow-empty-db", action="store_true",
                        help="Zezwól na generowanie do PUSTEJ bazy (tylko świeża instalacja bez historii).")
    parser.add_argument("--config", default=cfg.DEFAULT_CONFIG)
    return parser


def open_database(config: dict) -> Database:
    """Baza z gotowym schematem; przywraca też wiersze zawieszone w „Wysyłanie…” po przerwanym przebiegu."""
    db = Database.from_config(config)
    if not db.check_connection()["table_exists"]:
        raise DbError("Tabela maile nie istnieje. Uruchom migrate_csv_to_sql.py (lub: python3 -m perno.db --init).")
    db.prepare()  # idempotentne: dodaje brakującą tabelę firm, kolumny i indeksy, przyjmuje maile bez firmy
    recovered = db.recover_stale_claims()
    if recovered:
        print(f"UWAGA: zawieszone wysyłki oznaczono jako błąd (sprawdź folder Wysłane): {', '.join(recovered)}")
    return db


def generate(db: Database, args: argparse.Namespace, sender_name: str) -> None:
    segments = ("eventy", "driverzy") if args.segment == "both" else (args.segment,)
    result = generation.refresh_segments(
        db, segments, sender_name, args.regenerate_pending, allow_empty=args.allow_empty_db)
    for segment, stats in result.items():
        print(f"{segment}: {stats['created']} nowych, {stats['regenerated']} przeliczonych.")


def report_dry_run(db: Database, args: argparse.Namespace) -> int:
    if args.country:
        pending = db.count_rows(RowFilter(statuses=(st.STATUS_PENDING,), countries=args.country))
        scope = " w wybranych krajach"
    else:
        pending, scope = db.status_counts().get(st.STATUS_PENDING, 0), ""
    print(f"\n--dry-run: nic nie wysłano. {pending} maili{scope} ma status 'do_wyslania'. "
          "Otwórz panel (python3 server.py), żeby je przejrzeć.")
    return 0


def send_test(db: Database, mailer: sending.Mailer, args: argparse.Namespace) -> int:
    """Wysyła jeden wybrany mail na adres testowy; status wiersza zostaje bez zmian."""
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
    message = sending.build_message_for_row(row, args.test_send, mailer.sender)
    with mailer.smtp_factory() as client:
        client.send(message)
    print(f"Wysłano testowo na {args.test_send} (treść z {row['id']} / {row['company']}). "
          "Status tego wiersza NIE został zmieniony.")
    return 0


def confirm_batch(to_send: list, sent_today: int, args: argparse.Namespace, mailer: sending.Mailer) -> bool:
    print(f"\nDo wysłania teraz: {len(to_send)} maili (dzisiaj już wysłano {sent_today}/{args.daily_cap}).")
    summary, foreign = country_summary(to_send)
    print(f"Kraje: {summary}.")
    if foreign:
        print(f"UWAGA: {foreign} z tych maili jedzie do firm spoza Polski, a szablony są po polsku. "
              "Sprawdź treść albo zawęź wysyłkę do Polski: --country PL.")
    if args.yes:
        return True
    answer = input(f"Wysyłam {len(to_send)} maili z konta {mailer.sender.address}. Kontynuować? [y/N] ")
    return answer.strip().lower() in ("y", "yes", "tak")


def send_pending(db: Database, mailer: sending.Mailer, args: argparse.Namespace) -> int:
    # Terminal wysyła ze skrzynki głównej (rotacja skrzynek to panel i Automat), więc limit liczy tylko jej wysyłki.
    sent_today = sending.mailbox_usage(db, [mailer.sender.address])[0]["sent_today"]
    limit = min(args.limit, mail_rules.daily_quota_left(sent_today, args.daily_cap))
    to_send = db.select_candidates(
        segment=args.segment, row_id=args.id or "", company=args.company or "", countries=args.country,
        statuses=(st.STATUS_PENDING,), limit=limit, unheld=True, per_company=not args.all_addresses) if limit > 0 else []
    if not to_send:
        print(f"\nBrak maili do wysłania (dzisiaj już wysłano {sent_today}, limit dzienny {args.daily_cap}).")
        return 0
    if not confirm_batch(to_send, sent_today, args, mailer):
        print("Przerwano.")
        return 0

    labels = {"sent": "OK   ", "sent_unrecorded": "OK?  ", "error": "BLAD ", "skipped": "POMIN"}
    log_lines: list = []

    def report(row: dict, outcome: sending.SendResult) -> None:
        line = f"{labels[outcome.outcome]} {row['id']}  {row['company']}  <{row['email']}>"
        if outcome.message:
            line += f"  -- {outcome.message}"
        print(line)
        log_lines.append(line)

    try:
        sending.send_batch(db, mailer, to_send, delay=(args.delay_min, args.delay_max), on_result=report)
    except KeyboardInterrupt:
        print("\nPrzerwano (Ctrl+C). Wiersz, który był wysyłany, oznaczono jako błąd — sprawdź folder Wysłane.")
        return 130
    log_path = write_log(log_lines)
    ok_count = sum(1 for line in log_lines if line.startswith("OK"))
    print(f"\nGotowe: {ok_count}/{len(to_send)} wysłanych. Log: {log_path}")
    return 0


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not args.dry_run:
        require_smtp(config, args.config)
    db = open_database(config)
    generate(db, args, config.get("SENDER_NAME", ""))
    if args.dry_run:
        return report_dry_run(db, args)
    mailer = sending.build_mailer(config, args.config)
    if args.test_send:
        return send_test(db, mailer, args)
    return send_pending(db, mailer, args)


def main() -> None:
    configure_logging()
    args = build_parser().parse_args()
    try:
        sys.exit(run(args))
    except (ConfigError, DbError) as exc:
        sys.exit(f"BŁĄD: {exc}")


if __name__ == "__main__":
    main()
