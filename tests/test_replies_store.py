"""Magazyn odpowiedzi na sqlite: kursor, blokada przebiegu, zapis wiadomości z aktualizacją wątku, liczniki i przejście statusu."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import sqlalchemy as sa

from perno.db.errors import InvalidValue
from perno.db.replies_schema import odp_watki, odp_wiadomosci
from perno.db.replies_store import Cursor, ReplyStore
from perno.domain import message_ids
from perno.domain import replies as rp
from tests.fixtures import FakeClock, make_db, put, sample_row

NOW = datetime(2026, 10, 8, 10, 0)
MAILBOX = "roman@z3x.pl"


def message(identity="odp1@x.pl", *, thread="eventy-001", kind=rp.KIND_HUMAN, direction=rp.DIRECTION_IN, when=NOW, **kw) -> rp.NewMessage:
    fields = dict(direction=direction, kind=kind, mailbox=MAILBOX, identity=identity, message_id=identity, thread_id=thread,
                  company_id=thread, date_at=when, subject="Re: Temat", from_addr="anna@mylimo.pl", body_text="Treść",
                  match_method=rp.MATCH_ADDRESS, folder="INBOX", uid=5, uidvalidity=1, thread_mailbox=MAILBOX)
    fields.update(kw)
    return rp.NewMessage(**fields)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.db = make_db(self.clock)
        self.store = ReplyStore(self.db)
        self.store.ensure_schema()

    def thread(self, thread_id="eventy-001"):
        with self.store.engine.connect() as conn:
            return dict(conn.execute(sa.select(odp_watki).where(odp_watki.c.id == thread_id)).mappings().one())

    def rows(self):
        with self.store.engine.connect() as conn:
            return [dict(r) for r in conn.execute(sa.select(odp_wiadomosci).order_by(odp_wiadomosci.c.id)).mappings()]


class SchemaTests(StoreCase):
    def test_ensure_schema_is_idempotent_and_leaves_the_run_row(self):
        self.store.ensure_schema()
        self.assertEqual(self.store.last_run(), {"running": False, "started_at": None, "finished_at": None, "summary": {}})

    def test_the_main_mail_table_is_untouched(self):
        self.assertNotIn("odp_wiadomosci", [c.name for c in self.db.missing_columns()])


class CursorTests(StoreCase):
    def test_a_folder_has_no_cursor_until_one_is_saved_and_cursors_are_independent(self):
        self.assertIsNone(self.store.get_cursor(MAILBOX, "INBOX"))
        self.store.save_cursor(MAILBOX, "INBOX", Cursor(7, 120), NOW)
        self.store.save_cursor(MAILBOX, "Sent", Cursor(9, 3), NOW)
        self.store.save_cursor("drugi@z3x.pl", "INBOX", Cursor(1, 1), NOW)
        self.assertEqual(self.store.get_cursor(MAILBOX, "INBOX"), Cursor(7, 120))
        self.assertEqual(self.store.get_cursor(MAILBOX, "Sent"), Cursor(9, 3))
        self.store.save_cursor(MAILBOX, "INBOX", Cursor(7, 140), NOW)
        self.assertEqual(self.store.get_cursor(MAILBOX, "INBOX"), Cursor(7, 140))
        self.assertEqual(self.store.get_cursor("drugi@z3x.pl", "INBOX"), Cursor(1, 1))

    def test_an_error_is_remembered_per_mailbox_without_touching_cursors_and_a_success_clears_it(self):
        self.store.save_cursor(MAILBOX, "INBOX", Cursor(7, 120), NOW)
        self.store.record_error(MAILBOX, "Authentication failed", "auth", NOW + timedelta(minutes=1))
        state = self.store.mailbox_states()
        self.assertEqual([(s["mailbox"], s["error"], s["kind"]) for s in state], [(MAILBOX, "Authentication failed", "auth")])
        self.assertEqual(self.store.get_cursor(MAILBOX, "INBOX"), Cursor(7, 120))
        self.store.record_error(MAILBOX, "x" * 900, "network", NOW)
        self.assertEqual(len(self.store.mailbox_states()[0]["error"]), 500)
        self.store.save_cursor(MAILBOX, "", Cursor(0, 0), NOW)  # sukces zeruje błąd skrzynki
        self.assertEqual(self.store.mailbox_states()[0]["error"], "")

    def test_states_report_the_latest_fetch_and_the_folders(self):
        self.store.save_cursor(MAILBOX, "INBOX", Cursor(7, 120), NOW)
        self.store.save_cursor(MAILBOX, "Sent", Cursor(9, 3), NOW + timedelta(minutes=5))
        state = self.store.mailbox_states()[0]
        self.assertEqual(state["last_fetch_at"], NOW + timedelta(minutes=5))
        self.assertEqual(state["folders"], {"INBOX": {"uidvalidity": 7, "last_uid": 120}, "Sent": {"uidvalidity": 9, "last_uid": 3}})


class RunLockTests(StoreCase):
    def test_only_one_run_at_a_time_and_only_its_owner_can_release_it(self):
        self.assertTrue(self.store.acquire_run("a", NOW))
        self.assertFalse(self.store.acquire_run("b", NOW + timedelta(minutes=1)))
        self.assertTrue(self.store.last_run()["running"])
        self.store.finish_run("b", NOW, {"x": 1})  # nie jego blokada
        self.assertTrue(self.store.last_run()["running"])
        self.store.finish_run("a", NOW + timedelta(minutes=2), {"stored": 3})
        last = self.store.last_run()
        self.assertEqual((last["running"], last["summary"], last["finished_at"]), (False, {"stored": 3}, NOW + timedelta(minutes=2)))
        self.assertTrue(self.store.acquire_run("b", NOW + timedelta(minutes=3)))

    def test_a_dead_run_is_taken_over_after_the_stale_limit_but_not_before(self):
        self.assertTrue(self.store.acquire_run("a", NOW))
        self.assertFalse(self.store.acquire_run("b", NOW + timedelta(minutes=9, seconds=59)))
        self.assertTrue(self.store.acquire_run("b", NOW + timedelta(minutes=10, seconds=1)))


class SentMailsTests(StoreCase):
    def test_only_mails_that_really_went_out_are_listed_with_their_company_site(self):
        put(self.db, sample_row(1, status="wyslano", sent_at="2026-09-29T13:00:00", website="https://www.mylimo.pl/"),
            sample_row(2, status="do_wyslania"), sample_row(3, status="zwrot", sent_at="2026-09-30T10:00:00"),
            sample_row(4, status="odpowiedzial", sent_at="2026-10-01T10:00:00"), sample_row(5, status="wyslano", sent_at=""))
        self.db.mark_sent("eventy-001", "Roman@Z3X.pl", "orig@z3x.pl")
        rows = {r["id"]: r for r in self.store.sent_mails()}
        self.assertEqual(sorted(rows), ["eventy-001", "eventy-003", "eventy-004"])
        self.assertEqual((rows["eventy-001"]["sent_from"], rows["eventy-001"]["message_id"]), ("Roman@Z3X.pl", "orig@z3x.pl"))
        self.assertEqual(rows["eventy-001"]["website"], "https://www.mylimo.pl/")
        self.assertEqual(rows["eventy-001"]["company_id"], "eventy-001")

    def test_the_start_date_cuts_older_mails_and_the_first_sent_date_is_reported(self):
        put(self.db, sample_row(1, status="wyslano", sent_at="2026-09-29T13:00:00"), sample_row(2, status="wyslano", sent_at="2026-10-02T13:00:00"))
        self.assertEqual([r["id"] for r in self.store.sent_mails(datetime(2026, 10, 1))], ["eventy-002"])
        self.assertEqual(self.store.first_sent_at(), datetime(2026, 9, 29, 13, 0))

    def test_no_sent_mail_means_no_first_date(self):
        self.assertIsNone(self.store.first_sent_at())
        self.assertEqual(self.store.sent_mails(), [])


class SaveMessageTests(StoreCase):
    def test_a_human_reply_creates_the_thread_and_marks_it_unread_and_awaiting(self):
        self.assertIsNotNone(self.store.save_message(message(optout_hint=False), NOW))
        thread = self.thread()
        self.assertEqual((thread["human_in_count"], thread["awaiting_reply"], thread["read_at"], thread["last_in_at"], thread["mailbox"]),
                         (1, True, None, NOW, MAILBOX))
        self.assertEqual((thread["rating"], thread["optout"], thread["weak"], thread["company_id"]), (None, False, False, "eventy-001"))

    def test_the_same_message_twice_is_stored_once_even_without_a_message_id(self):
        first = message(identity=message_ids.identity("", "a@b.pl", "2026-10-08T10:00", "Temat", "Treść"), message_id="")
        self.assertIsNotNone(self.store.save_message(first, NOW))
        self.assertIsNone(self.store.save_message(first, NOW))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.thread()["human_in_count"], 1)  # duplikat nie liczy się drugi raz

    def test_replies_count_and_the_latest_activity_wins(self):
        self.store.save_message(message("a@x", when=NOW + timedelta(hours=5)), NOW)
        self.store.save_message(message("b@x", when=NOW), NOW)  # starsza, pobrana później
        thread = self.thread()
        self.assertEqual((thread["human_in_count"], thread["last_in_at"], thread["last_activity_at"]), (2, NOW + timedelta(hours=5), NOW + timedelta(hours=5)))

    def test_our_reply_after_theirs_clears_awaiting_but_an_older_one_does_not(self):
        self.store.save_message(message("a@x", when=NOW), NOW)
        self.store.save_message(message("o1@x", direction=rp.DIRECTION_OUT, when=NOW - timedelta(hours=1), from_addr=MAILBOX), NOW)
        self.assertTrue(self.thread()["awaiting_reply"])
        self.store.save_message(message("o2@x", direction=rp.DIRECTION_OUT, when=NOW + timedelta(hours=1), from_addr=MAILBOX), NOW)
        thread = self.thread()
        self.assertEqual((thread["awaiting_reply"], thread["last_out_at"], thread["human_in_count"]), (False, NOW + timedelta(hours=1), 1))

    def test_a_new_reply_reopens_a_closed_read_thread(self):
        self.store.save_message(message("a@x"), NOW)
        with self.store.engine.begin() as conn:
            conn.execute(sa.update(odp_watki).values(read_at=NOW, closed_at=NOW))
        self.store.save_message(message("b@x", when=NOW + timedelta(days=1)), NOW)
        thread = self.thread()
        self.assertEqual((thread["read_at"], thread["closed_at"], thread["awaiting_reply"]), (None, None, True))

    def test_auto_replies_and_bounces_attach_to_the_thread_without_making_it_a_conversation(self):
        self.store.save_message(message("auto@x", kind=rp.KIND_AUTO), NOW)
        self.store.save_message(message("bounce@x", kind=rp.KIND_BOUNCE, bounce_type="hard", from_addr="mailer-daemon@x"), NOW)
        thread = self.thread()
        self.assertEqual((thread["human_in_count"], thread["awaiting_reply"], thread["last_in_at"]), (0, False, None))
        counts = self.store.counts()
        self.assertEqual((counts["threads"], counts["auto"], counts["bounces"], counts["messages"]), (0, 1, 1, 2))

    def test_an_opt_out_hint_sticks_to_the_thread(self):
        self.store.save_message(message("a@x", optout_hint=True), NOW)
        self.store.save_message(message("b@x", optout_hint=False, when=NOW + timedelta(hours=1)), NOW)
        self.assertTrue(self.thread()["optout"])

    def test_a_weak_assignment_is_cleared_by_a_stronger_message_and_not_by_another_weak_one(self):
        self.store.save_message(message("a@x", match_method=rp.MATCH_DOMAIN, match_weak=True), NOW)
        self.assertTrue(self.thread()["weak"])
        self.store.save_message(message("b@x", match_method=rp.MATCH_DOMAIN, match_weak=True, when=NOW + timedelta(hours=1)), NOW)
        self.assertTrue(self.thread()["weak"])
        self.store.save_message(message("c@x", match_method=rp.MATCH_HEADER, when=NOW + timedelta(hours=2)), NOW)
        self.assertFalse(self.thread()["weak"])

    def test_an_unassigned_message_is_stored_without_a_thread(self):
        self.store.save_message(message("n@x", thread=None, company_id=None), NOW)
        self.assertEqual(self.store.counts()["unassigned"], 1)
        with self.store.engine.connect() as conn:
            self.assertEqual(conn.execute(sa.select(sa.func.count()).select_from(odp_watki)).scalar(), 0)

    def test_a_draft_is_stored_but_does_not_touch_the_thread(self):
        self.store.save_message(message("a@x"), NOW)
        before = self.thread()
        self.store.save_message(message("draft@x", direction=rp.DIRECTION_OUT, state=rp.STATE_DRAFT, when=NOW + timedelta(days=1)), NOW)
        self.assertEqual(self.thread(), before)
        self.assertEqual(len(self.rows()), 2)

    def test_stored_values_round_trip_and_unsafe_text_is_cleaned(self):
        self.store.save_message(message("a@x", subject="Temat\x00\x07", body_text="Treść\x00z\x1bzerem", attachments='[{"name": "a.pdf"}]', bounce_type="",
                                        in_reply_to="orig@z3x.pl", references="a@x orig@z3x.pl"), NOW)
        row = self.rows()[0]
        self.assertEqual((row["subject"], row["body_text"], row["attachments"], row["bounce_type"]), ("Temat", "Treśćzzerem", '[{"name": "a.pdf"}]', None))
        self.assertEqual((row["in_reply_to"], row["references_hdr"], row["fetched_at"], row["state"], row["direction"]),
                         ("orig@z3x.pl", "a@x orig@z3x.pl", NOW, "received", "in"))

    def test_known_threads_find_stored_message_ids_in_both_directions(self):
        self.store.save_message(message("a@x"), NOW)
        self.store.save_message(message("o@x", direction=rp.DIRECTION_OUT, thread="eventy-002"), NOW)
        self.store.save_message(message("n@x", thread=None), NOW)
        found = self.store.known_threads(["a@x", "o@x", "n@x", "nieznany@x", ""])
        self.assertEqual(found, {"a@x": "eventy-001", "o@x": "eventy-002"})  # nieprzypisana nie ma wątku
        self.assertEqual(self.store.known_threads([]), {})


class LimitsAndBoundariesTests(StoreCase):
    def test_long_values_are_cut_to_what_the_columns_hold(self):
        self.store.save_message(message("a@x", subject="s" * 600, from_name="n" * 300, from_addr="f" * 400, snippet="p" * 300,
                                        folder="d" * 300, thread_mailbox=""), NOW)
        row = self.rows()[0]
        self.assertEqual((len(row["subject"]), len(row["from_name"]), len(row["from_addr"]), len(row["snippet"]), len(row["folder"])),
                         (500, 255, 320, 255, 255))
        long_to = message("b@x", to_addr="t" * 1200)
        self.store.save_message(long_to, NOW)
        self.assertEqual(len(self.rows()[1]["to_addr"]), 1000)

    def test_a_thread_without_its_own_mailbox_takes_the_one_it_was_found_in(self):
        self.store.save_message(message("a@x", thread_mailbox=""), NOW)
        self.assertEqual(self.thread()["mailbox"], MAILBOX)

    def test_error_kind_and_text_are_cut(self):
        self.store.record_error(MAILBOX, "e" * 600, "k" * 40, NOW)
        state = self.store.mailbox_states()[0]
        self.assertEqual((len(state["error"]), len(state["kind"])), (500, 16))

    def test_the_run_lock_boundary_is_exactly_the_stale_limit(self):
        self.assertTrue(self.store.acquire_run("a", NOW))
        self.assertFalse(self.store.acquire_run("b", NOW + timedelta(minutes=10)))  # dokładnie 10 min: jeszcze trwa
        self.assertTrue(self.store.acquire_run("b", NOW + timedelta(minutes=10, microseconds=1)))

    def test_a_reply_written_at_the_very_moment_of_the_last_incoming_message_counts_as_an_answer(self):
        self.store.save_message(message("a@x", when=NOW), NOW)
        self.store.save_message(message("o@x", direction=rp.DIRECTION_OUT, when=NOW), NOW)
        self.assertFalse(self.thread()["awaiting_reply"])

    def test_the_start_date_is_inclusive(self):
        put(self.db, sample_row(1, status="wyslano", sent_at="2026-10-01T10:00:00"))
        self.assertEqual(len(self.store.sent_mails(datetime(2026, 10, 1, 10, 0))), 1)
        self.assertEqual(len(self.store.sent_mails(datetime(2026, 10, 1, 10, 0, 1))), 0)

    def test_the_latest_fetch_over_several_folders_wins_and_a_missing_summary_is_empty(self):
        self.store.save_cursor(MAILBOX, "INBOX", Cursor(1, 1), NOW + timedelta(hours=2))
        self.store.save_cursor(MAILBOX, "Sent", Cursor(1, 1), NOW)
        self.assertEqual(self.store.mailbox_states()[0]["last_fetch_at"], NOW + timedelta(hours=2))
        self.assertEqual(self.store.get_cursor(MAILBOX, "Nigdy"), None)

    def test_a_cursor_with_nulls_reads_as_zero(self):
        with self.store.engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO odp_stan (mailbox, folder, uidvalidity, last_uid) VALUES ('m', 'f', NULL, 0)"))
        self.assertEqual(self.store.get_cursor("m", "f"), Cursor(0, 0))

    def test_the_run_summary_survives_a_round_trip_and_garbage_is_ignored(self):
        self.store.acquire_run("a", NOW)
        self.store.finish_run("a", NOW, {"stored": 2, "ts": NOW})
        self.assertEqual(self.store.last_run()["summary"], {"stored": 2, "ts": "2026-10-08 10:00:00"})
        with self.store.engine.begin() as conn:
            conn.execute(sa.text("UPDATE odp_przebieg SET last_summary = 'to nie json'"))
        self.assertEqual(self.store.last_run()["summary"], {})


class CountsTests(StoreCase):
    def setUp(self):
        super().setUp()
        put(self.db, *(sample_row(n, status="wyslano", sent_at="2026-09-29T13:00:00") for n in (1, 2, 3)))

    def test_counts_reflect_threads_unread_unrated_and_unassigned(self):
        self.store.save_message(message("a@x", thread="eventy-001"), NOW)
        self.store.save_message(message("b@x", thread="eventy-002"), NOW)
        self.store.save_message(message("n@x", thread=None), NOW)
        with self.store.engine.begin() as conn:
            conn.execute(sa.update(odp_watki).where(odp_watki.c.id == "eventy-002").values(rating="positive", read_at=NOW, awaiting_reply=False))
        counts = self.store.counts()
        self.assertEqual((counts["threads"], counts["awaiting_reply"], counts["unrated"], counts["unread"], counts["unassigned"], counts["messages"]),
                         (2, 1, 1, 1, 1, 3))

    def test_every_counter_counts_its_own_thing(self):
        self.store.save_message(message("h1@x", thread="eventy-001"), NOW)
        self.store.save_message(message("au1@x", kind=rp.KIND_AUTO, thread="eventy-002"), NOW)
        self.store.save_message(message("au2@x", kind=rp.KIND_AUTO, thread="eventy-002"), NOW)
        self.store.save_message(message("b1@x", kind=rp.KIND_BOUNCE, thread="eventy-003"), NOW)
        counts = self.store.counts()
        self.assertEqual((counts["bounces"], counts["auto"], counts["threads"], counts["awaiting_reply"], counts["unread"], counts["unrated"]),
                         (1, 2, 1, 1, 1, 1))

    def test_an_empty_store_counts_zeros(self):
        self.assertEqual(set(self.store.counts().values()), {0})


class TransitionTests(StoreCase):
    def test_the_status_changes_only_from_the_expected_one(self):
        put(self.db, sample_row(1, status="wyslano", sent_at="2026-09-29T13:00:00"), sample_row(2, status="zainteresowany", sent_at="2026-09-29T13:00:00"),
            sample_row(3, status="do_wyslania"))
        self.assertTrue(self.db.transition_status("eventy-001", ["wyslano"], "odpowiedzial"))
        self.assertFalse(self.db.transition_status("eventy-002", ["wyslano"], "odpowiedzial"))  # człowiek zdążył ustawić inny
        self.assertFalse(self.db.transition_status("eventy-003", ["wyslano"], "odpowiedzial"))
        self.assertFalse(self.db.transition_status("nie-ma", ["wyslano"], "odpowiedzial"))
        self.assertEqual([self.db.get(f"eventy-00{n}")["status"] for n in (1, 2, 3)], ["odpowiedzial", "zainteresowany", "do_wyslania"])

    def test_the_send_date_is_kept_for_statuses_that_keep_it(self):
        put(self.db, sample_row(1, status="wyslano", sent_at="2026-09-29T13:00:00"))
        self.db.transition_status("eventy-001", ["wyslano"], "zwrot")
        self.assertEqual(self.db.get("eventy-001")["sent_at"], "2026-09-29T13:00:00")

    def test_an_unknown_status_is_refused(self):
        put(self.db, sample_row(1, status="wyslano"))
        with self.assertRaisesRegex(InvalidValue, "Nieznany status"):
            self.db.transition_status("eventy-001", ["wyslano"], "wymyslony")


if __name__ == "__main__":
    unittest.main()
