#!/usr/bin/env python3
"""Skraper stron firm: uprzejmie pobiera strony do analizy kontekstu i szukania kontaktów.

Zasady (nie do wyłączenia): robots.txt jest zawsze respektowany, na jedną domenę idzie najwyżej jedno żądanie
naraz z odstępem co najmniej 1 s, równoległość jest tylko MIĘDZY różnymi domenami, a odmowa serwera
(403/429/blokada dla botów) zatrzymuje firmę — bez obchodzenia. Bot przedstawia się jawnie (User-Agent + From).

Typowy przebieg:
  python3 scrape.py seed                          # firmy z tabeli maile (jedna firma = jedna domena)
  python3 scrape.py run --dry-run                 # plan i szacunek czasu, bez sieci
  python3 scrape.py run --firma 3 --pages 30 --concurrency 1     # próba na jednej firmie
  python3 scrape.py status
  python3 scrape.py stop                          # łagodne zatrzymanie (postęp zostaje, wznowisz poleceniem run)

Polecenia: seed | dodaj URL | run | status | stop | purge. Ustawienia domyślne: config.env (SKRAPER_*).
"""

from __future__ import annotations

import argparse
import sys
import threading
from typing import Optional

from perno import settings as cfg
from perno.db import Database, DbError
from perno.scraper import runner
from perno.scraper import statuses as st
from perno.scraper.settings import ScanSettings, SettingsError
from perno.scraper.store import InvalidFirma, RunActive, Store
from perno.settings import ConfigError, load_config

SETTING_FLAGS = (("concurrency", "--concurrency", int, "Ile domen naraz (1–100)."),
                 ("delay", "--delay", float, "Odstęp między żądaniami do jednej domeny w s (min. 1)."),
                 ("depth", "--depth", int, "Maksymalna głębokość linków (1–20)."),
                 ("max_pages", "--pages", int, "Limit stron na firmę (łącznie, także po wznowieniach)."),
                 ("max_mb", "--mb", int, "Limit danych na firmę w MB."),
                 ("max_minutes", "--minutes", int, "Limit czasu na jedno zajęcie firmy w minutach."))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=cfg.DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="Zaimportuj firmy z tabeli maile (idempotentnie).")

    add = sub.add_parser("dodaj", help="Dodaj jedną firmę po adresie strony.")
    add.add_argument("url")
    add.add_argument("--nazwa")

    run = sub.add_parser("run", help="Uruchom skraper (w tym oknie) albo pokaż plan (--dry-run).")
    run.add_argument("--run-id", type=int, help="(wewnętrzne) dołącz do przebiegu zarezerwowanego przez panel.")
    run.add_argument("--firma", action="append", default=[], metavar="ID|DOMENA",
                     help="Tylko ta firma (można powtarzać). Bez tego: wszystkie nieskanowane, w kolejce i z błędem.")
    run.add_argument("--statuses", nargs="+", choices=sorted(st.FIRMA_STATUSY),
                     help="Zamiast domyślnych statusów (nieskanowana, w_kolejce, blad).")
    run.add_argument("--dry-run", action="store_true", help="Tylko plan i szacunek; nie dotyka sieci ani kolejki.")
    for name, flag, kind, text in SETTING_FLAGS:
        run.add_argument(flag, type=kind, dest=name, help=text)

    sub.add_parser("status", help="Stan firm, przebiegu i kontaktów.")
    sub.add_parser("stop", help="Poproś aktywny przebieg o łagodne zatrzymanie.")

    purge = sub.add_parser("purge", help="Usuń stare dane skanu (minimalizacja przechowywania danych).")
    purge.add_argument("--older-than", type=int, required=True, metavar="DNI")
    purge.add_argument("--html-only", action="store_true", help="Usuń tylko zapisany surowy HTML (tekst i kontakty zostają).")
    return parser


def open_store(args) -> tuple:
    config = load_config(args.config)
    db = Database.from_config(config)
    store = Store(db)
    store.ensure_schema()
    return config, store


def settings_from(args, config: dict, stored: Optional[dict] = None) -> ScanSettings:
    params = dict(stored or {})
    for name, *_ in SETTING_FLAGS:
        value = getattr(args, name, None)
        if value is not None:
            params[name] = value
    settings = ScanSettings.from_params(params, config)
    if not settings.contact:
        raise SettingsError("Ustaw SKRAPER_CONTACT w config.env (adres e-mail lub URL) — strony muszą wiedzieć, "
                            "kto ich odwiedza. Bez tego skraper nie wystartuje.")
    if settings.contact_source == "smtp_user":
        print(f"UWAGA: SKRAPER_CONTACT nie jest ustawiony — jako kontakt w User-Agent użyję {settings.contact} (SMTP_USER).",
              file=sys.stderr)
    return settings


def resolve_firms(store: Store, refs: list) -> list:
    ids = []
    for ref in refs:
        firm = store.find_firma(ref)
        if firm is None:
            raise InvalidFirma("brak_www", f"Nie znaleziono firmy: {ref}. Dodaj ją poleceniem: scrape.py dodaj URL")
        ids.append(firm["id"])
    return ids


def cmd_seed(args) -> int:
    _, store = open_store(args)
    result = store.seed_from_maile()
    print(f"Firm w bazie: {result['razem']} (nowych: {result['dodano']}, pominiętych wierszy maile: {result['pominieto']}).")
    for code, count in sorted(result["powody"].items()):
        print(f"  pominięto {count}× — {st.PRZYCZYNY.get(code, code)}")
    return 0


def cmd_dodaj(args) -> int:
    _, store = open_store(args)
    result = store.add_manual(args.url, args.nazwa)
    print(f"{'Dodano' if result['nowa'] else 'Już istnieje'}: {result['domena']} (id {result['id']}).")
    return 0


def cmd_status(args) -> int:
    _, store = open_store(args)
    overview = store.status_overview()
    print(f"Firmy: {overview['firmy']}   strony: {overview['stron']}   dane: {overview['bajty'] / 1_048_576:.1f} MB")
    for status in st.FIRMA_META:
        count = overview["firmy_wg_statusu"].get(status, 0)
        if count:
            print(f"  {st.FIRMA_META[status][0]:<22} {count}")
    if overview["kontakty"]:
        print("Kontakty: " + ", ".join(f"{typ}={n}" for typ, n in sorted(overview["kontakty"].items())))
    run = store.active_run()
    if run:
        stats = run.get("statystyki") or {}
        print(f"\nAktywny przebieg #{run['id']} (pid {run['pid']}, status {run['status']}), heartbeat {run['heartbeat_at']}; "
              f"strony={stats.get('strony', 0)}, aktywne domeny={stats.get('aktywne', 0)}, "
              f"strony/min={stats.get('strony_na_min', 0)}")
        if run.get("log_path"):
            print(f"Log: {run['log_path']}")
        if run["stop_requested"]:
            print("Poproszono o zatrzymanie — przebieg kończy bieżące żądania.")
    else:
        last = store.latest_run()
        if last:
            print(f"\nOstatni przebieg #{last['id']}: {last['status']} ({last['end_at'] or '—'}).")
    return 0


def cmd_stop(args) -> int:
    _, store = open_store(args)
    recovered = store.recover_dead_runs()
    if recovered:
        print(f"Przebieg {', '.join(map(str, recovered))} nie żył — oznaczono jako przerwany, firmy wróciły do kolejki.")
        return 0
    if store.request_stop():
        print("Poproszono aktywny przebieg o zatrzymanie. Skończy bieżące żądania (do ~30 s) i zapisze postęp.")
    else:
        print("Żaden przebieg nie jest aktywny.")
    return 0


def cmd_purge(args) -> int:
    _, store = open_store(args)
    if args.older_than < 0:
        raise SettingsError("--older-than musi być liczbą dni ≥ 0.")
    if args.html_only:
        print(f"Usunięto surowy HTML z {store.purge_html(args.older_than)} stron starszych niż {args.older_than} dni.")
    else:
        print(f"Usunięto dane skanu {store.purge_older_than(args.older_than)} firm zakończonych przed {args.older_than} dniami.")
    return 0


def cmd_run(args) -> int:
    config, store = open_store(args)
    recovered = store.recover_dead_runs()
    if recovered:
        print(f"Odzyskano przerwane przebiegi: {', '.join(map(str, recovered))} (firmy wróciły do kolejki).")

    if args.dry_run:
        settings = settings_from(args, config)
        ids = resolve_firms(store, args.firma) if args.firma else None
        queued = store.count_runnable(ids, None if ids is not None else tuple(args.statuses or st.FIRMA_DOMYSLNIE_DO_SKANU))
        print("PLAN (--dry-run, żadnych połączeń sieciowych, kolejka bez zmian):")
        for line in runner.plan_summary(settings, queued):
            print("  " + line)
        print(f"  Tożsamość bota: {settings.user_agent()}")
        return 0

    from perno.scraper.extract import bs4_available
    if not bs4_available():
        raise DbError("Brak pakietu beautifulsoup4. Uruchom: uv sync")

    if args.run_id:
        run = store.get_run(args.run_id)
        if run is None or not run["aktywny"]:
            raise DbError(f"Przebieg {args.run_id} nie istnieje albo już się zakończył.")
        stored = run["parametry"]
        settings = settings_from(args, config, stored.get("ustawienia"))
        run_id, firm_ids = args.run_id, stored.get("firma_ids")
        statuses = tuple(stored.get("statuses") or st.FIRMA_DOMYSLNIE_DO_SKANU)
    else:
        settings = settings_from(args, config)
        firm_ids = resolve_firms(store, args.firma) if args.firma else None
        statuses = tuple(args.statuses or st.FIRMA_DOMYSLNIE_DO_SKANU)
        run_id = store.start_run({"ustawienia": settings.to_dict(), "firma_ids": firm_ids, "statuses": list(statuses)})

    queued = store.enqueue(firm_ids, None if firm_ids is not None else statuses)
    limit = runner.raise_nofile_limit()
    path = runner.log_path_for(run_id) if not args.run_id else (store.get_run(run_id) or {}).get("log_path")
    log = runner.RunLogger(path if not args.run_id else None, echo=True)
    log(f"W kolejce: {queued} firm. Limit otwartych plików: {limit or 'nieznany'}.")
    stop_event = threading.Event()
    restore = runner.install_signal_handlers(stop_event, log)
    try:
        result = runner.execute_run(store, settings, run_id, only_ids=firm_ids, log=log,
                                    log_path=path, stop_event=stop_event)
    finally:
        restore()
        log.close()
    return 0 if result["status"] in (st.RUN_ZAKONCZONY, st.RUN_ZATRZYMANY) else 1


COMMANDS = {"seed": cmd_seed, "dodaj": cmd_dodaj, "run": cmd_run, "status": cmd_status, "stop": cmd_stop, "purge": cmd_purge}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except RunActive as exc:
        print(f"BŁĄD: {exc} (sprawdź: python3 scrape.py status)", file=sys.stderr)
        return 1
    except (ConfigError, DbError, SettingsError) as exc:
        print(f"BŁĄD: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
