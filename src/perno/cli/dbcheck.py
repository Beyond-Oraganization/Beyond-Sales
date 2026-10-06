#!/usr/bin/env python3
"""Diagnostyka bazy (bez wypisywania haseł):

  python3 -m perno.db --check     # połączenie, wersja serwera, tryb SQL, stan tabel, spójność maili i firm
  python3 -m perno.db --init      # utwórz tabele maile i podmioty, jeśli nie istnieją (idempotentne)
  python3 -m perno.db --upgrade   # dodaj brakującą tabelę podmioty, kolumny i indeksy; dane bez zmian (bezpieczne przy
                                  # działającym starym panelu i demonie)
  python3 -m perno.db --adopt     # przypisz maile do firm (kopia zapasowa, odmawia przy działającym Automacie i panelu)
"""

from __future__ import annotations

import argparse
import socket
import sys
from typing import Optional

from perno import settings as cfg
from perno.db import Database, DbError
from perno.db.adoption import Diagnostics
from perno.db.automat_store import AutomatStore
from perno.services import firms_import
from perno.settings import DEFAULT_CONFIG, ConfigError, load_config

DEFAULT_PANEL_PORT = 8765


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Diagnostyka bazy (bez wypisywania haseł).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Sprawdź połączenie i stan tabel.")
    group.add_argument("--init", action="store_true", help="Utwórz tabele maile i podmioty, jeśli nie istnieją (idempotentne).")
    group.add_argument("--upgrade", action="store_true",
                       help="Dodaj tabelę podmioty oraz brakujące kolumny i indeksy; dane bez zmian.")
    group.add_argument("--adopt", action="store_true",
                       help="Przypisz maile do firm (po kopii zapasowej); wymaga wcześniejszego --upgrade.")
    parser.add_argument("--allow-running", action="store_true",
                        help="Przy --adopt: nie odmawiaj, gdy działa demon Automatu albo panel (na własną odpowiedzialność).")
    parser.add_argument("--panel-port", type=int, default=DEFAULT_PANEL_PORT, help="Port panelu sprawdzany przy --adopt.")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    return parser


def panel_listening(port: int) -> bool:
    """Czy na tym komputerze coś nasłuchuje na porcie panelu (stary panel mógłby w trakcie przejęcia zmieniać dane)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def adoption_refusal(db: Database, args: argparse.Namespace) -> Optional[str]:
    """Powód, dla którego nie wolno teraz przejmować maili (None = wolno)."""
    if not db.has_table("podmioty") or any(c.name in ("company_id", "position") for c in db.missing_columns()):
        return "Schemat nie jest uaktualniony — uruchom najpierw: python3 -m perno.db --upgrade"
    if args.allow_running:
        return None
    if db.has_table("automat_przebiegi") and AutomatStore(db).active_run():
        return ("Działa demon Automatu. Zatrzymaj go (automat.py uninstall; plan zostaje uzbrojony w bazie) "
                "albo dodaj --allow-running.")
    if panel_listening(args.panel_port):
        return f"Na porcie {args.panel_port} działa panel. Zatrzymaj go (Ctrl+C) albo dodaj --allow-running."
    return None


def run_adopt(db: Database, args: argparse.Namespace) -> None:
    refusal = adoption_refusal(db, args)
    if refusal:
        raise DbError(refusal)
    path = firms_import.backup_table(db, cfg.BACKUP_DIR, "przejecie-firm")
    print(f"Kopia zapasowa bazy: {path}")
    report = db.adopt()
    print(f"Przejęcie: założono firm: {report.companies_added}, przypisano maili do firm: {report.rows_linked}.")
    show_diagnostics(db.diagnose())


def show_diagnostics(found: Diagnostics) -> None:
    print(f"Firm: {found.state.companies}, maili: {found.state.mails}.")
    problems = [
        (found.state.orphans, "maili bez firmy (do przejęcia: --adopt albo start nowego programu)"),
        (found.companies_without_mail, "firm bez żadnego maila"),
        (found.mails_without_company, "maili wskazujących nieistniejącą firmę"),
        (found.mirror_drift, "maili ze starymi kolumnami firmowymi różnymi od firmy (stary kod czyta nieaktualne dane)"),
    ]
    for count, text in problems:
        if count:
            print(f"UWAGA: {count} {text}.")
    if found.clean and found.state.mails:
        print("Maile i firmy są spójne.")


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        db = Database.from_config(load_config(args.config))
        if args.init:
            db.check_connection()
            db.ensure_schema()
            print("Tabele maile i podmioty są gotowe.")
        if args.upgrade:
            if not db.check_connection()["table_exists"]:
                raise DbError("Tabela maile nie istnieje — użyj najpierw --init albo migrate_csv_to_sql.py.")
            added = db.upgrade_schema()
            print(f"Dodano: {', '.join(added)}." if added else "Schemat jest aktualny — nic do dodania.")
        if args.adopt:
            run_adopt(db, args)
        info = db.check_connection()
        ready = info["table_exists"] and db.has_table("podmioty") and not db.missing_columns()
        diagnostics = db.diagnose() if ready else None
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
    if diagnostics is not None and args.check:
        show_diagnostics(diagnostics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
