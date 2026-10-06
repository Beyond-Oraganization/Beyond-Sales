"""Firmy i ich adresy: schemat, przejęcie (`--adopt`) i zapis pól firmy do obu tabel (lustro dla starego kodu)."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import sqlalchemy as sa

from perno import db as dbmod
from perno import settings as cfg
from perno.cli import dbcheck
from perno.db import NotAdopted
from perno.db.automat_store import AutomatStore
from perno.domain import statuses as st
from tests.fixtures import FakeClock, assert_mirror_consistent, make_db, old_database, put, sample_row


def scalar(database, sql: str, **params):
    with database.engine.connect() as conn:
        return conn.execute(sa.text(sql), params).scalar()


def table_rows(database, table: str) -> list:
    with database.engine.connect() as conn:
        stmt = sa.select(sa.text("*")).select_from(sa.table(table)).order_by(sa.text("id"))
        return [dict(r) for r in conn.execute(stmt).mappings()]


class AdoptionTests(unittest.TestCase):
    def test_every_old_row_becomes_a_company_with_one_main_address(self):
        database = old_database(3)
        database.upgrade_schema()
        report = database.adopt()
        self.assertEqual((report.companies_added, report.rows_linked), (3, 3))
        companies, mails = table_rows(database, "podmioty"), table_rows(database, "maile")
        self.assertEqual([c["id"] for c in companies], ["eventy-001", "eventy-002", "eventy-003"])
        self.assertEqual({(m["company_id"], m["position"]) for m in mails},
                         {("eventy-001", 1), ("eventy-002", 1), ("eventy-003", 1)})
        self.assertEqual(companies[0]["company"], "Firma 1")
        self.assertEqual((companies[0]["segment"], companies[0]["row_number"]), ("eventy", 1))
        assert_mirror_consistent(self, database)

    def test_adoption_changes_nothing_in_the_old_columns(self):
        database = old_database(3)
        before = table_rows(database, "maile")
        database.upgrade_schema()
        database.adopt()
        after = table_rows(database, "maile")
        for old, new in zip(before, after):
            self.assertEqual({key: new[key] for key in old}, old)

    def test_adoption_is_idempotent(self):
        database = old_database(2)
        database.upgrade_schema()
        database.adopt()
        again = database.adopt()
        self.assertEqual((again.companies_added, again.rows_linked), (0, 0))
        self.assertEqual(scalar(database, "SELECT COUNT(*) FROM podmioty"), 2)

    def test_a_row_inserted_by_older_code_is_adopted_at_the_next_start(self):
        database = old_database(2)
        database.upgrade_schema()
        database.adopt()
        with database.engine.begin() as conn:  # tak wstawia wiersz poprzednia wersja programu: bez firmy
            conn.execute(sa.text("INSERT INTO maile (id, segment, company, status, retry_count) "
                                 "VALUES ('eventy-n001', 'eventy', 'Nowa', 'pominieto_brak_email', 0)"))
        self.assertEqual(database.adoption_state().orphans, 1)
        self.assertEqual(database.prepare(), [])
        self.assertEqual(database.adoption_state().orphans, 0)
        self.assertEqual(scalar(database, "SELECT company FROM podmioty WHERE id = 'eventy-n001'"), "Nowa")
        assert_mirror_consistent(self, database)

    def test_a_missing_company_of_a_main_address_is_rebuilt(self):
        """Kopia zapasowa `maile` wystarcza do odtworzenia firm: firma powstaje z wiersza-kotwicy."""
        database = old_database(2)
        database.upgrade_schema()
        database.adopt()
        with database.engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM podmioty WHERE id = 'eventy-002'"))
        self.assertEqual(database.adopt().companies_added, 1)
        self.assertEqual(scalar(database, "SELECT company FROM podmioty WHERE id = 'eventy-002'"), "Firma 2")

    def test_the_program_refuses_a_database_with_mails_but_no_companies(self):
        database = old_database(2)
        database.upgrade_schema()
        with self.assertRaisesRegex(NotAdopted, "--adopt"):
            database.prepare()

    def test_an_empty_database_needs_no_adoption(self):
        self.assertEqual(make_db().prepare(), [])

    def test_prepare_upgrades_and_continues_when_companies_exist(self):
        database = old_database(2)
        database.upgrade_schema()
        database.adopt()
        self.assertEqual(database.prepare(), [])

    def test_diagnostics_report_every_kind_of_inconsistency(self):
        database = old_database(2)
        database.upgrade_schema()
        database.adopt()
        with database.engine.begin() as conn:
            conn.execute(sa.text("UPDATE maile SET country = 'DE' WHERE id = 'eventy-001'"))  # lustro rozjechane z firmą
            conn.execute(sa.text("INSERT INTO podmioty (id, segment) VALUES ('eventy-099', 'eventy')"))  # firma bez maila
            conn.execute(sa.text("UPDATE maile SET company_id = 'nie-ma' WHERE id = 'eventy-002'"))  # firma nie istnieje
        found = database.diagnose()
        self.assertEqual((found.mirror_drift, found.companies_without_mail, found.mails_without_company),
                         (1, 2, 1))  # eventy-002 straciło firmę, więc jej firma też nie ma maila
        self.assertFalse(found.clean)


class UniqueAddressTests(unittest.TestCase):
    def insert(self, database, mail_id: str, email, company_id: str = "eventy-001"):
        with database.engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO maile (id, segment, email, status, retry_count, company_id, position) "
                                 "VALUES (:id, 'eventy', :email, 'do_wyslania', 0, :company, 2)"),
                         {"id": mail_id, "email": email, "company": company_id})

    def test_the_same_address_twice_in_one_company_is_refused_by_the_database(self):
        database = make_db()
        put(database, sample_row(1, email="a@example.com"))
        with self.assertRaises(sa.exc.IntegrityError):
            self.insert(database, "eventy-001~2", "a@example.com")

    def test_the_same_address_in_two_companies_is_allowed(self):
        database = make_db()
        put(database, sample_row(1, email="a@example.com"), sample_row(2, email="a@example.com"))
        self.assertEqual(scalar(database, "SELECT COUNT(*) FROM maile WHERE email = 'a@example.com'"), 2)

    def test_rows_without_an_address_do_not_collide(self):
        database = make_db()
        put(database, sample_row(1, email="", status=st.STATUS_SKIPPED_NO_EMAIL))
        self.insert(database, "eventy-001~2", None)
        self.insert(database, "eventy-001~3", None)
        self.assertEqual(scalar(database, "SELECT COUNT(*) FROM maile WHERE company_id = 'eventy-001'"), 3)


class CompanyWriteTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        put(self.database, sample_row(1, country="PL", city="Kraków", tag="a"), sample_row(2, status=st.STATUS_SENT))

    def company(self, company_id: str = "eventy-001") -> dict:
        return next(c for c in table_rows(self.database, "podmioty") if c["id"] == company_id)

    def test_new_rows_get_a_company_and_a_main_address(self):
        self.assertEqual([(c["id"], c["company"], c["country"], c["city"]) for c in table_rows(self.database, "podmioty")],
                         [("eventy-001", "Firma 1", "PL", "Kraków"), ("eventy-002", "Firma 2", None, None)])
        self.assertEqual(scalar(self.database, "SELECT position FROM maile WHERE id = 'eventy-001'"), 1)
        assert_mirror_consistent(self, self.database)

    def test_inserting_an_existing_id_again_is_an_error(self):
        with self.assertRaises(sa.exc.IntegrityError):
            put(self.database, sample_row(1))

    def test_edit_of_company_fields_goes_to_the_company_and_the_mirror(self):
        row = self.database.apply_edit("eventy-001", {"country": "Niemcy", "city": "Berlin", "subject": "Nowy"})
        self.assertEqual((row["country"], row["city"], row["subject"]), ("DE", "Berlin", "Nowy"))
        self.assertEqual((self.company()["country"], self.company()["city"]), ("DE", "Berlin"))
        assert_mirror_consistent(self, self.database)

    def test_edit_of_a_mail_field_leaves_the_company_alone(self):
        stamp = self.company()["updated_at"]
        self.database.apply_edit("eventy-001", {"subject": "Nowy"})
        self.assertEqual(self.company()["updated_at"], stamp)

    def test_edit_of_a_missing_row_is_not_found(self):
        with self.assertRaises(dbmod.RowNotFound):
            self.database.apply_edit("nie-ma", {"city": "x"})

    def test_bulk_update_writes_status_to_the_mail_and_data_to_the_company(self):
        with self.database.transaction() as conn:
            self.database.update_rows(conn, [
                ("eventy-001", {"status": st.STATUS_ERROR, "country": "DE", "city": None}),
                ("eventy-002", {"country": "DE", "city": None})], datetime(2026, 10, 6, 12, 0, 0))
        self.assertEqual([(c["country"], c["city"]) for c in table_rows(self.database, "podmioty")], [("DE", None), ("DE", None)])
        self.assertEqual(scalar(self.database, "SELECT status FROM maile WHERE id = 'eventy-001'"), st.STATUS_ERROR)
        assert_mirror_consistent(self, self.database)

    def test_generated_rows_create_and_update_companies_but_skip_protected_mails(self):
        stats = self.database.upsert_generated([
            sample_row(1, company="Zmieniona", tag="b"), sample_row(2, company="Nie ruszać"), sample_row(3)])
        self.assertEqual(stats, {"inserted": 1, "updated": 1, "skipped": 1})
        self.assertEqual((self.company()["company"], self.company()["tag"]), ("Zmieniona", "b"))
        self.assertEqual(self.company("eventy-002")["company"], "Firma 2")  # mail wysłany: firma też bez zmian
        self.assertEqual(self.company("eventy-003")["company"], "Firma 3")
        assert_mirror_consistent(self, self.database)

    def test_enrichment_writes_the_profile_to_the_company(self):
        self.database.apply_edit("eventy-001", {"status": st.STATUS_AWAITING_COPY})
        written = self.database.apply_enrichment(
            [("eventy-001", {"email": "n@example.com", "subject": "S", "body_html": "<p>x</p>", "city": "Gdańsk",
                             "tag": "t"})], (st.STATUS_AWAITING_COPY,))
        self.assertEqual(written, 1)
        self.assertEqual((self.company()["city"], self.company()["tag"]), ("Gdańsk", "t"))
        self.assertEqual(scalar(self.database, "SELECT status FROM maile WHERE id = 'eventy-001'"), st.STATUS_PENDING)
        assert_mirror_consistent(self, self.database)

    def test_enrichment_skips_a_mail_that_changed_status_meanwhile(self):
        written = self.database.apply_enrichment(
            [("eventy-002", {"email": "n@example.com", "city": "Gdańsk"})], (st.STATUS_AWAITING_COPY,))
        self.assertEqual(written, 0)
        self.assertIsNone(self.company("eventy-002")["city"])


class BackupTests(unittest.TestCase):
    def test_backup_writes_the_companies_next_to_the_mails(self):
        from perno.services import firms_import

        database = make_db()
        put(database, sample_row(1, city="Kraków"))
        with tempfile.TemporaryDirectory() as tmp:
            path = firms_import.backup_table(database, tmp, "testem", now=datetime(2026, 10, 6, 12, 0, 0))
            self.assertEqual(sorted(os.listdir(tmp)), ["Maile_20261006_120000_przed-testem.csv",
                                                       "Podmioty_20261006_120000_przed-testem.csv"])
            self.assertTrue(path.endswith("Maile_20261006_120000_przed-testem.csv"))
            with open(os.path.join(tmp, "Podmioty_20261006_120000_przed-testem.csv"), encoding="utf-8") as handle:
                self.assertIn("eventy-001", handle.read())

    def test_backup_of_a_database_without_companies_writes_only_the_mails(self):
        from perno.services import firms_import

        database = old_database(1)
        with tempfile.TemporaryDirectory() as tmp:
            firms_import.backup_table(database, tmp, "x")
            self.assertEqual(len(os.listdir(tmp)), 1)


class AdoptCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.backups = os.path.join(tmp.name, "backup")
        self.url = f"sqlite:///{os.path.join(tmp.name, 'adopt.db')}"
        self.config = os.path.join(tmp.name, "config.env")
        open(self.config, "w").close()
        for patcher in (mock.patch.dict(os.environ, {cfg.ENV_DB_URL: self.url}),
                        mock.patch.object(cfg, "BACKUP_DIR", self.backups),
                        mock.patch.object(dbcheck, "panel_listening", return_value=False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.database = old_database(2, url=self.url, clock=FakeClock())
        self.addCleanup(self.database.engine.dispose)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dbcheck.main([*argv, "--config", self.config])
        return code, out.getvalue(), err.getvalue()

    def test_adopt_needs_the_upgrade_first(self):
        code, _, err = self.run_cli("--adopt")
        self.assertEqual(code, 1)
        self.assertIn("--upgrade", err)
        self.assertFalse(os.path.exists(self.backups))

    def test_upgrade_then_adopt_makes_a_backup_and_links_every_mail(self):
        self.assertEqual(self.run_cli("--upgrade")[0], 0)
        code, out, _ = self.run_cli("--adopt")
        self.assertEqual(code, 0, out)
        self.assertIn("założono firm: 2", out)
        self.assertIn("Maile i firmy są spójne.", out)
        self.assertEqual(sorted(name.split("_")[0] for name in os.listdir(self.backups)), ["Maile", "Podmioty"])
        self.assertTrue(all("przejecie-firm" in name for name in os.listdir(self.backups)))
        self.assertEqual(self.database.adoption_state().orphans, 0)

    def test_adopt_refuses_while_the_daemon_runs_and_changes_nothing(self):
        self.run_cli("--upgrade")
        store = AutomatStore(self.database)
        store.ensure_schema()
        store.start_run(4242)
        code, _, err = self.run_cli("--adopt")
        self.assertEqual(code, 1)
        self.assertIn("demon Automatu", err)
        self.assertEqual(self.database.adoption_state().companies, 0)
        self.assertFalse(os.path.exists(self.backups))

    def test_adopt_refuses_while_the_panel_listens(self):
        self.run_cli("--upgrade")
        with mock.patch.object(dbcheck, "panel_listening", return_value=True):
            code, _, err = self.run_cli("--adopt")
        self.assertEqual(code, 1)
        self.assertIn("panel", err)

    def test_allow_running_overrides_both_guards(self):
        self.run_cli("--upgrade")
        store = AutomatStore(self.database)
        store.ensure_schema()
        store.start_run(4242)
        with mock.patch.object(dbcheck, "panel_listening", return_value=True):
            code, _, _ = self.run_cli("--adopt", "--allow-running")
        self.assertEqual(code, 0)

    def test_check_reports_unadopted_rows(self):
        self.run_cli("--upgrade")
        code, out, _ = self.run_cli("--check")
        self.assertEqual(code, 0)
        self.assertIn("2 maili bez firmy", out)


if __name__ == "__main__":
    unittest.main()
