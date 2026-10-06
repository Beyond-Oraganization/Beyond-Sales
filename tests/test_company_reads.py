"""Odczyty maili z dołączoną firmą: pola firmy pochodzą z `podmioty`, adresy jednej firmy stoją razem."""

from __future__ import annotations

import unittest

import sqlalchemy as sa

from perno import db as dbmod
from perno.db import format_value
from perno.domain import statuses as st
from perno.domain.rowfilter import ALL_ROWS, NO_COUNTRY, RowFilter
from tests.fixtures import make_db, old_database, put, sample_row


def add_sibling(database, company_id: str, n: int, email: str, status: str = st.STATUS_PENDING) -> str:
    """Dodatkowy adres firmy wstawiony wprost SQL-em (`add_mail` to osobna funkcja; tu liczą się tylko odczyty)."""
    mail_id = f"{company_id}~{n}"
    with database.engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO maile (id, segment, `row_number`, company, website, tag, country, city, address, description, "
            "email, subject, body_html, status, retry_count, company_id, position) "
            "SELECT :id, p.segment, p.`row_number`, p.company, p.website, p.tag, p.country, p.city, p.address, p.description, "
            ":email, 'Temat', '<p>x</p>', :status, 0, p.id, :position FROM podmioty p WHERE p.id = :company"),
            {"id": mail_id, "email": email, "status": status, "company": company_id, "position": n})
    return mail_id


class AdoptedRowsReadTests(unittest.TestCase):
    def test_rows_read_after_adoption_equal_the_old_table_column_by_column(self):
        """Wyrocznia zgodności: płaski wiersz z nowego odczytu = to, co stary kod czytał z tabeli `maile`."""
        database = old_database(3)
        with database.engine.begin() as conn:
            conn.execute(sa.text("UPDATE maile SET tag = 'wesela', mode = 'Panstwo', subject = 'Temat' WHERE id = 'eventy-002'"))
        with database.engine.connect() as conn:  # sqlite oddaje daty z tekstu SQL jako „RRRR-MM-DD GG:MM:SS”
            before = [{c: format_value(row[c]).replace(" ", "T") if c.endswith("_at") else format_value(row[c])
                       for c in dbmod.LEGACY_COLUMNS}
                      for row in conn.execute(sa.text("SELECT * FROM maile ORDER BY id")).mappings()]
        database.upgrade_schema()
        database.adopt()
        after = [{c: row[c] for c in dbmod.LEGACY_COLUMNS} for row in database.fetch_all()]
        self.assertEqual(after, before)

    def test_a_row_without_a_company_is_visible_with_blank_company_fields(self):
        database = make_db()
        put(database, sample_row(1))
        with database.engine.begin() as conn:  # stary kod wstawił wiersz bez firmy
            conn.execute(sa.text("INSERT INTO maile (id, segment, company, status, retry_count) "
                                 "VALUES ('eventy-n001', 'eventy', 'Sierota', 'pominieto_brak_email', 0)"))
        rows = database.fetch_all()
        self.assertEqual([r["id"] for r in rows], ["eventy-001", "eventy-n001"])
        self.assertEqual(rows[1]["company"], "")  # pola firmy czytamy z firmy, a tej jeszcze nie ma


class SingleSourceOfTruthTests(unittest.TestCase):
    def test_company_fields_are_read_from_the_company_not_from_the_mirror(self):
        database = make_db()
        put(database, sample_row(1, country="PL", company="Prawdziwa"))
        with database.engine.begin() as conn:
            conn.execute(sa.text("UPDATE maile SET country = 'DE', company = 'Lustro' WHERE id = 'eventy-001'"))
        self.assertEqual(database.get("eventy-001")["company"], "Prawdziwa")
        self.assertEqual(database.count_rows(RowFilter(countries=("DE",))), 0)
        self.assertEqual(database.count_rows(RowFilter(countries=("PL",))), 1)
        self.assertEqual(database.count_rows(RowFilter(search=None, segments=("eventy",))), 1)

    def test_mail_fields_are_read_from_the_mail(self):
        database = make_db()
        put(database, sample_row(1, email="a@example.com"))
        add_sibling(database, "eventy-001", 2, "b@example.com")
        self.assertEqual(sorted(r["email"] for r in database.fetch_all()), ["a@example.com", "b@example.com"])

    def test_siblings_share_the_company_fields(self):
        database = make_db()
        put(database, sample_row(1, country="PL", city="Kraków", company="Alfa"))
        add_sibling(database, "eventy-001", 2, "b@example.com")
        rows = database.fetch_all()
        self.assertEqual({(r["company"], r["country"], r["city"]) for r in rows}, {("Alfa", "PL", "Kraków")})
        self.assertEqual(database.count_rows(RowFilter(countries=("PL",))), 2)
        self.assertEqual(database.count_rows(RowFilter(countries=(NO_COUNTRY,))), 0)

    def test_editing_a_company_field_changes_it_for_every_address(self):
        database = make_db()
        put(database, sample_row(1, country="PL"))
        sibling = add_sibling(database, "eventy-001", 2, "b@example.com")
        database.apply_edit(sibling, {"city": "Gdańsk"})
        self.assertEqual({r["city"] for r in database.fetch_all()}, {"Gdańsk"})
        self.assertTrue(database.diagnose().clean, database.diagnose())


class AdjacencyTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        put(self.database,
            sample_row(1, id="eventy-n001", row_number="", company="Beta", country="PL", city="Łódź"),
            sample_row(2, id="eventy-n0010", row_number="", company="Alfa", country="PL", city="Łódź"),
            sample_row(3, id="eventy-007", company="Zeta", country="DE", city="Berlin"))
        add_sibling(self.database, "eventy-n001", 2, "druga@example.com")
        add_sibling(self.database, "eventy-n001", 3, "trzecia@example.com")

    def ids(self, sort: str = "default") -> list:
        return self.database.list_ids(ALL_ROWS, sort)

    def test_default_order_keeps_a_company_together_even_when_ids_sort_apart(self):
        # sam porządek id dałby: n001, n0010, n001~2, n001~3 (bo `~` jest za cyframi)
        self.assertEqual(self.ids(), ["eventy-007", "eventy-n001", "eventy-n001~2", "eventy-n001~3", "eventy-n0010"])

    def test_every_company_level_sort_keeps_the_addresses_together(self):
        for sort in ("company", "-company", "city", "-city", "country", "-country", "segment", "-segment"):
            ids = self.ids(sort)
            group = [i for i in ids if i.startswith("eventy-n001") and not i.startswith("eventy-n0010")]
            positions = [ids.index(i) for i in group]
            self.assertEqual(positions, list(range(positions[0], positions[0] + 3)), (sort, ids))
            self.assertEqual(group, ["eventy-n001", "eventy-n001~2", "eventy-n001~3"], sort)

    def test_company_sort_orders_by_company_name(self):
        self.assertEqual(self.ids("company"), ["eventy-n0010", "eventy-n001", "eventy-n001~2", "eventy-n001~3", "eventy-007"])

    def test_pages_are_cut_in_the_same_order(self):
        page = self.database.list_page(ALL_ROWS, "default", 1, 3)
        self.assertEqual([r["id"] for r in page], ["eventy-n001", "eventy-n001~2", "eventy-n001~3"])
        self.assertEqual({r["company"] for r in page}, {"Beta"})

    def test_counts_count_addresses(self):
        self.assertEqual(self.database.count_matching(ALL_ROWS), 5)
        overview = self.database.overview(ALL_ROWS)
        self.assertEqual((overview["total"], overview["countries"]["PL"], overview["countries"]["DE"]), (5, 4, 1))
        self.assertEqual(overview["segments"], {"eventy": 5})

    def test_search_finds_every_address_of_a_company_by_its_name_and_one_by_its_address(self):
        from perno.domain.search import parse_query

        by_company = self.database.count_matching(RowFilter(search=parse_query("beta", "company")))
        by_email = self.database.count_matching(RowFilter(search=parse_query("druga@", "email")))
        self.assertEqual((by_company, by_email), (3, 1))

    def test_distinct_segments_and_origin_follow_the_company(self):
        self.assertEqual(self.database.distinct_segments(), ["eventy"])
        imported = self.database.list_ids(RowFilter(origin="import"), "default")
        self.assertEqual(imported, ["eventy-n001", "eventy-n001~2", "eventy-n001~3", "eventy-n0010"])
        self.assertEqual(self.database.list_ids(RowFilter(origin="dossier"), "default"), ["eventy-007"])


if __name__ == "__main__":
    unittest.main()
