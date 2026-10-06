#!/usr/bin/env python3
"""Nowe firmy w bazie i ich kraj: import z CSV, przypisanie kraju/miasta/adresu, raport.

Typowy przebieg (każde polecenie zmieniające bazę najpierw zapisuje jej kopię w backup/):
  python3 firmy.py import data/nowe_eventy.csv --segment eventy --dry-run   # co by się stało, nic nie zapisuje
  python3 firmy.py import data/nowe_eventy.csv --segment eventy
  python3 firmy.py import data/nowe_driverzy.csv --segment driverzy
  python3 firmy.py kraje data/kraje_firm.csv --dry-run
  python3 firmy.py kraje data/kraje_firm.csv
  python3 firmy.py raport                    # ile firm w którym kraju i które nie mają jeszcze kraju

Nowa firma nie ma jeszcze e-maila, więc dostaje status „Brak e-maila" (nie da się jej wysłać przez pomyłkę).
`kraje` domyślnie uzupełnia tylko puste pola — to, co poprawiłeś ręcznie w panelu, zostaje (`--overwrite` nadpisuje).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from perno import settings as cfg
from perno.db import Database, DbError
from perno.domain import countries
from perno.domain import statuses as st
from perno.services import firms_import as firms
from perno.settings import ConfigError, load_config

SAMPLE = 8


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=cfg.DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="Dodaj firmy z CSV (company_display_name, website, what_they_sell) do segmentu.")
    imp.add_argument("csv")
    imp.add_argument("--segment", required=True, choices=st.SEGMENT_ORDER)
    imp.add_argument("--dry-run", action="store_true", help="Pokaż plan, niczego nie zapisuj (ani schematu, ani wierszy).")
    imp.add_argument("--no-backup", action="store_true", help="Pomiń kopię zapasową bazy przed zmianą.")

    assign = sub.add_parser("kraje", help="Przypisz firmom kraj/miasto/adres z pliku (domain, country, city, address).")
    assign.add_argument("csv")
    assign.add_argument("--overwrite", action="store_true", help="Nadpisz też pola, które firma ma już wypełnione.")
    assign.add_argument("--dry-run", action="store_true", help="Pokaż plan, niczego nie zapisuj.")
    assign.add_argument("--no-backup", action="store_true", help="Pomiń kopię zapasową bazy przed zmianą.")

    report = sub.add_parser("raport", help="Liczba firm wg kraju i lista firm bez kraju.")
    report.add_argument("--wszystkie", action="store_true", help="Wypisz wszystkie firmy bez kraju, nie tylko pierwsze.")
    return parser


def open_database(args) -> Database:
    db = Database.from_config(load_config(args.config))
    if not db.check_connection()["table_exists"]:
        raise DbError("Tabela maile nie istnieje. Uruchom migrate_csv_to_sql.py (lub: python3 -m perno.db --init).")
    return db


def prepare_writes(db: Database, args, label: str) -> None:
    """Kopia zapasowa przed jakąkolwiek zmianą, potem uaktualnienie schematu (kraj, miasto, adres, opis)."""
    if not args.no_backup:
        path = firms.backup_table(db, cfg.BACKUP_DIR, label)
        print(f"Kopia zapasowa bazy: {path}")
    added = db.prepare()
    if added:
        print(f"Uaktualniono schemat bazy — dodano: {', '.join(added)}.")


def require_profile_columns(db: Database) -> None:
    if db.missing_columns():
        raise DbError("Baza nie ma jeszcze kolumn z danymi o firmie (kraj, miasto, adres, opis). "
                      "Uruchom: python3 -m perno.db --upgrade (albo to polecenie bez --dry-run).")


def show(label: str, items: list, formatter) -> None:
    if not items:
        return
    print(f"{label} ({len(items)}):")
    for item in items[:SAMPLE]:
        print("  " + formatter(item))
    if len(items) > SAMPLE:
        print(f"  … i {len(items) - SAMPLE} kolejnych")


def cmd_import(args) -> int:
    rows = firms.read_import_csv(args.csv)
    db = open_database(args)
    if not args.dry_run:
        prepare_writes(db, args, "importem")
    result = firms.import_firms(db, rows, args.segment, dry_run=args.dry_run)
    verb = "Do dodania" if args.dry_run else "Dodano"
    print(f"{verb}: {len(result.new)} firm do segmentu {args.segment} (status „{st.STATUS_META[st.STATUS_SKIPPED_NO_EMAIL][0]}”).")
    show("Pominięto — domena jest już w bazie", result.skipped_existing, lambda x: f"{x[0]}  (jako {x[1]})")
    show("Pominięto — powtórzona w pliku", result.duplicates_in_file, lambda x: f"wiersz {x[0]}: {x[1]}")
    if result.new:
        print(f"Identyfikatory: {result.new[0]['id']} … {result.new[-1]['id']}")
    if args.dry_run:
        print("--dry-run: nic nie zapisano.")
    elif result.new:
        print("Następny krok: przypisz kraje — python3 firmy.py kraje data/kraje_firm.csv")
    return 0


def cmd_kraje(args) -> int:
    entries = firms.read_profile_csv(args.csv)
    db = open_database(args)
    if args.dry_run:
        require_profile_columns(db)
    else:
        prepare_writes(db, args, "krajami")
    result = firms.assign_profiles(db, entries, overwrite=args.overwrite, dry_run=args.dry_run)
    verb = "Do uzupełnienia" if args.dry_run else "Uzupełniono"
    print(f"{verb}: {len(result.updated)} firm; bez zmian: {result.unchanged}.")
    counts = Counter(changes.get("country") for _, changes in result.updated if changes.get("country"))
    if counts:
        print("Nowe kraje: " + ", ".join(f"{countries.country_name(code)} {n}" for code, n in counts.most_common()))
    show("Pozostawiono bez zmian (pole było już wypełnione; użyj --overwrite, żeby nadpisać)", result.kept,
         lambda x: f"{x[0]}.{x[1]}: {x[2]!r} (plik proponuje {x[3]!r})")
    show("Domeny z pliku, których nie ma w bazie", result.unmatched_domains, lambda x: x)
    show("Firmy w bazie, których nie ma w pliku", result.rows_without_entry, lambda x: x)
    if args.dry_run:
        print("--dry-run: nic nie zapisano.")
    return 0


def cmd_raport(args) -> int:
    db = open_database(args)
    require_profile_columns(db)
    report = firms.profile_report(db)
    print(f"Firm w bazie: {report['total']}; z krajem: {report['total'] - len(report['without_country'])}; "
          f"z miastem: {report['with_city']}; z adresem: {report['with_address']}.")
    for code, per_segment in sorted(report["by_country"].items(), key=lambda item: -sum(item[1].values())):
        detail = ", ".join(f"{segment} {n}" for segment, n in sorted(per_segment.items()))
        print(f"  {countries.flag(code)} {countries.country_name(code):<18} {sum(per_segment.values()):>4}   ({detail})")
    missing = report["without_country"]
    if missing:
        limit = len(missing) if args.wszystkie else SAMPLE
        print(f"Firmy bez kraju ({len(missing)}):")
        for item in missing[:limit]:
            print(f"  {item['id']:<16} {item['company']}  {item['website']}")
        if len(missing) > limit:
            print(f"  … i {len(missing) - limit} kolejnych (--wszystkie)")
    return 0


COMMANDS = {"import": cmd_import, "kraje": cmd_kraje, "raport": cmd_raport}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (ConfigError, DbError) as exc:
        print(f"BŁĄD: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"BŁĄD pliku: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
