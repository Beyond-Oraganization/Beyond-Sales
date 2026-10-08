"""Zapytania do zbioru ze skanu „bez bazy": `szukaj` (fragmenty wokół fraz) i `wyciagnij` (tabela pól do CSV).

Celem jest, żeby AI nie musiało czytać stron ani pisać kodu: dostaje krótkie fragmenty z adresem źródła albo gotowy plik
CSV z kolumnami, które samo zdefiniowało jednym argumentem (`--pole nip`, `--ma rezerwacje=rezerwacja|booking`)."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, cast

from perno.domain import text_search as ts
from perno.services.scrape_dataset import Dataset, DatasetError, safe_cell, short_url

MAX_CSV_HITS = 5000
DEFAULT_LIMIT = 30
DEFAULT_PER_DOMAIN = 3
SITE_FIELDS = {"email": "emaile", "telefon": "telefony", "social": "social", "formularz": "formularz",
               "tytul": "tytul", "opis": "opis"}
EVIDENCE_WIDTH = 60
NO_DATA = "brak_danych"
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class Hit:
    """Jedno trafienie frazy: skąd (domena, adres, tytuł), która fraza i fragment tekstu."""
    domain: str
    url: str
    title: str
    term: str
    snippet: str


@dataclass
class SearchResult:
    """Wszystkie trafienia zbioru i liczniki na domenę (wypis i CSV ograniczają je dopiero na końcu)."""
    hits: list = field(default_factory=list)  # wszystkie trafienia (do CSV), w kolejności domen
    per_domain: dict = field(default_factory=dict)  # domena -> liczba trafień
    pages_hit: dict = field(default_factory=dict)  # domena -> liczba stron z trafieniem
    first_url: dict = field(default_factory=dict)
    domains: list = field(default_factory=list)  # wszystkie domeny zbioru


def _matcher(terms: list, regex: bool) -> Callable[[str], list]:
    """Funkcja tekst -> lista `Span`: frazy bez wielkości liter i ogonków albo wyrażenia regularne."""
    if not terms:
        raise DatasetError("Podaj co najmniej jedną frazę.")
    if regex:
        patterns = ts.compile_patterns(terms)
        return lambda text: ts.regex_spans(text, patterns)
    folded = [ts.fold(term) for term in terms]
    return lambda text: ts.phrase_spans(text, folded)


def search(dataset: Dataset, terms: list, *, regex: bool = False, domain: Optional[str] = None,
           width: int = ts.SNIPPET_WIDTH, per_page: int = 2) -> SearchResult:
    """Szuka we wszystkich stronach zbioru (albo jednej domeny). Nie ogranicza liczby trafień - robi to wypis."""
    find = _matcher(terms, regex)
    result = SearchResult(domains=[row["domena"] for row in dataset.sites() if not domain or row["domena"] == domain])
    for page in dataset.pages(domain):
        found = ts.snippets(page.text, find(page.text), width, per_page)
        for item in found:
            result.hits.append(Hit(page.domain, page.url, page.title, item.term, item.text))
        if found:
            result.per_domain[page.domain] = result.per_domain.get(page.domain, 0) + len(found)
            result.pages_hit[page.domain] = result.pages_hit.get(page.domain, 0) + 1
            result.first_url.setdefault(page.domain, page.url)
    return result


def clean(text: str) -> str:
    """Tekst bez znaków sterujących (z cudzej strony nie może trafić do terminala)."""
    return _CTRL.sub(" ", text)


def hit_lines(result: SearchResult, *, limit: int, per_domain: int, skip: int = 0) -> tuple:
    """(linie do wypisu, liczba pokazanych). Do `per_domain` trafień z każdej domeny, żeby jedna witryna nie zalała wyniku."""
    shown: list = []
    taken: dict = {}
    for hit in result.hits:
        if taken.get(hit.domain, 0) >= per_domain:
            continue
        taken[hit.domain] = taken.get(hit.domain, 0) + 1
        shown.append(hit)
    page = shown[skip:skip + limit]
    return [f"[{hit.domain}] {short_url(hit.url)} — {clean(hit.snippet)}" for hit in page], len(page)


def search_header(result: SearchResult, shown: int, per_domain: int) -> str:
    """Nagłówek wypisu: ile trafień i w ilu domenach, ile pokazano oraz jak dostać więcej."""
    total = sum(result.per_domain.values())
    return (f"# trafień {total} w {len(result.per_domain)}/{len(result.domains)} domenach; pokazano {shown} "
            f"(do {per_domain} na domenę). Więcej: --limit, --na-domene, --pomin N, --domena D, --csv plik")


def domain_rows(result: SearchResult) -> list:
    """Wiersze trybu „domeny": domena, trafień, stron z trafieniem, pierwszy adres."""
    return [[domain, result.per_domain.get(domain, 0), result.pages_hit.get(domain, 0), result.first_url.get(domain, "")]
            for domain in result.domains]


def write_hits_csv(path: str, result: SearchResult) -> int:
    """Zapisuje trafienia (do `MAX_CSV_HITS`); zwraca ich liczbę."""
    rows = result.hits[:MAX_CSV_HITS]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["domena", "url", "tytul", "fraza", "fragment"])
        writer.writerows([[safe_cell(v) for v in (h.domain, h.url, h.title, h.term, h.snippet)] for h in rows])
    return len(rows)


def write_domains_csv(path: str, result: SearchResult) -> int:
    """Zapisuje liczniki na domenę; zwraca liczbę domen."""
    rows = domain_rows(result)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["domena", "trafien", "stron_z_trafieniem", "pierwszy_url"])
        writer.writerows([[safe_cell(v) for v in row] for row in rows])
    return len(rows)


# ---- wyciąganie pól ------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    """Kolumna tabeli `wyciagnij`: z witryny.csv, z wzorca regex albo tak/nie dla fraz."""
    name: str
    kind: str  # site | regex | has
    pattern: re.Pattern | tuple | str | None = None  # regex: skompilowany wzorzec; has: krotka złożonych fraz; site: nazwa kolumny witryny.csv


def build_columns(fields: list, has: list) -> list:
    """`--pole` (nazwa kolumny witryny / gotowy wzorzec / nazwa=regex) i `--ma` (nazwa=fraza|fraza) -> kolumny."""
    columns: list = []
    for spec in fields:
        if "=" in spec:
            name, pattern = ts.parse_name(spec, "--pole")
            columns.append(Column(name, "regex", ts.compile_patterns([pattern])[0]))
        elif spec in SITE_FIELDS:
            columns.append(Column(spec, "site", SITE_FIELDS[spec]))
        elif spec in ts.PRESETS:
            columns.append(Column(spec, "regex", ts.compile_patterns([ts.PRESETS[spec]])[0]))
        else:
            known = ", ".join([*SITE_FIELDS, *ts.PRESETS])
            raise ts.SpecError(f"--pole {spec}: nieznane pole. Gotowe: {known}; własne: nazwa=wyrażenie_regularne.")
    for spec in has:
        name, value = ts.parse_name(spec, "--ma")
        columns.append(Column(name, "has", ts.parse_phrases(value)))
    names = [column.name for column in columns]
    if not columns:
        raise DatasetError("Podaj co najmniej jedno --pole albo --ma.")
    if len(set(names)) != len(names):
        raise DatasetError("Nazwy kolumn muszą być różne.")
    return columns


def extract_table(dataset: Dataset, columns: list, *, evidence: bool = False) -> tuple:
    """(nagłówek, wiersze, liczba wypełnionych komórek na kolumnę). Jeden wiersz na witrynę, także gdy skan się nie udał."""
    header = ["domena", "status"]
    for column in columns:
        header.append(column.name)
        if column.kind == "has" and evidence:
            header.append(column.name + "_dowod")
    header.append("zrodlo")
    sites = {row["domena"]: row for row in dataset.sites()}
    needs_text = any(column.kind != "site" for column in columns)
    texts: dict = {domain: [] for domain in sites}
    if needs_text:
        for page in dataset.pages():
            texts[page.domain].append(page)
    rows, filled = [], {column.name: 0 for column in columns}
    for domain, site in sites.items():
        row, source = [domain, site["status"]], ""
        for column in columns:
            cells, found_url = _cell(column, site, texts[domain], evidence)
            source = source or found_url
            row.extend(cells)
            filled[column.name] += 1 if cells[0] not in ("", "nie", NO_DATA) else 0
        rows.append([*row, source])
    return header, rows, filled


def _cell(column: Column, site: dict, pages: list, evidence: bool) -> tuple:
    if column.kind == "site":
        return [site[column.pattern]], ""
    if column.kind == "regex":
        values: list = []
        source = ""
        for page in pages:
            found = ts.extract_values(page.text, cast(re.Pattern, column.pattern))
            source = source or (page.url if found else "")
            values.extend(value for value in found if value not in values)
            if len(values) >= ts.MAX_VALUES_PER_FIELD:
                break
        return [" | ".join(values[:ts.MAX_VALUES_PER_FIELD])], source
    for page in pages:
        spans = ts.phrase_spans(page.text, cast(tuple, column.pattern))
        if spans:
            cell = ["tak"]
            if evidence:
                cell.append(ts.snippets(page.text, spans, EVIDENCE_WIDTH, 1)[0].text)
            return cell, page.url
    answer = NO_DATA if not pages else "nie"  # witryna bez tekstu (zablokowana, błąd): "nie" byłoby nieprawdą
    return ([answer, ""] if evidence else [answer]), ""


def write_table(path: str, header: list, rows: list) -> None:
    """Zapisuje tabelę wyników jako CSV (UTF-8, wartości zabezpieczone przed formułami arkusza)."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows([[safe_cell(cell) for cell in row] for row in rows])
