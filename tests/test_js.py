"""Logika po stronie przeglądarki (Node) oraz zgodność wspólnych wektorów filtrów z `RowFilter` na serwerze."""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import unittest
from datetime import date

from perno.domain.rowfilter import RowFilter
from tests.fixtures import make_db, put, sample_row

HERE = os.path.dirname(os.path.abspath(__file__))
NODE = shutil.which("node")


def node_major() -> int:
    try:
        out = subprocess.run([NODE, "--version"], capture_output=True, text=True, timeout=20).stdout
        return int(re.match(r"v(\d+)", out.strip()).group(1))
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        return 0


@unittest.skipUnless(NODE and node_major() >= 18, "brak Node.js 18+ — testy JavaScript pominięte")
class NodeSuiteTests(unittest.TestCase):
    def test_every_javascript_suite_passes(self):
        files = sorted(glob.glob(os.path.join(HERE, "js", "*.test.js")))
        self.assertGreaterEqual(len(files), 2)
        for path in files:
            with self.subTest(suite=os.path.basename(path)):
                result = subprocess.run([NODE, "--test", path], capture_output=True, text=True, timeout=180, cwd=os.path.dirname(HERE))
                self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-4000:])


class SharedVectorTests(unittest.TestCase):
    """Te same wiersze i filtry, które sprawdza `filters.js`, muszą dać te same wiersze z `RowFilter`."""

    TRI = {"yes": True, "no": False}

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "filter_cases.json"), encoding="utf-8") as handle:
            cls.vectors = json.load(handle)
        cls.database = make_db()
        rows = []
        for n, row in enumerate(cls.vectors["rows"], start=1):
            body = "<p>treść</p>" if row["has_body"] else ""
            rows.append(sample_row(
                n, segment=row["segment"], id=row["id"], company=row["company"], email=row["email"],
                website=row["website"], status=row["status"], country=row["country"], city=row["city"],
                address=row["address"], description=row["description"], generated_at=row["generated_at"],
                sent_at=row["sent_at"], body_html=body, body_text=body))
        put(cls.database, *rows)

    def to_filter(self, state: dict) -> RowFilter:
        kwargs = {}
        for key, value in state.items():
            if key in ("statuses", "segments", "countries"):
                kwargs[key] = tuple(value)
            elif key == "email":
                kwargs["has_email"] = self.TRI[value]
            elif key == "body":
                kwargs["has_body"] = self.TRI[value]
            elif key == "sentFrom":
                kwargs["sent_from"] = date.fromisoformat(value)
            elif key == "sentTo":
                kwargs["sent_to"] = date.fromisoformat(value)
            else:  # wektor, którego serwer nie umie odtworzyć, nie może po cichu przejść jako „zielony”
                self.fail(f"wektor używa wymiaru {key!r}, którego RowFilter nie obsługuje")
        return RowFilter(**kwargs)

    def test_the_dataset_was_loaded_whole(self):
        self.assertEqual(self.database.count_rows(), len(self.vectors["rows"]))
        self.assertGreaterEqual(len(self.vectors["cases"]), 20)

    def test_server_filter_selects_exactly_what_the_page_filter_selects(self):
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                flt = self.to_filter(case["filter"])
                found = [row["id"] for row in self.database.iter_rows(flt, ("id",))]
                self.assertEqual(sorted(found), sorted(case["expect"]))
                self.assertEqual(self.database.count_rows(flt), len(case["expect"]))


if __name__ == "__main__":
    unittest.main()
