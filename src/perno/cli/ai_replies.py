"""Polecenia `ai.py odpowiedzi …`: pobieranie odpowiedzi z poczty, ocena i szkice dla Claude Code, raport.

Nic tu nie wysyła maili ani nie zmienia statusów firm (odpowiedź wysyła człowiek w panelu; tego importu nie ma w tym pliku,
pilnuje go test). Pobieranie jest wyłącznie do odczytu, a polecenia zapisujące w bazie (`pobierz`, `ocen`, `szkic`) najpierw
robią kopię tabel w backup/. Wywoływane z `perno.cli.ai`."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional

from perno import settings as cfg
from perno.cli.entries import read_entries
from perno.cli.plan_output import plan_flags, print_plan, refused
from perno.db import Database, DbError
from perno.db.replies_store import ReplyStore
from perno.domain.reply_listing import ReplyRequestError
from perno.mailer.imap_client import ImapError
from perno.services import ai_replies as flow
from perno.services import firms_import, imap_access, replies_report, replies_sync
from perno.settings import load_config

ERRORS = (ImapError, ReplyRequestError)
DEFAULT_LIMIT = 10
DEFAULT_CHARS = 800
DEFAULT_BUDGET = 10000
RATING_CHOICES = ("positive", "neutral", "negative")


def add_parsers(sub) -> None:
    root = sub.add_parser("odpowiedzi", help="Odpowiedzi firm: pobieranie z poczty, ocena, szkice i raport.")
    actions = root.add_subparsers(dest="akcja", required=True)

    fetch = actions.add_parser("pobierz", help="Pobierz odpowiedzi ze wszystkich skrzynek z IMAP (tylko odczyt).")
    fetch.add_argument("--dry-run", action="store_true", help="Policz, co by się zapisało; nic nie zapisuje i nie zmienia statusów.")
    fetch.add_argument("--od", metavar="RRRR-MM-DD", help="Szukaj od tego dnia (tylko w folderach, które jeszcze nie były czytane).")
    fetch.add_argument("--skrzynka", metavar="ADRES", help="Tylko ta skrzynka.")

    actions.add_parser("doctor", help="Sprawdź połączenie IMAP każdej skrzynki (bez zmian na serwerze).")
    actions.add_parser("stan", help="Liczniki odpowiedzi, stan skrzynek i ostatniego pobierania.")

    brief = actions.add_parser("brief", help="Skrót nieocenionych odpowiedzi człowieka (treść obca w ogrodzeniu, bez adresów).")
    brief.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Ile wątków (domyślnie {DEFAULT_LIMIT}).")
    brief.add_argument("--po", default="", metavar="ID", help="Następna paczka: wątki z id większym niż to.")
    brief.add_argument("--id", nargs="*", default=[], help="Tylko te wątki (id maila kampanii), także już ocenione.")
    brief.add_argument("--ocena", choices=(*RATING_CHOICES, "brak"), default="brak",
                       help="Które wątki: bez oceny (domyślnie) albo z tą oceną, np. positive do pisania szkiców.")
    brief.add_argument("--chars", type=int, default=DEFAULT_CHARS, help="Znaków treści na wątek.")
    brief.add_argument("--budzet", type=int, default=DEFAULT_BUDGET, help="Znaków na całą paczkę.")

    rate = actions.add_parser("ocen", help="Zapisz oceny AI (plik JSON {id, rating, note?, optout?} albo --id i --ocena).")
    rate.add_argument("json", nargs="?", help="Plik z listą ocen (id i etykiety; nigdy treść odpowiedzi).")
    rate.add_argument("--id", help="Jeden wątek zamiast pliku.")
    rate.add_argument("--ocena", choices=RATING_CHOICES, help="Ocena dla --id.")
    rate.add_argument("--notatka", default="", help="Krótkie uzasadnienie dla --id (do 200 znaków).")
    rate.add_argument("--wypisanie", action="store_true", help="Oznacz wątek jako prośbę o brak kontaktu (dla --id).")
    plan_flags(rate)

    draft = actions.add_parser("szkic", help="Zapisz szkice odpowiedzi do zatwierdzenia w panelu (plik JSON {id, body}). Nic nie wysyła.")
    draft.add_argument("json", help="Plik z listą szkiców.")
    plan_flags(draft)

    report = actions.add_parser("raport", help="Raport statystyk odpowiedzi (Markdown).")
    report.add_argument("--od", metavar="RRRR-MM-DD", help="Maile wysłane od tego dnia.")
    report.add_argument("--do", metavar="RRRR-MM-DD", help="Maile wysłane do tego dnia (włącznie).")
    report.add_argument("--segment", action="append", default=[], help="Tylko ten typ firm (można powtórzyć).")
    report.add_argument("--zapisz", action="store_true", help="Zapisz też do raporty/odpowiedzi_RRRRMMDD.md (poza gitem).")


def imap_factory():
    """Fabryka sesji IMAP (prawdziwa; baza demo ją blokuje). Testy podmieniają tę funkcję."""
    return imap_access.real_factory()


def open_replies(args) -> tuple:
    config = load_config(args.config)
    db = Database.from_config(config)
    if not db.check_connection()["table_exists"]:
        raise DbError("Tabela maile nie istnieje. Uruchom migrate_csv_to_sql.py (lub: python3 -m perno.db --init).")
    store = ReplyStore(db)
    store.ensure_schema()
    return config, db, store


def _day(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"--od: oczekiwano daty RRRR-MM-DD, jest „{raw}”.") from None


def _mailbox_line(box: replies_sync.MailboxReport, dry: bool) -> str:
    if not box.ok:
        return f"  {box.mailbox}: BŁĄD ({box.kind}): {box.error}"
    kinds = ", ".join(f"{name} {count}" for name, count in sorted(box.kinds.items())) or "nic nowego dla nas"
    folders = ", ".join(f"{item['folder']} {item['seen']}" for item in box.folders)
    saved = "do zapisu" if dry else "zapisano"
    extra = " (przerwane: limit czasu, reszta w następnym przebiegu)" if box.postponed else ""
    return (f"  {box.mailbox}: OK, nowych {box.seen} ({folders}); nasze: {kinds}; {saved} {box.stored}"
            f"{f', już były {box.duplicates}' if box.duplicates else ''}{extra}")


def report_lines(report: replies_sync.SyncReport) -> list:
    """Podsumowanie przebiegu w kilku liniach: bez adresów osób trzecich i bez treści wiadomości."""
    if report.skipped:
        return [f"Pobieranie pominięto: {report.skipped}."]
    totals = report.totals()
    head = "Pobieranie odpowiedzi" + (" (PRÓBA: nic nie zapisano)" if report.dry_run else "")
    lines = [f"{head}: skrzynek {totals['mailboxes']}, nowych wiadomości {totals['seen']}, dotyczą nas {totals['candidates']}."]
    lines += [_mailbox_line(box, report.dry_run) for box in report.mailboxes]
    if not report.dry_run:
        lines.append(f"Statusy maili: „odpowiedział” {report.replied}, „zwrot” {report.bounced}.")
    if totals["unassigned"]:
        lines.append(f"Nieprzypisane: {totals['unassigned']} (temat naszej kampanii, nie wiadomo od kogo; przypisz w panelu).")
    return lines


def cmd_fetch(args) -> int:
    config, db, store = open_replies(args)
    options = replies_sync.options_from_config(config, since=_day(args.od), only=args.skrzynka or "", dry_run=args.dry_run)
    if not args.dry_run:
        backup = firms_import.backup_table(db, cfg.BACKUP_DIR, "pobraniem-odpowiedzi", replies=True)
        print(f"Kopia zapasowa: {backup}")
    report = replies_sync.fetch_replies(db, store, config, imap_factory(), options=options)
    print("\n".join(report_lines(report)))
    return 1 if any(not box.ok for box in report.mailboxes) else 0


def cmd_doctor(args) -> int:
    config = load_config(args.config)
    result = imap_access.test_imap(config, imap_factory)
    for item in result["results"]:
        print(f"  {item['address']}: {'OK' if item['ok'] else 'BŁĄD'}: {item['message']}")
    if not result["results"]:
        print(result["message"])
    without = [box.user for box in cfg.configured_mailboxes(config) if box.slot not in {slot.slot for slot in _imap_slots(config)}]
    if without:
        print(f"Skrzynki bez IMAP (ich odpowiedzi nie są pobierane): {len(without)}" + (f", np. {', '.join(without[:3])}" if without else ""))
    return 0 if result["ok"] else 1


def _imap_slots(config) -> list:
    try:
        return cfg.imap_slots(config)
    except cfg.ConfigError:
        return []


def cmd_state(args) -> int:
    config, _, store = open_replies(args)
    counts = store.counts()
    print(f"Wątki z odpowiedzią człowieka: {counts['threads']} (czeka na odpowiedź {counts['awaiting_reply']}, "
          f"nieprzeczytane {counts['unread']}, nieocenione {counts['unrated']}).")
    print(f"Zwroty: {counts['bounces']}, autoodpowiedzi: {counts['auto']}, nieprzypisane: {counts['unassigned']}, wiadomości razem: {counts['messages']}.")
    states = {item["mailbox"]: item for item in store.mailbox_states()}
    receiving = cfg.imap_slot_numbers(config)
    total = len(cfg.configured_mailboxes(config))
    print(f"Odbiór poczty: {len(receiving)} z {total} skrzynek ma ustawiony IMAP.")
    for mailbox, state in sorted(states.items()):
        when = state["last_fetch_at"].strftime("%Y-%m-%d %H:%M") if state["last_fetch_at"] else "nigdy"
        print(f"  {mailbox}: ostatnio {when}" + (f", BŁĄD ({state['kind']}): {state['error']}" if state["error"] else ""))
    last = store.last_run()
    print("Pobieranie trwa teraz." if last["running"] else
          f"Ostatni przebieg: {last['finished_at'].strftime('%Y-%m-%d %H:%M') if last['finished_at'] else 'nigdy'}.")
    return 0


def cmd_brief(args) -> int:
    _, _, store = open_replies(args)
    rating = None if args.ocena == "brak" else args.ocena
    batch = flow.build_briefs(store, limit=args.limit, after=args.po, ids=args.id, chars=args.chars, budget=args.budzet, rating=rating)
    print(f"# wątków {'bez oceny' if rating is None else 'z oceną ' + rating}: {batch.pending}, w tej paczce: {len(batch.texts)}"
          + (f", następna paczka: --po {batch.next_after}" if batch.next_after else ", to ostatnia paczka"))
    print("\n\n".join(batch.texts))
    return 0


def _entries(args) -> list:
    """Wpisy do oceny: z pliku albo jeden z `--id`/`--ocena` (oba naraz albo żadne to błąd użycia)."""
    if bool(args.json) == bool(args.id):
        raise ValueError("Podaj plik JSON albo --id z --ocena (nie oba naraz).")
    if args.json:
        return read_entries(args.json)
    entry = {"id": args.id, "rating": args.ocena, "note": args.notatka}
    return [{**entry, "optout": True}] if args.wypisanie else [entry]


def cmd_rate(args) -> int:
    _, db, store = open_replies(args)
    plan = flow.plan_ratings(store, _entries(args))
    print_plan(plan, {item["id"]: f"{item['rating']}{'  [wypisanie]' if item['optout'] else ''}" for item in plan.items})
    if refused(plan, args):
        return 1
    if args.dry_run:
        print(f"--dry-run: {len(plan.items)} ocen gotowych do zapisu, nic nie zapisano.")
        return 0
    done, backup = flow.write_ratings(db, store, plan, db.now(), cfg.BACKUP_DIR)
    print(f"Zapisano {done} z {len(plan.items)} ocen (jako oceny AI do przejrzenia w panelu)." + (f" Kopia: {backup}" if backup else ""))
    return 1 if plan.errors else 0


def cmd_draft(args) -> int:
    config, db, store = open_replies(args)
    plan = flow.plan_drafts(store, config, read_entries(args.json))
    print_plan(plan, {item["id"]: f"{len(item['body'])} znaków" for item in plan.items})
    if refused(plan, args):
        return 1
    if args.dry_run:
        print(f"--dry-run: {len(plan.items)} szkiców gotowych do zapisu, nic nie zapisano.")
        return 0
    saved, skipped, backup = flow.write_drafts(db, store, config, plan, db.now(), cfg.BACKUP_DIR)
    for thread_id in skipped:
        print(f"  UWAGA {thread_id}: jest szkic człowieka albo wysyłka w toku, pominięto.")
    print(f"Zapisano {saved} z {len(plan.items)} szkiców. Wysyła człowiek z panelu (zakładka Odpowiedzi)." + (f" Kopia: {backup}" if backup else ""))
    return 1 if plan.errors else 0


def cmd_report(args) -> int:
    config, db, store = open_replies(args)
    payload = {"from": args.od, "to": args.do, "segments": args.segment}
    text = replies_report.markdown(replies_report.build_report(store, config, payload, db.now()))
    print(text)
    if args.zapisz:
        path = flow.report_path(cfg.BASE_DIR, db.now())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"\nZapisano: {path}")
    return 0


def cmd_replies(args) -> int:
    return {"pobierz": cmd_fetch, "doctor": cmd_doctor, "stan": cmd_state, "brief": cmd_brief, "ocen": cmd_rate,
            "szkic": cmd_draft, "raport": cmd_report}[args.akcja](args)


COMMANDS = {"odpowiedzi": cmd_replies}

