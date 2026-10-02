from __future__ import annotations

import random
import unittest

from perno.scraper import urls
from perno.scraper.urls import TrapGuard, host_key, normalize_url, page_class, site_key, static_reject


class NormalizeTests(unittest.TestCase):
    CASES = [
        ("HTTP://Example.COM:80/A/../b//c/?utm_source=x&b=2&a=1#frag", None, "http://example.com/b/c/?a=1&b=2"),
        ("https://example.com", None, "https://example.com/"),
        ("https://example.com:443/", None, "https://example.com/"),
        ("https://example.com:8080/x", None, "https://example.com:8080/x"),
        ("https://www.example.com/index.html", None, "https://www.example.com/"),
        ("https://example.com/dir/INDEX.PHP", None, "https://example.com/dir/"),
        ("https://example.com/index.php?x=1", None, "https://example.com/index.php?x=1"),
        ("https://example.com/a;jsessionid=ABC123/b", None, "https://example.com/a/b"),
        ("https://example.com/a%2fb/%7euser", None, "https://example.com/a%2Fb/~user"),
        ("https://example.com/a b", None, "https://example.com/a%20b"),
        ("https://example.com/?fbclid=zzz", None, "https://example.com/"),
        ("https://example.com/?b=2&a=1&a=0", None, "https://example.com/?a=0&a=1&b=2"),
        ("../x", "https://a.pl/dir/sub/", "https://a.pl/dir/x"),
        ("/kontakt/", "https://a.pl/dir/sub/", "https://a.pl/kontakt/"),
        ("#top", "https://a.pl/strona", "https://a.pl/strona"),
        ("//cdn.a.pl/x", "https://a.pl/", "https://cdn.a.pl/x"),
        ("https://example.com/a/b/..", None, "https://example.com/a/"),
        ("https://example.com/a/./b", None, "https://example.com/a/b"),
    ]

    def test_table(self):
        for raw, base, expected in self.CASES:
            self.assertEqual(normalize_url(raw, base), expected, f"{raw!r} base={base!r}")

    def test_rejected_inputs(self):
        for raw in ("", "   ", "mailto:a@b.pl", "javascript:void(0)", "tel:+48123456789", "ftp://example.com/x",
                    "https://user:pw@example.com/", "https://example.com:99999/", "data:text/html,x",
                    "https://exa mple.com/", "https://" + "a" * 70 + ".com/", "https:///path"):
            self.assertIsNone(normalize_url(raw), repr(raw))
        self.assertIsNone(normalize_url("https://example.com/" + "a" * 2500))

    def test_non_ascii_is_encoded_not_leaked(self):
        result = normalize_url("https://zażółć.pl/ścieżka/plik?imię=Żółw")
        self.assertTrue(result.isascii(), result)
        self.assertTrue(result.startswith("https://xn--"), result)
        self.assertIn("/%C5%9Bcie%C5%BCka/plik", result)
        self.assertIn("imi%C4%99=%C5%BB%C3%B3%C5%82w", result)

    def test_output_is_always_ascii_or_none(self):
        rng = random.Random(7)
        alphabet = "aAzZ09/?&=#%.-_~ąćęłńóśźżĄĆĘŁŃÓŚŹŻ€😀ßü "
        for _ in range(2000):
            tail = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
            for candidate in (f"https://example.com/{tail}", f"https://ex{tail[:5]}.pl/x", f"https://example.com/?{tail}"):
                result = normalize_url(candidate)
                self.assertTrue(result is None or result.isascii(), repr(candidate))
                if result:
                    self.assertEqual(normalize_url(result), result, f"nie jest idempotentne: {result}")

    def test_idempotent_on_table_outputs(self):
        for raw, base, expected in self.CASES:
            self.assertEqual(normalize_url(expected), expected)


class HostTests(unittest.TestCase):
    def test_host_key_and_site_key(self):
        self.assertEqual(host_key("WWW.Example.COM."), "example.com")
        self.assertEqual(host_key("example.com"), "example.com")
        self.assertEqual(host_key("www"), "www")
        self.assertTrue(host_key("zażółć.pl").startswith("xn--"))
        self.assertIsNone(host_key(""))
        self.assertIsNone(host_key("bad host"))
        self.assertEqual(site_key("https://www.example.com:8443/x"), "example.com:8443")
        self.assertEqual(site_key("http://example.com:80/"), "example.com")
        self.assertEqual(site_key("https://www.example.com/"), site_key("http://example.com/x"))
        self.assertNotEqual(site_key("https://blog.example.com/"), site_key("https://example.com/"))

    def test_platform_hosts_including_subdomains(self):
        for host in ("facebook.com", "www.facebook.com", "m.facebook.com", "pl.linkedin.com", "youtu.be"):
            self.assertTrue(urls.is_platform(host), host)
        for host in ("notfacebook.com", "facebook.com.evil.pl", "example.com"):
            self.assertFalse(urls.is_platform(host), host)


class StartUrlTests(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertEqual(urls.validate_start_url("example.pl"), ("https://example.pl/", ""))
        self.assertEqual(urls.validate_start_url(" http://example.pl/abc "), ("http://example.pl/abc", ""))
        self.assertEqual(urls.validate_start_url("https://www.sixt.pl/limousine/"), ("https://www.sixt.pl/limousine/", ""))
        expectations = {
            "": "brak_www", "   ": "brak_www", "localhost": "zly_url", "http://127.0.0.1/": "zly_url",
            "https://10.0.0.1": "zly_url", "http://[::1]/": "zly_url", "intranet": "zly_url",
            "https://foo.local/": "zly_url", "https://x.internal/": "zly_url", "mailto:a@b.pl": "zly_url",
            "https://www.facebook.com/firma": "platforma", "instagram.com/firma": "platforma",
        }
        for raw, reason in expectations.items():
            normalized, got = urls.validate_start_url(raw)
            self.assertEqual((normalized, got), (None, reason), repr(raw))


class StaticRejectTests(unittest.TestCase):
    def test_rejected(self):
        table = {
            "https://a.pl/file.pdf": "rozszerzenie", "https://a.pl/IMG.JPG": "rozszerzenie",
            "https://a.pl/cennik.xlsx": "rozszerzenie", "https://a.pl/wp-json/wp/v2/posts": "sciezka_zablokowana",
            "https://a.pl/wp-admin/admin-ajax.php": "sciezka_zablokowana", "https://a.pl/xmlrpc.php": "sciezka_zablokowana",
            "https://a.pl/wp-content/uploads/x": "sciezka_zablokowana", "https://a.pl/feed/": "sciezka_zablokowana",
            "https://a.pl/blog/feed": "sciezka_zablokowana", "https://a.pl/tag/foo/": "sciezka_zablokowana",
            "https://a.pl/blog/autor/jan": "sciezka_zablokowana", "https://a.pl/koszyk/": "sciezka_zablokowana",
            "https://a.pl/moje-konto/": "sciezka_zablokowana", "https://a.pl/post/embed": "sciezka_zablokowana",
            "https://a.pl/2023/": "archiwum_dat", "https://a.pl/2023/05/": "archiwum_dat",
            "https://a.pl/2023/05/12": "archiwum_dat", "https://a.pl/a/b/a/b/a/": "petla_segmentow",
            "https://a.pl/" + "/".join(f"s{i}" for i in range(11)): "zbyt_glebokie",
            "https://a.pl/?s=abc": "zapytanie_zablokowane", "https://a.pl/?filter_color=red": "zapytanie_zablokowane",
            "https://a.pl/?pa_size=m": "zapytanie_zablokowane", "https://a.pl/?orderby=price": "zapytanie_zablokowane",
            "https://a.pl/?" + "&".join(f"k{i}=1" for i in range(9)): "za_duzo_parametrow",
        }
        for url, reason in table.items():
            self.assertEqual(static_reject(url), reason, url)

    def test_allowed(self):
        for url in ("https://a.pl/", "https://a.pl/kontakt/", "https://a.pl/?p=12", "https://a.pl/2023/05/slug/",
                    "https://a.pl/o-nas.html", "https://a.pl/oferta/cennik", "https://a.pl/blog/wpis-1",
                    "https://a.pl/produkt/rower?kolor=czerwony"):
            self.assertIsNone(static_reject(url), url)


class PageClassTests(unittest.TestCase):
    def test_classes(self):
        table = {"/kontakt/": "kontakt", "/contact-us": "kontakt", "/o-nas": "o_nas", "/o-firmie/": "o_nas",
                 "/team": "o_nas", "/regulamin.html": "prawne", "/polityka-prywatnosci": "prawne",
                 "/oferta/cennik": "oferta", "/realizacje": "oferta", "/blog/post": "", "/firmy": ""}
        for path, expected in table.items():
            self.assertEqual(page_class(path), expected, path)


class TrapGuardTests(unittest.TestCase):
    def admitted(self, guard, urls_):
        return [guard.admit(u)[0] for u in urls_]

    def test_directory_cap_with_extra_room_for_key_pages(self):
        guard = TrapGuard()
        plain = [f"https://a.pl/produkt/item-{i}" for i in range(41)]
        results = self.admitted(guard, plain)
        self.assertEqual(results.count(True), 40)
        self.assertEqual(guard.admit(plain[-1]), (False, "limit_katalogu"))
        key_pages = [f"https://a.pl/produkt/kontakt-{i}" for i in range(11)]
        self.assertEqual(self.admitted(guard, key_pages).count(True), 10)

    def test_root_directory_has_a_higher_cap(self):
        guard = TrapGuard()
        results = self.admitted(guard, [f"https://a.pl/strona-{i}.html" for i in range(101)])
        self.assertEqual(results.count(True), 100)

    def test_pattern_cap_for_unknown_query_parameters(self):
        guard = TrapGuard()
        results = self.admitted(guard, [f"https://a.pl/lista?kolor=c{i}" for i in range(7)])
        self.assertEqual(results.count(True), 5)
        self.assertEqual(guard.admit("https://a.pl/lista?kolor=c9"), (False, "limit_wzorca"))
        self.assertTrue(guard.admit("https://a.pl/inna?kolor=c1")[0])  # inny wzorzec (ścieżka)

    def test_identity_parameters_have_a_looser_cap(self):
        guard = TrapGuard()
        results = self.admitted(guard, [f"https://a.pl/?p={i}" for i in range(102)])
        self.assertEqual(results.count(True), 100)
        self.assertEqual(guard.admit("https://a.pl/?p=500"), (False, "limit_identyfikatorow"))
        self.assertTrue(guard.admit("https://a.pl/?page_id=1")[0])  # osobny licznik na nazwę

    def test_pagination_cap_per_listing(self):
        guard = TrapGuard()
        results = self.admitted(guard, [f"https://a.pl/blog/page/{i}/" for i in range(1, 8)])
        self.assertEqual(results.count(True), 5)
        self.assertTrue(guard.admit("https://a.pl/aktualnosci/page/1/")[0])  # inny listing
        by_query = self.admitted(TrapGuard(), [f"https://a.pl/lista/?paged={i}" for i in range(1, 8)])
        self.assertEqual(by_query.count(True), 5)

    def test_category_cap(self):
        guard = TrapGuard()
        results = self.admitted(guard, [f"https://a.pl/category/k{i}/" for i in range(12)])
        self.assertEqual(results.count(True), 10)
        self.assertEqual(guard.admit("https://a.pl/kategoria/inna/"), (False, "limit_kategorii"))

    def test_rejection_does_not_consume_counters(self):
        guard = TrapGuard()
        guard.DIR_CAP = 2
        self.assertEqual(self.admitted(guard, [f"https://a.pl/category/c{i}" for i in range(4)]), [True, True, False, False])
        self.assertEqual(guard.category_count, 2)

    def test_dead_pattern_after_eight_useless_fetches(self):
        guard = TrapGuard()
        base = "https://a.pl/blog/"
        for i in range(8):
            self.assertTrue(guard.admit(f"{base}wpis-{i}")[0])
            guard.record(f"{base}wpis-{i}", useful=False)
        self.assertEqual(guard.admit(f"{base}wpis-99"), (False, "martwy_wzorzec"))
        self.assertTrue(guard.admit("https://a.pl/oferta/x")[0])
        healthy = TrapGuard()
        for i in range(20):
            healthy.record(f"{base}wpis-{i}", useful=(i % 4 == 0))
        self.assertTrue(healthy.admit(f"{base}nowy")[0])

    def test_static_reasons_are_reported(self):
        self.assertEqual(TrapGuard().admit("https://a.pl/plik.pdf"), (False, "rozszerzenie"))


class PriorityTests(unittest.TestCase):
    def test_contact_beats_plain_pages_and_depth_matters(self):
        contact = urls.priority("https://a.pl/kontakt/", 1)
        plain = urls.priority("https://a.pl/blog/", 1)
        deep = urls.priority("https://a.pl/a/b/c/d", 4)
        self.assertLess(contact, plain)
        self.assertLess(plain, deep)
        self.assertLess(urls.priority("https://a.pl/x", 1), urls.priority("https://a.pl/x", 2))

    def test_class_ordering(self):
        order = [urls.priority(f"https://a.pl/{p}", 1) for p in ("kontakt", "o-nas", "regulamin", "oferta", "inne")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(len(set(order)), 5)

    def test_nav_anchor_prefix_and_sitemap_bonuses(self):
        base = urls.priority("https://a.pl/x", 1)
        self.assertEqual(urls.priority("https://a.pl/x", 1, in_nav=True), base - 10)
        self.assertEqual(urls.priority("https://a.pl/x", 1, anchor_text="Kontakt"), base - 10)
        self.assertEqual(urls.priority("https://a.pl/limousine/x", 1, start_prefix="/limousine/"),
                         urls.priority("https://a.pl/limousine/x", 1) - 20)
        self.assertEqual(urls.priority("https://a.pl/kontakt", 1, from_sitemap=True), 150 + 5 - 80)
        self.assertGreater(urls.priority("https://a.pl/x", 1, from_sitemap=True), base)


class HashTests(unittest.TestCase):
    def test_hash(self):
        h = urls.url_hash("https://a.pl/")
        self.assertEqual(len(h), 40)
        self.assertEqual(h, urls.url_hash("https://a.pl/"))
        self.assertNotEqual(h, urls.url_hash("https://a.pl/x"))


if __name__ == "__main__":
    unittest.main()
