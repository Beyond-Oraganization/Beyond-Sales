#!/usr/bin/env python3
"""Buduje data/eventy.csv i data/driverzy.csv ze źródeł:
  - Eventy: tablica JS w Maile_GTM_Eventy/index.html + dossier w Firmy/Eventy/*.md
  - Driverzy: PERNO - GTM - Driverzy .csv, tylko segment ICP3

Oba pliki wyjściowe są w pełni odtwarzane przy każdym uruchomieniu — nie są
miejscem do ręcznej edycji treści. Treść Eventy edytuje się w tablicy JS
`index.html` (skąd ten skrypt ją weźmie przy następnym przebiegu); treść
Driverzy — w templates/driverzy.html.

Uruchom ponownie, gdy folder Firmy/Eventy urośnie o kolejne dossier.
"""

from __future__ import annotations

import argparse
import csv
import os
from collections import Counter
from typing import Optional

from perno import settings as cfg
from perno.services.seed import driverzy as de
from perno.services.seed import eventy as ee

# Źródła leżą obok katalogu projektu (w ~/Desktop/Perno), wyniki w data/ projektu.
EVENTY_HTML = os.path.join(cfg.PERNO_DIR, "Maile_GTM_Eventy", "index.html")
EVENTY_DOSSIERS_DIR = os.path.join(cfg.PERNO_DIR, "Firmy", "Eventy")
DRIVERZY_CSV = os.path.join(cfg.PERNO_DIR, "PERNO - GTM - Driverzy .csv")
DRIVERZY_DOSSIERS_DIR = os.path.join(cfg.PERNO_DIR, "Firmy", "Driverzy")

EVENTY_OUT = cfg.EVENTY_SEED
DRIVERZY_OUT = cfg.DRIVERZY_SEED

EVENTY_COLUMNS = [
    "row_number", "company_display_name", "name_suffix_raw",
    "contact_person_guess", "contact_person_confidence",
    "website", "email", "email_source", "tag", "mode",
    "subject", "obs", "pitch", "channel_summary",
    "already_customer", "already_customer_note", "needs_manual_copy",
    "source_dossier_file",
]

DRIVERZY_COLUMNS = [
    "row_number", "company_display_name", "website", "email", "icp_segment",
    "what_they_sell", "booking_channel_text", "airport_transfer",
    "round_trip", "direct_booking", "pay_online", "cross_sell_text",
    "mode", "subject", "obs", "pitch", "channel_summary", "fit_confidence",
    "source_dossier_file",
]


def _dossier_file(dossiers: dict, row_number) -> str:
    return os.path.basename(dossiers[row_number]["path"]) if row_number else ""


def _customer_row(company: dict, display_name: str, suffix: str, row_number, dossiers: dict) -> dict:
    """Firma, która już jest klientem: wiersz informacyjny, bez treści maila."""
    return {
        "row_number": row_number or "",
        "company_display_name": display_name,
        "name_suffix_raw": suffix,
        "contact_person_guess": "",
        "contact_person_confidence": "none",
        "website": company["website"],
        "email": "",
        "email_source": "",
        "tag": company["tag"],
        "mode": "",
        "subject": "", "obs": "", "pitch": "", "channel_summary": "",
        "already_customer": "true",
        "already_customer_note": company["note"],
        "needs_manual_copy": "false",
        "source_dossier_file": _dossier_file(dossiers, row_number),
    }


def _prospect_row(company: dict, display_name: str, suffix: str, row_number, dossiers: dict) -> dict:
    """Firma z treścią w tablicy JS; brakujący e-mail uzupełniamy z karty firmy w dossier."""
    contact_guess, contact_conf = ee.guess_contact_person(suffix)
    email, email_source = company["email"], "js"
    dossier_text = ee.read_dossier_text(dossiers[row_number]["path"]) if row_number else ""
    if not email and dossier_text:
        backfilled = ee.extract_company_card_email(dossier_text)
        if backfilled:
            email, email_source = backfilled, "dossier"
    if not email:
        email_source = "none"
    return {
        "row_number": row_number or "",
        "company_display_name": display_name,
        "name_suffix_raw": suffix,
        "contact_person_guess": contact_guess,
        "contact_person_confidence": contact_conf,
        "website": company["website"],
        "email": email,
        "email_source": email_source,
        "tag": company["tag"],
        "mode": company["mode"],
        "subject": company["subject"],
        "obs": company["obs"],
        "pitch": company["pitch"],
        "channel_summary": company["channel_summary"],
        "already_customer": "false",
        "already_customer_note": "",
        "needs_manual_copy": "false",
        "source_dossier_file": _dossier_file(dossiers, row_number),
    }


def _dossier_only_row(row_number: int, info: dict) -> dict:
    """Dossier bez wpisu w tablicy JS (brak ręcznie napisanej treści): „wymaga uzupełnienia treści”."""
    dossier_text = ee.read_dossier_text(info["path"])
    display_name = ee.split_name(ee.extract_h1_name(dossier_text) or info["slug_norm"].title())[0]
    email = ee.extract_company_card_email(dossier_text)
    return {
        "row_number": row_number,
        "company_display_name": display_name,
        "name_suffix_raw": "",
        "contact_person_guess": "",
        "contact_person_confidence": "none",
        "website": ee.extract_company_card_website(dossier_text),
        "email": email,
        "email_source": "dossier" if email else "none",
        "tag": "",
        "mode": "Panstwo",
        "subject": "", "obs": "", "pitch": "", "channel_summary": "",
        "already_customer": "false",
        "already_customer_note": "",
        "needs_manual_copy": "true",
        "source_dossier_file": os.path.basename(info["path"]),
    }


def build_eventy_rows() -> list[dict]:
    with open(EVENTY_HTML, encoding="utf-8") as handle:
        html_text = handle.read()
    companies = ee.parse_js_companies(html_text)
    dossiers = ee.list_dossiers(EVENTY_DOSSIERS_DIR)

    rows = []
    matched_dossier_rows = set()
    for company in companies:
        display_name, suffix = ee.split_name(company["name"])
        row_number = ee.match_dossier(company["name"], dossiers)
        if row_number:
            matched_dossier_rows.add(row_number)
        build = _customer_row if company["already_customer"] else _prospect_row
        rows.append(build(company, display_name, suffix, row_number, dossiers))

    rows.extend(_dossier_only_row(row_number, info) for row_number, info in sorted(dossiers.items())
                if row_number not in matched_dossier_rows)
    rows.sort(key=lambda r: (r["row_number"] == "", r["row_number"]))
    return rows


def write_csv(path: str, columns: list[str], rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)


def main(argv: Optional[list] = None) -> None:
    build_parser().parse_args(argv)  # bez opcji; chroni przed uruchomieniem budowy przy `--help`
    eventy_rows = build_eventy_rows()
    write_csv(EVENTY_OUT, EVENTY_COLUMNS, eventy_rows)

    driverzy_rows = de.build_icp3_records(DRIVERZY_CSV, DRIVERZY_DOSSIERS_DIR)
    write_csv(DRIVERZY_OUT, DRIVERZY_COLUMNS, driverzy_rows)

    prospects = [r for r in eventy_rows if r["already_customer"] == "false"]
    ready = [r for r in prospects if r["needs_manual_copy"] == "false"]
    with_email = [r for r in ready if r["email"]]
    no_email = [r for r in ready if not r["email"]]
    awaiting_copy = [r for r in prospects if r["needs_manual_copy"] == "true"]
    customers = [r for r in eventy_rows if r["already_customer"] == "true"]

    print(f"=== Eventy ({EVENTY_OUT}) ===")
    print(f"  Razem wierszy: {len(eventy_rows)}")
    print(f"  Już klienci (pomijane): {len(customers)}")
    print(f"  Gotowe do wysyłki (mają treść): {len(ready)}")
    print(f"    - z e-mailem: {len(with_email)}")
    print(f"    - BEZ e-maila (status pominieto_brak_email): {len(no_email)}")
    if no_email:
        print(f"      -> {', '.join(r['company_display_name'] for r in no_email)}")
    print(f"  Czekają na ręcznie napisaną treść (oczekuje_na_tresc): {len(awaiting_copy)}")
    if awaiting_copy:
        print(f"      -> {', '.join(r['company_display_name'] for r in awaiting_copy)}")
    low_conf = [r for r in ready if r["contact_person_confidence"] == "low"]
    if low_conf:
        print(f"  Niepewna osoba kontaktowa (warto sprawdzić ręcznie): {len(low_conf)}")
        print(f"      -> {', '.join(r['company_display_name'] for r in low_conf)}")

    fit_counts = Counter(r["fit_confidence"] for r in driverzy_rows)
    print(f"\n=== Driverzy ICP3 ({DRIVERZY_OUT}) ===")
    print(f"  Razem wierszy: {len(driverzy_rows)}")
    print(f"  Bez e-maila: {sum(1 for r in driverzy_rows if not r['email'])}")
    print(f"  fit_confidence: high={fit_counts['high']}, medium={fit_counts['medium']}, low={fit_counts['low']}")
    low_fit = [r for r in driverzy_rows if r["fit_confidence"] == "low"]
    if low_fit:
        print("  Niskie dopasowanie (już mają płatność online, przejrzyj przed wysyłką):")
        print(f"      -> {', '.join(r['company_display_name'] for r in low_fit)}")


if __name__ == "__main__":
    main()
