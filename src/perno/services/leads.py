"""Leady w panelu: lista, widok, zapis pól, zatwierdzanie pól AI, cofanie i eksport (usługa nad magazynem, bez SQL).

Każda zmiana zwraca poprzednie wartości pól razem z ich pochodzeniem (`previous`), więc panel może ją cofnąć. Edytuje tylko
człowiek; AI ma własną ścieżkę (`services/ai_leads`). Lista sama zakłada brakujące leady (`LeadStore.sync`), więc ocena
wątku na pozytywną albo neutralną wystarcza, żeby firma pojawiła się w zakładce.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any, Mapping

from perno.db.leads_store import LeadStore
from perno.domain import lead_listing as ll
from perno.domain import lead_rules as lr
from perno.domain import replies as rp
from perno.domain.reply_rules import RuleError
from perno.services.export import CSV_DANGEROUS
from perno.services.replies import iso

_DATES = ("created_at", "updated_at", "last_in_at")
EXPORT_HEADER = ("Firma", "Strona", "Kontakt", "Telefon", "E-mail", "Czym się zajmuje", "Etap", "Ocena", "Segment", "Notatka", "Uzupełnił AI")
BY_LABEL = {lr.BY_AI: "AI", lr.BY_HUMAN: "człowiek"}


class LeadNotFound(LookupError):
    """Nie ma takiego leada."""


def _row(row: Mapping) -> dict:
    """Wiersz leada w JSON: daty jako tekst, etykieta etapu, a wątki (jeśli są) w skróconej postaci do widoku."""
    out: dict[str, Any] = {key: iso(value) if key in _DATES else value for key, value in row.items() if key != "threads"}
    out["stage_label"] = lr.STAGE_LABELS.get(row["stage"], row["stage"])
    if "threads" in row:
        out["threads"] = [{**{key: iso(value) for key, value in thread.items() if key != "company_id"}, "optout": bool(thread["optout"]),
                           "awaiting_reply": bool(thread["awaiting_reply"])} for thread in row["threads"]]
    return out


def list_leads(store: LeadStore, payload: Mapping, now: datetime) -> dict:
    """Strona wybranej karty z licznikami wszystkich kart: `{rows, total, page, size, pages, counts, tab, segments}`."""
    request = ll.parse_list_request(payload)
    store.sync(now)
    rows, total = store.list_page(request.flt, request.page, request.size)
    return {"tab": request.flt.tab, "rows": [_row(row) for row in rows], "total": total, "page": request.page, "size": request.size,
            "pages": max(1, -(-total // request.size)), "counts": store.tab_counts(request.flt), "segments": store.segments()}


def lead_view(store: LeadStore, company_id: str) -> dict:
    """Jeden lead z wątkami, z których powstał."""
    lead = store.get(company_id)
    if lead is None:
        raise LeadNotFound(f"Nie ma leada {company_id}.")
    return {"lead": _row(lead)}


def save(store: LeadStore, company_id: str, changes: object, now: datetime) -> dict:
    """Zapis pól przez człowieka. Zwraca zapisany lead i `previous` (do „Cofnij”)."""
    result = store.update_fields(company_id, lr.check_changes(changes, by=lr.BY_HUMAN), by=lr.BY_HUMAN, now=now)
    if result is None:
        raise LeadNotFound(f"Nie ma leada {company_id}.")
    return {**lead_view(store, company_id), "previous": result["previous"]}


def approve(store: LeadStore, company_id: str, fields: object, now: datetime) -> dict:
    """Człowiek zatwierdza pola wpisane przez AI (`fields`: lista nazw pól). Zwraca lead, zatwierdzone pola i `previous`."""
    if not isinstance(fields, list) or not all(isinstance(name, str) for name in fields) or not fields:
        raise RuleError("Pole fields musi być niepustą listą nazw pól.")
    unknown = [name for name in fields if name not in lr.TEXT_FIELDS]
    if unknown:
        raise RuleError(f"Nieznane pola: {', '.join(unknown)}.")
    result = store.approve(company_id, fields, now)
    if result is None:
        raise LeadNotFound(f"Nie ma leada {company_id}.")
    return {**lead_view(store, company_id), "previous": result["previous"], "approved": result["approved"]}


def restore(store: LeadStore, company_id: str, previous: object, now: datetime) -> dict:
    """Cofnięcie zapisu albo zatwierdzenia: przywraca wartości i pochodzenie pól z `previous` (kształt jak z `save`)."""
    if not isinstance(previous, Mapping) or not previous:
        raise RuleError("Brak danych do cofnięcia.")
    clean: dict = {}
    for name, old in previous.items():
        if name not in lr.EDITABLE_FIELDS or not isinstance(old, Mapping) or old.get("src") not in (None, lr.BY_AI, lr.BY_HUMAN):
            raise RuleError("Dane do cofnięcia mają zły kształt.")
        value = old.get("value")
        clean[name] = {"value": lr.check_field(name, value) if name == lr.STAGE or value else None, "src": old.get("src")}
    if not store.restore_fields(company_id, clean, now):
        raise LeadNotFound(f"Nie ma leada {company_id}.")
    return lead_view(store, company_id)


def _safe(value: Any) -> Any:
    return "'" + value if isinstance(value, str) and value.startswith(CSV_DANGEROUS) else value


def export_csv(store: LeadStore, payload: Mapping, now: datetime) -> str:
    """Leady wybranej karty i filtrów jako CSV (średnik, UTF-8 z BOM: Excel); komórki z formułami zabezpieczone apostrofem."""
    flt = ll.parse_filter(payload)
    store.sync(now)
    rows, _ = store.list_page(flt, 1, ll.EXPORT_LIMIT)
    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(EXPORT_HEADER)
    for row in rows:
        writer.writerow([_safe(cell) for cell in (
            row["name"], row["website"], row["contact"], row["phone"], row["email"], row["about"], lr.STAGE_LABELS.get(row["stage"], row["stage"]),
            rp.RATING_LABELS.get(row["rating"], ""), row["segment"], row["note"], ", ".join(lr.LABELS[name] for name in row["pending"]))])
    return out.getvalue()
