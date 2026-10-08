"""Import nowych firm z CSV do tabeli `maile` oraz przypisywanie firmom kraju, miasta i adresu.

Nowa firma nie ma jeszcze e-maila ani treści, więc wchodzi do bazy ze statusem „Brak e-maila" — z panelu nie da się jej
wysłać przez pomyłkę. Dostaje identyfikator z osobnej przestrzeni (`eventy-n001`), żeby nigdy nie zderzyć się
z numeracją z dossier (`eventy-051`…), która może jeszcze urosnąć, i nie ma `row_number` (w panelu trafia na koniec segmentu).

Firmę rozpoznajemy po domenie (bez `www.`) — ta sama zasada, co w skraperze, który tworzy z tej tabeli swoją listę.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from perno.db import Database, DbError, InvalidValue, format_value, from_api
from perno.db import companies as companies_table
from perno.db import maile as maile_table
from perno.db.replies_schema import odp_oceny, odp_watki, odp_wiadomosci
from perno.domain import countries, segments
from perno.domain import statuses as st
from perno.scraper.urls import host_key

IMPORT_REQUIRED = ("company_display_name", "website")
PROFILE_REQUIRED = ("domain", "country")
PROFILE_FIELDS = ("country", "city", "address")


class FirmsError(DbError):
    """Błąd importu firm lub pliku z krajami (komunikat jest dla użytkownika)."""
    pass


def domain_key(url: object) -> Optional[str]:
    """Domena firmy: małe litery, bez `www.` i portu; None, gdy z adresu nie da się jej wyciągnąć."""
    text = str(url or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    return host_key(host) if host else None


# ---- kopia zapasowa ------------------------------------------------------------------

REPLY_BACKUPS = (("OdpWatki", odp_watki), ("OdpWiadomosci", odp_wiadomosci), ("OdpOceny", odp_oceny))

def _unused_path(directory: str, prefix: str, stamp: str, label: str) -> str:
    path = os.path.join(directory, f"{prefix}_{stamp}_przed-{label}.csv")
    counter = 1
    while os.path.exists(path):  # dwie kopie w tej samej sekundzie nie mogą się nadpisać — pierwsza jest stanem sprzed zmian
        counter += 1
        path = os.path.join(directory, f"{prefix}_{stamp}_przed-{label}_{counter}.csv")
    return path


def _dump_csv(db: Database, table: Any, path: str) -> None:
    tmp = path + ".tmp"
    with db.dump_table(table) as (names, rows), open(tmp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(names)
        for row in rows:
            writer.writerow([format_value(value) for value in row])
    os.replace(tmp, path)


def backup_table(db: Database, directory: str, label: str, now: Optional[datetime] = None, *, replies: bool = False) -> str:
    """Zapisuje całą tabelę `maile` (wszystkie kolumny, które w niej są teraz) do CSV, a gdy istnieje tabela firm
    (`podmioty`), obok niej drugi plik `Podmioty_*`. Z `replies=True` także tabele odpowiedzi, które już istnieją
    (`OdpWatki_*`, `OdpWiadomosci_*`, `OdpOceny_*`; zawierają cudzą korespondencję, więc katalog kopii jest poza gitem).
    Działa także na bazie sprzed uaktualnienia schematu, bo bierze kolumny z samej tabeli, a nie z definicji w kodzie.
    Zwraca ścieżkę pliku z `maile`."""
    os.makedirs(directory, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    path = _unused_path(directory, "Maile", stamp, label)
    _dump_csv(db, maile_table, path)
    if db.has_table(companies_table.name):
        _dump_csv(db, companies_table, _unused_path(directory, "Podmioty", stamp, label))
    for prefix, table in (REPLY_BACKUPS if replies else ()):
        if db.has_table(table.name):
            _dump_csv(db, table, _unused_path(directory, prefix, stamp, label))
    return path


# ---- import nowych firm --------------------------------------------------------------

@dataclass
class ImportResult:
    """Wynik importu firm: dodane oraz pominięte jako już istniejące albo zdublowane w pliku."""
    segment: str
    dry_run: bool
    new: list = field(default_factory=list)  # [{id, company, website, domain, country, city}]
    skipped_existing: list = field(default_factory=list)  # [(firma, id istniejącego wiersza)]
    duplicates_in_file: list = field(default_factory=list)  # [(wiersz, firma)]


def read_import_csv(path: str) -> list:
    """Wczytuje listę firm: wymagane kolumny `company_display_name`, `website`; opcjonalne `what_they_sell`,
    `country`, `city`, `address`. Wszystkie błędy zbiera i zgłasza naraz, z numerami wierszy."""
    problems, rows = [], []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = [h for h in (reader.fieldnames or []) if h]
        missing = [name for name in IMPORT_REQUIRED if name not in header]
        if missing:
            raise FirmsError(f"W pliku {os.path.basename(path)} brakuje kolumn: {', '.join(missing)} (jest: {', '.join(header)}).")
        for number, raw in enumerate(reader, start=2):
            row = {key: (value or "").strip() for key, value in raw.items() if key}
            if not any(row.values()):
                continue
            where = f"wiersz {number}"
            name, website = row["company_display_name"], row["website"]
            if not name:
                problems.append(f"{where}: brak nazwy firmy")
                continue
            if "://" not in website and website:
                website = "https://" + website
            domain = domain_key(website)
            if not domain:
                problems.append(f"{where} ({name}): nieprawidłowy adres strony {row['website']!r}")
                continue
            country = row.get("country", "")
            if country:
                code = countries.normalize_country(country)
                if code is None:
                    problems.append(f"{where} ({name}): nieznany kraj {country!r}")
                    continue
                country = code
            rows.append({"line": number, "company": name, "website": website, "domain": domain,
                         "description": row.get("what_they_sell", ""), "country": country,
                         "city": row.get("city", ""), "address": row.get("address", "")})
    if problems:
        raise FirmsError("Plik CSV zawiera błędy:\n  " + "\n  ".join(problems))
    return rows


def _next_sequence(ids: Iterable[str], segment: str) -> int:
    pattern = re.compile(rf"^{re.escape(segment)}-n(\d+)$")
    numbers = [int(m.group(1)) for m in (pattern.match(i) for i in ids) if m]
    return max(numbers, default=0) + 1


def import_firms(db: Database, rows: list, segment: str, *, dry_run: bool = False) -> ImportResult:
    """Dodaje firmy z `rows` (patrz `read_import_csv`) do segmentu. Firmy, których domena jest już w bazie
    (w jakimkolwiek segmencie), pomija. Wszystko albo nic: błąd w jednym wierszu cofa cały import."""
    if not segments.is_valid(segment):
        raise FirmsError(f"Niepoprawna nazwa segmentu: {segment!r} (małe litery, cyfry i myślniki, np. „kancelarie”).")
    result = ImportResult(segment=segment, dry_run=dry_run)
    with db.transaction() as conn:
        existing: dict[str, str] = {}
        for found in db.id_website_pairs(conn):
            key = domain_key(found["website"])
            if key:
                existing.setdefault(key, found["id"])
        sequence = _next_sequence(db.segment_ids(conn, segment), segment)

        seen, prepared, problems = set(), [], []
        for row in rows:
            if row["domain"] in existing:
                result.skipped_existing.append((row["company"], existing[row["domain"]]))
                continue
            if row["domain"] in seen:
                result.duplicates_in_file.append((row.get("line", "?"), row["company"]))
                continue
            seen.add(row["domain"])
            values = {
                "id": f"{segment}-n{sequence:03d}", "segment": segment, "company": row["company"],
                "website": row["website"], "status": st.STATUS_SKIPPED_NO_EMAIL, "retry_count": "0",
                "country": row["country"], "city": row["city"], "address": row["address"],
                "description": row["description"],
            }
            try:
                from_api(values)
            except InvalidValue as exc:
                problems.append(f"{row['company']}: {exc}")
                continue
            sequence += 1
            prepared.append(values)
            result.new.append({"id": values["id"], "company": row["company"], "website": row["website"],
                               "domain": row["domain"], "country": row["country"], "city": row["city"]})
        if problems:
            raise FirmsError("Nie można zaimportować — błędne dane:\n  " + "\n  ".join(problems))
        if prepared and not dry_run:
            db.insert_rows(conn, prepared)
    return result


# ---- kraj, miasto, adres ---------------------------------------------------------------

@dataclass
class AssignResult:
    """Wynik przypisania kraju, miasta i adresu: zmiany, wartości zostawione bez zmian i wpisy bez dopasowania."""
    dry_run: bool
    updated: list = field(default_factory=list)  # [(id, {pole: nowa wartość})]
    unchanged: int = 0
    kept: list = field(default_factory=list)  # [(id, pole, obecna wartość, proponowana)] — nie nadpisano
    unmatched_domains: list = field(default_factory=list)  # domeny z pliku, których nie ma w bazie
    rows_without_entry: list = field(default_factory=list)  # id wierszy bazy, których nie ma w pliku


def read_profile_csv(path: str) -> list:
    """Plik z przypisaniem: kolumny `domain`, `country`; opcjonalne `city`, `address`, `source` (skąd ta decyzja)."""
    problems, entries, seen = [], [], set()
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = [h for h in (reader.fieldnames or []) if h]
        missing = [name for name in PROFILE_REQUIRED if name not in header]
        if missing:
            raise FirmsError(f"W pliku {os.path.basename(path)} brakuje kolumn: {', '.join(missing)} (jest: {', '.join(header)}).")
        for number, raw in enumerate(reader, start=2):
            row = {key: (value or "").strip() for key, value in raw.items() if key}
            if not any(row.values()):
                continue
            where = f"wiersz {number}"
            domain = domain_key(row["domain"])
            code = countries.normalize_country(row["country"])
            if not domain:
                problems.append(f"{where}: nieprawidłowa domena {row['domain']!r}")
            elif code is None:
                problems.append(f"{where} ({domain}): nieznany kraj {row['country']!r}")
            elif domain in seen:
                problems.append(f"{where}: domena {domain} występuje w pliku drugi raz")
            else:
                seen.add(domain)
                entries.append({"domain": domain, "country": code, "city": row.get("city", ""),
                                "address": row.get("address", ""), "source": row.get("source", "")})
    if problems:
        raise FirmsError("Plik z krajami zawiera błędy:\n  " + "\n  ".join(problems))
    return entries


def assign_profiles(db: Database, entries: list, *, overwrite: bool = False, dry_run: bool = False,
                    now: Optional[datetime] = None) -> AssignResult:
    """Uzupełnia kraj, miasto i adres firm na podstawie domeny. Domyślnie wypełnia tylko puste pola — to, co ktoś
    poprawił ręcznie w panelu, zostaje (`overwrite=True` nadpisuje). Nie rusza statusów, maili ani niczego innego."""
    by_domain = {entry["domain"]: entry for entry in entries}
    result = AssignResult(dry_run=dry_run)
    stamp = (now or db.now())
    matched = set()
    with db.transaction() as conn:
        for row in db.profile_rows(conn):
            key = domain_key(row["website"])
            entry = by_domain.get(key)
            if entry is None:
                result.rows_without_entry.append(row["id"])
                continue
            matched.add(key)
            changes = {}
            for name in PROFILE_FIELDS:
                proposed = entry.get(name) or None
                current = row[name]
                if not proposed or current == proposed:
                    continue
                if current and not overwrite:
                    result.kept.append((row["id"], name, current, proposed))
                    continue
                changes[name] = proposed
            if changes:
                result.updated.append((row["id"], changes))
            else:
                result.unchanged += 1
        if result.updated and not dry_run:
            db.update_rows(conn, result.updated, stamp)
    result.unmatched_domains = sorted(set(by_domain) - matched)
    return result


# ---- raport ----------------------------------------------------------------------------------

def profile_report(db: Database) -> dict:
    """Ile firm w którym kraju i segmencie oraz które firmy nie mają jeszcze kraju."""
    rows = db.fetch_all()
    by_country: dict = {}
    without = []
    for row in rows:
        if row["country"]:
            by_country.setdefault(row["country"], {})
            by_country[row["country"]][row["segment"]] = by_country[row["country"]].get(row["segment"], 0) + 1
        else:
            without.append({"id": row["id"], "company": row["company"], "website": row["website"], "segment": row["segment"]})
    return {"total": len(rows), "by_country": by_country, "without_country": without,
            "with_city": sum(1 for r in rows if r["city"]), "with_address": sum(1 for r in rows if r["address"])}
