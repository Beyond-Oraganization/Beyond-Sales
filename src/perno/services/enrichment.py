"""Uzupełnianie maili treścią z zakończonych skanów: kandydaci, kontekst dla autora treści, walidacja i zapis.

Przepływ: `list_candidates` → `build_context` (materiał do napisania treści) → `plan_updates` (walidacja pliku
z treściami; nic nie zapisuje) → `write_updates`. Adres e-mail musi pochodzić z kontaktów znalezionych przez
skraper (nie wymyślamy adresów), a język maila wynika z kraju firmy zapisanego w tabeli.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from perno.db import Database
from perno.domain import localize, templating
from perno.domain import statuses as st
from perno.scraper import statuses as sst
from perno.scraper.store import Store
from perno.services.generation import render_row

FILLABLE = (st.STATUS_SKIPPED_NO_EMAIL, st.STATUS_AWAITING_COPY)
REQUIRED = ("id", "email", "subject", "obs", "pitch", "channel_summary")
OPTIONAL_PROFILE = ("city", "address", "description")
MIN_PARTIAL_PAGES = 10  # skan przerwany przez serwer (429/503) wystarcza, gdy zdążył pobrać tyle stron
KEY_PAGE_HINTS = ("zapis", "rezerw", "cennik", "kontakt", "oferta", "termin", "kurs", "wyjazd", "wyprawy", "o-nas", "onas",
                  "booking", "contact", "price", "preise", "buchen", "reserva", "precio", "about")
PAGE_TEXT_CHUNK = 2500  # najwięcej znaków tekstu z jednej strony w pliku kontekstu
DEFAULT_CONTEXT_CHARS = 9000


@dataclass(frozen=True)
class PlannedUpdate:
    row_id: str
    company: str
    language: str
    values: dict  # pola do zapisu w tabeli maile (e-mail, temat, treści, tag, …)


@dataclass
class EnrichmentPlan:
    items: list = field(default_factory=list)  # [PlannedUpdate]
    errors: list = field(default_factory=list)  # ["id: komunikat"]


def allowed_statuses(include_pending: bool) -> tuple:
    """Statusy wierszy, które wolno uzupełnić; „Do wysłania” (jeszcze niewysłane) tylko na wyraźne życzenie."""
    return FILLABLE + ((st.STATUS_PENDING,) if include_pending else ())


def list_candidates(store: Store, include_pending: bool = False, include_partial: bool = False) -> list:
    """Wiersze, które mają zakończony skan i czekają na treść (z `include_partial` także skan przerwany przez serwer,
    jeśli zdążył pobrać `MIN_PARTIAL_PAGES` stron)."""
    scans = (sst.FIRMA_ZAKONCZONA, sst.FIRMA_LIMIT) + ((sst.FIRMA_ZABLOKOWANA,) if include_partial else ())
    return store.enrichment_candidates(allowed_statuses(include_pending), scans,
                                       MIN_PARTIAL_PAGES if include_partial else 0)


def build_context(store: Store, candidate: Mapping, chars: int = DEFAULT_CONTEXT_CHARS) -> str:
    """Materiał do napisania treści (markdown): opis firmy, kontakty ze skanu, lista stron i tekst kluczowych stron
    do wyczerpania budżetu `chars` znaków."""
    firma_id = candidate["firma_id"]
    lines = [f"# {candidate['id']} — {candidate['company']}", "",
             f"strona: {candidate['website']}",
             f"kraj: {candidate['country']}  (język maila: {localize.language_for(candidate['country'])})",
             f"miasto: {candidate['city']}  adres: {candidate['address']}", f"opis w bazie: {candidate['description']}", "",
             "## E-maile ze skanu"]
    for found in store.scraped_emails(firma_id):
        lines.append(f"- {found['wartosc']}  (w domenie: {bool(found['w_domenie'])}, rola: {found['rola']}, "
                     f"źródło: {found['zrodlo_url']})")
    for kind in ("telefon", "formularz"):
        lines += ["", f"## {kind.capitalize()}"] + [f"- {value}" for value in store.contact_values(firma_id, kind)]
    lines += ["", "## Strony"]
    pages = store.fetched_pages(firma_id)
    lines += [f"- {url} | {title} | {(desc or '')[:160]}" for url, title, desc, _text in pages]
    budget = chars
    for url, _title, _desc, text in pages:
        if budget <= 0:
            break
        if not text or not (url.rstrip("/").count("/") <= 2 or any(hint in url.lower() for hint in KEY_PAGE_HINTS)):
            continue
        chunk = text[:min(PAGE_TEXT_CHUNK, budget)]
        budget -= len(chunk)
        lines += ["", f"### {url}", chunk]
    return "\n".join(lines) + "\n"


def _missing_fields(entry: Mapping) -> list:
    return [name for name in REQUIRED if not str(entry.get(name) or "").strip()]


def _read_template(segment: str, language: str) -> str:
    with open(localize.template_path(segment, language), encoding="utf-8") as handle:
        return handle.read()


def _plan_entry(entry: Mapping, row: Mapping, scraped: set, sender_name: str) -> PlannedUpdate | str:
    """`PlannedUpdate` albo komunikat błędu (tekst po „id: ”) dla jednego wpisu z pliku."""
    language = localize.language_for(row["country"])
    if language is None:
        return f"kraj „{row['country']}” bez szablonu językowego — ustaw kraj albo dodaj język w src/perno/domain/localize.py"
    email = entry["email"].strip()
    if email.lower() not in scraped:
        return f"adres {email} nie występuje wśród kontaktów ze skanu"
    texts = [entry["subject"], entry["obs"], entry["pitch"], entry["channel_summary"]]
    if language != "pl" and localize.looks_polish(*texts):
        return f"treść ma polskie litery, a mail ma być po {localize.LANGUAGE_NAMES[language]}"
    seed = {"mode": entry.get("mode") or "Panstwo", "obs": entry["obs"].strip(), "pitch": entry["pitch"].strip(),
            "channel_summary": entry["channel_summary"].strip(), "subject": entry["subject"].strip()}
    try:
        subject, body_html, body_text, inline_images = render_row(
            row["segment"], seed, _read_template(row["segment"], language), sender_name)
    except templating.MissingTokenError as exc:
        return str(exc)
    values = {"email": email, "subject": subject, "body_html": body_html, "body_text": body_text,
              "inline_images": inline_images, "mode": seed["mode"], "tag": (entry.get("tag") or "").strip(),
              "contact_person": (entry.get("contact_person") or "").strip()}
    for key in OPTIONAL_PROFILE:  # dane o firmie tylko uzupełniamy, nie nadpisujemy
        if entry.get(key) and not (row[key] or "").strip():
            values[key] = entry[key].strip()
    return PlannedUpdate(row["id"], row["company"], language, values)


def plan_updates(store: Store, entries: list, sender_name: str, include_pending: bool = False,
                 include_partial: bool = False) -> EnrichmentPlan:
    """Waliduje plik z treściami i przygotowuje zapis. Nic nie zmienia w bazie (tylko odczyt)."""
    by_id = {row["id"]: row for row in list_candidates(store, include_pending, include_partial)}
    plan, seen = EnrichmentPlan(), set()
    scraped_cache: dict = {}
    for entry in entries:
        row_id = entry.get("id", "?")
        missing = _missing_fields(entry)
        if missing:
            plan.errors.append(f"{row_id}: brak pól: {', '.join(missing)}")
            continue
        if row_id in seen:
            plan.errors.append(f"{row_id}: powtórzony id w pliku")
            continue
        seen.add(row_id)
        row: Optional[Mapping] = by_id.get(row_id)
        if row is None:
            plan.errors.append(f"{row_id}: nie kwalifikuje się (brak wiersza, skan niezakończony albo status inny niż "
                               "„Brak e-maila”/„Czeka na treść”)")
            continue
        firma_id = row["firma_id"]
        if firma_id not in scraped_cache:
            scraped_cache[firma_id] = {found["wartosc"].lower() for found in store.scraped_emails(firma_id)}
        outcome = _plan_entry(entry, row, scraped_cache[firma_id], sender_name)
        if isinstance(outcome, str):
            plan.errors.append(f"{row_id}: {outcome}")
        else:
            plan.items.append(outcome)
    return plan


def write_updates(db: Database, plan: EnrichmentPlan, include_pending: bool = False) -> int:
    """Zapisuje przygotowane treści; wiersze, które w międzyczasie zmieniły status (np. zostały wysłane), pomija.
    Zwraca liczbę zapisanych wierszy. Kopię zapasową tabeli robi wywołujący przed zapisem."""
    return db.apply_enrichment([(item.row_id, item.values) for item in plan.items], allowed_statuses(include_pending))
