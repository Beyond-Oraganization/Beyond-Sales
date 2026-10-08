"""Pobieranie odpowiedzi od końca do końca na atrapie poczty: etap nagłówków, status maila, wiele skrzynek, kursor i przebieg próbny."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import sqlalchemy as sa

from perno.db.replies_schema import odp_watki, odp_wiadomosci
from perno.db.replies_store import Cursor, ReplyStore
from perno.mailer.imap_client import KIND_AUTH, ImapError
from perno.services import replies_sync as sync
from tests import eml_samples as eml
from tests.fake_imap import FakeAccounts, FakeImap, make_eml
from tests.fixtures import FakeClock, make_db, put, sample_row

NOW = datetime(2026, 10, 8, 10, 0)
SENT = "2026-09-29T13:00:00"
CONFIG = {"SMTP_HOST": "s", "SMTP_PORT": "465", "SMTP_USER": "roman@z3x.pl", "SMTP_PASSWORD": "p", "IMAP_HOST": "imap.example.com",
          "SMTP2_HOST": "s", "SMTP2_PORT": "465", "SMTP2_USER": "drugi@z3x.pl", "SMTP2_PASSWORD": "p2", "IMAP2_HOST": "imap2.example.com"}
ONE_BOX = {k: v for k, v in CONFIG.items() if not k.startswith(("SMTP2", "IMAP2"))}


class SyncCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.db = make_db(self.clock)
        self.store = ReplyStore(self.db)
        self.store.ensure_schema()
        put(self.db,
            sample_row(1, status="wyslano", sent_at=SENT, email="anna@mylimo.pl", subject="Rezerwacje limuzyn, Kraków", website="https://mylimo.pl"),
            sample_row(2, status="wyslano", sent_at=SENT, email="nieistnieje@firma.pl", subject="Konsultacje dla spółdzielni"),
            sample_row(3, status="wyslano", sent_at=SENT, email="biuro@firma3.pl", subject="Szkoła jazdy, Gdańsk"),
            sample_row(4, status="zainteresowany", sent_at=SENT, email="jan@sklep.pl", subject="Sklep z rowerami"))
        self.tag("eventy-001", "roman@z3x.pl", "orig1@z3x.pl")
        self.tag("eventy-002", "roman@z3x.pl", "orig2@z3x.pl")
        self.tag("eventy-003", "drugi@z3x.pl", "orig3@z3x.pl")
        self.tag("eventy-004", "roman@z3x.pl", "orig4@z3x.pl")

    def tag(self, row_id, sent_from, message_id):
        from perno.db.schema import maile

        with self.db.engine.begin() as conn:
            conn.execute(sa.update(maile).where(maile.c.id == row_id).values(sent_from=sent_from, message_id=message_id))

    def inbox(self):
        box = FakeImap({"INBOX": {}, "Sent": {}, "Junk": {}, "Drafts": {}}, roles={"Sent": "sent", "Junk": "junk"})
        box.add("INBOX", 1, eml.plain_reply(in_reply_to="<orig1@z3x.pl>", body="Dzień dobry, proszę o cennik.\n\nAnna"))
        box.add("INBOX", 2, eml.newsletter())
        box.add("INBOX", 3, eml.dsn_bounce(recipient="nieistnieje@firma.pl", original_id="<orig2@z3x.pl>"))
        box.add("INBOX", 4, eml.internal_forward())
        box.add("INBOX", 5, eml.plain_reply(sender="nieznany@trzecia.pl", subject="Re: Konsultacje dla spółdzielni", message_id="<obcy@trzecia.pl>"))
        box.add("INBOX", 6, make_eml(sender="kolega@praca.pl", subject="Faktura za wrzesień", body="Praca robocza, nie dla nas.", message_id="<praca@praca.pl>"))
        return box

    def accounts(self, first=None, second=None):
        return FakeAccounts({"roman@z3x.pl": first or self.inbox(), "drugi@z3x.pl": second or FakeImap({"INBOX": {}})})

    def run_sync(self, factory, config=None, **opts):
        return sync.fetch_replies(self.db, self.store, config or CONFIG, factory, options=sync.SyncOptions(ignore=("z3x.io",), **opts),
                                  now=NOW, clock=self.clock)

    def messages(self):
        with self.store.engine.connect() as conn:
            return [dict(r) for r in conn.execute(sa.select(odp_wiadomosci).order_by(odp_wiadomosci.c.id)).mappings()]

    def thread(self, thread_id):
        with self.store.engine.connect() as conn:
            row = conn.execute(sa.select(odp_watki).where(odp_watki.c.id == thread_id)).mappings().first()
        return dict(row) if row else None

    def status(self, n):
        return self.db.get(f"eventy-{n:03d}")["status"]


class FirstRunTests(SyncCase):
    def test_a_reply_a_bounce_and_a_stranger_with_our_subject_are_stored_and_work_mail_is_not(self):
        first = self.inbox()
        report = self.run_sync(self.accounts(first))
        self.assertEqual(report.skipped, "")
        stored = {m["from_addr"]: m for m in self.messages()}
        self.assertEqual(sorted(stored), ["anna@mylimo.pl", "mailer-daemon@mx.z3x.pl", "nieznany@trzecia.pl"])
        self.assertEqual((stored["anna@mylimo.pl"]["thread_id"], stored["anna@mylimo.pl"]["kind"], stored["anna@mylimo.pl"]["match_method"]),
                         ("eventy-001", "human", "header"))
        self.assertEqual((stored["mailer-daemon@mx.z3x.pl"]["thread_id"], stored["mailer-daemon@mx.z3x.pl"]["bounce_type"]), ("eventy-002", "hard"))
        self.assertEqual((stored["nieznany@trzecia.pl"]["thread_id"], stored["nieznany@trzecia.pl"]["kind"]), (None, "human"))

    def test_the_bodies_of_unrelated_and_internal_mail_are_never_downloaded(self):
        first = self.inbox()
        self.run_sync(self.accounts(first))
        self.assertEqual(sorted(first.full_fetches), [1, 3, 5])  # 2: newsletter, 4: wewnętrzna, 6: praca
        self.assertEqual(sorted(first.header_fetches[0]), [1, 2, 3, 4, 5, 6])

    def test_the_company_status_follows_the_first_human_reply_and_a_hard_bounce(self):
        report = self.run_sync(self.accounts())
        self.assertEqual((self.status(1), self.status(2), self.status(3)), ("odpowiedzial", "zwrot", "wyslano"))
        self.assertEqual((report.replied, report.bounced), (1, 1))
        self.assertEqual(self.db.get("eventy-001")["sent_at"], SENT)  # data wysłania zostaje

    def test_a_status_chosen_by_a_human_is_never_overwritten(self):
        first = self.inbox()
        first.add("INBOX", 7, eml.plain_reply(sender="jan@sklep.pl", subject="Re: Sklep z rowerami", message_id="<sk1@sklep.pl>", in_reply_to="<orig4@z3x.pl>"))
        self.run_sync(self.accounts(first))
        self.assertEqual(self.status(4), "zainteresowany")
        self.assertEqual(self.thread("eventy-004")["human_in_count"], 1)

    def test_the_thread_is_unread_awaiting_and_remembers_its_mailbox(self):
        self.run_sync(self.accounts())
        thread = self.thread("eventy-001")
        self.assertEqual((thread["read_at"], thread["awaiting_reply"], thread["mailbox"], thread["human_in_count"], thread["weak"]),
                         (None, True, "roman@z3x.pl", 1, False))

    def test_an_auto_reply_is_attached_to_its_mail_without_changing_the_status(self):
        first = self.inbox()
        first.add("INBOX", 8, eml.auto_reply().replace(b"Rezerwacje limuzyn, Krak\xc3\xb3w", b"Szko\xc5\x82a jazdy, Gda\xc5\x84sk").replace(
            b"biuro@firma.pl", b"biuro@firma3.pl"))
        self.run_sync(self.accounts(first))
        auto = [m for m in self.messages() if m["kind"] == "auto"]
        self.assertEqual([(m["thread_id"], m["match_method"]) for m in auto], [("eventy-003", "address")])
        self.assertEqual(self.status(3), "wyslano")
        self.assertEqual(self.thread("eventy-003")["human_in_count"], 0)

    def test_a_soft_bounce_changes_nothing_but_is_recorded(self):
        first = self.inbox()
        first.add("INBOX", 9, eml.dsn_bounce(recipient="biuro@firma3.pl", original_id="<orig3@z3x.pl>", status="4.2.2", action="delayed"))
        self.run_sync(self.accounts(first))
        soft = [m for m in self.messages() if m["thread_id"] == "eventy-003"]
        self.assertEqual([(m["kind"], m["bounce_type"]) for m in soft], [("bounce", "soft")])
        self.assertEqual(self.status(3), "wyslano")

    def test_a_bounce_for_someone_we_never_wrote_to_is_dropped(self):
        first = FakeImap({"INBOX": {}})
        first.add("INBOX", 1, eml.dsn_bounce(recipient="obcy@gdzies.pl", original_id="<cudze@inny>"))
        self.run_sync(self.accounts(first), ONE_BOX)
        self.assertEqual(self.messages(), [])

    def test_the_first_search_starts_a_day_before_the_first_mail_went_out_and_reading_is_read_only(self):
        first = self.inbox()
        self.run_sync(self.accounts(first))
        self.assertIn(("INBOX", datetime(2026, 9, 28, 13, 0).date(), 0), first.searches)
        self.assertEqual((first.appended, first.opened_readonly), ([], True))

    def test_folders_are_detected_and_drafts_are_left_alone(self):
        first = self.inbox()
        first.add("Drafts", 1, eml.plain_reply(in_reply_to="<orig1@z3x.pl>", message_id="<szkic@x>"))
        self.run_sync(self.accounts(first))
        self.assertNotIn("Drafts", {s[0] for s in first.searches})
        self.assertEqual({s[0] for s in first.searches}, {"INBOX", "Sent", "Junk"})

    def test_a_reply_that_landed_in_junk_is_found(self):
        first = FakeImap({"INBOX": {}, "Junk": {}}, roles={"Junk": "junk"})
        first.add("Junk", 1, eml.plain_reply(in_reply_to="<orig1@z3x.pl>", message_id="<spam1@x>"))
        self.run_sync(self.accounts(first), ONE_BOX)
        self.assertEqual([(m["folder"], m["thread_id"]) for m in self.messages()], [("Junk", "eventy-001")])


class SentFolderTests(SyncCase):
    def test_a_reply_written_in_a_mail_client_joins_the_thread_and_clears_awaiting(self):
        first = self.inbox()
        first.add("Sent", 1, make_eml(sender="roman@z3x.pl", to="anna@mylimo.pl", subject="Re: Rezerwacje limuzyn, Kraków", body="Wysyłam cennik.",
                                      message_id="<moja@z3x.pl>", in_reply_to="<odp1@mylimo.pl>", when=datetime(2026, 10, 3, 12, 0)))
        first.add("Sent", 2, make_eml(sender="roman@z3x.pl", to="ktos@zupelnie-gdzie-indziej.pl", subject="Coś innego", message_id="<inna@z3x.pl>"))
        self.run_sync(self.accounts(first))
        out = [m for m in self.messages() if m["direction"] == "out"]
        self.assertEqual([(m["thread_id"], m["kind"], m["author"], m["to_addr"]) for m in out], [("eventy-001", "human", "human", "anna@mylimo.pl")])
        thread = self.thread("eventy-001")
        self.assertEqual((thread["awaiting_reply"], thread["last_out_at"] is not None), (False, True))
        self.assertEqual(self.status(1), "odpowiedzial")


class OutgoingTests(SyncCase):
    def test_our_own_message_never_changes_the_status_or_flags_an_opt_out(self):
        first = FakeImap({"INBOX": {}, "Sent": {}}, roles={"Sent": "sent"})
        first.add("Sent", 1, make_eml(sender="roman@z3x.pl", to="anna@mylimo.pl", subject="Re: Rezerwacje limuzyn, Kraków", message_id="<moja2@z3x.pl>",
                                      body="Potwierdzam wypisanie Państwa z naszej listy mailingowej.", in_reply_to="<orig1@z3x.pl>"))
        self.run_sync(self.accounts(first), ONE_BOX)
        self.assertEqual(self.status(1), "wyslano")
        thread = self.thread("eventy-001")
        self.assertEqual((thread["optout"], thread["human_in_count"], thread["awaiting_reply"]), (False, 0, False))
        self.assertFalse(self.messages()[0]["optout_hint"])


class IncrementalTests(SyncCase):
    def test_a_folder_larger_than_the_cap_is_read_in_pieces_oldest_first(self):
        first = FakeImap({"INBOX": {}})
        for uid in range(1, 7):
            first.add("INBOX", uid, eml.plain_reply(in_reply_to="<orig1@z3x.pl>", message_id=f"<m{uid}@mylimo.pl>"))
        accounts = self.accounts(first)
        original = sync.MAX_PER_FOLDER
        sync.MAX_PER_FOLDER = 2
        try:
            totals = [self.run_sync(accounts, ONE_BOX).totals()["stored"] for _ in range(4)]
        finally:
            sync.MAX_PER_FOLDER = original
        self.assertEqual(totals, [2, 2, 2, 0])
        self.assertEqual(self.store.get_cursor("roman@z3x.pl", "INBOX"), Cursor(1, 6))

    def test_untagged_mail_from_before_several_mailboxes_belongs_to_the_main_one(self):
        from perno.db.schema import maile

        with self.db.engine.begin() as conn:
            conn.execute(sa.update(maile).where(maile.c.id == "eventy-001").values(sent_from=None))
        first = FakeImap({"INBOX": {}})
        first.add("INBOX", 1, eml.plain_reply(in_reply_to="<orig1@z3x.pl>"))
        self.run_sync(self.accounts(first), ONE_BOX)
        self.assertEqual(self.thread("eventy-001")["mailbox"], "roman@z3x.pl")

    def test_without_an_explicit_time_the_database_clock_is_used(self):
        first = FakeImap({"INBOX": {}})
        first.add("INBOX", 1, eml.plain_reply(in_reply_to="<orig1@z3x.pl>"))
        sync.fetch_replies(self.db, self.store, ONE_BOX, self.accounts(first))
        self.assertEqual(self.messages()[0]["fetched_at"], NOW)

    def test_zero_workers_still_runs(self):
        self.assertEqual(self.run_sync(self.accounts(), workers=0).totals()["stored"], 3)


    def test_a_second_run_with_nothing_new_fetches_nothing_and_stores_nothing(self):
        first = self.inbox()
        accounts = self.accounts(first)
        self.run_sync(accounts)
        stored = len(self.messages())
        first.header_fetches.clear()
        first.full_fetches.clear()
        report = self.run_sync(accounts)
        self.assertEqual((report.totals()["stored"], len(self.messages()), first.header_fetches, first.full_fetches), (0, stored, [], []))
        self.assertEqual(self.store.get_cursor("roman@z3x.pl", "INBOX"), Cursor(1, 6))

    def test_only_new_messages_are_read_on_the_next_run(self):
        first = self.inbox()
        accounts = self.accounts(first)
        self.run_sync(accounts)
        first.header_fetches.clear()
        first.add("INBOX", 7, eml.plain_reply(in_reply_to="<odp1@mylimo.pl>", message_id="<odp2@mylimo.pl>", body="Jeszcze jedno pytanie."))
        report = self.run_sync(accounts)
        self.assertEqual((first.header_fetches, report.totals()["stored"]), ([[7]], 1))
        self.assertEqual(self.thread("eventy-001")["human_in_count"], 2)

    def test_a_reply_to_our_own_reply_stays_in_the_same_thread(self):
        first = self.inbox()
        accounts = self.accounts(first)
        self.run_sync(accounts)
        from perno.domain import replies as rpl

        self.store.save_message(rpl.NewMessage(direction="out", kind="human", mailbox="roman@z3x.pl", identity="panel-1@z3x.pl", message_id="panel-1@z3x.pl",
                                               thread_id="eventy-001", company_id="eventy-001", date_at=NOW, state="sent", author="human"), NOW)
        first.add("INBOX", 7, eml.plain_reply(sender="inna.osoba@mylimo.pl", subject="Zupełnie inny temat", message_id="<odp3@mylimo.pl>",
                                              in_reply_to="<panel-1@z3x.pl>"))
        self.run_sync(accounts)
        self.assertEqual([m["thread_id"] for m in self.messages() if m["message_id"] == "odp3@mylimo.pl"], ["eventy-001"])

    def test_a_changed_uidvalidity_rescans_without_duplicating_anything(self):
        first = self.inbox()
        accounts = self.accounts(first)
        self.run_sync(accounts)
        stored = len(self.messages())
        first.uidvalidity = 2
        report = self.run_sync(accounts)
        self.assertEqual((len(self.messages()), report.totals()["stored"], report.totals()["duplicates"]), (stored, 0, 3))
        self.assertEqual(self.store.get_cursor("roman@z3x.pl", "INBOX"), Cursor(2, 6))

    def test_a_message_without_a_message_id_is_not_stored_twice(self):
        first = FakeImap({"INBOX": {}})
        raw = eml.plain_reply(message_id="<x@y>", sender="anna@mylimo.pl").replace(b"Message-ID: <x@y>\r\n", b"")
        first.add("INBOX", 1, raw)
        accounts = self.accounts(first)
        self.run_sync(accounts, ONE_BOX)
        first.uidvalidity = 5
        self.run_sync(accounts, ONE_BOX)
        self.assertEqual(len(self.messages()), 1)


class DryRunTests(SyncCase):
    def test_a_trial_run_counts_but_writes_nothing_and_takes_no_lock(self):
        report = self.run_sync(self.accounts(), dry_run=True)
        totals = report.totals()
        self.assertEqual((totals["stored"], totals["candidates"], totals["dry_run"]), (3, 3, True))
        self.assertEqual((self.messages(), self.status(1), self.status(2)), ([], "wyslano", "wyslano"))
        self.assertIsNone(self.store.get_cursor("roman@z3x.pl", "INBOX"))
        self.assertEqual(self.store.last_run()["finished_at"], None)

    def test_a_trial_run_after_a_real_one_reports_everything_as_duplicates(self):
        accounts = self.accounts()
        self.run_sync(accounts)
        self.store.save_cursor("roman@z3x.pl", "INBOX", Cursor(0, 0), NOW)  # jakby kursor zginął
        report = self.run_sync(accounts, dry_run=True)
        self.assertEqual((report.totals()["stored"], report.totals()["duplicates"]), (0, 3))


class MailboxIsolationTests(SyncCase):
    def test_a_failing_mailbox_does_not_stop_the_others_and_its_error_is_remembered(self):
        second = FakeImap({"INBOX": {}}, fail_with=ImapError("Authentication failed", KIND_AUTH))
        report = self.run_sync(self.accounts(second=second))
        by_box = {m.mailbox: m for m in report.mailboxes}
        self.assertEqual((by_box["roman@z3x.pl"].ok, by_box["drugi@z3x.pl"].ok, by_box["drugi@z3x.pl"].kind), (True, False, "auth"))
        self.assertEqual(len(self.messages()), 3)
        state = {s["mailbox"]: s for s in self.store.mailbox_states()}
        self.assertEqual((state["drugi@z3x.pl"]["error"], state["drugi@z3x.pl"]["kind"], state["roman@z3x.pl"]["error"]), ("Authentication failed", "auth", ""))
        self.assertEqual(report.totals()["failed"], 1)

    def test_an_unexpected_crash_in_one_mailbox_is_contained(self):
        second = FakeImap({"INBOX": {}}, fail_with=ZeroDivisionError("boom"))
        report = self.run_sync(self.accounts(second=second))
        self.assertEqual([m.ok for m in report.mailboxes], [True, False])
        self.assertIn("ZeroDivisionError", report.mailboxes[1].error)

    def test_the_failure_counter_counts_failures_not_successes(self):
        config = {**CONFIG, "SMTP3_HOST": "s", "SMTP3_PORT": "1", "SMTP3_USER": "trzeci@z3x.pl", "SMTP3_PASSWORD": "p", "IMAP3_HOST": "i3"}
        sessions = FakeAccounts({"roman@z3x.pl": self.inbox(), "drugi@z3x.pl": FakeImap({"INBOX": {}}),
                                 "trzeci@z3x.pl": FakeImap({"INBOX": {}}, fail_with=ImapError("zły login", KIND_AUTH))})
        totals = self.run_sync(sessions, config).totals()
        self.assertEqual((totals["mailboxes"], totals["failed"]), (3, 1))

    def test_a_recovered_mailbox_clears_its_error(self):
        second = FakeImap({"INBOX": {}}, fail_with=ImapError("zły login", KIND_AUTH))
        accounts = self.accounts(second=second)
        self.run_sync(accounts)
        second.fail_with = None
        self.run_sync(accounts)
        self.assertEqual({s["mailbox"]: s["error"] for s in self.store.mailbox_states()}["drugi@z3x.pl"], "")

    def test_a_reply_that_arrives_in_another_mailbox_is_flagged_and_the_thread_keeps_the_senders_mailbox(self):
        second = FakeImap({"INBOX": {}})
        second.add("INBOX", 1, eml.plain_reply(in_reply_to="<orig1@z3x.pl>", message_id="<przekierowana@x>"))
        self.run_sync(self.accounts(FakeImap({"INBOX": {}}), second))
        thread = self.thread("eventy-001")
        self.assertEqual((thread["mailbox"], thread["other_mailbox"]), ("roman@z3x.pl", True))
        self.assertEqual(self.messages()[0]["mailbox"], "drugi@z3x.pl")

    def test_only_one_mailbox_can_be_requested(self):
        accounts = self.accounts()
        self.run_sync(accounts, only="drugi@z3x.pl")
        self.assertEqual(accounts.opened, ["drugi@z3x.pl"])

    def test_a_half_filled_mailbox_is_reported_and_the_rest_still_work(self):
        config = {**CONFIG, "IMAP3_PORT": "993", "SMTP3_USER": "trzeci@z3x.pl"}
        accounts = self.accounts()
        report = self.run_sync(accounts, config)
        broken = [m for m in report.mailboxes if not m.ok]
        self.assertEqual([(m.slot, m.kind) for m in broken], [(3, "config")])
        self.assertIn("IMAP3_HOST", broken[0].error)
        self.assertEqual(len(self.messages()), 3)


class GuardTests(SyncCase):
    def test_without_imap_nothing_runs(self):
        report = self.run_sync(self.accounts(), {k: v for k, v in CONFIG.items() if not k.startswith(("IMAP", "SMTP2"))})
        self.assertEqual((report.skipped, report.mailboxes), ("brak skrzynek z ustawionym IMAP", []))

    def test_before_the_first_mail_there_is_nothing_to_match(self):
        empty = ReplyStore(make_db())
        empty.ensure_schema()
        report = sync.fetch_replies(empty.db, empty, CONFIG, self.accounts(), now=NOW)
        self.assertEqual(report.skipped, "nie wysłano jeszcze żadnego maila")
        self.assertFalse(empty.last_run()["running"])  # blokada zwolniona

    def test_a_second_run_at_the_same_time_is_refused_and_does_not_release_the_others_lock(self):
        self.assertTrue(self.store.acquire_run("ktos-inny", NOW))
        report = self.run_sync(self.accounts())
        self.assertEqual(report.skipped, "inny przebieg pobierania trwa")
        self.assertTrue(self.store.last_run()["running"])
        self.assertEqual(self.messages(), [])

    def test_the_lock_is_released_and_the_summary_saved_after_a_run(self):
        self.run_sync(self.accounts())
        last = self.store.last_run()
        self.assertFalse(last["running"])
        self.assertEqual(last["summary"]["stored"], 3)
        self.assertIsNotNone(last["finished_at"])

    def test_an_exhausted_time_budget_postpones_the_rest_without_losing_the_cursor(self):
        first = self.inbox()
        report = self.run_sync(self.accounts(first), budget=timedelta(seconds=0))
        self.assertTrue(all(m.postponed for m in report.mailboxes))
        self.assertEqual(self.messages(), [])
        self.assertIsNone(self.store.get_cursor("roman@z3x.pl", "INBOX"))
        again = self.run_sync(self.accounts(first))
        self.assertEqual(again.totals()["stored"], 3)


class ReportTests(SyncCase):
    def test_the_report_lists_counts_per_kind_without_addresses_or_text(self):
        report = self.run_sync(self.accounts())
        totals = report.totals()
        self.assertEqual((totals["seen"], totals["candidates"], totals["unassigned"]), (6, 3, 1))
        self.assertEqual([m.postponed for m in report.mailboxes], [False, False])
        self.assertEqual(totals["kinds"], {"in:human": 2, "in:bounce": 1})
        self.assertNotIn("anna", repr(totals))
        folder = report.mailboxes[0].folders
        self.assertEqual([(f["folder"], f["seen"], f["found"]) for f in folder], [("INBOX", 6, 3), ("Sent", 0, 0), ("Junk", 0, 0)])

    def test_the_snippet_is_the_new_text_not_the_quote(self):
        first = FakeImap({"INBOX": {}})
        first.add("INBOX", 1, eml.plain_reply(in_reply_to="<orig1@z3x.pl>", body="Proszę o cennik.\n\nW dniu pon. Roman <roman@z3x.pl> napisał(a):\n> nasz stary mail"))
        self.run_sync(self.accounts(first), ONE_BOX)
        self.assertEqual(self.messages()[0]["snippet"], "Proszę o cennik.")

    def test_opt_out_requests_are_flagged_on_the_thread(self):
        first = FakeImap({"INBOX": {}})
        first.add("INBOX", 1, eml.plain_reply(in_reply_to="<orig1@z3x.pl>", body="Proszę o wypisanie nas z listy mailingowej."))
        self.run_sync(self.accounts(first), ONE_BOX)
        self.assertTrue(self.thread("eventy-001")["optout"])
        self.assertTrue(self.messages()[0]["optout_hint"])


if __name__ == "__main__":
    unittest.main()
