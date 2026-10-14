"""Strona Leady w panelu: trasy API, zapis z pochodzeniem pól, cofanie, zatwierdzanie, eksport, błędy i licznik w menu."""

from __future__ import annotations

import unittest
from datetime import datetime

import sqlalchemy as sa

from perno.db.schema import maile
from perno.domain import replies as rp
from tests.fixtures import put, sample_row
from tests.test_replies_web import msg
from tests.test_web import WebCase

T0 = datetime(2026, 9, 30, 9, 0)  # przed zegarem bazy testowej (30.09 12:00)


class LeadsWebCase(WebCase):
    def setUp(self):
        super().setUp()
        put(self.database, sample_row(1, company="MyLimo", website="https://mylimo.pl", status="odpowiedzial", sent_at="2026-09-29T13:00:00"),
            sample_row(2, company="SBE", website="https://sbe.pl", status="odpowiedzial", sent_at="2026-09-29T13:00:00"),
            sample_row(3, company="Zły", website="https://zly.pl", status="odpowiedzial", sent_at="2026-09-29T13:00:00"))
        with self.database.engine.begin() as conn:
            conn.execute(sa.update(maile).values(sent_from="demo@example.com"))
        self.replies = self.app.ctx.replies_store.get()
        self.leads = self.app.ctx.leads_store.get()
        self.thread("eventy-001", rp.RATING_POSITIVE, from_name="Anna Nowak", from_addr="anna@mylimo.pl")
        self.thread("eventy-002", rp.RATING_NEUTRAL, from_name="Bartek", from_addr="bartek@sbe.pl", rating_by=rp.BY_AI)
        self.thread("eventy-003", rp.RATING_NEGATIVE)

    def thread(self, thread_id, rating, *, rating_by=rp.BY_HUMAN, **kw):
        self.replies.save_message(msg(f"{thread_id}@x", thread_id, **kw), T0)
        self.replies.set_rating(thread_id, rating, rating_by, "pyta o cennik", T0)

    def post(self, path, payload=None):
        return self.call("POST", path, payload or {})

    def lead(self, company_id):
        resp = self.post("/api/leady/lead", {"id": company_id})
        self.assertEqual(resp.status, 200, resp.body)
        return self.data(resp)["lead"]


class ListTests(LeadsWebCase):
    def test_the_list_creates_the_missing_leads_and_returns_cards_and_counts(self):
        data = self.data(self.post("/api/leady/lista"))
        self.assertEqual(sorted(row["id"] for row in data["rows"]), ["eventy-001", "eventy-002"])
        self.assertEqual((data["total"], data["page"], data["pages"], data["tab"]), (2, 1, 1, "all"))
        self.assertEqual(data["counts"], {"all": 2, "missing": 2, "ai": 0, "withdrawn": 0})
        row = next(r for r in data["rows"] if r["id"] == "eventy-001")
        self.assertEqual((row["name"], row["website"], row["email"], row["contact"], row["rating"], row["rating_by"], row["stage_label"]),
                         ("MyLimo", "https://mylimo.pl", "anna@mylimo.pl", "Anna Nowak", "positive", "human", "Nowy"))
        self.assertEqual((row["missing"], row["complete"], row["pending"], row["src"], row["active"]), (["about"], 4, [], {}, True))
        self.assertEqual(data["segments"], ["eventy"])

    def test_the_ai_rated_lead_is_marked_so_in_the_row(self):
        data = self.data(self.post("/api/leady/lista", {"q": "bartek"}))
        self.assertEqual([(r["id"], r["rating_by"]) for r in data["rows"]], [("eventy-002", "ai")])

    def test_a_bad_request_is_a_400(self):
        for payload in ({"stages": ["zły"]}, {"page": 0}, {"q": 5}, {"segments": "x"}):
            self.assertEqual(self.post("/api/leady/lista", payload).status, 400, payload)

    def test_the_menu_counter_counts_incomplete_leads_and_the_screen_reader_label_says_so(self):
        html = self.call("GET", "/").body.decode("utf-8")
        self.assertIn('href="/leady">Leady<span class="topnav-badge" role="status" aria-label="2 do uzupełnienia">2</span>', html)
        self.leads.sync(T0)
        for company in ("eventy-001", "eventy-002"):
            self.post("/api/leady/zapisz", {"id": company, "changes": {"about": "Opis"}})
        self.assertNotIn('href="/leady">Leady<span', self.call("GET", "/").body.decode("utf-8"))


class SaveTests(LeadsWebCase):
    def setUp(self):
        super().setUp()
        self.post("/api/leady/lista")

    def test_a_human_saves_fields_and_gets_the_lead_and_the_previous_values_back(self):
        resp = self.post("/api/leady/zapisz", {"id": "eventy-001", "changes": {"about": "Wynajem limuzyn", "phone": "+48 600 100 200", "stage": "talking"}})
        data = self.data(resp)
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual((data["lead"]["about"], data["lead"]["phone"], data["lead"]["stage_label"]), ("Wynajem limuzyn", "+48 600 100 200", "W rozmowie"))
        self.assertEqual(data["lead"]["src"], {"about": "human", "phone": "human"})
        self.assertEqual(data["previous"]["about"], {"value": None, "src": None})
        self.assertEqual(data["previous"]["stage"], {"value": "new", "src": None})

    def test_bad_values_are_400_and_nothing_is_saved(self):
        for changes in ({"email": "nie-adres"}, {"phone": "12"}, {"website": "javascript:x"}, {"stage": "zły"}, {"about": "x" * 401},
                        {"zzz": "x"}, {}, "about", {"about": 5}):
            resp = self.post("/api/leady/zapisz", {"id": "eventy-001", "changes": changes})
            self.assertEqual(resp.status, 400, changes)
        self.assertIsNone(self.lead("eventy-001")["about"])

    def test_an_unknown_lead_is_404_and_the_id_is_required(self):
        self.assertEqual(self.post("/api/leady/zapisz", {"id": "nie-ma", "changes": {"about": "x"}}).status, 404)
        self.assertEqual(self.post("/api/leady/lead", {"id": "nie-ma"}).status, 404)
        self.assertEqual(self.post("/api/leady/zapisz", {"changes": {"about": "x"}}).status, 400)

    def test_undo_restores_values_and_origin(self):
        self.leads.update_fields("eventy-001", {"about": "Z AI"}, by="ai", now=T0)
        saved = self.data(self.post("/api/leady/zapisz", {"id": "eventy-001", "changes": {"about": "Ręcznie"}}))
        resp = self.post("/api/leady/cofnij", {"id": "eventy-001", "previous": saved["previous"]})
        self.assertEqual(resp.status, 200, resp.body)
        lead = self.data(resp)["lead"]
        self.assertEqual((lead["about"], lead["src"], lead["pending"]), ("Z AI", {"about": "ai"}, ["about"]))

    def test_undo_refuses_forged_data(self):
        for previous in ({}, None, {"zzz": {"value": "x"}}, {"about": "x"}, {"about": {"value": "x", "src": "robot"}}, {"email": {"value": "nie-adres"}},
                         {"stage": {"value": "zły"}}):
            self.assertEqual(self.post("/api/leady/cofnij", {"id": "eventy-001", "previous": previous}).status, 400, previous)
        self.assertEqual(self.post("/api/leady/cofnij", {"id": "nie-ma", "previous": {"about": {"value": "x"}}}).status, 404)


class ApproveTests(LeadsWebCase):
    def test_approving_an_ai_field_makes_it_a_human_one_and_can_be_undone(self):
        self.post("/api/leady/lista")
        self.leads.update_fields("eventy-001", {"about": "Z AI", "contact": "Anna Nowak, prezes"}, by="ai", now=T0)
        self.assertEqual(self.lead("eventy-001")["pending"], ["contact", "about"])
        resp = self.post("/api/leady/zatwierdz", {"id": "eventy-001", "fields": ["about"]})
        data = self.data(resp)
        self.assertEqual((data["approved"], data["lead"]["src"], data["lead"]["pending"]), (["about"], {"about": "human", "contact": "ai"}, ["contact"]))
        self.post("/api/leady/cofnij", {"id": "eventy-001", "previous": data["previous"]})
        self.assertEqual(self.lead("eventy-001")["pending"], ["contact", "about"])

    def test_bad_requests_are_400_or_404(self):
        self.post("/api/leady/lista")
        for fields in (None, [], "about", [5], ["zzz"]):
            self.assertEqual(self.post("/api/leady/zatwierdz", {"id": "eventy-001", "fields": fields}).status, 400, fields)
        self.assertEqual(self.post("/api/leady/zatwierdz", {"id": "nie-ma", "fields": ["about"]}).status, 404)


class ViewTests(LeadsWebCase):
    def test_the_lead_view_lists_the_threads_it_came_from(self):
        self.post("/api/leady/lista")
        lead = self.lead("eventy-001")
        self.assertEqual([(t["id"], t["rating"], t["rating_by"], t["rating_note"]) for t in lead["threads"]], [("eventy-001", "positive", "human", "pyta o cennik")])
        self.assertIsNone(lead["base_description"])
        self.assertIn("T", lead["threads"][0]["last_in_at"])  # data jako tekst ISO

    def test_a_withdrawn_lead_keeps_its_data_and_moves_to_its_own_card(self):
        self.post("/api/leady/lista")
        self.post("/api/leady/zapisz", {"id": "eventy-001", "changes": {"about": "Opis"}})
        self.replies.set_rating("eventy-001", rp.RATING_NEGATIVE, rp.BY_HUMAN, "", T0)
        withdrawn = self.data(self.post("/api/leady/lista", {"tab": "withdrawn"}))
        self.assertEqual([(r["id"], r["about"], r["active"], r["rating"]) for r in withdrawn["rows"]], [("eventy-001", "Opis", False, "negative")])
        self.assertEqual(self.data(self.post("/api/leady/lista"))["counts"]["withdrawn"], 1)


class ExportTests(LeadsWebCase):
    def test_the_export_is_a_semicolon_csv_with_a_bom_and_a_dated_file_name(self):
        self.post("/api/leady/lista")
        self.post("/api/leady/zapisz", {"id": "eventy-001", "changes": {"about": "=HYPERLINK(\"http://zly\")", "note": "Zadzwonić"}})
        resp = self.post("/api/leady/eksport", {"sort": "name"})
        text = resp.body.decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn('attachment; filename="leady_20260930.csv"', resp.headers["Content-Disposition"])
        self.assertTrue(text.startswith("﻿Firma;Strona;Kontakt;Telefon;E-mail;Czym się zajmuje;Etap;Ocena;Segment;Notatka;Uzupełnił AI\r\n"))
        self.assertIn("MyLimo;https://mylimo.pl;Anna Nowak;;anna@mylimo.pl;\"'=HYPERLINK(\"\"http://zly\"\")\";Nowy;Pozytywna;eventy;Zadzwonić;", text)
        self.assertEqual(text.count("\r\n"), 3)  # nagłówek i dwa leady

    def test_the_export_follows_the_card_and_filters(self):
        self.post("/api/leady/lista")
        self.assertEqual(self.post("/api/leady/eksport", {"tab": "ai"}).body.decode("utf-8").count("\r\n"), 1)
        self.assertEqual(self.post("/api/leady/eksport", {"q": "bartek"}).body.decode("utf-8").count("\r\n"), 2)
        self.assertEqual(self.post("/api/leady/eksport", {"stages": ["zły"]}).status, 400)


class PageTests(LeadsWebCase):
    def test_the_tab_sits_between_replies_and_the_automat(self):
        html = self.call("GET", "/leady").body.decode("utf-8")
        self.assertLess(html.index('href="/odpowiedzi"'), html.index('href="/leady"'))
        self.assertLess(html.index('href="/leady"'), html.index('href="/automat"'))
        self.assertIn('<a class="topnav-link" href="/leady" aria-current="page">Leady', html)
        self.assertIn("<title>(2) Beyond Sales — Leady</title>", html)

    def test_a_broken_counter_never_breaks_a_page(self):
        self.app.ctx.leads_store.get().pending_total = lambda: 1 / 0  # type: ignore[method-assign]
        resp = self.call("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertNotIn('href="/leady">Leady<span', resp.body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
