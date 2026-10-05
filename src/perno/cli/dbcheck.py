#!/usr/bin/env python3
"""Diagnostyka bazy (bez wypisywania haseł):

  python3 -m perno.db --check     # połączenie, wersja serwera, tryb SQL, stan tabeli
  python3 -m perno.db --init      # utwórz tabelę maile, jeśli nie istnieje (idempotentne)
  python3 -m perno.db --upgrade   # dodaj brakujące kolumny (kraj, miasto, adres, opis); dane bez zmian
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from perno.db import Database, DbError
from perno.settings import DEFAULT_CONFIG, ConfigError, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Diagnostyka bazy (bez wypisywania haseł).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Sprawdź połączenie i stan tabeli.")
    group.add_argument("--init", action="store_true", help="Utwórz tabelę maile, jeśli nie istnieje (idempotentne).")
    group.add_argument("--upgrade", action="store_true",
                       help="Dodaj do istniejącej tabeli maile brakujące kolumny (kraj, miasto, adres, opis); dane bez zmian.")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        db = Database.from_config(load_config(args.config))
        if args.init:
            db.check_connection()
            db.ensure_schema()
            print("Tabela maile jest gotowa.")
        if args.upgrade:
            if not db.check_connection()["table_exists"]:
                raise DbError("Tabela maile nie istnieje — użyj najpierw --init albo migrate_csv_to_sql.py.")
            added = db.upgrade_schema()
            print(f"Dodano kolumny: {', '.join(added)}." if added else "Schemat jest aktualny — nic do dodania.")
        info = db.check_connection()
    except (ConfigError, DbError) as exc:
        print(f"BŁĄD: {exc}", file=sys.stderr)
        return 1
    print(f"Połączono: {info['db']}  ({info['backend']} {info['version']})")
    print(f"sql_mode: {info['sql_mode']}")
    if info["backend"] == "mysql" and "STRICT" not in (info["sql_mode"] or ""):
        print("UWAGA: tryb SQL nie jest ścisły (brak STRICT_TRANS_TABLES) — zbyt długie dane mogą być cicho ucinane.")
    if info["search_warning"]:
        print(f"UWAGA: {info['search_warning']}")
    print(f"Tabela maile: {'jest, wierszy: ' + str(info['row_count']) if info['table_exists'] else 'jeszcze nie istnieje'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
