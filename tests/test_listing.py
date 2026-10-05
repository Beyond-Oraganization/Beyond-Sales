"""Lista firm liczona w bazie (filtry, wyszukiwarka, sortowanie, liczniki) kontra wyniki dawnej implementacji w przeglądarce.

Wektory z `tests/listing_cases.json` wygenerowano z `filters.js` przed przeniesieniem listy na serwer: te same wiersze
i te same stany filtrów muszą dać te same id w tej samej kolejności oraz te same liczniki. Sprawdzamy to na sqlite (zawsze)
i na prawdziwym MySQL (gdy ustawiono PERNO_TEST_DB_URL; patrz tests/test_mysql_smoke.py).
"""

from __future__ import annotations

import json
import os
import unittest

import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql, sqlite

from perno import db as dbmod
from perno.db.listing_sql import Folded, Joined, search_clauses, sort_clauses
from perno.db.schema import maile
from perno.domain.listing import DEFAULT_SORT, LIST_FACETS, parse_state, without_facet
from perno.domain.rowfilter import RowFilter
from perno.domain.search import parse_query
from perno.services import listing
from tests.fixtures import make_db, put, sample_row
from tests.test_mysql_smoke import SKIP_REASON, TEST_DB, resolve_test_url

HERE = os.path.dirname(os.path.abspath(__file__))


def load_vectors() -> dict:
    with open(os.path.join(HERE, "listing_cases.json"), encoding="utf-8") as handle:
        return json.load(handle)


def vector_rows(vectors: dict) -> list:
    rows = []
    for row in vectors["rows"]:
        body = "<p>treść</p>" if row["has_body"] else ""
        rows.append(sample_row(
            row["row_number"], segment=row["segment"], id=row["id"], company=row["company"], contact_person=row["contact_person"],
            email=row["email"], website=row["website"], subject=row["subject"], status=row["status"], country=row["country"],
            city=row["city"], address=row["address"], description=row["description"], generated_at=row["generated_at"],
            sent_at=row["sent_at"], body_html=body, body_text=body))
    return rows


class VectorChecks:
    """Sprawdzenia wspólne dla każdej bazy; podklasa ustawia `self.database` i `vectors`."""

    database: dbmod.Database
    vectors: dict

    def test_the_dataset_was_loaded_whole(self):
        self.assertEqual(self.database.count_rows(), len(self.vectors["rows"]))
        self.assertGreaterEqual(len(self.vectors["cases"]), 60)

    def test_filters_search_and_sort_match_the_browser_implementation(self):
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                query = parse_state(case["state"])
                self.assertEqual(self.database.list_ids(query.flt, query.sort), case["ids"])
                self.assertEqual(self.database.count_matching(query.flt), len(case["ids"]))

    def test_pages_are_slices_of_the_same_ordered_result(self):
        for case in self.vectors["cases"]:
            if len(case["ids"]) < 5:
                continue
            with self.subTest(case["name"]):
                query = parse_state(case["state"])
                page = self.database.list_page(query.flt, query.sort, 2, 3)
                self.assertEqual([row["id"] for row in page], case["ids"][2:5])
                self.assertNotIn("body_html", page[0])
                self.assertIn("has_body", page[0])

    def test_facet_counts_match_the_browser_implementation(self):
        for case in self.vectors["facets"]:
            query = parse_state(case["state"])
            for facet in LIST_FACETS:
                with self.subTest(state=case["state"], facet=facet):
                    got = self.database.facet_counts(without_facet(query.flt, facet), facet)
                    self.assertEqual(got, case["facets"][facet])

    def test_service_pages_and_counters_match_the_vectors(self):
        """Usługa (strona, suma, liczniki; na MySQL zapytania idą równolegle) daje to samo co zapytania po kolei."""
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                request = listing.parse_list_request({"state": case["state"], "page": 2, "size": 3, "counts": True, "totals": True})
                result = listing.list_view(self.database, request)
                pages = max(1, -(-len(case["ids"]) // 3))
                page = min(2, pages)
                self.assertEqual([row["id"] for row in result["rows"]], case["ids"][(page - 1) * 3:page * 3])
                self.assertEqual((result["total"], result["page"], result["pages"]), (len(case["ids"]), page, pages))
                self.assertEqual(result["totals"]["all"], len(self.vectors["rows"]))

    def test_overview_gives_the_total_and_every_counter_from_one_pass(self):
        for case in self.vectors["facets"]:
            with self.subTest(case["state"]):
                query = parse_state(case["state"])
                overview = self.database.overview(query.flt)
                self.assertEqual({facet: overview[facet] for facet in LIST_FACETS}, case["facets"])
                self.assertEqual(overview["total"], self.database.count_matching(query.flt))

    def test_overview_total_equals_the_number_of_matching_rows_for_every_state(self):
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                self.assertEqual(self.database.overview(parse_state(case["state"]).flt)["total"], len(case["ids"]))

    def test_quick_view_counts_match_the_browser_implementation(self):
        presets = self.vectors["presets"]
        counts = self.database.match_counts([parse_state(preset["state"]).flt for preset in presets])
        self.assertEqual(counts, [preset["count"] for preset in presets])

    def test_the_ready_flag_selects_exactly_the_ready_quick_view(self):
        ready = next(preset for preset in self.vectors["presets"] if preset["key"] == "ready")
        self.assertEqual(self.database.count_matching(RowFilter(ready=True)), ready["count"])
        view = parse_state(ready["state"]).flt
        self.assertEqual(self.database.list_ids(view, DEFAULT_SORT), self.database.list_ids(RowFilter(ready=True), DEFAULT_SORT))

    def test_only_selected_rows_keep_the_view_order_not_the_order_of_the_ids(self):
        chosen = ("eventy-010", "driverzy-018", "eventy-001")
        flt = RowFilter(ids=chosen)
        self.assertEqual(self.database.list_ids(flt, DEFAULT_SORT), ["eventy-001", "eventy-010", "driverzy-018"])
        self.assertEqual(self.database.list_ids(flt, "-company"), ["eventy-010", "driverzy-018", "eventy-001"])
        self.assertEqual(self.database.list_ids(RowFilter(ids=()), DEFAULT_SORT), [])
        self.assertEqual(self.database.count_matching(RowFilter(ids=("nie-ma", "eventy-001"))), 1)

    def test_distinct_emails_are_trimmed_unique_and_sorted(self):
        ids = ["eventy-008", "eventy-001", "eventy-002", "eventy-007", "driverzy-021", "eventy-004"]
        self.assertEqual(self.database.distinct_emails(ids), ["a01@x.pl", "a02@x.pl", "a07@x.pl"])
        self.assertEqual(self.database.distinct_emails([]), [])


class SqlBuildingTests(unittest.TestCase):
    """Wyrażenia SQL listy dla każdej bazy: tekst składany po swojemu, polska kolacja tylko tam, gdzie baza ją ma."""

    def compile(self, expression, dialect):
        return str(expression.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))

    def test_folded_text_adapts_to_the_database(self):
        plain = Folded(sa.column("x"))
        self.assertEqual(self.compile(plain, sqlite.dialect()), "perno_fold(x)")
        self.assertEqual(self.compile(plain, mysql.dialect()), "x")  # kolacja *_ai_ci sama składa litery
        self.assertEqual(self.compile(plain, postgresql.dialect()), "lower(x)")
        letters = Folded(sa.column("x"), (("ß", "ss"), ("æ", "ae")))
        self.assertEqual(self.compile(letters, mysql.dialect()), "replace(replace(lower(x), 'ß', 'ss'), 'æ', 'ae')")

    def test_search_adds_the_replace_only_for_phrases_that_need_it(self):
        mysql_sql = lambda text: self.compile(sa.and_(*search_clauses(parse_query(text, "company"))), mysql.dialect())
        self.assertNotIn("replace(", mysql_sql("zuraw"))
        for needle, letter in (("strasse", "ß"), ("aero", "æ"), ("oeuvre", "œ"), ("thor", "þ")):
            self.assertIn(f"'{letter}'", mysql_sql(needle), needle)
        self.assertEqual(mysql_sql("-zuraw").count("NOT"), 1)

    def test_joined_fields_skip_empty_values_on_mysql(self):
        joined = Joined(sa.column("a"), sa.column("b"))
        self.assertEqual(self.compile(joined, mysql.dialect()), "concat_ws(' ', nullif(trim(a), ''), nullif(trim(b), ''))")
        self.assertEqual(self.compile(joined, sqlite.dialect()), "coalesce(a, '') || ' ' || coalesce(b, '')")

    def test_text_sort_uses_the_polish_collation_of_each_database(self):
        def order_sql(dialect_name, dialect):
            return self.compile(sa.select(maile.c.id).order_by(*sort_clauses("-company", dialect_name, [])), dialect)

        self.assertIn("COLLATE utf8mb4_pl_0900_ai_ci", order_sql("mysql", mysql.dialect()))
        self.assertIn("COLLATE perno_pl", order_sql("sqlite", sqlite.dialect()))
        self.assertNotIn("COLLATE", order_sql("postgresql", postgresql.dialect()))
        self.assertEqual(sort_clauses("nie_ma", "mysql", ["x"]), ["x"])  # nieznany klucz: tylko kolejność domyślna

    def test_counting_no_filters_needs_no_query(self):
        self.assertEqual(make_db().match_counts([]), [])


class SqliteVectorTests(VectorChecks, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vectors = load_vectors()
        cls.database = make_db()
        put(cls.database, *vector_rows(cls.vectors))


@unittest.skipUnless(TEST_DB, SKIP_REASON)
class MysqlVectorTests(VectorChecks, unittest.TestCase):
    """Te same wektory na prawdziwym MySQL: kolacja bazy i REPLACE zamiast funkcji z Pythona."""

    @classmethod
    def setUpClass(cls):
        cls.vectors = load_vectors()
        cls.database = dbmod.Database(dbmod.make_engine(resolve_test_url()))
        dbmod.maile.drop(cls.database.engine, checkfirst=True)
        cls.database.ensure_schema()
        put(cls.database, *vector_rows(cls.vectors))

    @classmethod
    def tearDownClass(cls):
        dbmod.maile.drop(cls.database.engine, checkfirst=True)


if __name__ == "__main__":
    unittest.main()
