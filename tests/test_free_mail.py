"""Domeny poczty publicznej i domeny firm."""

from __future__ import annotations

import unittest

from perno.domain import free_mail as fm


class DomainTests(unittest.TestCase):
    def test_the_domain_of_an_address(self):
        self.assertEqual(fm.email_domain("Jan.Kowalski@MyLimo.PL"), "mylimo.pl")
        self.assertEqual(fm.email_domain(" a@b.c. "), "b.c")
        for junk in ("bez-malpy", "", "@x.pl"):
            self.assertEqual(fm.email_domain(junk), "", junk)

    def test_public_providers_and_their_subdomains_are_recognised(self):
        for domain in ("gmail.com", "WP.PL", "poczta.wp.pl", "mail.gmx.de", "o2.pl.", "outlook.com"):
            with self.subTest(domain=domain):
                self.assertTrue(fm.is_public_domain(domain))

    def test_company_domains_are_not_public(self):
        for domain in ("mylimo.pl", "firma.com.pl", "wp.pl.firma.pl", "notgmail.com", "gmail.com.evil.pl", ""):
            with self.subTest(domain=domain):
                self.assertFalse(fm.is_public_domain(domain))

    def test_a_bare_top_level_domain_is_not_public(self):
        self.assertFalse(fm.is_public_domain("com"))
        self.assertFalse(fm.is_public_domain("pl"))


class SiteDomainTests(unittest.TestCase):
    def test_urls_reduce_to_the_host_without_www(self):
        for site, expected in (("https://www.mylimo.pl/kontakt?x=1#a", "mylimo.pl"), ("http://Firma.COM.pl", "firma.com.pl"),
                               ("mylimo.pl/oferta", "mylimo.pl"), ("https://user@sklep.example.com:8443/", "sklep.example.com"),
                               ("https://sub.mylimo.pl", "sub.mylimo.pl")):
            with self.subTest(site=site):
                self.assertEqual(fm.site_domain(site), expected)

    def test_things_that_are_not_sites_give_nothing(self):
        for site in ("", "   ", "localhost", "brak strony", "https://"):
            with self.subTest(site=site):
                self.assertEqual(fm.site_domain(site), "")


if __name__ == "__main__":
    unittest.main()
