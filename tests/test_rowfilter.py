"""`RowFilter`: wymiary filtra (kraj, e-mail, treść, daty wysłania) i jawna lista wierszy w zadanej kolejności."""

from __future__ import annotations

import random
import unittest
from datetime import date
from unittest import mock

from perno import db as dbmod
from perno.db import NO_COUNTRY, RowFilter
from tests.fixtures import make_db, put, sample_row


def ids_of(database, flt, columns=("id",)):
    return [row["id"] for row in database.iter_rows(flt, columns)]


class DimensionTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        put(self.database,
            sample_row(1, country="PL", status="wyslano", sent_at="2026-09-29T00:00:00"),
            sample_row(2, country="GB", body_html="", body_text="", status="pominieto_brak_email"),
            sample_row(3, segment="driverzy", country="DE", email="", body_html="", body_text="",
                       status="pominieto_brak_email"),
            sample_row(4, segment="driverzy", country="", status="blad", sent_at=""),
            sample_row(5, segment="driverzy", country="PL", status="wyslano", sent_at="2026-09-29T23:59:59"),
            sample_row(6, segment="driverzy", country="", status="odpowiedzial", sent_at="2026-09-30T00:00:00"))

    def check(self, expected, **kwargs):
        flt = RowFilter(**kwargs)
        self.assertEqual(sorted(ids_of(self.database, flt)), sorted(expected), kwargs)
        self.assertEqual(self.database.count_rows(flt), len(expected), kwargs)

    def test_default_filter_selects_everything(self):
        self.check([f"eventy-00{n}" for n in (1, 2)] + [f"driverzy-00{n}" for n in (3, 4, 5, 6)])

    def test_countries_by_code_and_for_firms_without_country(self):
        self.check(["eventy-001", "driverzy-005"], countries=("PL",))
        self.check(["eventy-001", "eventy-002", "driverzy-005"], countries=("PL", "GB"))
        self.check(["driverzy-004", "driverzy-006"], countries=(NO_COUNTRY,))
        self.check(["driverzy-003", "driverzy-004", "driverzy-006"], countries=("DE", NO_COUNTRY))

    def test_email_and_body_are_tristate(self):
        self.check(["driverzy-003"], has_email=False)
        self.check(["eventy-001", "eventy-002", "driverzy-004", "driverzy-005", "driverzy-006"], has_email=True)
        self.check(["eventy-002", "driverzy-003"], has_body=False)
        self.check(["eventy-001", "driverzy-004", "driverzy-005", "driverzy-006"], has_body=True)
        self.check(["eventy-002"], has_email=True, has_body=False)

    def test_sent_range_counts_whole_calendar_days_at_both_ends(self):
        self.check(["eventy-001", "driverzy-005"], sent_from=date(2026, 9, 29), sent_to=date(2026, 9, 29))
        self.check(["eventy-001", "driverzy-005", "driverzy-006"], sent_from=date(2026, 9, 29))
        self.check(["eventy-001", "driverzy-005", "driverzy-006"], sent_to=date(2026, 9, 30))
        self.check(["driverzy-006"], sent_from=date(2026, 9, 30), sent_to=date(2026, 9, 30))
        self.check([], sent_from=date(2026, 10, 1))

    def test_dimensions_combine_with_and(self):
        self.check(["driverzy-005"], segments=("driverzy",), countries=("PL",), statuses=("wyslano",))
        self.check(["eventy-001"], countries=("PL",), has_body=True, sent_to=date(2026, 9, 29), segments=("eventy",))
        self.check([], segments=("eventy",), has_email=False)


class IdsTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        put(self.database, *[sample_row(n, country="PL" if n % 2 else "GB") for n in range(1, 8)])

    def test_rows_come_out_in_the_order_of_the_ids(self):
        wanted = ("eventy-005", "eventy-001", "eventy-007", "eventy-003")
        self.assertEqual(ids_of(self.database, RowFilter(ids=wanted)), list(wanted))

    def test_duplicates_and_unknown_ids_are_dropped(self):
        flt = RowFilter(ids=("eventy-003", "nie-ma", "eventy-001", "eventy-003"))
        self.assertEqual(ids_of(self.database, flt), ["eventy-003", "eventy-001"])
        self.assertEqual(self.database.count_rows(flt), 2)

    def test_none_means_everything_and_empty_means_nothing(self):
        self.assertEqual(len(ids_of(self.database, RowFilter(ids=None))), 7)
        self.assertEqual(ids_of(self.database, RowFilter(ids=())), [])
        self.assertEqual(self.database.count_rows(RowFilter(ids=())), 0)

    def test_other_conditions_still_apply_to_the_listed_ids(self):
        flt = RowFilter(ids=("eventy-004", "eventy-001", "eventy-002", "eventy-003"), countries=("PL",))
        self.assertEqual(ids_of(self.database, flt), ["eventy-001", "eventy-003"])
        self.assertEqual(self.database.count_rows(flt), 2)

    def test_requested_columns_only_and_id_is_not_leaked(self):
        rows = list(self.database.iter_rows(RowFilter(ids=("eventy-002", "eventy-001")), ("company", "email")))
        self.assertEqual(rows, [{"company": "Firma 2", "email": "firma@example.com"},
                                {"company": "Firma 1", "email": "firma@example.com"}])
        with_id = list(self.database.iter_rows(RowFilter(ids=("eventy-002",)), ("company", "id")))
        self.assertEqual(with_id, [{"company": "Firma 2", "id": "eventy-002"}])

    def test_order_survives_batching(self):
        wanted = [f"eventy-{n:03d}" for n in (7, 2, 6, 1, 5, 3, 4)]
        with mock.patch.object(dbmod, "ID_CHUNK", 3):
            self.assertEqual(ids_of(self.database, RowFilter(ids=tuple(wanted))), wanted)
            self.assertEqual(self.database.count_rows(RowFilter(ids=tuple(wanted + ["nie-ma"]))), 7)


class SelectCandidatesCountryTests(unittest.TestCase):
    def test_country_filter_for_the_cli(self):
        database = make_db()
        put(database, sample_row(1, country="PL"), sample_row(2, country="GB"), sample_row(3),
            sample_row(4, country="PL", status="blad"))

        def ids(**kwargs):
            return [row["id"] for row in database.select_candidates(**kwargs)]

        self.assertEqual(ids(), ["eventy-001", "eventy-002", "eventy-003"])
        self.assertEqual(ids(countries=("PL",)), ["eventy-001"])
        self.assertEqual(ids(countries=("GB", NO_COUNTRY)), ["eventy-002", "eventy-003"])
        self.assertEqual(ids(countries=("PL",), statuses=("do_wyslania", "blad")), ["eventy-001", "eventy-004"])


class LargeIdListTests(unittest.TestCase):
    """Prawdziwy rozmiar partii (500): lista id dłuższa niż jedna partia, w losowej kolejności."""

    def test_more_ids_than_one_batch_keep_the_requested_order(self):
        database = make_db()
        total = dbmod.ID_CHUNK * 2 + 201
        rows = [sample_row(n, segment=f"seg{n % 3}", company=f"Firma {n}") for n in range(1, total + 1)]
        for row in rows:
            row["id"] = f"x-{row['row_number'].zfill(5)}"
        put(database, *rows)
        wanted = [row["id"] for row in rows]
        random.Random(7).shuffle(wanted)
        self.assertEqual(ids_of(database, RowFilter(ids=tuple(wanted))), wanted)
        self.assertEqual(database.count_rows(RowFilter(ids=tuple(wanted))), total)


if __name__ == "__main__":
    unittest.main()
