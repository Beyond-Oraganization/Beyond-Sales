"""Odpowiedzi z panelu: widok edytora, szkice, wysyłka ze skrzynki wątku, ostrzeżenia, błędy SMTP i kopia do Wysłanych (na atrapach)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc

from perno.db.replies_schema import odp_watki, odp_wiadomosci
from perno.db.replies_store import ReplyStore
from perno.db.schema import maile
from perno.domain import replies as rp
from perno.domain import reply_compose as rc
from perno.services import replies_send as send
from perno.services import replies_sync
from perno.services.sending import Mailer, Sender
from tests.fake_imap import FakeAccounts, FakeImap
from tests.fixtures import FakeClock, FakeSmtp, make_db, put, sample_row

T0 = datetime(2026, 10, 5, 10, 0)
NOW = T0 + timedelta(days=1)
CONFIG = {"SMTP_HOST": "s", "SMTP_PORT": "465", "SMTP_USER": "roman@z3x.pl", "SMTP_PASSWORD": "p", "SENDER_NAME": "Roman Patlai", "IMAP_HOST": "imap.example.com",
          "SMTP2_HOST": "s", "SMTP2_PORT": "465", "SMTP2_USER": "drugi@z3x.pl", "SMTP2_PASSWORD": "p2"}


def inbound(identity="odp1@mylimo.pl", *, mailbox="roman@z3x.pl", sender="anna@mylimo.pl", text="Proszę o cennik.\n\nPozdrawiam\nAnna", **kw):
    fields = dict(direction="in", kind="human", mailbox=mailbox, identity=identity, message_id=identity, thread_id="eventy-001", company_id="eventy-001",
                  date_at=T0, subject="Re: Rezerwacje limuzyn", from_addr=sender, from_name="Anna Nowak", body_text=text, snippet=text[:60],
                  match_method="header", thread_mailbox="roman@z3x.pl", references="orig1@z3x.pl")
    fields.update(kw)
    return rp.NewMessage(**fields)


class SendCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.db = make_db(self.clock)
        self.store = ReplyStore(self.db)
        self.store.ensure_schema()
        put(self.db, sample_row(1, company="MyLimo", email="anna@mylimo.pl", status="odpowiedzial", sent_at="2026-09-29T13:00:00", subject="Rezerwacje limuzyn"))
        with self.db.engine.begin() as conn:
            conn.execute(sa.update(maile).values(sent_from="roman@z3x.pl", message_id="orig1@z3x.pl"))
        self.store.save_message(inbound(), T0)
        self.smtp = {"roman@z3x.pl": FakeSmtp(), "drugi@z3x.pl": FakeSmtp()}
        self.mailers = [Mailer((lambda box=box: self.smtp[box]), Sender(box, "Roman Patlai")) for box in self.smtp]
        self.sessions = FakeAccounts({"roman@z3x.pl": FakeImap({"INBOX": {}, "Sent": {}}, roles={"Sent": "sent"})})

    def draft(self, body="Dziękuję za wiadomość. Cennik w załączeniu.", **kw):
        return send.save_draft(self.store, kw.pop("config", CONFIG), "eventy-001", body, author=kw.pop("author", "human"), now=NOW, **kw)

    def go(self, **options):
        options.setdefault("imap_factory", self.sessions)
        return send.send_reply(self.store, CONFIG, self.mailers, "eventy-001", send.SendOptions(**options), NOW)

    def thread(self):
        with self.store.engine.connect() as conn:
            return dict(conn.execute(sa.select(odp_watki).where(odp_watki.c.id == "eventy-001")).mappings().one())

    def rows(self, **where):
        with self.store.engine.connect() as conn:
            return [dict(r) for r in conn.execute(sa.select(odp_wiadomosci).where(odp_wiadomosci.c.direction == "out").order_by(odp_wiadomosci.c.id)).mappings()]


class ComposeViewTests(SendCase):
    def view(self, config=CONFIG):
        return send.compose_view(self.store.thread_detail("eventy-001"), config, self.store.get_draft("eventy-001"))

    def test_the_editor_knows_who_writes_to_whom_with_which_subject(self):
        view = self.view()
        self.assertEqual((view["can_reply"], view["mailbox"], view["mailbox_ok"], view["to"], view["subject"], view["draft"]),
                         (True, "roman@z3x.pl", True, "anna@mylimo.pl", "Re: Rezerwacje limuzyn", None))
        self.assertEqual((view["needs_ack"], view["signature"], view["mailbox_problem"]), ([], "Roman Patlai", ""))

    def test_a_mailbox_that_is_no_longer_configured_is_reported_with_a_reason(self):
        view = self.view({k: v for k, v in CONFIG.items() if not k.startswith(("SMTP_", "IMAP"))} | {"SMTP2_HOST": "s", "SMTP2_PORT": "1", "SMTP2_USER": "drugi@z3x.pl", "SMTP2_PASSWORD": "p"})
        self.assertEqual((view["can_reply"], view["mailbox_ok"]), (True, False))
        self.assertIn("roman@z3x.pl", view["mailbox_problem"])

    def test_the_mailbox_follows_the_mailbox_that_received_the_last_reply(self):
        self.store.save_message(inbound("odp2@mylimo.pl", mailbox="drugi@z3x.pl", date_at=T0 + timedelta(hours=1)), T0)
        self.assertEqual(self.view()["mailbox"], "drugi@z3x.pl")

    def test_an_opt_out_or_do_not_contact_requires_a_confirmation(self):
        self.store.update_thread("eventy-001", optout=True)
        self.assertEqual(self.view()["needs_ack"], ["optout"])

    def test_a_thread_without_a_human_reply_cannot_be_answered(self):
        put(self.db, sample_row(2, status="wyslano", sent_at="2026-09-29T13:00:00"))
        self.store.save_message(inbound("auto@x", kind="auto", thread_id="eventy-002", company_id="eventy-002"), T0)
        view = send.compose_view(self.store.thread_detail("eventy-002"), CONFIG, None)
        self.assertEqual((view["can_reply"], view["reason"]), (False, "W tym wątku nie ma odpowiedzi człowieka."))

    def test_an_unusable_sender_address_blocks_replying_instead_of_guessing(self):
        self.store.save_message(inbound("zly@x", sender="nie-adres", text="Hej", date_at=T0 + timedelta(hours=2)), T0)
        view = self.view()
        self.assertEqual(view["can_reply"], False)
        self.assertIn("adres", view["reason"].lower())

    def test_the_draft_is_part_of_the_view(self):
        self.draft("Treść szkicu")
        self.assertEqual(self.view()["draft"], {"id": mock.ANY, "body": "Treść szkicu", "author": "human", "state": "draft", "error": ""})


class DraftTests(SendCase):
    def test_a_draft_is_saved_replaced_in_place_and_belongs_to_one_thread(self):
        first = self.draft("Pierwsza wersja")["id"]
        second = self.draft("Druga wersja")["id"]
        self.assertEqual(first, second)
        rows = self.rows()
        self.assertEqual([(r["state"], r["body_text"], r["author"], r["to_addr"], r["subject"], r["mailbox"]) for r in rows],
                         [("draft", "Druga wersja", "human", "anna@mylimo.pl", "Re: Rezerwacje limuzyn", "roman@z3x.pl")])
        self.assertEqual(self.thread()["last_direction"], "in", "szkic nie rusza wątku")

    def test_ai_may_not_overwrite_a_draft_written_by_a_human_but_may_replace_its_own(self):
        self.draft("Moja wersja", author="human")
        refused = self.draft("Wersja AI", author="ai", keep_human=True)
        self.assertEqual(refused, {"saved": False, "id": None})
        self.assertEqual(self.store.get_draft("eventy-001")["body_text"], "Moja wersja")
        self.store.discard_draft("eventy-001")
        self.assertTrue(self.draft("Wersja AI", author="ai", keep_human=True)["saved"])
        self.assertTrue(self.draft("Poprawka AI", author="ai", keep_human=True)["saved"])
        self.assertEqual(self.store.get_draft("eventy-001")["body_text"], "Poprawka AI")

    def test_a_human_overwrites_an_ai_draft_and_becomes_its_author(self):
        self.draft("Wersja AI", author="ai")
        self.draft("Po mojej edycji", author="human")
        self.assertEqual(self.store.get_draft("eventy-001")["author"], "human")

    def test_a_draft_being_sent_is_not_touched_and_a_failed_one_can_be_edited(self):
        draft_id = self.draft("Do wysłania")["id"]
        self.assertTrue(self.store.claim_draft(draft_id))
        self.assertEqual(self.draft("Zmiana w trakcie"), {"saved": False, "id": None})
        self.store.release_draft(draft_id, "SMTP odmówił")
        self.assertEqual(self.store.get_draft("eventy-001")["state"], "failed")
        self.assertTrue(self.draft("Poprawiona treść")["saved"])
        self.assertEqual((self.store.get_draft("eventy-001")["state"], self.store.get_draft("eventy-001")["error"]), ("draft", None))

    def test_empty_oversized_and_unanswerable_drafts_are_refused(self):
        for body in ("", "   \n", None, 5, "x" * 20_001):
            with self.subTest(body=body), self.assertRaises(rc.ComposeError):
                self.draft(body)
        with self.assertRaises(send.SendRefused):
            send.save_draft(self.store, CONFIG, "nie-ma", "Treść", author="human", now=NOW)

    def test_discarding_removes_the_draft_but_never_a_sent_message(self):
        self.draft("Do wyrzucenia")
        self.assertTrue(self.store.discard_draft("eventy-001"))
        self.assertFalse(self.store.discard_draft("eventy-001"))
        self.assertIsNone(self.store.get_draft("eventy-001"))
        self.draft("Wyślij mnie")
        self.go()
        self.assertFalse(self.store.discard_draft("eventy-001"))
        self.assertEqual([r["state"] for r in self.rows()], ["sent"])


class SendTests(SendCase):
    def test_the_reply_goes_out_from_the_thread_mailbox_to_the_sender_with_thread_headers_and_a_quote(self):
        self.draft("Dziękuję za wiadomość.\nCennik w załączeniu.")
        result = self.go()
        self.assertEqual((result["sent"], result["warning"]), (True, ""))
        self.assertEqual((len(self.smtp["roman@z3x.pl"].sent), len(self.smtp["drugi@z3x.pl"].sent)), (1, 0))
        message = self.smtp["roman@z3x.pl"].sent[0]
        self.assertEqual((message["To"], message["Subject"], message["In-Reply-To"], message["References"]),
                         ("anna@mylimo.pl", "Re: Rezerwacje limuzyn", "<odp1@mylimo.pl>", "<orig1@z3x.pl> <odp1@mylimo.pl>"))
        self.assertIn("roman@z3x.pl", message["From"])
        self.assertRegex(message["Message-ID"], r"^<[^<>\s]+@z3x\.pl>$")
        plain = message.get_body(("plain",)).get_content()
        self.assertTrue(plain.startswith("Dziękuję za wiadomość.\nCennik w załączeniu.\nRoman Patlai\n\nW dniu 05.10.2026 10:00 Anna Nowak <anna@mylimo.pl> napisał(a):"))
        self.assertIn("> Proszę o cennik.", plain)
        self.assertIn("<blockquote>", message.get_body(("html",)).get_content())

    def test_the_thread_state_follows_the_send(self):
        self.draft()
        key = self.go()["message_id"]
        thread = self.thread()
        self.assertEqual((thread["awaiting_reply"], thread["last_direction"], thread["last_snippet"]), (False, "out", "Dziękuję za wiadomość. Cennik w załączeniu."))
        self.assertIsNotNone(thread["last_out_at"])
        sent = self.rows()[0]
        self.assertEqual((sent["state"], sent["message_id"], sent["in_reply_to"], sent["author"], sent["mailbox"]), ("sent", key, "odp1@mylimo.pl", "human", "roman@z3x.pl"))
        self.assertEqual(sent["body_text"], "Dziękuję za wiadomość. Cennik w załączeniu.\nRoman Patlai")
        self.assertIsNone(self.store.get_draft("eventy-001"))

    def test_the_company_status_and_the_campaign_counters_are_untouched(self):
        self.draft()
        self.go()
        row = self.db.get("eventy-001")
        self.assertEqual((row["status"], row["sent_at"]), ("odpowiedzial", "2026-09-29T13:00:00"))
        self.assertEqual(self.db.sent_by_sender_today(NOW), {})

    def test_a_reply_that_arrived_in_the_second_mailbox_goes_out_from_it(self):
        self.store.save_message(inbound("odp2@mylimo.pl", mailbox="drugi@z3x.pl", date_at=T0 + timedelta(hours=1)), T0)
        self.draft()
        self.go()
        self.assertEqual((len(self.smtp["drugi@z3x.pl"].sent), len(self.smtp["roman@z3x.pl"].sent)), (1, 0))
        self.assertEqual(self.smtp["drugi@z3x.pl"].sent[0]["In-Reply-To"], "<odp2@mylimo.pl>")

    def test_a_mailbox_without_smtp_means_nothing_is_sent_and_the_draft_survives(self):
        self.draft()
        without_first = {k: v for k, v in CONFIG.items() if not k.startswith(("SMTP_", "IMAP"))}
        others = [m for m in self.mailers if m.sender.address != "roman@z3x.pl"]
        with self.assertRaisesRegex(send.SendRefused, "nie jest skonfigurowana"):
            send.send_reply(self.store, without_first, others, "eventy-001", send.SendOptions(), NOW)
        with self.assertRaisesRegex(send.SendRefused, "Nie ma skrzynki"):  # konfiguracja ją zna, ale nie ma jej wśród działających skrzynek
            send.send_reply(self.store, CONFIG, others, "eventy-001", send.SendOptions(), NOW)
        self.assertEqual((self.store.get_draft("eventy-001")["state"], len(self.smtp["roman@z3x.pl"].sent)), ("draft", 0))

    def test_the_quote_is_optional(self):
        self.draft()
        self.go(quote=False)
        plain = self.smtp["roman@z3x.pl"].sent[0].get_body(("plain",)).get_content()
        self.assertNotIn("napisał(a):", plain)
        self.assertNotIn("<blockquote>", self.smtp["roman@z3x.pl"].sent[0].get_body(("html",)).get_content())

    def test_nothing_to_send_is_refused(self):
        with self.assertRaisesRegex(send.SendRefused, "Nie ma szkicu"):
            self.go()
        with self.assertRaises(send.SendRefused):
            send.send_reply(self.store, CONFIG, self.mailers, "nie-ma", send.SendOptions(), NOW)

    def test_a_second_send_of_the_same_draft_goes_nowhere(self):
        self.draft()
        self.go()
        with self.assertRaises(send.SendRefused):
            self.go()
        self.assertEqual(len(self.smtp["roman@z3x.pl"].sent), 1)

    def test_a_draft_claimed_by_someone_else_is_not_sent_again(self):
        draft_id = self.draft()["id"]
        self.store.claim_draft(draft_id)
        with self.assertRaisesRegex(send.SendRefused, "Nie ma szkicu|już wysyłana"):
            self.go()
        self.assertEqual(len(self.smtp["roman@z3x.pl"].sent), 0)

    def test_closing_the_thread_with_the_send(self):
        self.draft()
        self.go(close=True)
        self.assertIsNotNone(self.thread()["closed_at"])


class AckTests(SendCase):
    def test_an_opt_out_needs_a_confirmation_and_then_goes_out(self):
        self.store.update_thread("eventy-001", optout=True)
        self.draft()
        with self.assertRaises(send.AckRequired) as raised:
            self.go()
        self.assertEqual(raised.exception.codes, ["optout"])
        self.assertEqual(len(self.smtp["roman@z3x.pl"].sent), 0)
        self.assertEqual(self.store.get_draft("eventy-001")["state"], "draft")
        self.go(acks=("optout",))
        self.assertEqual(len(self.smtp["roman@z3x.pl"].sent), 1)

    def test_do_not_contact_status_needs_the_same_confirmation(self):
        self.db.apply_edit("eventy-001", {"status": "nie_kontaktowac"})
        self.draft()
        with self.assertRaises(send.AckRequired):
            self.go()


class FailureTests(SendCase):
    def test_an_smtp_failure_returns_the_draft_with_the_reason_and_changes_nothing_else(self):
        self.smtp["roman@z3x.pl"].fail_with = RuntimeError("550 odrzucone")
        self.draft()
        with self.assertRaisesRegex(send.SendFailed, "550 odrzucone"):
            self.go()
        draft = self.store.get_draft("eventy-001")
        self.assertEqual((draft["state"], draft["error"]), ("failed", "550 odrzucone"))
        self.assertEqual((self.thread()["awaiting_reply"], self.thread()["last_out_at"]), (True, None))
        self.smtp["roman@z3x.pl"].fail_with = None
        self.go()  # po naprawie ten sam szkic można wysłać
        self.assertEqual(len(self.smtp["roman@z3x.pl"].sent), 1)

    def test_a_database_failure_after_a_successful_send_is_a_loud_warning_not_a_resend(self):
        self.draft()
        with mock.patch.object(self.store, "finish_sent", side_effect=sa_exc.OperationalError("x", {}, Exception("y"))), mock.patch("time.sleep"):
            result = self.go()
        self.assertTrue(result["sent"])
        self.assertIn("WYSŁANO", result["warning"])
        self.assertEqual(len(self.smtp["roman@z3x.pl"].sent), 1)


class SentCopyTests(SendCase):
    def test_a_copy_goes_to_the_sent_folder_of_the_same_mailbox_as_read(self):
        self.draft()
        result = self.go()
        session = self.sessions.sessions["roman@z3x.pl"]
        self.assertEqual(result["warning"], "")
        self.assertEqual(len(session.appended), 1)
        folder, raw, when = session.appended[0]
        self.assertEqual(folder, "Sent")
        self.assertIn(b"Subject: Re: Rezerwacje limuzyn", raw)
        self.assertEqual(self.sessions.opened, ["roman@z3x.pl"])

    def test_the_copy_read_back_from_sent_is_the_same_message_not_a_second_one(self):
        self.draft()
        self.go()
        session = self.sessions.sessions["roman@z3x.pl"]
        session.add("Sent", 1, session.appended[0][1])
        before = len(self.rows())
        replies_sync.fetch_replies(self.db, self.store, CONFIG, self.sessions, options=replies_sync.SyncOptions(only="roman@z3x.pl"), now=NOW, clock=self.clock)
        self.assertEqual(len(self.rows()), before)

    def test_a_failing_copy_only_warns(self):
        self.sessions.sessions["roman@z3x.pl"].fail_with = TimeoutError("timed out")
        self.draft()
        result = self.go()
        self.assertTrue(result["sent"])
        self.assertIn("kopii nie zapisano", result["warning"])
        self.assertEqual(len(self.smtp["roman@z3x.pl"].sent), 1)

    def test_no_copy_is_made_when_it_is_switched_off_or_the_mailbox_has_no_imap_or_no_factory(self):
        self.draft()
        self.go_with = None
        send.send_reply(self.store, {**CONFIG, "REPLIES_SAVE_SENT": "false"}, self.mailers, "eventy-001", send.SendOptions(imap_factory=self.sessions), NOW)
        self.assertEqual(self.sessions.opened, [])
        self.draft("Druga")
        result = send.send_reply(self.store, CONFIG, self.mailers, "eventy-001", send.SendOptions(imap_factory=None), NOW)
        self.assertIn("wyłączony", result["warning"])
        self.draft("Trzecia")
        no_imap = {k: v for k, v in CONFIG.items() if not k.startswith("IMAP")}
        result = send.send_reply(self.store, no_imap, self.mailers, "eventy-001", send.SendOptions(imap_factory=self.sessions), NOW)
        self.assertIn("nie ma ustawionego IMAP", result["warning"])

    def test_a_mailbox_without_a_sent_folder_is_a_warning(self):
        self.sessions.sessions["roman@z3x.pl"] = FakeImap({"INBOX": {}})
        self.draft()
        self.assertIn("nie znaleziono folderu Wysłane", self.go()["warning"])


if __name__ == "__main__":
    unittest.main()
