"""Kolejka automatu widziana jako firmy: ile maili czeka, ile firm, ile wstrzymanych i odroczonych odstępem oraz prognoza.

Jedno miejsce dla podglądu przed Startem, dziennego podsumowania i komunikatu o zakończeniu planu, żeby wszędzie liczby
brały się z tych samych zapytań i tej samej symulacji (`automat_rules.estimate_finish_grouped`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional

from perno.db import Database
from perno.domain import automat_rules as ar
from perno.domain import company_rules
from perno.domain.listing import DEFAULT_SORT
from perno.domain.rowfilter import RowFilter


@dataclass(frozen=True)
class QueueForecast:
    """Stan kolejki i prognoza (pola opisane przy każdym z nich)."""

    queue: int  # maile „Do wysłania” (z adresem i treścią) w ustawionym segmencie i krajach, bez wstrzymanych blokadą firmy
    companies: int  # ile firm ma te maile
    held: int  # takie maile wstrzymane statusem innego adresu firmy (nie wchodzą do kolejki)
    deferred_today: int  # maile firm, do których odstęp każe dziś nie pisać
    gap_days: int
    estimate: ar.FinishEstimate


def companies_in_gap(db: Database, settings: ar.AutomatSettings, now: datetime) -> dict:
    """`{id firmy: dzień ostatniego maila}` dla firm, do których odstęp (`company_gap_days`) każe dziś nie pisać; przy
    odstępie 0 pusty słownik."""
    cutoff = company_rules.gap_cutoff(now.date(), settings.company_gap_days)
    return db.last_sent_by_company(cutoff) if cutoff is not None else {}


def next_filter(db: Database, settings: ar.AutomatSettings, now: datetime) -> RowFilter:
    """Filtr „co można wysłać teraz”: kolejka bez firm z odstępu."""
    return ar.queue_filter(settings, tuple(companies_in_gap(db, settings, now)))


def forecast(db: Database, settings: ar.AutomatSettings, now: datetime, sent_today: int,
             until: Optional[datetime] = None) -> QueueForecast:
    """Liczby kolejki i prognoza zakończenia przy ustawionych limitach i odstępie."""
    base = ar.queue_filter(settings)
    queue = db.count_matching(base)
    last = companies_in_gap(db, settings, now)
    if settings.company_gap_days > 0 and queue:
        estimate = ar.estimate_finish_grouped(now, settings, db.queue_company_ids(base, DEFAULT_SORT), sent_today, until, last)
    else:
        estimate = ar.estimate_finish(now, settings, queue, sent_today, until)
    held = db.count_matching(replace(base, unheld=False)) - queue
    deferred = queue - db.count_matching(replace(base, exclude_companies=tuple(last))) if last and queue else 0
    return QueueForecast(queue, db.count_companies(base) if queue else 0, held, deferred, settings.company_gap_days, estimate)
