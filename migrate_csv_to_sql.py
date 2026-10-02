#!/usr/bin/env python3
"""Cienki wrapper: uruchamia `perno.cli.migrate` (kod w src/perno/cli/migrate.py).

Wymaga środowiska projektu: `uv sync`, potem `.venv/bin/python migrate_csv_to_sql.py`.
"""

try:
    from perno.cli.migrate import main
except ModuleNotFoundError as exc:
    if exc.name == "perno" or (exc.name or "").startswith("perno."):
        raise SystemExit(
            "Brak pakietu perno w tym Pythonie. Uruchom `uv sync`, potem: .venv/bin/python migrate_csv_to_sql.py"
        ) from exc
    raise

if __name__ == "__main__":
    raise SystemExit(main())
