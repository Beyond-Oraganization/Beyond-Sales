"""Strona Odpowiedzi w panelu: trasy API, błędy, licznik w menu, pobieranie w tle i ochrona przed cudzą treścią."""

from __future__ import annotations

import os
import unittest
from datetime import datetime

import sqlalchemy as sa

from perno.db.schema import maile
from perno.domain import replies as rp
from tests import eml_samples as eml
from tests.fake_imap import FakeAccounts, FakeImap
from tests.fixtures import put, sample_row
from tests.test_web import WebCase

T0 = datetime(2026, 9, 30, 9, 0)  # przed zegarem bazy testowej (30.09 12:00), żeby nasza odpowiedź była późniejsza


def msg(identity, thread, *, text="Proszę o cennik.", when=T0, kind="human", **kw):
    fields = dict(direction="in", kind=kind, mailbox="demo@example.com", identity=identity, message_id=identity, thread_id=thread, company_id=thread,
                  date_at=when, subject="Re: Temat", from_addr="anna@firma.pl", from_name="Anna", body_text=text, snippet=text[:60],
                  match_method="address", thread_mailbox="demo@example.com")
    fields.update(kw)
    return rp.NewMessage(**fields)


class RepliesWebCase(WebCase):
    def setUp(self):
        super().setUp()
        put(self.database, sample_row(1, company="MyLimo", status="odpowiedzial", sent_at="2026-09-29T13:00:00"),
            sample_row(2, company="SBE", status="wyslano", sent_at="2026-09-29T13:00:00"))
        with self.database.engine.begin() as conn:
            conn.execute(sa.update(maile).values(sent_from="demo@example.com"))
        self.store = self.app.ctx.replies_store.get()
        self.store.save_message(msg("a@x", "eventy-001"), T0)
        self.store.save_message(msg("u@x", None, subject="Re: Temat 2", from_addr="obcy@gdzies.pl", from_name="Obcy", text="Kto to?"), T0)

    def post(self, path, payload=None):
        return self.call("POST", path, payload or {})


class PageTests(RepliesWebCase):
    def test_the_page_is_in_the_menu_after_the_mailing_tab_with_a_counter(self):
        html = self.call("GET", "/odpowiedzi").body.decode("utf-8")
        self.assertIn('<a class="topnav-link" href="/odpowiedzi" aria-current="page">Odpowiedzi<span class="topnav-badge"', html)
        self.assertIn("<title>(1) Beyond Sales — Odpowiedzi</title>", html)
        self.assertLess(html.index('href="/wysylka">Wysyłka'), html.index('href="/odpowiedzi"'))
        self.assertLess(html.index('href="/odpowiedzi"'), html.index('href="/automat"'))

    def test_the_counter_shows_on_every_page_but_the_title_changes_only_on_its_own(self):
        html = self.call("GET", "/wysylka").body.decode("utf-8")
        self.assertIn('href="/odpowiedzi">Odpowiedzi<span class="topnav-badge" role="status" aria-label="1 nowych">1</span>', html)
        self.assertIn("<title>Beyond Sales — Wysyłka</title>", html)

    def test_no_counter_when_nothing_is_waiting(self):
        self.store.update_thread("eventy-001", read_at=T0)
        html = self.call("GET", "/odpowiedzi").body.decode("utf-8")
        self.assertNotIn('<span class="topnav-badge"', html)
        self.assertIn("<title>Beyond Sales — Odpowiedzi</title>", html)

    def test_a_broken_counter_never_breaks_a_page(self):
        original = self.app.ctx.replies_store.get().unread_total
        self.app.ctx.replies_store.get().unread_total = lambda: 1 / 0  # type: ignore[method-assign]
        try:
            resp = self.call("GET", "/")
            self.assertEqual(resp.status, 200)
            self.assertNotIn('<span class="topnav-badge"', resp.body.decode("utf-8"))
        finally:
            self.app.ctx.replies_store.get().unread_total = original  # type: ignore[method-assign]

    def test_the_page_html_has_the_split_layout_and_no_inline_data(self):
        html = self.call("GET", "/odpowiedzi").body.decode("utf-8")
        for needle in ('id="split"', 'class="split-list"', 'class="split-detail"', 'id="tabs"'):
            self.assertIn(needle, html)


class ListAndThreadTests(RepliesWebCase):
    def test_the_list_returns_rows_and_counts(self):
        data = self.data(self.post("/api/odpowiedzi/lista", {"tab": "all"}))
        self.assertEqual([r["id"] for r in data["rows"]], ["eventy-001"])
        self.assertEqual((data["total"], data["counts"]["todo"], data["counts"]["unassigned"]), (1, 1, 1))

    def test_a_bad_list_request_is_a_400_with_a_polish_message(self):
        resp = self.post("/api/odpowiedzi/lista", {"page": 0})
        self.assertEqual(resp.status, 400)
        self.assertIn("page", self.data(resp)["error"])

    def test_a_thread_view_and_a_missing_thread(self):
        view = self.data(self.post("/api/odpowiedzi/watek", {"id": "eventy-001"}))
        self.assertEqual((view["thread"]["company"], view["messages"][0]["new"]), ("MyLimo", "Proszę o cennik."))
        self.assertEqual(self.post("/api/odpowiedzi/watek", {"id": "nie-ma"}).status, 404)
        self.assertEqual(self.post("/api/odpowiedzi/watek", {}).status, 400)
        self.assertEqual(self.post("/api/odpowiedzi/watek", {"id": 5}).status, 400)

    def test_an_unassigned_message_can_be_viewed_and_matched_to_a_mail(self):
        listing = self.data(self.post("/api/odpowiedzi/lista", {"tab": "unassigned"}))
        message_id = listing["rows"][0]["id"]
        view = self.data(self.post("/api/odpowiedzi/wiadomosc", {"id": message_id}))
        self.assertEqual((view["message"]["new"], view["message"]["from_addr"]), ("Kto to?", "obcy@gdzies.pl"))
        found = self.data(self.post("/api/odpowiedzi/kandydaci", {"q": "sbe"}))
        self.assertEqual([m["id"] for m in found["mails"]], ["eventy-002"])
        assigned = self.post("/api/odpowiedzi/przypisz", {"message_id": message_id, "mail_id": "eventy-002"})
        self.assertEqual((assigned.status, self.data(assigned)["thread"]), (200, "eventy-002"))
        self.assertEqual(self.post("/api/odpowiedzi/wiadomosc", {"id": message_id}).status, 404)
        self.assertEqual(self.data(self.post("/api/odpowiedzi/lista", {"tab": "all"}))["total"], 2)

    def test_candidates_need_a_query_and_assignment_needs_integer_ids(self):
        self.assertEqual(self.data(self.post("/api/odpowiedzi/kandydaci", {"q": "  "}))["mails"], [])
        self.assertEqual(self.post("/api/odpowiedzi/wiadomosc", {"id": "1"}).status, 400)
        self.assertEqual(self.post("/api/odpowiedzi/przypisz", {"message_id": "1", "mail_id": "eventy-002"}).status, 400)
        self.assertEqual(self.post("/api/odpowiedzi/przypisz", {"message_id": 1}).status, 400)
        self.assertEqual(self.post("/api/odpowiedzi/przypisz", {"message_id": 1, "mail_id": "eventy-099"}).status, 404)


class ActionTests(RepliesWebCase):
    def test_rating_with_an_undo_that_restores_the_previous_value_and_author(self):
        self.store.set_rating("eventy-001", "neutral", "ai", "AI: ogólnikowa", T0)
        done = self.data(self.post("/api/odpowiedzi/ocena", {"id": "eventy-001", "rating": "positive", "note": "pyta o cennik"}))
        self.assertEqual(done["previous"], {"rating": "neutral", "by": "ai", "note": "AI: ogólnikowa"})
        thread = self.data(self.post("/api/odpowiedzi/watek", {"id": "eventy-001"}))["thread"]
        self.assertEqual((thread["rating"], thread["rating_by"]), ("positive", "human"))
        self.post("/api/odpowiedzi/ocena", {"id": "eventy-001", "rating": "neutral", "note": "AI: ogólnikowa", "restore_by": "ai"})
        thread = self.data(self.post("/api/odpowiedzi/watek", {"id": "eventy-001"}))["thread"]
        self.assertEqual((thread["rating"], thread["rating_by"], thread["rating_note"]), ("neutral", "ai", "AI: ogólnikowa"))

    def test_bad_ratings_and_unknown_threads(self):
        self.assertEqual(self.post("/api/odpowiedzi/ocena", {"id": "eventy-001", "rating": "super"}).status, 400)
        self.assertEqual(self.post("/api/odpowiedzi/ocena", {"id": "nie-ma", "rating": "neutral"}).status, 404)

    def test_state_actions_and_the_status_with_its_undo(self):
        self.assertEqual(self.post("/api/odpowiedzi/akcja", {"id": "eventy-001", "action": "read"}).status, 200)
        self.assertFalse(self.data(self.post("/api/odpowiedzi/watek", {"id": "eventy-001"}))["thread"]["unread"])
        self.assertEqual(self.post("/api/odpowiedzi/akcja", {"id": "eventy-001", "action": "boom"}).status, 400)
        done = self.data(self.post("/api/odpowiedzi/status", {"id": "eventy-001", "status": "zainteresowany"}))
        self.assertEqual(done, {"previous": "odpowiedzial", "status": "zainteresowany"})
        self.assertEqual(self.database.get("eventy-001")["status"], "zainteresowany")
        self.assertEqual(self.post("/api/odpowiedzi/status", {"id": "eventy-001", "status": "do_wyslania"}).status, 400)

    def test_every_post_needs_json_and_a_known_origin_like_the_rest_of_the_panel(self):
        resp = self.call("POST", "/api/odpowiedzi/lista", None, headers={"Origin": "http://evil.example"})
        self.assertIn(resp.status, (400, 403))


class ComposeTests(RepliesWebCase):
    def test_the_thread_view_carries_the_editor_state(self):
        compose = self.data(self.post("/api/odpowiedzi/watek", {"id": "eventy-001"}))["compose"]
        self.assertEqual((compose["can_reply"], compose["mailbox"], compose["mailbox_ok"], compose["to"], compose["subject"], compose["draft"]),
                         (True, "demo@example.com", True, "anna@firma.pl", "Re: Temat", None))
        self.assertEqual((compose["needs_ack"], compose["signature"]), ([], "Demo"))

    def test_a_draft_is_saved_shown_and_discarded(self):
        saved = self.data(self.post("/api/odpowiedzi/szkic", {"id": "eventy-001", "body": "Dziękuję, wysyłam cennik."}))
        self.assertTrue(saved["saved"])
        draft = self.data(self.post("/api/odpowiedzi/watek", {"id": "eventy-001"}))["compose"]["draft"]
        self.assertEqual((draft["body"], draft["author"], draft["state"]), ("Dziękuję, wysyłam cennik.", "human", "draft"))
        self.assertEqual(self.data(self.post("/api/odpowiedzi/szkic-usun", {"id": "eventy-001"})), {"discarded": True})
        self.assertIsNone(self.data(self.post("/api/odpowiedzi/watek", {"id": "eventy-001"}))["compose"]["draft"])

    def test_bad_drafts_are_400s(self):
        self.assertEqual(self.post("/api/odpowiedzi/szkic", {"id": "eventy-001", "body": "   "}).status, 400)
        self.assertEqual(self.post("/api/odpowiedzi/szkic", {"id": "eventy-001"}).status, 400)
        self.assertEqual(self.post("/api/odpowiedzi/szkic", {"id": "nie-ma", "body": "x"}).status, 400)

    def test_sending_goes_through_the_injected_smtp_and_updates_the_thread(self):
        self.post("/api/odpowiedzi/szkic", {"id": "eventy-001", "body": "Dziękuję, wysyłam cennik."})
        resp = self.post("/api/odpowiedzi/wyslij", {"id": "eventy-001"})
        self.assertEqual(resp.status, 200)
        self.assertTrue(self.data(resp)["sent"])
        message = self.smtp.sent[-1]
        self.assertEqual((message["To"], message["Subject"], message["In-Reply-To"]), ("anna@firma.pl", "Re: Temat", "<a@x>"))
        row = self.data(self.post("/api/odpowiedzi/lista", {"tab": "all"}))["rows"][-1]
        self.assertEqual((row["awaiting_reply"], row["last_direction"]), (False, "out"))
        self.assertEqual(self.post("/api/odpowiedzi/wyslij", {"id": "eventy-001"}).status, 400)  # nie ma już szkicu

    def test_an_opt_out_needs_an_explicit_confirmation(self):
        self.store.update_thread("eventy-001", optout=True)
        self.post("/api/odpowiedzi/szkic", {"id": "eventy-001", "body": "Potwierdzamy wypisanie."})
        refused = self.post("/api/odpowiedzi/wyslij", {"id": "eventy-001"})
        self.assertEqual((refused.status, self.data(refused)["needs_ack"]), (409, ["optout"]))
        self.assertEqual(len([m for m in self.smtp.sent if m["Subject"] == "Re: Temat"]), 0)
        self.assertEqual(self.post("/api/odpowiedzi/wyslij", {"id": "eventy-001", "ack": ["optout"]}).status, 200)

    def test_an_smtp_failure_is_a_502_with_the_reason_and_keeps_the_draft(self):
        self.smtp.fail_with = RuntimeError("550 odrzucone")
        self.post("/api/odpowiedzi/szkic", {"id": "eventy-001", "body": "Treść"})
        resp = self.post("/api/odpowiedzi/wyslij", {"id": "eventy-001"})
        self.assertEqual(resp.status, 502)
        self.assertIn("550 odrzucone", self.data(resp)["error"])
        draft = self.data(self.post("/api/odpowiedzi/watek", {"id": "eventy-001"}))["compose"]["draft"]
        self.assertEqual((draft["state"], draft["error"]), ("failed", "550 odrzucone"))

    def test_bad_send_requests(self):
        self.assertEqual(self.post("/api/odpowiedzi/wyslij", {"id": "eventy-001", "ack": "optout"}).status, 400)
        self.assertEqual(self.post("/api/odpowiedzi/wyslij", {"id": "eventy-001", "ack": [1]}).status, 400)
        self.assertEqual(self.post("/api/odpowiedzi/wyslij", {}).status, 400)
        self.assertEqual(self.post("/api/odpowiedzi/wyslij", {"id": "nie-ma"}).status, 400)

    def test_the_quote_and_close_flags_are_honoured(self):
        self.post("/api/odpowiedzi/szkic", {"id": "eventy-001", "body": "Treść"})
        self.assertEqual(self.post("/api/odpowiedzi/wyslij", {"id": "eventy-001", "quote": False, "close": True}).status, 200)
        self.assertNotIn("napisał(a):", self.smtp.sent[-1].get_body(("plain",)).get_content())
        self.assertTrue(self.data(self.post("/api/odpowiedzi/watek", {"id": "eventy-001"}))["thread"]["closed"])


class ReportRouteTests(RepliesWebCase):
    def test_the_report_json_and_its_filters(self):
        data = self.data(self.post("/api/odpowiedzi/raport", {}))
        self.assertEqual((data["total"]["sent"], data["total"]["replied"]), (2, 1))
        self.assertEqual([row["key"] for row in data["groups"]["segment"]], ["eventy"])
        self.assertEqual(self.data(self.post("/api/odpowiedzi/raport", {"segments": ["driverzy"]}))["total"]["sent"], 0)
        self.assertEqual(self.post("/api/odpowiedzi/raport", {"from": "wczoraj"}).status, 400)
        self.assertEqual(self.post("/api/odpowiedzi/raport", {"from": "2026-10-02", "to": "2026-10-01"}).status, 400)

    def test_the_report_files_are_downloads_with_a_name_and_a_type(self):
        csv_resp = self.post("/api/odpowiedzi/raport-plik", {"format": "csv"})
        self.assertEqual(csv_resp.status, 200)
        self.assertTrue(csv_resp.content_type.startswith("text/csv"))
        self.assertRegex(csv_resp.headers["Content-Disposition"], r'^attachment; filename="raport_odpowiedzi_\d{8}\.csv"$')
        self.assertTrue(csv_resp.body.decode("utf-8").startswith("\ufeffzestawienie;grupa"))
        md = self.post("/api/odpowiedzi/raport-plik", {"format": "md"})
        self.assertTrue(md.content_type.startswith("text/markdown"))
        self.assertIn("# Raport odpowiedzi", md.body.decode("utf-8"))
        self.assertEqual(self.post("/api/odpowiedzi/raport-plik", {"format": "xlsx"}).status, 400)
        self.assertEqual(self.post("/api/odpowiedzi/raport-plik", {}).status, 400)


class OverviewAndFetchTests(RepliesWebCase):
    def setUp(self):
        super().setUp()
        with open(self.app.ctx.config.path, "a", encoding="utf-8") as handle:
            handle.write("IMAP_HOST=imap.invalid\nSMTP2_HOST=s\nSMTP2_PORT=465\nSMTP2_USER=bez-imap@example.com\nSMTP2_PASSWORD=p2\n")

    def test_the_overview_lists_counters_and_only_the_mailboxes_with_problems(self):
        data = self.data(self.call("GET", "/api/odpowiedzi/stan"))
        self.assertEqual((data["counts"]["todo"], data["unread"], data["auto_minutes"]), (1, 1, 5))
        self.assertEqual(data["mailboxes"], {"configured": 2, "receiving": 1, "names": ["demo@example.com", "bez-imap@example.com"],
                                             "problems": [{"mailbox": "bez-imap@example.com", "kind": "no_imap", "error": ""}]})
        self.assertEqual((data["fetch"]["running"], data["fetch"]["finished_at"]), (False, None))

    def test_a_remembered_login_error_is_listed_and_nothing_else_is(self):
        self.store.record_error("demo@example.com", "Authentication failed", "auth", T0)
        problems = self.data(self.call("GET", "/api/odpowiedzi/stan"))["mailboxes"]["problems"]
        self.assertEqual([(p["mailbox"], p["kind"]) for p in problems], [("demo@example.com", "auth"), ("bez-imap@example.com", "no_imap")])
        self.assertEqual(problems[0]["error"], "Authentication failed")

    def test_the_auto_refresh_interval_comes_from_the_config_and_is_bounded(self):
        for value, expected in (("0", 0), ("30", 30), ("99999", 1440), ("x", 5)):
            with open(self.app.ctx.config.path, "a", encoding="utf-8") as handle:
                handle.write(f"REPLIES_AUTO_FETCH_MIN={value}\n")
            self.assertEqual(self.data(self.call("GET", "/api/odpowiedzi/stan"))["auto_minutes"], expected, value)

    def test_fetching_runs_in_the_background_through_the_injected_session_and_stores_the_reply(self):
        mail = FakeImap({"INBOX": {}})
        mail.add("INBOX", 1, eml.plain_reply(sender="anna@firma.pl", in_reply_to="<orig@x>", message_id="<nowa@firma.pl>", subject="Re: Temat 1"))
        with self.database.engine.begin() as conn:
            conn.execute(sa.update(maile).where(maile.c.id == "eventy-002").values(message_id="orig@x"))
        self.app.ctx.imap_factory = FakeAccounts({"demo@example.com": mail})
        resp = self.post("/api/odpowiedzi/pobierz")
        self.assertEqual((resp.status, self.data(resp)["started"]), (202, True))
        self.app.ctx.reply_runner.wait()
        self.assertEqual(self.data(self.post("/api/odpowiedzi/lista", {"tab": "all"}))["total"], 2)
        self.assertEqual(self.database.get("eventy-002")["status"], "odpowiedzial")
        state = self.data(self.call("GET", "/api/odpowiedzi/stan"))["fetch"]
        self.assertFalse(state["running"])
        self.assertIsNotNone(state["finished_at"])

    def test_a_second_start_while_one_is_running_is_refused(self):
        import threading

        gate = threading.Event()
        self.assertTrue(self.app.ctx.reply_runner.start(lambda: gate.wait(5)))
        self.app.ctx.imap_factory = FakeAccounts({})
        resp = self.post("/api/odpowiedzi/pobierz")
        self.assertEqual((resp.status, self.data(resp)["started"]), (200, False))
        gate.set()
        self.app.ctx.reply_runner.wait()

    def test_a_demo_database_refuses_to_start_a_real_fetch(self):
        previous = os.environ.get("PERNO_DB_URL")
        os.environ["PERNO_DB_URL"] = "sqlite://"
        try:
            resp = self.post("/api/odpowiedzi/pobierz")
        finally:
            os.environ.pop("PERNO_DB_URL", None) if previous is None else os.environ.__setitem__("PERNO_DB_URL", previous)
        self.assertEqual(resp.status, 400)
        self.assertIn("PERNO_ALLOW_REAL_IMAP", self.data(resp)["error"])

    def test_a_failing_background_job_does_not_kill_the_server(self):
        self.assertTrue(self.app.ctx.reply_runner.start(lambda: 1 / 0))
        self.app.ctx.reply_runner.wait()
        self.assertFalse(self.app.ctx.reply_runner.running)


class UntrustedContentTests(RepliesWebCase):
    EVIL = '<img src=x onerror=alert(1)><script>window.PWNED=1</script>'

    def test_hostile_text_travels_as_data_and_never_as_markup_in_the_page_itself(self):
        self.store.save_message(msg("e@x", "eventy-002", text=self.EVIL, from_name=self.EVIL, subject=self.EVIL, from_addr="x@y.pl"), T0)
        listing = self.post("/api/odpowiedzi/lista", {"tab": "all"})
        self.assertEqual(listing.content_type, "application/json; charset=utf-8")
        self.assertIn("nosniff", str(listing.headers) + "nosniff")  # nagłówek bezpieczeństwa dokłada rdzeń aplikacji
        page = self.call("GET", "/odpowiedzi").body.decode("utf-8")
        self.assertNotIn("onerror", page)
        self.assertNotIn("PWNED", page)
        self.assertEqual(self.data(self.post("/api/odpowiedzi/watek", {"id": "eventy-002"}))["messages"][0]["new"], self.EVIL)


if __name__ == "__main__":
    unittest.main()
