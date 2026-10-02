#!/usr/bin/env python3
"""Cienki wrapper: uruchamia `perno.cli.send` (kod w src/perno/cli/send.py).

Wymaga środowiska projektu: `uv sync`, potem `.venv/bin/python send_emails.py`.
"""

try:
    from perno.cli.send import main
except ModuleNotFoundError as exc:
    if exc.name == "perno" or (exc.name or "").startswith("perno."):
        raise SystemExit(
            "Brak pakietu perno w tym Pythonie. Uruchom `uv sync`, potem: .venv/bin/python send_emails.py"
        ) from exc
    raise

if __name__ == "__main__":
    raise SystemExit(main())
