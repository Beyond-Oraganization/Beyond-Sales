"""Polecenia `ai.py` do skanu „bez bazy": zeskrapuj strony do plików, szukaj w nich i wyciągaj pola do CSV.

Nie dotykają bazy firm ani kolejki skrapera (patrz `services/adhoc_scrape`). Wywoływane z `perno.cli.ai`."""

from __future__ import annotations

from datetime import datetime

from perno.domain import text_search as ts
from perno.scraper import runner
from perno.services import adhoc_scrape, scrape_dataset, scrape_query
from perno.services.scrape_dataset import Dataset, DatasetError
from perno.settings import load_config

ERRORS = (DatasetError, ts.SpecError)
PREVIEW_ROWS = 15


def add_parsers(sub) -> None:
    run = sub.add_parser("zeskrapuj", help="Zeskrapuj strony do plików (bez bazy): CSV + tekst do przeszukiwania.")
    run.add_argument("adresy", nargs="*", help="Adresy stron (po jednym na domenę; reszty serwisu skraper dojdzie sam).")
    run.add_argument("--plik", help="Plik z adresami: .csv (kolumna website/url/adres/strona) albo tekst, jeden adres w linii.")
    run.add_argument("--nazwa", help="Nazwa zbioru (a-z, 0-9, - i _); domyślnie skrap_<data>.")
    run.add_argument("--max-stron", type=int, default=adhoc_scrape.DEFAULT_MAX_PAGES, help="Stron na domenę (domyślnie 25).")
    run.add_argument("--glebokosc", type=int, help="Ile kliknięć od strony startowej (domyślnie z profilu: 4).")
    run.add_argument("--rownolegle", type=int, default=adhoc_scrape.DEFAULT_CONCURRENCY, help="Ile domen naraz (po jednej na wątek).")
    run.add_argument("--nadpisz", action="store_true", help="Zastąp istniejący zbiór o tej nazwie.")
    run.add_argument("--dry-run", action="store_true", help="Pokaż adresy i plan ruchu, nic nie pobieraj.")

    find = sub.add_parser("szukaj", help="Znajdź frazy w zeskrapowanym tekście (fragmenty ze źródłem).")
    find.add_argument("nazwa")
    find.add_argument("frazy", nargs="+", help="Frazy (bez wielkości liter i ogonków); z --regex: wyrażenia regularne.")
    find.add_argument("--regex", action="store_true")
    find.add_argument("--domena", help="Tylko ta domena.")
    find.add_argument("--limit", type=int, default=scrape_query.DEFAULT_LIMIT, help="Ile fragmentów wypisać.")
    find.add_argument("--na-domene", type=int, default=scrape_query.DEFAULT_PER_DOMAIN, help="Najwyżej tyle fragmentów z jednej domeny.")
    find.add_argument("--pomin", type=int, default=0, help="Pomiń N pierwszych fragmentów (następna porcja).")
    find.add_argument("--szerokosc", type=int, default=ts.SNIPPET_WIDTH, help="Znaków kontekstu z każdej strony trafienia.")
    find.add_argument("--domeny", action="store_true", help="Zamiast fragmentów: tylko liczba trafień na domenę.")
    find.add_argument("--csv", nargs="?", const="", metavar="PLIK", help="Zapisz wszystkie trafienia (bez nazwy: szukaj.csv w katalogu zbioru).")

    pull = sub.add_parser("wyciagnij", help="Zbuduj tabelę CSV z polami wyciągniętymi z zeskrapowanego tekstu.")
    pull.add_argument("nazwa")
    pull.add_argument("--pole", action="append", default=[], metavar="POLE",
                      help="email|telefon|social|formularz|tytul|opis (z skanu), nip|regon|krs|cena (wzorce) albo nazwa=regex.")
    pull.add_argument("--ma", action="append", default=[], metavar="NAZWA=FRAZA|FRAZA",
                      help="Kolumna tak/nie: czy któraś fraza występuje na stronach domeny.")
    pull.add_argument("--dowod", action="store_true", help="Przy --ma dodaj kolumnę z fragmentem tekstu, który to potwierdza.")
    pull.add_argument("--csv", metavar="PLIK", help="Plik wynikowy (domyślnie wyniki.csv w katalogu zbioru).")
    pull.add_argument("--limit", type=int, default=PREVIEW_ROWS, help="Ile wierszy podglądu wypisać.")

    sub.add_parser("zbiory", help="Zbiory zeskrapowane wcześniej (data/skrap/).")


def cmd_scrape(args) -> int:
    name = args.nazwa or f"skrap_{datetime.now():%Y%m%d_%H%M%S}"
    targets = adhoc_scrape.read_targets(args.adresy, args.plik)
    options = adhoc_scrape.ScrapeOptions(name=scrape_dataset.check_name(name), max_pages=args.max_stron, depth=args.glebokosc,
                                         concurrency=args.rownolegle, overwrite=args.nadpisz)
    config = load_config(args.config)
    if args.dry_run:
        return _print_plan(targets, config, options)
    report = adhoc_scrape.run_scrape(targets, config, options)
    print("\n".join(adhoc_scrape.report_lines(report)))
    print(f"Dalej: ai.py szukaj {name} \"fraza\"  |  ai.py wyciagnij {name} --pole email --ma \"cos=fraza|fraza\"")
    return 1 if report.interrupted or report.errors else 0


def _print_plan(targets, config, options) -> int:
    settings = adhoc_scrape.build_settings(config, options)
    print(f"DRY-RUN zbiór „{options.name}”: domen {len(targets.sites)}, odrzucone {len(targets.rejected)}")
    for domain, url in targets.sites[:20]:
        print(f"  + {domain:<32} {url}")
    for entry, why in targets.rejected[:10]:
        print(f"  - {entry} ({why})")
    print("\n".join("  plan: " + line for line in runner.plan_summary(settings, len(targets.sites))))
    return 0


def _target_csv(dataset: Dataset, requested, default: str):
    return dataset.output_path(requested or default, default) if requested is not None else None


def cmd_search(args) -> int:
    dataset = Dataset(args.nazwa).require()
    result = scrape_query.search(dataset, args.frazy, regex=args.regex, domain=args.domena, width=args.szerokosc)
    if args.domeny:
        return _print_domains(dataset, result, args)
    lines, shown = scrape_query.hit_lines(result, limit=args.limit, per_domain=args.na_domene, skip=args.pomin)
    print(scrape_query.search_header(result, shown, args.na_domene))
    print("\n".join(lines))
    path = _target_csv(dataset, args.csv, "szukaj.csv")
    if path:
        print(f"Zapisano {scrape_query.write_hits_csv(path, result)} trafień: {path}")
    return 0


def _print_domains(dataset: Dataset, result, args) -> int:
    hit = [d for d in result.domains if result.per_domain.get(d)]
    missed = [d for d in result.domains if not result.per_domain.get(d)]
    print(f"# domeny z trafieniem: {len(hit)}/{len(result.domains)}")
    for domain in hit[:args.limit * 3]:
        print(f"{domain}: {result.per_domain[domain]} trafień na {result.pages_hit[domain]} str.")
    print(f"# bez trafienia ({len(missed)}): " + ", ".join(missed[:40]) + ("…" if len(missed) > 40 else ""))
    path = _target_csv(dataset, args.csv, "domeny.csv")
    if path:
        print(f"Zapisano {scrape_query.write_domains_csv(path, result)} domen: {path}")
    return 0


def cmd_extract(args) -> int:
    dataset = Dataset(args.nazwa).require()
    columns = scrape_query.build_columns(args.pole, args.ma)
    header, rows, filled = scrape_query.extract_table(dataset, columns, evidence=args.dowod)
    path = dataset.output_path(args.csv, "wyniki.csv")
    scrape_query.write_table(path, header, rows)
    print(f"Zapisano {len(rows)} wierszy: {path}")
    print("wypełnione: " + ", ".join(f"{name} {count}/{len(rows)}" for name, count in filled.items()))
    interesting = [row for row in rows if any(cell not in ("", "nie", scrape_query.NO_DATA) for cell in row[2:-1])]
    for row in (interesting or rows)[:args.limit]:
        print(" | ".join(_short(cell) for cell in row))
    return 0


def _short(cell: str, limit: int = 60) -> str:
    text = " ".join(str(cell).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def cmd_datasets(args) -> int:
    found = scrape_dataset.list_datasets()
    if not found:
        print("Brak zbiorów (data/skrap/ jest pusty). Utwórz: ai.py zeskrapuj ADRES --nazwa NAZWA")
    for meta in found:
        print(f"{meta['nazwa']:<28} {meta.get('status', '?'):<8} domen {meta.get('gotowe', 0)}/{meta.get('domen', '?')}  {meta.get('utworzono', '')}")
    return 0


COMMANDS = {"zeskrapuj": cmd_scrape, "szukaj": cmd_search, "wyciagnij": cmd_extract, "zbiory": cmd_datasets}

