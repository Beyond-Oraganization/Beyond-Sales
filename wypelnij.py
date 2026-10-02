#!/usr/bin/env python3
"""Cienki wrapper: uruchamia `perno.cli.enrich` (kod w src/perno/cli/enrich.py).

Wymaga środowiska projektu: `uv sync`, potem `.venv/bin/python wypelnij.py`.
"""

try:
    from perno.cli.enrich import main
except ModuleNotFoundError as exc:
    if exc.name == "perno" or (exc.name or "").startswith("perno."):
        raise SystemExit(
            "Brak pakietu perno w tym Pythonie. Uruchom `uv sync`, potem: .venv/bin/python wypelnij.py"
        ) from exc
    raise

if __name__ == "__main__":
    raise SystemExit(main())
