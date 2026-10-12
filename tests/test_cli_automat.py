"""Polecenie `automat`: flagi limitów, Start z potwierdzeniem, stop/pauza, doctor i instalacja (bez prawdziwego launchctl)."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from perno.cli import automat as cli
from perno.db.automat_store import AutomatStore
from perno.domain import automat_rules as ar
from perno.services import automat_control
from perno.settings import ConfigFile
from perno.system import launchd
from perno.system.power import PowerState
from tests.fixtures import NO_CONFIG, FakeClock, make_db, put, sample_row


def run_cmd(handler, ctx, argv, **kw):
    args = cli.build_parser().parse_args(["--config", NO_CONFIG, *argv])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = handler(args, ctx, **kw)
    return code, out.getvalue()


class CliCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 10, 5, 10, 0, 0))
        db = make_db(self.clock)
        store = AutomatStore(db)
        store.ensure_schema()
        self.ctx = cli.Context(ConfigFile(NO_CONFIG), db, store)
        self.smtp = mock.patch("perno.services.sending.build_mailers", return_value=[mock.Mock()]).start()
        self.addCleanup(mock.patch.stopall)


class ParseTests(unittest.TestCase):
    def test_parse_days(self):
        self.assertEqual(cli.parse_days("pn-pt"), [0, 1, 2, 3, 4])
        self.assertEqual(cli.parse_days("pt, pn,sr,śr"), [0, 2, 4])
        self.assertEqual(cli.parse_days("sb-nd"), [5, 6])
        for bad in ("poniedziałek", "pn-xx", ""):
            with self.subTest(bad=bad), self.assertRaises(ar.SettingsInvalid):
                cli.parse_days(bad)

    def test_flags_to_settings(self):
        args = cli.build_parser().parse_args(["limits", "--daily-cap", "30", "--window", "8:30-15:00", "--days", "pn-cz",
                                              "--countries", "pl, de,_foreign", "--horizon", "4"])
        self.assertEqual(cli.settings_from_flags(args), {
            "daily_cap": 30, "window_start": "8:30", "window_end": "15:00", "weekdays": [0, 1, 2, 3],
            "countries": ["PL", "DE", "_foreign"], "horizon_days": 4})

    def test_no_flags_changes_nothing_and_empty_countries_means_all(self):
        self.assertEqual(cli.settings_from_flags(cli.build_parser().parse_args(["limits"])), {})
        args = cli.build_parser().parse_args(["limits", "--countries", ""])
        self.assertEqual(cli.settings_from_flags(args), {"countries": []})


class LimitsTests(CliCase):
    def test_limits_are_saved_and_remembered(self):
        code, out = run_cmd(cli.cmd_limits, self.ctx, ["limits", "--daily-cap", "25", "--window", "9:00-14:00"])
        self.assertEqual(code, 0)
        self.assertIn("Zapisano limity.", out)
        self.assertIn("dzienny limit:     25", out)
        _, again = run_cmd(cli.cmd_limits, self.ctx, ["limits"])
        self.assertIn("dzienny limit:     25", again)
        self.assertIn("09:00–14:00", again)
        self.assertIn("zmienione 05.10.2026 10:00", again)

    def test_the_company_gap_flag_is_saved_and_shown(self):
        _, out = run_cmd(cli.cmd_limits, self.ctx, ["limits"])
        self.assertIn("odstęp dla firmy:  1 dn. między mailami do jednej firmy", out)  # domyślnie jeden mail na firmę dziennie
        _, out = run_cmd(cli.cmd_limits, self.ctx, ["limits", "--company-gap", "3"])
        self.assertIn("odstęp dla firmy:  3 dn.", out)
        _, out = run_cmd(cli.cmd_limits, self.ctx, ["limits", "--company-gap", "0"])
        self.assertIn("brak (kilka adresów jednej firmy tego samego dnia)", out)
        self.assertEqual(self.ctx.store.get_state().settings.company_gap_days, 0)

    def test_delay_bands_flag_is_saved_and_listed(self):
        _, out = run_cmd(cli.cmd_limits, self.ctx, ["limits", "--delay-bands", "1m-2m=50,5m-10m=40,20m-60m=10"])
        self.assertEqual(len(self.ctx.store.get_state().settings.delay_bands), 3)
        for line in ("1 min – 2 min: 50%", "5 min – 10 min: 40%", "20 min – 60 min: 10%"):
            self.assertIn(line, out)
        _, again = run_cmd(cli.cmd_limits, self.ctx, ["limits"])
        self.assertIn("20 min – 60 min: 10%", again)

    def test_a_single_range_is_still_shown_on_one_line(self):
        _, out = run_cmd(cli.cmd_limits, self.ctx, ["limits"])
        self.assertIn("odstęp między mailami: 45–120 s", out)
        _, out = run_cmd(cli.cmd_limits, self.ctx, ["limits", "--delay-bands", "30-90=100"])
        self.assertIn("odstęp między mailami: 30–90 s", out)

    def test_old_delay_flags_replace_the_bands_with_one_range(self):
        run_cmd(cli.cmd_limits, self.ctx, ["limits", "--delay-bands", "1m-2m=50,5m-10m=50"])
        _, out = run_cmd(cli.cmd_limits, self.ctx, ["limits", "--delay-min", "20", "--delay-max", "40"])
        self.assertIn("odstęp między mailami: 20–40 s", out)
        self.assertEqual(len(self.ctx.store.get_state().settings.delay_bands), 1)

    def test_bad_bands_text_and_mixing_with_old_flags_are_refused(self):
        for argv in (["--delay-bands", "1m-2m=60"], ["--delay-bands", "nonsense"], ["--delay-bands", "1m-2m=100", "--delay-min", "20"]):
            with self.subTest(argv=argv), self.assertRaises(ar.SettingsInvalid):
                run_cmd(cli.cmd_limits, self.ctx, ["limits", *argv])
        self.assertEqual(self.ctx.store.get_state().settings, ar.DEFAULT_SETTINGS)

    def test_a_bad_gap_is_refused(self):
        with self.assertRaises(ar.SettingsInvalid):
            run_cmd(cli.cmd_limits, self.ctx, ["limits", "--company-gap", "99"])

    def test_defaults_are_marked_as_not_saved(self):
        _, out = run_cmd(cli.cmd_limits, self.ctx, ["limits"])
        self.assertIn("domyślne, jeszcze nie zapisane", out)


class StartTests(CliCase):
    def test_start_shows_limits_and_arms_only_after_yes(self):
        put(self.ctx.db, sample_row(1, country="DE"), sample_row(2, country="PL"))
        run_cmd(cli.cmd_limits, self.ctx, ["limits", "--daily-cap", "10"])
        code, out = run_cmd(cli.cmd_start, self.ctx, ["start"], ask=lambda prompt: "tak")
        self.assertEqual(code, 0)
        self.assertIn("W kolejce: 2 maili, w tym 1 do firm spoza Polski", out)
        self.assertIn("dzienny limit:     10", out)
        self.assertIn("ostatnia zmiana limitów: 05.10.2026 10:00", out)
        self.assertTrue(self.ctx.store.get_state().armed)
        self.assertIn("demon nie działa", out)

    def test_start_tells_about_companies_deferred_and_held_addresses(self):
        put(self.ctx.db, sample_row(1), sample_row(2))
        self.ctx.db.add_mail("eventy-001", "druga@firma.pl")
        self.ctx.db.claim_for_sending("eventy-001")
        self.ctx.db.mark_sent("eventy-001")
        _, out = run_cmd(cli.cmd_start, self.ctx, ["start"], ask=lambda prompt: "nie")
        self.assertIn("W kolejce: 2 maili", out)
        self.assertIn("to 2 firm; dziś czeka na odstęp: 1; wstrzymane statusem innego adresu firmy: 0.", out)

    def test_anything_but_yes_cancels(self):
        put(self.ctx.db, sample_row(1))
        for answer in ("", "nie", "n", "yes"):
            with self.subTest(answer=answer):
                code, out = run_cmd(cli.cmd_start, self.ctx, ["start"], ask=lambda prompt, a=answer: a)
                self.assertEqual(code, 1)
                self.assertIn("Anulowano", out)
                self.assertFalse(self.ctx.store.get_state().armed)

    def test_default_limits_are_called_out(self):
        put(self.ctx.db, sample_row(1))
        _, out = run_cmd(cli.cmd_start, self.ctx, ["start"], ask=lambda prompt: "nie")
        self.assertIn("DOMYŚLNE (nigdy nie zapisane)", out)

    def test_stop_pause_resume_and_status(self):
        put(self.ctx.db, sample_row(1))
        run_cmd(cli.cmd_start, self.ctx, ["start"], ask=lambda prompt: "tak")
        with mock.patch.object(automat_control, "Probes") as probes:
            probes.return_value = automat_control.Probes(
                launchd=automat_control.Cached(lambda: {"installed": False, "loaded": False, "pid": None}),
                power=automat_control.Cached(lambda: PowerState(None)))
            run_cmd(cli.cmd_pause, self.ctx, ["pause"])
            _, out = run_cmd(cli.cmd_status, self.ctx, ["status"])
            self.assertIn("WSTRZYMANY: Wstrzymano ręcznie.", out)
            self.assertIn("Demon: NIE działa", out)
            run_cmd(cli.cmd_resume, self.ctx, ["resume"])
            self.assertFalse(self.ctx.store.get_state().paused)
            _, stopped = run_cmd(cli.cmd_stop, self.ctx, ["stop"])
            self.assertIn("Plan zatrzymany.", stopped)
            _, again = run_cmd(cli.cmd_stop, self.ctx, ["stop"])
            self.assertIn("nie był uzbrojony", again)


class SystemCommandsTests(CliCase):
    def test_install_and_uninstall_use_launchd_with_this_python(self):
        with mock.patch.object(launchd, "install", return_value="/x/pl.perno.automat.plist") as install, \
                mock.patch.object(launchd, "uninstall", return_value=True) as uninstall, \
                mock.patch("perno.cli.automat.cfg.LOG_DIR", tempfile.mkdtemp()):
            _, out = run_cmd(cli.cmd_install, self.ctx, ["install"])
            self.assertIn("Zainstalowano LaunchAgent", out)
            self.assertIn(b"perno.cli.automat", install.call_args.kwargs["content"])
            _, out = run_cmd(cli.cmd_uninstall, self.ctx, ["uninstall"])
            self.assertIn("Usunięto LaunchAgent", out)
            uninstall.assert_called_once()

    def test_print_pmset_wakes_five_minutes_before_the_window(self):
        _, out = run_cmd(cli.cmd_print_pmset, self.ctx, ["print-pmset"])
        self.assertIn("sudo pmset repeat wakeorpoweron MTWRF 08:55:00", out)

    def test_doctor_flags_missing_pieces(self):
        with mock.patch.object(launchd, "status", return_value=launchd.LaunchdStatus(False, False)), \
                mock.patch("perno.cli.automat.power.read_power_state", return_value=PowerState(True, 63)):
            lines = cli.doctor_lines(self.ctx)
        text = "\n".join(f"{level} {line}" for level, line in lines)
        self.assertIn("UWAGA brak kompletnych danych SMTP", text)
        self.assertIn("UWAGA brak NTFY_TOPIC", text)
        self.assertIn("UWAGA brak HEALTHCHECK_URL", text)
        self.assertIn("UWAGA LaunchAgent niezainstalowany", text)
        self.assertIn("UWAGA Mac na baterii (63%)", text)
        self.assertIn("OK baza", text)

    def test_test_notify_reports_each_channel(self):
        fake = mock.Mock()
        fake.dispatch.return_value = mock.Mock(sent=["macos"], failed={"ntfy": "brak połączenia"}, skipped=["email"])
        with mock.patch.object(automat_control, "notifier_from_config", return_value=fake):
            code, out = run_cmd(cli.cmd_test_notify, self.ctx, ["test-notify"])
        self.assertEqual(code, 1)
        self.assertIn("OK       macos", out)
        self.assertIn("BŁĄD     ntfy: brak połączenia", out)
        self.assertIn("pominięto email", out)


class MainTests(unittest.TestCase):
    def test_errors_become_messages_not_tracebacks(self):
        err = io.StringIO()
        with mock.patch.object(cli, "open_context", side_effect=cli.ConfigError("Brak config.env.")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["status"]), 1)
        self.assertIn("BŁĄD: Brak config.env.", err.getvalue())

    def test_invalid_limits_are_reported(self):
        db = make_db()
        store = AutomatStore(db)
        store.ensure_schema()
        ctx = cli.Context(ConfigFile(NO_CONFIG), db, store)
        err = io.StringIO()
        with mock.patch.object(cli, "open_context", return_value=ctx), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["limits", "--daily-cap", "0"]), 1)
        self.assertIn("dzienny limit", err.getvalue())


if __name__ == "__main__":
    unittest.main()
