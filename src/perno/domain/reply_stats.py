"""Statystyki odpowiedzi: wiersze zestawień, odsetki, czas do odpowiedzi, pokrycie odbioru i zapis raportu (czysta logika).

Jednostką jest **mail kampanii** (jeden adres, jeden wątek). „Odpowiedź” to wątek z co najmniej jedną wiadomością człowieka;
autoodpowiedzi i zwroty liczą się osobno. Odsetek odpowiedzi to odpowiedzi / dostarczone (wysłane bez twardych zwrotów).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Iterable, Mapping, Optional, Sequence

SMALL_SAMPLE = 20  # poniżej tylu dostarczonych maili odsetek jest za mało pewny, żeby na nim polegać
LATENCY_BUCKETS = (("do 1 dnia", 1), ("2–3 dni", 3), ("4–7 dni", 7), ("powyżej 7 dni", None))
GROUPS = ("segment", "country", "mailbox", "day")
GROUP_LABELS = {"segment": "Segment", "country": "Kraj", "mailbox": "Skrzynka", "day": "Dzień wysyłki"}


def ratio(part: int, whole: int) -> Optional[float]:
    """Udział w [0, 1]; None, gdy nie ma z czego liczyć (zero w mianowniku to „brak danych”, nie 0%)."""
    return part / whole if whole > 0 else None


@dataclass(frozen=True)
class StatsRow:
    """Jedna linia zestawienia (całość albo grupa): ile wysłano, ile wróciło zwrotów, ile odpowiedzi i jakiej oceny."""

    key: str
    sent: int = 0
    bounced: int = 0  # twarde zwroty (status „Zwrot”)
    replied: int = 0
    positive: int = 0
    neutral: int = 0
    negative: int = 0
    unrated: int = 0
    optout: int = 0
    awaiting: int = 0  # odpowiedzi czekające na naszą odpowiedź (nie zamknięte)

    @property
    def delivered(self) -> int:
        return max(0, self.sent - self.bounced)

    @property
    def reply_rate(self) -> Optional[float]:
        return ratio(self.replied, self.delivered)

    @property
    def positive_rate(self) -> Optional[float]:
        """Udział pozytywnych wśród ocenionych odpowiedzi (nieocenione nie zaniżają ani nie zawyżają)."""
        return ratio(self.positive, self.positive + self.neutral + self.negative)

    @property
    def small_sample(self) -> bool:
        return self.delivered < SMALL_SAMPLE

    def to_json(self) -> dict:
        return {"key": self.key, "sent": self.sent, "bounced": self.bounced, "delivered": self.delivered, "replied": self.replied,
                "positive": self.positive, "neutral": self.neutral, "negative": self.negative, "unrated": self.unrated, "optout": self.optout,
                "awaiting": self.awaiting, "reply_rate": self.reply_rate, "positive_rate": self.positive_rate, "small_sample": self.small_sample}


def row_from_sql(key: object, values: Mapping) -> StatsRow:
    """Wiersz z wyniku SQL (liczby mogą być None albo Decimal po SUM na MySQL)."""
    def number(name: str) -> int:
        return int(values.get(name) or 0)

    return StatsRow(str(key if key is not None and key != "" else "brak"), number("sent"), number("bounced"), number("replied"),
                    number("positive"), number("neutral"), number("negative"), number("unrated"), number("optout"), number("awaiting"))


def latency_stats(pairs: Iterable[tuple]) -> dict:
    """Czas od wysłania maila do pierwszej odpowiedzi człowieka: liczba, mediana w godzinach i rozkład po przedziałach. Pary
    z odpowiedzią przed wysyłką (zegar, przesunięcie `sent_at`) liczą się jako zero, nie jako ujemny czas."""
    hours = []
    for sent, replied in pairs:
        if isinstance(sent, datetime) and isinstance(replied, datetime):
            hours.append(max(0.0, (replied - sent).total_seconds() / 3600))
    buckets = {label: 0 for label, _ in LATENCY_BUCKETS}
    for value in hours:
        days = value / 24
        label = next((name for name, limit in LATENCY_BUCKETS if limit is None or days <= limit), LATENCY_BUCKETS[-1][0])
        buckets[label] += 1
    return {"n": len(hours), "median_hours": round(median(hours), 1) if hours else None, "buckets": buckets}


def coverage(configured: Sequence[str], receiving: Sequence[str], failing: Sequence[str], used: Mapping[str, int]) -> dict:
    """Pokrycie odbioru: z których skrzynek, z jakich wysyłano, faktycznie pobieramy pocztę. `used` to liczba maili wysłanych z
    każdej skrzynki w okresie raportu. Maile ze skrzynek bez IMAP albo z błędem pobierania zaniżają odsetek odpowiedzi."""
    receiving_ok = {box.lower() for box in receiving} - {box.lower() for box in failing}
    blind = {box: count for box, count in used.items() if box.lower() not in receiving_ok and count > 0}
    return {"configured": len(configured), "receiving": len(receiving_ok & {box.lower() for box in configured}),
            "blind_mailboxes": sorted(blind), "blind_mails": sum(blind.values()), "complete": not blind}


def adjust_for_coverage(total: StatsRow, blind_mails: int, blind_bounced: int = 0) -> Optional[float]:
    """Odsetek odpowiedzi tylko dla maili ze skrzynek objętych odbiorem (druga wartość obok ogólnej)."""
    delivered = total.delivered - max(0, blind_mails - blind_bounced)
    return ratio(total.replied, delivered)


def percent(value: Optional[float]) -> str:
    """Odsetek do wyświetlenia („12,5%”); „—” bez danych."""
    return "—" if value is None else f"{value * 100:.1f}".replace(".", ",") + "%"


def to_markdown(report: Mapping) -> str:
    """Raport jako tekst Markdown do wklejenia (podsumowanie, ostrzeżenia, zestawienia); bez adresów osób trzecich."""
    total, period = report["total"], report["period"]
    lines = [f"# Raport odpowiedzi ({period})", "",
             f"- Wysłano maili: {total['sent']}, dostarczono: {total['delivered']} (twarde zwroty: {total['bounced']})",
             f"- Odpowiedzi człowieka: {total['replied']} ({percent(total['reply_rate'])} dostarczonych), firm: {report['replied_companies']}",
             f"- Oceny: pozytywne {total['positive']}, neutralne {total['neutral']}, negatywne {total['negative']}, nieocenione {total['unrated']}",
             f"- Prośby o wypisanie: {total['optout']}, czeka na naszą odpowiedź: {total['awaiting']}",
             f"- Autoodpowiedzi: {report['auto_threads']}, zwroty czasowe: {report['soft_bounces']}"]
    latency = report["latency"]
    if latency["n"]:
        lines.append(f"- Czas do pierwszej odpowiedzi: mediana {latency['median_hours']} h (n={latency['n']})")
    if report.get("warnings"):
        lines += ["", "## Uwagi", *(f"- {text}" for text in report["warnings"])]
    for group in GROUPS:
        rows = report["groups"].get(group) or []
        if not rows:
            continue
        lines += ["", f"## {GROUP_LABELS[group]}", "", "| | wysłano | dostarczono | odpowiedzi | odsetek | pozytywne | neutralne | negatywne |", "|---|---|---|---|---|---|---|---|"]
        lines += [f"| {row['key']}{' (mała próba)' if row['small_sample'] else ''} | {row['sent']} | {row['delivered']} | {row['replied']} | "
                  f"{percent(row['reply_rate'])} | {row['positive']} | {row['neutral']} | {row['negative']} |" for row in rows]
    return "\n".join(lines) + "\n"


CSV_HEADER = ("zestawienie", "grupa", "wysłano", "twarde zwroty", "dostarczono", "odpowiedzi", "odsetek odpowiedzi", "pozytywne", "neutralne",
              "negatywne", "nieocenione", "prośby o wypisanie", "czeka na odpowiedź", "mała próba")


def to_csv_rows(report: Mapping) -> list:
    """Wiersze CSV: pierwszy to nagłówek, potem całość i każda grupa każdego zestawienia."""
    def line(label: str, row: Mapping) -> list:
        return [label, row["key"], row["sent"], row["bounced"], row["delivered"], row["replied"], percent(row["reply_rate"]), row["positive"],
                row["neutral"], row["negative"], row["unrated"], row["optout"], row["awaiting"], "tak" if row["small_sample"] else "nie"]

    rows = [list(CSV_HEADER), line("razem", report["total"])]
    for group in GROUPS:
        rows += [line(GROUP_LABELS[group], row) for row in report["groups"].get(group) or []]
    return rows
