"""Statyczny podgląd index.html: powstaje na żądanie, domyślnie bez treści maili."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from perno import settings as cfg
from perno.cli import dashboard as dashboard_cli
from perno.services import dashboard
from tests.fixtures import make_db, put, sample_row


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def embedded_rows(path: str) -> list:
    match = re.search(r"const MAILE_ROWS = (.*?);\nconst STATUS_LABELS", read_text(path), re.S)
    if match is None:
        raise AssertionError("brak danych w podglądzie")
    return json.loads(match.group(1).replace("<\\/script", "</script"))


class WriteDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "index.html")
        self.database = make_db()
        put(self.database, sample_row(1), sample_row(2, body_html="", body_text="", email=""))

    def test_default_preview_has_no_mail_bodies_but_flags_whether_they_exist(self):
        self.assertEqual(dashboard.write_dashboard(self.database, self.out), 2)
        rows = embedded_rows(self.out)
        self.assertEqual([r["id"] for r in rows], ["eventy-001", "eventy-002"])
        self.assertTrue(all("body_html" not in r and "body_text" not in r for r in rows))
        self.assertEqual([r["has_body"] for r in rows], [True, False])
        self.assertNotIn("Treść 1", read_text(self.out))

    def test_preview_inlines_the_shared_design_tokens(self):
        dashboard.write_dashboard(self.database, self.out)
        html = read_text(self.out)
        self.assertNotIn("/*__TOKENS__*/", html)
        self.assertIn("--accent:#0071e3", html)
        self.assertIn("prefers-color-scheme: dark", html)
        self.assertIn('<meta name="color-scheme" content="light dark">', html)
        self.assertNotIn("style=\"background:", html)  # kolor statusu przychodzi jako --c, nie jako tło

    def test_bodies_can_be_included_on_request(self):
        dashboard.write_dashboard(self.database, self.out, with_bodies=True)
        rows = embedded_rows(self.out)
        self.assertEqual(rows[0]["body_html"], "<p>Treść 1</p>")

    def test_closing_script_tags_in_data_cannot_break_out_of_the_page(self):
        put(self.database, sample_row(3, subject="</script><script>alert(1)</script>"))
        dashboard.write_dashboard(self.database, self.out)
        self.assertNotIn("</script><script>alert(1)", read_text(self.out))
        self.assertEqual(embedded_rows(self.out)[2]["subject"], "</script><script>alert(1)</script>")

    def test_file_is_replaced_atomically_without_leaving_a_temporary_copy(self):
        dashboard.write_dashboard(self.database, self.out)
        dashboard.write_dashboard(self.database, self.out)
        self.assertEqual(os.listdir(self.tmp.name), ["index.html"])


class DashboardCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "podglad.html")
        self.config = os.path.join(self.tmp.name, "config.env")
        open(self.config, "w").close()
        self.database = make_db(url=f"sqlite:///{os.path.join(self.tmp.name, 'cli.db')}")
        self.addCleanup(self.database.engine.dispose)
        put(self.database, sample_row(1))
        env = mock.patch.dict(os.environ, {cfg.ENV_DB_URL: str(self.database.engine.url)})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dashboard_cli.main(["--config", self.config, "--output", self.out, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_writes_the_light_preview_by_default(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("1 wierszy (bez treści maili)", out)
        self.assertNotIn("body_html", embedded_rows(self.out)[0])

    def test_with_bodies_flag(self):
        code, out, _ = self.run_cli("--with-bodies")
        self.assertEqual(code, 0)
        self.assertIn("z treściami maili", out)
        self.assertIn("body_html", embedded_rows(self.out)[0])

    def test_unwritable_target_is_reported_not_raised(self):
        self.out = os.path.join(self.tmp.name, "brak-katalogu", "podglad.html")
        code, out, err = self.run_cli()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("BŁĄD zapisu pliku", err)


if __name__ == "__main__":
    unittest.main()
