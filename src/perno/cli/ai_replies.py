"""Polecenia `ai.py odpowiedzi …`: pobieranie odpowiedzi z poczty, kontrola połączeń i stan modułu.

Nic tu nie wysyła maili ani nie zmienia treści wiadomości. Pobieranie jest wyłącznie do odczytu, a polecenia, które coś
zapisują w bazie (`pobierz`), najpierw robią kopię tabel w backup/. Wywoływane z `perno.cli.ai`."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from perno import settings as cfg
from perno.db import Database, DbError
from perno.db.replies_store import ReplyStore
from perno.mailer.imap_client import ImapError
from perno.services import firms_import, imap_access, replies_sync
from perno.settings import load_config

ERRORS = (ImapError,)


def add_parsers(sub) -> None:
    root = sub.add_parser("odpowiedzi", help="Odpowiedzi firm: pobieranie z poczty, stan, (dalej) ocena i raport.")
    actions = root.add_subparsers(dest="akcja", required=True)

    fetch = actions.add_parser("pobierz", help="Pobierz odpowiedzi ze wszystkich skrzynek z IMAP (tylko odczyt).")
    fetch.add_argument("--dry-run", action="store_true", help="Policz, co by się zapisało; nic nie zapisuje i nie zmienia statusów.")
    fetch.add_argument("--od", metavar="RRRR-MM-DD", help="Szukaj od tego dnia (tylko w folderach, które jeszcze nie były czytane).")
    fetch.add_argument("--skrzynka", metavar="ADRES", help="Tylko ta skrzynka.")

    actions.add_parser("doctor", help="Sprawdź połączenie IMAP każdej skrzynki (bez zmian na serwerze).")
    actions.add_parser("stan", help="Liczniki odpowiedzi, stan skrzynek i ostatniego pobierania.")


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
    settings = cfg.ReplySettings.from_config(config)
    options = replies_sync.SyncOptions(ignore=settings.ignore, folders=settings.folders, since=_day(args.od), workers=settings.workers,
                                       only=args.skrzynka or "", dry_run=args.dry_run)
    if not args.dry_run:
        backup = firms_import.backup_table(db, cfg.BACKUP_DIR, "przed-pobraniem-odpowiedzi", replies=True)
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


def cmd_replies(args) -> int:
    return {"pobierz": cmd_fetch, "doctor": cmd_doctor, "stan": cmd_state}[args.akcja](args)


COMMANDS = {"odpowiedzi": cmd_replies}

