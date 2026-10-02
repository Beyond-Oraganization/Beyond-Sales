#!/usr/bin/env python3
"""Cienki wrapper: uruchamia `perno.cli.scrape` (kod w src/perno/cli/scrape.py).

Wymaga środowiska projektu: `uv sync`, potem `.venv/bin/python scrape.py`.
"""

try:
    from perno.cli.scrape import main
except ModuleNotFoundError as exc:
    if exc.name == "perno" or (exc.name or "").startswith("perno."):
        raise SystemExit(
            "Brak pakietu perno w tym Pythonie. Uruchom `uv sync`, potem: .venv/bin/python scrape.py"
        ) from exc
    raise

if __name__ == "__main__":
    raise SystemExit(main())
