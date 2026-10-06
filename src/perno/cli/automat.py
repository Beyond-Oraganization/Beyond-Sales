#!/usr/bin/env python3
"""Automat wysyłki: wysyła maile sam, przez kilka dni, w oknie godzin i do dziennego limitu.

Typowo: ustaw limity (`limits`), sprawdź kolejkę i uzbrój plan (`start` — pyta o potwierdzenie), a demon, który działa
w tle (`install` ustawia jego start po zalogowaniu), robi resztę i powiadamia (macOS, ntfy, e-mail).

  python3 automat.py limits --daily-cap 40 --window 9:00-15:00 --days pn,wt,sr,cz,pt --horizon 3
  python3 automat.py doctor          # sprawdź, czy wszystko jest gotowe
  python3 automat.py test-notify     # wyślij próbne powiadomienie każdym kanałem
  python3 automat.py install         # demon po zalogowaniu (LaunchAgent)
  python3 automat.py start           # pokazuje limity i kolejkę, pyta o potwierdzenie
  python3 automat.py status | pause | resume | stop | uninstall | print-pmset

Polecenie `run` to sam demon (uruchamia go launchd; ręcznie: do prób w terminalu).
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from perno import settings as cfg
from perno.db import Database, DbError
from perno.db.automat_store import AutomatStore, DaemonActive
from perno.domain import automat_rules as ar
from perno.logging_setup import configure_logging
from perno.notify import Notification
from perno.notify.factory import build_pinger
from perno.services import automat, automat_control, sending
from perno.settings import ConfigError, ConfigFile
from perno.system import launchd, power

DAY_NAMES = {"pn": 0, "wt": 1, "sr": 2, "śr": 2, "cz": 3, "pt": 4, "sb": 5, "nd": 6}


@dataclass
class Context:
    """Połączenie z bazą i magazynem automatu dla jednego polecenia."""
    config: ConfigFile
    db: Database
    store: AutomatStore


def open_context(args: argparse.Namespace) -> Context:
    """Baza i magazyn automatu (schemat tabel zakładany od razu)."""
    config = ConfigFile(args.config)
    db = Database.from_config(config.load())
    db.check_connection()
    db.prepare()  # nowy demon na starej bazie: bez tego zapytania o firmy kończyłyby się błędem w pętli restartów
    store = AutomatStore(db)
    store.ensure_schema()
    return Context(config, db, store)


def parse_days(text: str) -> list:
    """„pn,wt,pt” albo „pn-pt” na numery dni (0 = poniedziałek)."""
    days: list = []
    for part in text.lower().split(","):
        first, _, last = part.strip().partition("-")
        if first not in DAY_NAMES or (last and last not in DAY_NAMES):
            raise ar.SettingsInvalid(f"Nieznany dzień: {part.strip()!r} (użyj pn,wt,sr,cz,pt,sb,nd albo zakresu pn-pt).")
        low, high = DAY_NAMES[first], DAY_NAMES[last] if last else DAY_NAMES[first]
        days += list(range(low, high + 1))
    return sorted(set(days))


def settings_from_flags(args: argparse.Namespace) -> dict:
    """Pola ustawień podane we flagach (pominięte zostają bez zmian)."""
    raw: dict = {}
    for flag, key in (("daily_cap", "daily_cap"), ("delay_min", "delay_min"), ("delay_max", "delay_max"),
                      ("segment", "segment"), ("horizon", "horizon_days"), ("max_errors", "max_consecutive_errors")):
        if getattr(args, flag) is not None:
            raw[key] = getattr(args, flag)
    if args.window:
        start, _, end = args.window.partition("-")
        raw.update(window_start=start, window_end=end)
    if args.days:
        raw["weekdays"] = parse_days(args.days)
    if args.countries is not None:
        raw["countries"] = [code.strip().upper() if not code.startswith("_") else code.strip()
                            for code in args.countries.split(",") if code.strip()]
    return raw


def describe_settings(s: ar.AutomatSettings) -> list:
    """Limity do pokazania człowiekowi (przed potwierdzeniem Startu i w `status`)."""
    days = ", ".join(ar.WEEKDAY_LABELS[d] for d in s.weekdays)
    return [f"  dzienny limit:     {s.daily_cap}", f"  dni wysyłki:       {days}",
            f"  okno godzin:       {s.window_start:%H:%M}–{s.window_end:%H:%M}",
            f"  odstęp między mailami: {s.delay_min:g}–{s.delay_max:g} s",
            f"  segment:           {s.segment}", f"  kraje:             {', '.join(s.countries) or 'wszystkie'}",
            f"  horyzont planu:    {s.horizon_days} dni (potem trzeba potwierdzić od nowa)",
            f"  pauza po błędach z rzędu: {s.max_consecutive_errors}"]


def cmd_limits(args: argparse.Namespace, ctx: Context) -> int:
    raw = settings_from_flags(args)
    if raw:
        log = automat_control.build_event_log(ctx.store, ctx.db, ctx.config.load())
        result = automat_control.save_settings(ctx.store, log, raw)
        print("Zapisano limity." + (" Plan był uzbrojony — rozbrojono go, potwierdź Start od nowa." if result["disarmed"] else ""))
    state = ctx.store.get_state()
    print("Limity automatu" + (f" (zmienione {state.changed_at:%d.%m.%Y %H:%M})" if state.changed_at else " (domyślne, jeszcze nie zapisane)") + ":")
    print("\n".join(describe_settings(state.settings)))
    return 0


def cmd_status(args: argparse.Namespace, ctx: Context) -> int:
    now = ctx.db.now()
    report = automat_control.status_report(ctx.db, ctx.store, now, automat_control.Probes(), channels=[])
    daemon = report["daemon"]
    print(f"Demon: {'działa' if daemon['running'] else 'NIE działa'}"
          + (f" (pulsy sprzed {daemon['heartbeat_age']:.0f} s)" if daemon["heartbeat_age"] is not None else ""))
    print(f"LaunchAgent: {'załadowany' if report['launchd']['loaded'] else 'niezaładowany'}")
    plan = "uzbrojony do " + report["armed_until"] if report["armed"] else "nieuzbrojony"
    print(f"Plan: {plan}" + (f" — WSTRZYMANY: {report['pause_reason']}" if report["paused"] else ""))
    print(f"Dziś wysłano: {report['sent_today']} z {report['settings']['daily_cap']}")
    for event in reversed(report["events"][:8]):
        print(f"  {event['ts'][5:16].replace('T', ' ')} [{event['poziom']}] {event['tresc'][:110]}")
    return 0


def cmd_start(args: argparse.Namespace, ctx: Context, ask: Callable = input) -> int:
    state = ctx.store.get_state()
    data = automat_control.preview(ctx.db, ctx.store, ctx.db.now())
    print("Plan automatu — sprawdź limity:")
    print("\n".join(describe_settings(state.settings)))
    print(f"  ostatnia zmiana limitów: {state.changed_at:%d.%m.%Y %H:%M}" if state.changed_at else "  limity: DOMYŚLNE (nigdy nie zapisane)")
    print(f"W kolejce: {data['queue']} maili, w tym {data['foreign']} do firm spoza Polski"
          f" (maile są dziś generowane po polsku) i {data['no_country']} bez kraju.")
    if data["last_day"]:
        print(f"Przy tym tempie koniec około {data['last_day']} ({data['send_days']} dni wysyłki)"
              + ("" if data["fits_in_horizon"] else f" — NIE zmieści się w horyzoncie planu (do {data['until'][:10]})."))
    answer = ask("Wpisz „tak”, żeby uzbroić plan z tymi limitami: ").strip().lower()
    if answer not in ("tak", "t"):
        print("Anulowano — nic nie uzbrojono.")
        return 1
    log = automat_control.build_event_log(ctx.store, ctx.db, ctx.config.load())
    mailer = lambda: sending.build_mailer(ctx.config.load(), ctx.config.path)
    automat_control.arm(ctx.db, ctx.store, log, mailer, acknowledged=True, stamp=automat_control.stamp_of(state.changed_at))
    print("Plan uzbrojony. Demon wyśle maile w oknie godzin; powiadomienia pójdą skonfigurowanymi kanałami.")
    if not ctx.store.active_run():
        print("UWAGA: demon nie działa — uruchom `python3 automat.py install` (albo `run` w osobnym terminalu).")
    return 0


def cmd_stop(args: argparse.Namespace, ctx: Context) -> int:
    log = automat_control.build_event_log(ctx.store, ctx.db, ctx.config.load())
    print("Plan zatrzymany." if automat_control.disarm(ctx.store, log) else "Plan nie był uzbrojony.")
    return 0


def cmd_pause(args: argparse.Namespace, ctx: Context) -> int:
    automat_control.pause(ctx.store, automat_control.build_event_log(ctx.store, ctx.db, ctx.config.load()))
    print("Wysyłka wstrzymana.")
    return 0


def cmd_resume(args: argparse.Namespace, ctx: Context) -> int:
    automat_control.resume(ctx.store, automat_control.build_event_log(ctx.store, ctx.db, ctx.config.load()))
    print("Wysyłka wznowiona.")
    return 0


def cmd_run(args: argparse.Namespace, ctx: Context) -> int:
    config = ctx.config.load()
    settings = automat_control.notify_settings(config)
    runner = automat.AutomatRunner(automat.RunnerDeps(
        db=ctx.db, store=ctx.store, mailer=lambda: sending.build_mailer(ctx.config.load(), ctx.config.path),
        notifier=automat_control.notifier_from_config(config), pinger=build_pinger(settings), guard=power.SleepGuard()))
    stop = threading.Event()

    def nap(seconds: float) -> None:
        stop.wait(seconds)

    runner.d.sleep = nap
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        runner.run_forever(stop)
    except DaemonActive:
        print("Demon automatu już działa — kończę (to nie jest błąd).")
    return 0


def cmd_install(args: argparse.Namespace, ctx: Context) -> int:
    path = automat_control.install_daemon(automat_control.Probes())
    print(f"Zainstalowano LaunchAgent: {path}\nDemon startuje po każdym zalogowaniu i po awarii. Sprawdź: python3 automat.py doctor")
    return 0


def cmd_uninstall(args: argparse.Namespace, ctx: Context) -> int:
    removed = automat_control.remove_daemon(automat_control.Probes())
    print("Usunięto LaunchAgent." if removed else "LaunchAgent nie był zainstalowany.")
    return 0


def cmd_test_notify(args: argparse.Namespace, ctx: Context) -> int:
    notifier = automat_control.notifier_from_config(ctx.config.load())
    result = notifier.dispatch(Notification("Automat: próba", "Próbne powiadomienie z automatu wysyłki Perno.", "info"),
                               ("macos", "ntfy", "email"))
    for name in result.sent:
        print(f"  OK       {name}")
    for name, reason in result.failed.items():
        print(f"  BŁĄD     {name}: {reason}")
    for name in result.skipped:
        print(f"  pominięto {name} (nieskonfigurowany)")
    return 0 if not result.failed else 1


def cmd_print_pmset(args: argparse.Namespace, ctx: Context) -> int:
    print("Wybudzanie Maca przed oknem wysyłki (wymaga hasła administratora; tego polecenia nie uruchamiam sam):")
    print("  " + automat_control.pmset_command(ctx.store.get_state().settings))
    print("Cofnięcie: sudo pmset repeat cancel   |   Podgląd: pmset -g sched")
    print("Uwaga: zamknięta klapa na baterii nie utrzyma Maca w pracy — zostaw klapę otwartą i podłącz zasilacz.")
    return 0


def doctor_lines(ctx: Context) -> list:
    """Lista kontroli gotowości: (poziom, tekst); poziom „OK” albo „UWAGA”."""
    report = automat_control.doctor_report(ctx.db, ctx.store, ctx.config.load(), automat_control.Probes())
    return [(item["level"], item["text"]) for item in report]


def cmd_doctor(args: argparse.Namespace, ctx: Context) -> int:
    lines = doctor_lines(ctx)
    for level, text in lines:
        print(f"  {level:<6} {text}")
    return 0


COMMANDS = {"limits": cmd_limits, "status": cmd_status, "start": cmd_start, "stop": cmd_stop, "pause": cmd_pause,
            "resume": cmd_resume, "run": cmd_run, "install": cmd_install, "uninstall": cmd_uninstall,
            "test-notify": cmd_test_notify, "print-pmset": cmd_print_pmset, "doctor": cmd_doctor}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=cfg.DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    limits = sub.add_parser("limits", help="Pokaż albo zapisz limity (zapamiętane do następnej zmiany).")
    limits.add_argument("--daily-cap", type=int)
    limits.add_argument("--window", metavar="GG:MM-GG:MM")
    limits.add_argument("--days", metavar="pn,wt,... lub pn-pt")
    limits.add_argument("--delay-min", type=float)
    limits.add_argument("--delay-max", type=float)
    limits.add_argument("--segment", choices=["both", "eventy", "driverzy"])
    limits.add_argument("--countries", help="kody ISO po przecinku (np. PL,DE); „_foreign” = spoza Polski; puste = wszystkie")
    limits.add_argument("--horizon", type=int, help="na ile dni uzbrajamy plan")
    limits.add_argument("--max-errors", type=int, help="po ilu błędach z rzędu automat się wstrzymuje")
    helps = {"status": "Stan demona, planu i ostatnie zdarzenia.", "start": "Pokaż limity i kolejkę, zapytaj, uzbrój plan.",
             "stop": "Zatrzymaj plan.", "pause": "Wstrzymaj wysyłkę.", "resume": "Wznów wstrzymaną wysyłkę.",
             "run": "Demon (uruchamia go launchd).", "install": "Zainstaluj LaunchAgent (start po zalogowaniu).",
             "uninstall": "Usuń LaunchAgent.", "test-notify": "Wyślij próbne powiadomienie.",
             "print-pmset": "Pokaż polecenie wybudzania Maca przed oknem.", "doctor": "Sprawdź gotowość."}
    for name, text in helps.items():
        sub.add_parser(name, help=text)
    return parser


def main(argv: Optional[list] = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    try:
        ctx = open_context(args)
        return COMMANDS[args.command](args, ctx)
    except (ConfigError, DbError, ar.SettingsInvalid, automat_control.ArmRefused, automat_control.StaleConfirmation,
            launchd.LaunchdError) as exc:
        print(f"BŁĄD: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
