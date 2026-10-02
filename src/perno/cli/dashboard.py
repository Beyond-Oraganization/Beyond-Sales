#!/usr/bin/env python3
"""Generuje statyczny podgląd index.html z bazy — na żądanie, nie po każdej zmianie.

  python3 dashboard.py                  # lekki podgląd bez treści maili
  python3 dashboard.py --with-bodies    # z treściami maili (duży plik przy tysiącach firm)
  python3 dashboard.py --output /ścieżka/podglad.html

Podgląd działa po dwukliku (bez serwera). Dane na żywo, edycja i wysyłka są w panelu: python3 server.py.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from perno import settings as cfg
from perno.db import Database, DbError
from perno.services import dashboard
from perno.settings import ConfigError, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default=cfg.INDEX_HTML, help="Gdzie zapisać plik (domyślnie index.html w katalogu projektu).")
    parser.add_argument("--with-bodies", action="store_true", help="Dołącz treści maili (plik jest wtedy kilkadziesiąt razy większy).")
    parser.add_argument("--config", default=cfg.DEFAULT_CONFIG)
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        db = Database.from_config(load_config(args.config))
        count = dashboard.write_dashboard(db, args.output, with_bodies=args.with_bodies)
    except (ConfigError, DbError) as exc:
        print(f"BŁĄD: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"BŁĄD zapisu pliku: {exc}", file=sys.stderr)
        return 1
    print(f"Zapisano {args.output}: {count} wierszy{' z treściami maili' if args.with_bodies else ' (bez treści maili)'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
