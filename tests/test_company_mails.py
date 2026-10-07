"""Adresy jednej firmy: dodawanie, usuwanie, wiersz API (`mail_count`, `hold`), bramka wersji demona i trasy panelu."""

from __future__ import annotations

import unittest

import sqlalchemy as sa

from perno import db as dbmod
from perno.db.automat_store import AutomatStore
from perno.domain import automat_rules as ar
from perno.domain import statuses as st
from perno.domain.rowfilter import RowFilter
from perno.services import company_mails
from tests.fixtures import FakeClock, assert_mirror_consistent, make_db, put, sample_row
from tests.test_web import WebCase


def mails_of(database, company_id: str) -> list:
    return [(m["id"], m["position"], m["email"], m["status"]) for m in database.company_mails(company_id)]


class AddMailTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        put(self.database, sample_row(1, email="glowny@firma.pl", country="PL", city="Kraków", status=st.STATUS_SENT,
                                      sent_at="2026-10-01T10:00:00"))

    def test_extra_address_copies_the_text_and_waits_in_the_queue(self):
        row = self.database.add_mail("eventy-001", "Drugi@Firma.pl")
        self.assertEqual((row["id"], row["company_id"], row["position"], row["mail_count"]), ("eventy-001~2", "eventy-001", "2", "2"))
        self.assertEqual((row["email"], row["status"], row["sent_at"]), ("Drugi@Firma.pl", st.STATUS_PENDING, ""))
        self.assertEqual((row["subject"], row["body_html"]), ("Temat 1", "<p>Treść 1</p>"))
        self.assertTrue(row["generated_at"])
        self.assertEqual((row["company"], row["country"], row["city"]), ("Firma 1", "PL", "Kraków"))  # firma wspólna
        assert_mirror_consistent(self, self.database)

    def test_without_copy_the_address_waits_for_its_text(self):
        row = self.database.add_mail("eventy-001", "drugi@firma.pl", copy_text=False)
        self.assertEqual((row["status"], row["subject"], row["body_html"], row["generated_at"]),
                         (st.STATUS_AWAITING_COPY, "", "", ""))

    def test_the_contact_person_is_not_copied(self):
        self.database.apply_edit("eventy-001", {"contact_person": "Anna"})
        row = self.database.add_mail("eventy-001", "drugi@firma.pl")
        self.assertEqual(row["contact_person"], "")

    def test_numbers_and_ids_grow_and_the_main_address_stays_first(self):
        self.database.add_mail("eventy-001", "b@firma.pl")
        self.database.add_mail("eventy-001", "c@firma.pl")
        self.assertEqual([(i, p) for i, p, _, _ in mails_of(self.database, "eventy-001")],
                         [("eventy-001", "1"), ("eventy-001~2", "2"), ("eventy-001~3", "3")])
        self.assertEqual(self.database.get("eventy-001")["mail_count"], "3")

    def test_the_same_address_twice_is_refused_whatever_its_case_or_spaces(self):
        for duplicate in ("glowny@firma.pl", "  GLOWNY@Firma.PL "):
            with self.assertRaisesRegex(dbmod.InvalidValue, "już przypisany"):
                self.database.add_mail("eventy-001", duplicate)
        self.assertEqual(len(self.database.company_mails("eventy-001")), 1)

    def test_the_same_address_in_another_company_is_allowed_and_reported(self):
        put(self.database, sample_row(2, email="info@inna.pl", company="Inna"))
        self.database.add_mail("eventy-002", "glowny@firma.pl")
        self.assertEqual(self.database.companies_with_address(" GLOWNY@firma.pl", "eventy-002"), ["Firma 1"])
        self.assertEqual(self.database.companies_with_address("glowny@firma.pl", "eventy-001"), ["Inna"])
        self.assertEqual(self.database.companies_with_address("nikt@firma.pl", "eventy-001"), [])

    def test_bad_addresses_and_unknown_companies_are_refused(self):
        for bad in ("", "   ", "bez-malpy", "a@b", "a b@c.pl", "x" * 320 + "@a.pl"):
            with self.assertRaises(dbmod.InvalidValue, msg=bad):
                self.database.add_mail("eventy-001", bad)
        with self.assertRaises(dbmod.RowNotFound):
            self.database.add_mail("nie-ma", "a@firma.pl")

    def test_the_first_address_of_a_company_without_one_fills_the_placeholder_row(self):
        put(self.database, sample_row(2, email="", status=st.STATUS_SKIPPED_NO_EMAIL, body_html="", body_text="", subject=""))
        row = self.database.add_mail("eventy-002", "nowy@firma.pl")
        self.assertEqual((row["id"], row["position"], row["mail_count"]), ("eventy-002", "1", "1"))
        self.assertEqual((row["email"], row["status"]), ("nowy@firma.pl", st.STATUS_AWAITING_COPY))
        self.assertEqual(len(self.database.company_mails("eventy-002")), 1)

    def test_a_row_without_address_but_with_another_status_is_not_a_placeholder(self):
        put(self.database, sample_row(2, email="", status=st.STATUS_SKIPPED_CUSTOMER, body_html="", body_text=""))
        row = self.database.add_mail("eventy-002", "nowy@firma.pl")
        self.assertEqual((row["id"], row["position"]), ("eventy-002~2", "2"))

    def test_an_address_added_to_a_company_with_text_only_on_a_sibling_copies_that_text(self):
        put(self.database, sample_row(2, email="", status=st.STATUS_SKIPPED_NO_EMAIL, body_html="", body_text="", subject=""))
        self.database.add_mail("eventy-002", "a@firma.pl", copy_text=False)  # zajmuje wiersz bez adresu
        row = self.database.add_mail("eventy-002", "b@firma.pl")  # nie ma z czego kopiować
        self.assertEqual(row["status"], st.STATUS_AWAITING_COPY)

    def test_siblings_see_company_edits_made_through_any_address(self):
        sibling = self.database.add_mail("eventy-001", "b@firma.pl")
        self.database.apply_edit(sibling["id"], {"country": "Niemcy"})
        self.assertEqual({r["country"] for r in self.database.fetch_all()}, {"DE"})
        assert_mirror_consistent(self, self.database)


class DuplicateGuardTests(unittest.TestCase):
    """Ten sam adres dwa razy w jednej firmie jest odrzucany także przy edycji, uzupełnianiu ze skanu i regeneracji."""

    def setUp(self):
        self.database = make_db()
        put(self.database, sample_row(1, email="glowny@firma.pl"), sample_row(2, email="inna@firma.pl"))
        self.sibling = self.database.add_mail("eventy-001", "drugi@firma.pl")["id"]

    def test_editing_an_address_into_a_duplicate_is_refused_whatever_the_case(self):
        for duplicate in ("glowny@firma.pl", " GLOWNY@Firma.PL "):
            with self.assertRaisesRegex(dbmod.InvalidValue, "już przypisany"):
                self.database.apply_edit(self.sibling, {"email": duplicate})
        self.assertEqual(self.database.get(self.sibling)["email"], "drugi@firma.pl")

    def test_editing_an_address_to_its_own_value_or_another_companys_is_fine(self):
        self.database.apply_edit(self.sibling, {"email": "DRUGI@firma.pl"})  # ten sam adres: nie koliduje sam ze sobą
        self.database.apply_edit(self.sibling, {"email": "inna@firma.pl"})  # adres innej firmy: dozwolone
        self.assertEqual(self.database.get(self.sibling)["email"], "inna@firma.pl")

    def test_clearing_an_address_is_not_a_duplicate(self):
        self.database.apply_edit(self.sibling, {"email": ""})
        self.assertEqual(self.database.get(self.sibling)["email"], "")

    def test_enrichment_skips_a_row_whose_new_address_a_sibling_already_has(self):
        self.database.apply_edit("eventy-001", {"status": st.STATUS_AWAITING_COPY})
        written = self.database.apply_enrichment(
            [("eventy-001", {"email": "DRUGI@firma.pl", "subject": "S", "body_html": "<p>x</p>"})], (st.STATUS_AWAITING_COPY,))
        self.assertEqual(written, 0)
        self.assertEqual(self.database.get("eventy-001")["email"], "glowny@firma.pl")

    def test_regeneration_skips_a_row_whose_seed_address_a_sibling_already_has(self):
        stats = self.database.upsert_generated([sample_row(1, email="drugi@firma.pl", subject="Nowy")])
        self.assertEqual(stats, {"inserted": 0, "updated": 0, "skipped": 1})
        self.assertEqual(self.database.get("eventy-001")["subject"], "Temat 1")


class QueryPlanTests(unittest.TestCase):
    def test_looking_up_a_companys_addresses_uses_an_index_not_a_table_scan(self):
        """`coalesce(company_id, id) = …` przeglądałoby całą tabelę przy każdym dodaniu adresu (zmierzone na 50 tys. firm)."""
        from perno.db.company_sql import in_companies, select_mails
        from perno.db.schema import maile

        database = make_db()
        put(database, sample_row(1))
        statement = select_mails(maile.c.id).where(in_companies(["eventy-001"]))
        with database.engine.connect() as conn:
            compiled = str(statement.compile(dialect=conn.dialect, compile_kwargs={"literal_binds": True}))
            plan = " ".join(row[3] for row in conn.execute(sa.text("EXPLAIN QUERY PLAN " + compiled)))
        self.assertNotIn("coalesce", compiled.lower())
        self.assertNotRegex(plan, r"SCAN maile(?! USING)")  # po indeksach (company_id, id), nie przegląd tabeli


class DeleteMailTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        put(self.database, sample_row(1, status=st.STATUS_SENT, sent_at="2026-10-01T10:00:00"))
        self.b = self.database.add_mail("eventy-001", "b@firma.pl")["id"]
        self.c = self.database.add_mail("eventy-001", "c@firma.pl")["id"]
        self.d = self.database.add_mail("eventy-001", "d@firma.pl")["id"]

    def test_deleting_an_unsent_address_renumbers_the_rest(self):
        result = self.database.delete_mail(self.c)
        self.assertEqual(result["deleted"], self.c)
        self.assertEqual([(m["id"], m["position"]) for m in result["mails"]],
                         [("eventy-001", "1"), (self.b, "2"), (self.d, "3")])
        self.assertEqual(self.database.get("eventy-001")["mail_count"], "3")
        self.assertIsNone(self.database.get(self.c))
        assert_mirror_consistent(self, self.database)

    def test_the_list_says_which_addresses_the_panel_may_delete(self):
        self.database.apply_edit(self.b, {"status": st.STATUS_SENT})
        flags = {m["id"]: m["deletable"] for m in self.database.company_mails("eventy-001")}
        self.assertEqual(flags, {"eventy-001": False, self.b: False, self.c: True, self.d: True})  # kotwica i wysłany zostają

    def test_the_main_address_cannot_be_deleted(self):
        with self.assertRaisesRegex(dbmod.InvalidValue, "adres główny"):
            self.database.delete_mail("eventy-001")
        self.assertEqual(len(self.database.company_mails("eventy-001")), 4)

    def test_an_address_that_was_mailed_stays_in_the_history(self):
        self.database.apply_edit(self.b, {"status": st.STATUS_SENT})
        with self.assertRaisesRegex(dbmod.InvalidValue, "historii"):
            self.database.delete_mail(self.b)

    def test_an_address_being_sent_cannot_be_deleted(self):
        self.database.claim_for_sending(self.b)
        with self.assertRaisesRegex(dbmod.InvalidValue, "wysyłany"):
            self.database.delete_mail(self.b)

    def test_unknown_address_is_not_found(self):
        with self.assertRaises(dbmod.RowNotFound):
            self.database.delete_mail("eventy-001~99")

    def test_a_deleted_number_is_reused_for_the_next_id_only_after_renumbering(self):
        self.database.delete_mail(self.d)
        again = self.database.add_mail("eventy-001", "e@firma.pl")
        self.assertEqual((again["id"], again["position"]), ("eventy-001~4", "4"))  # id rośnie od najwyższego istniejącego


class ComputedFieldsTests(unittest.TestCase):
    def setUp(self):
        self.database = make_db()
        put(self.database, sample_row(1), sample_row(2))
        self.b = self.database.add_mail("eventy-001", "b@firma.pl")["id"]

    def test_a_stop_status_on_one_address_holds_the_other_ones_only(self):
        self.database.apply_edit("eventy-001", {"status": st.STATUS_REPLIED})
        self.assertEqual(self.database.get(self.b)["hold"], st.STATUS_REPLIED)
        self.assertEqual(self.database.get("eventy-001")["hold"], "")  # własny status widać na wierszu
        self.assertEqual(self.database.get("eventy-002")["hold"], "")  # inna firma bez zmian

    def test_every_stop_status_holds(self):
        for status in st.COMPANY_STOPS:
            self.database.apply_edit("eventy-001", {"status": status})
            self.assertEqual(self.database.get(self.b)["hold"], status, status)

    def test_ordinary_statuses_do_not_hold(self):
        for status in (st.STATUS_SENT, st.STATUS_ERROR, st.STATUS_PENDING, st.STATUS_AWAITING_COPY):
            self.database.apply_edit("eventy-001", {"status": status})
            self.assertEqual(self.database.get(self.b)["hold"], "", status)

    def test_the_more_important_stop_is_reported(self):
        self.database.apply_edit("eventy-001", {"status": st.STATUS_SKIPPED_CUSTOMER})
        third = self.database.add_mail("eventy-001", "c@firma.pl")["id"]
        self.database.apply_edit(third, {"status": st.STATUS_REPLIED})
        self.assertEqual(self.database.get(self.b)["hold"], st.STATUS_REPLIED)

    def test_light_rows_and_the_full_list_carry_the_computed_fields(self):
        self.database.apply_edit("eventy-001", {"status": st.STATUS_INTERESTED})
        light = {r["id"]: r for r in self.database.fetch_all(light=True)}
        full = {r["id"]: r for r in self.database.fetch_all()}
        for rows in (light, full):
            self.assertEqual((rows[self.b]["hold"], rows[self.b]["mail_count"], rows[self.b]["position"],
                              rows[self.b]["company_id"]), (st.STATUS_INTERESTED, "2", "2", "eventy-001"))
            self.assertEqual((rows["eventy-002"]["hold"], rows["eventy-002"]["mail_count"]), ("", "1"))
        listed = {r["id"]: r for r in self.database.list_page(RowFilter(), "default", 0, 10)}
        self.assertEqual((listed[self.b]["hold"], listed[self.b]["mail_count"]), (st.STATUS_INTERESTED, "2"))

    def test_a_row_without_a_company_counts_as_the_only_address(self):
        with self.database.engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO maile (id, segment, status, retry_count) VALUES ('eventy-n001', 'eventy', 'do_wyslania', 0)"))
        row = self.database.get("eventy-n001")
        self.assertEqual((row["mail_count"], row["hold"], row["company_id"]), ("1", "", "eventy-n001"))


class DaemonGateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.database = make_db(self.clock)
        put(self.database, sample_row(1))
        self.store = AutomatStore(self.database)
        self.store.ensure_schema()

    def add(self):
        return company_mails.add_address(self.database, self.store, {"company_id": "eventy-001", "email": "b@firma.pl"})

    def test_without_a_daemon_addresses_can_be_added(self):
        result = self.add()
        self.assertEqual((result["row"]["id"], len(result["mails"]), result["warnings"]), ("eventy-001~2", 2, []))

    def test_an_old_daemon_blocks_adding_addresses(self):
        run = self.store.start_run(111)
        self.store.heartbeat(run, {"stan": "wait", "dzis": 0})  # stary demon: bez znacznika wersji
        with self.assertRaisesRegex(company_mails.DaemonOutdated, "starej wersji"):
            self.add()
        self.assertEqual(len(self.database.company_mails("eventy-001")), 1)

    def test_a_daemon_that_has_not_beaten_yet_blocks_too(self):
        self.store.start_run(111)
        with self.assertRaises(company_mails.DaemonOutdated):
            self.add()

    def test_a_current_daemon_does_not_block(self):
        run = self.store.start_run(111)
        self.store.heartbeat(run, {"v": ar.DAEMON_VERSION, "stan": "wait"})
        self.assertEqual(self.add()["row"]["id"], "eventy-001~2")

    def test_an_older_version_number_blocks(self):
        run = self.store.start_run(111)
        self.store.heartbeat(run, {"v": ar.DAEMON_VERSION - 1})
        with self.assertRaises(company_mails.DaemonOutdated):
            self.add()

    def test_a_finished_old_daemon_no_longer_blocks(self):
        run = self.store.start_run(111)
        self.store.finish_run(run)
        self.assertEqual(self.add()["row"]["id"], "eventy-001~2")

    def test_the_same_address_in_another_company_gives_a_warning_not_an_error(self):
        put(self.database, sample_row(2, email="b@firma.pl", company="Inna"))
        self.assertEqual(self.add()["warnings"], ["Ten adres ma też firma: Inna."])

    def test_payload_is_validated(self):
        for payload in ({}, {"company_id": "eventy-001"}, {"email": "a@b.pl"}, {"company_id": 5, "email": "a@b.pl"},
                        {"company_id": "eventy-001", "email": "a@b.pl", "copy_text": "tak"}):
            with self.assertRaises(dbmod.InvalidValue, msg=payload):
                company_mails.add_address(self.database, self.store, payload)

    def test_delete_payload_is_validated(self):
        with self.assertRaises(dbmod.InvalidValue):
            company_mails.delete_address(self.database, {})


class MailRoutesTests(WebCase):
    def setUp(self):
        super().setUp()
        put(self.database, sample_row(1), sample_row(2))

    def test_adding_an_address_returns_the_row_and_the_companys_addresses(self):
        resp = self.call("POST", "/api/mailing/mails", {"company_id": "eventy-001", "email": "b@firma.pl"})
        data = self.data(resp)
        self.assertEqual(resp.status, 200, data)
        self.assertEqual((data["row"]["id"], data["row"]["position"]), ("eventy-001~2", "2"))
        self.assertEqual([m["id"] for m in data["mails"]], ["eventy-001", "eventy-001~2"])
        self.assertEqual(data["warnings"], [])

    def test_a_duplicate_is_400_and_an_unknown_company_404(self):
        self.assertEqual(self.call("POST", "/api/mailing/mails", {"company_id": "eventy-001", "email": "firma@example.com"}).status, 400)
        self.assertEqual(self.call("POST", "/api/mailing/mails", {"company_id": "nie-ma", "email": "a@firma.pl"}).status, 404)
        self.assertEqual(self.call("POST", "/api/mailing/mails", {"company_id": "eventy-001"}).status, 400)

    def test_an_old_daemon_gives_409(self):
        store = self.app.ctx.automat_store.get()
        store.heartbeat(store.start_run(1), {"stan": "wait"})
        resp = self.call("POST", "/api/mailing/mails", {"company_id": "eventy-001", "email": "b@firma.pl"})
        self.assertEqual(resp.status, 409)
        self.assertIn("starej wersji", self.data(resp)["error"])

    def test_editing_an_address_into_a_duplicate_of_a_sibling_is_400(self):
        self.call("POST", "/api/mailing/mails", {"company_id": "eventy-001", "email": "b@firma.pl"})
        resp = self.call("POST", "/api/update-row", {"id": "eventy-001~2", "email": "firma@example.com"})
        self.assertEqual(resp.status, 400)
        self.assertIn("już przypisany", self.data(resp)["error"])
        self.assertEqual(self.database.get("eventy-001~2")["email"], "b@firma.pl")

    def test_deleting_an_address(self):
        self.call("POST", "/api/mailing/mails", {"company_id": "eventy-001", "email": "b@firma.pl"})
        resp = self.call("POST", "/api/mailing/mails/delete", {"id": "eventy-001~2"})
        data = self.data(resp)
        self.assertEqual((resp.status, data["deleted"], [m["id"] for m in data["mails"]]), (200, "eventy-001~2", ["eventy-001"]))
        self.assertEqual(self.call("POST", "/api/mailing/mails/delete", {"id": "eventy-001"}).status, 400)
        self.assertEqual(self.call("POST", "/api/mailing/mails/delete", {"id": "nie-ma"}).status, 404)

    def test_siblings_of_the_selected_addresses_are_all_the_addresses_of_their_companies(self):
        self.call("POST", "/api/mailing/mails", {"company_id": "eventy-001", "email": "b@firma.pl"})
        self.call("POST", "/api/mailing/mails", {"company_id": "eventy-001", "email": "c@firma.pl"})
        for ids in (["eventy-001"], ["eventy-001~2"], ["eventy-001~3", "eventy-001"]):
            data = self.data(self.call("POST", "/api/mailing/siblings", {"ids": ids}))
            self.assertEqual(data, {"ids": ["eventy-001", "eventy-001~2", "eventy-001~3"], "truncated": False}, ids)
        both = self.data(self.call("POST", "/api/mailing/siblings", {"ids": ["eventy-002", "eventy-001~2"]}))
        self.assertEqual(both["ids"], ["eventy-001", "eventy-001~2", "eventy-001~3", "eventy-002"])

    def test_siblings_of_nothing_or_of_unknown_ids_are_empty_and_bad_input_is_400(self):
        for ids in ([], ["nie-ma"]):
            self.assertEqual(self.data(self.call("POST", "/api/mailing/siblings", {"ids": ids}))["ids"], [])
        self.assertEqual(self.data(self.call("POST", "/api/mailing/siblings", {}))["ids"], [])
        self.assertEqual(self.call("POST", "/api/mailing/siblings", {"ids": "eventy-001"}).status, 400)

    def test_the_single_row_comes_with_the_addresses_of_its_company(self):
        self.call("POST", "/api/mailing/mails", {"company_id": "eventy-001", "email": "b@firma.pl"})
        data = self.data(self.call("GET", "/api/maile/row", query={"id": ["eventy-001~2"]}))
        self.assertEqual(data["row"]["id"], "eventy-001~2")
        self.assertEqual([m["id"] for m in data["mails"]], ["eventy-001", "eventy-001~2"])
        self.assertNotIn("body_html", data["mails"][0])


if __name__ == "__main__":
    unittest.main()
