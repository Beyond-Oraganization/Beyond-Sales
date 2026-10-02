from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc

from perno.scraper import crawler as crawler_mod
from perno.scraper import statuses as st
from perno.scraper.crawler import Crawler
from perno.scraper.fetcher import Fetcher
from perno.scraper.politeness import IpLimiter
from perno.scraper.schema import firmy, kontakty, strony, strony_tresc
from perno.scraper.settings import ScanSettings
from perno.scraper.store import Store
from tests.fake_sites import FakeInternet, NoNetworkMixin, Page, html, local_resolver
from tests.fixtures import FakeClock, FakeTime, ZeroRng, make_db


class CrawlerCase(NoNetworkMixin):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="perno-crawl-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_clock = FakeClock()
        self.db = make_db(self.db_clock, url=f"sqlite:///{os.path.join(self.tmp, 'crawl.db')}")
        self.addCleanup(self.db.engine.dispose)
        self.store = Store(self.db)
        self.store.ensure_schema()
        self.net = FakeInternet().start()
        self.addCleanup(self.net.stop)
        self.logs = []

    # ---- pomocnicy ----------------------------------------------------------------
    def site(self, host, pages=None, robots=None):
        return self.net.site(host, pages, robots)

    def firm(self, host, name=None):
        return self.store.add_manual(self.net.url(host, "/"), name)["id"]

    def settings(self, **kwargs):
        kwargs.setdefault("delay", 0.0)
        return ScanSettings.for_tests(**kwargs)

    def crawl(self, settings=None, *, fetcher=None, fake_time=False, only_ids=None, enqueue=True, **kwargs):
        settings = settings or self.settings()
        if enqueue:
            self.store.enqueue()
        run_id = self.run_id = self.store.start_run({})
        clock = FakeTime() if fake_time else None
        self.clock = clock
        kwargs.setdefault("ip_limiter", IpLimiter(cap=settings.ip_cap, min_spacing=0.0, clock=clock))
        engine = Crawler(
            self.store, settings, run_id,
            fetcher=fetcher or Fetcher(settings, resolver=local_resolver, allow_private=True, request_deadline=5.0, recv_timeout=2.0),
            clock=clock, rng=ZeroRng(), log=self.logs.append, only_ids=only_ids, stagger=0, heartbeat_every=0.05, **kwargs)
        self.crawler = engine
        result = engine.run()
        self.store.finish_run(run_id, result["status"], result["stats"])
        return result

    def row(self, firma_id):
        return self.store.get_firma(firma_id)

    def pages(self, firma_id, **where):
        with self.db.engine.connect() as conn:
            query = sa.select(strony).where(strony.c.firma_id == firma_id).order_by(strony.c.id)
            return [dict(r) for r in conn.execute(query).mappings() if all(r[k] == v for k, v in where.items())]

    def by_path(self, firma_id):
        found = {}
        for page in self.pages(firma_id):
            found[page["url"].split("/", 3)[3] if page["url"].count("/") >= 3 else ""] = page
        return {("/" + key): value for key, value in found.items()}

    def contacts(self, firma_id):
        with self.db.engine.connect() as conn:
            return [dict(r) for r in conn.execute(sa.select(kontakty).where(kontakty.c.firma_id == firma_id)).mappings()]

    def hit_paths(self, host):
        return [e["path"] for e in self.net.hits(host)]

    def gaps(self, host):
        times = sorted(e["t"] for e in self.net.hits(host))
        return [b - a for a, b in zip(times, times[1:])]


class BasicCrawlTests(CrawlerCase):
    def test_small_site_end_to_end(self):
        self.site("a.test", {
            "/": Page(html("/kontakt", "/o-nas", "/oferta", title="Strona główna")),
            "/kontakt": Page(html("/", title="Kontakt",
                                  extra='<p>Napisz: <a href="mailto:biuro@a.test">biuro@a.test</a>, tel. 22 123 45 67</p>')),
            "/o-nas": Page(html("/kontakt", title="O nas")),
            "/oferta": Page(html("/blog/post", title="Oferta")),
            "/blog/post": Page(html(title="Wpis")),
        })
        firma_id = self.firm("a.test", "Firma A")
        result = self.crawl()
        self.assertEqual(result["status"], st.RUN_ZAKONCZONY)
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["stron_pobranych"]), (st.FIRMA_ZAKONCZONA, 5))
        self.assertEqual(firm["robots_status"], "brak")
        found = self.by_path(firma_id)
        self.assertEqual({p: found[p]["status"] for p in found}, {p: "pobrano" for p in ("/", "/kontakt", "/o-nas", "/oferta", "/blog/post")})
        self.assertEqual(found["/"]["zrodlo"], "start")
        self.assertEqual(found["/kontakt"]["tytul"], "Kontakt")
        self.assertEqual(firm["glebokosc_max"], 2)
        contacts = {(c["typ"], c["wartosc"]): c for c in self.contacts(firma_id)}
        self.assertIn(("email", "biuro@a.test"), contacts)
        self.assertEqual(contacts[("email", "biuro@a.test")]["w_domenie"], 1)
        self.assertIn(("telefon", "+48221234567"), contacts)

    def test_first_request_is_robots_and_headers_identify_the_bot(self):
        self.site("a.test", {"/": Page(html())})
        self.firm("a.test")
        self.crawl()
        paths = self.hit_paths("a.test")
        self.assertEqual(paths[0], "/robots.txt")  # zawsze pierwszy
        self.assertLess(paths.index("/robots.txt"), paths.index("/"))
        entry = self.net.hits("a.test", "/")[0]
        self.assertEqual(entry["ua"], "PernoContextBot/1.0 (+test@example.com)")
        self.assertEqual(entry["from"], "test@example.com")
        self.assertIsNone(entry["cookie"])

    def test_depth_limit_stops_a_long_chain(self):
        pages = {f"/p{i}": Page(html(f"/p{i + 1}", title=f"P{i}")) for i in range(1, 30)}
        pages["/"] = Page(html("/p1"))
        self.site("a.test", pages)
        firma_id = self.firm("a.test")
        self.crawl(self.settings(depth=5))
        found = self.by_path(firma_id)
        self.assertEqual(sorted(found, key=len)[-1], "/p5")  # głębokość 0 = start, /pN ma głębokość N
        self.assertEqual(self.row(firma_id)["glebokosc_max"], 5)
        self.assertNotIn("/p6", self.hit_paths("a.test"))
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)

    def test_link_cycles_do_not_refetch(self):
        self.site("a.test", {"/": Page(html("/a", "/b", "/")), "/a": Page(html("/b", "/", "/a")), "/b": Page(html("/a", "/"))})
        self.firm("a.test")
        self.crawl()
        paths = self.hit_paths("a.test")
        for page in ("/", "/a", "/b"):
            self.assertEqual(paths.count(page), 1, page)

    def test_calendar_style_infinite_chain_is_capped(self):
        def chain(entry):
            number = int(entry["path"].rsplit("/", 1)[1])
            return Page(html(f"/kalendarz/{number + 1}", title=f"K{number}"))

        pages = {"/": Page(html("/kalendarz/1"))}
        pages.update({f"/kalendarz/{i}": chain for i in range(1, 400)})
        self.site("a.test", pages)
        firma_id = self.firm("a.test")
        self.crawl(self.settings(max_pages=500, depth=20))
        fetched = [p for p in self.hit_paths("a.test") if p.startswith("/kalendarz/")]
        self.assertLessEqual(len(fetched), 41)
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)

    def test_foreign_links_and_social_platforms_are_never_fetched(self):
        self.site("a.test", {"/": Page(html("https://www.facebook.com/firma", "http://b.test/x", "/ok",
                                           extra='<a href="http://127.0.0.1:1/">lokalny</a>')),
                             "/ok": Page(html())})
        self.site("b.test", {"/x": Page(html())})
        self.firm("a.test")
        self.crawl()
        self.assertEqual(self.net.hits("b.test"), [])
        self.assertEqual(sorted(set(self.hit_paths("a.test"))), ["/", "/ok", "/robots.txt", "/sitemap.xml"])

    def test_static_noise_urls_are_skipped_before_any_request(self):
        self.site("a.test", {"/": Page(html("/plik.pdf", "/obraz.jpg", "/wp-admin/x", "/wp-json/v2", "/koszyk", "/dobra",
                                           "/?utm_source=x", "/dobra?utm_campaign=y")),
                             "/dobra": Page(html())})
        self.firm("a.test")
        self.crawl()
        self.assertEqual(sorted(set(self.hit_paths("a.test")) - {"/robots.txt", "/sitemap.xml"}), ["/", "/dobra"])

    def test_404_and_non_html_pages_are_recorded_without_stopping(self):
        self.site("a.test", {"/": Page(html("/zniknela", "/plik", "/dobra")),
                             "/plik": Page(b"%PDF", content_type="application/pdf"), "/dobra": Page(html())})
        firma_id = self.firm("a.test")
        self.crawl()
        found = self.by_path(firma_id)
        self.assertEqual((found["/zniknela"]["status"], found["/zniknela"]["http_status"], found["/zniknela"]["blad"]), ("blad", 404, "HTTP 404"))
        self.assertEqual(found["/plik"]["status"], "niehtml")
        self.assertEqual(found["/dobra"]["status"], "pobrano")
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)

    def test_oversized_page_is_marked_and_others_continue(self):
        self.site("a.test", {"/": Page(html("/duza", "/mala")), "/duza": Page("<html>" + "x" * 200_000 + "</html>"),
                             "/mala": Page(html())})
        firma_id = self.firm("a.test")
        fetcher = Fetcher(self.settings(), resolver=local_resolver, allow_private=True, max_body=50_000)
        self.crawl(fetcher=fetcher)
        found = self.by_path(firma_id)
        self.assertEqual((found["/duza"]["status"], found["/mala"]["status"]), ("za_duze", "pobrano"))

    def test_identical_pages_are_stored_once(self):
        body = html("/", title="Taki sam", extra="<p>" + "unikalna tresc " * 30 + "</p>")
        self.site("a.test", {"/": Page(html("/oryginal", "/kopia", title="Start")), "/oryginal": Page(body), "/kopia": Page(body)})
        firma_id = self.firm("a.test")
        self.crawl()
        found = self.by_path(firma_id)
        self.assertEqual(sorted([found["/oryginal"]["status"], found["/kopia"]["status"]]), ["duplikat", "pobrano"])
        with self.db.engine.connect() as conn:
            texts = conn.execute(sa.select(sa.func.count()).select_from(strony_tresc)).scalar()
        self.assertEqual(texts, 2)  # start + jeden z dwóch identycznych

    def test_nofollow_via_meta_and_header(self):
        self.site("a.test", {
            "/": Page(html("/meta", "/header", "/rel")),
            "/meta": Page('<html><head><meta name="robots" content="nofollow"></head><body>' + "x " * 200 + '<a href="/po-meta">x</a></body></html>'),
            "/header": Page(html("/po-naglowku"), headers={"X-Robots-Tag": "noindex, nofollow"}),
            "/rel": Page(html(extra='<a href="/po-rel" rel="nofollow">x</a><a href="/zwykly">x</a>')),
            "/po-meta": Page(html()), "/po-naglowku": Page(html()), "/po-rel": Page(html()), "/zwykly": Page(html()),
        })
        self.firm("a.test")
        self.crawl()
        fetched = set(self.hit_paths("a.test"))
        self.assertTrue({"/meta", "/header", "/rel", "/zwykly"} <= fetched)
        self.assertFalse(fetched & {"/po-meta", "/po-naglowku", "/po-rel"})

    def test_only_key_pages_keep_html_by_default(self):
        self.site("a.test", {"/": Page(html("/kontakt", "/blog/wpis", title="Start")), "/kontakt": Page(html(title="K")),
                             "/blog/wpis": Page(html(title="W"))})
        firma_id = self.firm("a.test")
        self.crawl()
        with self.db.engine.connect() as conn:
            rows = conn.execute(sa.select(strony.c.url, strony_tresc.c.html_gz).join(
                strony_tresc, strony_tresc.c.strona_id == strony.c.id).where(strony.c.firma_id == firma_id)).all()
        kept = {r.url.rsplit("/", 1)[1] or "start": r.html_gz is not None for r in rows}
        self.assertEqual(kept, {"start": True, "kontakt": True, "wpis": False})

    def test_store_html_modes(self):
        for mode, expected in (("all", 3), ("nie", 0)):
            self.setUp()
            self.site("a.test", {"/": Page(html("/kontakt", "/blog/wpis", title="Start")), "/kontakt": Page(html(title="K")),
                                 "/blog/wpis": Page(html(title="W"))})
            self.firm("a.test")
            self.crawl(self.settings(store_html=mode))
            with self.db.engine.connect() as conn:
                count = conn.execute(sa.select(sa.func.count()).select_from(strony_tresc).where(strony_tresc.c.html_gz.is_not(None))).scalar()
            self.assertEqual(count, expected, mode)


class RobotsAndSitemapTests(CrawlerCase):
    def test_disallowed_paths_are_never_requested(self):
        self.site("a.test", {"/": Page(html("/prywatne/x", "/publiczne", "/wp-admin/admin-ajax.php")),
                             "/publiczne": Page(html()), "/prywatne/x": Page(html()), "/wp-admin/admin-ajax.php": Page(html())},
                  robots="User-agent: *\nDisallow: /prywatne/\nDisallow: /publiczne/ukryte\n")
        firma_id = self.firm("a.test")
        self.crawl()
        self.assertNotIn("/prywatne/x", self.hit_paths("a.test"))
        found = self.by_path(firma_id)
        self.assertEqual(found["/prywatne/x"]["status"], "pominieto_robots")
        self.assertEqual(found["/publiczne"]["status"], "pobrano")
        self.assertEqual(self.row(firma_id)["robots_status"], "ok")

    def test_disallow_everything_blocks_the_firm_without_touching_it(self):
        self.site("a.test", {"/": Page(html())}, robots="User-agent: *\nDisallow: /\n")
        firma_id = self.firm("a.test")
        self.crawl()
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_ZABLOKOWANA, "robots_disallow"))
        self.assertEqual(self.hit_paths("a.test"), ["/robots.txt"])

    def test_our_own_group_overrides_the_wildcard_group(self):
        self.site("a.test", {"/": Page(html("/x")), "/x": Page(html())},
                  robots="User-agent: *\nDisallow: /\n\nUser-agent: PernoContextBot\nDisallow: /tajne/\n")
        firma_id = self.firm("a.test")
        self.crawl()
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)
        self.assertIn("/x", self.hit_paths("a.test"))

    def test_crawl_delay_is_honoured_between_requests(self):
        self.site("a.test", {"/": Page(html("/a", "/b", title="S")), "/a": Page(html(title="A")), "/b": Page(html(title="B"))},
                  robots="User-agent: *\nCrawl-delay: 0.6\n")
        firma_id = self.firm("a.test")
        self.crawl()
        gaps = self.gaps("a.test")
        self.assertGreaterEqual(len(gaps), 4)  # robots, sitemap, /, /a, /b
        self.assertGreaterEqual(min(gaps), 0.58, gaps)
        self.assertEqual(self.row(firma_id)["crawl_delay"], 0.6)

    def test_sitemap_urls_are_crawled_with_their_source_and_foreign_ones_ignored(self):
        sitemap = ("<urlset><url><loc>{a}</loc></url><url><loc>{b}</loc></url>"
                   "<url><loc>http://obca.test/x</loc></url></urlset>")
        self.site("a.test", {"/": Page(html()), "/ukryta": Page(html(title="Tylko w sitemapie")), "/ukryta2": Page(html())},
                  robots=lambda e: Page("User-agent: *\nSitemap: " + self.net.url("a.test", "/mapa.xml") + "\n", content_type="text/plain"))
        self.net.sites["a.test"]["pages"]["/mapa.xml"] = lambda e: Page(
            sitemap.format(a=self.net.url("a.test", "/ukryta"), b=self.net.url("a.test", "/ukryta2")), content_type="application/xml")
        self.site("obca.test", {"/x": Page(html())})
        firma_id = self.firm("a.test")
        self.crawl()
        found = self.by_path(firma_id)
        self.assertEqual((found["/ukryta"]["status"], found["/ukryta"]["zrodlo"]), ("pobrano", "sitemap"))
        self.assertIn("/ukryta2", found)
        self.assertEqual(self.net.hits("obca.test"), [])

    def test_sitemap_index_is_followed_one_level(self):
        index = "<sitemapindex><sitemap><loc>{c}</loc></sitemap></sitemapindex>"
        self.site("a.test", {"/": Page(html()), "/dziecko": Page(html())})
        pages = self.net.sites["a.test"]["pages"]
        pages["/sitemap.xml"] = lambda e: Page(index.format(c=self.net.url("a.test", "/sitemap-1.xml")), content_type="application/xml")
        pages["/sitemap-1.xml"] = lambda e: Page(
            "<urlset><url><loc>" + self.net.url("a.test", "/dziecko") + "</loc></url></urlset>", content_type="application/xml")
        firma_id = self.firm("a.test")
        self.crawl()
        self.assertEqual(self.by_path(firma_id)["/dziecko"]["zrodlo"], "sitemap")

    def test_missing_sitemap_and_robots_are_harmless(self):
        self.site("a.test", {"/": Page(html())})
        firma_id = self.firm("a.test")
        self.crawl()
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)

    def test_robots_is_cached_for_24_hours(self):
        self.site("a.test", {"/": Page(html())}, robots="User-agent: *\nDisallow: /nic\n")
        firma_id = self.firm("a.test")
        self.crawl()
        self.store.requeue([firma_id])
        self.crawl(enqueue=False)
        self.assertEqual(self.hit_paths("a.test").count("/robots.txt"), 1)
        self.db_clock.advance(hours=25)
        self.store.requeue([firma_id])
        self.crawl(enqueue=False)
        self.assertEqual(self.hit_paths("a.test").count("/robots.txt"), 2)

    def test_robots_forbidden_stops_the_firm_after_one_polite_retry(self):
        self.site("a.test", {"/": Page(html())}, robots=Page("forbidden", status=403, content_type="text/plain"))
        firma_id = self.firm("a.test")
        self.crawl(fake_time=True)
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_ZABLOKOWANA, "robots_odmowa"))
        self.assertEqual(self.hit_paths("a.test"), ["/robots.txt", "/robots.txt"])
        self.assertIn(60.0, self.clock.sleeps)

    def test_robots_server_error_is_an_error_not_permission(self):
        self.site("a.test", {"/": Page(html())}, robots=Page("boom", status=500, content_type="text/plain"))
        firma_id = self.firm("a.test")
        self.crawl(fake_time=True)
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_BLAD, "robots_niedostepny"))
        self.assertEqual(self.hit_paths("a.test").count("/"), 0)


def counting(first, then):
    """Strona zwracająca `first` przy pierwszym trafieniu, a potem `then`."""
    state = {"n": 0}

    def handler(entry):
        state["n"] += 1
        return first if state["n"] == 1 else then

    return handler


def make_site(case, host, count, delay=0.0, headers=None):
    pages = {"/": Page(html(*[f"/s{i}" for i in range(1, count)], title=f"{host} start"), delay=delay, headers=headers or {})}
    for i in range(1, count):
        pages[f"/s{i}"] = Page(html(title=f"{host} strona {i}"), delay=delay, headers=headers or {})
    case.site(host, pages)


class RefusalTests(CrawlerCase):
    def test_429_with_retry_after_waits_then_stops_after_three(self):
        self.site("a.test", {"/": Page(html("/a", "/b", title="S")),
                             "/a": Page("slow down", status=429, headers={"Retry-After": "120"}, content_type="text/plain"),
                             "/b": Page(html(title="B"))})
        firma_id = self.firm("a.test")
        self.crawl(fake_time=True)
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_ZABLOKOWANA, "http_429"))
        self.assertEqual(self.hit_paths("a.test").count("/a"), 3)
        self.assertNotIn("/b", self.hit_paths("a.test"))
        self.assertTrue(any(sleep >= 120 for sleep in self.clock.sleeps), self.clock.sleeps)
        self.assertGreater(firm["nastepna_proba_po"], self.db_clock.now)
        self.assertEqual(self.by_path(firma_id)["/a"]["status"], "oczekuje")  # zostaje do wznowienia

    def test_long_retry_after_stops_at_once_and_records_when_to_come_back(self):
        self.site("a.test", {"/": Page(html("/a", title="S")),
                             "/a": Page("later", status=503, headers={"Retry-After": "7200"}, content_type="text/plain")})
        firma_id = self.firm("a.test")
        self.crawl(fake_time=True)
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_ZABLOKOWANA, "retry_after"))
        self.assertEqual(self.hit_paths("a.test").count("/a"), 1)
        self.assertGreater(firm["nastepna_proba_po"], self.db_clock.now + timedelta(hours=1))
        self.assertEqual(self.store.claim_next_firma(self.run_id), None)

    def test_a_429_that_clears_up_is_retried_transparently(self):
        self.site("a.test", {"/": Page(html("/a", title="S")),
                             "/a": counting(Page("wait", status=429, headers={"Retry-After": "1"}, content_type="text/plain"),
                                            Page(html(title="A")))})
        firma_id = self.firm("a.test")
        self.crawl(fake_time=True)
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)
        self.assertEqual(self.hit_paths("a.test").count("/a"), 2)
        self.assertEqual(self.by_path(firma_id)["/a"]["status"], "pobrano")

    def test_403_on_the_start_page_gets_one_polite_retry_then_stops(self):
        self.site("a.test", {"/": Page("no", status=403, content_type="text/plain")})
        firma_id = self.firm("a.test")
        self.crawl(fake_time=True)
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_ZABLOKOWANA, "http_403"))
        self.assertEqual(self.hit_paths("a.test").count("/"), 2)
        self.assertIn(60.0, self.clock.sleeps)
        self.assertEqual(self.by_path(firma_id)["/"]["status"], "oczekuje")

    def test_bot_wall_stops_the_firm_and_we_never_try_to_get_around_it(self):
        wall = Page("<html><title>Just a moment...</title><body>Checking your browser</body></html>", status=403,
                    headers={"cf-mitigated": "challenge", "cf-ray": "abc"})
        self.site("a.test", {"/": wall})
        firma_id = self.firm("a.test")
        self.crawl(fake_time=True)
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_ZABLOKOWANA, "bot_wall"))
        self.assertEqual(self.hit_paths("a.test").count("/"), 2)

    def test_recaptcha_on_a_normal_page_is_not_mistaken_for_a_wall(self):
        self.site("a.test", {"/": Page(html("/kontakt", title="Start", extra='<div class="g-recaptcha"></div>')),
                             "/kontakt": Page(html(title="Kontakt", extra='<div class="g-recaptcha"></div>'))})
        firma_id = self.firm("a.test")
        self.crawl(fake_time=True)
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)

    def test_three_403s_in_a_row_on_inner_pages_stop_the_firm(self):
        pages = {"/": Page(html(*[f"/a{i}" for i in range(1, 7)], title="S"))}
        pages.update({f"/a{i}": Page("no", status=403, content_type="text/plain") for i in range(1, 7)})
        self.site("a.test", pages)
        firma_id = self.firm("a.test")
        self.crawl(fake_time=True)
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_ZABLOKOWANA, "http_403"))
        inner = [p for p in self.hit_paths("a.test") if p.startswith("/a")]
        self.assertEqual(len(inner), 3)

    def test_server_errors_are_retried_twice_then_recorded(self):
        self.site("a.test", {"/": Page(html("/x", "/ok", title="S")), "/x": Page("boom", status=500, content_type="text/plain"),
                             "/ok": Page(html(title="OK"))})
        firma_id = self.firm("a.test")
        self.crawl(fake_time=True)
        self.assertEqual(self.hit_paths("a.test").count("/x"), 3)
        self.assertEqual([round(s) for s in self.clock.sleeps if abs(s - 30) < 1 or abs(s - 90) < 1], [30, 90])
        found = self.by_path(firma_id)
        self.assertEqual((found["/x"]["status"], found["/x"]["blad"]), ("blad", "HTTP 500"))
        self.assertEqual(found["/ok"]["status"], "pobrano")
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)

    def test_timeouts_on_the_start_page_are_an_error_that_can_be_retried_later(self):
        self.site("a.test", {"/": Page(html(title="S"), header_delay=3.0)})
        firma_id = self.firm("a.test")
        fetcher = Fetcher(self.settings(), resolver=local_resolver, allow_private=True, request_deadline=0.5, recv_timeout=0.3)
        self.crawl(fetcher=fetcher, fake_time=True)
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_BLAD, "timeout"))
        self.assertEqual(self.hit_paths("a.test").count("/"), 3)
        self.net.sites["a.test"]["pages"]["/"] = Page(html(title="Naprawiona"))
        self.assertEqual(self.store.requeue([firma_id]), 1)
        self.crawl(enqueue=False)
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)
        self.assertEqual(self.by_path(firma_id)["/"]["status"], "pobrano")

    def test_dns_failure_stops_at_once(self):
        import socket

        def failing(host, port):
            raise socket.gaierror(8, "nie ma")

        self.site("a.test", {"/": Page(html())})
        firma_id = self.firm("a.test")
        self.crawl(fetcher=Fetcher(self.settings(), resolver=failing, allow_private=True))
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_BLAD, "dns"))
        self.assertEqual(self.net.log, [])

    def test_private_addresses_are_refused_without_any_request(self):
        self.site("a.test", {"/": Page(html())})
        firma_id = self.firm("a.test")
        self.crawl(fetcher=Fetcher(self.settings(), resolver=local_resolver, allow_private=False))
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_POMINIETA, "ssrf"))
        self.assertEqual(self.net.log, [])

    def test_missing_home_page_is_an_error_and_recoverable(self):
        self.site("a.test", {})
        firma_id = self.firm("a.test")
        self.crawl()
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_BLAD, "brak_strony_glownej"))
        self.net.sites["a.test"]["pages"]["/"] = Page(html(title="Już jest"))
        self.store.requeue([firma_id])
        self.crawl(enqueue=False)
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)


class RedirectTests(CrawlerCase):
    def test_start_redirect_to_another_domain_is_not_followed(self):
        self.site("a.test", {"/": Page("moved", status=301, location=self.net.url("b.test", "/"))})
        self.site("b.test", {"/": Page(html())})
        firma_id = self.firm("a.test")
        self.crawl()
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"], firm["domena_docelowa"]),
                         (st.FIRMA_POMINIETA, "przekierowanie", "b.test"))
        self.assertEqual(self.net.hits("b.test"), [])
        self.assertEqual(self.store.use_target_domain(firma_id)["domena"], "b.test")

    def test_same_site_redirect_is_followed_and_both_urls_recorded(self):
        self.site("a.test", {"/": Page(html("/stara", title="S")), "/stara": Page("m", status=301, location="/nowa"),
                             "/nowa": Page(html(title="Nowa"))})
        firma_id = self.firm("a.test")
        self.crawl()
        found = self.by_path(firma_id)
        self.assertEqual((found["/stara"]["status"], found["/stara"]["http_status"]), ("przekierowanie", 301))
        self.assertEqual((found["/nowa"]["status"], found["/nowa"]["tytul"]), ("pobrano", "Nowa"))
        self.assertEqual(self.hit_paths("a.test").count("/nowa"), 1)

    def test_start_page_http_to_canonical_path_redirect(self):
        self.site("a.test", {"/": Page("m", status=302, location="/pl/"), "/pl/": Page(html(title="PL"))})
        firma_id = self.firm("a.test")
        self.crawl()
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)
        self.assertEqual(self.by_path(firma_id)["/pl/"]["status"], "pobrano")

    def test_redirect_to_an_already_known_page_does_not_refetch_it(self):
        self.site("a.test", {"/": Page(html("/a", "/b", title="S")), "/a": Page("m", status=302, location="/b"),
                             "/b": Page(html(title="B"))})
        firma_id = self.firm("a.test")
        self.crawl()
        self.assertEqual(self.hit_paths("a.test").count("/b"), 1)
        self.assertEqual(self.by_path(firma_id)["/a"]["status"], "przekierowanie")

    def test_redirect_loop_and_long_chains_end(self):
        self.site("a.test", {"/": Page(html("/p", "/q", title="S")), "/p": Page("m", status=302, location="/p2"),
                             "/p2": Page("m", status=302, location="/p"),
                             "/q": Page("m", status=302, location="/q1"),
                             **{f"/q{i}": Page("m", status=302, location=f"/q{i + 1}") for i in range(1, 12)}})
        firma_id = self.firm("a.test")
        self.crawl()
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)
        found = self.by_path(firma_id)
        self.assertEqual(found["/p"]["status"], "przekierowanie")
        self.assertIn("pętla", found["/p"]["blad"])
        self.assertIn("za wiele", found["/q"]["blad"])
        self.assertLessEqual(len([p for p in self.hit_paths("a.test") if p.startswith("/q")]), 7)

    def test_mid_site_redirects_to_foreign_or_forbidden_targets_are_skipped(self):
        self.site("a.test", {"/": Page(html("/obce", "/zakaz", title="S")),
                             "/obce": Page("m", status=302, location=self.net.url("b.test", "/")),
                             "/zakaz": Page("m", status=302, location="/prywatne/x"), "/prywatne/x": Page(html())},
                  robots="User-agent: *\nDisallow: /prywatne/\n")
        self.site("b.test", {"/": Page(html())})
        firma_id = self.firm("a.test")
        self.crawl()
        found = self.by_path(firma_id)
        self.assertIn("obca domena", found["/obce"]["blad"])
        self.assertIn("robots", found["/zakaz"]["blad"])
        self.assertEqual(self.net.hits("b.test"), [])
        self.assertNotIn("/prywatne/x", self.hit_paths("a.test"))
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)


class ConcurrencyTests(CrawlerCase):
    def test_one_request_per_host_but_hosts_run_in_parallel(self):
        hosts = [f"h{i}.test" for i in range(1, 7)]
        for host in hosts:
            make_site(self, host, 5, delay=0.15)
            self.firm(host)
        settings = self.settings(concurrency=6, delay=0.05, ip_cap=10, max_rps=50.0)  # bramka globalna 15/s sama ograniczyłaby równoległość
        started = time.monotonic()
        result = self.crawl(settings)
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], st.RUN_ZAKONCZONY)
        for host in hosts:
            self.assertEqual(self.net.max_inflight[host], 1, host)
            self.assertEqual(len([p for p in self.hit_paths(host) if p == "/" or p[:2] == "/s" and p[2:].isdigit()]), 5, host)
            self.assertGreaterEqual(min(self.gaps(host)), 0.045, host)
        self.assertGreaterEqual(self.net.max_overlap(), 4)  # naprawdę równolegle między domenami
        self.assertLess(elapsed, 6 * 5 * 0.2)  # istotnie szybciej niż sekwencyjnie

    def test_ip_cap_of_one_serializes_hosts_that_share_an_address(self):
        for i in range(1, 5):
            make_site(self, f"h{i}.test", 3, delay=0.12)
            self.firm(f"h{i}.test")
        self.crawl(self.settings(concurrency=4, ip_cap=1))
        self.assertEqual(self.net.max_overlap(), 1)

    def test_default_ip_cap_of_two(self):
        for i in range(1, 6):
            make_site(self, f"h{i}.test", 3, delay=0.12)
            self.firm(f"h{i}.test")
        self.crawl(self.settings(concurrency=5, ip_cap=2))
        self.assertEqual(self.net.max_overlap(), 2)

    def test_cdn_headers_lift_the_cap_for_shared_edge_addresses(self):
        for i in range(1, 6):
            make_site(self, f"h{i}.test", 4, delay=0.15, headers={"cf-ray": "abc-WAW", "Server": "cloudflare"})
            self.firm(f"h{i}.test")
        self.crawl(self.settings(concurrency=5, ip_cap=1))
        self.assertGreaterEqual(self.net.max_overlap(), 3)

    def test_global_rate_gate_bounds_total_throughput(self):
        for i in range(1, 5):
            make_site(self, f"h{i}.test", 6)
            self.firm(f"h{i}.test")
        started = time.monotonic()
        self.crawl(self.settings(concurrency=4, max_rps=10.0, ip_cap=10))
        elapsed = time.monotonic() - started
        total = len(self.net.log)
        self.assertGreaterEqual(total, 4 * 8)
        self.assertGreaterEqual(elapsed, (total - 1) / 10.0 * 0.9)

    def test_only_ids_restricts_the_run(self):
        for host in ("h1.test", "h2.test", "h3.test"):
            make_site(self, host, 2)
        ids = [self.firm(h) for h in ("h1.test", "h2.test", "h3.test")]
        self.crawl(only_ids=[ids[1]])
        self.assertEqual(self.net.hits("h1.test"), [])
        self.assertEqual(self.net.hits("h3.test"), [])
        self.assertEqual(self.row(ids[1])["skan_status"], st.FIRMA_ZAKONCZONA)
        self.assertEqual(self.row(ids[0])["skan_status"], st.FIRMA_W_KOLEJCE)

    def test_many_firms_through_a_small_pool(self):
        for i in range(1, 13):
            make_site(self, f"h{i}.test", 3)
            self.firm(f"h{i}.test")
        result = self.crawl(self.settings(concurrency=3, ip_cap=3))
        self.assertEqual(result["stats"]["firmy_wynik"], {st.FIRMA_ZAKONCZONA: 12})
        self.assertEqual(result["stats"]["firmy_start"], 12)
        self.assertEqual(result["stats"]["strony"], 36)


class LimitTests(CrawlerCase):
    def chain_site(self, host, count, delay=0.0, size=0):
        pad = "<p>" + "x " * size + "</p>" if size else ""
        pages = {"/": Page(html(*[f"/p{i}" for i in range(1, count)], title="start", extra=pad), delay=delay)}
        pages.update({f"/p{i}": Page(html(title=f"strona {i}", extra=pad), delay=delay) for i in range(1, count)})
        self.site(host, pages)

    def test_page_limit_is_cumulative_and_resume_never_refetches(self):
        self.chain_site("a.test", 20)
        firma_id = self.firm("a.test")
        self.crawl(self.settings(max_pages=5))
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"], firm["stron_pobranych"]),
                         (st.FIRMA_LIMIT, "limit_stron", 5))
        self.store.requeue([firma_id])
        self.crawl(self.settings(max_pages=100), enqueue=False)
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)
        paths = self.hit_paths("a.test")
        self.assertEqual(max(paths.count(f"/p{i}") for i in range(1, 20)), 1)
        self.assertEqual(paths.count("/"), 1)
        self.assertEqual(paths.count("/robots.txt"), 1)
        self.assertEqual(self.row(firma_id)["stron_pobranych"], 20)

    def test_byte_limit(self):
        self.chain_site("a.test", 20, size=8000)
        firma_id = self.firm("a.test")
        self.crawl(self.settings(max_mb=0.06))
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_LIMIT, "limit_bajtow"))
        self.assertLess(firm["stron_pobranych"], 8)
        self.assertGreater(firm["bajty_pobranych"], 0.06 * 1024 * 1024)

    def test_time_limit_per_firm(self):
        self.chain_site("a.test", 20, delay=0.4)
        firma_id = self.firm("a.test")
        self.crawl(self.settings(max_minutes=0.04))  # 2,4 s
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_LIMIT, "limit_czasu"))
        self.assertGreaterEqual(firm["stron_pobranych"], 2)
        self.assertLess(firm["stron_pobranych"], 12)

    def test_frontier_is_refilled_from_the_database_with_a_tiny_memory_cap(self):
        self.chain_site("a.test", 25)
        firma_id = self.firm("a.test")
        with mock.patch.object(crawler_mod, "FRONTIER_MEMORY_CAP", 3):
            self.crawl()
        self.assertEqual(self.row(firma_id)["stron_pobranych"], 25)
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)

    def test_discovery_cap_bounds_the_number_of_known_urls(self):
        self.chain_site("a.test", 40)
        firma_id = self.firm("a.test")
        with mock.patch.object(crawler_mod, "MAX_DISCOVERED_PER_FIRM", 10):
            self.crawl()
        self.assertLessEqual(len(self.pages(firma_id)), 11)
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)


class ResilienceTests(CrawlerCase):
    def slow_chain(self, host, count, delay):
        pages = {"/": Page(html("/p1", title="start"), delay=delay)}
        pages.update({f"/p{i}": Page(html(f"/p{i + 1}", title=f"strona {i}"), delay=delay) for i in range(1, count)})
        self.site(host, pages)

    def run_in_thread(self, **kwargs):
        box = {}
        thread = threading.Thread(target=lambda: box.update(result=self.crawl(**kwargs)), daemon=True)
        thread.start()
        return thread, box

    def wait_for(self, predicate, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_user_stop_requeues_the_firm_and_resume_continues_where_it_stopped(self):
        self.slow_chain("a.test", 30, 0.08)
        firma_id = self.firm("a.test")
        thread, box = self.run_in_thread(settings=self.settings(depth=40))
        self.assertTrue(self.wait_for(lambda: len(self.net.log) >= 8))
        self.assertTrue(self.store.request_stop())
        thread.join(30)
        self.assertEqual(box["result"]["status"], st.RUN_ZATRZYMANY)
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["skan_przyczyna"]), (st.FIRMA_W_KOLEJCE, "stop_uzytkownika"))
        done = len(self.pages(firma_id, status="pobrano"))
        self.assertTrue(3 <= done < 30, done)
        self.crawl(self.settings(depth=40), enqueue=False)
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)
        paths = self.hit_paths("a.test")
        self.assertEqual(max(paths.count(p) for p in paths if p.startswith("/p") or p == "/"), 1)
        self.assertEqual(len(self.pages(firma_id, status="pobrano")), 30)

    def test_external_stop_event_works_like_a_signal(self):
        self.slow_chain("a.test", 30, 0.08)
        firma_id = self.firm("a.test")
        event = threading.Event()
        thread, box = self.run_in_thread(stop_event=event)
        self.assertTrue(self.wait_for(lambda: len(self.net.log) >= 5))
        event.set()
        thread.join(30)
        self.assertEqual(box["result"]["status"], st.RUN_ZATRZYMANY)
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_W_KOLEJCE)

    def test_run_row_receives_heartbeats_with_statistics(self):
        make_site(self, "a.test", 4)
        self.firm("a.test")
        self.crawl()
        run = self.store.get_run(self.run_id)
        self.assertEqual(run["status"], st.RUN_ZAKONCZONY)
        self.assertEqual(run["statystyki"]["strony"], 4)
        self.assertEqual(run["statystyki"]["firmy_wynik"], {st.FIRMA_ZAKONCZONA: 1})

    def test_losing_the_lease_mid_crawl_abandons_the_firm_without_writing(self):
        firma_holder = {}

        def steal(entry):  # inny przebieg przejmuje firmę w trakcie pobierania /p3
            with self.db.engine.begin() as conn:
                conn.execute(sa.update(firmy).where(firmy.c.id == firma_holder["id"]).values(przebieg_id=999))
            return Page(html("/p4", title="strona 3"))

        pages = {"/": Page(html("/p1", title="start"))}
        pages.update({f"/p{i}": Page(html(f"/p{i + 1}", title=f"strona {i}")) for i in range(1, 12)})
        pages["/p3"] = steal
        self.site("a.test", pages)
        firma_holder["id"] = firma_id = self.firm("a.test")
        result = self.crawl()
        self.assertNotIn("/p5", self.hit_paths("a.test"))
        firm = self.row(firma_id)
        self.assertEqual((firm["skan_status"], firm["przebieg_id"]), (st.FIRMA_SKANOWANIE, 999))
        self.assertNotEqual(self.by_path(firma_id)["/p3"]["status"], "pobrano")
        self.assertEqual(result["stats"]["firmy_wynik"], {"dzierzawa_utracona": 1})
        self.assertTrue(any("dzierżawa utracona" in line for line in self.logs))

    def test_database_failure_ends_the_run_as_interrupted_and_firms_return_to_the_queue(self):
        self.slow_chain("a.test", 20, 0.05)
        firma_id = self.firm("a.test")

        class Boom(Exception):
            pass

        failing = sa_exc.OperationalError("INSERT", {}, Boom("MySQL server has gone away"))
        real = self.store.save_result
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] > 3:
                raise failing
            return real(*args, **kwargs)

        with mock.patch.object(self.store, "save_result", flaky):
            result = self.crawl()
        self.assertEqual(result["status"], st.RUN_PRZERWANY)
        self.assertEqual(result["reason"], "baza")
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_W_KOLEJCE)  # finish_run odkolejkował firmę
        self.assertEqual(self.store.get_run(self.run_id)["status"], st.RUN_PRZERWANY)
        self.crawl(enqueue=False)  # po odzyskaniu bazy dokańcza bez ponownego pobierania
        self.assertEqual(self.row(firma_id)["skan_status"], st.FIRMA_ZAKONCZONA)
        paths = self.hit_paths("a.test")
        self.assertLessEqual(max(paths.count(p) for p in paths if p.startswith("/p")), 2)

    def test_unexpected_exception_in_one_firm_does_not_kill_the_others(self):
        make_site(self, "ok.test", 3)
        self.site("bad.test", {"/": Page(html("/x", title="bad"))})
        ids = [self.firm("bad.test"), self.firm("ok.test")]
        real = crawler_mod.extract.parse_html

        def explode(html_text, base):
            if "bad.test" in base:
                raise RuntimeError("parser padł")
            return real(html_text, base)

        with mock.patch.object(crawler_mod.extract, "parse_html", explode):
            self.crawl(self.settings(concurrency=2))
        bad, good = self.row(ids[0]), self.row(ids[1])
        self.assertEqual(bad["skan_status"], st.FIRMA_BLAD)
        self.assertIn("parser padł", bad["ostatni_blad"])
        self.assertEqual(good["skan_status"], st.FIRMA_ZAKONCZONA)

    def test_contacts_seen_on_many_pages_are_counted_per_page(self):
        footer = '<footer><a href="mailto:biuro@a.test">biuro@a.test</a></footer>'
        self.site("a.test", {"/": Page(html("/a", "/b", title="S", extra=footer)), "/a": Page(html(title="A", extra=footer)),
                             "/b": Page(html(title="B", extra=footer))})
        firma_id = self.firm("a.test")
        self.crawl()
        email = [c for c in self.contacts(firma_id) if c["typ"] == "email"]
        self.assertEqual(len(email), 1)
        self.assertEqual(email[0]["liczba_stron"], 3)


if __name__ == "__main__":
    unittest.main()
