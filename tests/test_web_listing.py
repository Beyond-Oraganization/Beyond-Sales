"""Endpointy listy firm: strona z licznikami, id widoku, podsumowanie zaznaczenia i adresy e-mail."""

from __future__ import annotations

from perno.services import listing
from tests.fixtures import put, sample_row
from tests.test_web import WebCase

# numer, segment, firma, status, kraj, e-mail, treść, wysłano
ROWS = (
    (1, "eventy", "Firma A", "do_wyslania", "PL", "f01@x.pl", True, ""),
    (2, "eventy", "Firma B", "wyslano", "DE", "f02@x.pl", True, "2026-09-29T10:00:00"),
    (3, "eventy", "Firma C", "blad", "", "f03@x.pl", True, ""),
    (4, "eventy", "Firma D", "do_wyslania", "PL", "", True, ""),
    (5, "eventy", "Firma E", "do_wyslania", "DE", "f05@x.pl", False, ""),
    (6, "eventy", "Firma F", "blad", "PL", "f06@x.pl", True, ""),
    (7, "driverzy", "Firma G", "wyslano", "", "f07@x.pl", True, "2026-09-29T11:00:00"),
    (8, "driverzy", "Firma H", "do_wyslania", "PL", "f08@x.pl", True, ""),
    (9, "driverzy", "Firma I", "blad", "DE", "f09@x.pl", True, ""),
    (10, "driverzy", "Firma J", "wyslano", "PL", "f10@x.pl", True, "2026-09-29T12:00:00"),
)
READY = ["eventy-001", "eventy-003", "eventy-006", "driverzy-008", "driverzy-009"]  # status do wysłania/błąd + e-mail + treść


class ListingCase(WebCase):
    def setUp(self):
        super().setUp()
        put(self.database, *[
            sample_row(n, segment=segment, company=company, status=status, country=country, email=email,
                       body_html="<p>x</p>" if body else "", sent_at=sent)
            for n, segment, company, status, country, email, body, sent in ROWS])

    def list(self, **payload):
        resp = self.call("POST", "/api/mailing/list", payload)
        self.assertEqual(resp.status, 200, resp.body)
        return self.data(resp)

    def ids(self, data):
        return [row["id"] for row in data["rows"]]


class ListPageTests(ListingCase):
    def test_default_page_has_light_rows_in_the_panel_order_and_the_total(self):
        data = self.list()
        self.assertEqual((data["total"], data["page"], data["pages"], data["size"]), (10, 1, 1, 100))
        self.assertEqual(self.ids(data)[:2], ["eventy-001", "eventy-002"])
        self.assertEqual(self.ids(data)[-1], "driverzy-010")
        row = data["rows"][0]
        self.assertNotIn("body_html", row)
        self.assertIs(row["has_body"], True)
        self.assertNotIn("overview", data)  # liczniki tylko na życzenie

    def test_paging_sorting_and_the_state_object_from_the_browser(self):
        data = self.list(size=3, page=2, state={"sort": "-company"})
        self.assertEqual((data["pages"], data["page"], data["total"]), (4, 2, 10))
        self.assertEqual([row["company"] for row in data["rows"]], ["Firma G", "Firma F", "Firma E"])
        data = self.list(size=3, state={"q": "firma"}, page=99)
        self.assertEqual((data["page"], data["pages"]), (4, 4))  # za daleko: ostatnia strona, nie pusta
        self.assertEqual(len(data["rows"]), 1)

    def test_filters_and_search_use_the_same_state_as_the_url(self):
        data = self.list(state={"statuses": ["blad"], "countries": ["_none"]})
        self.assertEqual(self.ids(data), ["eventy-003"])
        self.assertEqual(self.ids(self.list(state={"statuses": ["blad"]})), ["eventy-003", "eventy-006", "driverzy-009"])
        self.assertEqual(self.ids(self.list(state={"q": '"firma c"'})), ["eventy-003"])
        self.assertEqual(self.list(state={"q": "niemcy"})["total"], 3)
        self.assertEqual(self.list(state={"email": "no"})["total"], 1)

    def test_overview_totals_and_quick_views_come_with_the_page_on_request(self):
        data = self.list(state={"statuses": ["blad"]}, counts=True, totals=True,
                         presets=[{}, {"email": "no"}, {"statuses": ["do_wyslania", "blad"], "email": "yes", "body": "yes"}])
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["overview"]["statuses"], {"do_wyslania": 4, "wyslano": 3, "blad": 3})  # bez własnego filtra
        self.assertEqual(data["overview"]["countries"], {"_none": 1, "PL": 1, "DE": 1, "_foreign": 1})  # przy filtrze błędów
        self.assertEqual(data["totals"]["all"], 10)
        self.assertEqual(data["totals"]["segments"], {"eventy": 6, "driverzy": 4})
        self.assertEqual(data["presets"], [10, 1, len(READY)])

    def test_only_selected_view_keeps_the_sort_not_the_order_of_the_ids(self):
        data = self.list(ids=["driverzy-010", "eventy-002", "eventy-001"], state={"sort": "-company"}, counts=True)
        self.assertEqual(self.ids(data), ["driverzy-010", "eventy-002", "eventy-001"])
        self.assertEqual(data["total"], 3)
        data = self.list(ids=["driverzy-010", "eventy-002", "eventy-001"], state={"statuses": ["wyslano"]})
        self.assertEqual(data["total"], 2)
        self.assertEqual(self.list(ids=[])["rows"], [])

    def test_garbage_in_the_state_means_no_restriction(self):
        data = self.list(state={"statuses": "blad", "sort": "; DROP TABLE", "origin": 7, "sentFrom": "2026-13-45", "q": 5})
        self.assertEqual(data["total"], 10)

    def test_invalid_requests_are_rejected_with_a_message(self):
        for payload in ({"page": 0}, {"page": "1"}, {"size": 0}, {"size": listing.MAX_PAGE_SIZE + 1}, {"size": True},
                        {"ids": "eventy-001"}, {"ids": [1]}, {"ids": [""]}, {"presets": "x"},
                        {"presets": [{}] * (listing.MAX_PRESETS + 1)}):
            with self.subTest(payload):
                resp = self.call("POST", "/api/mailing/list", payload)
                self.assertEqual(resp.status, 400)
                self.assertIn("error", self.data(resp))

    def test_get_is_not_allowed_and_the_legacy_full_list_still_works(self):
        self.assertNotEqual(self.call("GET", "/api/mailing/list").status, 200)
        legacy = self.data(self.call("GET", "/api/maile", query={"light": ["1"]}))
        self.assertEqual(len(legacy["rows"]), 10)


class ViewIdsTests(ListingCase):
    def test_ids_follow_the_view_order_and_the_limit(self):
        data = self.data(self.call("POST", "/api/mailing/ids", {"state": {"sort": "company"}}))
        self.assertEqual(len(data["ids"]), 10)
        self.assertFalse(data["truncated"])
        first = self.data(self.call("POST", "/api/mailing/ids", {"state": {"sort": "-company"}, "limit": 2}))
        self.assertEqual((first["ids"], first["truncated"]), (["driverzy-010", "driverzy-009"], True))

    def test_ready_narrows_the_view_to_firms_that_can_be_sent(self):
        self.assertEqual(self.data(self.call("POST", "/api/mailing/ids", {"ready": True}))["ids"], READY)
        narrowed = self.data(self.call("POST", "/api/mailing/ids", {"ready": True, "state": {"countries": ["PL"]}}))["ids"]
        self.assertEqual(narrowed, ["eventy-001", "eventy-006", "driverzy-008"])

    def test_excluded_statuses_are_left_out_of_the_ids(self):
        ids = self.data(self.call("POST", "/api/mailing/ids", {"exclude_statuses": ["wyslano", "blad"]}))["ids"]
        self.assertEqual(ids, ["eventy-001", "eventy-004", "eventy-005", "driverzy-008"])
        for bad in ("wyslano", [1], [""], ["x"] * 21):
            self.assertEqual(self.call("POST", "/api/mailing/ids", {"exclude_statuses": bad}).status, 400, bad)

    def test_limit_is_validated(self):
        for limit in (0, -1, "3", listing.MAX_LIST_IDS + 1):
            self.assertEqual(self.call("POST", "/api/mailing/ids", {"limit": limit}).status, 400, limit)


class SelectionTests(ListingCase):
    def test_summary_counts_ready_foreign_and_what_is_outside_the_view(self):
        ids = ["eventy-001", "eventy-002", "eventy-004", "eventy-005", "driverzy-008", "nie-ma"]
        data = self.data(self.call("POST", "/api/mailing/selection", {"ids": ids, "state": {"countries": ["PL"]}}))
        self.assertEqual(data["count"], 5)  # „nie-ma” nie istnieje
        self.assertEqual(data["ready"], 2)  # 001 i 008; 002 jest już wysłane, 004 nie ma e-maila, 005 nie ma treści
        self.assertEqual(data["foreign"], {"DE": 2})
        self.assertEqual(data["in_view"], 3)  # 001, 004 i 008 mieszczą się w filtrze „PL”

    def test_ready_ids_are_returned_in_the_view_order(self):
        ids = ["driverzy-010", "eventy-001", "eventy-003"]
        data = self.data(self.call("POST", "/api/mailing/selection",
                                   {"ids": ids, "ready_ids": True, "state": {"sort": "-company"}}))
        self.assertEqual(data["ready_ids"], ["eventy-003", "eventy-001"])  # 010 jest już wysłane
        self.assertEqual(data["ready"], 2)
        self.assertNotIn("in_view", self.data(self.call("POST", "/api/mailing/selection", {"ids": ids})))

    def test_empty_selection_is_all_zeros(self):
        data = self.data(self.call("POST", "/api/mailing/selection", {"ids": []}))
        self.assertEqual((data["count"], data["ready"], data["foreign"]), (0, 0, {}))

    def test_selected_emails_are_unique_trimmed_and_sorted(self):
        put(self.database, sample_row(11, email="  f01@x.pl "), sample_row(12, email=""))
        ids = ["eventy-004", "eventy-001", "eventy-011", "eventy-012", "eventy-002"]
        data = self.data(self.call("POST", "/api/mailing/emails", {"ids": ids}))
        self.assertEqual(data["emails"], ["f01@x.pl", "f02@x.pl"])
        self.assertEqual((data["rows"], data["without_email"]), (5, 2))

    def test_selection_endpoints_validate_ids(self):
        for path in ("/api/mailing/selection", "/api/mailing/emails"):
            self.assertEqual(self.call("POST", path, {"ids": "x"}).status, 400)
            self.assertEqual(self.call("POST", path, {"ids": [None]}).status, 400)
            too_many = [f"x{n}" for n in range(listing.MAX_LIST_IDS + 1)]
            self.assertEqual(self.call("POST", path, {"ids": too_many}).status, 400)


if __name__ == "__main__":
    import unittest

    unittest.main()
