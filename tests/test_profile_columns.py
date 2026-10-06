from __future__ import annotations

import csv
import os
import tempfile
import unittest

import sqlalchemy as sa
from sqlalchemy.dialects import mysql, sqlite

from perno import db as dbmod
from perno.db import InvalidValue, add_column_statements, from_api, legacy_csv, legacy_key
from tests.fixtures import make_db, old_database, put, sample_row


class CountryValidationTests(unittest.TestCase):
    def test_country_is_stored_as_iso_code_whatever_the_input_form(self):
        for raw in ("PL", "pl", "Polska", "polska", " Poland "):
            self.assertEqual(from_api(sample_row(1, country=raw))["country"], "PL", raw)
        self.assertEqual(from_api(sample_row(1, country="UK"))["country"], "GB")
        self.assertEqual(from_api(sample_row(1, country="Wielka Brytania"))["country"], "GB")

    def test_empty_country_means_unknown(self):
        self.assertIsNone(from_api(sample_row(1, country=""))["country"])
        self.assertIsNone(from_api(sample_row(1))["country"])
        self.assertIsNone(from_api({"id": "x-1", "segment": "eventy", "status": "do_wyslania"})["country"])

    def test_unknown_country_is_rejected_with_a_helpful_message(self):
        for raw in ("Atlantyda", "XX", "Polska (Warszawa)", "123"):
            with self.assertRaises(InvalidValue) as ctx:
                from_api(sample_row(1, country=raw))
            self.assertIn("nieznany kraj", str(ctx.exception))
            self.assertIn("kod ISO", str(ctx.exception))

    def test_length_limits(self):
        self.assertEqual(from_api(sample_row(1, city="x" * 120))["city"], "x" * 120)
        for field, limit in (("city", 120), ("address", 500), ("description", 5000)):
            with self.assertRaises(InvalidValue):
                from_api(sample_row(1, **{field: "x" * (limit + 1)}))

    def test_columns_contract(self):
        self.assertEqual(len(dbmod.LEGACY_COLUMNS), 19)
        self.assertEqual(dbmod.PROFILE_COLUMNS, ("country", "city", "address", "description"))
        self.assertEqual(dbmod.COLUMNS, dbmod.LEGACY_COLUMNS + dbmod.PROFILE_COLUMNS)
        self.assertEqual(dbmod.LINK_COLUMNS, ("company_id", "position"))
        self.assertEqual({c.name for c in dbmod.maile.columns} - {"updated_at"}, set(dbmod.COLUMNS) | set(dbmod.LINK_COLUMNS))
        self.assertEqual(set(dbmod.COMPANY_COLUMNS) | set(dbmod.MAIL_COLUMNS), set(dbmod.COLUMNS))
        self.assertFalse(set(dbmod.COMPANY_COLUMNS) & set(dbmod.MAIL_COLUMNS))
        self.assertEqual({c.name for c in dbmod.companies.columns} - {"id", "updated_at"}, set(dbmod.COMPANY_COLUMNS))


class ProfileRoundtripTests(unittest.TestCase):
    def test_profile_fields_roundtrip_through_the_database(self):
        database = make_db()
        put(database, sample_row(1, country="Niemcy", city="Berlin", address="Unter den Linden 1, 10117 Berlin",
                                 description="Limuzyny z kierowcą"))
        row = database.get("eventy-001")
        self.assertEqual((row["country"], row["city"], row["address"], row["description"]),
                         ("DE", "Berlin", "Unter den Linden 1, 10117 Berlin", "Limuzyny z kierowcą"))
        with database.engine.connect() as conn:
            stored = conn.execute(dbmod.maile.select()).mappings().first()
        self.assertEqual(stored["country"], "DE")

    def test_edit_country_city_address_and_description(self):
        database = make_db()
        put(database, sample_row(1, status="wyslano", sent_at="2026-09-29T14:23:11"))
        row = database.apply_edit("eventy-001", {"country": "Austria", "city": "Wiedeń", "address": "Ring 1",
                                                  "description": "Opis"})
        self.assertEqual((row["country"], row["city"], row["address"], row["description"]), ("AT", "Wiedeń", "Ring 1", "Opis"))
        self.assertEqual((row["status"], row["sent_at"]), ("wyslano", "2026-09-29T14:23:11"))  # wysłany mail nietknięty
        cleared = database.apply_edit("eventy-001", {"country": "", "city": ""})
        self.assertEqual((cleared["country"], cleared["city"], cleared["address"]), ("", "", "Ring 1"))

    def test_edit_rejects_unknown_country_and_leaves_the_row_alone(self):
        database = make_db()
        put(database, sample_row(1, country="PL"))
        with self.assertRaises(InvalidValue):
            database.apply_edit("eventy-001", {"country": "Mordor"})
        self.assertEqual(database.get("eventy-001")["country"], "PL")

    def test_sending_row_stays_locked_for_profile_edits_too(self):
        database = make_db()
        put(database, sample_row(1, status="wysylanie"))
        with self.assertRaises(dbmod.RowLocked):
            database.apply_edit("eventy-001", {"country": "PL"})

    def test_regenerating_a_pending_row_never_wipes_its_profile(self):
        database = make_db()
        put(database, sample_row(1, status="do_wyslania", country="GB", city="Londyn", description="Opis"))
        stats = database.upsert_generated([sample_row(1, subject="Nowy temat")])
        self.assertEqual(stats["updated"], 1)
        row = database.get("eventy-001")
        self.assertEqual((row["subject"], row["country"], row["city"], row["description"]),
                         ("Nowy temat", "GB", "Londyn", "Opis"))

    def test_rows_without_row_number_sort_after_numbered_ones_in_their_segment(self):
        database = make_db()
        put(database, sample_row(2), sample_row(1),
            sample_row(7, segment="driverzy"),
            sample_row(1, segment="eventy", id="eventy-n001", row_number=""),
            sample_row(2, segment="driverzy", id="driverzy-n001", row_number=""))
        self.assertEqual([r["id"] for r in database.fetch_all()],
                         ["eventy-001", "eventy-002", "eventy-n001", "driverzy-007", "driverzy-n001"])


class SchemaUpgradeTests(unittest.TestCase):
    def test_old_table_gets_new_columns_and_keeps_every_value(self):
        database = old_database(3)
        new_columns = set(dbmod.PROFILE_COLUMNS) | set(dbmod.LINK_COLUMNS)
        self.assertEqual({c.name for c in database.missing_columns()}, new_columns)
        before = [dict(r) for r in database.engine.connect().execute(sa.text("SELECT * FROM maile ORDER BY id")).mappings()]
        added = database.upgrade_schema()
        self.assertEqual(set(added) & new_columns, new_columns)
        self.assertIn("podmioty", added)  # nowa tabela firm
        self.assertIn("uq_maile_company_email", added)  # unikalny indeks adresu w firmie powstaje jako ostatni
        self.assertEqual(added[-1], "uq_maile_company_email")
        self.assertEqual(database.missing_columns(), [])
        after = [dict(r) for r in database.engine.connect().execute(sa.text("SELECT * FROM maile ORDER BY id")).mappings()]
        self.assertEqual(len(after), 3)
        for old, new in zip(before, after):
            self.assertEqual({k: new[k] for k in old}, old)  # żadna dotychczasowa wartość się nie zmieniła
            self.assertTrue(all(new[c] is None for c in new_columns))

    def test_upgrade_is_idempotent(self):
        database = old_database()
        self.assertTrue(database.upgrade_schema())
        self.assertEqual(database.upgrade_schema(), [])
        self.assertEqual(make_db().upgrade_schema(), [])  # świeża baza ma już wszystko

    def test_upgraded_database_works_with_the_application_layer(self):
        database = old_database(2)
        database.upgrade_schema()
        database.adopt()
        row = database.apply_edit("eventy-001", {"country": "Polska", "city": "Kraków"})
        self.assertEqual((row["country"], row["city"]), ("PL", "Kraków"))
        self.assertEqual([r["country"] for r in database.fetch_all()], ["PL", ""])
        self.assertEqual(database.get("eventy-002")["company"], "Firma 2")

    def test_old_table_is_unreadable_by_new_code_until_upgraded(self):
        """To jest powód, dla którego serwer i CLI wołają upgrade_schema() przy starcie."""
        database = old_database()
        with self.assertRaises(sa.exc.OperationalError):
            database.fetch_all()
        database.upgrade_schema()
        self.assertEqual(len(database.fetch_all()), 2)

    def test_ensure_schema_also_upgrades(self):
        database = old_database()
        database.ensure_schema()
        self.assertEqual(database.missing_columns(), [])

    def test_missing_table_means_nothing_to_upgrade(self):
        import sqlalchemy as sa_

        database = dbmod.Database(dbmod.make_engine(sa_.engine.make_url("sqlite://")))
        self.assertEqual(database.missing_columns(), [])
        self.assertEqual(database.upgrade_schema(), [])

    def test_a_concurrent_upgrade_by_another_process_is_not_an_error(self):
        database = old_database()
        real = database.missing_columns
        calls = {"n": 0}

        def racing():
            calls["n"] += 1
            if calls["n"] == 1:  # pierwsze sprawdzenie widzi brak, ale inny proces dodaje kolumny przed naszym ALTER
                missing = real()
                with database.engine.begin() as conn:
                    for statement in add_column_statements(database.engine.dialect, missing):
                        conn.execute(sa.text(statement))
                return missing
            return real()

        database.missing_columns = racing
        database.upgrade_schema()  # ALTER padnie na „duplicate column", ale kolumny są — brak wyjątku
        self.assertEqual(real(), [])


class AddColumnStatementTests(unittest.TestCase):
    def columns(self):
        return [dbmod.maile.c[legacy_key(name)] for name in dbmod.PROFILE_COLUMNS]

    def test_mysql_gets_one_atomic_alter_statement(self):
        statements = add_column_statements(mysql.dialect(), self.columns())
        self.assertEqual(statements, [
            "ALTER TABLE maile ADD COLUMN country VARCHAR(2) NULL, ADD COLUMN city VARCHAR(120) NULL, "
            "ADD COLUMN address VARCHAR(500) NULL, ADD COLUMN description TEXT NULL"])

    def test_sqlite_gets_one_statement_per_column(self):
        statements = add_column_statements(sqlite.dialect(), self.columns())
        self.assertEqual(len(statements), 4)
        self.assertTrue(all(s.startswith("ALTER TABLE maile ADD COLUMN ") and s.endswith(" NULL") for s in statements))

    def test_required_columns_are_refused(self):
        with self.assertRaises(dbmod.DbError):
            add_column_statements(mysql.dialect(), [dbmod.maile.c.status])


class LegacyFormatStaysLegacyTests(unittest.TestCase):
    def test_legacy_csv_has_exactly_the_nineteen_original_columns(self):
        rows = [sample_row(1, country="PL", city="Kraków", description="Opis")]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Maile.csv")
            legacy_csv.write_rows(path, rows)
            with open(path, newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle))
        self.assertEqual(tuple(header), dbmod.LEGACY_COLUMNS)
        self.assertNotIn("country", header)


if __name__ == "__main__":
    unittest.main()
