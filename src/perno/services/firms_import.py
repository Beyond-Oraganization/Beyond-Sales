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
from typing import Optional
from urllib.parse import urlsplit

import sqlalchemy as sa

from perno.db import Database, DbError, InvalidValue, format_value, from_api, maile
from perno.domain import countries
from perno.domain import statuses as st
from perno.scraper.urls import host_key

IMPORT_REQUIRED = ("company_display_name", "website")
PROFILE_REQUIRED = ("domain", "country")
PROFILE_FIELDS = ("country", "city", "address")


class FirmsError(DbError):
    pass


def domain_key(url) -> Optional[str]:
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

def backup_table(db: Database, directory: str, label: str, now: Optional[datetime] = None) -> str:
    """Zapisuje całą tabelę `maile` (wszystkie kolumny, które w niej są teraz) do CSV. Działa także na bazie sprzed
    uaktualnienia schematu, bo bierze kolumny z samej tabeli, a nie z definicji w kodzie."""
    os.makedirs(directory, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(directory, f"Maile_{stamp}_przed-{label}.csv")
    counter = 1
    while os.path.exists(path):  # dwie kopie w tej samej sekundzie nie mogą się nadpisać — pierwsza jest stanem sprzed zmian
        counter += 1
        path = os.path.join(directory, f"Maile_{stamp}_przed-{label}_{counter}.csv")
    with db.engine.connect() as conn:
        present = [column["name"] for column in sa.inspect(conn).get_columns(maile.name)]
        # kolumny z typami z definicji tabeli (daty zapisują się tak samo na MySQL i sqlite); nieznane — jako tekst
        selected = [maile.c[name] if name in maile.c else sa.column(name) for name in present]
        result = conn.execute(sa.select(*selected).select_from(maile).order_by(maile.c.id))
        names = list(result.keys())
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(names)
            for row in result:
                writer.writerow([format_value(value) for value in row])
    os.replace(tmp, path)
    return path


# ---- import nowych firm --------------------------------------------------------------

@dataclass
class ImportResult:
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


def _next_sequence(ids, segment: str) -> int:
    pattern = re.compile(rf"^{re.escape(segment)}-n(\d+)$")
    numbers = [int(m.group(1)) for m in (pattern.match(i) for i in ids) if m]
    return max(numbers, default=0) + 1


def import_firms(db: Database, rows: list, segment: str, *, dry_run: bool = False) -> ImportResult:
    """Dodaje firmy z `rows` (patrz `read_import_csv`) do segmentu. Firmy, których domena jest już w bazie
    (w jakimkolwiek segmencie), pomija. Wszystko albo nic: błąd w jednym wierszu cofa cały import."""
    if segment not in st.SEGMENT_ORDER:
        raise FirmsError(f"Nieznany segment: {segment!r} (dozwolone: {', '.join(st.SEGMENT_ORDER)}).")
    result = ImportResult(segment=segment, dry_run=dry_run)
    with db.engine.begin() as conn:
        existing: dict[str, str] = {}
        for found in conn.execute(sa.select(maile.c.id, maile.c.website)):
            key = domain_key(found.website)
            if key:
                existing.setdefault(key, found.id)
        in_segment = [r.id for r in conn.execute(sa.select(maile.c.id).where(maile.c.segment == segment))]
        sequence = _next_sequence(in_segment, segment)

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
    with db.engine.begin() as conn:
        rows = conn.execute(sa.select(maile.c.id, maile.c.website, maile.c.country, maile.c.city, maile.c.address)).all()
        for row in rows:
            key = domain_key(row.website)
            entry = by_domain.get(key)
            if entry is None:
                result.rows_without_entry.append(row.id)
                continue
            matched.add(key)
            changes = {}
            for name in PROFILE_FIELDS:
                proposed = entry.get(name) or None
                current = getattr(row, name)
                if not proposed or current == proposed:
                    continue
                if current and not overwrite:
                    result.kept.append((row.id, name, current, proposed))
                    continue
                changes[name] = proposed
            if changes:
                result.updated.append((row.id, changes))
            else:
                result.unchanged += 1
        if result.updated and not dry_run:
            for row_id, changes in result.updated:
                conn.execute(sa.update(maile).where(maile.c.id == row_id).values(**changes, updated_at=stamp))
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
