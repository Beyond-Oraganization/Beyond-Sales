from __future__ import annotations

import contextlib
import glob
import io
import os
import shutil
import socket
import tempfile
import unittest
from unittest import mock

from perno import db as dbmod
from perno import settings as cfg
from perno.cli import migrate
from perno.db import legacy_csv
from tests.fixtures import sample_row


def _real_csv():
    candidates = [os.path.join(cfg.BASE_DIR, "Maile.csv")] + sorted(
        glob.glob(os.path.join(cfg.BASE_DIR, "backup", "Maile_*_przed-migracja.csv")))
    return next((p for p in candidates if os.path.exists(p)), None)


REAL_CSV = _real_csv()


def synthetic_rows():
    # kolejność jak w prawdziwym pliku: eventy wg row_number, potem driverzy
    return [
        sample_row(1, status="wyslano", sent_at="2026-09-29T14:23:11", company="Zażółć gęślą jaźń",
                   body_html="<p>a</p>\n<p>\"b\", c; d</p>", body_text="l1\r\nl2", retry_count="1"),
        sample_row(3, status="pominieto_klient", email="", body_html="", body_text="", inline_images=""),
        sample_row(4, status="blad", error="SMTP timeout", retry_count="2"),
        sample_row(2, segment="driverzy", status="do_wyslania"),
    ]


class MigrationCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.csv_path = os.path.join(self.tmp, "Maile.csv")
        legacy_csv.write_rows(self.csv_path, synthetic_rows())
        self.original_hash = migrate.sha256_of(self.csv_path)
        for patcher in (
            mock.patch.dict(os.environ, {"PERNO_DB_URL": f"sqlite:///{os.path.join(self.tmp, 'mig.db')}"}),
            mock.patch.object(cfg, "BACKUP_DIR", os.path.join(self.tmp, "backup")),
            mock.patch.object(cfg, "INDEX_HTML", os.path.join(self.tmp, "index.html")),
            mock.patch.object(cfg, "LEGACY_CSV", self.csv_path),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = os.path.join(self.tmp, "none.env")

    def run_migrate(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = migrate.main([*argv, "--config", self.config, "--server-port", "1"])
        return code, out.getvalue(), err.getvalue()

    def db(self):
        return dbmod.Database.from_config({})


class ValidationTests(MigrationCase):
    def test_check_reports_counts_and_hash(self):
        code, out, _ = self.run_migrate("--check")
        self.assertEqual(code, 0)
        self.assertIn("4 wierszy", out)
        self.assertIn(self.original_hash, out)

    def rewrite(self, mutate):
        rows = synthetic_rows()
        mutate(rows)
        legacy_csv.write_rows(self.csv_path, rows)

    def test_rejects_bad_status_duplicate_id_and_mismatched_id(self):
        def mutate(rows):
            rows[0]["status"] = "wysłane"
            rows[2]["id"] = rows[1]["id"]
            rows[3]["id"] = "eventy-999"
        self.rewrite(mutate)
        code, _, err = self.run_migrate("--check")
        self.assertEqual(code, 1)
        for expected in ("nieznany status", "zduplikowane id", "id nie zgadza się"):
            self.assertIn(expected, err)

    def test_rejects_sending_status_bad_dates_and_bad_header(self):
        self.rewrite(lambda rows: rows[0].update(status="wysylanie", sent_at="wczoraj"))
        code, _, err = self.run_migrate("--check")
        self.assertEqual(code, 1)
        self.assertIn("wysylanie", err)
        self.assertIn("nieprawidłowa data", err)

        with open(self.csv_path, "w", encoding="utf-8") as f:
            f.write("id,segment\n1,a\n")
        code, _, err = self.run_migrate("--check")
        self.assertEqual(code, 1)
        self.assertIn("Nagłówek CSV", err)

    def test_warnings_do_not_block(self):
        self.rewrite(lambda rows: rows[0].update(sent_at=""))
        code, out, _ = self.run_migrate("--check")
        self.assertEqual(code, 0)
        self.assertIn("ostrzeżenie", out)


class MigrationTests(MigrationCase):
    def test_full_migration_moves_csv_and_preserves_everything(self):
        code, out, err = self.run_migrate()
        self.assertEqual(code, 0, err + out)
        database = self.db()
        self.assertEqual(database.count_rows(), 4)
        self.assertEqual(database.get("eventy-001")["sent_at"], "2026-09-29T14:23:11")
        self.assertEqual(database.get("eventy-004")["retry_count"], "2")

        self.assertFalse(os.path.exists(self.csv_path))
        backups = glob.glob(os.path.join(cfg.BACKUP_DIR, "Maile_*_przed-migracja.csv"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(migrate.sha256_of(backups[0]), self.original_hash)
        self.assertEqual(len(glob.glob(os.path.join(cfg.BACKUP_DIR, "MIGRACJA_*.txt"))), 1)
        self.assertFalse(os.path.exists(cfg.INDEX_HTML))  # podgląd powstaje tylko na żądanie (dashboard.py)

    def test_row_order_in_the_csv_does_not_matter(self):
        legacy_csv.write_rows(self.csv_path, list(reversed(synthetic_rows())))
        code, out, err = self.run_migrate()
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.db().count_rows(), 4)

    def test_export_legacy_reproduces_the_original_bytes(self):
        self.run_migrate()
        out_path = os.path.join(self.tmp, "rollback.csv")
        code, out, _ = self.run_migrate("--export-legacy", out_path)
        self.assertEqual(code, 0)
        self.assertEqual(migrate.sha256_of(out_path), self.original_hash)
        self.assertIn(self.original_hash, out)

    def test_refuses_non_empty_table_and_leaves_csv_alone(self):
        self.assertEqual(self.run_migrate()[0], 0)
        shutil.copy(glob.glob(os.path.join(cfg.BACKUP_DIR, "Maile_*.csv"))[0], self.csv_path)
        code, _, err = self.run_migrate()
        self.assertEqual(code, 1)
        self.assertIn("nie jest pusta", err)
        self.assertTrue(os.path.exists(self.csv_path))
        self.assertEqual(self.db().count_rows(), 4)

    def test_refuses_while_the_server_is_running(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = migrate.main(["--config", self.config, "--server-port", str(port)])
        self.assertEqual(code, 1)
        self.assertIn("server.py", err.getvalue())
        self.assertTrue(os.path.exists(self.csv_path))

    def test_failed_verification_rolls_back_and_keeps_csv(self):
        failing = [("V3 pola wszystkich wierszy", False, "sztuczny błąd")]
        with mock.patch.object(migrate, "run_checks", return_value=failing):
            code, _, err = self.run_migrate()
        self.assertEqual(code, 1)
        self.assertIn("wycofano transakcję", err)
        self.assertEqual(self.db().count_rows(), 0)
        self.assertTrue(os.path.exists(self.csv_path))
        self.assertFalse(os.path.exists(cfg.BACKUP_DIR))

    def test_missing_csv_and_missing_table_messages(self):
        os.remove(self.csv_path)
        code, _, err = self.run_migrate()
        self.assertEqual(code, 1)
        self.assertIn("Brak pliku", err)


class VerifyTests(MigrationCase):
    def test_verify_only_passes_then_detects_drift(self):
        self.run_migrate()
        code, out, _ = self.run_migrate("--verify-only")
        self.assertEqual(code, 0, out)
        self.assertIn("V4", out)

        self.db().apply_edit("driverzy-002", {"subject": "zmieniony"})
        code, out, _ = self.run_migrate("--verify-only")
        self.assertEqual(code, 2)
        self.assertIn("BŁĄD V3", out)

    def test_run_checks_flags_each_kind_of_difference(self):
        rows = synthetic_rows()
        good = [dbmod.to_api({**dbmod.from_api(r), "updated_at": None}) for r in rows]
        self.assertTrue(all(ok for _, ok, _ in migrate.run_checks(good, rows, self.csv_path)))
        missing = migrate.run_checks(good[:-1], rows, self.csv_path)
        self.assertFalse(dict((n[:2], ok) for n, ok, _ in missing)["V1"])
        changed = [dict(r) for r in good]
        changed[0]["email"] = "inny@example.com"
        self.assertFalse(dict((n[:2], ok) for n, ok, _ in migrate.run_checks(changed, rows, self.csv_path))["V3"])


@unittest.skipUnless(REAL_CSV, "brak prawdziwego CSV")
class RealCsvTests(MigrationCase):
    def test_real_history_migrates_cleanly_on_a_scratch_database(self):
        shutil.copy(REAL_CSV, self.csv_path)
        code, out, err = self.run_migrate()
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.db().count_rows(), 100)
        self.assertEqual(self.db().status_counts().get("wyslano"), 98)


if __name__ == "__main__":
    unittest.main()
