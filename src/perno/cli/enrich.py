#!/usr/bin/env python3
"""Uzupełnia tabelę `maile` (e-mail, temat, treść) firmami z zakończonym skanem stron.

  python3 wypelnij.py lista                       # które firmy się kwalifikują (skan zakończony, brak maila w tabeli)
  python3 wypelnij.py kontekst --out DIR          # po jednym pliku .md na firmę: opis, kontakty, treść stron
  python3 wypelnij.py zapisz tresci.json --dry-run   # walidacja i podgląd, nic nie zapisuje
  python3 wypelnij.py zapisz tresci.json
  python3 wypelnij.py zapisz tresci.json --takze-przerwane   # także skan przerwany przez serwer (≥10 stron)
  python3 wypelnij.py zapisz tresci.json --ponownie   # przelicz też wiersze „Do wysłania”, których jeszcze nie wysłano

`tresci.json` to lista obiektów: {id, email, subject, mode, tag, obs, pitch, channel_summary}
(+ opcjonalnie contact_person, city, address, description). Pola obs/pitch/channel_summary/subject muszą być
w języku kraju siedziby firmy (PL→polski, GB/IE→angielski, DE/AT→niemiecki, ES→hiszpański); szablon wybiera się
z kraju zapisanego w tabeli. channel_summary to czynność w bezokoliczniku z dopełniaczem (po „Nie musi już Pan…”: „potwierdzać rezerwacji”,
nie „rezerwacje”) i BEZ płatności (szablon sam dopisuje „i osobno rozliczać płatności”), a pitch kończy się bezokolicznikiem („…wprowadzić płatność online”). Adres e-mail musi pochodzić z kontaktów znalezionych przez skraper (nie wymyślamy adresów).
Wiersze wysłane / odpowiedziane / „Nie kontaktować” / już klient nie są ruszane. Przed zapisem powstaje kopia w backup/.
Firma po uzupełnieniu dostaje status „Do wysłania”; wysyłka nadal wymaga Twojej decyzji (panel albo send_emails.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from perno import settings as cfg
from perno.db import Database, DbError
from perno.domain import localize
from perno.scraper.store import Store
from perno.services import enrichment, firms_import
from perno.settings import load_config


def open_stores(args) -> tuple:
    """(baza maili, magazyn skrapera na tym samym silniku); bez zakładania tabel skrapera."""
    db = Database.from_config(load_config(args.config))
    return db, Store(db)


def cmd_lista(args) -> int:
    _, store = open_stores(args)
    rows = enrichment.list_candidates(store)
    print(f"Kwalifikuje się {len(rows)} firm (skan zakończony, w tabeli maile bez treści):")
    for r in rows:
        lang = localize.language_for(r["country"])
        emails = len(store.scraped_emails(r["firma_id"]))
        print(f"  {r['id']:<14} {r['company'][:34]:<34} kraj={r['country'] or '—':<3} język={lang or 'BRAK'}  e-maile ze skanu={emails}  stron={r['stron_pobranych']}")
    return 0


def cmd_kontekst(args) -> int:
    _, store = open_stores(args)
    os.makedirs(args.out, exist_ok=True)
    rows = enrichment.list_candidates(store)
    if args.id:
        rows = [r for r in rows if r["id"] in args.id]
    for r in rows:
        with open(os.path.join(args.out, f"{r['id']}.md"), "w", encoding="utf-8") as fh:
            fh.write(enrichment.build_context(store, r, args.chars))
    print(f"Zapisano {len(rows)} plików w {args.out}")
    return 0


def cmd_zapisz(args) -> int:
    with open(args.json, encoding="utf-8") as fh:
        entries = json.load(fh)
    db, store = open_stores(args)
    config = load_config(args.config)
    plan = enrichment.plan_updates(store, entries, config.get("SENDER_NAME", ""), args.ponownie, args.takze_przerwane)
    for item in plan.items:
        print(f"  OK  {item.row_id:<14} {item.company[:30]:<30} [{item.language}] {item.values['email']:<34} „{item.values['subject']}”")
    for message in plan.errors:
        print(f"  BŁĄD {message}", file=sys.stderr)
    if plan.errors:
        print(f"Przerwano: {len(plan.errors)} błędów, nic nie zapisano (popraw plik i uruchom ponownie).", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"--dry-run: {len(plan.items)} wierszy gotowych do zapisu, nic nie zapisano.")
        return 0
    path = firms_import.backup_table(db, cfg.BACKUP_DIR, "uzupelnieniem-ze-skanu")
    print(f"Kopia zapasowa bazy: {path}")
    written = enrichment.write_updates(db, plan, args.ponownie)
    print(f"Zapisano {written} z {len(plan.items)} wierszy (status „Do wysłania”).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=cfg.DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("lista")
    ctx = sub.add_parser("kontekst")
    ctx.add_argument("--out", required=True)
    ctx.add_argument("--id", nargs="*")
    ctx.add_argument("--chars", type=int, default=enrichment.DEFAULT_CONTEXT_CHARS, help="Limit treści stron na firmę (znaki).")
    save = sub.add_parser("zapisz")
    save.add_argument("json")
    save.add_argument("--dry-run", action="store_true")
    save.add_argument("--takze-przerwane", action="store_true",
                      help="Uwzględnij firmy, których skan przerwał serwer (429/503), jeśli pobrano ≥10 stron. Serwerów, które odmówiły dostępu, nie ponawiamy.")
    save.add_argument("--ponownie", action="store_true", help="Przelicz też wiersze „Do wysłania” (nigdy niewysłane).")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {"lista": cmd_lista, "kontekst": cmd_kontekst, "zapisz": cmd_zapisz}[args.command](args)
    except DbError as exc:
        print(f"Błąd bazy: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
