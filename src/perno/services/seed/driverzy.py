"""Ekstrakcja danych Driverzy z 'PERNO - GTM - Driverzy .csv', tylko segment
ICP3 (50 polskich firm przewozowych) — ICP4 (property-management SaaS) jest
pomijane całkowicie w tej wersji programu, zgodnie z decyzją z planowania.

Treść (obs/pitch/channel_summary) jest generowana MECHANICZNIE z kolumn
arkusza, nie ręcznie pisana jak dla Eventy — stąd zauważalnie prostsza i
bardziej ogólna niż wzorzec Eventy. Kolumna fit_confidence sygnalizuje,
gdzie warto rzucić okiem przed wysyłką.
"""

from __future__ import annotations

import csv
import glob
import os
import re

COL_NAZWA = 0
COL_EMAIL = 1
COL_STRONA = 2
COL_UWAGI = 5
COL_CO_SPRZEDAJE = 7
COL_JAK_REZERWUJE = 8
COL_TRANSFER_LOTNISKOWY = 9
COL_ROUND_TRIP = 10
COL_BEZPOSREDNIA_REZERWACJA = 11
COL_PLATNOSC_ONLINE = 12
COL_CROSS_SELL = 14


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


def _clean(s: str) -> str:
    s = s.strip().rstrip(".")
    # Uniknij podwójnego "przez" w zdaniach szablonu (np. źródłowe "Online
    # przez platformę X" wstawione w "rezerwacje przyjmujecie Państwo przez
    # {channel}" dałoby "...przez online przez platformę X").
    return re.sub(r"(?i)^online\s+przez\s+", "", s)


def read_driverzy_csv(path: str) -> list[list[str]]:
    """Wiersze arkusza Driverzy bez nagłówka."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    return rows[1:]  # bez nagłówka


def classify_icp(uwagi: str) -> str:
    """Segment ICP z kolumny uwag („ICP3”, „ICP4” albo UNKNOWN)."""
    if "ICP 3" in uwagi:
        return "ICP3"
    if "ICP 4" in uwagi:
        return "ICP4"
    return "UNKNOWN"


def generate_pitch(booking_channel: str, pay_online: str) -> tuple[str, str, str, str]:
    """Zwraca (obs, pitch, channel_summary, fit_confidence) na podstawie
    kolumn 'jak rezerwuje' i 'czy można zapłacić online'."""
    channel = _lower_first(_clean(booking_channel)) or "formularz, telefon lub e-mail"
    pay_online = pay_online.strip().upper()

    if pay_online == "NIE":
        obs = (
            f"Szukałem firm oferujących przejazdy z kierowcą. Zauważyłem, że rezerwacje "
            f"przyjmujecie Państwo przez {channel}, ale nie widziałem możliwości zapłaty "
            f"online za przejazd. Czy dziś rozliczenie odbywa się gotówką lub przelewem po kursie?"
        )
        pitch = "wprowadzić płatność online od razu przy rezerwacji przejazdu"
        fit = "high"
    elif pay_online == "TAK":
        obs = (
            f"Szukałem firm oferujących przejazdy z kierowcą. Zauważyłem, że rezerwacje "
            f"przyjmujecie Państwo przez {channel} z opcją płatności online."
        )
        pitch = "[NISKIE DOPASOWANIE — firma już ma płatność online; przejrzyj ręcznie przed wysyłką]"
        fit = "low"
    else:  # "DO WERYFIKACJI" lub inna/pusta wartość
        obs = (
            f"Szukałem firm oferujących przejazdy z kierowcą. Zauważyłem, że rezerwacje "
            f"przyjmujecie Państwo przez {channel}. Czy klient może opłacić przejazd online "
            f"od razu przy rezerwacji, czy rozliczenie odbywa się już na miejscu z kierowcą?"
        )
        pitch = "dodać możliwość płatności online od razu przy rezerwacji"
        fit = "medium"

    channel_summary = f"ręcznie potwierdzać każdą rezerwację przez {channel}"
    return obs, pitch, channel_summary, fit


def find_dossier(dossiers_dir: str, row_number: int) -> str:
    """Ścieżka dossier firmy o danym numerze wiersza (pusta, gdy go nie ma)."""
    matches = glob.glob(os.path.join(dossiers_dir, f"{row_number:03d}_*.md"))
    return matches[0] if matches else ""


def build_icp3_records(csv_path: str, dossiers_dir: str) -> list[dict]:
    """Rekordy segmentu ICP3 z arkusza: dane firmy i mechanicznie wygenerowana treść."""
    records = []
    for row_number, row in enumerate(read_driverzy_csv(csv_path), start=1):
        if classify_icp(row[COL_UWAGI]) != "ICP3":
            continue
        booking_channel = row[COL_JAK_REZERWUJE]
        pay_online = row[COL_PLATNOSC_ONLINE]
        obs, pitch, channel_summary, fit = generate_pitch(booking_channel, pay_online)
        name = row[COL_NAZWA].strip()
        records.append({
            "row_number": row_number,
            "company_display_name": name,
            "website": row[COL_STRONA].strip(),
            "email": row[COL_EMAIL].strip(),
            "icp_segment": "ICP3",
            "what_they_sell": row[COL_CO_SPRZEDAJE].strip(),
            "booking_channel_text": booking_channel.strip(),
            "airport_transfer": row[COL_TRANSFER_LOTNISKOWY].strip(),
            "round_trip": row[COL_ROUND_TRIP].strip(),
            "direct_booking": row[COL_BEZPOSREDNIA_REZERWACJA].strip(),
            "pay_online": pay_online.strip(),
            "cross_sell_text": row[COL_CROSS_SELL].strip(),
            "mode": "Panstwo",
            "subject": f"Płatność online przy rezerwacji — {name}",
            "obs": obs,
            "pitch": pitch,
            "channel_summary": channel_summary,
            "fit_confidence": fit,
            "source_dossier_file": os.path.basename(find_dossier(dossiers_dir, row_number)),
        })
    return records
