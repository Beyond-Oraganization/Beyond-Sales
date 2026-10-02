from __future__ import annotations

import unittest

import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from perno import db as dbmod
from perno.scraper import schema, statuses
from perno.scraper.settings import HARD_DELAY_FLOOR, LIMITS, STORE_HTML_MODES, ScanSettings, SettingsError
from perno.scraper.store import Store
from tests.fixtures import make_db


class SettingsTests(unittest.TestCase):
    def test_defaults_match_the_agreed_balanced_profile(self):
        s = ScanSettings()
        self.assertEqual((s.concurrency, s.delay, s.depth, s.max_pages, s.max_mb, s.max_minutes),
                         (30, 2.5, 10, 500, 50, 30))
        self.assertEqual((s.ip_cap, s.max_rps, s.store_html, s.floor), (2, 15.0, "klucz", 1.0))

    def test_delay_floor_cannot_be_bypassed(self):
        self.assertEqual(ScanSettings.from_params({"delay": 0.1}).delay, HARD_DELAY_FLOOR)
        self.assertEqual(ScanSettings.from_params({"opoznienie": 0}).delay, HARD_DELAY_FLOOR)
        self.assertEqual(ScanSettings.from_params({"delay": -5}).delay, HARD_DELAY_FLOOR)
        self.assertEqual(ScanSettings.from_params({"floor": 0, "delay": 0.2}).floor, HARD_DELAY_FLOOR)
        self.assertEqual(ScanSettings.from_params(config={"SKRAPER_DELAY": "0.01"}).delay, HARD_DELAY_FLOOR)

    def test_values_are_clamped_to_limits(self):
        s = ScanSettings.from_params({"concurrency": 10_000, "depth": 0, "max_pages": 10**9,
                                      "max_mb": 0, "max_minutes": 9999, "ip_cap": 99, "max_rps": 500, "delay": 10**6})
        self.assertEqual((s.concurrency, s.depth, s.max_pages, s.max_mb, s.max_minutes, s.ip_cap, s.max_rps, s.delay),
                         (100, 1, 5000, 1, 240, 10, 50.0, 60.0))
        for name, (low, high) in LIMITS.items():
            self.assertLessEqual(low, high, name)

    def test_polish_aliases_and_precedence(self):
        config = {"SKRAPER_CONCURRENCY": "12", "SKRAPER_MAX_DEPTH": "4", "SKRAPER_DELAY": "5"}
        s = ScanSettings.from_params({"wspolbieznosc": 7, "glebokosc": "3", "max_stron": 50, "max_minut": 5}, config)
        self.assertEqual((s.concurrency, s.depth, s.max_pages, s.max_minutes, s.delay), (7, 3, 50, 5, 5.0))
        from_config_only = ScanSettings.from_params(None, config)
        self.assertEqual((from_config_only.concurrency, from_config_only.depth), (12, 4))

    def test_blank_and_none_params_fall_back(self):
        s = ScanSettings.from_params({"depth": "", "max_pages": None}, {"SKRAPER_MAX_DEPTH": "4"})
        self.assertEqual((s.depth, s.max_pages), (4, 500))

    def test_invalid_values_raise(self):
        for params in ({"depth": "abc"}, {"delay": float("nan")}, {"store_html": "wszystko"},
                       {"contact": "zły adres ze spacją"}, {"contact": "a"}, {"contact": "x" * 201}):
            with self.assertRaises(SettingsError, msg=str(params)):
                ScanSettings.from_params(params)
        self.assertEqual(STORE_HTML_MODES, ("all", "klucz", "nie"))

    def test_contact_sources_and_user_agent(self):
        explicit = ScanSettings.from_params({"contact": "bot@firma.pl"}, {"SMTP_USER": "roman@z3x.pl"})
        self.assertEqual((explicit.contact, explicit.contact_source), ("bot@firma.pl", "param"))
        from_config = ScanSettings.from_params(None, {"SKRAPER_CONTACT": "a@b.pl", "SMTP_USER": "roman@z3x.pl"})
        self.assertEqual((from_config.contact, from_config.contact_source), ("a@b.pl", "config"))
        fallback = ScanSettings.from_params(None, {"SMTP_USER": "roman@z3x.pl"})
        self.assertEqual((fallback.contact, fallback.contact_source), ("roman@z3x.pl", "smtp_user"))
        self.assertEqual(fallback.user_agent(), "PernoContextBot/1.0 (+roman@z3x.pl)")
        none = ScanSettings.from_params()
        self.assertEqual((none.contact, none.contact_source), ("", ""))
        self.assertEqual(none.user_agent(), "PernoContextBot/1.0")

    def test_for_tests_is_the_only_way_below_the_floor(self):
        s = ScanSettings.for_tests(delay=0.01, concurrency=3)
        self.assertEqual((s.delay, s.floor, s.concurrency), (0.01, 0.0, 3))


class StatusTests(unittest.TestCase):
    def test_vocabularies_are_consistent(self):
        self.assertEqual(set(statuses.FIRMA_META), statuses.FIRMA_STATUSY)
        for group in (statuses.FIRMA_DOMYSLNIE_DO_SKANU, statuses.FIRMA_PONAWIALNE):
            self.assertTrue(set(group) <= statuses.FIRMA_STATUSY)
        self.assertNotIn(statuses.FIRMA_ZABLOKOWANA, statuses.FIRMA_DOMYSLNIE_DO_SKANU)
        self.assertNotIn(statuses.FIRMA_LIMIT, statuses.FIRMA_DOMYSLNIE_DO_SKANU)
        self.assertTrue(set(statuses.RUN_AKTYWNE) <= statuses.RUN_STATUSY)


class SchemaTests(unittest.TestCase):
    def test_separate_metadata_does_not_touch_maile(self):
        self.assertNotIn("maile", schema.skraper_metadata.tables)
        self.assertEqual(set(schema.skraper_metadata.tables), set(schema.TABLE_NAMES))
        self.assertEqual(len(schema.TABLE_NAMES), 6)

    def test_mysql_ddl_uses_expected_types(self):
        dialect = mysql.dialect()
        ddl = {t.name: str(CreateTable(t).compile(dialect=dialect)) for t in schema.skraper_metadata.sorted_tables}
        self.assertIn("MEDIUMBLOB", ddl["strony_tresc"])
        self.assertIn("MEDIUMTEXT", ddl["strony_tresc"])
        self.assertIn("CHARACTER SET ascii COLLATE ascii_bin", ddl["strony"])
        self.assertIn("url_hash CHAR(40)", ddl["strony"])
        self.assertIn("CONSTRAINT uq_strony_firma_url UNIQUE (firma_id, url_hash)", ddl["strony"])
        self.assertIn("BIGINT NOT NULL AUTO_INCREMENT", ddl["strony"])
        self.assertIn("CONSTRAINT uq_firmy_domena UNIQUE (domena)", ddl["firmy"])
        self.assertIn("CONSTRAINT uq_przebiegi_aktywny UNIQUE (aktywny)", ddl["skan_przebiegi"])
        self.assertIn("CONSTRAINT uq_kontakty UNIQUE (firma_id, typ, wartosc)", ddl["kontakty"])
        for text in ddl.values():
            self.assertIn("utf8mb4", text)
            self.assertNotIn("FOREIGN KEY", text)
            self.assertNotIn("REFERENCES", text)
        for table in schema.skraper_metadata.sorted_tables:
            for index in table.indexes:
                str(CreateIndex(index).compile(dialect=dialect))

    def test_url_columns_are_ascii_and_fit_index_limits(self):
        # najdłuższy indeks/unikat musi mieścić się w 3072 bajtach InnoDB (utf8mb4 = 4 B/znak, ascii = 1 B)
        kontakty_key = 4 * 16 + 4 * 255 + 4
        self.assertLess(kontakty_key, 3072)
        self.assertEqual(schema.strony.c.url_hash.type.length, 40)

    def test_create_all_on_sqlite_is_idempotent_and_coexists_with_maile(self):
        database = make_db()
        store = Store(database)
        store.ensure_schema()
        store.ensure_schema()
        names = set(sa.inspect(database.engine).get_table_names())
        self.assertTrue(set(schema.TABLE_NAMES) <= names)
        self.assertIn(dbmod.maile.name, names)

    def test_constraints_enforced(self):
        database = make_db()
        Store(database).ensure_schema()
        with database.engine.begin() as conn:
            conn.execute(sa.insert(schema.firmy).values(domena="a.pl", website="https://a.pl/"))
            conn.execute(sa.insert(schema.strony).values(firma_id=1, url="https://a.pl/", url_hash="h" * 40))
            conn.execute(sa.insert(schema.skan_przebiegi).values(aktywny=None))
            conn.execute(sa.insert(schema.skan_przebiegi).values(aktywny=None))  # NULL nie koliduje
            conn.execute(sa.insert(schema.skan_przebiegi).values(aktywny=1))
        for table, values in (
            (schema.firmy, dict(domena="a.pl", website="https://a.pl/")),
            (schema.strony, dict(firma_id=1, url="https://a.pl/x", url_hash="h" * 40)),
            (schema.skan_przebiegi, dict(aktywny=1)),
        ):
            with self.assertRaises(sa.exc.IntegrityError):
                with database.engine.begin() as conn:
                    conn.execute(sa.insert(table).values(**values))
        with database.engine.connect() as conn:
            row = conn.execute(sa.select(schema.firmy)).mappings().first()
        self.assertEqual((row["skan_status"], row["zrodlo"], row["stron_pobranych"]), ("nieskanowana", "maile", 0))


if __name__ == "__main__":
    unittest.main()
