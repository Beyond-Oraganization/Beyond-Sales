"""Reguły firm z wieloma adresami — czysta logika, bez bazy."""

from __future__ import annotations

import unittest
from datetime import date, datetime

from perno.domain import company_rules as rules
from perno.domain import mail_rules
from perno.domain import statuses as st


class EmailKeyTests(unittest.TestCase):
    def test_case_and_surrounding_spaces_do_not_matter(self):
        self.assertEqual(rules.email_key("  Biuro@Firma.PL "), "biuro@firma.pl")

    def test_missing_address_is_an_empty_key(self):
        self.assertEqual(rules.email_key(None), "")
        self.assertEqual(rules.email_key("   "), "")


class EmailProblemTests(unittest.TestCase):
    def test_ordinary_addresses_are_fine(self):
        for address in ("a@b.pl", "imie.nazwisko+tag@firma.co.uk", "x@sub.domena.example.com"):
            self.assertIsNone(rules.email_problem(address), address)

    def test_an_empty_address_asks_for_one(self):
        self.assertEqual(rules.email_problem(""), "Podaj adres e-mail.")

    def test_wrong_shapes_are_refused_with_the_text_shown(self):
        for address in ("bez-malpy", "a@b", "@firma.pl", "a@@firma.pl", "a b@firma.pl", "a@fir ma.pl", "a@b."):
            self.assertIn("nie wygląda na adres e-mail", rules.email_problem(address) or "", address)
        self.assertIn("„bez-malpy”", rules.email_problem("bez-malpy"))

    def test_length_limit_is_the_column_width(self):
        longest = "a" * (rules.MAX_EMAIL_LENGTH - len("@b.pl")) + "@b.pl"
        self.assertEqual(len(longest), 320)
        self.assertIsNone(rules.email_problem(longest))
        self.assertIn("za długi", rules.email_problem("a" + longest))

    def test_a_very_long_wrong_text_is_cut_in_the_message(self):
        message = rules.email_problem("x" * 100)
        self.assertIn("x" * 60 + "”", message)
        self.assertNotIn("x" * 61, message)


class HoldTests(unittest.TestCase):
    def test_every_company_stop_status_holds_the_siblings(self):
        for status in st.COMPANY_STOPS:
            self.assertEqual(rules.hold_reason(["wyslano", status]), status)

    def test_ordinary_statuses_do_not_hold(self):
        self.assertIsNone(rules.hold_reason([st.STATUS_SENT, st.STATUS_PENDING, st.STATUS_ERROR, st.STATUS_SENDING,
                                             st.STATUS_AWAITING_COPY, st.STATUS_SKIPPED_NO_EMAIL]))
        self.assertIsNone(rules.hold_reason([]))

    def test_the_stop_set_is_exactly_the_four_company_level_statuses(self):
        self.assertEqual(set(st.COMPANY_STOPS), {"odpowiedzial", "zainteresowany", "nie_kontaktowac", "pominieto_klient"})

    def test_the_most_important_status_is_reported_when_several_are_present(self):
        self.assertEqual(rules.hold_reason([st.STATUS_SKIPPED_CUSTOMER, st.STATUS_REPLIED]), st.STATUS_REPLIED)

    def test_message_names_the_blocking_status(self):
        self.assertIn("Odpowiedział", rules.hold_message(st.STATUS_REPLIED))
        self.assertIn("Nie kontaktować", rules.hold_message(st.STATUS_DO_NOT_CONTACT))

    def test_send_is_refused_while_a_sibling_holds_the_company(self):
        row = {"status": st.STATUS_PENDING, "email": "a@example.com", "body_html": "<p>x</p>", "hold": st.STATUS_REPLIED}
        self.assertIn("Odpowiedział", mail_rules.sendability_problem(row))

    def test_send_is_allowed_without_a_hold(self):
        base = {"status": st.STATUS_PENDING, "email": "a@example.com", "body_html": "<p>x</p>"}
        self.assertIsNone(mail_rules.sendability_problem(base))
        self.assertIsNone(mail_rules.sendability_problem({**base, "hold": ""}))

    def test_the_rows_own_problems_come_before_the_hold(self):
        row = {"status": st.STATUS_SENT, "email": "a@example.com", "body_html": "<p>x</p>", "hold": st.STATUS_REPLIED}
        self.assertEqual(mail_rules.sendability_problem(row), "Ten mail już został wysłany.")
        missing = {"status": st.STATUS_PENDING, "email": "", "body_html": "", "hold": st.STATUS_REPLIED}
        self.assertIn("Brak e-maila", mail_rules.sendability_problem(missing))


class MailIdTests(unittest.TestCase):
    def test_first_extra_mail_gets_number_two(self):
        self.assertEqual(rules.next_mail_id("eventy-001", ["eventy-001"]), "eventy-001~2")

    def test_number_follows_the_highest_existing_one(self):
        ids = ["eventy-001", "eventy-001~2", "eventy-001~5"]
        self.assertEqual(rules.next_mail_id("eventy-001", ids), "eventy-001~6")

    def test_ids_of_other_companies_are_ignored(self):
        ids = ["eventy-001", "eventy-0010~9", "eventy-002~7", "eventy-001~x"]
        self.assertEqual(rules.next_mail_id("eventy-001", ids), "eventy-001~2")

    def test_too_long_company_id_is_refused(self):
        with self.assertRaises(ValueError):
            rules.next_mail_id("x" * 31, ["x" * 31])

    def test_an_id_that_just_fits_the_column_is_allowed(self):
        self.assertEqual(rules.next_mail_id("x" * 30, ["x" * 30]), "x" * 30 + "~2")


class GapTests(unittest.TestCase):
    TODAY = date(2026, 10, 6)

    def test_no_gap_means_no_cutoff(self):
        self.assertIsNone(rules.gap_cutoff(self.TODAY, 0))

    def test_gap_of_one_day_is_the_start_of_today(self):
        self.assertEqual(rules.gap_cutoff(self.TODAY, 1), datetime(2026, 10, 6))

    def test_longer_gap_reaches_back_by_the_extra_days(self):
        self.assertEqual(rules.gap_cutoff(self.TODAY, 3), datetime(2026, 10, 4))


class DeleteTests(unittest.TestCase):
    def test_extra_unsent_address_may_be_deleted(self):
        self.assertIsNone(rules.deletable_problem({"status": st.STATUS_PENDING, "sent_at": None}, anchor=False))

    def test_the_main_address_may_not_be_deleted(self):
        self.assertIn("adres główny", rules.deletable_problem({"status": st.STATUS_PENDING, "sent_at": None}, anchor=True))

    def test_address_being_sent_may_not_be_deleted(self):
        self.assertIn("wysyłany", rules.deletable_problem({"status": st.STATUS_SENDING, "sent_at": None}, anchor=False))

    def test_address_that_was_mailed_stays_in_the_history(self):
        for row in ({"status": st.STATUS_SENT, "sent_at": None},
                    {"status": st.STATUS_REPLIED, "sent_at": None},
                    {"status": st.STATUS_ERROR, "sent_at": datetime(2026, 10, 1)}):
            self.assertIn("historii", rules.deletable_problem(row, anchor=False), row)
