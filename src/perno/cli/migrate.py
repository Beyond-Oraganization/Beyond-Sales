#!/usr/bin/env python3
"""Jednorazowa migracja historii z Maile.csv do bazy (tabela `maile`).

Tryby:
  (domyślnie)          migracja: walidacja CSV → wstawienie w jednej transakcji →
                       weryfikacja przed i po zatwierdzeniu → przeniesienie Maile.csv do backup/
  --check              tylko sprawdza i waliduje CSV (bez dotykania bazy)
  --verify-only        tylko do odczytu: porównuje bazę z CSV (sensowne tuż po migracji)
  --export-legacy P    zapisuje zawartość bazy w starym formacie Maile.csv do pliku P
                       (dowód identyczności i ŚCIEŻKA POWROTU do starej aplikacji)

Bezpieczeństwo: skrypt nigdy nie zapisuje do Maile.csv, odmawia pracy na niepustej
tabeli (bez opcji „zastąp") i przenosi CSV dopiero po pozytywnej weryfikacji.
Przed migracją zatrzymaj server.py.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import os
import shutil
import socket
import sys
import tempfile
from collections import Counter
from datetime import datetime
from typing import Optional

from perno import db as dbmod
from perno import settings as cfg
from perno.db import legacy_csv
from perno.domain import statuses as st
from perno.settings import ConfigError, load_config


class MigrationError(Exception):
    pass


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_csv_path() -> Optional[str]:
    if os.path.exists(cfg.LEGACY_CSV):
        return cfg.LEGACY_CSV
    backups = sorted(glob.glob(os.path.join(cfg.BACKUP_DIR, "Maile_*_przed-migracja.csv")))
    return backups[-1] if backups else None


def parse_csv(path: str) -> tuple:
    """Zwraca (wiersze, ostrzeżenia); przy błędach rzuca MigrationError z pełną listą."""
    with open(path, newline="", encoding="utf-8") as f:
        header = f.readline().rstrip("\r\n").split(",")
    if tuple(header) != dbmod.LEGACY_COLUMNS:
        raise MigrationError(f"Nagłówek CSV nie zgadza się z oczekiwanym.\n  jest:      {header}\n  oczekiwano: {list(dbmod.LEGACY_COLUMNS)}")

    rows = legacy_csv.read_rows(path)
    errors, warnings, seen = [], [], set()
    for number, row in enumerate(rows, start=2):
        where = f"wiersz {number} ({row.get('id', '?')})"
        if row["id"] in seen:
            errors.append(f"{where}: zduplikowane id")
        seen.add(row["id"])
        if row["status"] not in st.VALID_STATUSES:
            errors.append(f"{where}: nieznany status {row['status']!r}")
        elif row["status"] == st.STATUS_SENDING:
            errors.append(f"{where}: status 'wysylanie' nie może pochodzić z pliku CSV")
        if row["row_number"]:
            expected = f"{row['segment']}-{int(row['row_number']):03d}" if row["row_number"].isdigit() else None
            if expected != row["id"]:
                errors.append(f"{where}: id nie zgadza się z segment/row_number (oczekiwano {expected})")
        try:
            dbmod.from_api(row)
        except dbmod.InvalidValue as exc:
            errors.append(f"{where}: {exc}")
        if row["status"] == st.STATUS_SENT and not row["sent_at"]:
            warnings.append(f"{where}: status 'wyslano' bez sent_at")
        if row["status"] not in st.KEEPS_SENT_AT and row["sent_at"]:
            warnings.append(f"{where}: sent_at przy statusie {row['status']!r}")
    if errors:
        raise MigrationError("Plik CSV zawiera błędy:\n  " + "\n  ".join(errors))
    return rows, warnings


def run_checks(db_rows: list, csv_rows: list, csv_path: str) -> list:
    """V1–V6: lista (nazwa, ok, szczegóły)."""
    results = []

    def add(name, ok, detail=""):
        results.append((name, ok, detail))

    add("V1 liczba wierszy", len(db_rows) == len(csv_rows), f"baza {len(db_rows)}, CSV {len(csv_rows)}")

    pairs = lambda rows: Counter((r["segment"], r["status"]) for r in rows)
    add("V2 liczby (segment, status)", pairs(db_rows) == pairs(csv_rows), str(dict(pairs(db_rows))))

    db_by_id = {r["id"]: r for r in db_rows}
    diffs = []
    for row in csv_rows:
        other = db_by_id.get(row["id"])
        if other is None:
            diffs.append(f"{row['id']}: brak w bazie")
            continue
        for name in dbmod.LEGACY_COLUMNS:
            if other[name] != row[name]:
                diffs.append(f"{row['id']}.{name}: baza {other[name]!r} ≠ CSV {row[name]!r}")
    add("V3 pola wszystkich wierszy", not diffs, "; ".join(diffs[:5]) + (" …" if len(diffs) > 5 else ""))

    in_csv_order = [db_by_id[row["id"]] for row in csv_rows if row["id"] in db_by_id]
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "roundtrip.csv")
        legacy_csv.write_rows(out, in_csv_order)
        same = sha256_of(out) == sha256_of(csv_path)
    add("V4 CSV z bazy bajt w bajt = oryginał", same, "" if same else "dane mogą być identyczne, ale zapis pliku się różni")

    span = lambda rows: (min((r["sent_at"] for r in rows if r["sent_at"]), default=""),
                         max((r["sent_at"] for r in rows if r["sent_at"]), default=""),
                         sum(1 for r in rows if r["sent_at"]))
    add("V5 sent_at (min, max, liczba)", span(db_rows) == span(csv_rows), str(span(db_rows)))

    broken = [r["id"] for r in db_rows
              if r["status"] not in st.VALID_STATUSES
              or (r["row_number"] and r["id"] != f"{r['segment']}-{int(r['row_number']):03d}")
              or (r["status"] == st.STATUS_SENT and not r["sent_at"])]
    add("V6 niezmienniki", not broken, ", ".join(broken[:5]))
    return results


def print_checks(results: list) -> bool:
    for name, ok, detail in results:
        print(f"  {'OK   ' if ok else 'BŁĄD'} {name}" + (f"  — {detail}" if detail and (not ok or name.startswith('V2')) else ""))
    return all(ok for _, ok, _ in results)


def server_is_running(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def open_db(args) -> dbmod.Database:
    return dbmod.Database.from_config(load_config(args.config))


def cmd_check(args) -> int:
    path = args.csv or default_csv_path()
    if not path:
        raise MigrationError("Nie znaleziono Maile.csv.")
    rows, warnings = parse_csv(path)
    print(f"Plik {path}: {len(rows)} wierszy, {len(set(r['segment'] for r in rows))} segmenty, "
          f"statusy: {dict(Counter(r['status'] for r in rows))}")
    for warning in warnings:
        print(f"  ostrzeżenie: {warning}")
    print(f"sha256: {sha256_of(path)}")
    print("CSV jest poprawny i gotowy do migracji.")
    return 0


def cmd_verify(args) -> int:
    path = args.csv or default_csv_path()
    if not path:
        raise MigrationError("Nie znaleziono CSV do porównania (podaj --csv).")
    rows, _ = parse_csv(path)
    db = open_db(args)
    print(f"Porównuję bazę ({db.describe()}) z {path}:")
    ok = print_checks(run_checks(db.fetch_all(), rows, path))
    return 0 if ok else 2


def cmd_export_legacy(args) -> int:
    db = open_db(args)
    db.check_connection()
    rows = db.fetch_all()
    legacy_csv.write_rows(args.export_legacy, rows)
    print(f"Zapisano {len(rows)} wierszy w starym formacie: {args.export_legacy}")
    print(f"sha256: {sha256_of(args.export_legacy)}")
    return 0


def cmd_migrate(args) -> int:
    if server_is_running(args.server_port):
        raise MigrationError(f"Na porcie {args.server_port} działa serwer. Zatrzymaj server.py przed migracją "
                             "(stara wersja mogłaby odtworzyć Maile.csv).")
    path = args.csv or cfg.LEGACY_CSV
    if not os.path.exists(path):
        raise MigrationError(f"Brak pliku {path}. Jeśli migracja już się odbyła, użyj --verify-only.")

    db = open_db(args)
    db.check_connection()
    db.ensure_schema()
    existing = db.count_rows()
    if existing:
        raise MigrationError(
            f"Tabela maile nie jest pusta ({existing} wierszy) — odmawiam nadpisania historii.\n"  # noqa: S608 — komunikat, nie SQL
            "Jeśli to świadomy powrót do stanu z CSV, opróżnij ją ręcznie w kliencie MySQL:\n"
            "    DELETE FROM maile;")

    rows, warnings = parse_csv(path)
    original_hash = sha256_of(path)
    print(f"CSV: {len(rows)} wierszy, sha256 {original_hash[:16]}…")
    for warning in warnings:
        print(f"  ostrzeżenie: {warning}")

    print("Wstawiam w jednej transakcji i weryfikuję przed zatwierdzeniem…")
    with db.transaction() as conn:
        db.insert_rows(conn, rows)
        if not print_checks(run_checks(db.fetch_all(conn), rows, path)):
            raise MigrationError("Weryfikacja przed zatwierdzeniem nie przeszła — wycofano transakcję, tabela jest pusta.")

    print("Zatwierdzono. Weryfikacja na nowym połączeniu…")
    if not print_checks(run_checks(db.fetch_all(), rows, path)):
        print("ALARM: dane zatwierdzone, ale weryfikacja końcowa nie przeszła. Maile.csv NIE został przeniesiony.")
        return 2

    os.makedirs(cfg.BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(cfg.BACKUP_DIR, f"Maile_{stamp}_przed-migracja.csv")
    shutil.move(path, backup_path)
    if sha256_of(backup_path) != original_hash:
        print(f"ALARM: suma kontrolna kopii {backup_path} różni się od oryginału!")
        return 2
    report_path = os.path.join(cfg.BACKUP_DIR, f"MIGRACJA_{stamp}.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Migracja Maile.csv → {db.describe()}\nData: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"Wierszy: {len(rows)}\nsha256 CSV: {original_hash}\nKopia: {backup_path}\n"
                f"Statusy: {dict(Counter(r['status'] for r in rows))}\n")
    print(f"\nGotowe. Stary plik: {backup_path}\nRaport: {report_path}\n"
          f"Powrót do starej aplikacji: python3 migrate_csv_to_sql.py --export-legacy Maile.csv")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Tylko waliduj CSV (bez bazy).")
    mode.add_argument("--verify-only", action="store_true", help="Tylko odczyt: porównaj bazę z CSV.")
    mode.add_argument("--export-legacy", metavar="PLIK", help="Zapisz bazę w starym formacie Maile.csv.")
    parser.add_argument("--csv", help="Plik źródłowy (domyślnie Maile.csv; dla --verify-only także kopia z backup/).")
    parser.add_argument("--config", default=cfg.DEFAULT_CONFIG)
    parser.add_argument("--server-port", type=int, default=8765, help="Port, na którym nie może działać server.py.")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.check:
            return cmd_check(args)
        if args.verify_only:
            return cmd_verify(args)
        if args.export_legacy:
            return cmd_export_legacy(args)
        return cmd_migrate(args)
    except (MigrationError, ConfigError, dbmod.DbError) as exc:
        print(f"BŁĄD: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
