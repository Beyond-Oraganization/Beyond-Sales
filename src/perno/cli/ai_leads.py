"""Polecenia `ai.py leady …`: skrót leadów do uzupełnienia, zapis pól wpisanych przez AI i liczniki.

Lead to firma z odpowiedzią ocenioną jako pozytywna albo neutralna. AI uzupełnia jego kontakt, telefon, e-mail i opis działalności;
nic tu nie wysyła maili, nie zmienia statusów firm ani `podmioty` (pilnuje tego test), a pola wpisane przez człowieka są nietykalne.
Skrót zawiera prawdziwe kontakty z odpowiedzi firmy, ale polecenia **nigdy nie wypisują wpisanych wartości** (tylko id i nazwy pól).
Polecenie zapisujące najpierw robi kopię tabel w backup/. Wywoływane z `perno.cli.ai`."""

from __future__ import annotations

from perno import settings as cfg
from perno.cli.entries import read_entries
from perno.cli.plan_output import plan_flags, print_plan, refused
from perno.db import Database, DbError
from perno.db.leads_store import LeadStore
from perno.domain.reply_listing import ReplyRequestError
from perno.scraper.store import Store
from perno.services import ai_leads as flow
from perno.settings import load_config

ERRORS = (ReplyRequestError,)
DEFAULT_LIMIT = 10
DEFAULT_CHARS = 900
DEFAULT_BUDGET = 12000


def add_parsers(sub) -> None:
    root = sub.add_parser("leady", help="Leady (firmy z odpowiedzią pozytywną lub neutralną): skrót do uzupełnienia, zapis pól, liczniki.")
    actions = root.add_subparsers(dest="akcja", required=True)

    actions.add_parser("stan", help="Liczniki leadów: aktywne, kompletne, do uzupełnienia, wpisy AI do zatwierdzenia, wycofane (bez danych osobowych).")

    brief = actions.add_parser("brief", help="Skrót leadów do uzupełnienia: pola, odpowiedzi z podpisem i dane ze strony (treść obca w ogrodzeniu).")
    brief.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Ile leadów (domyślnie {DEFAULT_LIMIT}).")
    brief.add_argument("--po", default="", metavar="ID", help="Następna paczka: leady z id większym niż to.")
    brief.add_argument("--id", nargs="*", default=[], help="Tylko te leady (id firmy albo jej wątku z oceną), także kompletne.")
    brief.add_argument("--wszystkie", action="store_true", help="Także kompletne leady (domyślnie tylko te, którym czegoś brakuje).")
    brief.add_argument("--chars", type=int, default=DEFAULT_CHARS, help="Znaków treści na odpowiedź firmy (początek i koniec z podpisem).")
    brief.add_argument("--budzet", type=int, default=DEFAULT_BUDGET, help="Znaków na całą paczkę.")

    save = actions.add_parser("zapisz", help="Zapisz pola leadów (plik JSON {id, contact?, phone?, email?, about?} albo --id i pola).")
    save.add_argument("json", nargs="?", help="Plik z listą wpisów (w raporty/, poza gitem: zawiera dane kontaktowe).")
    save.add_argument("--id", help="Jeden lead zamiast pliku (id firmy albo jej wątku).")
    save.add_argument("--kontakt", help="Osoba kontaktowa (imię, nazwisko, stanowisko) dla --id.")
    save.add_argument("--telefon", help="Telefon dla --id (musi występować w wątku albo na stronie firmy).")
    save.add_argument("--email", help="E-mail dla --id (musi występować w wątku albo na stronie firmy).")
    save.add_argument("--opis", help="Czym firma się zajmuje (1-2 zdania) dla --id.")
    plan_flags(save)


def open_leads(args) -> tuple:
    """`(db, magazyn leadów, magazyn skrapera)`: oba magazyny z gotowym schematem, tak jak w panelu."""
    config = load_config(args.config)
    db = Database.from_config(config)
    if not db.check_connection()["table_exists"]:
        raise DbError("Tabela maile nie istnieje. Uruchom migrate_csv_to_sql.py (lub: python3 -m perno.db --init).")
    leads, scan = LeadStore(db), Store(db)
    leads.ensure_schema()
    scan.ensure_schema()
    return db, leads, scan


def cmd_state(args) -> int:
    db, leads, _ = open_leads(args)
    counts = flow.stats(leads, db.now())
    print(f"Leady aktywne: {counts['all']} (kompletne {counts['all'] - counts['missing']}, do uzupełnienia {counts['missing']}, "
          f"wpisy AI do zatwierdzenia w {counts['ai']}).")
    print(f"Wycofane (ocena zmieniona na negatywną, dane zachowane): {counts['withdrawn']}.")
    return 0


def cmd_brief(args) -> int:
    db, leads, scan = open_leads(args)
    options = flow.BriefOptions(limit=args.limit, after=args.po, ids=args.id, only_missing=not args.wszystkie, chars=args.chars, budget=args.budzet)
    batch = flow.build_briefs(leads, scan, options, db.now())
    what = "leadów" if args.wszystkie or args.id else "leadów do uzupełnienia"
    print(f"# {what}: {batch.pending}, w tej paczce: {len(batch.texts)}"
          + (f", następna paczka: --po {batch.next_after}" if batch.next_after else ", to ostatnia paczka"))
    print("\n\n".join(batch.texts))
    return 0


def _entries(args) -> list:
    """Wpisy do zapisu: z pliku albo jeden z `--id` i pól (oba naraz albo żadne to błąd użycia)."""
    if bool(args.json) == bool(args.id):
        raise ValueError("Podaj plik JSON albo --id z polami (--kontakt, --telefon, --email, --opis), nie oba naraz.")
    if args.json:
        return read_entries(args.json)
    fields = {"contact": args.kontakt, "phone": args.telefon, "email": args.email, "about": args.opis}
    return [{"id": args.id, **{name: value for name, value in fields.items() if value}}]


def cmd_save(args) -> int:
    db, leads, scan = open_leads(args)
    plan = flow.plan_leads(leads, scan, _entries(args), db.now())
    print_plan(plan, {item["id"]: "pola: " + ", ".join(item["changes"]) for item in plan.items})
    if refused(plan, args):
        return 1
    if args.dry_run:
        print(f"--dry-run: {len(plan.items)} leadów gotowych do zapisu, nic nie zapisano.")
        return 0
    done, skipped, backup = flow.write_leads(db, leads, plan, db.now(), cfg.BACKUP_DIR)
    for item_id, name in skipped:
        print(f"  UWAGA {item_id}: pole {name} wpisał człowiek w międzyczasie, pominięto.")
    print(f"Zapisano {done} z {len(plan.items)} leadów (jako wpisy AI do zatwierdzenia w panelu, zakładka Leady)."
          + (f" Kopia: {backup}" if backup else ""))
    return 1 if plan.errors else 0


def cmd_leads(args) -> int:
    return {"stan": cmd_state, "brief": cmd_brief, "zapisz": cmd_save}[args.akcja](args)


COMMANDS = {"leady": cmd_leads}

__all__ = ["COMMANDS", "ERRORS", "add_parsers"]
