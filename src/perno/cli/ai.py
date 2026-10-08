#!/usr/bin/env python3
"""Narzędzie dla autora treści (Claude Code): krótkie, tanie w odczycie polecenia do dodawania firm i pisania maili.

  python3 ai.py szablony [--opis NAZWA]            # szablony z templates/: języki, pola do napisania, zasady
  python3 ai.py znane [--segment TYP]              # domeny firm, które już mamy (żeby ich nie dodawać drugi raz)
  python3 ai.py dodaj plik.csv --segment TYP [--dry-run] [--skan]   # import firm + kolejka skrapera (+ start skanu)
  python3 ai.py status [--segment TYP]             # skan i maile wg typu firm, ile firm czeka na analizę
  python3 ai.py brief [--segment TYP] [--limit 10] [--po ID]        # skrót firm gotowych do napisania maila
  python3 ai.py zapisz maile.json [--dry-run] [--czesciowo]         # waliduje i zapisuje maile do kolejki wysyłki
  python3 ai.py pomin ID... --powod "..."          # firma nie pasuje: wychodzi z kolejki treści
  python3 ai.py skan stop|wznow                    # zatrzymanie / wznowienie skanu stron

Skan jednorazowy bez bazy (pliki w data/skrap/<nazwa>/, nic nie trafia do tabel firm ani maili):
  python3 ai.py zeskrapuj ADRES... [--plik f] [--nazwa N] [--max-stron 25]   # strony do CSV + tekstu
  python3 ai.py szukaj NAZWA FRAZA... [--domeny] [--csv]                    # fragmenty wokół fraz / trafienia na domenę
  python3 ai.py wyciagnij NAZWA --pole nip --ma "rezerwacje=rezerwacja|booking" # tabela CSV z polami
  python3 ai.py zbiory                                                       # zbiory zeskrapowane wcześniej

Odpowiedzi firm (pobieranie z poczty, tylko odczyt):
  python3 ai.py odpowiedzi pobierz [--dry-run] [--od RRRR-MM-DD] [--skrzynka ADRES]   # pobierz odpowiedzi ze skrzynek z IMAP
  python3 ai.py odpowiedzi doctor                                                     # sprawdź połączenia IMAP
  python3 ai.py odpowiedzi stan                                                       # liczniki i stan skrzynek

Nic tu nie wysyła maili: zapisane wiersze mają status „Do wysłania”, a wysyłkę zatwierdza człowiek (panel / Automat).
Każde polecenie zmieniające bazę najpierw zapisuje jej kopię w backup/.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from perno import settings as cfg
from perno.cli import ai_replies, ai_scrape
from perno.cli import scrape as scrape_cli
from perno.cli.firms import segment_arg
from perno.db import Database, DbError
from perno.domain import statuses as st
from perno.scraper import statuses as sst
from perno.scraper.settings import SettingsError
from perno.scraper.store import RunActive, Store
from perno.services import ai_workflow as flow
from perno.services import firms_import, scan_control, template_catalog
from perno.settings import ConfigError, load_config

DEFAULT_LIMIT = 10
DEFAULT_CHARS = 1500
DEFAULT_BUDGET = 12000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=cfg.DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)

    templates = sub.add_parser("szablony", help="Szablony z templates/ i pola do napisania.")
    templates.add_argument("--opis", metavar="NAZWA", help="Wypisz zasady pisania pól tego szablonu.")

    known = sub.add_parser("znane", help="Domeny firm już w bazie.")
    known.add_argument("--segment", type=segment_arg)

    add = sub.add_parser("dodaj", help="Dodaj firmy z CSV (company_display_name, website, what_they_sell, country, city).")
    add.add_argument("csv")
    add.add_argument("--segment", required=True, type=segment_arg, help="Typ firm, np. kancelarie.")
    add.add_argument("--dry-run", action="store_true", help="Pokaż, co by się stało; nic nie zapisuje.")
    add.add_argument("--skan", action="store_true", help="Po dodaniu uruchom skan nowych firm (profil kontekst, mały ruch).")

    status = sub.add_parser("status", help="Przegląd skanu i maili wg typu firm.")
    status.add_argument("--segment", type=segment_arg)

    show = sub.add_parser("brief", help="Skrót firm gotowych do napisania maila (po skanie).")
    show.add_argument("--segment", type=segment_arg)
    show.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Ile firm (domyślnie {DEFAULT_LIMIT}).")
    show.add_argument("--po", default="", metavar="ID", help="Następna paczka: firmy z id większym niż to.")
    show.add_argument("--id", nargs="*", default=[], help="Tylko te wiersze.")
    show.add_argument("--chars", type=int, default=DEFAULT_CHARS, help="Znaków na firmę.")
    show.add_argument("--budzet", type=int, default=DEFAULT_BUDGET, help="Znaków na całą paczkę.")
    show.add_argument("--takze-przerwane", action="store_true", help="Także skan przerwany przez serwer (≥10 stron).")

    save = sub.add_parser("zapisz", help="Zapisz maile z pliku JSON do kolejki wysyłki.")
    save.add_argument("json")
    save.add_argument("--dry-run", action="store_true")
    save.add_argument("--czesciowo", action="store_true", help="Zapisz poprawne wpisy, wypisz odrzucone (domyślnie wszystko albo nic).")
    save.add_argument("--ponownie", action="store_true", help="Przelicz też wiersze „Do wysłania” (nigdy niewysłane).")
    save.add_argument("--takze-przerwane", action="store_true")

    skip = sub.add_parser("pomin", help="Oznacz firmy jako „Nie pasuje”.")
    skip.add_argument("ids", nargs="+", metavar="ID")
    skip.add_argument("--powod", required=True)

    scan = sub.add_parser("skan", help="Zatrzymaj albo wznów skan stron.")
    scan.add_argument("akcja", choices=("stop", "wznow"))
    ai_scrape.add_parsers(sub)
    ai_replies.add_parsers(sub)
    return parser


def open_stores(args) -> tuple:
    config = load_config(args.config)
    db = Database.from_config(config)
    if not db.check_connection()["table_exists"]:
        raise DbError("Tabela maile nie istnieje. Uruchom migrate_csv_to_sql.py (lub: python3 -m perno.db --init).")
    store = Store(db)
    store.ensure_schema()
    return config, db, store


def cmd_templates(args) -> int:
    if args.opis:
        guide = template_catalog.read_guide(args.opis)
        print(guide or f"Szablon „{args.opis}” nie ma pliku z zasadami (templates/{args.opis}.md).")
        return 0
    for info in template_catalog.discover():
        fields = ", ".join([*info.fields, *(f"[{name}]" for name in info.optional)])
        mark = "" if info.writable else "  (nie do zapisu przez ai.py zapisz)"
        print(f"{info.name}: języki {','.join(info.languages)} | pola: {fields}{mark}")
        print(f"  zasady: ai.py szablony --opis {info.name}" if info.guide else "  zasad pisania brak")
    return 0


def cmd_known(args) -> int:
    _, db, _ = open_stores(args)
    domains = flow.known_domains(db, args.segment)
    print(f"{len(domains)} domen" + (f" w segmencie {args.segment}" if args.segment else ""))
    print("\n".join(domains))
    return 0


def print_add_report(report: flow.AddReport) -> None:
    mode = "DRY-RUN" if report.dry_run else "DODANO"
    print(f"{mode} segment={report.segment}: nowych {len(report.new)}, już w bazie {len(report.skipped_existing)}, "
          f"duplikatów w pliku {len(report.duplicates_in_file)}")
    for item in report.new:
        print(f"  + {item['id']:<22} {item['company'][:40]:<40} {item['domain']}")
    for company, existing in report.skipped_existing:
        print(f"  = {company[:40]:<40} już jako {existing}")
    for line in report.scan_plan:
        print("  plan skanu: " + line)
    if report.backup:
        print(f"Kopia zapasowa: {report.backup}")
    if report.scan:
        print(f"Skan uruchomiony: przebieg #{report.scan.run_id}, firm w kolejce {report.scan.queued}. "
              "Postęp: python3 ai.py status")


def cmd_add(args) -> int:
    config, db, store = open_stores(args)
    scan = flow.ScanRequest(config, args.config) if args.skan else None
    report = flow.add_firms(db, store, args.csv, args.segment, dry_run=args.dry_run, scan=scan)
    print_add_report(report)
    return 0


def cmd_status(args) -> int:
    _, db, store = open_stores(args)
    print("\n".join(flow.status_lines(db, store, args.segment)))
    return 0


def cmd_brief(args) -> int:
    _, _, store = open_stores(args)
    batch = flow.build_briefs(store, segment=args.segment, limit=args.limit, after=args.po, ids=args.id,
                              chars=args.chars, budget=args.budzet, include_partial=args.takze_przerwane)
    print(f"# kandydatów: {batch.total}, w tej paczce: {len(batch.texts)}"
          + (f", następna paczka: --po {batch.next_after}" if batch.next_after else ", to ostatnia paczka"))
    print("\n\n".join(batch.texts))
    return 0


def read_entries(path: str) -> list:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise DbError("Plik musi zawierać listę obiektów (jeden na firmę).")
    return data


def cmd_save(args) -> int:
    config, db, store = open_stores(args)
    plan = flow.plan_entries(store, read_entries(args.json), config.get("SENDER_NAME", ""), include_pending=args.ponownie)
    for item in plan.items:
        print(f"  OK  {item.row_id:<22} {item.company[:28]:<28} [{item.language}] {item.values['email']:<32} „{item.values['subject']}”")
    for message in plan.warnings:
        print(f"  UWAGA {message}")
    for message in plan.errors:
        print(f"  BŁĄD {message}", file=sys.stderr)
    if plan.errors and not args.czesciowo:
        print(f"Przerwano: {len(plan.errors)} błędów, nic nie zapisano (popraw plik albo użyj --czesciowo).", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"--dry-run: {len(plan.items)} wierszy gotowych do zapisu, nic nie zapisano.")
        return 0
    written, backup = flow.write_plan(db, plan, include_pending=args.ponownie)
    print(f"Zapisano {written} z {len(plan.items)} wierszy (status „Do wysłania”)." + (f" Kopia: {backup}" if backup else ""))
    return 1 if plan.errors else 0


def cmd_skip(args) -> int:
    _, db, _ = open_stores(args)
    done, errors, backup = flow.skip_rows(db, args.ids, args.powod)
    for row_id in done:
        print(f"  {row_id}: {st.STATUS_META[st.STATUS_SKIPPED_FIT][0]}")
    for message in errors:
        print(f"  BŁĄD {message}", file=sys.stderr)
    print(f"Oznaczono {len(done)} z {len(args.ids)}." + (f" Kopia: {backup}" if backup else ""))
    return 1 if errors else 0


def cmd_scan(args) -> int:
    if args.akcja == "stop":
        return scrape_cli.main(["--config", args.config, "stop"])
    config, _, store = open_stores(args)
    scan_control.ensure_parser_available()
    settings = scan_control.settings_for_run(config, {"profile": flow.SCAN_PROFILE})
    started = scan_control.start_scan(store, settings, firma_ids=None, statuses=sst.FIRMA_DOMYSLNIE_DO_SKANU,
                                      config_path=args.config)
    print(f"Skan wznowiony: przebieg #{started.run_id}, firm w kolejce {started.queued}.")
    return 0


COMMANDS = {"szablony": cmd_templates, "znane": cmd_known, "dodaj": cmd_add, "status": cmd_status, "brief": cmd_brief,
            "zapisz": cmd_save, "pomin": cmd_skip, "skan": cmd_scan, **ai_scrape.COMMANDS, **ai_replies.COMMANDS}


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (DbError, ConfigError, SettingsError, firms_import.FirmsError, scan_control.ScanRequestError, RunActive,
            template_catalog.UnknownTemplate, OSError, ValueError, *ai_scrape.ERRORS, *ai_replies.ERRORS) as exc:
        print(f"BŁĄD: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
