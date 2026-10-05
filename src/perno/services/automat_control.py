"""Sterowanie automatem z panelu i terminala: limity, uzbrojenie z potwierdzeniem, pauza, podgląd kolejki i stan.

Demon (`services.automat`) tylko wykonuje plan; to tutaj człowiek go ustawia i zatwierdza. Uzbrojenie wymaga jawnego
potwierdzenia limitów, które widział (znacznik czasu ich ostatniej zmiany), żeby nie dało się uruchomić planu na
wartościach, których nikt nie sprawdził.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from perno.db import Database
from perno.db.automat_store import AutomatState, AutomatStore
from perno.domain import automat_events as ev
from perno.domain import automat_rules as ar
from perno.domain.rowfilter import FOREIGN_COUNTRY, HOME_COUNTRY, NO_COUNTRY, country_selected
from perno.notify.factory import build_notifier
from perno.services import sending
from perno.services.automat_log import EventLog
from perno.settings import ConfigError, NotifySettings
from perno.system import launchd, power

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"


class ArmRefused(Exception):
    """Plan nie może zostać uzbrojony (komunikat jest dla użytkownika)."""


class StaleConfirmation(Exception):
    """Potwierdzenie dotyczy innych limitów niż zapisane teraz (ktoś je zmienił po wyświetleniu okna)."""


def stamp_of(moment: Optional[datetime]) -> str:
    """Znacznik czasu jako tekst (pusty, gdy brak). Dla limitów to to, co człowiek potwierdza przy Starcie."""
    return moment.strftime(_STAMP_FORMAT) if moment else ""


class Cached:
    """Wartość odświeżana najwyżej raz na `ttl` sekund (status panelu odpytuje co 3 s, a `launchctl` i `pmset` to podprocesy)."""

    def __init__(self, fn: Callable, ttl: float = 30.0, clock: Callable = time.monotonic):
        self._fn, self._ttl, self._clock = fn, ttl, clock
        self._at: Optional[float] = None
        self._value: Any = None

    def get(self) -> Any:
        now = self._clock()
        if self._at is None or now - self._at >= self._ttl:
            self._value, self._at = self._fn(), now
        return self._value


def _launchd_probe() -> dict:
    state = launchd.status(home=os.path.expanduser("~"))
    return {"installed": state.installed, "loaded": state.loaded, "pid": state.pid}


@dataclass
class Probes:
    """Odczyty systemu (LaunchAgent, zasilanie) w podręcznej pamięci; w testach podmieniane atrapami."""
    launchd: Cached = field(default_factory=lambda: Cached(_launchd_probe))
    power: Cached = field(default_factory=lambda: Cached(power.read_power_state))


def notify_settings(config: Mapping) -> NotifySettings:
    """Ustawienia powiadomień; niepoprawne wartości nie blokują sterowania (zostają domyślne: tylko macOS)."""
    try:
        return NotifySettings.from_config(config)
    except ConfigError:
        return NotifySettings()


def build_event_log(store: AutomatStore, db: Database, config: Mapping, smtp_factory: Optional[Callable] = None) -> EventLog:
    """Dziennik zdarzeń z kanałami z config.env (kanał e-mail tylko przy kompletnym SMTP)."""
    from_addr, factory = "", None
    try:
        mailer = sending.build_mailer(config, smtp_factory=smtp_factory)
        from_addr, factory = mailer.sender.address, mailer.smtp_factory
    except ConfigError:
        pass
    notifier = build_notifier(notify_settings(config), smtp_factory=factory, from_addr=from_addr)
    return EventLog(store, notifier, db.now)


# ---- zmiany stanu ---------------------------------------------------------------------------------------------------


def save_settings(store: AutomatStore, log: EventLog, raw: Mapping) -> dict:
    """Zapisuje limity (rzuca `SettingsInvalid`). Zmiana przy uzbrojonym planie rozbraja go i wymaga nowego Startu."""
    settings = ar.settings_from_mapping(raw, base=store.get_state().settings)
    disarmed = store.save_settings(settings)
    text = "Zapisano nowe limity automatu." + (
        " Plan był uzbrojony, więc go rozbrojono — potwierdź Start od nowa." if disarmed else "")
    log.emit(ev.EVENT_SETTINGS_CHANGED, text, notify=False)
    return {"settings": settings.to_json(), "disarmed": disarmed, "stamp": stamp_of(store.get_state().changed_at)}


def arm(db: Database, store: AutomatStore, log: EventLog, mailer: Callable, *, acknowledged: bool, stamp: str) -> AutomatState:
    """Uzbraja plan po potwierdzeniu: wymaga zaznaczenia „sprawdziłem limity”, tych samych limitów co wyświetlone,
    kompletnego SMTP i niepustej kolejki."""
    if not acknowledged:
        raise ArmRefused("Potwierdź, że sprawdzono limity (zaznacz „Sprawdziłem limity”).")
    state = store.get_state()
    if stamp != stamp_of(state.changed_at):
        raise StaleConfirmation("Limity zmieniły się od wyświetlenia okna potwierdzenia — sprawdź je jeszcze raz.")
    mailer()  # rzuca ConfigError, gdy brakuje danych SMTP
    queue = db.count_matching(ar.queue_filter(state.settings))
    if queue == 0:
        raise ArmRefused("Kolejka jest pusta: brak maili „Do wysłania” pasujących do ustawionego segmentu i krajów.")
    store.arm(ar.horizon_end(db.now(), state.settings),
              {"settings": state.settings.to_json(), "stamp": stamp, "queue": queue})
    log.emit(ev.EVENT_ARMED, f"Plan uzbrojony: do {state.settings.daily_cap} maili dziennie, w kolejce {queue}.")
    return store.get_state()


def disarm(store: AutomatStore, log: EventLog) -> bool:
    """Zatrzymuje plan (demon zostaje, ale nic nie wysyła)."""
    was = store.disarm()
    if was:
        log.emit(ev.EVENT_DISARMED, "Plan zatrzymany ręcznie.")
    return was


def pause(store: AutomatStore, log: EventLog) -> None:
    """Wstrzymuje wysyłkę do ręcznego wznowienia."""
    store.pause("Wstrzymano ręcznie.")
    log.emit(ev.EVENT_PAUSED_BY_USER, "Wysyłka wstrzymana ręcznie.")


def resume(store: AutomatStore, log: EventLog) -> None:
    """Wznawia wstrzymaną wysyłkę (zeruje serię błędów)."""
    store.resume()
    log.emit(ev.EVENT_RESUMED, "Wysyłka wznowiona ręcznie.", notify=False)


# ---- odczyty dla panelu ---------------------------------------------------------------------------------------------


def preview(db: Database, store: AutomatStore, now: datetime) -> dict:
    """Kolejka i prognoza przy zapisanych limitach: liczby, ile maili idzie za granicę, plan na najbliższe dni."""
    state = store.get_state()
    settings = state.settings
    flt = replace(ar.queue_filter(settings), countries=())
    countries = {key: n for key, n in db.facet_counts(flt, "countries").items()
                 if key != FOREIGN_COUNTRY and country_selected(settings.countries, key)}
    queue = sum(countries.values())
    foreign = sum(n for key, n in countries.items() if key not in (NO_COUNTRY, HOME_COUNTRY))
    sent_today = db.sent_today_count(now)
    until = state.armed_until or ar.horizon_end(now, settings)
    estimate = ar.estimate_finish(now, settings, queue, sent_today, until)
    return {
        "queue": queue, "foreign": foreign, "no_country": countries.get(NO_COUNTRY, 0),
        "countries": sorted(countries.items(), key=lambda item: -item[1])[:12],
        "sent_today": sent_today, "send_days": estimate.send_days,
        "last_day": estimate.last_day.isoformat() if estimate.last_day else None,
        "fits_in_horizon": estimate.fits_in_horizon, "until": stamp_of(until),
        "plan": [[day.isoformat(), count] for day, count in estimate.plan],
    }


def status_report(db: Database, store: AutomatStore, now: datetime, probes: Probes, *, channels: list) -> dict:
    """Szybki stan do odpytywania co kilka sekund: ustawienia, plan, demon, dziś wysłano, zdarzenia i stan systemu."""
    state = store.get_state()
    run = store.active_run() or {}
    age = None
    if run.get("heartbeat_at"):
        age = max(0.0, (now - datetime.strptime(run["heartbeat_at"], _STAMP_FORMAT)).total_seconds())
    battery = probes.power.get()
    return {
        "settings": state.settings.to_json(), "settings_ok": state.settings_ok, "stamp": stamp_of(state.changed_at),
        "armed": state.armed, "armed_at": stamp_of(state.armed_at), "armed_until": stamp_of(state.armed_until),
        "paused": state.paused, "pause_reason": state.pause_reason, "streak": state.streak,
        "daemon": {"running": bool(run), "heartbeat_age": age, "stats": run.get("statystyki", {}), "pid": run.get("pid")},
        "sent_today": db.sent_today_count(now), "now": stamp_of(now),
        "launchd": probes.launchd.get(), "power": {"on_battery": battery.on_battery, "percent": battery.percent},
        "channels": channels, "events": store.recent_events(30),
    }
