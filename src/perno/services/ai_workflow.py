"""Przepływ pracy autora treści (Claude Code) w kilku krótkich operacjach: dodaj firmy, pokaż skrót, zapisz maile.

Wszystko, co AI potrzebuje do analizy firm, jest tu zebrane tak, żeby wynik był mały (stały budżet znaków) i żeby nie
trzeba było sięgać do bazy ręcznie. Operacje zmieniające bazę robią kopię zapasową, a skan stron startuje dopiero na
wyraźne żądanie (`scan=True`) i tylko dla nowo dodanych firm."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence
from urllib.parse import urlparse

from perno import settings as cfg
from perno.db import Database
from perno.domain import brief as brief_rules
from perno.domain import contact_pick, icebreaker_rules, localize, segments
from perno.domain import statuses as st
from perno.scraper import runner
from perno.scraper import statuses as sst
from perno.scraper.store import Store
from perno.services import enrichment, firms_import, scan_control, template_catalog

MAX_FIRMS_PER_ADD = 100  # tyle firm naraz może trafić do skanu z jednego wywołania
BRIEF_PAGES = 60  # ile najpłytszych stron firmy przeglądamy, szukając fragmentów do skrótu
BRIEF_SNIPPETS = 4
SCAN_PROFILE = "kontekst"  # mały ruch na firmę: ok. 25 stron zamiast 500
SKIPPABLE = (st.STATUS_SKIPPED_NO_EMAIL, st.STATUS_AWAITING_COPY, st.STATUS_PENDING)
DEFAULT_TEMPLATE = localize.ICEBREAKER


@dataclass
class AddReport:
    """Wynik dodawania firm: kto wszedł do bazy, kto został pominięty i (opcjonalnie) uruchomiony skan."""
    segment: str
    dry_run: bool
    new: list = field(default_factory=list)
    skipped_existing: list = field(default_factory=list)
    duplicates_in_file: list = field(default_factory=list)
    backup: str = ""
    scan_plan: list = field(default_factory=list)
    scan: Optional[scan_control.StartedScan] = None


@dataclass(frozen=True)
class ScanRequest:
    """Prośba o skan nowo dodanych firm: konfiguracja i ścieżka pliku (potrzebna odłączonemu procesowi); `spawn` do testów."""
    config: Mapping
    config_path: str
    spawn: Optional[Callable] = None


@dataclass
class BriefBatch:
    """Paczka skrótów: teksty firm, ile jest wszystkich kandydatów i od jakiego id wziąć następną paczkę."""
    texts: list
    total: int
    next_after: str = ""


@dataclass
class WritePlan:
    """Wynik kontroli pliku z treściami: wpisy do zapisu, błędy blokujące i ostrzeżenia (wszystko „id: komunikat”)."""
    items: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def known_domains(db: Database, segment: Optional[str] = None) -> list:
    """Domeny firm już w bazie (wszystkich albo jednego segmentu), posortowane i bez duplikatów."""
    keys = {firms_import.domain_key(site) for site in db.company_websites(segment)}
    return sorted(key for key in keys if key)


def add_firms(db: Database, store: Store, csv_path: str, segment_name: str, *, dry_run: bool,
              scan: Optional[ScanRequest] = None) -> AddReport:
    """Importuje firmy z CSV do segmentu i kolejkuje je w skraperze; z `scan` od razu startuje skan nowych firm
    (przy `dry_run` pokazuje tylko jego plan)."""
    segment = segments.normalize(segment_name)
    rows = firms_import.read_import_csv(csv_path)
    if len(rows) > MAX_FIRMS_PER_ADD:
        raise firms_import.FirmsError(f"W pliku jest {len(rows)} firm, naraz wolno najwyżej {MAX_FIRMS_PER_ADD} - podziel plik.")
    result = firms_import.import_firms(db, rows, segment, dry_run=True)
    report = AddReport(segment, dry_run, result.new, result.skipped_existing, result.duplicates_in_file)
    if dry_run:
        if scan:
            report.scan_plan = _scan_plan(scan.config, len(result.new))
        return report
    report.backup = firms_import.backup_table(db, cfg.BACKUP_DIR, "importem-ai")
    db.prepare()
    result = firms_import.import_firms(db, rows, segment)
    report.new = result.new
    store.ensure_schema()
    store.seed_from_maile()
    if scan and result.new:
        report.scan_plan = _scan_plan(scan.config, len(result.new))
        report.scan = _start_scan(store, scan, result.new)
    return report


def _scan_plan(config: Mapping, firms: int) -> list:
    settings = scan_control.settings_for_run(config, {"profile": SCAN_PROFILE})
    return runner.plan_summary(settings, firms)


def _start_scan(store: Store, request: ScanRequest, new: Sequence[Mapping]) -> scan_control.StartedScan:
    scan_control.ensure_parser_available()
    settings = scan_control.settings_for_run(request.config, {"profile": SCAN_PROFILE})
    firm_ids = [found["id"] for found in (store.find_firma(item["domain"]) for item in new) if found]
    return scan_control.start_scan(store, settings, firma_ids=firm_ids, statuses=sst.FIRMA_DOMYSLNIE_DO_SKANU,
                                   config_path=request.config_path, spawn=request.spawn)


# ---- skrót firm do analizy ---------------------------------------------------------------

def _address_line(store: Store, row: Mapping) -> str:
    found = store.scraped_emails(row["firma_id"])
    if row["email"]:
        return f"{row['email']} (już w bazie)"
    pick = contact_pick.pick_email(found, firms_import.domain_key(row["website"]) or "")
    if pick:
        return f"{pick} (wybrany ze skanu)"
    options = ", ".join(f"{item['wartosc']} ({item['liczba_stron']} str.)" for item in found[:3])
    return "BRAK pewnego adresu" + (f"; kandydaci: {options}" if options else "; skan nie znalazł żadnego e-maila")


def _template_line(language: Optional[str], name: str = DEFAULT_TEMPLATE) -> str:
    if language is None:
        return "BRAK szablonu dla tego kraju"
    info = template_catalog.get(name)
    fields = [*info.fields, *(f"[{item}]" for item in info.optional)]
    return f"{name}/{language}, pola: {', '.join(fields)}"


OFFER_HINTS = ("cennik", "cena", "ceny", "zapis", "rezerw", "bilet", "termin", "oferta", "kurs", "booking", "price", "sklep")


def _page_snippets(pages: Sequence[tuple]) -> list:
    """Fragmenty ze stron poza główną; najpierw strony o ofercie, cenach i zapisach (po adresie), potem reszta."""
    found, seen = [], set()
    ordered = sorted(pages[1:], key=lambda page: not any(hint in page[0].lower() for hint in OFFER_HINTS))
    for url, _title, _description, text in ordered:
        for fragment in brief_rules.snippets(text or "", limit=1):
            if fragment[:60] not in seen:
                seen.add(fragment[:60])
                found.append((urlparse(url).path or "/", fragment))
    return found[:BRIEF_SNIPPETS]


def brief_for(store: Store, row: Mapping) -> brief_rules.FirmBrief:
    """Skrót jednej firmy z wiersza kandydata (`enrichment.list_candidates`)."""
    pages = store.brief_pages(row["firma_id"], BRIEF_PAGES)
    language = localize.language_for(row["country"])
    signals = brief_rules.detect([part for url, _t, _d, text in pages for part in (url, text or "")])
    phones = store.contact_values(row["firma_id"], "telefon", 1)
    forms = store.contact_values(row["firma_id"], "formularz", 1)
    first = pages[0] if pages else ("", "", "", "")
    return brief_rules.FirmBrief(
        row_id=row["id"], company=row["company"], place=" ".join(p for p in (row["country"], row["city"]) if p) or "-",
        language=language or "BRAK", pages=row["stron_pobranych"], address=_address_line(store, row),
        phone=phones[0] if phones else "", form=forms[0] if forms else "", description=row["description"] or "",
        home=brief_rules.home_line(first[1], first[2]), template=_template_line(language), signals=signals,
        snippets=_page_snippets(pages))


def build_briefs(store: Store, *, segment: Optional[str], limit: int, after: str, ids: Sequence[str],
                 chars: int, budget: int, include_partial: bool = False) -> BriefBatch:
    """Skróty kolejnych kandydatów (po id): najwyżej `limit` firm, `chars` znaków na firmę i `budget` znaków razem."""
    candidates = sorted(enrichment.list_candidates(store, include_partial=include_partial), key=lambda row: row["id"])
    if segment:
        candidates = [row for row in candidates if row["segment"] == segment]
    if ids:
        candidates = [row for row in candidates if row["id"] in set(ids)]
    remaining = [row for row in candidates if row["id"] > after] if after else candidates
    texts: list = []
    used, last = 0, ""
    for row in remaining[:max(1, limit)]:
        text = brief_for(store, row).format(chars)
        if texts and used + len(text) > budget:
            break
        texts.append(text)
        used += len(text)
        last = row["id"]
    more = len(remaining) > len(texts)
    return BriefBatch(texts, len(candidates), last if more else "")


# ---- zapis treści ------------------------------------------------------------------------

def _entry_problem(entry: Mapping, row: Optional[Mapping], store: Store) -> tuple:
    """`(błąd albo None, wpis z uzupełnionym adresem i formą)`; ostrzeżenia dopisuje wywołujący."""
    if row is None:
        return ("nie kwalifikuje się (brak wiersza, skan niezakończony albo status inny niż „Brak e-maila”/„Czeka na treść”)", entry)
    name = str(entry.get("szablon") or DEFAULT_TEMPLATE)
    try:
        info = template_catalog.get(name)
    except template_catalog.UnknownTemplate:
        return (f"nie ma szablonu „{name}” w templates/ (sprawdź: ai.py szablony)", entry)
    if not info.writable:
        return (f"szablon „{name}” wymaga pól, których zapis maili pierwszego kontaktu nie obsługuje", entry)
    language = localize.language_for(row["country"])
    if language is None or language not in info.languages:
        return (f"brak wersji szablonu „{name}” dla kraju „{row['country']}”", entry)
    prepared = dict(entry)
    if not str(prepared.get("email") or "").strip():
        prepared["email"] = row["email"] or contact_pick.pick_email(
            store.scraped_emails(row["firma_id"]), firms_import.domain_key(row["website"]) or "") or ""
        if not prepared["email"]:
            return ("brak pewnego adresu - podaj \"email\" z kandydatów w skrócie albo oznacz firmę poleceniem pomin", prepared)
    return None, prepared


def plan_entries(store: Store, entries: Sequence[Mapping], sender_name: str, *, include_pending: bool = False) -> WritePlan:
    """Sprawdza wpisy (pola szablonu, reguły treści, adres) i składa maile. Nic nie zapisuje do bazy."""
    plan = WritePlan()
    rows = {row["id"]: row for row in enrichment.list_candidates(store, include_pending)}
    accepted: list = []
    for entry in entries:
        row_id = str(entry.get("id") or "?")
        problem, prepared = _entry_problem(entry, rows.get(row_id), store)
        language = localize.language_for(rows[row_id]["country"]) if row_id in rows else None
        if problem is None and language:
            try:
                prepared["mode"] = icebreaker_rules.normalize_mode(prepared.get("mode"))
            except ValueError as exc:
                problem = str(exc)
        if problem is None and language:
            found = icebreaker_rules.check(prepared, language)
            plan.warnings += [f"{row_id}: {message}" for message in found.warnings]
            problem = "; ".join(found.errors) or None
        if problem:
            plan.errors.append(f"{row_id}: {problem}")
        else:
            accepted.append(prepared)
    checked = enrichment.plan_updates(store, accepted, sender_name, include_pending)
    plan.items, plan.errors = checked.items, [*plan.errors, *checked.errors]
    return plan


def write_plan(db: Database, plan: WritePlan, *, include_pending: bool = False) -> tuple:
    """Zapisuje poprawne wpisy planu. Zwraca (liczba zapisanych, ścieżka kopii zapasowej)."""
    if not plan.items:
        return 0, ""
    backup = firms_import.backup_table(db, cfg.BACKUP_DIR, "tresciami-ai")
    return enrichment.write_updates(db, enrichment.EnrichmentPlan(items=plan.items), include_pending), backup


# ---- odrzucanie firm i raport stanu ------------------------------------------------------

def skip_rows(db: Database, ids: Sequence[str], reason: str) -> tuple:
    """Oznacza wiersze „Nie pasuje” (powód trafia na początek opisu firmy). Zwraca (zapisane id, błędy „id: komunikat”)."""
    done, errors = [], []
    backup = firms_import.backup_table(db, cfg.BACKUP_DIR, "odrzuceniem-ai") if ids else ""
    for row_id in ids:
        row = db.get(row_id)
        if row is None:
            errors.append(f"{row_id}: nie ma takiego wiersza")
        elif row["status"] not in SKIPPABLE:
            errors.append(f"{row_id}: status „{st.STATUS_META[row['status']][0]}” - nie ruszam (tylko jeszcze niewysłane)")
        else:
            note = f"Nie pasuje: {reason.strip()}. {row['description'] or ''}".strip()
            db.apply_edit(row_id, {"status": st.STATUS_SKIPPED_FIT, "description": note[:1000]})
            done.append(row_id)
    return done, errors, backup


def status_lines(db: Database, store: Store, segment: Optional[str] = None) -> list:
    """Przegląd kolejki i skanu wg segmentów oraz liczba firm gotowych do analizy; jedna linia na segment."""
    mails, scans = db.segment_status_counts(), store.scan_counts_by_segment()
    names = sorted({s for s, _ in mails} | {s for s, _ in scans} - {""}, key=lambda s: (s not in st.SEGMENT_ORDER, s))
    ready: dict = {}
    for row in enrichment.list_candidates(store):
        ready[row["segment"]] = ready.get(row["segment"], 0) + 1
    lines = []
    for name in (n for n in names if not segment or n == segment):
        scan = ", ".join(f"{sst.FIRMA_META[k][0]} {v}" for (s, k), v in sorted(scans.items()) if s == name and k in sst.FIRMA_META)
        mail = ", ".join(f"{st.STATUS_META[k][0]} {v}" for (s, k), v in sorted(mails.items()) if s == name and k in st.STATUS_META)
        lines.append(f"{name}: skan [{scan or '-'}] | maile [{mail or '-'}] | gotowe do analizy: {ready.get(name, 0)}")
    run = store.active_run()
    lines.append(f"aktywny skan: #{run['id']} ({run['status']})" if run else "aktywny skan: brak")
    return lines
