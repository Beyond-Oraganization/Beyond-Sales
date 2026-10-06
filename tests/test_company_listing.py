"""Lista firm z wieloma adresami: sortowanie po liczbie adresów i „grupuj wg firmy”, filtry „wiele adresów” i „wstrzymane”,
eksport kolumn firmy i kolejność zaznaczonych wierszy."""

from __future__ import annotations

import unittest

from perno import db as dbmod
from perno.domain import statuses as st
from perno.domain.listing import DEFAULT_SORT, GROUP_PREFIX, parse_state, split_sort
from perno.domain.rowfilter import ALL_ROWS, RowFilter
from perno.services import export as exporter
from perno.services import listing
from tests.fixtures import make_db, put, sample_row
from tests.test_web import WebCase


class ParseStateTests(unittest.TestCase):
    def test_group_applies_only_to_address_level_sort_keys(self):
        for key in ("email", "status", "generated_at", "sent_at"):
            self.assertEqual(parse_state({"sort": key, "group": "yes"}).sort, GROUP_PREFIX + key)
            self.assertEqual(parse_state({"sort": "-" + key, "group": "yes"}).sort, GROUP_PREFIX + "-" + key)
        for key in ("company", "city", "country", "segment", "mails", "default", ""):
            self.assertEqual(parse_state({"sort": key, "group": "yes"}).sort, key if key else DEFAULT_SORT, key)

    def test_group_is_off_unless_it_is_exactly_yes(self):
        for value in ("", "no", True, 1, None, ["yes"], "YES"):
            self.assertEqual(parse_state({"sort": "status", "group": value}).sort, "status", value)

    def test_unknown_sort_keys_stay_default_even_with_group(self):
        self.assertEqual(parse_state({"sort": "kosmos", "group": "yes"}).sort, DEFAULT_SORT)

    def test_split_sort(self):
        self.assertEqual(split_sort("status"), ("status", False, False))
        self.assertEqual(split_sort("-status"), ("status", True, False))
        self.assertEqual(split_sort("g:-email"), ("email", True, True))
        self.assertEqual(split_sort("default"), ("default", False, False))

    def test_multi_and_held_filters_are_three_state(self):
        flt = parse_state({"multi": "yes", "held": "no"}).flt
        self.assertEqual((flt.multi, flt.held), (True, False))
        flt = parse_state({"multi": "no", "held": "yes"}).flt
        self.assertEqual((flt.multi, flt.held), (False, True))
        flt = parse_state({"multi": "x", "held": 3}).flt
        self.assertEqual((flt.multi, flt.held), (None, None))


class SortTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        put(self.database,
            sample_row(1, email="a1@x.pl", status=st.STATUS_PENDING, company="Alfa"),
            sample_row(2, email="b1@x.pl", status=st.STATUS_SENT, sent_at="2026-10-02T10:00:00", company="Beta"),
            sample_row(3, email="c1@x.pl", status=st.STATUS_PENDING, company="Gamma"))
        self.a2 = self.database.add_mail("eventy-001", "a2@x.pl")["id"]
        self.database.apply_edit(self.a2, {"status": st.STATUS_ERROR})
        self.c2 = self.database.add_mail("eventy-003", "c2@x.pl")["id"]
        self.c3 = self.database.add_mail("eventy-003", "c3@x.pl")["id"]

    def ids(self, sort: str, flt=ALL_ROWS) -> list:
        return self.database.list_ids(flt, sort)

    def test_sorting_by_the_number_of_addresses_keeps_each_company_together(self):
        self.assertEqual(self.ids("mails"), ["eventy-002", "eventy-001", self.a2, "eventy-003", self.c2, self.c3])
        self.assertEqual(self.ids("-mails"), ["eventy-003", self.c2, self.c3, "eventy-001", self.a2, "eventy-002"])

    def test_without_grouping_an_address_level_sort_scatters_the_addresses(self):
        # a1 do_wyslania, b1 wyslano, c1 do_wyslania, a2 blad ... : rosnąco wg rangi statusu
        ordered = self.ids("status")
        self.assertEqual(ordered.index("eventy-001") < ordered.index("eventy-002") < ordered.index(self.a2), True)
        self.assertGreater(ordered.index(self.a2) - ordered.index("eventy-001"), 1)

    def test_grouping_keeps_the_addresses_of_a_company_together_and_orders_companies_by_their_best_address(self):
        grouped = self.ids("g:status")
        # najlepszy (najmniejszy) status: Alfa i Gamma mają „Do wysłania”, Beta „Wysłano”
        self.assertEqual(grouped, ["eventy-001", self.a2, "eventy-003", self.c2, self.c3, "eventy-002"])

    def test_grouped_descending_uses_the_worst_address_of_each_company(self):
        grouped = self.ids("g:-status")
        # największy status: Alfa „Błąd”, Beta „Wysłano”, Gamma „Do wysłania”
        self.assertEqual(grouped, ["eventy-001", self.a2, "eventy-002", "eventy-003", self.c2, self.c3])

    def test_grouped_email_sort_orders_companies_by_their_first_address_and_puts_blanks_last(self):
        put(self.database, sample_row(4, email="", status=st.STATUS_SKIPPED_NO_EMAIL, body_html="", body_text="", company="Delta"))
        grouped = self.ids("g:email")
        self.assertEqual(grouped, ["eventy-001", self.a2, "eventy-002", "eventy-003", self.c2, self.c3, "eventy-004"])
        self.assertEqual(self.ids("g:-email")[-1], "eventy-004")

    def test_grouped_date_sort_puts_companies_without_a_date_last(self):
        grouped = self.ids("g:sent_at")
        self.assertEqual(grouped[0], "eventy-002")
        self.assertEqual(set(grouped[1:]), {"eventy-001", self.a2, "eventy-003", self.c2, self.c3})
        parts = [grouped[1:3], grouped[3:6]]
        self.assertEqual({tuple(sorted(p)) for p in parts}, {tuple(sorted(["eventy-001", self.a2])), tuple(sorted(["eventy-003", self.c2, self.c3]))})

    def test_every_grouped_sort_keeps_every_company_contiguous(self):
        for key in ("status", "-status", "email", "-email", "sent_at", "-sent_at", "generated_at", "-generated_at"):
            ordered = self.ids(GROUP_PREFIX + key)
            companies = [i.split("~")[0] for i in ordered]
            seen: list = []
            for company in companies:
                if not seen or seen[-1] != company:
                    seen.append(company)
            self.assertEqual(len(seen), len(set(seen)), (key, ordered))

    def test_pages_follow_the_grouped_order(self):
        page = self.database.list_page(ALL_ROWS, "g:status", 0, 3)
        self.assertEqual([r["id"] for r in page], ["eventy-001", self.a2, "eventy-003"])


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        put(self.database, sample_row(1), sample_row(2), sample_row(3))
        self.a2 = self.database.add_mail("eventy-001", "a2@x.pl")["id"]
        self.database.add_mail("eventy-003", "c2@x.pl")

    def test_multi_picks_companies_with_more_than_one_address(self):
        self.assertEqual(self.database.list_ids(RowFilter(multi=True), "default"),
                         ["eventy-001", self.a2, "eventy-003", "eventy-003~2"])
        self.assertEqual(self.database.list_ids(RowFilter(multi=False), "default"), ["eventy-002"])
        self.assertEqual(self.database.count_matching(RowFilter(multi=None)), 5)

    def test_held_picks_addresses_held_by_a_sibling(self):
        self.database.apply_edit("eventy-001", {"status": st.STATUS_REPLIED})
        self.assertEqual(self.database.list_ids(RowFilter(held=True), "default"), [self.a2])
        self.assertEqual(self.database.list_ids(RowFilter(held=False), "default"),
                         ["eventy-001", "eventy-002", "eventy-003", "eventy-003~2"])

    def test_the_filter_state_from_the_browser_reaches_the_query(self):
        query = parse_state({"multi": "yes", "statuses": [st.STATUS_PENDING]})
        self.assertEqual(self.database.count_matching(query.flt), 4)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        put(self.database, sample_row(1), sample_row(2))
        self.a2 = self.database.add_mail("eventy-001", "a2@x.pl")["id"]

    def test_company_columns_are_exported_with_polish_headers(self):
        names = ("company_id", "position", "mail_count")
        self.assertEqual(tuple(exporter.headers(names)), ("Identyfikator firmy", "Numer adresu w firmie", "Liczba adresów firmy"))
        rows = list(self.database.iter_rows(ALL_ROWS, ("id", *names)))
        self.assertEqual([(r["id"], r["company_id"], r["position"], r["mail_count"]) for r in rows],
                         [("eventy-001", "eventy-001", 1, 2), (self.a2, "eventy-001", 2, 2), ("eventy-002", "eventy-002", 1, 1)])

    def test_selected_rows_are_ordered_like_the_list_including_grouping(self):
        ids = (self.a2, "eventy-002", "eventy-001")
        self.assertEqual(listing.order_ids(self.database, ids, "company"), ("eventy-001", self.a2, "eventy-002"))
        self.assertEqual(listing.order_ids(self.database, ids, "status", "yes"), ("eventy-001", self.a2, "eventy-002"))
        self.assertEqual(listing.order_ids(self.database, ids, "-mails"), ("eventy-001", self.a2, "eventy-002"))

    def test_hold_is_exportable_for_a_report_of_blocked_addresses(self):
        self.database.apply_edit("eventy-001", {"status": st.STATUS_REPLIED})
        held = {r["id"]: r["hold"] for r in self.database.iter_rows(ALL_ROWS, ("id", "hold"))}
        self.assertEqual(held, {"eventy-001": "", self.a2: st.STATUS_REPLIED, "eventy-002": ""})


class WebTests(WebCase):
    def setUp(self):
        super().setUp()
        put(self.database,
            sample_row(1, status=st.STATUS_PENDING), sample_row(2, status=st.STATUS_SENT, sent_at="2026-10-02T10:00:00"))
        self.a2 = self.database.add_mail("eventy-001", "a2@x.pl")["id"]
        self.database.apply_edit(self.a2, {"status": st.STATUS_ERROR})

    def listed(self, state: dict) -> list:
        return [r["id"] for r in self.data(self.call("POST", "/api/mailing/list", {"state": state}))["rows"]]

    def test_the_list_endpoint_honours_group_and_the_new_filters(self):
        self.assertEqual(self.listed({"sort": "status"}), ["eventy-001", "eventy-002", self.a2])
        self.assertEqual(self.listed({"sort": "status", "group": "yes"}), ["eventy-001", self.a2, "eventy-002"])
        self.assertEqual(self.listed({"multi": "yes"}), ["eventy-001", self.a2])
        self.assertEqual(self.listed({"held": "yes"}), [])
        self.assertEqual(self.listed({"sort": "-mails"}), ["eventy-001", self.a2, "eventy-002"])

    def test_the_rows_carry_the_computed_fields_the_page_needs(self):
        rows = self.data(self.call("POST", "/api/mailing/list", {"state": {}}))["rows"]
        self.assertEqual([(r["id"], r["position"], r["mail_count"]) for r in rows],
                         [("eventy-001", "1", "2"), (self.a2, "2", "2"), ("eventy-002", "1", "1")])


class DbExportsTests(unittest.TestCase):
    def test_the_package_exports_the_computed_column_names(self):
        self.assertEqual(dbmod.EXTRA_COLUMNS, ("company_id", "position", "mail_count", "hold"))


if __name__ == "__main__":
    unittest.main()
