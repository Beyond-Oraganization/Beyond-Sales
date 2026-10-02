from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

from perno import settings as cfg
from perno.cli import scrape
from perno.scraper import runner
from perno.scraper import statuses as st
from perno.scraper.settings import ScanSettings
from perno.scraper.store import Store
from tests.fake_sites import FakeInternet, NoNetworkMixin, Page, html
from tests.fixtures import FakeClock, make_db, put, sample_row

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliCase(NoNetworkMixin):
    contact = "SKRAPER_CONTACT=ops@example.com\n"

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="perno-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_url = f"sqlite:///{os.path.join(self.tmp, 'cli.db')}"
        self.config = os.path.join(self.tmp, "config.env")
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(self.contact)
        env = mock.patch.dict(os.environ, {"PERNO_DB_URL": self.db_url, "PERNO_SKRAPER_ALLOW_PRIVATE": "1",
                                           "PERNO_LOG_DIR": os.path.join(self.tmp, "logs"),
                                           "PERNO_SKRAPER_HOSTS": "a.test=127.0.0.1,b.test=127.0.0.1,c.test=127.0.0.1"})
        env.start()
        self.addCleanup(env.stop)
        self.db = make_db(datetime.now, url=self.db_url)  # prawdziwy zegar — tak jak w CLI, które testujemy
        self.addCleanup(self.db.engine.dispose)
        self.store = Store(self.db)
        self.store.ensure_schema()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scrape.main(["--config", self.config, *argv])
        return code, out.getvalue(), err.getvalue()


class SeedAddStatusTests(CliCase):
    def test_seed_reports_counts_and_reasons(self):
        put(self.db, sample_row(1), sample_row(2, website=""), sample_row(3, website="https://www.facebook.com/x"))
        code, out, _ = self.cli("seed")
        self.assertEqual(code, 0)
        self.assertIn("Firm w bazie: 1 (nowych: 1, pominiętych wierszy maile: 2)", out)
        self.assertIn("brak adresu strony", out)
        self.assertIn("platforma społecznościowa", out)
        self.assertIn("nowych: 0", self.cli("seed")[1])

    def test_add_company_new_existing_and_invalid(self):
        self.assertIn("Dodano: firma.pl", self.cli("dodaj", "https://www.Firma.pl/kontakt", "--nazwa", "Firma")[1])
        self.assertIn("Już istnieje: firma.pl", self.cli("dodaj", "firma.pl")[1])
        code, _, err = self.cli("dodaj", "https://twitter.com/x")
        self.assertEqual(code, 1)
        self.assertIn("BŁĄD", err)
        self.assertIn("platforma", err)

    def test_status_and_stop_without_a_run(self):
        self.cli("dodaj", "firma.pl")
        code, out, _ = self.cli("status")
        self.assertEqual((code, "Firmy: 1" in out, "Nieskanowana" in out), (0, True, True))
        self.assertIn("Żaden przebieg nie jest aktywny", self.cli("stop")[1])

    def test_stop_flags_a_live_run_and_recovers_a_dead_one(self):
        self.cli("dodaj", "firma.pl")
        run_id = self.store.start_run({}, pid=os.getpid())
        self.store.attach_run(run_id, pid=os.getpid())
        out = self.cli("status")[1]
        self.assertIn(f"Aktywny przebieg #{run_id}", out)
        self.assertIn("Poproszono aktywny przebieg o zatrzymanie", self.cli("stop")[1])
        self.assertIn("Poproszono o zatrzymanie", self.cli("status")[1])
        self.store.finish_run(run_id, st.RUN_ZATRZYMANY)
        dead = self.store.start_run({}, pid=2_000_000_000)
        self.assertIn(f"Przebieg {dead} nie żył", self.cli("stop")[1])

    def test_purge(self):
        self.cli("dodaj", "firma.pl")
        code, out, _ = self.cli("purge", "--older-than", "30")
        self.assertEqual(code, 0)
        self.assertIn("0 firm", out)
        self.assertIn("0 stron", self.cli("purge", "--older-than", "30", "--html-only")[1])
        self.assertEqual(self.cli("purge", "--older-than", "-1")[0], 1)


class DryRunAndValidationTests(CliCase):
    def test_dry_run_prints_a_plan_and_touches_nothing(self):
        self.cli("dodaj", "a.pl")
        self.cli("dodaj", "b.pl")
        before = self.store.status_overview()
        code, out, _ = self.cli("run", "--dry-run", "--concurrency", "5", "--delay", "3")
        self.assertEqual(code, 0)
        self.assertIn("PLAN (--dry-run", out)
        self.assertIn("Firm do przetworzenia: 2; równolegle: 2", out)
        self.assertIn("odstęp co najmniej 3 s", out)
        self.assertIn("PernoContextBot/1.0 (+ops@example.com)", out)
        self.assertIn("robots.txt jest zawsze respektowany", out)
        self.assertEqual(self.store.status_overview(), before)
        self.assertIsNone(self.store.latest_run())  # żaden przebieg nie powstał

    def test_the_one_second_floor_cannot_be_bypassed_from_the_command_line(self):
        self.cli("dodaj", "a.pl")
        out = self.cli("run", "--dry-run", "--delay", "0.01")[1]
        self.assertIn("odstęp co najmniej 1 s", out)

    def test_profile_flag_selects_the_per_firm_budget_and_numbers_override_it(self):
        self.cli("dodaj", "a.pl")
        default = self.cli("run", "--dry-run")[1]
        self.assertIn("Profil „zbalansowany”; limity na firmę: 500 stron, 50 MB, 30 min, głębokość 10.", default)
        context = self.cli("run", "--dry-run", "--profile", "kontekst")[1]
        self.assertIn("Profil „kontekst”; limity na firmę: 25 stron, 15 MB, 10 min, głębokość 4.", context)
        self.assertIn("odstęp co najmniej 2.5 s", context)  # uprzejmość nie zależy od profilu
        tweaked = self.cli("run", "--dry-run", "--profile", "kontekst", "--pages", "40")[1]
        self.assertIn("limity na firmę: 40 stron, 15 MB, 10 min, głębokość 4.", tweaked)
        code, _, err = self.cli("run", "--dry-run", "--profile", "pelny")
        self.assertNotEqual(code, 0)
        self.assertIn("Profil musi być jednym z", err)

    def test_dry_run_with_specific_firm_and_unknown_firm(self):
        self.cli("dodaj", "a.pl")
        self.cli("dodaj", "b.pl")
        self.assertIn("Firm do przetworzenia: 1;", self.cli("run", "--dry-run", "--firma", "a.pl")[1])
        code, _, err = self.cli("run", "--dry-run", "--firma", "nie-ma.pl")
        self.assertEqual(code, 1)
        self.assertIn("Nie znaleziono firmy", err)

    def test_missing_contact_refuses_to_start(self):
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write("# bez kontaktu\n")
        self.cli("dodaj", "a.pl")
        code, _, err = self.cli("run", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("SKRAPER_CONTACT", err)

    def test_smtp_user_is_used_as_contact_with_a_warning(self):
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write("SMTP_USER=roman@z3x.pl\n")
        self.cli("dodaj", "a.pl")
        code, out, err = self.cli("run", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("(+roman@z3x.pl)", out)
        self.assertIn("SKRAPER_CONTACT nie jest ustawiony", err)

    def test_invalid_setting_in_config_is_reported(self):
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(self.contact + "SKRAPER_CONCURRENCY=dużo\n")
        code, _, err = self.cli("run", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("nie jest liczbą", err)

    def test_second_run_is_refused_while_one_is_active(self):
        self.cli("dodaj", "a.pl")
        self.store.start_run({}, pid=os.getpid())
        code, _, err = self.cli("run")
        self.assertEqual(code, 1)
        self.assertIn("już działa", err)


class InProcessRunTests(CliCase):
    def test_full_run_through_the_cli_respects_the_floor_and_writes_everything(self):
        net = FakeInternet().start()
        self.addCleanup(net.stop)
        net.site("a.test", {"/": Page(html(title="Start", extra='<a href="mailto:biuro@a.test">biuro@a.test</a>'))})
        added = self.store.add_manual(net.url("a.test", "/"))
        started = time.monotonic()
        code, out, _ = self.cli("run", "--concurrency", "1", "--pages", "5")
        elapsed = time.monotonic() - started
        self.assertEqual(code, 0, out)
        self.assertIn("Tożsamość bota: PernoContextBot/1.0 (+ops@example.com)", out)
        self.assertIn("koniec: zakonczona", out)
        run = self.store.latest_run()
        self.assertEqual((run["status"], run["aktywny"], run["pid"]), (st.RUN_ZAKONCZONY, None, os.getpid()))
        self.assertEqual(self.store.get_firma(added["id"])["skan_status"], st.FIRMA_ZAKONCZONA)
        gaps = [b["t"] - a["t"] for a, b in zip(net.log, net.log[1:])]
        self.assertGreaterEqual(min(gaps), 0.99)  # twarda podłoga 1 s mimo braku jakiegokolwiek ustawienia
        self.assertGreaterEqual(elapsed, 2.0)
        _, total = self.store.list_firmy()
        self.assertEqual(total, 1)
        detail = self.store.firma_detail(added["id"])
        self.assertEqual([c["wartosc"] for c in detail["kontakty"]], ["biuro@a.test"])

    def test_only_selected_firm_is_scanned(self):
        net = FakeInternet().start()
        self.addCleanup(net.stop)
        net.site("a.test", {"/": Page(html(title="A"))})
        net.site("b.test", {"/": Page(html(title="B"))})
        first = self.store.add_manual(net.url("a.test", "/"))
        second = self.store.add_manual(net.url("b.test", "/"))
        code, _, _ = self.cli("run", "--firma", str(second["id"]), "--concurrency", "1")
        self.assertEqual(code, 0)
        self.assertEqual(net.hits("a.test"), [])
        self.assertEqual(self.store.get_firma(first["id"])["skan_status"], st.FIRMA_NIESKANOWANA)
        self.assertEqual(self.store.get_firma(second["id"])["skan_status"], st.FIRMA_ZAKONCZONA)


class RealProcessTests(unittest.TestCase):
    """Prawdziwe procesy: SIGTERM = łagodny stop, SIGKILL = odzyskanie bez ponownego pobierania."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perno-proc-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_url = f"sqlite:///{os.path.join(self.tmp, 'proc.db')}"
        self.config = os.path.join(self.tmp, "config.env")
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write("SKRAPER_CONTACT=ops@example.com\n")
        self.env = {**os.environ, "PERNO_DB_URL": self.db_url, "PERNO_SKRAPER_ALLOW_PRIVATE": "1",
                    "PERNO_LOG_DIR": os.path.join(self.tmp, "logs"), "PERNO_SKRAPER_HOSTS": "a.test=127.0.0.1"}
        self.net = FakeInternet().start()
        self.addCleanup(self.net.stop)
        self.db = make_db(datetime.now, url=self.db_url)
        self.addCleanup(self.db.engine.dispose)
        self.store = Store(self.db)
        self.store.ensure_schema()
        self.procs = []
        self.addCleanup(self.reap)

    def reap(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(10)
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()

    def site(self, count):
        pages = {"/": Page(html(*[f"/p{i}" for i in range(1, count)], title="Start"))}
        pages.update({f"/p{i}": Page(html(title=f"Strona {i}")) for i in range(1, count)})
        self.net.site("a.test", pages)
        return self.store.add_manual(self.net.url("a.test", "/"))["id"]

    def spawn(self, *argv):
        proc = subprocess.Popen([sys.executable, "-u", "-m", "perno.cli.scrape", "--config", self.config, *argv],
                                cwd=BASE, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)
        return proc

    def wait_for(self, predicate, timeout=40.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_sigterm_stops_gracefully_and_keeps_progress(self):
        firma_id = self.site(6)
        proc = self.spawn("run", "--concurrency", "1")
        self.assertTrue(self.wait_for(lambda: len(self.net.hits("a.test", "/")) >= 1))
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("Sygnał zatrzymania", out)
        run = self.store.latest_run()
        self.assertEqual((run["status"], run["aktywny"]), (st.RUN_ZATRZYMANY, None))
        firm = self.store.get_firma(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_W_KOLEJCE, "stop_uzytkownika"))

    def test_sigkill_then_a_new_run_recovers_and_does_not_refetch_finished_pages(self):
        firma_id = self.site(4)
        proc = self.spawn("run", "--concurrency", "1")
        self.assertTrue(self.wait_for(lambda: len(self.net.hits("a.test", "/p1")) >= 1 or len(self.net.hits("a.test", "/")) >= 1))
        self.assertTrue(self.wait_for(lambda: self.store.get_firma(firma_id)["stron_pobranych"] >= 1))
        proc.kill()
        proc.wait(15)
        killed = self.store.get_firma(firma_id)
        self.assertEqual(killed["skan_status"], st.FIRMA_SKANOWANIE)  # osierocona dzierżawa
        self.assertIsNotNone(self.store.active_run())
        second = self.spawn("run", "--concurrency", "1")
        out, err = second.communicate(timeout=90)
        self.assertEqual(second.returncode, 0, err)
        self.assertIn("Odzyskano przerwane przebiegi", out)
        firm = self.store.get_firma(firma_id)
        self.assertEqual((firm["skan_status"], firm["stron_pobranych"]), (st.FIRMA_ZAKONCZONA, 4))
        paths = [e["path"] for e in self.net.log]
        self.assertEqual(paths.count("/robots.txt"), 1)  # robots z pamięci podręcznej
        refetched = [p for p in set(paths) if paths.count(p) > 1]
        self.assertLessEqual(len(refetched), 1, refetched)  # co najwyżej strona w locie w chwili SIGKILL


class RunnerHelperTests(unittest.TestCase):
    def setUp(self):
        self.logs = tempfile.mkdtemp(prefix="perno-logs-")
        self.addCleanup(shutil.rmtree, self.logs, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"PERNO_LOG_DIR": self.logs})
        env.start()
        self.addCleanup(env.stop)

    def test_tail_refuses_paths_outside_the_log_directory(self):
        with tempfile.TemporaryDirectory() as outside:
            secret = os.path.join(outside, "sekret.txt")
            with open(secret, "w") as handle:
                handle.write("hasło=x\n")
            self.assertEqual(runner.tail(secret), [])
        self.assertEqual(runner.tail(None), [])
        self.assertEqual(runner.tail(os.path.join(self.logs, "..", "config.env")), [])

    def test_tail_reads_the_last_lines_of_a_log_inside_the_directory(self):
        path = os.path.join(self.logs, "skraper_test_tail.log")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("".join(f"linia {i}\n" for i in range(100)))
        self.assertEqual(runner.tail(path, lines=3), ["linia 97", "linia 98", "linia 99"])

    def test_log_directory_defaults_to_the_project_logs_folder(self):
        with mock.patch.dict(os.environ, {"PERNO_LOG_DIR": ""}):
            self.assertEqual(runner.log_dir(), cfg.LOG_DIR)

    def test_plan_summary_arithmetic(self):
        lines = "\n".join(runner.plan_summary(ScanSettings.from_params({"concurrency": 30, "delay": 2.5}), 100))
        self.assertIn("Firm do przetworzenia: 100; równolegle: 30", lines)
        self.assertIn("odstęp co najmniej 2.5 s", lines)
        self.assertIn("bramka globalna 15/s", lines)
        self.assertIn("najwyżej", lines)
        self.assertIn("Firm do przetworzenia: 0; równolegle: 0", "\n".join(runner.plan_summary(ScanSettings.from_params(), 0)))

    def test_plan_summary_scales_the_typical_time_with_the_page_budget(self):
        def minutes(profile):
            text = "\n".join(runner.plan_summary(ScanSettings.from_params({"profile": profile, "concurrency": 10}), 100))
            return text, int(re.search(r"typowo ≈(\d+) min \(≈(\d+) stron", text).group(1))
        balanced, context = minutes("zbalansowany"), minutes("kontekst")
        self.assertIn("(≈60 stron na firmę)", balanced[0])
        self.assertIn("(≈25 stron na firmę)", context[0])
        self.assertLess(context[1], balanced[1])

    def test_raise_nofile_limit_returns_a_usable_number(self):
        self.assertGreaterEqual(runner.raise_nofile_limit(), 256)

    def test_signal_handler_sets_the_event_once_and_restores(self):
        event, lines = threading.Event(), []
        restore = runner.install_signal_handlers(event, lines.append)
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(event.wait(2))
            self.assertEqual(len(lines), 1)
            with mock.patch.object(os, "_exit") as hard_exit:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.1)
                hard_exit.assert_called_once_with(130)
        finally:
            restore()
        self.assertNotEqual(signal.getsignal(signal.SIGTERM).__name__ if hasattr(signal.getsignal(signal.SIGTERM), "__name__") else "", "handler")

    def test_signal_handlers_are_skipped_outside_the_main_thread(self):
        box = {}
        thread = threading.Thread(target=lambda: box.update(restore=runner.install_signal_handlers(threading.Event(), print)))
        thread.start()
        thread.join()
        self.assertTrue(callable(box["restore"]))
        box["restore"]()

    def test_logger_writes_timestamped_lines_from_many_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.log")
            logger = runner.RunLogger(path, echo=False)
            threads = [threading.Thread(target=lambda n=n: [logger(f"w{n} {i}") for i in range(50)]) for n in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            logger.close()
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        self.assertEqual(len(lines), 300)
        self.assertTrue(all(line[2] == ":" and " w" in line for line in lines))

    def test_spawn_detached_uses_an_argv_list_and_a_new_session(self):
        fake = mock.Mock(pid=4321)
        with mock.patch.object(runner.subprocess, "Popen", return_value=fake) as popen:
            pid, path = runner.spawn_detached(7, "/x/config.env")
        argv = popen.call_args[0][0]
        self.assertIsInstance(argv, list)
        self.assertEqual(pid, 4321)
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1:4], ["-u", "-m", "perno.cli.scrape"])  # moduł, nie ścieżka pliku: nie zależy od wrappera
        self.assertEqual(argv[4:], ["--config", "/x/config.env", "run", "--run-id", "7"])  # --config przed podpoleceniem
        kwargs = popen.call_args[1]
        self.assertTrue(kwargs["start_new_session"] and kwargs["close_fds"])
        self.assertNotIn("shell", kwargs)
        self.assertTrue(path.startswith(self.logs) and path.endswith(".log"))

    def test_spawn_detached_really_starts_a_process_that_the_cli_accepts(self):
        """Bez atrapy Popen: argv zbudowany przez panel musi przejść parser scrape.py i domknąć przebieg."""
        with tempfile.TemporaryDirectory() as tmp:
            db_url = f"sqlite:///{os.path.join(tmp, 'spawn.db')}"
            config = os.path.join(tmp, "config.env")
            with open(config, "w", encoding="utf-8") as handle:
                handle.write("SKRAPER_CONTACT=ops@example.com\n")
            db = make_db(datetime.now, url=db_url)
            self.addCleanup(db.engine.dispose)
            store = Store(db)
            store.ensure_schema()
            run_id = store.start_run({"ustawienia": ScanSettings.from_params({}, {"SKRAPER_CONTACT": "ops@example.com"}).to_dict(),
                                      "firma_ids": None, "statuses": list(st.FIRMA_DOMYSLNIE_DO_SKANU)})
            with mock.patch.dict(os.environ, {"PERNO_DB_URL": db_url}):
                pid, path = runner.spawn_detached(run_id, config)
            end = time.monotonic() + 30
            while time.monotonic() < end and store.get_run(run_id)["aktywny"]:
                time.sleep(0.1)
            run = store.get_run(run_id)
            log = "\n".join(runner.tail(path, 50))
            self.assertIsNone(run["aktywny"], log)
            self.assertEqual(run["status"], st.RUN_ZAKONCZONY, log)
            self.assertEqual(run["pid"], pid)
            self.assertIn(f"Przebieg #{run_id}", log)
            self.assertNotIn("unrecognized arguments", log)

    def test_execute_run_closes_the_run_even_when_the_crawler_explodes(self):
        db = make_db(FakeClock())
        store = Store(db)
        store.ensure_schema()
        run_id = store.start_run({})

        class Exploding:
            def __init__(self, *args, **kwargs):
                pass

            def run(self):
                raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            runner.execute_run(store, ScanSettings.for_tests(), run_id, only_ids=None, log=lambda m: None,
                               crawler_factory=Exploding)
        run = store.get_run(run_id)
        self.assertEqual((run["status"], run["aktywny"]), (st.RUN_BLAD, None))
        self.assertIn("boom", run["statystyki"]["blad"])


if __name__ == "__main__":
    unittest.main()
