"""Zbiorcza zmiana statusu/danych: walidacja, podgląd, ostrzeżenia, zapis w jednej transakcji i cofanie."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

import sqlalchemy as sa

from perno import db as dbmod
from perno.db import InvalidValue, mail_repository
from perno.services import bulk
from tests.fixtures import FakeClock, make_db, put, sample_row

SENT_AT = "2026-09-29T14:00:00"


def req(ids, status=None, ack=(), **data):
    changes = dict(data)
    if status is not None:
        changes["status"] = status
    return bulk.parse_request({"ids": list(ids), "changes": changes, "acknowledge": list(ack)})


class ParseTests(unittest.TestCase):
    def parse(self, **payload):
        base = {"ids": ["eventy-001"], "changes": {"status": "odpowiedzial"}}
        return bulk.parse_request({**base, **payload})

    def test_minimal_request(self):
        request = self.parse()
        self.assertEqual((request.ids, request.status, dict(request.data), request.acknowledged),
                         (("eventy-001",), "odpowiedzial", {}, frozenset()))

    def test_duplicate_ids_collapse_and_keep_order(self):
        self.assertEqual(self.parse(ids=["b", "a", "b"]).ids, ("b", "a"))

    def test_data_fields_are_normalised_and_empty_means_clear(self):
        request = self.parse(changes={"country": " polska ", "city": "", "address": None})
        self.assertEqual(dict(request.data), {"country": "PL", "city": None, "address": None})
        self.assertIsNone(request.status)

    def test_invalid_requests_are_rejected_with_a_polish_message(self):
        too_many = [f"x-{n}" for n in range(bulk.MAX_BULK_IDS + 1)]
        cases = [
            ({"ids": "eventy-001"}, "ids"), ({"ids": [1]}, "ids"), ({"ids": [""]}, "ids"),
            ({"ids": []}, "przynajmniej jedną"), ({"ids": too_many}, "Za dużo"),
            ({"changes": None}, "changes"), ({"changes": {}}, "changes"), ({"changes": []}, "changes"),
            ({"changes": {"email": "a@b.pl"}}, "email"), ({"changes": {"status": "x", "company": "Y"}}, "company"),
            ({"changes": {"status": "nonsens"}}, "Nieprawidłowy status"),
            ({"changes": {"status": ["wyslano"]}}, "Nieprawidłowy status"),
            ({"changes": {"status": "wysylanie"}}, "Wysyłanie"),
            ({"changes": {"country": "Atlantyda"}}, "kraj"), ({"changes": {"city": 5}}, "city"),
            ({"changes": {"city": "x" * 121}}, "za długie"),
            ({"acknowledge": "resend_risk"}, "acknowledge"), ({"acknowledge": [1]}, "acknowledge"),
        ]
        for patch, expected in cases:
            with self.assertRaisesRegex(InvalidValue, expected, msg=str(patch)[:80]):
                self.parse(**patch)

    def test_the_exact_limit_is_allowed(self):
        self.assertEqual(len(self.parse(ids=[f"x-{n}" for n in range(bulk.MAX_BULK_IDS)]).ids), bulk.MAX_BULK_IDS)


class BulkCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.database = make_db(self.clock)
        put(self.database,
            sample_row(1, status="wyslano", sent_at=SENT_AT),
            sample_row(2),
            sample_row(3, status="pominieto_brak_email", email="", body_html="", body_text=""),
            sample_row(4, status="wysylanie"),
            sample_row(5, status="nie_kontaktowac", sent_at="2026-09-20T09:00:00"),
            sample_row(6, status="blad", error="SMTP"),
            sample_row(7, status="odpowiedzial", sent_at="2026-09-25T10:00:00", country="GB", city="London"),
            sample_row(8, status="nie_kontaktowac", sent_at=""))
        self.log = bulk.UndoLog()
        self.clock.advance(hours=1)  # updated_at po zmianie ma się różnić od wstawienia

    def row(self, n):
        return self.database.get(f"eventy-{n:03d}")

    def ids(self, *numbers):
        return [f"eventy-{n:03d}" for n in numbers]

    def snapshot(self):
        return {row["id"]: row for row in self.database.fetch_all()}

    def stamp(self, n):
        with self.database.engine.connect() as conn:
            return conn.execute(sa.text("SELECT updated_at FROM maile WHERE id = :id"),
                                {"id": f"eventy-{n:03d}"}).scalar()

    def preview(self, *numbers, **kw):
        return bulk.preview(self.database, req(self.ids(*numbers), **kw))

    def apply(self, *numbers, **kw):
        return bulk.apply(self.database, req(self.ids(*numbers), **kw), self.log)


class PreviewTests(BulkCase):
    def test_every_row_lands_in_exactly_one_group(self):
        plan = bulk.preview(self.database, req(self.ids(1, 2, 4, 7, 99), status="odpowiedzial"))
        data = plan.to_json()
        self.assertEqual((data["requested"], data["found"], data["changed"], data["unchanged"], data["skipped_count"]),
                         (5, 4, 2, 1, 2))
        self.assertEqual({row_id: code for row_id, code in plan.skipped}, {"eventy-004": "locked", "eventy-099": "not_found"})
        self.assertEqual(plan.unchanged, ["eventy-007"])
        self.assertEqual(data["by_status"], {"wyslano": 1, "do_wyslania": 1, "wysylanie": 1, "odpowiedzial": 1})
        self.assertEqual(data["needs_ack"], [])

    def test_preview_never_writes(self):
        before = self.snapshot()
        self.preview(1, 2, 5, status="do_wyslania")
        self.preview(1, 2, country="PL", city="")
        self.assertEqual(self.snapshot(), before)

    def test_ready_status_needs_email_and_body(self):
        plan = self.preview(2, 3, 6, status="do_wyslania")
        self.assertEqual(dict(plan.skipped), {"eventy-003": "not_ready"})
        self.assertEqual(plan.unchanged, ["eventy-002"])
        self.assertEqual([c.id for c in plan.changes], ["eventy-006"])

    def test_ready_status_needs_both_email_and_body_not_just_one_of_them(self):
        put(self.database,
            sample_row(9, status="pominieto_brak_email", email="x@example.com", body_html="", body_text=""),
            sample_row(10, status="pominieto_brak_email", email="", body_html="<p>treść</p>"))
        plan = self.preview(9, 10, status="do_wyslania")
        self.assertEqual(dict(plan.skipped), {"eventy-009": "not_ready", "eventy-010": "not_ready"})
        self.assertEqual(plan.changes, [])

    def test_statuses_after_contact_keep_the_date_so_nothing_to_warn_about(self):
        plan = self.preview(1, 2, 8, status="odpowiedzial")
        self.assertEqual(plan.needs_ack, [])
        by_id = {c.id: c for c in plan.changes}
        self.assertEqual(by_id["eventy-001"].after, {"status": "odpowiedzial"})  # data wysłania nie rusza
        self.assertEqual(by_id["eventy-008"].after, {"status": "odpowiedzial"})  # a nieznanej się nie zmyśla

    def test_resending_a_contacted_firm_needs_acknowledgement(self):
        plan = self.preview(1, 5, status="do_wyslania")
        self.assertEqual(plan.needs_ack, ["resend_risk", "reopens_blocked"])
        self.assertEqual(plan.warnings, {"resend_risk": ["eventy-001", "eventy-005"], "reopens_blocked": ["eventy-005"]})
        self.assertEqual({c.id: c.after for c in plan.changes}["eventy-001"], {"status": "do_wyslania", "sent_at": None})

    def test_dropping_the_date_without_making_the_row_sendable_is_a_milder_warning(self):
        plan = self.preview(1, status="pominieto_klient")
        self.assertEqual(plan.needs_ack, ["drops_sent_at"])

    def test_manual_sent_stamp_is_flagged_only_when_it_invents_a_date(self):
        plan = self.preview(1, 2, 7, status="wyslano")
        self.assertEqual(plan.warnings, {"manual_sent": ["eventy-002"]})  # 001 już ma datę, 007 zachowuje swoją
        self.assertEqual({c.id: c.after for c in plan.changes}["eventy-007"], {"status": "wyslano"})

    def test_reopening_a_blocked_firm_that_was_never_contacted(self):
        plan = self.preview(8, status="blad")
        self.assertEqual(plan.warnings, {"reopens_blocked": ["eventy-008"]})

    def test_data_fields_compare_with_what_is_stored(self):
        plan = self.preview(1, 7, country="GB")
        self.assertEqual(plan.unchanged, ["eventy-007"])  # 007 jest już w GB
        self.assertEqual({c.id: c.before for c in plan.changes}, {"eventy-001": {"country": None}})
        cleared = self.preview(1, 7, city="")
        self.assertEqual([(c.id, c.before) for c in cleared.changes], [("eventy-007", {"city": "London"})])
        self.assertEqual(cleared.unchanged, ["eventy-001"])

    def test_json_lists_only_a_few_ids_per_warning_but_counts_all(self):
        rows = [sample_row(100 + n, status="wyslano", sent_at=SENT_AT) for n in range(30)]
        put(self.database, *rows)
        plan = bulk.preview(self.database, req([r["id"] for r in rows], status="do_wyslania"))
        warning = plan.to_json()["warnings"][0]
        self.assertEqual((warning["code"], warning["count"], len(warning["ids"])), ("resend_risk", 30, bulk.LISTED_IDS))
        self.assertTrue(warning["message"].endswith("Dotyczy firm: 30."))


class ApplyTests(BulkCase):
    def test_status_change_follows_the_single_edit_rules(self):
        result = self.apply(1, 2, 6, status="odpowiedzial")
        self.assertEqual((result["changed"], result["unchanged"], result["skipped_count"]), (3, 0, 0))
        self.assertEqual((self.row(1)["status"], self.row(1)["sent_at"]), ("odpowiedzial", SENT_AT))
        self.assertEqual((self.row(2)["status"], self.row(2)["sent_at"]), ("odpowiedzial", ""))
        self.assertEqual(self.row(6)["status"], "odpowiedzial")
        self.assertEqual(self.row(6)["error"], "SMTP")  # tak samo jak przy edycji pojedynczej

    def test_untouched_rows_keep_their_data_and_timestamp(self):
        before = {n: self.stamp(n) for n in (1, 2, 4)}
        self.apply(1, 4, status="odpowiedzial")
        self.assertEqual(self.stamp(2), before[2])  # nie było go w zaznaczeniu
        self.assertEqual(self.stamp(4), before[4])  # „Wysyłanie…" zostaje nietknięte
        self.assertEqual(self.row(4)["status"], "wysylanie")
        self.assertNotEqual(self.stamp(1), before[1])

    def test_resend_needs_acknowledgement_and_without_it_nothing_changes(self):
        before = self.snapshot()
        with self.assertRaises(bulk.AckRequired) as ctx:
            self.apply(1, 2, 6, status="do_wyslania")
        self.assertEqual(ctx.exception.plan.needs_ack, ["resend_risk"])
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.log.get())

    def test_every_warning_must_be_acknowledged(self):
        with self.assertRaises(bulk.AckRequired):
            self.apply(5, status="do_wyslania", ack=["resend_risk"])  # brakuje reopens_blocked
        self.apply(5, status="do_wyslania", ack=["resend_risk", "reopens_blocked"])
        self.assertEqual((self.row(5)["status"], self.row(5)["sent_at"]), ("do_wyslania", ""))

    def test_acknowledged_resend_clears_the_date(self):
        self.apply(1, status="do_wyslania", ack=["resend_risk"])
        self.assertEqual((self.row(1)["status"], self.row(1)["sent_at"]), ("do_wyslania", ""))

    def test_manual_sent_stamps_the_current_time(self):
        self.apply(2, status="wyslano", ack=["manual_sent"])
        self.assertEqual((self.row(2)["status"], self.row(2)["sent_at"]), ("wyslano", "2026-09-30T13:00:00"))

    def test_data_and_status_in_one_operation(self):
        self.apply(1, 2, status="zainteresowany", country="Wielka Brytania", city=" Leeds ", address="")
        for n in (1, 2):
            row = self.row(n)
            self.assertEqual((row["status"], row["country"], row["city"], row["address"]),
                             ("zainteresowany", "GB", "Leeds", ""))

    def test_clearing_a_field(self):
        self.apply(7, city="")
        self.assertEqual((self.row(7)["city"], self.row(7)["country"]), ("", "GB"))

    def test_ready_status_skips_rows_without_email_or_body(self):
        result = self.apply(2, 3, 6, status="do_wyslania")
        self.assertEqual(result["skipped_by_reason"], {"not_ready": 1})
        self.assertEqual(self.row(3)["status"], "pominieto_brak_email")

    def test_failure_in_the_middle_rolls_everything_back(self):
        before = self.snapshot()
        real_update, calls = sa.update, []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("awaria bazy")
            return real_update(*args, **kwargs)

        with mock.patch.object(mail_repository.sa, "update", flaky):
            with self.assertRaises(RuntimeError):
                self.apply(1, 2, 6, 7, status="zainteresowany")
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.log.get())

    def test_ids_are_loaded_in_batches_without_losing_any(self):
        with mock.patch.object(dbmod, "ID_CHUNK", 2):
            result = self.apply(1, 2, 3, 5, 6, 7, 8, status="odpowiedzial")
        self.assertEqual((result["changed"], result["unchanged"]), (6, 1))  # 007 już „Odpowiedział"

    def test_nothing_to_do_records_no_operation(self):
        result = self.apply(7, status="odpowiedzial")
        self.assertEqual(result["changed"], 0)
        self.assertNotIn("operation", result)
        self.assertIsNone(self.log.get())

    def test_a_blocked_status_stops_the_send_claim_immediately(self):
        self.apply(2, status="nie_kontaktowac")
        self.assertIsNone(self.database.claim_for_sending("eventy-002"))


class LockingTests(unittest.TestCase):
    """Blokady wierszy (SELECT … FOR UPDATE) mają sens tylko przy zapisie i zawsze w tej samej kolejności; podgląd
    nie blokuje niczego. Sqlite ignoruje FOR UPDATE, więc pilnujemy samych wywołań (MySQL sprawdza test_mysql_smoke)."""

    def test_preview_reads_without_locks_while_apply_and_undo_lock_the_rows(self):
        database = make_db(FakeClock())
        put(database, sample_row(1), sample_row(2))
        log = bulk.UndoLog()
        request = req(["eventy-001", "eventy-002"], status="odpowiedzial")
        with mock.patch.object(database, "load_for_bulk", wraps=database.load_for_bulk) as load:
            bulk.preview(database, request)
            bulk.apply(database, request, log)
            bulk.undo(database, log)
        self.assertEqual([call.kwargs["lock"] for call in load.call_args_list], [False, True, True])


class UndoTests(BulkCase):
    def test_undo_restores_status_date_and_data(self):
        self.apply(1, 5, 7, status="do_wyslania", ack=["resend_risk", "reopens_blocked"], country="DE", city="Berlin")
        before_undo = self.snapshot()
        self.assertEqual(self.row(1)["sent_at"], "")
        result = bulk.undo(self.database, self.log)
        self.assertEqual((result["restored"], result["total"], result["conflict_count"]), (3, 3, 0))
        self.assertEqual((self.row(1)["status"], self.row(1)["sent_at"], self.row(1)["country"]), ("wyslano", SENT_AT, ""))
        self.assertEqual((self.row(5)["status"], self.row(5)["sent_at"]), ("nie_kontaktowac", "2026-09-20T09:00:00"))
        self.assertEqual((self.row(7)["status"], self.row(7)["country"], self.row(7)["city"]), ("odpowiedzial", "GB", "London"))
        self.assertNotEqual(self.snapshot(), before_undo)

    def test_an_operation_can_be_undone_only_once(self):
        self.apply(2, status="odpowiedzial")
        bulk.undo(self.database, self.log)
        self.assertEqual(self.row(2)["status"], "do_wyslania")
        with self.assertRaises(dbmod.RowNotFound):
            bulk.undo(self.database, self.log)

    def test_later_manual_edits_are_not_overwritten(self):
        self.apply(1, 2, 6, status="zainteresowany", country="DE")
        self.database.apply_edit("eventy-001", {"status": "blad"})
        self.database.apply_edit("eventy-002", {"country": "AT"})
        result = bulk.undo(self.database, self.log)
        self.assertEqual((result["restored"], result["conflict_count"]), (3, 2))
        self.assertEqual(self.row(1)["status"], "blad")  # późniejsza decyzja zostaje
        self.assertEqual(self.row(1)["country"], "")  # ale pole, którego nikt nie ruszył, wraca
        self.assertEqual((self.row(2)["status"], self.row(2)["country"]), ("do_wyslania", "AT"))
        self.assertEqual((self.row(6)["status"], self.row(6)["country"]), ("blad", ""))
        self.assertEqual({c["id"] for c in result["conflicts"]}, {"eventy-001", "eventy-002"})

    def test_status_and_date_come_back_together_or_not_at_all(self):
        self.apply(1, status="do_wyslania", ack=["resend_risk"])
        self.database.apply_edit("eventy-001", {"status": "wyslano"})  # „wysłano" ponownie — z nową datą
        new_date = self.row(1)["sent_at"]
        self.assertNotEqual(new_date, SENT_AT)
        result = bulk.undo(self.database, self.log)
        self.assertEqual(result["restored"], 0)
        self.assertEqual((self.row(1)["status"], self.row(1)["sent_at"]), ("wyslano", new_date))

    def test_a_deleted_row_is_reported_not_fatal(self):
        self.apply(1, 2, status="odpowiedzial")
        with self.database.engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM maile WHERE id = 'eventy-002'"))
        result = bulk.undo(self.database, self.log)
        self.assertEqual((result["restored"], result["conflicts"][0]["id"]), (1, "eventy-002"))
        self.assertEqual(self.row(1)["status"], "wyslano")

    def test_operations_can_be_picked_by_id_and_the_latest_is_the_default(self):
        first = self.apply(2, status="odpowiedzial")["operation"]
        second = self.apply(6, status="zainteresowany")["operation"]
        self.assertEqual([op["id"] for op in self.log.listing()], [second["id"], first["id"]])
        bulk.undo(self.database, self.log, first["id"])
        self.assertEqual((self.row(2)["status"], self.row(6)["status"]), ("do_wyslania", "zainteresowany"))
        bulk.undo(self.database, self.log)  # domyślnie najnowsza = druga
        self.assertEqual(self.row(6)["status"], "blad")
        with self.assertRaises(dbmod.RowNotFound):
            bulk.undo(self.database, self.log, "nie-ma")

    def test_the_log_keeps_only_the_last_operations(self):
        log = bulk.UndoLog(limit=3)
        now = datetime(2026, 9, 30, 12, 0, 0)
        for n in range(5):
            log.record(bulk.Operation(f"op{n}", now, f"zmiana {n}", []))
        self.assertEqual([op["id"] for op in log.listing()], ["op4", "op3", "op2"])
        self.assertIsNone(log.get("op0"))
        self.assertEqual(bulk.UNDO_LIMIT, 20)


class LabelTests(BulkCase):
    def test_labels_describe_what_was_changed_and_how_many_rows(self):
        self.assertEqual(self.apply(2, 6, status="odpowiedzial")["operation"]["label"], "status „Odpowiedział” (2)")
        label = self.apply(1, 2, country="Polska", city="", address="Długa 1")["operation"]["label"]
        self.assertEqual(label, "kraj Polska, miasto wyczyszczone, adres „Długa 1” (2)")
        self.assertEqual(self.apply(7, address="")["changed"], 0)  # pusty adres 007 już jest pusty


if __name__ == "__main__":
    unittest.main()
