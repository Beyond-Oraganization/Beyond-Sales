"""Punkty wejścia: skrypty w katalogu głównym, polecenia konsolowe z pyproject i skrypty wymieniane w README."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
import unittest
from importlib import import_module

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_SCRIPTS = ("server.py", "send_emails.py", "scrape.py", "firmy.py", "wypelnij.py", "migrate_csv_to_sql.py",
                "build_seed_data.py", "dashboard.py")


def read(name: str) -> str:
    with open(os.path.join(BASE, name), encoding="utf-8") as handle:
        return handle.read()


class RootScriptTests(unittest.TestCase):
    """Każdy skrypt z README startuje i pokazuje pomoc (to też łapie zepsute wrappery po przeniesieniu kodu)."""

    def test_every_root_script_prints_its_help(self):
        for script in ROOT_SCRIPTS:
            with self.subTest(script=script):
                done = subprocess.run([sys.executable, os.path.join(BASE, script), "--help"], capture_output=True, text=True,
                                      cwd=BASE, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr[-800:])
                self.assertIn("usage:", done.stdout.lower())

    def test_python_dash_m_perno_db_still_works(self):
        done = subprocess.run([sys.executable, "-m", "perno.db", "--help"], capture_output=True, text=True, cwd=BASE, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        self.assertIn("--check", done.stdout)


class ConsoleScriptTests(unittest.TestCase):
    def test_every_console_script_points_at_an_importable_function(self):
        scripts = tomllib.loads(read("pyproject.toml"))["project"]["scripts"]
        self.assertGreaterEqual(len(scripts), 8)
        for name, target in scripts.items():
            with self.subTest(script=name):
                module, _, function = target.partition(":")
                self.assertTrue(callable(getattr(import_module(module), function, None)), target)

    def test_every_root_script_has_a_console_script_twin(self):
        targets = set(tomllib.loads(read("pyproject.toml"))["project"]["scripts"].values())
        for script in ROOT_SCRIPTS:
            with self.subTest(script=script):
                wrapper = read(script)
                match = re.search(r"from (perno\.cli\.\w+) import main", wrapper)
                self.assertIsNotNone(match, script)
                self.assertIn(f"{match.group(1)}:main", targets)


class ReadmeTests(unittest.TestCase):
    def test_scripts_mentioned_in_the_readme_exist(self):
        mentioned = set(re.findall(r"python3? ([a-z_]+\.py)", read("README.md")))
        self.assertGreaterEqual(len(mentioned), 5)
        for script in sorted(mentioned):
            with self.subTest(script=script):
                self.assertTrue(os.path.exists(os.path.join(BASE, script)), f"README wspomina {script}, którego nie ma")


if __name__ == "__main__":
    unittest.main()
