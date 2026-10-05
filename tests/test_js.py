"""Logika po stronie przeglądarki (Node): każdy plik tests/js/*.test.js przechodzi. Wiersze, filtry i liczniki liczy serwer
(tests/test_listing.py); wektory tests/listing_cases.json sprawdza także filters.test.js (szybkie widoki)."""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import unittest

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


if __name__ == "__main__":
    unittest.main()
