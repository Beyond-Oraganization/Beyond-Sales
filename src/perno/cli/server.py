#!/usr/bin/env python3
"""Lokalny serwer aplikacji: panel wysyłki, eksport i kolejne strony z menu.

Uruchom:
    python3 server.py
i otwórz http://localhost:8765/ (otwiera się automatycznie).

Serwer nasłuchuje WYŁĄCZNIE na 127.0.0.1 — nie jest widoczny w sieci lokalnej
ani w internecie. Cała logika (zabezpieczenia, trasy) jest w src/perno/web/app.py i
src/perno/web/pages/; ten plik to tylko adapter HTTP.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from perno import settings as cfg
from perno.db import Database, DbError
from perno.logging_setup import configure_logging
from perno.services import scan_control
from perno.settings import ConfigError, ConfigFile
from perno.web.app import App
from perno.web.context import AppContext
from perno.web.http import MAX_BODY, Request, Response
from perno.web.pages import PAGES

CHUNK = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    server: AppServer  # aplikacja leży na serwerze (`self.server.app`), nie na klasie handlera

    def log_message(self, fmt: str, *args) -> None:
        pass

    def _handle(self) -> None:
        parts = urlsplit(self.path)
        headers = {name.lower(): value for name, value in self.headers.items()}
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if 0 < length <= MAX_BODY else b""
        request = Request(
            method=self.command, path=parts.path, query=parse_qs(parts.query),
            headers=headers, body=body, content_length=length,
        )
        self._write(self.server.app.dispatch(request))

    def _write(self, resp: Response) -> None:
        length = resp.length if resp.stream is not None else len(resp.body)
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(length))
        for name, value in resp.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if resp.stream is not None:
            with resp.stream:
                while True:
                    chunk = resp.stream.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        else:
            self.wfile.write(resp.body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _handle


class AppServer(ThreadingHTTPServer):
    """Serwer HTTP niosący aplikację: handler bierze ją z `self.server.app`, więc nie ma współdzielonego stanu klasy."""

    def __init__(self, address: tuple, app: App):
        super().__init__(address, Handler)
        self.app = app


def build_app(config_path: str) -> App:
    """Korzeń kompozycji: tu powstają zależności stron (baza, konfiguracja, dziennik cofania, magazyn skrapera)."""
    config = ConfigFile(config_path)
    db = Database.from_config(config.load())
    info = db.check_connection()
    if not info["table_exists"]:
        raise DbError("Tabela maile nie istnieje. Uruchom migrate_csv_to_sql.py (lub: python3 -m perno.db --init).")
    added = db.upgrade_schema()  # starsza baza dostaje nowe, opcjonalne kolumny (kraj, miasto, adres, opis)
    if added:
        print(f"Uaktualniono tabelę maile — dodano kolumny: {', '.join(added)}.")
    recovered = db.recover_stale_claims()
    if recovered:
        print(f"UWAGA: zawieszone wysyłki oznaczono jako błąd (sprawdź folder Wysłane): {', '.join(recovered)}")
    if info["row_count"] == 0:
        print("UWAGA: baza jest pusta. Jeśli masz historię w Maile.csv, uruchom najpierw migrate_csv_to_sql.py — "
              "„Odśwież dane” w panelu odmówi generowania do pustej bazy.")
    ctx = AppContext(db=db, pages=PAGES, config=config)
    scan_control.recover_on_start(ctx.scan_store.get)
    return App(ctx)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Nie otwieraj przeglądarki automatycznie.")
    parser.add_argument("--config", default=cfg.DEFAULT_CONFIG)
    args = parser.parse_args()

    try:
        app = build_app(args.config)
    except (ConfigError, DbError) as exc:
        sys.exit(f"BŁĄD: {exc}")

    server = AppServer(("127.0.0.1", args.port), app)
    port = server.server_address[1]
    app.set_port(port)
    url = f"http://localhost:{port}/"
    print(f"Aplikacja: {url}   (baza: {app.ctx.db.describe()})")
    print("Serwer nasłuchuje tylko na 127.0.0.1. Zatrzymaj: Ctrl+C")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nZatrzymano.")
        hint = scan_control.shutdown_hint(app.ctx.scan_store.get)
        if hint:
            print(hint)


if __name__ == "__main__":
    main()
