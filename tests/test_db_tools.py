"""Narzędzia bazy: adres i silnik, diagnostyka `perno.db`/`perno-dbcheck` oraz start panelu (`build_app`)."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import sqlalchemy as sa

from perno import db as dbmod
from perno import settings as cfg
from perno.cli import dbcheck, server
from perno.db import engine as engine_mod
from perno.settings import ConfigError, Environment
from tests.fixtures import FakeClock, make_db, put, sample_row

MYSQL = {"DB_NAME": "perno_maile", "DB_USER": "perno_app", "DB_PASSWORD": "tajne", "DB_HOST": "db.example.com", "DB_PORT": "3307"}


class BuildUrlTests(unittest.TestCase):
    def test_mysql_url_from_the_configuration(self):
        url = engine_mod.build_url(MYSQL, Environment())
        self.assertEqual((url.drivername, url.host, url.port, url.database, url.username),
                         ("mysql+pymysql", "db.example.com", 3307, "perno_maile", "perno_app"))
        self.assertEqual(url.query["charset"], "utf8mb4")

    def test_defaults_for_host_and_port(self):
        url = engine_mod.build_url({k: v for k, v in MYSQL.items() if k not in ("DB_HOST", "DB_PORT")}, Environment())
        self.assertEqual((url.host, url.port), ("127.0.0.1", 3306))

    def test_a_socket_replaces_tcp(self):
        url = engine_mod.build_url({**MYSQL, "DB_SOCKET": "/tmp/mysql.sock"}, Environment())  # noqa: S108
        self.assertEqual(url.query["unix_socket"], "/tmp/mysql.sock")  # noqa: S108

    def test_missing_credentials_are_named_without_leaking_values(self):
        with self.assertRaisesRegex(ConfigError, "DB_NAME, DB_PASSWORD") as caught:
            engine_mod.build_url({"DB_USER": "perno_app"}, Environment())
        self.assertNotIn("perno_app", str(caught.exception))

    def test_a_non_numeric_port_is_a_configuration_error(self):
        with self.assertRaisesRegex(ConfigError, "DB_PORT"):
            engine_mod.build_url({**MYSQL, "DB_PORT": "abc"}, Environment())

    def test_the_environment_override_wins_over_the_configuration(self):
        url = engine_mod.build_url(MYSQL, Environment(db_url="sqlite:///demo.db"))
        self.assertEqual((url.get_backend_name(), url.database), ("sqlite", "demo.db"))

    def test_engines_are_built_per_backend(self):
        memory = engine_mod.make_engine(sa.engine.make_url("sqlite://"))
        self.assertEqual(memory.dialect.name, "sqlite")
        with memory.connect() as conn:
            conn.execute(sa.text("CREATE TABLE t (x INTEGER)"))
        with memory.connect() as conn:  # StaticPool: ta sama baza w pamięci na każdym połączeniu
            self.assertEqual(conn.execute(sa.text("SELECT COUNT(*) FROM t")).scalar(), 0)
        mysql_engine = engine_mod.make_engine(engine_mod.build_url(MYSQL, Environment()))
        self.assertEqual(mysql_engine.dialect.name, "mysql")  # bez łączenia: silnik jest leniwy


class DbCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.url = f"sqlite:///{os.path.join(tmp.name, 'check.db')}"
        self.config = os.path.join(tmp.name, "config.env")
        open(self.config, "w").close()
        patcher = mock.patch.dict(os.environ, {cfg.ENV_DB_URL: self.url})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dbcheck.main([*argv, "--config", self.config])
        return code, out.getvalue(), err.getvalue()

    def test_init_creates_the_table_and_reports_the_state(self):
        code, out, _ = self.run_cli("--init")
        self.assertEqual(code, 0)
        self.assertIn("Tabele maile i podmioty są gotowe.", out)
        self.assertIn("Tabela maile: jest, wierszy: 0", out)
        self.assertIn("sqlite", out)

    def test_check_works_on_an_existing_table_and_does_not_change_it(self):
        self.run_cli("--init")
        code, out, _ = self.run_cli("--check")
        self.assertEqual((code, "gotowa" in out), (0, False))

    def test_check_on_an_empty_database_says_the_table_is_missing(self):
        code, out, _ = self.run_cli("--check")
        self.assertEqual(code, 0)
        self.assertIn("jeszcze nie istnieje", out)

    def test_upgrade_needs_the_table_first(self):
        code, _, err = self.run_cli("--upgrade")
        self.assertEqual(code, 1)
        self.assertIn("użyj najpierw --init", err)

    def test_upgrade_reports_when_there_is_nothing_to_add(self):
        self.run_cli("--init")
        code, out, _ = self.run_cli("--upgrade")
        self.assertEqual(code, 0)
        self.assertIn("Schemat jest aktualny", out)

    def test_a_search_collation_warning_is_printed_when_the_database_reports_one(self):
        self.run_cli("--init")
        real = dbmod.Database.check_connection

        def with_warning(database):
            return {**real(database), "search_warning": "kolumny maile mają złą kolację"}

        with mock.patch.object(dbmod.Database, "check_connection", with_warning):
            code, out, _ = self.run_cli("--check")
        self.assertEqual(code, 0)
        self.assertIn("UWAGA: kolumny maile mają złą kolację", out)
        _, quiet, _ = self.run_cli("--check")
        self.assertNotIn("kolację", quiet)  # sqlite nie ma tej kolacji do sprawdzenia

    def test_a_failing_collation_query_does_not_break_the_check(self):
        class Denied:
            def execute(self, *args, **kwargs):
                raise sa.exc.OperationalError("SELECT", {}, Exception("brak uprawnień do information_schema"))

        self.assertEqual(dbmod.database.search_collation_warning(Denied()), "")

    def test_a_configuration_problem_is_a_message_not_a_traceback(self):
        with mock.patch.dict(os.environ, {cfg.ENV_DB_URL: ""}):
            code, _, err = self.run_cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("DB_NAME", err)


class BuildAppTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.url = f"sqlite:///{os.path.join(tmp.name, 'app.db')}"
        self.config = os.path.join(tmp.name, "config.env")
        open(self.config, "w").close()
        patcher = mock.patch.dict(os.environ, {cfg.ENV_DB_URL: self.url})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = FakeClock(datetime.now())
        self.database = make_db(self.clock, url=self.url)
        self.addCleanup(self.database.engine.dispose)

    def build(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app = server.build_app(self.config)
        return app, out.getvalue()

    def test_a_missing_table_stops_the_server_with_a_hint(self):
        with self.database.engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE maile"))
        with self.assertRaisesRegex(dbmod.DbError, "Tabela maile nie istnieje"):
            self.build()

    def test_an_empty_database_gets_a_warning_but_the_app_starts(self):
        app, out = self.build()
        self.assertIn("baza jest pusta", out)
        self.assertEqual([page.key for page in app.ctx.pages], ["start", "wysylka", "odpowiedzi", "leady", "automat", "eksport", "skaper", "konfiguracja", "instrukcje", "dokumentacja", "zasady-maili", "zasady-projektu"])

    def test_the_search_collation_warning_is_shown_at_start(self):
        put(self.database, sample_row(1))
        real = dbmod.Database.check_connection
        with mock.patch.object(dbmod.Database, "check_connection",
                               lambda database: {**real(database), "search_warning": "zła kolacja kolumn"}):
            _, out = self.build()
        self.assertIn("UWAGA: zła kolacja kolumn", out)

    def test_a_populated_database_starts_silently(self):
        put(self.database, sample_row(1))
        _, out = self.build()
        self.assertEqual(out, "")

    def test_rows_stuck_in_sending_are_marked_as_errors_on_start(self):
        put(self.database, sample_row(1, status="wysylanie"))
        with self.database.engine.begin() as conn:
            conn.execute(sa.update(dbmod.maile).values(updated_at=datetime.now() - timedelta(hours=1)))
        _, out = self.build()
        self.assertIn("zawieszone wysyłki", out)
        self.assertEqual(self.database.get("eventy-001")["status"], "blad")

    def test_the_scraper_store_is_ready_to_use(self):
        put(self.database, sample_row(1))
        app, _ = self.build()
        self.assertEqual(app.ctx.scan_store.get().status_overview()["firmy"], 0)


if __name__ == "__main__":
    unittest.main()
