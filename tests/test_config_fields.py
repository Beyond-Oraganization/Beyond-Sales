"""Tabela ustawień edytowalnych w panelu: walidacja każdego rodzaju pola i zgodność z regułami skrapera."""

from __future__ import annotations

import unittest

from perno.domain import config_fields as cf
from perno.scraper import settings as scraper_settings


def check(key, raw):
    return cf.validate(cf.BY_KEY[key], raw)


class MailboxFieldTests(unittest.TestCase):
    def test_every_mailbox_slot_has_the_same_five_fields(self):
        for slot in range(1, 11):
            prefix = "SMTP" if slot == 1 else f"SMTP{slot}"
            group = "mail" if slot == 1 else f"mail{slot}"
            keys = [f.key for f in cf.FIELDS if f.group == group and f.key.startswith(prefix + "_")]
            self.assertEqual(keys, [f"{prefix}_{name}" for name in ("HOST", "PORT", "USE_SSL", "USER", "PASSWORD")], slot)
            self.assertEqual(cf.mailbox_slot(group), slot)

    def test_only_mailbox_groups_have_a_slot(self):
        self.assertEqual([cf.mailbox_slot(key) for key in ("notify", "scraper", "mailx")], [None, None, None])

    def test_extra_mailboxes_are_validated_like_the_first(self):
        self.assertEqual(check("SMTP3_PORT", " 587 "), "587")
        for key, bad in (("SMTP3_PORT", "99999"), ("SMTP3_HOST", "złe host"), ("SMTP3_USER", "a b"), ("SMTP3_PASSWORD", "x" * 501)):
            with self.subTest(key=key), self.assertRaises(cf.ConfigValueError):
                check(key, bad)

    def test_passwords_of_every_slot_are_secrets(self):
        self.assertTrue(all(cf.BY_KEY[f"SMTP{n}_PASSWORD"].kind == cf.KIND_SECRET for n in range(2, 11)))

    def test_the_sender_name_stays_single_and_shared(self):
        self.assertEqual([f.key for f in cf.FIELDS if f.key.endswith("SENDER_NAME")], ["SENDER_NAME"])


class ValidationTests(unittest.TestCase):
    def test_numbers_are_checked_for_type_and_range(self):
        self.assertEqual(check("SMTP_PORT", " 587 "), "587")
        self.assertEqual(check("SMTP_PORT", "1"), "1")
        self.assertEqual(check("SMTP_PORT", "65535"), "65535")
        self.assertEqual(check("SKRAPER_DELAY", "2.5"), "2.5")
        self.assertEqual(check("SKRAPER_CONCURRENCY", "30.0"), "30")
        for key, bad in (("SMTP_PORT", "0"), ("SMTP_PORT", "65536"), ("SMTP_PORT", "abc"), ("SMTP_PORT", "25.5"),
                         ("SKRAPER_DELAY", "0.5"), ("SKRAPER_DELAY", "61"), ("SKRAPER_DELAY", "nan"), ("SKRAPER_DELAY", "inf"),
                         ("SKRAPER_IP_CAP", "11"), ("SKRAPER_MAX_PAGES", "5001"), ("SKRAPER_MAX_MB", "0")):
            with self.subTest(key=key, bad=bad), self.assertRaises(cf.ConfigValueError):
                check(key, bad)

    def test_bool_and_choice(self):
        self.assertEqual(check("SMTP_USE_SSL", "TRUE"), "true")
        self.assertEqual(check("NOTIFY_MACOS", "false"), "false")
        self.assertEqual(check("SKRAPER_PROFILE", "kontekst"), "kontekst")
        for key, bad in (("SMTP_USE_SSL", "tak"), ("SMTP_USE_SSL", "1"), ("SKRAPER_PROFILE", "szybki"), ("SKRAPER_STORE_HTML", "")):
            with self.subTest(key=key, bad=bad), self.assertRaises(cf.ConfigValueError):
                check(key, bad)

    def test_text_rules(self):
        self.assertEqual(check("SMTP_HOST", "smtp.zenbox.pl"), "smtp.zenbox.pl")
        self.assertEqual(check("NOTIFY_EMAIL", "ja@example.com"), "ja@example.com")
        self.assertEqual(check("NTFY_SERVER", "https://ntfy.example"), "https://ntfy.example")
        self.assertEqual(check("SKRAPER_CONTACT", "https://firma.pl/kontakt"), "https://firma.pl/kontakt")
        for key, bad in (("SMTP_HOST", "ma spację.pl"), ("SMTP_HOST", "-zly.pl"), ("NOTIFY_EMAIL", "bez-malpy"),
                         ("NOTIFY_EMAIL", "a b@c.pl"), ("NTFY_SERVER", "http://ntfy.sh"), ("SMTP_USER", "a b"),
                         ("SENDER_NAME", "x" * 101), ("SKRAPER_CONTACT", "ab"), ("SKRAPER_CONTACT", "a b c"),
                         ("SENDER_NAME", "linia\nnowa"), ("SMTP_USER", "a\x00b")):
            with self.subTest(key=key, bad=bad), self.assertRaises(cf.ConfigValueError):
                check(key, bad)

    def test_length_limits_are_inclusive(self):
        self.assertEqual(len(check("SMTP_USER", "u" * 200)), 200)
        self.assertEqual(len(check("SENDER_NAME", "n" * 100)), 100)
        self.assertEqual(len(check("SMTP_PASSWORD", "p" * 500)), 500)
        for key, bad in (("SMTP_USER", "u" * 201), ("SENDER_NAME", "n" * 101), ("SMTP_PASSWORD", "p" * 501), ("SMTP_USER", "a b")):
            with self.subTest(key=key), self.assertRaises(cf.ConfigValueError):
                check(key, bad)
        self.assertEqual(check("SMTP_USER", "x@example.com"), "x@example.com")

    def test_secrets_are_checked_without_echoing_them(self):
        self.assertEqual(check("NTFY_TOPIC", "bardzo-tajny-temat"), "bardzo-tajny-temat")
        self.assertEqual(check("HEALTHCHECK_URL", "https://hc-ping.com/abc"), "https://hc-ping.com/abc")
        for key, bad in (("NTFY_TOPIC", "krótki"), ("NTFY_TOPIC", "z spacją i ąę"), ("HEALTHCHECK_URL", "http://x.pl/tajne"),
                         ("SMTP_PASSWORD", "x" * 501)):
            with self.subTest(key=key), self.assertRaises(cf.ConfigValueError) as ctx:
                check(key, bad)
            self.assertNotIn(bad, str(ctx.exception))

    def test_smtp_password_accepts_any_printable_text(self):
        self.assertEqual(check("SMTP_PASSWORD", "  p@ss=wo#rd ąę 'x' \"y\" "), "p@ss=wo#rd ąę 'x' \"y\"")


class TableTests(unittest.TestCase):
    def test_database_keys_are_not_editable(self):
        self.assertFalse([key for key in cf.BY_KEY if key.startswith("DB_")])

    def test_every_field_belongs_to_a_known_group_and_keys_are_unique(self):
        groups = {key for key, _, _ in cf.GROUPS}
        self.assertEqual(len(cf.BY_KEY), len(cf.FIELDS))
        self.assertTrue(all(field.group in groups for field in cf.FIELDS))

    def test_numeric_fields_have_ranges_and_choices_have_options(self):
        for field in cf.FIELDS:
            with self.subTest(key=field.key):
                if field.kind in (cf.KIND_INT, cf.KIND_FLOAT):
                    self.assertIsNotNone(field.low)
                    self.assertIsNotNone(field.high)
                if field.kind == cf.KIND_CHOICE:
                    self.assertIn(field.default, field.choices)

    def test_scraper_ranges_match_the_scraper(self):
        mapping = {"SKRAPER_CONCURRENCY": "concurrency", "SKRAPER_DELAY": "delay", "SKRAPER_IP_CAP": "ip_cap",
                   "SKRAPER_MAX_RPS": "max_rps", "SKRAPER_MAX_DEPTH": "depth", "SKRAPER_MAX_PAGES": "max_pages",
                   "SKRAPER_MAX_MB": "max_mb", "SKRAPER_MAX_MINUTES": "max_minutes"}
        for key, name in mapping.items():
            with self.subTest(key=key):
                low, high = scraper_settings.LIMITS[name]
                self.assertEqual((cf.BY_KEY[key].low, cf.BY_KEY[key].high), (low, high))
                self.assertEqual(scraper_settings.CONFIG_KEYS[name], key)

    def test_scraper_choices_and_contact_rule_match(self):
        self.assertEqual(set(cf.BY_KEY["SKRAPER_STORE_HTML"].choices), set(scraper_settings.STORE_HTML_MODES))
        self.assertEqual(set(cf.BY_KEY["SKRAPER_PROFILE"].choices), set(scraper_settings.PROFILES))
        for sample in ("ops@example.com", "https://x.pl/k", "ab", "a b c", "x" * 201, "a" * 200):
            with self.subTest(sample=sample):
                self.assertEqual(bool(scraper_settings.CONTACT_RE.match(sample)), bool(cf._CONTACT_RE.fullmatch(sample)))

    def test_notification_keys_ask_for_a_daemon_restart(self):
        restart = {f.key for f in cf.FIELDS if f.restart == cf.RESTART_DEMON}
        self.assertEqual(restart, {"NOTIFY_MACOS", "NTFY_TOPIC", "NTFY_SERVER", "NOTIFY_EMAIL", "HEALTHCHECK_URL"})


if __name__ == "__main__":
    unittest.main()
