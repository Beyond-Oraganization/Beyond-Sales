"""Magazyn leadów na sqlite: kto jest leadem, zasiew, karty i sortowanie listy, edycja z pochodzeniem pól, cofanie i zatwierdzanie."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import sqlalchemy as sa

from perno.db.leads_store import LeadStore
from perno.db.replies_schema import odp_leady
from perno.db.replies_store import ReplyStore
from perno.domain import lead_listing as ll
from perno.domain import lead_rules as lr
from perno.domain import replies as rp
from tests.fixtures import FakeClock, make_db, put, sample_row

T0 = datetime(2026, 10, 8, 10, 0)
LATER = T0 + timedelta(days=1)
MAILBOX = "roman@z3x.pl"


def incoming(thread, *, who="Anna Nowak", addr="anna@mylimo.pl", when=T0, text="Proszę o cennik.", identity=None, **kw):
    fields = dict(direction=rp.DIRECTION_IN, kind=rp.KIND_HUMAN, mailbox=MAILBOX, identity=identity or f"{thread}-{when:%d%H%M}@x", thread_id=thread,
                  company_id=thread.split("~")[0], date_at=when, subject="Re: Temat", from_addr=addr, from_name=who, body_text=text,
                  snippet=text[:60], match_method=rp.MATCH_ADDRESS, thread_mailbox=MAILBOX)
    fields.update(kw)
    return rp.NewMessage(**fields)


class LeadCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.db = make_db(self.clock)
        put(self.db, sample_row(1, company="MyLimo", website="https://mylimo.pl", segment="eventy"),
            sample_row(2, company="SBE", website="https://sbe.pl", segment="eventy"),
            sample_row(3, company="Kancelaria", website="https://kancelaria.pl", segment="kancelarie"),
            sample_row(4, company="Zły Klient", website="https://zly.pl", segment="eventy"),
            sample_row(5, company="Bez Oceny", website="https://bez.pl", segment="eventy"))
        self.replies = ReplyStore(self.db)
        self.replies.ensure_schema()
        self.leads = LeadStore(self.db)
        self.leads.ensure_schema()

    def reply(self, n, rating, *, by=rp.BY_HUMAN, thread=None, **kw):
        thread = thread or f"eventy-{n:03d}"
        self.replies.save_message(incoming(thread, **kw), T0)
        if rating:
            self.replies.set_rating(thread, rating, by, "", T0)
        return thread

    def ids(self, **filters):
        rows, _ = self.leads.list_page(ll.LeadFilter(**filters), 1, 100)
        return [row["id"] for row in rows]

    def stored(self, company_id):
        with self.leads.engine.connect() as conn:
            return dict(conn.execute(sa.select(odp_leady).where(odp_leady.c.company_id == company_id)).mappings().one())


class QualifyingTests(LeadCase):
    def test_only_positive_and_neutral_threads_with_a_human_reply_make_a_lead(self):
        self.reply(1, rp.RATING_POSITIVE)
        self.reply(2, rp.RATING_NEUTRAL, by=rp.BY_AI)
        self.reply(4, rp.RATING_NEGATIVE)
        self.reply(5, None)
        self.replies.save_message(incoming("kancelarie-003", kind=rp.KIND_AUTO, company_id="kancelarie-003"), T0)
        self.assertEqual(self.leads.sync(LATER), 2)
        self.assertEqual(sorted(self.ids()), ["eventy-001", "eventy-002"])
        self.assertEqual(self.leads.sync(LATER), 0)  # drugi zasiew nic nie dokłada

    def test_a_late_rating_creates_the_lead_on_the_next_sync_and_a_negative_one_withdraws_it_keeping_the_data(self):
        thread = self.reply(1, rp.RATING_POSITIVE)
        self.leads.sync(T0)
        self.leads.update_fields("eventy-001", {"about": "Wynajem limuzyn"}, by=lr.BY_HUMAN, now=T0)
        self.replies.set_rating(thread, rp.RATING_NEGATIVE, rp.BY_HUMAN, "", LATER)
        self.assertEqual((self.ids(), self.ids(tab=ll.TAB_WITHDRAWN)), ([], ["eventy-001"]))
        withdrawn = self.leads.get("eventy-001")
        self.assertEqual((withdrawn["active"], withdrawn["about"], withdrawn["rating"]), (False, "Wynajem limuzyn", rp.RATING_NEGATIVE))
        self.replies.set_rating(thread, rp.RATING_NEUTRAL, rp.BY_HUMAN, "", LATER)
        self.assertEqual(self.ids(), ["eventy-001"])
        self.assertEqual(self.leads.get("eventy-001")["about"], "Wynajem limuzyn")

    def test_a_company_with_two_addresses_is_one_lead_with_the_best_rating(self):
        self.db.add_mail("eventy-001", "drugi@mylimo.pl")
        self.reply(1, rp.RATING_NEUTRAL)
        self.reply(1, rp.RATING_POSITIVE, thread="eventy-001~2", addr="szef@mylimo.pl", who="Jan Szef", when=LATER)
        self.assertEqual(self.leads.sync(LATER), 1)
        lead = self.leads.get("eventy-001")
        self.assertEqual((lead["rating"], lead["thread_id"], lead["thread_ids"]), (rp.RATING_POSITIVE, "eventy-001~2", ["eventy-001~2", "eventy-001"]))
        self.assertEqual((lead["email"], lead["contact"]), ("szef@mylimo.pl", "Jan Szef"))  # nadawca najnowszej odpowiedzi


class SeedTests(LeadCase):
    def test_a_new_lead_gets_the_company_and_the_sender_but_no_description(self):
        self.reply(1, rp.RATING_POSITIVE, who="Anna Nowak", addr="Anna@MyLimo.pl")
        self.leads.sync(LATER)
        lead = self.leads.get("eventy-001")
        self.assertEqual((lead["name"], lead["website"], lead["email"], lead["contact"], lead["about"], lead["phone"]),
                         ("MyLimo", "https://mylimo.pl", "anna@mylimo.pl", "Anna Nowak", None, None))
        self.assertEqual((lead["stage"], lead["src"], lead["pending"], lead["missing"], lead["complete"]), ("new", {}, [], ["about"], 4))
        self.assertEqual((lead["segment"], lead["created_at"]), ("eventy", LATER))

    def test_a_sender_name_that_is_not_a_person_is_not_a_contact(self):
        self.reply(1, rp.RATING_POSITIVE, who="MyLimo")
        self.reply(2, rp.RATING_POSITIVE, who="", addr="")
        self.leads.sync(LATER)
        self.assertIsNone(self.leads.get("eventy-001")["contact"])
        self.assertEqual(self.leads.get("eventy-002")["email"], "firma@example.com")  # adres z kolejki, gdy brak nadawcy

    def test_seeding_does_not_change_the_company_or_the_mail_queue(self):
        self.reply(1, rp.RATING_POSITIVE)
        before = self.db.get("eventy-001")
        self.leads.sync(LATER)
        self.leads.update_fields("eventy-001", {"name": "Inna nazwa", "email": "x@y.pl"}, by=lr.BY_HUMAN, now=LATER)
        self.assertEqual(self.db.get("eventy-001"), before)


class ListTests(LeadCase):
    def setUp(self):
        super().setUp()
        self.reply(1, rp.RATING_POSITIVE, when=T0)
        self.reply(2, rp.RATING_NEUTRAL, when=T0 + timedelta(hours=5), who="Bartek Sbe", addr="b@sbe.pl")
        self.reply(3, rp.RATING_POSITIVE, when=T0 + timedelta(hours=9), who="Celina Kowal", addr="c@kancelaria.pl", thread="kancelarie-003")
        self.reply(4, rp.RATING_NEGATIVE)
        self.leads.sync(LATER)
        self.leads.update_fields("eventy-001", {"about": "Wynajem limuzyn", "phone": "+48 600 100 200"}, by=lr.BY_AI, now=LATER)
        self.leads.update_fields("eventy-002", {"about": "Konferencje", "contact": "Bartek Sbe, dyrektor"}, by=lr.BY_HUMAN, now=LATER)

    def test_cards_filter_by_completeness_and_ai_origin(self):
        self.assertEqual(sorted(self.ids()), ["eventy-001", "eventy-002", "kancelarie-003"])
        self.assertEqual(self.ids(tab=ll.TAB_MISSING), ["kancelarie-003"])
        self.assertEqual(self.ids(tab=ll.TAB_AI), ["eventy-001"])
        self.assertEqual(self.leads.tab_counts(ll.LeadFilter()), {"all": 3, "missing": 1, "ai": 1, "withdrawn": 0})

    def test_counts_follow_the_other_filters_but_not_the_card(self):
        self.assertEqual(self.leads.tab_counts(ll.LeadFilter(segments=("eventy",), tab=ll.TAB_MISSING)),
                         {"all": 2, "missing": 0, "ai": 1, "withdrawn": 0})

    def test_sorting_recent_name_rating_and_completeness(self):
        self.assertEqual(self.ids(sort=ll.SORT_RECENT), ["kancelarie-003", "eventy-002", "eventy-001"])
        self.assertEqual(self.ids(sort=ll.SORT_NAME), ["kancelarie-003", "eventy-001", "eventy-002"])
        self.assertEqual(self.ids(sort=ll.SORT_RATING), ["kancelarie-003", "eventy-001", "eventy-002"])
        self.assertEqual(self.ids(sort=ll.SORT_COMPLETE)[0], "kancelarie-003")

    def test_search_covers_name_contact_email_about_and_ignores_like_wildcards(self):
        self.assertEqual(self.ids(words=("limuzyn",)), ["eventy-001"])
        self.assertEqual(self.ids(words=("dyrektor",)), ["eventy-002"])
        self.assertEqual(self.ids(words=("sbe.pl",)), ["eventy-002"])
        self.assertEqual(self.ids(words=("%",)), [])
        self.assertEqual(self.ids(words=("kancelaria", "celina")), ["kancelarie-003"])

    def test_segment_and_stage_filters(self):
        self.assertEqual(sorted(self.ids(segments=("eventy",))), ["eventy-001", "eventy-002"])
        self.leads.update_fields("eventy-002", {"stage": "meeting"}, by=lr.BY_HUMAN, now=LATER)
        self.assertEqual(self.ids(stages=("meeting",)), ["eventy-002"])
        self.assertEqual(self.leads.segments(), ["eventy", "kancelarie"])

    def test_paging_reports_the_total(self):
        rows, total = self.leads.list_page(ll.LeadFilter(sort=ll.SORT_NAME), 2, 2)
        self.assertEqual((len(rows), total), (1, 3))

    def test_warnings_for_optout_and_do_not_contact(self):
        self.db.apply_edit("eventy-002", {"status": "nie_kontaktowac"})
        self.replies.update_thread("eventy-001", optout=True)
        rows = {row["id"]: row for row in self.leads.list_page(ll.LeadFilter(), 1, 10)[0]}
        self.assertEqual((rows["eventy-001"]["optout"], rows["eventy-001"]["do_not_contact"]), (True, False))
        self.assertEqual((rows["eventy-002"]["optout"], rows["eventy-002"]["do_not_contact"]), (False, True))
        self.assertEqual((rows["kancelarie-003"]["optout"], rows["kancelarie-003"]["do_not_contact"]), (False, False))

    def test_the_row_carries_the_representative_thread_and_the_rating_origin(self):
        row = {r["id"]: r for r in self.leads.list_page(ll.LeadFilter(), 1, 10)[0]}["eventy-002"]
        self.assertEqual((row["thread_id"], row["rating"], row["rating_by"], row["snippet"], row["last_from"]),
                         ("eventy-002", rp.RATING_NEUTRAL, rp.BY_HUMAN, "Proszę o cennik.", "Bartek Sbe"))


class EditTests(LeadCase):
    def setUp(self):
        super().setUp()
        self.reply(1, rp.RATING_POSITIVE)
        self.leads.sync(T0)

    def test_the_ai_fills_blank_fields_and_marks_them(self):
        result = self.leads.update_fields("eventy-001", {"about": "Wynajem limuzyn", "phone": "+48 600 100 200"}, by=lr.BY_AI, now=LATER)
        self.assertEqual(result, {"previous": {"about": {"value": None, "src": None}, "phone": {"value": None, "src": None}}, "skipped": []})
        stored = self.stored("eventy-001")
        self.assertEqual((stored["about"], stored["updated_by"], stored["updated_at"], lr.parse_src(stored["src"])),
                         ("Wynajem limuzyn", "ai", LATER, {"about": "ai", "phone": "ai"}))

    def test_the_ai_never_overwrites_a_human_value_and_reports_the_skip(self):
        self.leads.update_fields("eventy-001", {"email": "szef@mylimo.pl"}, by=lr.BY_HUMAN, now=T0)
        result = self.leads.update_fields("eventy-001", {"email": "inny@mylimo.pl", "about": "x"}, by=lr.BY_AI, now=LATER)
        self.assertEqual(result["skipped"], ["email"])
        self.assertEqual(list(result["previous"]), ["about"])
        self.assertEqual(self.stored("eventy-001")["email"], "szef@mylimo.pl")

    def test_the_ai_may_replace_the_seed_and_its_own_earlier_value(self):
        self.leads.update_fields("eventy-001", {"contact": "Anna Nowak, dyrektor"}, by=lr.BY_AI, now=LATER)
        self.leads.update_fields("eventy-001", {"contact": "Anna Nowak, prezes"}, by=lr.BY_AI, now=LATER)
        self.assertEqual(self.stored("eventy-001")["contact"], "Anna Nowak, prezes")

    def test_a_human_edit_wins_and_clearing_a_field_lets_the_ai_fill_it_again(self):
        self.leads.update_fields("eventy-001", {"about": "Ręczny opis"}, by=lr.BY_HUMAN, now=T0)
        self.assertEqual(lr.parse_src(self.stored("eventy-001")["src"]), {"about": "human"})
        self.leads.update_fields("eventy-001", {"about": ""}, by=lr.BY_HUMAN, now=T0)
        stored = self.stored("eventy-001")
        self.assertEqual((stored["about"], stored["src"]), (None, None))
        self.assertEqual(self.leads.update_fields("eventy-001", {"about": "Nowy"}, by=lr.BY_AI, now=LATER)["skipped"], [])

    def test_restoring_returns_both_the_values_and_the_origin(self):
        self.leads.update_fields("eventy-001", {"about": "Z AI"}, by=lr.BY_AI, now=T0)
        result = self.leads.update_fields("eventy-001", {"about": "Ręcznie", "stage": "won"}, by=lr.BY_HUMAN, now=LATER)
        self.assertTrue(self.leads.restore_fields("eventy-001", result["previous"], LATER))
        stored = self.stored("eventy-001")
        self.assertEqual((stored["about"], stored["stage"], lr.parse_src(stored["src"])), ("Z AI", "new", {"about": "ai"}))

    def test_approving_turns_ai_fields_into_human_ones_without_touching_the_value(self):
        self.leads.update_fields("eventy-001", {"about": "Z AI", "phone": "+48 600 100 200"}, by=lr.BY_AI, now=T0)
        result = self.leads.approve("eventy-001", ["about", "email", "about"], LATER)
        self.assertEqual(result["approved"], ["about"])  # e-mail nie był od AI
        self.assertEqual(lr.parse_src(self.stored("eventy-001")["src"]), {"about": "human", "phone": "ai"})
        self.assertEqual(self.stored("eventy-001")["about"], "Z AI")
        self.assertTrue(self.leads.restore_fields("eventy-001", result["previous"], LATER))
        self.assertEqual(lr.parse_src(self.stored("eventy-001")["src"]), {"about": "ai", "phone": "ai"})

    def test_missing_leads_are_reported_not_raised(self):
        self.assertIsNone(self.leads.update_fields("nie-ma", {"about": "x"}, by=lr.BY_HUMAN, now=T0))
        self.assertIsNone(self.leads.approve("nie-ma", ["about"], T0))
        self.assertFalse(self.leads.restore_fields("nie-ma", {}, T0))
        self.assertIsNone(self.leads.get("nie-ma"))


class HelpersTests(LeadCase):
    def test_pending_total_counts_unsynced_firms_without_writing(self):
        self.reply(1, rp.RATING_POSITIVE)
        self.reply(2, rp.RATING_NEUTRAL)
        self.assertEqual(self.leads.pending_total(), 2)
        with self.leads.engine.connect() as conn:
            self.assertEqual(conn.execute(sa.select(sa.func.count()).select_from(odp_leady)).scalar(), 0)
        self.leads.sync(T0)
        self.assertEqual(self.leads.pending_total(), 2)  # obaj mają braki (brak opisu)
        self.leads.update_fields("eventy-001", {"about": "x"}, by=lr.BY_HUMAN, now=T0)
        self.assertEqual(self.leads.pending_total(), 1)

    def test_stats_and_lead_ids_paging(self):
        for n in (1, 2):
            self.reply(n, rp.RATING_POSITIVE)
        self.assertEqual(self.leads.stats()["unsynced"], 2)
        self.leads.sync(T0)
        self.leads.update_fields("eventy-001", {"about": "x"}, by=lr.BY_HUMAN, now=T0)
        self.assertEqual(self.leads.stats(), {"all": 2, "missing": 1, "ai": 0, "withdrawn": 0, "unsynced": 0, "pending": 1})
        self.assertEqual(self.leads.lead_ids(), ["eventy-002"])
        self.assertEqual(self.leads.lead_ids(only_missing=False), ["eventy-001", "eventy-002"])
        self.assertEqual(self.leads.lead_ids("eventy-001", only_missing=False), ["eventy-002"])

    def test_an_entry_id_may_be_the_lead_or_one_of_its_qualifying_threads(self):
        self.db.add_mail("eventy-001", "drugi@mylimo.pl")
        self.reply(1, rp.RATING_POSITIVE, thread="eventy-001~2")
        self.reply(4, rp.RATING_NEGATIVE)
        self.assertIsNone(self.leads.resolve("eventy-001~2"))  # jeszcze bez zasiewu
        self.leads.sync(T0)
        self.assertEqual(self.leads.resolve("eventy-001"), {"company_id": "eventy-001", "active": True})
        self.assertEqual(self.leads.resolve("eventy-001~2"), {"company_id": "eventy-001", "active": True})
        self.assertIsNone(self.leads.resolve("eventy-004"))  # wątek negatywny nie jest leadem
        self.assertIsNone(self.leads.resolve("zzz"))

    def test_evidence_lists_the_human_messages_and_the_addresses_of_the_company(self):
        self.reply(1, rp.RATING_POSITIVE, text="Pozdrawiam, Anna, tel. 600 100 200")
        self.replies.save_message(incoming("eventy-001", kind=rp.KIND_AUTO, identity="auto@x", text="Poza biurem"), T0)
        evidence = self.leads.evidence("eventy-001")
        self.assertEqual([m["body_text"] for m in evidence["messages"]], ["Pozdrawiam, Anna, tel. 600 100 200"])
        self.assertEqual(evidence["addresses"], ["firma@example.com"])

    def test_the_schema_is_idempotent(self):
        self.leads.ensure_schema()
        self.leads.ensure_schema()


if __name__ == "__main__":
    unittest.main()
