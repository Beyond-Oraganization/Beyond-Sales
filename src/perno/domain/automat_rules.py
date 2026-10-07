"""Reguły automatu wysyłki: ustawienia, okno godzin, dzienny limit, prognoza i polityka błędów.

Czysta logika bez bazy, SMTP i zegara: wszystkie funkcje dostają „teraz” jako argument (daty w projekcie są naiwnymi
czasami lokalnymi, więc testy mogą sprawdzić zmianę czasu i przełom dnia bez czekania). Pętla, która z tego korzysta,
jest w `perno.services.automat`.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Mapping, Optional, Sequence

from perno.domain import mail_rules, segments
from perno.domain import statuses as st
from perno.domain.rowfilter import FOREIGN_COUNTRY, NO_COUNTRY, RowFilter

WEEKDAY_LABELS = ("pn", "wt", "śr", "cz", "pt", "sb", "nd")
HARD_MAX_DAILY_CAP = 500
MIN_DELAY = 10.0  # sekund; poniżej tego odstęp przestaje przypominać człowieka
MAX_DELAY = 3600.0
MAX_HORIZON_DAYS = 60
MAX_COMPANY_GAP_DAYS = 30
DAEMON_VERSION = 2  # rośnie, gdy demon poznaje nowe reguły (2 = blokada firmy i odstęp); panel odmawia dodawania adresów starszemu
TICK_SECONDS = 30.0  # najdłuższy sen pętli; krótszy niż to, co uznajemy za „zniknięcie” procesu
GAP_THRESHOLD = 180.0  # przerwa między tyknięciami dłuższa niż to = komputer spał albo proces stał
_COUNTRY_RE = re.compile(r"[A-Z]{2}")


class SettingsInvalid(ValueError):
    """Ustawienia automatu są nieprawidłowe; komunikat po polsku jest dla użytkownika."""


@dataclass(frozen=True)
class AutomatSettings:
    """Zapamiętane ustawienia automatu (trzymane w bazie do czasu zmiany przez użytkownika)."""
    daily_cap: int = 50
    weekdays: tuple = (0, 1, 2, 3, 4)  # 0 = poniedziałek
    window_start: time = time(9, 0)
    window_end: time = time(16, 0)
    delay_min: float = 45.0
    delay_max: float = 120.0
    segment: str = "both"
    countries: tuple = ()  # puste = wszystkie kraje (kody ISO oraz NO_COUNTRY/FOREIGN_COUNTRY jak w filtrze panelu)
    horizon_days: int = 5  # na ile dni kalendarzowych (z dniem startu) uzbrajamy plan; potem trzeba potwierdzić od nowa
    max_consecutive_errors: int = 3
    company_gap_days: int = 1  # najmniejszy odstęp (w dniach kalendarzowych) między mailami do jednej firmy; 0 = bez odstępu

    def to_json(self) -> dict:
        """Postać zapisywana w bazie i zwracana przez API."""
        return {
            "daily_cap": self.daily_cap, "weekdays": list(self.weekdays),
            "window_start": self.window_start.strftime("%H:%M"), "window_end": self.window_end.strftime("%H:%M"),
            "delay_min": self.delay_min, "delay_max": self.delay_max, "segment": self.segment,
            "countries": list(self.countries), "horizon_days": self.horizon_days,
            "max_consecutive_errors": self.max_consecutive_errors, "company_gap_days": self.company_gap_days,
        }


DEFAULT_SETTINGS = AutomatSettings()


def _integer(raw: object, name: str, low: int, high: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)) or (isinstance(raw, float) and not raw.is_integer()):
        raise SettingsInvalid(f"Pole {name} musi być liczbą całkowitą.")
    try:
        number = int(raw)
    except ValueError:
        raise SettingsInvalid(f"Pole {name} musi być liczbą całkowitą.") from None
    if not low <= number <= high:
        raise SettingsInvalid(f"Pole {name} poza zakresem ({low}–{high}).")
    return number


def _seconds(raw: object, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise SettingsInvalid(f"Pole {name} musi być liczbą sekund.")
    try:
        number = float(raw)
    except ValueError:
        raise SettingsInvalid(f"Pole {name} musi być liczbą sekund.") from None
    if not MIN_DELAY <= number <= MAX_DELAY:
        raise SettingsInvalid(f"Pole {name} poza zakresem ({MIN_DELAY:g}–{MAX_DELAY:g} s).")
    return number


def _clock_time(raw: object, name: str) -> time:
    if not isinstance(raw, str) or not re.fullmatch(r"\d{1,2}:\d{2}", raw.strip()):
        raise SettingsInvalid(f"Pole {name} musi mieć postać GG:MM.")
    hours, minutes = (int(part) for part in raw.strip().split(":"))
    if hours > 23 or minutes > 59:
        raise SettingsInvalid(f"Pole {name} musi być poprawną godziną (GG:MM).")
    return time(hours, minutes)


def _weekdays(raw: object) -> tuple:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise SettingsInvalid("Wybierz co najmniej jeden dzień tygodnia.")
    days = {_integer(day, "dni tygodnia", 0, 6) for day in raw}
    return tuple(sorted(days))


def _countries(raw: object) -> tuple:
    if not isinstance(raw, (list, tuple)):
        raise SettingsInvalid("Pole kraje musi być listą kodów.")
    codes = []
    for code in raw:
        if not isinstance(code, str) or not (_COUNTRY_RE.fullmatch(code) or code in (NO_COUNTRY, FOREIGN_COUNTRY)):
            raise SettingsInvalid(f"Nieznany kod kraju: {code!r}.")
        codes.append(code)
    return tuple(dict.fromkeys(codes))


def _segment(raw: object) -> str:
    if raw != "both" and not segments.is_valid(raw):
        raise SettingsInvalid(f"Nieznany segment: {raw!r}.")
    return str(raw)


def settings_from_mapping(raw: Mapping, base: AutomatSettings = DEFAULT_SETTINGS) -> AutomatSettings:
    """Ustawienia z obiektu JSON: pola nieobecne zostają takie jak w `base`, obecne są sprawdzane (rzuca `SettingsInvalid`)."""
    values: dict = {}
    if "daily_cap" in raw:
        values["daily_cap"] = _integer(raw["daily_cap"], "dzienny limit", 1, HARD_MAX_DAILY_CAP)
    if "weekdays" in raw:
        values["weekdays"] = _weekdays(raw["weekdays"])
    if "window_start" in raw:
        values["window_start"] = _clock_time(raw["window_start"], "okno od")
    if "window_end" in raw:
        values["window_end"] = _clock_time(raw["window_end"], "okno do")
    if "delay_min" in raw:
        values["delay_min"] = _seconds(raw["delay_min"], "odstęp min")
    if "delay_max" in raw:
        values["delay_max"] = _seconds(raw["delay_max"], "odstęp max")
    if "segment" in raw:
        values["segment"] = _segment(raw["segment"])
    if "countries" in raw:
        values["countries"] = _countries(raw["countries"])
    if "horizon_days" in raw:
        values["horizon_days"] = _integer(raw["horizon_days"], "horyzont (dni)", 1, MAX_HORIZON_DAYS)
    if "max_consecutive_errors" in raw:
        values["max_consecutive_errors"] = _integer(raw["max_consecutive_errors"], "błędy z rzędu", 1, 20)
    if "company_gap_days" in raw:
        values["company_gap_days"] = _integer(raw["company_gap_days"], "odstęp dla firmy (dni)", 0, MAX_COMPANY_GAP_DAYS)
    merged = replace(base, **values)
    _check_consistency(merged)
    return merged


def _check_consistency(settings: AutomatSettings) -> None:
    if settings.window_start >= settings.window_end:
        raise SettingsInvalid("Okno wysyłki: godzina „od” musi być wcześniejsza niż „do”.")
    if settings.delay_min > settings.delay_max:
        raise SettingsInvalid("Odstęp min nie może być większy niż odstęp max.")


def queue_filter(settings: AutomatSettings, exclude_companies: tuple = ()) -> RowFilter:
    """Kolejka automatu: maile „Do wysłania” z adresem i treścią, w wybranym segmencie i krajach, bez adresów wstrzymanych
    przez status innego adresu firmy. Wiersze w statusie „Błąd” celowo nie wchodzą: nieudana wysyłka wymaga decyzji człowieka
    (mogła jednak wyjść). `exclude_companies` to firmy, do których odstęp (`company_gap_days`) każe dziś nie pisać."""
    return RowFilter(segments=() if settings.segment == "both" else (settings.segment,),
                     statuses=(st.STATUS_PENDING,), countries=settings.countries, has_email=True, has_body=True,
                     unheld=True, exclude_companies=tuple(exclude_companies))


# ---- dni i okno godzin ------------------------------------------------------------------------------------------------


def is_send_day(day: date, settings: AutomatSettings) -> bool:
    """Czy w ten dzień tygodnia automat wolno wysyłać."""
    return day.weekday() in settings.weekdays


def window_bounds(day: date, settings: AutomatSettings) -> tuple:
    """Początek i koniec okna wysyłki danego dnia (naiwne czasy lokalne)."""
    return datetime.combine(day, settings.window_start), datetime.combine(day, settings.window_end)


def in_window(now: datetime, settings: AutomatSettings) -> bool:
    """Czy teraz jest dzień wysyłki i godzina mieści się w oknie (początek włącznie, koniec wyłącznie)."""
    if not is_send_day(now.date(), settings):
        return False
    start, end = window_bounds(now.date(), settings)
    return start <= now < end


def next_send_time(now: datetime, settings: AutomatSettings) -> datetime:
    """Najwcześniejszy moment nie wcześniejszy niż `now`, w którym wolno wysyłać (okno zaczyna się o `window_start`)."""
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        if not is_send_day(day, settings):
            continue
        start, end = window_bounds(day, settings)
        if offset == 0:
            if now < start:
                return start
            if now < end:
                return now
            continue
        return start
    raise SettingsInvalid("Brak dni wysyłki w ustawieniach.")  # nieosiągalne: weekdays jest niepuste (walidacja)


# ---- decyzja na jedno tyknięcie ---------------------------------------------------------------------------------------

ACTION_SEND = "send"
ACTION_WAIT = "wait"  # uzbrojony, ale teraz nie wysyłamy; `until` mówi, do kiedy
ACTION_IDLE = "idle"  # nic do roboty (nieuzbrojony albo wstrzymany); pętla tylko sprawdza stan co jakiś czas
ACTION_END = "end"  # horyzont planu minął: rozbrój i podsumuj

REASON_NOT_ARMED = "not_armed"
REASON_PAUSED = "paused"
REASON_HORIZON = "horizon_over"
REASON_OUTSIDE_WINDOW = "outside_window"
REASON_NOT_SEND_DAY = "not_send_day"
REASON_CAP_REACHED = "cap_reached"
REASON_COMPANY_GAP = "company_gap"  # są maile do wysłania, ale ich firmy czekają na odstęp (decyzja pętli, nie `plan_tick`)


@dataclass(frozen=True)
class Decision:
    """Co zrobić teraz: wysłać, czekać do `until`, bezczynnie odpytywać stan albo zakończyć plan."""
    action: str
    reason: str = ""
    until: Optional[datetime] = None


def plan_tick(now: datetime, settings: AutomatSettings, *, armed: bool, paused: bool,
              armed_until: Optional[datetime], sent_today: int) -> Decision:
    """Decyzja jednego tyknięcia. Nadrabiania zaległości celowo brak: po uśpieniu komputera nie wysyłamy serii, tylko
    wznawiamy w oknie i do dziennego limitu (wspólnego z ręcznymi wysyłkami z panelu, bo `sent_today` liczy bazę)."""
    if not armed:
        return Decision(ACTION_IDLE, REASON_NOT_ARMED)
    if armed_until is not None and now >= armed_until:
        return Decision(ACTION_END, REASON_HORIZON)
    if paused:
        return Decision(ACTION_IDLE, REASON_PAUSED)
    if not in_window(now, settings):
        reason = REASON_NOT_SEND_DAY if not is_send_day(now.date(), settings) else REASON_OUTSIDE_WINDOW
        return Decision(ACTION_WAIT, reason, next_send_time(now, settings))
    if mail_rules.daily_quota_left(sent_today, settings.daily_cap) == 0:
        tomorrow = datetime.combine(now.date() + timedelta(days=1), time.min)
        return Decision(ACTION_WAIT, REASON_CAP_REACHED, next_send_time(tomorrow, settings))
    return Decision(ACTION_SEND)


def horizon_end(armed_at: datetime, settings: AutomatSettings) -> datetime:
    """Koniec planu: północ po ostatnim dniu horyzontu (dzień uzbrojenia liczy się jako pierwszy)."""
    return datetime.combine(armed_at.date() + timedelta(days=settings.horizon_days), time.min)


# ---- prognoza ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FinishEstimate:
    """Prognoza opróżnienia kolejki przy ustawionym limicie i dniach: ile dni wysyłki, kiedy koniec, plan na kolejne dni."""
    send_days: int
    last_day: Optional[date]
    fits_in_horizon: bool
    plan: tuple  # ((data, ile maili), …) dla najbliższych dni wysyłki, maksymalnie 14
    limited_by_gap: bool = False  # odstęp dla firmy wydłuża plan (bez niego kolejka skończyłaby się wcześniej)


def estimate_finish(now: datetime, settings: AutomatSettings, queue: int, sent_today: int,
                    until: Optional[datetime] = None) -> FinishEstimate:
    """Przybliżenie, ile dni potrwa wysłanie `queue` maili (zakłada, że kolejka się nie zmienia i że komputer pracuje)."""
    remaining, send_days, last_day = queue, 0, None
    plan: list = []
    for offset in range(400):
        if remaining <= 0:
            break
        day = now.date() + timedelta(days=offset)
        capacity = _capacity(day, now, settings, sent_today)
        if capacity <= 0:
            continue
        today = min(capacity, remaining)
        remaining -= today
        send_days, last_day = send_days + 1, day
        if len(plan) < 14:
            plan.append((day, today))
    fits = until is None or last_day is None or last_day < until.date()
    return FinishEstimate(send_days, last_day, fits and remaining <= 0, tuple(plan))


def estimate_finish_grouped(now: datetime, settings: AutomatSettings, queue: Sequence[str], sent_today: int,
                            until: Optional[datetime] = None, last_sent: Optional[Mapping] = None) -> FinishEstimate:
    """Prognoza z odstępem dla firmy. `queue` to id firm, po jednym na oczekujący mail, w kolejności wysyłki (bez maili
    wstrzymanych blokadą firmy); `last_sent` mówi, kiedy ostatnio wysłano mail do firmy (dzień), dla firm z ostatnich dni.

    Dzień po dniu: pojemność dnia (limit, okno, dni tygodnia) wypełniają maile firm w kolejności ich pierwszego maila w
    kolejce, najwyżej jeden na firmę dziennie, a firma wraca do gry najwcześniej po `company_gap_days` dniach od ostatniego
    maila. Przy odstępie 0 to dawna prognoza. Kolejka jest sprowadzona do firm z liczbą maili, więc dzień kosztuje tyle co
    liczba firm, które trzeba przejrzeć, a nie liczba maili."""
    gap = settings.company_gap_days
    plain = estimate_finish(now, settings, len(queue), sent_today, until)
    if gap <= 0 or not queue:
        return plain
    remaining = Counter(queue)
    order = list(remaining)  # pierwsze wystąpienie wyznacza miejsce firmy w kolejce
    last = dict(last_sent or {})
    left, send_days, last_day = len(queue), 0, None
    plan: list = []
    for offset in range(400):
        if left <= 0:
            break
        day = now.date() + timedelta(days=offset)
        capacity = _capacity(day, now, settings, sent_today)  # 0 w dni bez wysyłki: pętla niżej niczego wtedy nie weźmie
        sent: list = []
        for company in order:
            if len(sent) >= capacity:
                break
            if company in last and (day - last[company]).days < gap:
                continue
            sent.append(company)
        if not sent:
            continue
        for company in sent:
            last[company] = day
            remaining[company] -= 1
        order = [company for company in order if remaining[company] > 0]
        left -= len(sent)
        send_days, last_day = send_days + 1, day
        if len(plan) < 14:
            plan.append((day, len(sent)))
    fits = until is None or last_day is None or last_day < until.date()
    slower = last_day is not None and (plain.last_day is None or last_day > plain.last_day)
    return FinishEstimate(send_days, last_day, fits and left <= 0, tuple(plan), slower)


def _capacity(day: date, now: datetime, settings: AutomatSettings, sent_today: int) -> int:
    if not is_send_day(day, settings):
        return 0
    if day != now.date():
        return settings.daily_cap
    if now >= window_bounds(day, settings)[1]:
        return 0
    return mail_rules.daily_quota_left(sent_today, settings.daily_cap)


# ---- błędy wysyłki ----------------------------------------------------------------------------------------------------

FAILURE_FATAL = "fatal"  # logowanie, limit nadawcy, reputacja: kolejne próby tylko pogarszają sprawę — pauza od razu
FAILURE_RECIPIENT = "recipient"  # problem konkretnego adresata (nie istnieje, odrzucony): nie świadczy o awarii skrzynki
FAILURE_OTHER = "other"  # reszta (sieć, przekroczony czas): liczy się do serii błędów

_FATAL_MARKERS = ("535", "534", "530", "authentication", "login", "password", "rate limit", "too many",
                  "sending limit", "daily limit", "exceeded", "421", "spam", "blacklist", "blocked", "reputation")
_RECIPIENT_MARKERS = ("550", "551", "553", "user unknown", "no such user", "mailbox unavailable", "does not exist",
                      "invalid recipient", "recipient rejected", "recipients refused")


def classify_failure(message: str) -> str:
    """Rodzaj błędu wysyłki na podstawie tekstu wyjątku SMTP (nie ma lepszego źródła: `send_row` oddaje tylko komunikat)."""
    text = (message or "").lower()
    if any(marker in text for marker in _FATAL_MARKERS):
        return FAILURE_FATAL
    if any(marker in text for marker in _RECIPIENT_MARKERS):
        return FAILURE_RECIPIENT
    return FAILURE_OTHER


def next_streak(streak: int, *, sent: bool, failure: Optional[str]) -> int:
    """Seria kolejnych błędów: udana wysyłka zeruje, błąd adresata jej nie zmienia, inny błąd zwiększa o jeden."""
    if sent:
        return 0
    return streak if failure == FAILURE_RECIPIENT else streak + 1


def pause_reason(streak: int, failure: Optional[str], settings: AutomatSettings) -> Optional[str]:
    """Powód automatycznej pauzy (None = kontynuuj)."""
    if failure == FAILURE_FATAL:
        return "Błąd SMTP wymagający uwagi (logowanie, limit nadawcy albo odrzucenie jako spam) — sprawdź skrzynkę."
    if streak >= settings.max_consecutive_errors:
        return f"{streak} błędów wysyłki z rzędu — sprawdź połączenie i skrzynkę, potem wznów."
    return None


# ---- podsumowania i pominięte dni -------------------------------------------------------------------------------------


def summary_due(now: datetime, settings: AutomatSettings, *, sent_today: int, reported_today: bool,
                armed_at: Optional[datetime]) -> bool:
    """Czy pora na dzienne podsumowanie: okno się skończyło albo limit wyczerpany; dzień bez żadnej wysyłki zgłaszamy
    tylko wtedy, gdy plan był uzbrojony przed początkiem okna (inaczej uzbrojenie po oknie dałoby fałszywy alarm)."""
    if reported_today or not is_send_day(now.date(), settings):
        return False
    start, end = window_bounds(now.date(), settings)
    if sent_today >= settings.daily_cap:
        return True
    if now < end:
        return False
    return sent_today > 0 or (armed_at is not None and armed_at <= start)


def skipped_days(sent_by_day: Mapping, armed_at: datetime, today: date, settings: AutomatSettings) -> list:
    """Dni wysyłki od uzbrojenia do wczoraj, w których nic nie wysłano (komputer wyłączony albo uśpiony przez całe okno)."""
    first = armed_at.date()
    if armed_at.time() >= settings.window_end:
        first += timedelta(days=1)  # uzbrojono po zakończeniu okna: ten dzień nie był „do zrobienia”
    days, day = [], first
    while day < today:
        if is_send_day(day, settings) and not sent_by_day.get(day, 0):
            days.append(day)
        day += timedelta(days=1)
    return days


def is_sleep_gap(previous: Optional[datetime], now: datetime) -> bool:
    """Czy między tyknięciami minęło tyle czasu, że komputer najpewniej spał (albo proces stał)."""
    return previous is not None and (now - previous).total_seconds() > GAP_THRESHOLD
