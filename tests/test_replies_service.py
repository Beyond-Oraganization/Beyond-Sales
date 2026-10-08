"""Lista, widok wątku, ocena i stan wątku: zapytania i usługa na sqlite (wątki zakładane bezpośrednio w magazynie)."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta

import sqlalchemy as sa

from perno.db.replies_schema import odp_oceny, odp_watki, odp_wiadomosci
from perno.db.replies_store import Cursor, ReplyStore
from perno.db.schema import maile
from perno.domain import replies as rp
from perno.domain import reply_listing as rl
from perno.domain import reply_rules as rr
from perno.services import replies as svc
from tests.fixtures import FakeClock, make_db, put, sample_row

T0 = datetime(2026, 10, 5, 10, 0)
BOX1, BOX2 = "roman@z3x.pl", "drugi@z3x.pl"


def msg(identity, thread, *, direction="in", kind="human", when=T0, mailbox=BOX1, text="Treść", subject="Re: Temat", sender="x@firma.pl",
        name="Anna", **kw):
    fields = dict(direction=direction, kind=kind, mailbox=mailbox, identity=identity, message_id=identity, thread_id=thread, company_id=thread,
                  date_at=when, subject=subject, from_addr=sender, from_name=name, body_text=text, snippet=text[:60], match_method="address",
                  thread_mailbox=mailbox)
    fields.update(kw)
    return rp.NewMessage(**fields)


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(T0 + timedelta(days=5))
        self.db = make_db(self.clock)
        self.store = ReplyStore(self.db)
        self.store.ensure_schema()
        put(self.db,
            sample_row(1, company="MyLimo", country="PL", email="anna@mylimo.pl", status="odpowiedzial", sent_at="2026-09-29T13:00:00", subject="Rezerwacje limuzyn"),
            sample_row(2, company="SBE", country="DE", status="wyslano", sent_at="2026-09-29T13:00:00"),
            sample_row(3, company="Trzecia", country="PL", status="odpowiedzial", sent_at="2026-09-29T13:00:00"),
            sample_row(4, company="Czwarta", country="PL", status="wyslano", sent_at="2026-09-29T13:00:00"),
            sample_row(5, company="Piąta", country="PL", status="wyslano", sent_at="2026-09-29T13:00:00"),
            sample_row(6, company="Szósta", country="GB", status="odpowiedzial", sent_at="2026-09-29T13:00:00"),
            sample_row(1, segment="driverzy", company="Szkoła Jazdy", country="PL", status="wyslano", sent_at="2026-09-29T13:00:00"))
        with self.db.engine.begin() as conn:
            conn.execute(sa.update(maile).where(maile.c.id == "eventy-002").values(sent_from=BOX2))
            conn.execute(sa.update(maile).where(maile.c.id != "eventy-002").values(sent_from=BOX1))
        s = self.store
        s.save_message(msg("t1a", "eventy-001", text="Proszę o cennik dla naszej firmy.", when=T0), T0)
        s.save_message(msg("t2a", "eventy-002", mailbox=BOX2, text="Dziękujemy, wrócimy do tematu.", when=T0 + timedelta(days=1)), T0)
        s.save_message(msg("t3a", "eventy-003", text="Zainteresowani, proszę o kontakt.", when=T0 + timedelta(days=2)), T0)
        s.save_message(msg("t4a", "eventy-004", kind="bounce", sender="mailer-daemon@mx", bounce_type="hard", when=T0 + timedelta(days=2)), T0)
        s.save_message(msg("t5a", "eventy-005", kind="auto", sender="biuro@piata.pl", text="Poza biurem.", when=T0 + timedelta(days=2)), T0)
        s.save_message(msg("t6a", "eventy-006", text="Nie życzymy sobie wiadomości, wypisać nas.", when=T0 + timedelta(days=3), optout_hint=True), T0)
        s.save_message(msg("d1a", "driverzy-001", text="Pytanie o płatności kartą.", when=T0 + timedelta(days=4)), T0)
        s.save_message(msg("u1", None, text="Kto to pisze?", subject="Re: Rezerwacje limuzyn", sender="nieznany@trzecia.pl", name="Obcy", when=T0 + timedelta(days=4)), T0)
        s.set_rating("eventy-002", "neutral", rp.BY_HUMAN, "", T0)
        s.update_thread("eventy-002", read_at=T0, closed_at=None)
        with self.db.engine.begin() as conn:
            conn.execute(sa.update(odp_watki).where(odp_watki.c.id == "eventy-002").values(awaiting_reply=False))
        s.set_rating("eventy-003", "positive", rp.BY_AI, "pyta o kontakt", T0)
        s.set_rating("eventy-006", "negative", rp.BY_HUMAN, "", T0)

    def ids(self, **payload):
        return [row["id"] for row in svc.list_replies(self.store, payload)["rows"]]


class TabTests(ServiceCase):
    def test_the_todo_tab_lists_threads_waiting_for_us_newest_first(self):
        self.assertEqual(self.ids(tab="todo"), ["driverzy-001", "eventy-006", "eventy-003", "eventy-001"])

    def test_all_shows_every_human_conversation_and_not_machine_only_threads(self):
        self.assertEqual(self.ids(tab="all"), ["driverzy-001", "eventy-006", "eventy-003", "eventy-002", "eventy-001"])

    def test_rating_tabs_and_unrated(self):
        self.assertEqual(self.ids(tab="positive"), ["eventy-003"])
        self.assertEqual(self.ids(tab="neutral"), ["eventy-002"])
        self.assertEqual(self.ids(tab="negative"), ["eventy-006"])
        self.assertEqual(self.ids(tab="unrated"), ["driverzy-001", "eventy-001"])

    def test_machine_messages_live_in_their_own_tab(self):
        self.assertEqual(self.ids(tab="machine"), ["eventy-004", "eventy-005"])  # ta sama chwila: kolejność po id

    def test_unassigned_messages_are_a_separate_list_of_messages_not_threads(self):
        result = svc.list_replies(self.store, {"tab": "unassigned"})
        self.assertEqual(result["total"], 1)
        row = result["rows"][0]
        self.assertEqual((row["unassigned"], row["from_addr"], row["subject"], row["snippet"]), (True, "nieznany@trzecia.pl", "Re: Rezerwacje limuzyn", "Kto to pisze?"))

    def test_the_counts_cover_every_tab_at_once(self):
        counts = svc.list_replies(self.store, {})["counts"]
        self.assertEqual(counts, {"todo": 4, "all": 5, "positive": 1, "neutral": 1, "negative": 1, "unrated": 2, "machine": 2, "unassigned": 1})

    def test_a_closed_thread_leaves_the_todo_tab_but_stays_in_all(self):
        svc.set_state(self.store, "eventy-001", "close", self.clock())
        self.assertNotIn("eventy-001", self.ids(tab="todo"))
        self.assertIn("eventy-001", self.ids(tab="all"))
        svc.set_state(self.store, "eventy-001", "reopen", self.clock())
        self.assertIn("eventy-001", self.ids(tab="todo"))


class FilterTests(ServiceCase):
    def test_mailbox_filter_is_case_insensitive_and_can_take_several(self):
        self.assertEqual(self.ids(tab="all", mailboxes=["DRUGI@z3x.pl"]), ["eventy-002"])
        self.assertEqual(sorted(self.ids(tab="all", mailboxes=[BOX1, BOX2])), sorted(self.ids(tab="all")))

    def test_segment_and_country_filters(self):
        self.assertEqual(self.ids(tab="all", segments=["driverzy"]), ["driverzy-001"])
        self.assertEqual(self.ids(tab="all", countries=["DE"]), ["eventy-002"])
        self.assertEqual(self.ids(tab="all", countries=["DE", "GB"]), ["eventy-006", "eventy-002"])

    def test_the_period_filters_the_last_activity_day_inclusively(self):
        self.assertEqual(self.ids(tab="all", **{"from": "2026-10-07", "to": "2026-10-08"}), ["eventy-006", "eventy-003"])
        self.assertEqual(self.ids(tab="all", **{"from": "2026-10-09"}), ["driverzy-001"])
        self.assertEqual(self.ids(tab="all", **{"from": "2026-10-10"}), [])
        self.assertEqual(self.ids(tab="all", **{"to": "2026-10-05"}), ["eventy-001"])
        self.assertEqual(self.ids(tab="all", **{"from": "2026-10-06", "to": "2026-10-06"}), ["eventy-002"])

    def test_search_matches_company_snippet_address_and_message_text_with_all_words_required(self):
        self.assertEqual(self.ids(tab="all", q="mylimo"), ["eventy-001"])
        self.assertEqual(self.ids(tab="all", q="cennik"), ["eventy-001"])
        self.assertEqual(self.ids(tab="all", q="kartą płatności"), ["driverzy-001"])
        self.assertEqual(self.ids(tab="all", q="cennik mylimo"), ["eventy-001"])
        self.assertEqual(self.ids(tab="all", q="cennik kartą"), [])
        self.assertEqual(self.ids(tab="all", q="anna@mylimo.pl"), ["eventy-001"])

    def test_percent_and_underscore_in_a_search_are_literal(self):
        self.assertEqual(self.ids(tab="all", q="%"), [])
        self.assertEqual(self.ids(tab="all", q="_"), [])

    def test_filters_narrow_the_counts_too_but_not_by_tab(self):
        counts = svc.list_replies(self.store, {"tab": "positive", "countries": ["PL"]})["counts"]
        self.assertEqual((counts["all"], counts["positive"], counts["neutral"], counts["unassigned"]), (3, 1, 0, 1))

    def test_pagination(self):
        first = svc.list_replies(self.store, {"tab": "all", "size": 2, "page": 1})
        last = svc.list_replies(self.store, {"tab": "all", "size": 2, "page": 3})
        self.assertEqual((first["total"], first["pages"], [r["id"] for r in first["rows"]], [r["id"] for r in last["rows"]]),
                         (5, 3, ["driverzy-001", "eventy-006"], ["eventy-001"]))
        self.assertEqual(svc.list_replies(self.store, {"tab": "all", "page": 9})["rows"], [])

    def test_rows_carry_what_the_list_needs(self):
        row = svc.list_replies(self.store, {"tab": "all"})["rows"][-1]
        self.assertEqual((row["id"], row["company"], row["segment"], row["country"], row["mailbox"], row["unread"], row["awaiting_reply"], row["rating"]),
                         ("eventy-001", "MyLimo", "eventy", "PL", BOX1, True, True, None))
        self.assertEqual((row["last_snippet"], row["last_from"], row["last_direction"], row["mail_status"], row["campaign_subject"]),
                         ("Proszę o cennik dla naszej firmy.", "Anna", "in", "odpowiedzial", "Rezerwacje limuzyn"))
        self.assertEqual(row["last_activity_at"], "2026-10-05T10:00:00")

    def test_bad_requests_are_errors(self):
        with self.assertRaises(rl.ReplyRequestError):
            svc.list_replies(self.store, {"page": 0})


class ThreadViewTests(ServiceCase):
    def test_the_view_has_the_campaign_mail_the_messages_and_the_suggestion(self):
        view = svc.thread_view(self.store, "eventy-003")
        self.assertEqual((view["thread"]["company"], view["origin"]["subject"], view["origin"]["status"], view["origin"]["sent_from"]),
                         ("Trzecia", "Temat 3", "odpowiedzial", BOX1))
        self.assertEqual(view["suggestion"], "zainteresowany")  # ocena pozytywna + status „odpowiedział”
        self.assertEqual([m["new"] for m in view["messages"]], ["Zainteresowani, proszę o kontakt."])
        self.assertEqual([s["code"] for s in view["statuses"]], ["odpowiedzial", "zainteresowany", "nie_kontaktowac"])

    def test_an_opt_out_suggests_do_not_contact(self):
        self.assertEqual(svc.thread_view(self.store, "eventy-006")["suggestion"], "nie_kontaktowac")

    def test_the_quote_is_split_from_the_new_text_and_attachments_are_listed(self):
        self.store.save_message(msg("q1", "eventy-001", when=T0 + timedelta(hours=1), text="Nowy tekst.\n\nOn Mon Roman <r@z3x.pl> wrote:\n> nasz mail",
                                    attachments=json.dumps([{"name": "oferta.pdf", "content_type": "application/pdf", "size": 9}])), T0)
        message = svc.thread_view(self.store, "eventy-001")["messages"][-1]
        self.assertEqual((message["new"], message["quoted"].endswith("> nasz mail"), message["attachments"][0]["name"]), ("Nowy tekst.", True, "oferta.pdf"))
        self.assertEqual(message["date"], "2026-10-05T11:00:00")

    def test_an_unsent_draft_belongs_to_the_composer_and_is_not_listed_as_a_message(self):
        draft = dict(body="Dziękuję, wracam z ofertą.", subject="Re: Temat", to_addr="x@firma.pl", mailbox=BOX1, author=rp.BY_HUMAN, now=T0 + timedelta(days=6))
        self.store.save_draft("eventy-001", **draft)
        self.store.save_message(msg("sent1", "eventy-001", direction="out", state="sent", author=rp.BY_HUMAN, when=T0 + timedelta(hours=1), text="Wysłana odpowiedź."), T0)
        view = svc.thread_view(self.store, "eventy-001", {"SMTP_USER": BOX1, "SMTP_HOST": "s", "SMTP_PORT": "465", "SMTP_PASSWORD": "p"})
        self.assertEqual([m["new"] for m in view["messages"]], ["Proszę o cennik dla naszej firmy.", "Wysłana odpowiedź."])
        self.assertEqual(view["messages"][-1]["state"], "sent")
        self.assertEqual(view["compose"]["draft"]["body"], "Dziękuję, wracam z ofertą.")

    def test_a_failed_or_sending_draft_is_not_listed_either(self):
        draft_id = self.store.save_draft("eventy-001", body="Szkic", subject="Re: x", to_addr="x@firma.pl", mailbox=BOX1, author=rp.BY_HUMAN, now=T0 + timedelta(days=6))
        for state in ("failed", "sending"):
            with self.db.engine.begin() as conn:
                conn.execute(sa.update(odp_wiadomosci).where(odp_wiadomosci.c.id == draft_id).values(state=state))
            with self.subTest(state=state):
                self.assertEqual([m["state"] for m in svc.thread_view(self.store, "eventy-001")["messages"]], ["received"])

    def test_broken_attachment_json_is_ignored_not_fatal(self):
        self.store.save_message(msg("b1", "eventy-001", when=T0 + timedelta(hours=2), attachments="to nie json"), T0)
        self.assertEqual(svc.thread_view(self.store, "eventy-001")["messages"][-1]["attachments"], [])

    def test_messages_come_oldest_first_and_the_rating_history_newest_first(self):
        self.store.save_message(msg("late", "eventy-001", when=T0 + timedelta(days=2)), T0)
        self.store.save_message(msg("early", "eventy-001", when=T0 - timedelta(days=2)), T0)
        view = svc.thread_view(self.store, "eventy-001")
        self.assertEqual([m["date"][:10] for m in view["messages"]], ["2026-10-03", "2026-10-05", "2026-10-07"])
        svc.rate(self.store, "eventy-001", "neutral", now=T0)
        svc.rate(self.store, "eventy-001", "positive", note="zmiana zdania", now=T0 + timedelta(minutes=1))
        self.assertEqual([(r["rating"], r["by"]) for r in svc.thread_view(self.store, "eventy-001")["ratings"]], [("positive", "human"), ("neutral", "human")])

    def test_an_unknown_thread_is_not_found(self):
        with self.assertRaises(svc.ThreadNotFound):
            svc.thread_view(self.store, "nie-ma")


class RatingTests(ServiceCase):
    def test_a_human_rating_is_stored_logged_and_gives_the_previous_one_for_undo(self):
        result = svc.rate(self.store, "eventy-001", "positive", note="  pyta o cennik ", now=T0)
        self.assertEqual(result["previous"], {"rating": None, "by": None, "note": ""})
        row = svc.thread_view(self.store, "eventy-001")["thread"]
        self.assertEqual((row["rating"], row["rating_by"], row["rating_note"]), ("positive", "human", "pyta o cennik"))
        undo = svc.rate(self.store, "eventy-001", result["previous"]["rating"], now=T0)
        self.assertEqual(undo["previous"]["rating"], "positive")
        self.assertIsNone(svc.thread_view(self.store, "eventy-001")["thread"]["rating"])

    def test_clearing_a_rating_clears_its_author_note_and_time(self):
        svc.rate(self.store, "eventy-001", "negative", note="n", now=T0)
        svc.rate(self.store, "eventy-001", None, now=T0)
        with self.store.engine.connect() as conn:
            row = conn.execute(sa.select(odp_watki.c.rating, odp_watki.c.rating_by, odp_watki.c.rating_note, odp_watki.c.rated_at)
                               .where(odp_watki.c.id == "eventy-001")).first()
            logged = conn.execute(sa.select(sa.func.count()).select_from(odp_oceny).where(odp_oceny.c.thread_id == "eventy-001")).scalar()
        self.assertEqual(tuple(row), (None, None, None, None))
        self.assertEqual(logged, 2)  # ocena i jej wyczyszczenie są w dzienniku

    def test_ai_fills_a_gap_and_replaces_its_own_rating_but_never_a_humans(self):
        svc.rate(self.store, "eventy-001", "neutral", note="AI: ogólnikowa", by=rp.BY_AI, now=T0)
        svc.rate(self.store, "eventy-001", "positive", note="AI: poprawka", by=rp.BY_AI, now=T0)
        self.assertEqual(svc.thread_view(self.store, "eventy-001")["thread"]["rating_by"], "ai")
        with self.assertRaisesRegex(rr.RuleError, "człowieka"):
            svc.rate(self.store, "eventy-002", "negative", by=rp.BY_AI, now=T0)  # 002 ma ocenę człowieka
        self.assertEqual(svc.thread_view(self.store, "eventy-002")["thread"]["rating"], "neutral")

    def test_a_human_overrides_an_ai_rating_and_becomes_its_author(self):
        svc.rate(self.store, "eventy-003", "neutral", now=T0)  # 003 miał ocenę AI
        self.assertEqual(svc.thread_view(self.store, "eventy-003")["thread"]["rating_by"], "human")

    def test_ai_cannot_clear_a_rating(self):
        with self.assertRaises(rr.RuleError):
            svc.rate(self.store, "eventy-003", None, by=rp.BY_AI, now=T0)

    def test_bad_values_and_unknown_threads(self):
        with self.assertRaises(rr.RuleError):
            svc.rate(self.store, "eventy-001", "super", now=T0)
        with self.assertRaises(rr.RuleError):
            svc.rate(self.store, "eventy-001", "neutral", note=5, now=T0)
        with self.assertRaises(svc.ThreadNotFound):
            svc.rate(self.store, "nie-ma", "neutral", now=T0)
        with self.assertRaises(svc.ThreadNotFound):
            svc.rate(self.store, "nie-ma", "neutral", by=rp.BY_AI, now=T0)


class StateTests(ServiceCase):
    def test_read_unread_and_the_nav_counter(self):
        self.assertEqual(self.store.unread_total(), 4)
        svc.set_state(self.store, "eventy-001", "read", T0)
        self.assertEqual(self.store.unread_total(), 3)
        self.assertFalse(svc.thread_view(self.store, "eventy-001")["thread"]["unread"])
        svc.set_state(self.store, "eventy-001", "unread", T0)
        self.assertEqual(self.store.unread_total(), 4)

    def test_opt_out_can_be_set_and_cleared(self):
        svc.set_state(self.store, "eventy-001", "optout", T0)
        self.assertTrue(svc.thread_view(self.store, "eventy-001")["thread"]["optout"])
        svc.set_state(self.store, "eventy-001", "no_optout", T0)
        self.assertFalse(svc.thread_view(self.store, "eventy-001")["thread"]["optout"])

    def test_unknown_actions_and_threads(self):
        with self.assertRaises(rr.RuleError):
            svc.set_state(self.store, "eventy-001", "usun_wszystko", T0)
        with self.assertRaises(svc.ThreadNotFound):
            svc.set_state(self.store, "nie-ma", "read", T0)


class MailStatusTests(ServiceCase):
    def test_a_status_is_set_through_the_normal_edit_path_and_the_previous_one_comes_back(self):
        result = svc.set_mail_status(self.db, self.store, "eventy-003", "zainteresowany")
        self.assertEqual(result, {"previous": "odpowiedzial", "status": "zainteresowany"})
        self.assertEqual(self.db.get("eventy-003")["status"], "zainteresowany")
        undo = svc.set_mail_status(self.db, self.store, "eventy-003", result["previous"])
        self.assertEqual(self.db.get("eventy-003")["status"], undo["status"])

    def test_other_statuses_and_unknown_threads_are_refused(self):
        with self.assertRaises(rr.RuleError):
            svc.set_mail_status(self.db, self.store, "eventy-003", "do_wyslania")
        with self.assertRaises(svc.ThreadNotFound):
            svc.set_mail_status(self.db, self.store, "eventy-099", "zainteresowany")


class AssignTests(ServiceCase):
    def unassigned_id(self):
        return svc.list_replies(self.store, {"tab": "unassigned"})["rows"][0]["id"]

    def test_an_unassigned_message_joins_a_campaign_mail_and_creates_its_thread(self):
        message_id = self.unassigned_id()
        result = svc.assign(self.store, message_id, "eventy-004", BOX1, self.clock())
        self.assertEqual(result, {"ok": True, "thread": "eventy-004"})
        thread = svc.thread_view(self.store, "eventy-004")
        self.assertEqual((thread["thread"]["human_in_count"], thread["thread"]["mailbox"], thread["messages"][-1]["match_method"]),
                         (1, BOX1, "manual"))
        self.assertEqual(svc.list_replies(self.store, {"tab": "unassigned"})["total"], 0)
        self.assertIn("eventy-004", self.ids(tab="todo"))

    def test_the_thread_mailbox_is_the_one_the_mail_was_sent_from(self):
        put(self.db, sample_row(7, company="Siódma", status="wyslano", sent_at="2026-09-29T13:00:00"))  # bez sent_from
        svc.assign(self.store, self.unassigned_id(), "eventy-007", "Glowna@Z3X.pl", self.clock())
        self.assertEqual(svc.thread_view(self.store, "eventy-007")["thread"]["mailbox"], "glowna@z3x.pl")

    def test_a_message_can_be_assigned_only_once_and_only_to_a_real_mail(self):
        message_id = self.unassigned_id()
        with self.assertRaises(svc.ThreadNotFound):
            svc.assign(self.store, message_id, "eventy-099", BOX1, self.clock())
        svc.assign(self.store, message_id, "eventy-004", BOX1, self.clock())
        with self.assertRaises(svc.ThreadNotFound):
            svc.assign(self.store, message_id, "eventy-005", BOX1, self.clock())
        with self.assertRaises(svc.ThreadNotFound):
            svc.assign(self.store, 999999, "eventy-005", BOX1, self.clock())


PANEL_CONFIG = {"SMTP_HOST": "s", "SMTP_PORT": "465", "SMTP_USER": BOX1, "SMTP_PASSWORD": "p", "IMAP_HOST": "imap.example.com",
                "SMTP2_HOST": "s", "SMTP2_PORT": "465", "SMTP2_USER": BOX2, "SMTP2_PASSWORD": "p2", "IMAP2_HOST": "imap2.example.com",
                "SMTP3_HOST": "s", "SMTP3_PORT": "465", "SMTP3_USER": "trzeci@z3x.pl", "SMTP3_PASSWORD": "p3"}


class PanelStateTests(ServiceCase):
    def state(self, running=False, config=None):
        return svc.panel_state(self.store, config or PANEL_CONFIG, running)

    def test_only_mailboxes_that_cannot_be_read_are_listed_as_problems(self):
        self.store.record_error(BOX2, "Authentication failed", "auth", T0)
        problems = self.state()["mailboxes"]["problems"]
        self.assertEqual(problems, [{"mailbox": BOX2, "kind": "auth", "error": "Authentication failed"},
                                    {"mailbox": "trzeci@z3x.pl", "kind": "no_imap", "error": ""}])
        mailboxes = self.state()["mailboxes"]
        self.assertEqual((mailboxes["configured"], mailboxes["receiving"], mailboxes["names"]), (3, 2, [BOX1, BOX2, "trzeci@z3x.pl"]))

    def test_an_old_error_of_a_mailbox_that_no_longer_receives_is_not_repeated(self):
        self.store.record_error("trzeci@z3x.pl", "Authentication failed", "auth", T0)
        self.assertEqual([p["kind"] for p in self.state()["mailboxes"]["problems"]], ["no_imap"])

    def test_a_healthy_receiving_mailbox_is_not_a_problem(self):
        self.store.save_cursor(BOX1, "INBOX", Cursor(1, 5), T0)
        self.assertNotIn(BOX1, [p["mailbox"] for p in self.state()["mailboxes"]["problems"]])

    def test_a_fetch_is_running_when_this_server_or_the_database_lock_says_so(self):
        self.assertFalse(self.state()["fetch"]["running"])
        self.assertTrue(self.state(running=True)["fetch"]["running"])
        self.assertTrue(self.store.acquire_run("token", T0))
        self.assertTrue(self.state()["fetch"]["running"])

    def test_an_empty_list_has_one_page_and_the_unread_total_matches_the_menu_badge(self):
        listing = svc.list_replies(self.store, {"tab": "positive", "q": "zzzz-nic-takiego"})
        self.assertEqual((listing["total"], listing["pages"]), (0, 1))
        self.assertEqual(self.state()["unread"], self.store.unread_total())

    def test_a_simple_action_reports_ok(self):
        self.assertEqual(svc.set_state(self.store, "eventy-001", "read", T0), {"ok": True, "action": "read"})


if __name__ == "__main__":
    unittest.main()
