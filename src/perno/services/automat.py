"""Pętla automatu wysyłki: co kilkadziesiąt sekund sprawdza plan i wysyła najwyżej jeden mail.

Demon działa stale (uruchamia go launchd po zalogowaniu), ale wysyła tylko wtedy, gdy człowiek uzbroił plan
potwierdzonym Startem. Każdy mail idzie przez `sending.send_row`, więc podwójna wysyłka z panelu jest wykluczona przez
zajęcie wiersza w bazie, a limit dzienny liczy się z bazy (wspólnie z ręcznymi wysyłkami). Reguły („co teraz”, limity,
błędy) są w `perno.domain.automat_rules`; tu zostaje wykonanie: baza, SMTP, powiadomienia i sen.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Callable, Optional

from perno.db import DB_ERRORS, Database
from perno.db.automat_store import AutomatState, AutomatStore
from perno.domain import automat_events as ev
from perno.domain import automat_rules as ar
from perno.domain import delay_bands as delays
from perno.domain import statuses as st
from perno.domain.listing import DEFAULT_SORT
from perno.notify import Notification, Notifier, NotifyError
from perno.notify.healthcheck import HealthcheckPinger
from perno.services import automat_queue, sending
from perno.services.automat_log import TITLES, EventLog
from perno.settings import ConfigError
from perno.system.power import PowerState, SleepGuard, read_power_state

logger = logging.getLogger(__name__)

IDLE_SECONDS = ar.TICK_SECONDS
FAULT_SECONDS = 60.0  # po awarii bazy albo błędzie w pętli nie męczymy jej co 30 s
SKIPPED_ROW_SECONDS = 1.0
BOX_COOLDOWN = timedelta(minutes=60)  # skrzynka po błędzie połączenia wraca na próbę po tej przerwie (błąd krytyczny: do północy)


@dataclass
class RunnerDeps:
    """Zależności pętli (wszystko wstrzykiwane: testy używają atrap SMTP, zegara i snu)."""
    db: Database
    store: AutomatStore
    mailers: Callable[[], list]  # wszystkie skrzynki do rotacji (`ConfigError`, gdy konfiguracja jest niekompletna)
    notifier: Notifier
    pinger: Optional[HealthcheckPinger] = None
    guard: Optional[SleepGuard] = None
    power: Callable[[], PowerState] = read_power_state
    sleep: Callable[[float], None] = time.sleep
    jitter: Callable[[float, float], float] = random.uniform  # czas w wybranym przedziale odstępu
    roll: Callable[[], float] = random.random  # [0, 1): wybór przedziału odstępu według prawdopodobieństw


def _short_error(message: str) -> str:
    """Komunikat błędu do panelu: bez znaków sterujących i długi najwyżej na 200 znaków (pochodzi z serwera poczty)."""
    return "".join(ch if ch.isprintable() else " " for ch in (message or ""))[:200].strip()


def _label(row: Optional[dict]) -> str:
    row = row or {}
    return f"{row.get('company') or row.get('id') or '?'} ({row.get('id', '?')})"


class AutomatRunner:
    """Jeden proces demona. `tick()` robi jeden krok i zwraca, ile sekund czekać; `run_forever()` składa to w pętlę."""

    def __init__(self, deps: RunnerDeps, *, pid: Optional[int] = None):
        self.d = deps
        self.log = EventLog(deps.store, deps.notifier, deps.db.now)
        self.pid = pid or os.getpid()
        self.run_id: Optional[int] = None
        self._last_beat: Optional[datetime] = None
        self._gap_minutes: Optional[int] = None
        self._checked_day: Optional[date] = None
        self._power_day: Optional[date] = None
        self._last_alert: dict = {}
        self._mailbox_count = 1  # skrzynki, które dziś mogą wysyłać (mnożnik dziennego limitu w planie)
        self._suspended: dict = {}  # adres (małymi literami) → chwila, do której skrzynka jest wyłączona po błędach
        self._box_failures: dict = {}  # adres → kolejne nieudane wysyłki z tej skrzynki
        self._box_error: dict = {}  # adres → komunikat ostatniego błędu skrzynki (panel pokazuje go przy wyłączonej)

    # ---- cykl życia -----------------------------------------------------------------------------------------------

    def start(self) -> int:
        """Rejestruje demona (rzuca `DaemonActive`, gdy inny działa) i sprząta po poprzednim uruchomieniu."""
        self.d.store.recover_dead_runs()
        stuck = self.d.db.recover_stale_claims()
        self.run_id = self.d.store.start_run(self.pid)
        self.d.store.heartbeat(self.run_id, {"v": ar.DAEMON_VERSION, **ar.DAEMON_FEATURES})  # panel widzi od razu, że demon zna nowe reguły
        self._last_beat = self.d.db.now()
        state = self.d.store.get_state()
        self.log.emit(ev.EVENT_DAEMON_START, f"Demon automatu wystartował (pid {self.pid}).", notify=state.armed)
        if stuck:
            self.log.emit(ev.EVENT_STUCK_ROW, (
                f"{len(stuck)} mail(i) utknęło w „Wysyłanie…” (proces przerwano w trakcie) i oznaczono je jako błąd: "
                f"{', '.join(stuck[:5])}. Sprawdź folder Wysłane, zanim ponowisz wysyłkę."))
        return self.run_id

    def run_forever(self, stop: threading.Event) -> None:
        """Pętla demona do `stop` (SIGTERM/SIGINT ustawia zdarzenie; trwającą wysyłkę dokańczamy)."""
        self.start()
        try:
            while not stop.is_set():
                self._sleep(self._safe_tick(), stop)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Zwalnia blokadę uśpienia i zamyka przebieg (żeby następny demon mógł wystartować od razu)."""
        if self.d.guard is not None:
            self.d.guard.release()
        if self.run_id is not None:
            try:
                self.d.store.finish_run(self.run_id)
            except DB_ERRORS:
                logger.warning("Nie udało się zamknąć przebiegu automatu (baza niedostępna).")

    # ---- jeden krok -----------------------------------------------------------------------------------------------

    def tick(self) -> float:
        """Jeden krok pętli: stan, decyzja, ewentualna wysyłka. Zwraca czas oczekiwania w sekundach."""
        now = self.d.db.now()
        state = self.d.store.get_state()
        self._note_time(now)
        if self._gap_minutes is not None and state.armed:
            self.log.emit(ev.EVENT_RESUMED, f"Wznowiono po przerwie (około {self._gap_minutes} min) — komputer najpewniej spał.")
        self._gap_minutes = None
        self._on_new_day(now, state)
        sent_today = self.d.db.sent_today_count(now)
        mailers = self._configured_mailers()
        self._mailbox_count = len(self._usable(mailers, now)) or 1
        planned = self._planned_today(mailers, now, state.settings.daily_cap)
        decision = ar.plan_tick(now, self._total(state.settings), armed=state.armed, paused=state.paused,
                                armed_until=state.armed_until, sent_today=planned)
        self._housekeeping(now, state, decision, (sent_today, planned))
        if decision.action == ar.ACTION_END:
            self._end_plan(state)
            return IDLE_SECONDS
        if decision.action == ar.ACTION_SEND:
            return self._send_next(state)
        return self._wait_seconds(decision, now)

    def _total(self, settings: ar.AutomatSettings) -> ar.AutomatSettings:
        """Limity z łącznym dziennym limitem sprawnych skrzynek (w bazie `daily_cap` jest na jedną skrzynkę). Skrzynka wyłączona
        w ciągu dnia zmniejsza sumę, a jej dzisiejsze wysyłki zostają w liczniku: plan nigdy nie przekroczy limitów."""
        return ar.scaled(settings, self._mailbox_count)

    def _planned_today(self, mailers: list, now: datetime, daily_cap: int) -> int:
        """Ile z łącznego limitu sprawnych skrzynek dziś zużyto: każda liczy się osobno (najwyżej do swojego limitu), a
        skrzynka wyłączona w ciągu dnia nie zabiera pojemności pozostałym przez to, co sama wysłała."""
        if not mailers:
            return self.d.db.sent_today_count(now)
        off = self._unavailable(now)
        usage = sending.mailbox_usage(self.d.db, [mailer.sender.address for mailer in mailers], now)
        return sum(min(box["sent_today"], daily_cap) for box in usage if box["address"].casefold() not in off)

    def _off_numbers(self, now: datetime) -> list:
        """Numery (jak w Konfiguracji) skrzynek wyłączonych teraz po błędach."""
        return [number for number, _ in self._off_boxes(now)]

    def _off_boxes(self, now: datetime) -> list:
        off = self._unavailable(now)
        return [(number, mailer.sender.address) for number, mailer in enumerate(self._configured_mailers(), 1)
                if mailer.sender.address.casefold() in off]

    def _off_beat(self, now: datetime) -> dict:
        """Pola znaku życia o wyłączonych skrzynkach: numery i (dla panelu) adres, powód oraz chwila powrotu."""
        boxes = self._off_boxes(now)
        info = [{"address": address, "blad": self._box_error.get(address.casefold(), ""),
                 "do": self._suspended[address.casefold()].strftime("%Y-%m-%dT%H:%M:%S")} for _, address in boxes]
        return {"wylaczone": [number for number, _ in boxes], "wylaczone_info": info}

    def _configured_mailers(self) -> list:
        try:
            return self.d.mailers()
        except ConfigError:
            return []  # wysyłka zgłosi to jako pauzę; tu tylko liczymy pojemność

    def _unavailable(self, now: datetime) -> set:
        return {address for address, until in self._suspended.items() if until > now}

    def _usable(self, mailers: list, now: datetime) -> list:
        off = self._unavailable(now)
        return [mailer for mailer in mailers if mailer.sender.address.casefold() not in off]

    @staticmethod
    def _wait_seconds(decision: ar.Decision, now: datetime) -> float:
        if decision.until is None:
            return IDLE_SECONDS
        return max(1.0, min(IDLE_SECONDS, (decision.until - now).total_seconds()))

    def _safe_tick(self) -> float:
        try:
            return self.tick()
        except DB_ERRORS as exc:
            logger.warning("Baza niedostępna: %s", type(exc).__name__)
            self._alert_without_db(ev.EVENT_DB_DOWN, "Nie mogę połączyć się z bazą danych — automat czeka i ponawia. "
                                                     "Sprawdź, czy MySQL działa (brew services list).")
        except Exception as exc:
            logger.exception("Nieoczekiwany błąd w pętli automatu")
            self._alert_without_db(ev.EVENT_INTERNAL_ERROR, f"Nieoczekiwany błąd demona ({type(exc).__name__}). "
                                                            "Szczegóły: logs/automat_launchd.log.")
        return FAULT_SECONDS

    def _alert_without_db(self, event_type: str, text: str) -> None:
        """Alert, gdy nie ma gdzie zapisać zdarzenia (baza padła): powiadamiamy bezpośrednio, z ograniczeniem częstości."""
        now = self.d.db.now()
        if not ev.should_notify(event_type, self._last_alert.get(event_type), now):
            return
        self._last_alert[event_type] = now
        self.d.notifier.dispatch(Notification(TITLES.get(event_type, "Automat"), text, ev.level_for(event_type)),
                                 ev.channels_for(event_type))
        self._fail_ping()

    # ---- sen i czas -----------------------------------------------------------------------------------------------

    def _sleep(self, seconds: float, stop: threading.Event) -> None:
        """Śpi do terminu krótkimi kawałkami: po każdym zapisuje puls i wykrywa „zniknięcie” komputera (uśpienie)."""
        deadline = self.d.db.now() + timedelta(seconds=seconds)
        while not stop.is_set():
            remaining = (deadline - self.d.db.now()).total_seconds()
            if remaining <= 0:
                return
            self.d.sleep(min(ar.TICK_SECONDS, remaining))
            self._note_time(self.d.db.now())
            self._beat()

    def _note_time(self, now: datetime) -> None:
        if ar.is_sleep_gap(self._last_beat, now) and self._last_beat is not None:
            self._gap_minutes = max(1, round((now - self._last_beat).total_seconds() / 60))
        self._last_beat = now

    def _beat(self) -> None:
        if self.run_id is None:
            return
        try:
            self.d.store.heartbeat(self.run_id)
        except DB_ERRORS:
            logger.debug("Puls pominięty: baza niedostępna.")

    # ---- porządki w każdym kroku ----------------------------------------------------------------------------------

    def _housekeeping(self, now: datetime, state: AutomatState, decision: ar.Decision, counts: tuple) -> None:
        sent_today, planned = counts
        working = state.armed and not state.paused and ar.in_window(now, state.settings)
        if self.run_id is not None:
            self.d.store.heartbeat(self.run_id, {
                "v": ar.DAEMON_VERSION, **ar.DAEMON_FEATURES, "stan": decision.action, "powod": decision.reason, "dzis": sent_today,
                "limit": self._total(state.settings).daily_cap, "skrzynki": self._mailbox_count,
                **self._off_beat(now), "do": decision.until.strftime("%Y-%m-%dT%H:%M:%S") if decision.until else None})
        if self.d.guard is not None:
            self.d.guard.set_needed(working)
        if working:
            self._ping()
            self._check_power(now)
        if state.armed:
            self._maybe_summary(now, state, sent_today, planned)

    def _ping(self) -> None:
        if self.d.pinger is None:
            return
        try:
            self.d.pinger.ping()
        except NotifyError as exc:
            logger.warning("Ping dead-man's switch nie dotarł: %s", exc)

    def _fail_ping(self) -> None:
        if self.d.pinger is None:
            return
        try:
            self.d.pinger.fail()
        except NotifyError as exc:
            logger.warning("Zgłoszenie awarii do dead-man's switch nie dotarło: %s", exc)

    def _check_power(self, now: datetime) -> None:
        if self._power_day == now.date():
            return
        self._power_day = now.date()
        power = self.d.power()
        if power.on_battery:
            level = f" ({power.percent}%)" if power.percent is not None else ""
            self.log.emit(ev.EVENT_ON_BATTERY, f"Okno wysyłki trwa, a Mac jest na baterii{level}. Zamknięcie klapy uśpi "
                                               "wysyłkę — podłącz zasilacz i zostaw klapę otwartą.")

    def _on_new_day(self, now: datetime, state: AutomatState) -> None:
        """Pierwszy krok każdego dnia: zgłasza dni wysyłki, w których nic nie wyszło (komputer spał albo był wyłączony)."""
        if self._checked_day == now.date():
            return
        self._checked_day = now.date()
        if not state.armed or state.armed_at is None:
            return
        counts = self.d.db.sent_counts_by_day(state.armed_at.date(), now.date() - timedelta(days=1))
        missed = ar.skipped_days(counts, state.armed_at, now.date(), state.settings)
        fresh = [day for day in missed if not self.d.store.has_event(ev.EVENT_SKIPPED_DAY, day.isoformat())]
        if not fresh:
            return
        for day in fresh[:-1]:
            self.d.store.add_event(ev.LEVEL_WARN, ev.EVENT_SKIPPED_DAY, f"Pominięty dzień wysyłki: {day:%d.%m.%Y}.",
                                   day.isoformat())
        listing = ", ".join(f"{day:%d.%m}" for day in fresh)
        self.log.emit(ev.EVENT_SKIPPED_DAY, f"Pominięte dni wysyłki: {listing} — komputer był wyłączony albo uśpiony przez "
                                            "całe okno. Plan nie nadrabia zaległości.", key=fresh[-1].isoformat())

    def _maybe_summary(self, now: datetime, state: AutomatState, sent_today: int, planned: int) -> None:
        if not ar.summary_due(now, self._total(state.settings), sent_today=planned, reported_today=False, armed_at=state.armed_at):
            return
        key = now.date().isoformat()
        if self.d.store.has_event(ev.EVENT_DAILY_SUMMARY, key):
            return
        self.log.emit(ev.EVENT_DAILY_SUMMARY, self._summary_text(now, state, sent_today), key=key)

    def _summary_text(self, now: datetime, state: AutomatState, sent_today: int) -> str:
        settings = state.settings
        today_start = datetime.combine(now.date(), dtime.min)
        errors = self.d.store.count_events(ev.EVENT_ROW_ERROR, since=today_start)
        total = self._total(settings)
        mailers = self._configured_mailers()
        lines = [f"Wysłano dziś: {sent_today} z {settings.daily_cap * max(1, len(mailers))}."]
        if len(mailers) > 1:
            usage = sending.mailbox_usage(self.d.db, [mailer.sender.address for mailer in mailers], now)
            # numery jak w Konfiguracji, bez adresów: podsumowanie idzie też na zewnętrzne kanały (ntfy, e-mail)
            lines.append("Skrzynki: " + ", ".join(f"nr {number}: {box['sent_today']}" for number, box in enumerate(usage, 1)) + ".")
        off = self._off_numbers(now)
        if off:  # bez tego „x z y” wygląda, jakby limit nie został wyczerpany, a skrzynka po prostu nie wysyłała
            lines.append("Wyłączone po błędach: skrzynka nr " + ", ".join(str(number) for number in off) + ".")
        if errors:
            lines.append(f"Błędy wysyłki dziś: {errors}.")
        view = automat_queue.forecast(self.d.db, total, now, self._planned_today(mailers, now, settings.daily_cap), state.armed_until)
        lines.append(f"W kolejce zostało: {view.queue}.")
        if view.companies and view.companies != view.queue:  # kilka adresów na firmę: ile to firm
            lines.append(f"Dotyczy firm: {view.companies}.")
        if view.deferred_today:
            lines.append(f"Czeka na odstęp dla firmy: {view.deferred_today}.")
        if view.held:
            lines.append(f"Wstrzymane przez status innego adresu firmy: {view.held}.")
        estimate = view.estimate
        if view.queue and estimate.last_day:
            lines.append(f"Przy tym tempie koniec około {estimate.last_day:%d.%m.%Y} ({estimate.send_days} dni wysyłki).")
            if estimate.limited_by_gap:
                lines.append(f"Odstęp dla firmy ({view.gap_days} dn.) wydłuża plan.")
            if not estimate.fits_in_horizon and state.armed_until:
                lines.append(f"Plan jest uzbrojony do {state.armed_until:%d.%m.%Y} i nie zmieści całej kolejki.")
        return "\n".join(lines)

    # ---- wysyłka --------------------------------------------------------------------------------------------------

    def _send_next(self, state: AutomatState) -> float:
        settings = state.settings
        now = self.d.db.now()
        ids = self.d.db.list_ids(automat_queue.next_filter(self.d.db, settings, now), DEFAULT_SORT, limit=1)
        if not ids:
            if self.d.db.count_matching(ar.queue_filter(settings)):  # są maile, ale ich firmy czekają na odstęp
                return self._wait_for_gap(state, now)
            self._queue_empty(state)
            return IDLE_SECONDS
        try:
            mailers = self.d.mailers()
        except ConfigError as exc:
            self._pause(f"Konfiguracja SMTP: {exc}", ev.EVENT_PAUSED)
            return IDLE_SECONDS
        mailer = sending.pick_mailer(self.d.db, mailers, settings.daily_cap, now=now, unavailable=self._unavailable(now))
        if mailer is None:
            return self._no_mailbox(mailers, now)
        result = sending.send_row(self.d.db, ids[0], mailer, allowed_from=(st.STATUS_PENDING,))
        if self._after_send(result, state, mailer, mailers) is not None:
            return IDLE_SECONDS
        if result.outcome == "skipped":
            return SKIPPED_ROW_SECONDS
        return float(delays.pick(settings.delay_bands, self.d.roll(), self.d.jitter))

    def _no_mailbox(self, mailers: list, now: datetime) -> float:
        """Żadna skrzynka nie może teraz wysłać: wszystkie wyłączone po błędach (pauza, bo trzeba to naprawić) albo
        wszystkie wyczerpały dzienny limit (czekamy do jutra, plan liczy limit osobno dla każdej)."""
        if not self._usable(mailers, now):
            self._pause("Wszystkie skrzynki nadawcze są wyłączone po błędach — sprawdź hasła i limity u dostawcy poczty, "
                        "potem wznów.", ev.EVENT_PAUSED)
        return IDLE_SECONDS

    def _wait_for_gap(self, state: AutomatState, now: datetime) -> float:
        """Kolejka nie jest pusta, ale każda firma z oczekującym mailem dostała już mail w ostatnich dniach odstępu: czekamy
        (nie rozbrajamy planu), a w dzienniku zostaje jeden wpis na dzień."""
        tomorrow = datetime.combine(now.date() + timedelta(days=1), dtime.min)
        until = ar.next_send_time(tomorrow, state.settings)
        if self.run_id is not None:
            self.d.store.heartbeat(self.run_id, {
                "v": ar.DAEMON_VERSION, **ar.DAEMON_FEATURES, "stan": ar.ACTION_WAIT, "powod": ar.REASON_COMPANY_GAP,
                "dzis": self.d.db.sent_today_count(now), "limit": self._total(state.settings).daily_cap,
                **self._off_beat(now), "do": until.strftime("%Y-%m-%dT%H:%M:%S")})
        key = now.date().isoformat()
        if not self.d.store.has_event(ev.EVENT_GAP_WAIT, key):
            self.log.emit(ev.EVENT_GAP_WAIT, "Wszystkie firmy z oczekującymi mailami dostały już mail w ostatnich "
                          f"{state.settings.company_gap_days} dn. — czekam na koniec odstępu.", key=key, notify=False)
        return IDLE_SECONDS

    def _after_send(self, result: sending.SendResult, state: AutomatState, mailer: sending.Mailer,
                    mailers: list) -> Optional[str]:
        """Zapisuje wynik wysyłki; zwraca powód pauzy, jeśli automat się wstrzymał (None = kontynuuj)."""
        label = _label(result.row)
        if result.outcome == "sent":
            self._box_failures.pop(mailer.sender.address.casefold(), None)
            self._box_error.pop(mailer.sender.address.casefold(), None)
            if state.streak:
                self.d.store.set_streak(0)
            self.d.store.add_event(ev.LEVEL_INFO, ev.EVENT_SENT, f"Wysłano: {label}.")
            return None
        if result.outcome == "sent_unrecorded":
            reason = (f"Mail do {label} WYSZEDŁ, ale baza nie zapisała statusu. Wysyłka wstrzymana — sprawdź folder "
                      "Wysłane i ustaw status ręcznie, nie wysyłaj ponownie.")
            self._pause(reason, ev.EVENT_STUCK_ROW)
            return reason
        if result.outcome != "error":
            return None
        failure = ar.classify_failure(result.message)
        if failure != ar.FAILURE_RECIPIENT:
            self._box_error[mailer.sender.address.casefold()] = _short_error(result.message)
        streak = ar.next_streak(state.streak, sent=False, failure=failure)
        self.d.store.set_streak(streak)
        self.d.store.add_event(ev.LEVEL_WARN, ev.EVENT_ROW_ERROR, f"Błąd wysyłki do {label}: {result.message[:300]}")
        pause_reason = ar.pause_reason(streak, failure, state.settings)
        if self._switch_off_mailbox(mailer, failure, state.settings, mailers):
            return None
        if pause_reason:
            self._pause(pause_reason, ev.EVENT_PAUSED)
        return pause_reason

    def _switch_off_mailbox(self, mailer: sending.Mailer, failure: str, settings: ar.AutomatSettings, mailers: list) -> bool:
        """Wyłącza do końca dnia skrzynkę, która zawiodła (błąd krytyczny albo `max_consecutive_errors` z rzędu), jeśli są
        inne sprawne: reszta wysyła dalej zamiast pauzy całego automatu. Ostatnia sprawna skrzynka nigdy nie jest wyłączana
        w ten sposób — wtedy działa zwykła pauza. Błąd adresata nie świadczy o skrzynce."""
        if failure == ar.FAILURE_RECIPIENT:
            return False
        address = mailer.sender.address.casefold()
        failures = self._box_failures[address] = self._box_failures.get(address, 0) + 1
        if failure != ar.FAILURE_FATAL and failures < settings.max_consecutive_errors:
            return False
        now = self.d.db.now()
        others = [m for m in self._usable(mailers, now) if m.sender.address.casefold() != address]
        if not others:
            return False
        fatal = failure == ar.FAILURE_FATAL
        # po błędzie krytycznym (logowanie, limit dostawcy) skrzynka odpoczywa do północy; po błędzie połączenia tylko
        # godzinę, a potem dostaje jedną próbę: kolejna porażka od razu wyłącza ją znowu
        self._suspended[address] = datetime.combine(now.date() + timedelta(days=1), dtime.min) if fatal else now + BOX_COOLDOWN
        if fatal:
            self._box_failures.pop(address, None)
        else:
            self._box_failures[address] = settings.max_consecutive_errors - 1
        number = {m.sender.address.casefold(): n for n, m in enumerate(mailers, 1)}
        span = "do końca dnia" if fatal else f"na {int(BOX_COOLDOWN.total_seconds() // 60)} min (potem spróbuję ponownie)"
        self.log.emit(ev.EVENT_MAILBOX_OFF, (  # numery jak w Konfiguracji, bez adresów (powiadomienie idzie na zewnętrzne kanały)
            f"Skrzynka nr {number[address]} wyłączona {span} po błędach wysyłki (logowanie, limit dostawcy albo "
            f"łączność). Wysyłka idzie dalej ze skrzynek: {', '.join(str(number[m.sender.address.casefold()]) for m in others)}. "
            "Sprawdź jej hasło i limity u dostawcy poczty."))
        return True

    def _pause(self, reason: str, event_type: str) -> None:
        self.d.store.pause(reason)
        self.log.emit(event_type, reason)
        self._fail_ping()

    def _queue_empty(self, state: AutomatState) -> None:
        sent = self.d.store.count_events(ev.EVENT_SENT, since=state.armed_at)
        self.d.store.disarm()
        self.log.emit(ev.EVENT_QUEUE_EMPTY, f"Kolejka „Do wysłania” jest pusta. W tym planie wysłano {sent}. Plan rozbrojony.")

    def _end_plan(self, state: AutomatState) -> None:
        sent = self.d.store.count_events(ev.EVENT_SENT, since=state.armed_at)
        queue = self.d.db.count_matching(ar.queue_filter(state.settings))
        self.d.store.disarm()
        self.log.emit(ev.EVENT_HORIZON_END, (
            f"Plan na {state.settings.horizon_days} dni dobiegł końca. Wysłano {sent}, w kolejce zostało {queue}. "
            "Żeby kontynuować, uzbrój plan ponownie w panelu (sprawdź wtedy limity)."))
