from __future__ import annotations

import unittest

from lib.scraper import contacts
from lib.scraper.contacts import (decode_cfemail, email_role, emails_in_text, extract_contacts, normalize_email,
                                  normalize_phone, phones_in_text, social_profile)
from lib.scraper.extract import parse_html

PAGE = "https://firma.pl/kontakt"


def cf_encode(email: str, key: int = 0x42) -> str:
    return f"{key:02x}" + "".join(f"{byte ^ key:02x}" for byte in email.encode("utf-8"))


def run(body: str, host: str = "firma.pl", **kwargs):
    return extract_contacts(parse_html(body, PAGE), PAGE, host, **kwargs)


def values(found, typ):
    return sorted(c.wartosc for c in found if c.typ == typ)


class EmailTests(unittest.TestCase):
    def test_valid_addresses_are_lowercased_and_trimmed(self):
        self.assertEqual(normalize_email(" <Info@Firma.PL> "), "info@firma.pl")
        self.assertEqual(normalize_email("mailto:jan.kowalski+b2b@sub.firma.com.pl"), "jan.kowalski+b2b@sub.firma.com.pl")

    def test_junk_is_rejected(self):
        for junk in ("logo@2x.png", "sprite@firma.svg", "a@b", "x@example.com", "jan@test.com", "abc@sentry.io",
                     "abc@o123.ingest.sentry.io", "u003einfo@firma.pl", "e3b0c44298fc1c149afbf4c8996fb924@sentry-next.wixpress.com",
                     "noreply@firma.pl", "no-reply@firma.pl", "mailer-daemon@firma.pl", "postmaster@firma.pl",
                     ".info@firma.pl", "info.@firma.pl", "in..fo@firma.pl", "info@-firma.pl", "info@firma..pl",
                     "user@schema.org", "x@gravatar.com", "info@yourdomain.com", "%20info@firma.pl", "plain", "", None):
            self.assertIsNone(normalize_email(junk or ""), junk)

    def test_domain_pl_is_not_treated_as_a_placeholder(self):
        self.assertEqual(normalize_email("bok@domena.pl"), "bok@domena.pl")  # prawdziwa domena (rejestrator)

    def test_roles(self):
        table = {"info@a.pl": "ogolny", "biuro.warszawa@a.pl": "ogolny", "kontakt@a.pl": "ogolny",
                 "sales@a.pl": "sprzedaz", "zamowienia@a.pl": "sprzedaz", "oferty@a.pl": "sprzedaz",
                 "iod@a.pl": "prawny", "rodo@a.pl": "prawny", "dpo@a.pl": "prawny", "abuse@a.pl": "prawny",
                 "privacy@a.pl": "prawny", "jan.kowalski@a.pl": "inny", "anna@a.pl": "inny"}
        for email, role in table.items():
            self.assertEqual(email_role(email), role, email)

    def test_text_extraction_and_deobfuscation(self):
        text = ("Pisz: Info@Firma.pl, albo handel [at] firma [dot] pl lub biuro(at)firma(dot)com.pl; "
                "też zespol{małpa}firma.pl oraz kierownik[@]firma.pl. Powtórka: info@firma.pl")
        self.assertEqual(emails_in_text(text), ["info@firma.pl", "handel@firma.pl", "biuro@firma.com.pl",
                                                "zespol@firma.pl", "kierownik@firma.pl"])

    def test_plain_words_at_and_dot_are_not_deobfuscated(self):
        self.assertEqual(emails_in_text("meet us at office dot com or jan at firma dot pl"), [])

    def test_email_must_not_be_glued_to_neighbouring_word_characters(self):
        self.assertEqual(emails_in_text("kontaktinfo@firma.pl."), ["kontaktinfo@firma.pl"])
        self.assertEqual(emails_in_text("(info@firma.pl)"), ["info@firma.pl"])
        self.assertEqual(emails_in_text("x@firma.pl-extra"), [])

    def test_cloudflare_decode_roundtrip_and_garbage(self):
        self.assertEqual(decode_cfemail(cf_encode("biuro@firma.pl")), "biuro@firma.pl")
        self.assertEqual(decode_cfemail(cf_encode("zażółć@firma.pl", key=0x7f)), "zażółć@firma.pl")
        for bad in ("", "zz", "12", "ff", "4a2"):
            self.assertIsNone(decode_cfemail(bad), bad)


class PhoneTests(unittest.TestCase):
    def test_polish_formats_normalize_to_plus48(self):
        for raw in ("22 123 45 67", "(22) 123-45-67", "+48 22 123 45 67", "0048 221234567", "022 123 45 67",
                    "+48(22)1234567", "221234567", "+48.22.123.45.67"):
            self.assertEqual(normalize_phone(raw), "+48221234567", raw)
        for raw in ("600 100 200", "+48 600-100-200", "600100200", "0048600100200"):
            self.assertEqual(normalize_phone(raw), "+48600100200", raw)

    def test_krakow_area_code_starting_with_one_is_valid(self):
        self.assertEqual(normalize_phone("12 634 56 78"), "+48126345678")
        self.assertIsNone(normalize_phone("12 345 67 89"))  # to dokładnie ciąg 123456789 — odrzucany jako sekwencja

    def test_invalid_numbers(self):
        for raw in ("123456789", "000000000", "600600600600", "111 111 111", "99 123 45 67", "22 123 45", "+48 22 123 45",
                    "+1 202 555 0143", "0 600 100 200", "", "abc"):
            self.assertIsNone(normalize_phone(raw), raw)

    def test_international_numbers_need_explicit_prefix_and_permission(self):
        self.assertIsNone(normalize_phone("+49 30 123456"))
        self.assertEqual(normalize_phone("+49 30 1234567", international=True), "+49301234567")
        self.assertEqual(normalize_phone("+49 30 1234567", trusted=True), "+49301234567")
        self.assertIsNone(normalize_phone("49 30 1234567", international=True))  # bez + nie zgadujemy
        self.assertIsNone(normalize_phone("+49 12", international=True))

    def test_text_extraction_skips_identifiers_dates_and_prices(self):
        text = ("Tel. 22 123 45 67, komórka +48 600 100 200. NIP 525 123 45 67. REGON 123 456 789. KRS: 0000123456. "
                "Konto: PL61 1090 1014 0000 0712 1981 2874. Data 12.05.2024. Cena 123 456 789 zł. "
                "Nr rachunku 22 123 45 67 89. Kod 30-123 Kraków. EAN 5901234123457")
        self.assertEqual(phones_in_text(text), ["+48221234567", "+48600100200"])

    def test_numbers_on_neighbouring_lines_are_not_merged(self):
        self.assertEqual(phones_in_text("Tel: 22 123 45 67\n600 100 200\n601 200 300"),
                         ["+48221234567", "+48600100200", "+48601200300"])

    def test_glued_prefix_without_space(self):
        self.assertEqual(phones_in_text("tel.+48 22 123 45 67"), ["+48221234567"])

    def test_international_flag_in_text(self):
        text = "Call +44 20 7946 0958 or +49 30 1234567"
        self.assertEqual(phones_in_text(text), [])
        self.assertEqual(phones_in_text(text, international=True), ["+442079460958", "+49301234567"])


class SocialTests(unittest.TestCase):
    def test_company_profiles_are_canonicalized(self):
        table = {
            "https://www.facebook.com/FirmaPL/?ref=page_internal": ("facebook", "https://facebook.com/FirmaPL"),
            "http://m.facebook.com/firma.pl": ("facebook", "https://facebook.com/firma.pl"),
            "https://instagram.com/firma_pl/": ("instagram", "https://instagram.com/firma_pl"),
            "https://www.linkedin.com/company/firma-sp-z-o-o/about/": ("linkedin", "https://linkedin.com/company/firma-sp-z-o-o"),
            "https://twitter.com/firma": ("x", "https://x.com/firma"),
            "https://x.com/firma?lang=pl": ("x", "https://x.com/firma"),
            "https://www.youtube.com/@Firma": ("youtube", "https://youtube.com/@Firma"),
            "https://www.youtube.com/channel/UCabc123": ("youtube", "https://youtube.com/channel/UCabc123"),
            "https://www.tiktok.com/@firma": ("tiktok", "https://tiktok.com/@firma"),
            "https://github.com/firma-org": ("github", "https://github.com/firma-org"),
        }
        for url, expected in table.items():
            self.assertEqual(social_profile(url), expected, url)

    def test_personal_share_and_non_profile_links_are_rejected(self):
        for url in ("https://www.linkedin.com/in/jan-kowalski", "https://www.facebook.com/profile.php?id=100",
                    "https://www.facebook.com/sharer/sharer.php?u=x", "https://www.facebook.com/people/Jan/123",
                    "https://www.facebook.com/tr?id=1", "https://twitter.com/intent/tweet?text=x",
                    "https://x.com/share", "https://www.instagram.com/p/Cabc/", "https://www.youtube.com/watch?v=abc",
                    "https://youtu.be/abc", "https://www.tiktok.com/video/1", "https://github.com/features",
                    "https://facebook.com/", "ftp://facebook.com/firma", "https://evil.com/facebook.com/firma",
                    "https://facebook.com.evil.pl/firma", "not a url", ""):
            self.assertIsNone(social_profile(url), url)

    def test_non_ascii_path_is_percent_encoded_to_stay_ascii(self):
        platform, url = social_profile("https://facebook.com/Zażółć")
        self.assertTrue(url.isascii())
        self.assertEqual(platform, "facebook")


class FormTests(unittest.TestCase):
    def kinds(self, body):
        return [c.zrodlo_typ for c in run(body) if c.typ == "formularz"]

    def test_contact_form_by_fields(self):
        self.assertEqual(self.kinds('<form><input name="email"><textarea name="m"></textarea></form>'), ["forma"])
        self.assertEqual(self.kinds('<form><input type="email" name="x"><input name="wiadomosc"></form>'), ["forma"])

    def test_widget_marker_with_email_field(self):
        self.assertEqual(self.kinds('<form class="wpcf7-form"><input type="email" name="e"></form>'), ["forma"])

    def test_not_contact_forms(self):
        for body in ('<form><input type="email" name="newsletter"></form>',
                     '<form role="search"><input type="search" name="s"><input name="email"><textarea></textarea></form>',
                     '<form><input type="email"><input type="password"><textarea></textarea></form>',
                     '<form id="commentform" action="/wp-comments-post.php"><input name="email"><textarea name="comment"></textarea></form>',
                     '<form><input name="q"></form>'):
            self.assertEqual(self.kinds(body), [], body)

    def test_embedded_form_services(self):
        self.assertEqual(self.kinds('<iframe src="https://tally.so/embed/x"></iframe>'), ["forma"])
        self.assertEqual(self.kinds('<iframe src="https://docs.google.com/forms/d/e/1/viewform"></iframe>'), ["forma"])
        self.assertEqual(self.kinds('<iframe src="https://www.youtube.com/embed/x"></iframe>'), [])

    def test_form_value_is_the_page_url(self):
        found = run('<form><input name="email"><textarea></textarea></form>')
        self.assertEqual([c.wartosc for c in found if c.typ == "formularz"], [PAGE])


class ExtractContactsTests(unittest.TestCase):
    def test_wordpress_footer_end_to_end(self):
        body = f"""<html><body><footer class="site-footer">
            <p>Firma Sp. z o.o., ul. Długa 5, 31-001 Kraków<br>NIP 525-123-45-67, REGON 123456789</p>
            <p>Tel.: <a href="tel:+48221234567">22 123 45 67</a>, kom. 600 100 200</p>
            <p><a href="mailto:Biuro@Firma.pl?subject=Zapytanie">Biuro@Firma.pl</a> |
               <a href="mailto:iod@firma.pl">iod@firma.pl</a> | <span data-cfemail="{cf_encode('handel@firma.pl')}">[email&#160;protected]</span></p>
            <a href="https://www.facebook.com/firmapl">FB</a><a href="https://www.facebook.com/sharer/sharer.php?u=1">Udostępnij</a>
            <a href="https://www.linkedin.com/in/ktos">Autor</a><a href="https://www.instagram.com/firma_pl/">IG</a>
            </footer></body></html>"""
        found = run(body)
        self.assertEqual(values(found, "email"), ["biuro@firma.pl", "handel@firma.pl", "iod@firma.pl"])
        self.assertEqual(values(found, "telefon"), ["+48221234567", "+48600100200"])
        self.assertEqual(values(found, "social"), ["https://facebook.com/firmapl", "https://instagram.com/firma_pl"])
        by_email = {c.wartosc: c for c in found if c.typ == "email"}
        self.assertEqual(by_email["biuro@firma.pl"].zrodlo_typ, "mailto")
        self.assertEqual(by_email["handel@firma.pl"].zrodlo_typ, "cfemail")
        self.assertEqual(by_email["iod@firma.pl"].rola, "prawny")
        self.assertEqual(by_email["biuro@firma.pl"].rola, "ogolny")
        self.assertTrue(all(c.w_domenie for c in by_email.values()))

    def test_best_source_wins_for_the_same_value(self):
        found = run('<a href="mailto:info@firma.pl">x</a><p>info@firma.pl</p>'
                    '<script type="application/ld+json">{"@type":"Organization","email":"info@firma.pl"}</script>')
        self.assertEqual([(c.wartosc, c.zrodlo_typ) for c in found if c.typ == "email"], [("info@firma.pl", "mailto")])

    def test_jsonld_email_phone_and_sameas(self):
        found = run("""<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization",
            "email":"mailto:Kontakt@Firma.pl","telephone":"+48 22 123 45 67",
            "sameAs":["https://www.facebook.com/firmapl","https://pl.wikipedia.org/wiki/Firma"],
            "contactPoint":{"@type":"ContactPoint","telephone":"600 100 200","email":"sales@firma.pl"},
            "review":{"author":"Jan","email":"jan@gmail.com"}}</script>""")
        self.assertEqual(values(found, "email"), ["kontakt@firma.pl", "sales@firma.pl"])
        self.assertEqual(values(found, "telefon"), ["+48221234567", "+48600100200"])
        self.assertEqual(values(found, "social"), ["https://facebook.com/firmapl"])
        self.assertTrue(all(c.zrodlo_typ == "jsonld" for c in found if c.typ in ("email", "telefon")))

    def test_w_domenie_for_subdomain_target_domain_and_foreign(self):
        body = '<p>a@firma.pl b@mail.firma.pl c@nowa-domena.pl d@gmail.com e@xfirma.pl</p>'
        found = {c.wartosc: c.w_domenie for c in run(body, extra_hosts=["www.nowa-domena.pl"]) if c.typ == "email"}
        self.assertEqual(found, {"a@firma.pl": True, "b@mail.firma.pl": True, "c@nowa-domena.pl": True,
                                 "d@gmail.com": False, "e@xfirma.pl": False})

    def test_false_positives_in_markup_are_not_reported(self):
        body = """<html><head><script>var cfg={"mail":"dev@sentry.io"}</script></head><body>
            <img src="/logo@2x.png" alt="logo@2x.png"><p>Zdjęcie: banner@3x.jpg</p>
            <a href="mailto:">pusty</a><a href="mailto:not-an-email">zły</a>
            <p>REGON 123456789, NIP 5251234567, KRS 0000123456, rachunek 22 123 45 67 89</p>
            <a href="tel:12345">krótki</a><a href="tel:">pusty</a></body></html>"""
        self.assertEqual(run(body), [])

    def test_mailto_with_several_recipients(self):
        found = run('<a href="mailto:a@firma.pl,b@firma.pl?cc=c@firma.pl">x</a><a href="mailto:Jan%20K%20%3Cjan@firma.pl%3E">y</a>')
        self.assertEqual(values(found, "email"), ["a@firma.pl", "b@firma.pl", "jan@firma.pl"])

    def test_per_type_cap_and_value_length_cap(self):
        body = "<p>" + " ".join(f"u{i}@firma.pl" for i in range(contacts.MAX_PER_TYPE + 30)) + "</p>"
        self.assertEqual(len(values(run(body), "email")), contacts.MAX_PER_TYPE)
        long_url = "https://firma.pl/" + "a" * 300
        form = '<form><input name="email"><textarea></textarea></form>'
        self.assertEqual(extract_contacts(parse_html(form, long_url), long_url, "firma.pl"), [])

    def test_international_site_keeps_foreign_phone(self):
        body = '<a href="tel:+49301234567">Berlin</a><p>+44 20 7946 0958</p>'
        found = run(body, host="firma.de", international=True)
        self.assertEqual(values(found, "telefon"), ["+442079460958", "+49301234567"])
        self.assertEqual(values(run(body, host="firma.pl"), "telefon"), ["+49301234567"])  # tel: jest zaufane, tekst nie

    def test_empty_page(self):
        self.assertEqual(run(""), [])


if __name__ == "__main__":
    unittest.main()
