from __future__ import annotations

import glob
import hashlib
import os
import tempfile
import threading
import unittest
from datetime import timedelta

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from lib import config, legacy_csv
from lib import db as dbmod
from lib import statuses as st
from tests.fixtures import FakeClock, make_db, put, sample_row


class ApiShapeTests(unittest.TestCase):
    def test_roundtrip_keeps_legacy_shape(self):
        database = make_db()
        row = sample_row(3, status="wyslano", sent_at="2026-09-29T14:23:11", retry_count="1")
        put(database, row)
        self.assertEqual(database.get("eventy-003"), row)

    def test_null_becomes_empty_string_and_empty_becomes_null(self):
        database = make_db()
        put(database, sample_row(1, contact_person="", error=""))
        with database.engine.connect() as conn:
            stored = conn.execute(dbmod.maile.select()).mappings().first()
        self.assertIsNone(stored["contact_person"])
        self.assertEqual(database.get("eventy-001")["contact_person"], "")

    def test_from_api_rejects_bad_values(self):
        with self.assertRaises(dbmod.InvalidValue):
            dbmod.from_api(sample_row(1, sent_at="wczoraj"))
        with self.assertRaises(dbmod.InvalidValue):
            dbmod.from_api(sample_row(1, row_number="x"))
        with self.assertRaises(dbmod.InvalidValue):
            dbmod.from_api(sample_row(1, subject="x" * 501))
        with self.assertRaises(dbmod.InvalidValue):
            dbmod.from_api(sample_row(1, status=""))

    def test_api_rows_have_exactly_the_legacy_keys(self):
        database = make_db()
        put(database, sample_row(1))
        self.assertEqual(tuple(database.get("eventy-001")), dbmod.COLUMNS)


class UpsertTests(unittest.TestCase):
    def test_insert_then_update_keeps_sent_fields(self):
        database = make_db()
        put(database, sample_row(1, status="blad", error="stary", retry_count="2"))
        stats = database.upsert_generated([sample_row(1, subject="Nowy temat")])
        self.assertEqual(stats, {"inserted": 0, "updated": 1, "skipped": 0})
        row = database.get("eventy-001")
        self.assertEqual(row["subject"], "Nowy temat")
        self.assertEqual(row["error"], "")
        self.assertEqual(row["retry_count"], "2")

    def test_new_rows_are_inserted(self):
        database = make_db()
        stats = database.upsert_generated([sample_row(1), sample_row(2)])
        self.assertEqual(stats["inserted"], 2)
        self.assertEqual(database.count_rows(), 2)

    def test_never_touches_sent_or_sending_rows(self):
        database = make_db()
        put(database,
            sample_row(1, status="wyslano", sent_at="2026-09-29T14:00:00"),
            sample_row(2, status="wysylanie"))
        stats = database.upsert_generated([sample_row(1, subject="X"), sample_row(2, subject="Y")])
        self.assertEqual(stats["skipped"], 2)
        self.assertEqual(database.get("eventy-001")["subject"], "Temat 1")
        self.assertEqual(database.get("eventy-002")["subject"], "Temat 2")

    def test_never_overwrites_a_human_decision(self):
        database = make_db()
        decided = (st.STATUS_REPLIED, st.STATUS_INTERESTED, st.STATUS_DO_NOT_CONTACT)
        put(database, *[sample_row(n, status=status, sent_at="2026-09-29T14:00:00")
                        for n, status in enumerate(decided, start=1)])
        stats = database.upsert_generated([sample_row(n, subject="Nadpisane") for n in (1, 2, 3)])
        self.assertEqual(stats, {"inserted": 0, "updated": 0, "skipped": 3})
        for n, status in enumerate(decided, start=1):
            row = database.get(f"eventy-{n:03d}")
            self.assertEqual((row["status"], row["subject"], row["sent_at"]), (status, f"Temat {n}", "2026-09-29T14:00:00"))


class EditTests(unittest.TestCase):
    def test_adding_email_unblocks_row_with_body(self):
        database = make_db()
        put(database, sample_row(1, status="pominieto_brak_email", email=""))
        row = database.apply_edit("eventy-001", {"email": "  nowy@example.com "})
        self.assertEqual(row["email"], "nowy@example.com")
        self.assertEqual(row["status"], st.STATUS_PENDING)

    def test_clearing_email_blocks_pending_row(self):
        database = make_db()
        put(database, sample_row(1))
        self.assertEqual(database.apply_edit("eventy-001", {"email": ""})["status"], st.STATUS_SKIPPED_NO_EMAIL)

    def test_unblock_needs_body(self):
        database = make_db()
        put(database, sample_row(1, status="pominieto_brak_email", email="", body_html="", body_text=""))
        self.assertEqual(database.apply_edit("eventy-001", {"email": "a@b.pl"})["status"], "pominieto_brak_email")

    def test_manual_sent_stamps_time_and_revert_clears_it(self):
        clock = FakeClock()
        database = make_db(clock)
        put(database, sample_row(1))
        sent = database.apply_edit("eventy-001", {"status": "wyslano"})
        self.assertEqual(sent["sent_at"], "2026-09-30T12:00:00")
        again = database.apply_edit("eventy-001", {"status": "wyslano"})
        self.assertEqual(again["sent_at"], "2026-09-30T12:00:00")
        back = database.apply_edit("eventy-001", {"status": "do_wyslania"})
        self.assertEqual(back["sent_at"], "")

    def test_statuses_after_contact_keep_the_sent_date(self):
        database = make_db()
        put(database, sample_row(1, status="wyslano", sent_at="2026-09-29T14:23:11"))
        for status in ("odpowiedzial", "zainteresowany", "nie_kontaktowac", "wyslano"):
            row = database.apply_edit("eventy-001", {"status": status})
            self.assertEqual((row["status"], row["sent_at"]), (status, "2026-09-29T14:23:11"), status)
        back = database.apply_edit("eventy-001", {"status": "do_wyslania"})
        self.assertEqual(back["sent_at"], "")  # powrót do „do wysłania" jest świadomą zgodą na ponowny mail

    def test_new_status_on_a_never_sent_row_does_not_invent_a_date(self):
        database = make_db()
        put(database, sample_row(1))
        for status in ("odpowiedzial", "nie_kontaktowac"):
            self.assertEqual(database.apply_edit("eventy-001", {"status": status})["sent_at"], "")

    def test_manual_sent_after_a_reply_keeps_the_original_date(self):
        database = make_db(FakeClock())
        put(database, sample_row(1, status="odpowiedzial", sent_at="2026-09-20T09:00:00"))
        self.assertEqual(database.apply_edit("eventy-001", {"status": "wyslano"})["sent_at"], "2026-09-20T09:00:00")

    def test_editing_the_email_never_reopens_a_decided_row(self):
        database = make_db()
        put(database, sample_row(1, status="nie_kontaktowac", email="", sent_at=""))
        row = database.apply_edit("eventy-001", {"email": "a@b.pl"})
        self.assertEqual((row["email"], row["status"]), ("a@b.pl", "nie_kontaktowac"))

    def test_explicit_status_beats_auto_unblock(self):
        database = make_db()
        put(database, sample_row(1, status="pominieto_brak_email", email=""))
        row = database.apply_edit("eventy-001", {"email": "a@b.pl", "status": "pominieto_brak_email"})
        self.assertEqual(row["status"], "pominieto_brak_email")

    def test_rejects_invalid_and_sending_status(self):
        database = make_db()
        put(database, sample_row(1))
        with self.assertRaises(dbmod.InvalidValue):
            database.apply_edit("eventy-001", {"status": "nonsens"})
        with self.assertRaises(dbmod.InvalidValue):
            database.apply_edit("eventy-001", {"status": "wysylanie"})

    def test_unknown_row_and_overlong_value(self):
        database = make_db()
        put(database, sample_row(1))
        with self.assertRaises(dbmod.RowNotFound):
            database.apply_edit("eventy-999", {"email": "a@b.pl"})
        with self.assertRaises(dbmod.InvalidValue):
            database.apply_edit("eventy-001", {"subject": "x" * 501})

    def test_sending_row_is_locked_except_for_status_recovery(self):
        database = make_db()
        put(database, sample_row(1, status="wysylanie"))
        with self.assertRaises(dbmod.RowLocked) as ctx:
            database.apply_edit("eventy-001", {"subject": "X"})
        self.assertEqual(ctx.exception.row["id"], "eventy-001")
        self.assertEqual(database.apply_edit("eventy-001", {"status": "blad"})["status"], "blad")

    def test_fields_outside_whitelist_are_ignored(self):
        database = make_db()
        put(database, sample_row(1))
        database.apply_edit("eventy-001", {"company": "Hak", "sent_at": "2026-01-01T00:00:00", "subject": "OK"})
        row = database.get("eventy-001")
        self.assertEqual((row["company"], row["sent_at"], row["subject"]), ("Firma 1", "", "OK"))


class ClaimTests(unittest.TestCase):
    def test_second_claim_loses(self):
        database = make_db()
        put(database, sample_row(1))
        first = database.claim_for_sending("eventy-001")
        self.assertEqual(first["status"], st.STATUS_SENDING)
        self.assertIsNone(database.claim_for_sending("eventy-001"))

    def test_claim_requires_email_body_and_allowed_status(self):
        database = make_db()
        put(database,
            sample_row(1, email=""),
            sample_row(2, body_html="", body_text=""),
            sample_row(3, status="wyslano", sent_at="2026-09-29T14:00:00"),
            sample_row(4, status="blad", error="x"))
        self.assertIsNone(database.claim_for_sending("eventy-001"))
        self.assertIsNone(database.claim_for_sending("eventy-002"))
        self.assertIsNone(database.claim_for_sending("eventy-003"))
        self.assertIsNone(database.claim_for_sending("eventy-004", allowed_from=(st.STATUS_PENDING,)))
        self.assertIsNotNone(database.claim_for_sending("eventy-004"))

    def test_mark_sent_and_error(self):
        database = make_db()
        put(database, sample_row(1), sample_row(2))
        database.claim_for_sending("eventy-001")
        sent = database.mark_sent("eventy-001")
        self.assertEqual((sent["status"], sent["sent_at"], sent["error"]), ("wyslano", "2026-09-30T12:00:00", ""))
        database.claim_for_sending("eventy-002")
        failed = database.mark_error("eventy-002", "SMTP padł")
        self.assertEqual((failed["status"], failed["error"], failed["retry_count"]), ("blad", "SMTP padł", "1"))

    def test_mark_error_respects_manual_fix(self):
        database = make_db()
        put(database, sample_row(1))
        database.claim_for_sending("eventy-001")
        database.apply_edit("eventy-001", {"status": "wyslano"})
        row = database.mark_error("eventy-001", "za późno")
        self.assertEqual((row["status"], row["retry_count"]), ("wyslano", "0"))

    def test_race_between_threads_has_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = make_db(url=f"sqlite:///{os.path.join(tmp, 'race.db')}")
            put(database, sample_row(1))
            winners, barrier = [], threading.Barrier(8)

            def worker():
                barrier.wait()
                if database.claim_for_sending("eventy-001"):
                    winners.append(1)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(len(winners), 1)


class StaleClaimTests(unittest.TestCase):
    def test_only_old_claims_are_recovered_and_never_back_to_pending(self):
        clock = FakeClock()
        database = make_db(clock)
        put(database, sample_row(1), sample_row(2))
        database.claim_for_sending("eventy-001")
        clock.advance(minutes=9)
        database.claim_for_sending("eventy-002")
        clock.advance(minutes=2)
        recovered = database.recover_stale_claims()
        self.assertEqual(recovered, ["eventy-001"])
        row = database.get("eventy-001")
        self.assertEqual((row["status"], row["error"]), ("blad", dbmod.STALE_CLAIM_MESSAGE))
        self.assertEqual(row["retry_count"], "0")
        self.assertEqual(database.get("eventy-002")["status"], st.STATUS_SENDING)


class SentTodayTests(unittest.TestCase):
    def test_day_boundaries(self):
        database = make_db(FakeClock())
        put(database,
            sample_row(1, status="wyslano", sent_at="2026-09-29T23:59:59"),
            sample_row(2, status="wyslano", sent_at="2026-09-30T00:00:00"),
            sample_row(3, status="wyslano", sent_at="2026-09-30T23:59:59"),
            sample_row(4, status="wyslano", sent_at="2026-10-01T00:00:00"),
            sample_row(5, status="blad", sent_at=""))
        self.assertEqual(database.sent_today_count(), 2)

    def test_replies_do_not_lower_todays_count(self):
        database = make_db(FakeClock())
        put(database,
            sample_row(1, status="wyslano", sent_at="2026-09-30T08:00:00"),
            sample_row(2, status="odpowiedzial", sent_at="2026-09-30T09:00:00"),
            sample_row(3, status="zainteresowany", sent_at="2026-09-30T10:00:00"),
            sample_row(4, status="nie_kontaktowac", sent_at="2026-09-30T11:00:00"),
            sample_row(5, status="nie_kontaktowac", sent_at=""),
            sample_row(6, status="odpowiedzial", sent_at="2026-09-29T11:00:00"))
        self.assertEqual(database.sent_today_count(), 4)


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        put(self.database,
            sample_row(2, segment="driverzy", company="Zażółć Gęślą"),
            sample_row(10, segment="eventy", status="wyslano", sent_at="2026-09-30T09:00:00"),
            sample_row(2, segment="eventy"),
            sample_row(1, segment="eventy", status="blad", error="x"))

    def test_fetch_all_orders_eventy_first_then_row_number(self):
        self.assertEqual([r["id"] for r in self.database.fetch_all()],
                         ["eventy-001", "eventy-002", "eventy-010", "driverzy-002"])

    def test_select_candidates_orders_by_id_and_filters(self):
        ids = lambda **kw: [r["id"] for r in self.database.select_candidates(**kw)]
        self.assertEqual(ids(), ["driverzy-002", "eventy-002"])
        self.assertEqual(ids(segment="eventy"), ["eventy-002"])
        self.assertEqual(ids(company="gęślą"), ["driverzy-002"])
        self.assertEqual(ids(company="%"), [])
        self.assertEqual(ids(statuses=("blad",)), ["eventy-001"])
        self.assertEqual(ids(limit=1), ["driverzy-002"])

    def test_counts_and_iteration(self):
        self.assertEqual(self.database.count_rows(), 4)
        self.assertEqual(self.database.count_rows(dbmod.RowFilter(segments=("eventy",), statuses=("blad", "wyslano"))), 2)
        self.assertEqual(self.database.status_counts(), {"do_wyslania": 2, "wyslano": 1, "blad": 1})
        self.assertEqual(self.database.distinct_segments(), ["eventy", "driverzy"])
        rows = list(self.database.iter_rows(columns=("id", "sent_at")))
        self.assertEqual(rows[2]["id"], "eventy-010")
        self.assertEqual(rows[2]["sent_at"].year, 2026)


class MysqlDdlTests(unittest.TestCase):
    def test_table_compiles_for_mysql_with_expected_types(self):
        ddl = str(CreateTable(dbmod.maile).compile(dialect=mysql.dialect()))
        for expected in ("MEDIUMTEXT", "`row_number` INTEGER", "DEFAULT 0", "utf8mb4", "InnoDB",
                         "utf8mb4_0900_ai_ci", "sent_at DATETIME", "id VARCHAR(32) NOT NULL"):
            self.assertIn(expected, ddl)
        indexes = [str(CreateIndex(i).compile(dialect=mysql.dialect())) for i in dbmod.maile.indexes]
        self.assertTrue(any("`row_number`" in i for i in indexes))


def _legacy_csv_path():
    candidates = [config.LEGACY_CSV] + sorted(glob.glob(os.path.join(config.BACKUP_DIR, "Maile_*_przed-migracja.csv")))
    return next((p for p in candidates if os.path.exists(p)), None)


@unittest.skipUnless(_legacy_csv_path(), "brak Maile.csv ani kopii z migracji")
class LegacyRoundTripTests(unittest.TestCase):
    def test_real_csv_survives_db_roundtrip_byte_for_byte(self):
        path = _legacy_csv_path()
        database = make_db()
        with database.transaction() as conn:
            database.insert_rows(conn, legacy_csv.read_rows(path))
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "roundtrip.csv")
            legacy_csv.write_rows(out, database.fetch_all())
            def digest(p):
                with open(p, "rb") as f:
                    return hashlib.sha256(f.read()).hexdigest()

            self.assertEqual(digest(out), digest(path))


if __name__ == "__main__":
    unittest.main()
