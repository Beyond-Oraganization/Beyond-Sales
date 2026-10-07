"""Wybór adresu firmy spośród adresów ze skanu (domain/contact_pick)."""

from __future__ import annotations

import unittest

from perno.domain import contact_pick as cp


def found(email: str, own: bool = True, pages: int = 1, role: str = "inny") -> dict:
    return {"wartosc": email, "w_domenie": own, "rola": role, "liczba_stron": pages}


class PickEmailTests(unittest.TestCase):
    def pick(self, domain: str, *rows: dict):
        return cp.pick_email(list(rows), domain)

    def test_a_generic_mailbox_beats_a_personal_one_even_if_the_person_appears_more_often(self):
        self.assertEqual(self.pick("alfa.pl", found("jan.kowalski@alfa.pl", pages=90), found("biuro@alfa.pl", pages=3)),
                         "biuro@alfa.pl")

    def test_among_generic_mailboxes_the_one_on_more_pages_wins(self):
        self.assertEqual(self.pick("alfa.pl", found("biuro@alfa.pl", pages=1), found("handlowy@alfa.pl", pages=94)),
                         "handlowy@alfa.pl")

    def test_a_mailbox_named_after_the_brand_beats_a_personal_one(self):
        self.assertEqual(self.pick("probit.com.pl", found("a.lesniak@probit.com.pl", pages=16),
                                   found("probit@probit.com.pl", pages=78)), "probit@probit.com.pl")

    def test_the_tiers_rank_generic_then_brand_then_other_whatever_the_page_counts(self):
        self.assertEqual(self.pick("alfa.pl", found("alfa@alfa.pl", pages=50), found("biuro@alfa.pl", pages=1)),
                         "biuro@alfa.pl")
        self.assertEqual(self.pick("alfa.pl", found("jan@alfa.pl", pages=50), found("alfa@alfa.pl", pages=1)),
                         "alfa@alfa.pl")

    def test_a_better_tier_wins_over_a_worse_one_with_the_same_page_count(self):
        self.assertEqual(self.pick("alfa.pl", found("jan@alfa.pl", pages=3), found("biuro@alfa.pl", pages=3)),
                         "biuro@alfa.pl")

    def test_a_brand_mailbox_may_leave_out_the_hyphens_of_the_domain(self):
        self.assertEqual(self.pick("sbm-szkolenia.pl", found("jan@sbm-szkolenia.pl"), found("sbmszkolenia@sbm-szkolenia.pl")),
                         "sbmszkolenia@sbm-szkolenia.pl")

    def test_a_missing_page_count_means_one_page(self):
        row = {"wartosc": "marek@alfa.pl", "w_domenie": True, "rola": "inny"}
        self.assertIsNone(self.pick("alfa.pl", row, found("ewa@alfa.pl", pages=1)))

    def test_specialised_and_automatic_mailboxes_are_never_chosen(self):
        for local in ("iod", "rekrutacja", "praca", "pomoc", "reklamacje", "noreply", "appstore"):
            self.assertIsNone(self.pick("alfa.pl", found(f"{local}@alfa.pl", pages=50)), local)

    def test_a_mailbox_marked_legal_is_skipped(self):
        self.assertIsNone(self.pick("alfa.pl", found("kancelaria@alfa.pl", role="prawny")))

    def test_placeholder_addresses_from_page_templates_are_skipped(self):
        self.assertIsNone(self.pick("alfa.pl", found("biuro@firma.pl", own=False, pages=9),
                                    found("imie.nazwisko@alfa.pl")))

    def test_two_equally_good_personal_addresses_are_a_tie_and_nothing_is_chosen(self):
        self.assertIsNone(self.pick("alfa.pl", found("kowalski@alfa.pl"), found("nowak@alfa.pl")))

    def test_a_single_personal_address_is_chosen_when_it_is_the_only_one(self):
        self.assertEqual(self.pick("alfa.pl", found("kowalski@alfa.pl", pages=148)), "kowalski@alfa.pl")

    def test_a_clear_winner_among_personal_addresses_is_chosen(self):
        self.assertEqual(self.pick("alfa.pl", found("marek@alfa.pl", pages=43), found("ewa@alfa.pl", pages=1)),
                         "marek@alfa.pl")

    def test_a_tie_in_a_better_tier_is_not_decided_by_a_worse_one(self):
        self.assertIsNone(self.pick("alfa.pl", found("biuro@alfa.pl", pages=5), found("kontakt@alfa.pl", pages=5),
                                    found("szef@alfa.pl", pages=9)))

    def test_the_same_address_found_twice_is_not_a_tie(self):
        self.assertEqual(self.pick("alfa.pl", found("Biuro@Alfa.pl", pages=2), found("biuro@alfa.pl", pages=7)),
                         "biuro@alfa.pl")

    def test_a_foreign_address_is_ignored_when_the_firm_has_its_own(self):
        self.assertEqual(self.pick("alfa.pl", found("biuro@partner.pl", own=False, pages=99), found("info@alfa.pl")),
                         "info@alfa.pl")

    def test_a_foreign_address_counts_only_when_it_returns_on_many_pages(self):
        self.assertIsNone(self.pick("alfa.pl", found("biuro@klient.pl", own=False, pages=1)))
        self.assertEqual(self.pick("alfa.pl", found("alfa@poczta.onet.pl", own=False, pages=5)), "alfa@poczta.onet.pl")

    def test_the_same_brand_on_another_domain_ending_is_the_firms_own_address(self):
        self.assertEqual(self.pick("e-lokator.org", found("kontakt@e-lokator.pl", own=False)), "kontakt@e-lokator.pl")

    def test_a_similar_but_different_brand_is_foreign(self):
        self.assertIsNone(self.pick("nexusfinanse.pl", found("biuro@nexus.pl", own=False, pages=2)))

    def test_no_addresses_or_malformed_ones_give_nothing(self):
        self.assertIsNone(self.pick("alfa.pl"))
        self.assertIsNone(self.pick("alfa.pl", found("nie-adres"), found("a@b"), found("a@@alfa.pl"), found("")))


class HelperTests(unittest.TestCase):
    def test_split_email_lowercases_and_validates(self):
        self.assertEqual(cp.split_email(" Biuro@Alfa.PL "), ("biuro", "alfa.pl"))
        for bad in ("", "biuro", "biuro@alfa", "@alfa.pl", "a b@alfa.pl"):
            self.assertIsNone(cp.split_email(bad), bad)

    def test_brand_label_is_the_first_part_without_www(self):
        self.assertEqual(cp.brand_label("www.kancelariamistewicz.com.pl"), "kancelariamistewicz")
        self.assertEqual(cp.brand_label(""), "")


if __name__ == "__main__":
    unittest.main()
