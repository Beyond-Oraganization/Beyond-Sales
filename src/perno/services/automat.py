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


@dataclass
class RunnerDeps:
    """Zależności pętli (wszystko wstrzykiwane: testy używają atrap SMTP, zegara i snu)."""
    db: Database
    store: AutomatStore
    mailer: Callable[[], sending.Mailer]
    notifier: Notifier
    pinger: Optional[HealthcheckPinger] = None
    guard: Optional[SleepGuard] = None
    power: Callable[[], PowerState] = read_power_state
    sleep: Callable[[float], None] = time.sleep
    jitter: Callable[[float, float], float] = random.uniform


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

    # ---- cykl życia -----------------------------------------------------------------------------------------------

    def start(self) -> int:
        """Rejestruje demona (rzuca `DaemonActive`, gdy inny działa) i sprząta po poprzednim uruchomieniu."""
        self.d.store.recover_dead_runs()
        stuck = self.d.db.recover_stale_claims()
        self.run_id = self.d.store.start_run(self.pid)
        self.d.store.heartbeat(self.run_id, {"v": ar.DAEMON_VERSION})  # panel widzi od razu, że demon zna reguły firm
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
        decision = ar.plan_tick(now, state.settings, armed=state.armed, paused=state.paused,
                                armed_until=state.armed_until, sent_today=sent_today)
        self._housekeeping(now, state, decision, sent_today)
        if decision.action == ar.ACTION_END:
            self._end_plan(state)
            return IDLE_SECONDS
        if decision.action == ar.ACTION_SEND:
            return self._send_next(state)
        return self._wait_seconds(decision, now)

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

    def _housekeeping(self, now: datetime, state: AutomatState, decision: ar.Decision, sent_today: int) -> None:
        working = state.armed and not state.paused and ar.in_window(now, state.settings)
        if self.run_id is not None:
            self.d.store.heartbeat(self.run_id, {
                "v": ar.DAEMON_VERSION, "stan": decision.action, "powod": decision.reason, "dzis": sent_today, "limit": state.settings.daily_cap,
                "do": decision.until.strftime("%Y-%m-%dT%H:%M:%S") if decision.until else None})
        if self.d.guard is not None:
            self.d.guard.set_needed(working)
        if working:
            self._ping()
            self._check_power(now)
        if state.armed:
            self._maybe_summary(now, state, sent_today)

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

    def _maybe_summary(self, now: datetime, state: AutomatState, sent_today: int) -> None:
        if not ar.summary_due(now, state.settings, sent_today=sent_today, reported_today=False, armed_at=state.armed_at):
            return
        key = now.date().isoformat()
        if self.d.store.has_event(ev.EVENT_DAILY_SUMMARY, key):
            return
        self.log.emit(ev.EVENT_DAILY_SUMMARY, self._summary_text(now, state, sent_today), key=key)

    def _summary_text(self, now: datetime, state: AutomatState, sent_today: int) -> str:
        settings = state.settings
        today_start = datetime.combine(now.date(), dtime.min)
        errors = self.d.store.count_events(ev.EVENT_ROW_ERROR, since=today_start)
        lines = [f"Wysłano dziś: {sent_today} z {settings.daily_cap}."]
        if errors:
            lines.append(f"Błędy wysyłki dziś: {errors}.")
        view = automat_queue.forecast(self.d.db, settings, now, sent_today, state.armed_until)
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
            mailer = self.d.mailer()
        except ConfigError as exc:
            self._pause(f"Konfiguracja SMTP: {exc}", ev.EVENT_PAUSED)
            return IDLE_SECONDS
        result = sending.send_row(self.d.db, ids[0], mailer, allowed_from=(st.STATUS_PENDING,))
        if self._after_send(result, state) is not None:
            return IDLE_SECONDS
        if result.outcome == "skipped":
            return SKIPPED_ROW_SECONDS
        return float(self.d.jitter(settings.delay_min, settings.delay_max))

    def _wait_for_gap(self, state: AutomatState, now: datetime) -> float:
        """Kolejka nie jest pusta, ale każda firma z oczekującym mailem dostała już mail w ostatnich dniach odstępu: czekamy
        (nie rozbrajamy planu), a w dzienniku zostaje jeden wpis na dzień."""
        tomorrow = datetime.combine(now.date() + timedelta(days=1), dtime.min)
        until = ar.next_send_time(tomorrow, state.settings)
        if self.run_id is not None:
            self.d.store.heartbeat(self.run_id, {
                "v": ar.DAEMON_VERSION, "stan": ar.ACTION_WAIT, "powod": ar.REASON_COMPANY_GAP,
                "dzis": self.d.db.sent_today_count(now), "limit": state.settings.daily_cap,
                "do": until.strftime("%Y-%m-%dT%H:%M:%S")})
        key = now.date().isoformat()
        if not self.d.store.has_event(ev.EVENT_GAP_WAIT, key):
            self.log.emit(ev.EVENT_GAP_WAIT, "Wszystkie firmy z oczekującymi mailami dostały już mail w ostatnich "
                          f"{state.settings.company_gap_days} dn. — czekam na koniec odstępu.", key=key, notify=False)
        return IDLE_SECONDS

    def _after_send(self, result: sending.SendResult, state: AutomatState) -> Optional[str]:
        """Zapisuje wynik wysyłki; zwraca powód pauzy, jeśli automat się wstrzymał (None = kontynuuj)."""
        label = _label(result.row)
        if result.outcome == "sent":
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
        streak = ar.next_streak(state.streak, sent=False, failure=failure)
        self.d.store.set_streak(streak)
        self.d.store.add_event(ev.LEVEL_WARN, ev.EVENT_ROW_ERROR, f"Błąd wysyłki do {label}: {result.message[:300]}")
        pause_reason = ar.pause_reason(streak, failure, state.settings)
        if pause_reason:
            self._pause(pause_reason, ev.EVENT_PAUSED)
        return pause_reason

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
