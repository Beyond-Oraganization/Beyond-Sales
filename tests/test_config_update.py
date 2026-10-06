"""Zapis config.env: podmiana w miejscu, odkomentowanie wzoru, usuwanie, atomowość, kopia .bak i prawa 0600."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
import unittest

from perno import settings as cfg

SAMPLE = """# Wysyłka
SMTP_HOST=smtp.example.com
SMTP_PORT=465
# SKRAPER_MAX_PAGES=500
SKRAPER_DELAY=2.5

# Notatka użytkownika
SENDER_NAME=Roman
"""


class UpdateCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "config.env")
        self.dir = tmp.name
        self.write(SAMPLE)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()


class EditTests(UpdateCase):
    def test_replaces_in_place_and_keeps_comments_and_order(self):
        changed = cfg.update_config(self.path, {"SMTP_PORT": "587"})
        self.assertEqual(changed, ["SMTP_PORT"])
        self.assertEqual(self.read(), SAMPLE.replace("SMTP_PORT=465", "SMTP_PORT=587"))

    def test_uncomments_a_commented_template_in_its_place(self):
        cfg.update_config(self.path, {"SKRAPER_MAX_PAGES": "100"})
        self.assertIn("SMTP_PORT=465\nSKRAPER_MAX_PAGES=100\nSKRAPER_DELAY=2.5", self.read())

    def test_new_keys_are_appended(self):
        cfg.update_config(self.path, {"NTFY_TOPIC": "bardzo-tajny-temat"})
        self.assertTrue(self.read().endswith("SENDER_NAME=Roman\nNTFY_TOPIC=bardzo-tajny-temat\n"))

    def test_none_removes_the_key_but_not_its_commented_template(self):
        cfg.update_config(self.path, {"SMTP_PORT": None, "SKRAPER_MAX_PAGES": None})
        self.assertNotIn("SMTP_PORT", self.read())
        self.assertIn("# SKRAPER_MAX_PAGES=500", self.read())

    def test_duplicates_collapse_to_one_line_with_the_new_value(self):
        self.write("A=1\nB=2\nA=3\n")
        cfg.update_config(self.path, {"A": "9"})
        self.assertEqual(self.read(), "A=9\nB=2\n")
        self.assertEqual(cfg.load_config(self.path), {"A": "9", "B": "2"})

    def test_value_with_equals_hash_and_unicode_roundtrips(self):
        cfg.update_config(self.path, {"SMTP_PASSWORD": "p@ss=wo#rd ąę 'x' \"y\""})
        self.assertEqual(cfg.load_config(self.path)["SMTP_PASSWORD"], "p@ss=wo#rd ąę 'x' \"y\"")

    def test_unchanged_value_writes_nothing_and_makes_no_backup(self):
        self.assertEqual(cfg.update_config(self.path, {"SMTP_PORT": "465"}), [])
        self.assertFalse(os.path.exists(self.path + ".bak"))

    def test_returns_only_keys_whose_effective_value_changed(self):
        self.assertEqual(cfg.update_config(self.path, {"SMTP_PORT": "587", "SMTP_HOST": "smtp.example.com"}), ["SMTP_PORT"])

    def test_creates_the_file_when_missing(self):
        os.unlink(self.path)
        self.assertEqual(cfg.update_config(self.path, {"SMTP_HOST": "h"}), ["SMTP_HOST"])
        self.assertEqual(self.read(), "SMTP_HOST=h\n")
        self.assertFalse(os.path.exists(self.path + ".bak"))

    def test_removing_everything_leaves_an_empty_file(self):
        self.write("A=1\n")
        cfg.update_config(self.path, {"A": None})
        self.assertEqual(self.read(), "")


class SafetyTests(UpdateCase):
    def test_backup_holds_the_previous_version_with_private_permissions(self):
        cfg.update_config(self.path, {"SMTP_PORT": "587"})
        with open(self.path + ".bak", encoding="utf-8") as f:
            self.assertEqual(f.read(), SAMPLE)
        self.assertEqual(stat.S_IMODE(os.stat(self.path + ".bak").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_rejects_bad_keys_and_values_without_touching_the_file(self):
        bad = [{"smtp_port": "1"}, {"SMTP PORT": "1"}, {"": "1"}, {"A": "x\ny"}, {"A": "x\ry"}, {"A": " x"}, {"A": "x "},
               {"A": "x\x00"}, {"1A": "x"}]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(cfg.ConfigError):
                cfg.update_config(self.path, changes)
        self.assertEqual(self.read(), SAMPLE)

    def test_rejects_the_whole_batch_when_one_entry_is_bad(self):
        with self.assertRaises(cfg.ConfigError):
            cfg.update_config(self.path, {"SMTP_PORT": "587", "bad key": "x"})
        self.assertEqual(self.read(), SAMPLE)

    def test_failed_write_leaves_the_original_and_no_temp_files(self):
        original_replace = os.replace

        def boom(*a, **k):
            raise OSError("dysk pełny")

        os.replace = boom
        try:
            with self.assertRaises(OSError):
                cfg.update_config(self.path, {"SMTP_PORT": "587"})
        finally:
            os.replace = original_replace
        self.assertEqual(self.read(), SAMPLE)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])

    def test_concurrent_writers_do_not_lose_updates(self):
        errors = []

        def worker(n):
            try:
                cfg.update_config(self.path, {f"KEY_{n}": str(n)})
            except Exception as exc:  # noqa: BLE001 — test zbiera każdy błąd wątku
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        loaded = cfg.load_config(self.path)
        self.assertEqual({k: v for k, v in loaded.items() if k.startswith("KEY_")}, {f"KEY_{n}": str(n) for n in range(12)})

    def test_configfile_notices_the_change_without_restart(self):
        config = cfg.ConfigFile(self.path)
        self.assertEqual(config.load()["SMTP_PORT"], "465")
        cfg.update_config(self.path, {"SMTP_PORT": "2525"})
        self.assertEqual(config.load()["SMTP_PORT"], "2525")


if __name__ == "__main__":
    unittest.main()
