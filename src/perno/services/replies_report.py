"""Raport odpowiedzi: składanie liczb z bazy w raport (JSON) oraz eksport do CSV i Markdown.

Liczy baza (`db/replies_report`), usługa dodaje pokrycie odbioru (z których skrzynek faktycznie pobieramy pocztę), czas do
odpowiedzi i ostrzeżenia, a `domain/reply_stats` zamienia wynik w tekst. W raporcie nie ma adresów ani treści wiadomości.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any, Mapping, Optional

from perno import settings as cfg
from perno.db.replies_store import ReplyStore
from perno.domain import reply_listing as rl
from perno.domain import reply_stats as rs
from perno.services.export import CSV_DANGEROUS

GROUP_ORDER = rs.GROUPS


def _period(flt: rl.ReportFilter, first_day: Optional[date]) -> str:
    start = flt.since or first_day
    return f"{start.isoformat() if start else 'od początku'} – {flt.until.isoformat() if flt.until else 'dziś'}"


def _failing(store: ReplyStore) -> list:
    return [state["mailbox"] for state in store.mailbox_states() if state["error"]]


def build_report(store: ReplyStore, config: Mapping, payload: Mapping, now: Optional[datetime] = None) -> dict:
    """Raport dla wybranego okresu i zawężeń: całość, zestawienia (segment, kraj, skrzynka, dzień), czas reakcji, pokrycie odbioru."""
    flt = rl.parse_report_request(payload)
    primary = (config.get("SMTP_USER") or "").strip().lower()
    raw = store.report_data(flt, primary, GROUP_ORDER)
    total = rs.row_from_sql("razem", raw["total"])
    configured = [box.user for box in cfg.configured_mailboxes(config)]
    receiving = [slot.address for slot in _receiving(config)]
    coverage = rs.coverage(configured, receiving, _failing(store), raw["usage"])
    warnings = _warnings(total, coverage, raw["total"])
    return {
        "period": _period(flt, raw["first_day"]), "filter": {"from": flt.since.isoformat() if flt.since else "", "to": flt.until.isoformat() if flt.until else "",
                                                             "segments": list(flt.segments), "countries": list(flt.countries), "mailboxes": list(flt.mailboxes)},
        "total": total.to_json(), "replied_companies": int(raw["total"].get("replied_companies") or 0), "auto_threads": int(raw["total"].get("auto_threads") or 0),
        "soft_bounces": int(raw["total"].get("soft_bounces") or 0),
        "awaiting_since": raw["total"].get("awaiting_since").strftime("%Y-%m-%dT%H:%M:%S") if isinstance(raw["total"].get("awaiting_since"), datetime) else None,
        "groups": {group: [rs.row_from_sql(row["key"], row).to_json() for row in rows] for group, rows in raw["groups"].items()},
        "latency": rs.latency_stats(raw["latency"]), "coverage": coverage, "warnings": warnings,
        "generated_at": (now or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _receiving(config: Mapping) -> list:
    boxes = []
    for slot in cfg.imap_slot_numbers(config):
        try:
            boxes.append(cfg.ImapSettings.from_config(config, slot))
        except cfg.ConfigError:
            continue
    return boxes


def _warnings(total: rs.StatsRow, coverage: Mapping, raw_total: Mapping) -> list:
    warnings = []
    if not coverage["complete"]:
        warnings.append(f"Odsetek odpowiedzi jest zaniżony: {coverage['blind_mails']} maili wyszło ze skrzynek, z których nie pobieramy poczty "
                        f"(brak IMAP albo błąd pobierania): {', '.join(coverage['blind_mailboxes'][:5])}"
                        f"{' i inne' if len(coverage['blind_mailboxes']) > 5 else ''}.")
    if total.sent and total.small_sample:
        warnings.append(f"Mała próba: dostarczono {total.delivered} maili (mniej niż {rs.SMALL_SAMPLE}), więc odsetki są niepewne.")
    if int(raw_total.get("auto_threads") or 0):
        warnings.append("Autoodpowiedzi nie są liczone jako odpowiedzi.")
    return warnings


def markdown(report: Mapping) -> str:
    """Raport jako Markdown (do skopiowania do notatek lub wiadomości)."""
    return rs.to_markdown(report)


def csv_text(report: Mapping) -> str:
    """Raport jako CSV (średnik, UTF-8 z BOM: Excel). Komórki zaczynające się od znaków formuł są zabezpieczone apostrofem."""
    def safe(value: Any) -> Any:
        return "'" + value if isinstance(value, str) and value.startswith(CSV_DANGEROUS) else value

    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    for row in rs.to_csv_rows(report):
        writer.writerow([safe(cell) for cell in row])
    return out.getvalue()
