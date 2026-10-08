"""Ustawienia: migawka środowiska, config.env z cache'em po mtime i dane SMTP."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from perno import settings as cfg
from perno.settings import ConfigError, ConfigFile, Environment, ImapSettings, SmtpSettings

SMTP = {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "465", "SMTP_USER": "me@example.com", "SMTP_PASSWORD": "sekret"}


class EnvironmentTests(unittest.TestCase):
    def test_empty_environment_has_safe_defaults(self):
        env = Environment.from_environ({})
        self.assertEqual((env.db_url, env.log_dir, env.ssl_cert_file), ("", "", ""))
        self.assertFalse(env.scraper_allow_private)
        self.assertEqual(dict(env.scraper_hosts), {})
        self.assertTrue(env.real_smtp_allowed)

    def test_test_database_blocks_real_smtp_unless_overridden(self):
        self.assertFalse(Environment.from_environ({cfg.ENV_DB_URL: "sqlite://"}).real_smtp_allowed)
        self.assertTrue(Environment.from_environ({cfg.ENV_DB_URL: "sqlite://", cfg.ENV_ALLOW_REAL_SMTP: "1"}).real_smtp_allowed)
        self.assertFalse(Environment.from_environ({cfg.ENV_DB_URL: "sqlite://", cfg.ENV_ALLOW_REAL_SMTP: "yes"}).real_smtp_allowed)

    def test_host_map_works_only_together_with_the_private_address_switch(self):
        hosts = "A.test=127.0.0.1, b.test = 10.0.0.2 ,broken,=1.1.1.1,c.test="
        without = Environment.from_environ({cfg.ENV_SCRAPER_HOSTS: hosts})
        self.assertEqual(dict(without.scraper_hosts), {})
        with_switch = Environment.from_environ({cfg.ENV_SCRAPER_HOSTS: hosts, cfg.ENV_SCRAPER_ALLOW_PRIVATE: "1"})
        self.assertTrue(with_switch.scraper_allow_private)
        self.assertEqual(dict(with_switch.scraper_hosts), {"a.test": "127.0.0.1", "b.test": "10.0.0.2"})

    def test_default_source_is_the_process_environment(self):
        with mock.patch.dict(os.environ, {cfg.ENV_LOG_DIR: "katalog-logow", cfg.ENV_SSL_CERT_FILE: "certyfikaty-ca.pem"}):
            env = Environment.from_environ()
        self.assertEqual((env.log_dir, env.ssl_cert_file), ("katalog-logow", "certyfikaty-ca.pem"))


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.env")

    def write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_missing_file_is_an_empty_configuration(self):
        source = ConfigFile(self.path)
        self.assertEqual(source.load(), {})
        self.assertFalse(source.exists())

    def test_parses_key_value_lines_and_skips_comments(self):
        self.write("# komentarz\n\nA=1\nB = dwa = trzy \nbez_znaku\n")
        self.assertEqual(ConfigFile(self.path).load(), {"A": "1", "B": "dwa = trzy"})

    def test_file_is_read_only_when_it_changes(self):
        self.write("A=1\n")
        source = ConfigFile(self.path)
        with mock.patch.object(cfg, "load_config", wraps=cfg.load_config) as reader:
            source.load()
            source.load()
            self.assertEqual(reader.call_count, 1)
            self.write("A=22\n")  # inny rozmiar, więc zmiana jest widoczna nawet przy tym samym mtime
            self.assertEqual(source.load(), {"A": "22"})
            self.assertEqual(reader.call_count, 2)

    def test_callers_cannot_corrupt_the_cached_values(self):
        self.write("A=1\n")
        source = ConfigFile(self.path)
        source.load()["A"] = "zmienione"
        self.assertEqual(source.load(), {"A": "1"})


BOX2 = {"SMTP2_HOST": "smtp2.example.com", "SMTP2_PORT": "587", "SMTP2_USER": "two@example.com", "SMTP2_PASSWORD": "sekret2"}


IMAP2 = {"IMAP2_HOST": "imap2.example.com"}


class ImapSettingsTests(unittest.TestCase):
    def test_login_and_password_default_to_the_smtp_ones_and_the_port_to_the_ssl_default(self):
        box = ImapSettings.from_config({**SMTP, **BOX2, **IMAP2}, 2)
        self.assertEqual((box.host, box.port, box.user, box.password, box.use_ssl, box.slot, box.address),
                         ("imap2.example.com", 993, "two@example.com", "sekret2", True, 2, "two@example.com"))
        plain = ImapSettings.from_config({**SMTP, **BOX2, **IMAP2, "IMAP2_USE_SSL": "false"}, 2)
        self.assertEqual((plain.port, plain.use_ssl), (143, False))

    def test_own_login_password_and_port_win_over_the_smtp_ones(self):
        own = {**SMTP, **BOX2, **IMAP2, "IMAP2_USER": "login2", "IMAP2_PASSWORD": "inne", "IMAP2_PORT": "1993"}
        box = ImapSettings.from_config(own, 2)
        self.assertEqual((box.user, box.password, box.port, box.address), ("login2", "inne", 1993, "two@example.com"))

    def test_the_first_mailbox_uses_unnumbered_keys(self):
        box = ImapSettings.from_config({**SMTP, "IMAP_HOST": "imap.example.com"})
        self.assertEqual((box.slot, box.user, box.address), (1, "me@example.com", "me@example.com"))

    def test_the_password_never_appears_in_the_representation(self):
        self.assertNotIn("sekret", repr(ImapSettings.from_config({**SMTP, "IMAP_HOST": "imap.example.com"})))

    def test_missing_pieces_are_named(self):
        with self.assertRaisesRegex(ConfigError, "IMAP2_HOST"):
            ImapSettings.from_config({**SMTP, **BOX2}, 2)
        with self.assertRaisesRegex(ConfigError, "SMTP2_USER"):
            ImapSettings.from_config({**IMAP2}, 2)
        with self.assertRaisesRegex(ConfigError, "IMAP2_PASSWORD albo SMTP2_PASSWORD"):
            ImapSettings.from_config({**BOX2, **IMAP2, "SMTP2_PASSWORD": ""}, 2)
        with self.assertRaisesRegex(ConfigError, "IMAP2_PORT"):
            ImapSettings.from_config({**BOX2, **IMAP2, "IMAP2_PORT": "x"}, 2)

    def test_only_mailboxes_with_an_imap_entry_receive_and_a_half_filled_one_is_an_error(self):
        config = {**SMTP, "IMAP_HOST": "imap.example.com", **BOX2, "SMTP30_HOST": "h", "IMAP30_HOST": "i30", "SMTP30_USER": "t@x.pl",
                  "SMTP30_PORT": "1", "SMTP30_PASSWORD": "p"}
        self.assertEqual(cfg.imap_slot_numbers(config), [1, 30])
        self.assertEqual([box.slot for box in cfg.imap_slots(config)], [1, 30])
        self.assertEqual(cfg.imap_slots({**SMTP, **BOX2}), [])
        with self.assertRaisesRegex(ConfigError, "IMAP2_HOST"):
            cfg.imap_slots({**SMTP, **BOX2, "IMAP2_PORT": "993"})


class SmtpSettingsTests(unittest.TestCase):
    def test_builds_typed_settings(self):
        smtp = SmtpSettings.from_config({**SMTP, "SMTP_USE_SSL": "False"})
        self.assertEqual((smtp.host, smtp.port, smtp.user, smtp.use_ssl), ("smtp.example.com", 465, "me@example.com", False))
        self.assertTrue(SmtpSettings.from_config(SMTP).use_ssl)

    def test_password_never_appears_in_the_representation(self):
        self.assertNotIn("sekret", repr(SmtpSettings.from_config(SMTP)))

    def test_missing_and_malformed_values_are_named(self):
        with self.assertRaisesRegex(ConfigError, "SMTP_HOST, SMTP_PASSWORD"):
            SmtpSettings.from_config({"SMTP_PORT": "465", "SMTP_USER": "u"})
        with self.assertRaisesRegex(ConfigError, "SMTP_PORT"):
            SmtpSettings.from_config({**SMTP, "SMTP_PORT": "x"})

    def test_the_first_mailbox_is_slot_one_and_the_rest_use_numbered_keys(self):
        second = SmtpSettings.from_config({**BOX2}, 2)
        self.assertEqual((second.host, second.port, second.user, second.slot), ("smtp2.example.com", 587, "two@example.com", 2))
        self.assertEqual(SmtpSettings.from_config(SMTP).slot, 1)
        with self.assertRaisesRegex(ConfigError, "SMTP2_PASSWORD"):
            SmtpSettings.from_config({**BOX2, "SMTP2_PASSWORD": ""}, 2)
        with self.assertRaisesRegex(ConfigError, "SMTP2_PORT"):
            SmtpSettings.from_config({**BOX2, "SMTP2_PORT": "x"}, 2)

    def test_extra_mailboxes_are_listed_in_slot_order_and_empty_slots_are_ignored(self):
        config = {**SMTP, **BOX2, "SMTP4_HOST": "h", "SMTP4_PORT": "465", "SMTP4_USER": "four@example.com", "SMTP4_PASSWORD": "p"}
        self.assertEqual([slot.slot for slot in cfg.extra_smtp_slots(config)], [2, 4])
        self.assertEqual([slot.user for slot in cfg.smtp_slots(config)], ["me@example.com", "two@example.com", "four@example.com"])
        self.assertEqual(cfg.extra_smtp_slots(SMTP), [])

    def test_there_is_no_cap_on_the_number_of_mailboxes(self):
        config = dict(SMTP)
        for n in (2, 9, 10, 11, 25, 99, 250):
            config.update({f"SMTP{n}_HOST": "h", f"SMTP{n}_PORT": "465", f"SMTP{n}_USER": f"box{n}@example.com", f"SMTP{n}_PASSWORD": "p"})
        slots = cfg.smtp_slots(config)
        self.assertEqual([slot.slot for slot in slots], [1, 2, 9, 10, 11, 25, 99, 250])
        self.assertEqual([box.slot for box in cfg.configured_mailboxes(config)], [1, 2, 9, 10, 11, 25, 99, 250])

    def test_a_slot_with_only_other_keys_is_not_a_mailbox(self):
        self.assertEqual(cfg.extra_smtp_slots({**SMTP, "IMAP5_HOST": "imap.example.com"}), [])

    def test_a_half_filled_extra_mailbox_is_an_error_not_a_silent_skip(self):
        with self.assertRaisesRegex(ConfigError, "SMTP3_HOST, SMTP3_PORT, SMTP3_PASSWORD"):
            cfg.smtp_slots({**SMTP, "SMTP3_USER": "three@example.com"})

    def test_two_mailboxes_with_the_same_address_are_refused(self):
        with self.assertRaisesRegex(ConfigError, "Skrzynki 1 i 2"):
            cfg.smtp_slots({**SMTP, **BOX2, "SMTP2_USER": "ME@example.com"})

    def test_configured_mailboxes_skips_broken_ones_without_raising(self):
        config = {**SMTP, **BOX2, "SMTP3_USER": "three@example.com", "SMTP4_HOST": "h", "SMTP4_PORT": "465",
                  "SMTP4_USER": "TWO@example.com", "SMTP4_PASSWORD": "p"}
        self.assertEqual([box.user for box in cfg.configured_mailboxes(config)], ["me@example.com", "two@example.com"])

    def test_check_mailboxes_accepts_a_file_without_the_primary_mailbox_yet(self):
        cfg.check_mailboxes({})
        cfg.check_mailboxes(BOX2)
        with self.assertRaises(ConfigError):
            cfg.check_mailboxes({**SMTP, **BOX2, "SMTP2_USER": "me@example.com"})

    def test_require_smtp_and_smtp_configured_agree(self):
        self.assertTrue(cfg.smtp_configured(SMTP))
        self.assertFalse(cfg.smtp_configured({**SMTP, "SMTP_USER": ""}))
        self.assertIs(cfg.require_smtp(SMTP, __file__), SMTP)
        with self.assertRaisesRegex(ConfigError, "SMTP_USER"):
            cfg.require_smtp({**SMTP, "SMTP_USER": ""}, __file__)
        with self.assertRaisesRegex(ConfigError, "Brak config.env"):
            cfg.require_smtp({}, "/nie/ma/takiego/config.env")


if __name__ == "__main__":
    unittest.main()
