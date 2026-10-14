"""Trasy panelu: zbiorcza zmiana (`/api/bulk-*`), lekka lista (`/api/maile?light=1`) i pojedynczy wiersz."""

from __future__ import annotations

import unittest

from perno import db as dbmod
from tests.fixtures import put, sample_row
from tests.test_web import WebCase

SENT_AT = "2026-09-29T14:00:00"


class BulkWebCase(WebCase):
    def setUp(self):
        super().setUp()
        put(self.database,
            sample_row(1, status="wyslano", sent_at=SENT_AT, country="PL"),
            sample_row(2),
            sample_row(3, status="wysylanie"),
            sample_row(4, status="nie_kontaktowac", email="", body_html="", body_text=""),
            sample_row(5, segment="driverzy", status="blad", error="SMTP"))

    def update(self, ids, changes, **extra):
        return self.call("POST", "/api/bulk-update", {"ids": ids, "changes": changes, **extra})

    def status_of(self, row_id):
        return self.database.get(row_id)["status"]


class BulkUpdateTests(BulkWebCase):
    def test_dry_run_reports_and_changes_nothing(self):
        resp = self.update(["eventy-001", "eventy-002", "eventy-003", "nie-ma"], {"status": "odpowiedzial"}, dry_run=True)
        data = self.data(resp)
        self.assertEqual(resp.status, 200)
        self.assertTrue(data["dry_run"])
        self.assertEqual((data["changed"], data["skipped_count"], data["needs_ack"]), (2, 2, []))
        self.assertEqual(data["skipped_by_reason"], {"locked": 1, "not_found": 1})
        self.assertNotIn("operation", data)
        self.assertEqual((self.status_of("eventy-001"), self.status_of("eventy-002")), ("wyslano", "do_wyslania"))

    def test_real_run_changes_rows_and_records_the_operation(self):
        resp = self.update(["eventy-001", "eventy-002"], {"status": "zainteresowany", "city": "Leeds"})
        data = self.data(resp)
        self.assertEqual(resp.status, 200)
        self.assertFalse(data["dry_run"])
        self.assertEqual(data["changed"], 2)
        self.assertEqual(data["operation"]["label"], "status „Zainteresowany”, miasto „Leeds” (2)")
        self.assertEqual((self.status_of("eventy-001"), self.status_of("eventy-002")), ("zainteresowany",) * 2)
        self.assertEqual(self.database.get("eventy-001")["sent_at"], SENT_AT)  # historia wysyłki przeżyła

    def test_unconfirmed_warnings_give_409_with_details_and_change_nothing(self):
        resp = self.update(["eventy-001"], {"status": "do_wyslania"})
        data = self.data(resp)
        self.assertEqual(resp.status, 409)
        self.assertEqual(data["needs_ack"], ["resend_risk"])
        self.assertEqual(data["warnings"][0]["count"], 1)
        self.assertIn("error", data)
        self.assertEqual(self.status_of("eventy-001"), "wyslano")

    def test_dry_run_of_the_same_request_lists_the_warnings_without_failing(self):
        data = self.data(self.update(["eventy-001"], {"status": "do_wyslania"}, dry_run=True))
        self.assertEqual(data["needs_ack"], ["resend_risk"])

    def test_confirmed_warnings_let_the_change_through(self):
        resp = self.update(["eventy-001"], {"status": "do_wyslania"}, acknowledge=["resend_risk"])
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.database.get("eventy-001")["sent_at"], "")

    def test_invalid_requests_get_400(self):
        cases = [
            {"ids": [], "changes": {"status": "wyslano"}},
            {"ids": ["eventy-001"], "changes": {}},
            {"ids": ["eventy-001"], "changes": {"status": "nonsens"}},
            {"ids": ["eventy-001"], "changes": {"status": "wysylanie"}},
            {"ids": ["eventy-001"], "changes": {"email": "x@y.pl"}},
            {"ids": ["eventy-001"], "changes": {"country": "Atlantyda"}},
            {"ids": "eventy-001", "changes": {"status": "wyslano"}},
            {"changes": {"status": "wyslano"}},
        ]
        for payload in cases:
            resp = self.call("POST", "/api/bulk-update", payload)
            self.assertEqual(resp.status, 400, payload)
            self.assertIn("error", self.data(resp))
        self.assertEqual(self.status_of("eventy-001"), "wyslano")

    def test_rules_of_the_transport_still_apply(self):
        self.assertEqual(self.call("POST", "/api/bulk-update", {"ids": ["eventy-001"], "changes": {"city": "X"}},
                                   headers={"content-type": "text/plain"}).status, 415)
        self.assertEqual(self.call("GET", "/api/bulk-update").status, 404)

    def test_sending_is_blocked_right_after_marking_a_firm_do_not_contact(self):
        self.update(["eventy-002"], {"status": "nie_kontaktowac"})
        resp = self.call("POST", "/api/send-one", {"id": "eventy-002"})
        self.assertEqual(resp.status, 400)
        self.assertIn("Nie kontaktować", self.data(resp)["error"])
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(self.status_of("eventy-002"), "nie_kontaktowac")


class UndoWebTests(BulkWebCase):
    def test_undo_restores_and_log_lists_operations_newest_first(self):
        first = self.data(self.update(["eventy-002"], {"status": "odpowiedzial"}))["operation"]
        second = self.data(self.update(["driverzy-005"], {"country": "DE"}))["operation"]
        log = self.data(self.call("GET", "/api/bulk-log"))
        self.assertEqual([op["id"] for op in log["operations"]], [second["id"], first["id"]])
        self.assertEqual((log["limit"], log["operations"][0]["count"]), (20, 1))
        self.assertRegex(log["operations"][0]["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

        resp = self.call("POST", "/api/bulk-undo", {})
        data = self.data(resp)
        self.assertEqual((resp.status, data["restored"], data["conflict_count"]), (200, 1, 0))
        self.assertEqual(self.database.get("driverzy-005")["country"], "")
        self.assertEqual(self.status_of("eventy-002"), "odpowiedzial")  # starsza operacja czeka na swoją kolej
        self.assertEqual(self.call("POST", "/api/bulk-undo", {"operation": first["id"]}).status, 200)
        self.assertEqual(self.status_of("eventy-002"), "do_wyslania")

    def test_undo_without_anything_to_undo_is_404(self):
        resp = self.call("POST", "/api/bulk-undo", {})
        self.assertEqual(resp.status, 404)
        self.assertIn("restarcie", self.data(resp)["error"])
        self.assertEqual(self.call("POST", "/api/bulk-undo", {"operation": "nie-ma"}).status, 404)
        self.assertEqual(self.call("POST", "/api/bulk-undo", {"operation": 5}).status, 400)

    def test_log_is_empty_at_start(self):
        self.assertEqual(self.data(self.call("GET", "/api/bulk-log"))["operations"], [])

    def test_conflicts_are_reported_to_the_client(self):
        self.update(["eventy-002"], {"status": "odpowiedzial"})
        self.database.apply_edit("eventy-002", {"status": "blad"})
        data = self.data(self.call("POST", "/api/bulk-undo", {}))
        self.assertEqual((data["restored"], data["conflicts"][0]["id"]), (0, "eventy-002"))
        self.assertEqual(self.status_of("eventy-002"), "blad")


class LightListTests(BulkWebCase):
    def test_light_list_has_no_bodies_but_says_whether_there_is_one(self):
        rows = self.data(self.call("GET", "/api/maile", query={"light": ["1"]}))["rows"]
        self.assertEqual([r["id"] for r in rows], ["eventy-001", "eventy-002", "eventy-003", "eventy-004", "driverzy-005"])
        for row in rows:
            self.assertNotIn("body_html", row)
            self.assertNotIn("body_text", row)
        self.assertEqual({r["id"]: r["has_body"] for r in rows},
                         {"eventy-001": True, "eventy-002": True, "eventy-003": True, "eventy-004": False, "driverzy-005": True})
        self.assertEqual(rows[0]["country"], "PL")
        self.assertEqual(rows[0]["sent_at"], SENT_AT)

    def test_light_rows_carry_every_other_column(self):
        light = self.data(self.call("GET", "/api/maile", query={"light": ["1"]}))["rows"][0]
        full = self.data(self.call("GET", "/api/maile"))["rows"][0]
        self.assertEqual({k: v for k, v in full.items() if k not in ("body_html", "body_text")},
                         {k: v for k, v in light.items() if k != "has_body"})

    def test_default_list_is_unchanged(self):
        rows = self.data(self.call("GET", "/api/maile"))["rows"]
        self.assertEqual(set(rows[0]), set(dbmod.COLUMNS) | set(dbmod.EXTRA_COLUMNS))
        self.assertEqual(rows[0]["body_html"], "<p>Treść 1</p>")

    def test_single_row_has_the_full_content(self):
        resp = self.call("GET", "/api/maile/row", query={"id": ["eventy-001"]})
        row = self.data(resp)["row"]
        self.assertEqual((row["body_html"], row["body_text"]), ("<p>Treść 1</p>", "Treść 1"))
        self.assertEqual(self.call("GET", "/api/maile/row", query={"id": ["nie-ma"]}).status, 404)
        self.assertEqual(self.call("GET", "/api/maile/row").status, 400)


class PanelPageTests(BulkWebCase):
    def test_new_statuses_reach_the_page_for_the_status_pickers(self):
        html = self.call("GET", "/wysylka").body.decode("utf-8")
        for code, label in (("odpowiedzial", "Odpowiedział"), ("zainteresowany", "Zainteresowany"),
                            ("nie_kontaktowac", "Nie kontaktować")):
            self.assertIn(code, html)
            self.assertIn(label, html)


if __name__ == "__main__":
    unittest.main()
