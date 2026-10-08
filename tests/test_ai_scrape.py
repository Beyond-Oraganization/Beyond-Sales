"""ai.py zeskrapuj / szukaj / wyciagnij / zbiory: skan bez bazy na lokalnym „internecie", pliki w katalogu tymczasowym."""

from __future__ import annotations

import contextlib
import csv
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from perno.cli import ai
from perno.domain import text_search as ts
from perno.scraper.fetcher import Fetcher
from perno.scraper.memory_store import MemoryStore
from perno.scraper.politeness import IpLimiter
from perno.scraper.settings import ScanSettings
from perno.services import adhoc_scrape, scrape_dataset
from perno.services.scrape_dataset import Dataset, DatasetError, safe_cell
from tests.fake_sites import FakeInternet, NoNetworkMixin, Page, html, local_resolver
from tests.fixtures import FakeTime

HOME = html("/kontakt", "/cennik", "/prywatne/tajne", title="Salon Alfa")
CONTACT = ("<html><head><title>Kontakt</title></head><body><p>Salon Alfa, Łódź. " + "Zapraszamy. " * 30 +
           '</p><p><a href="mailto:biuro@a.test">biuro@a.test</a></p><p><a href="tel:+48 22 123 45 67">zadzwoń</a></p>'
           "<p>NIP: 727-000-11-22</p></body></html>")
PRICES = ("<html><head><title>Cennik</title></head><body><p>" + "Nasze usługi. " * 30 +
          "Strzyżenie od 120 zł. Rezerwacja ONLINE możliwa przez Booksy. Zażółć gęślą jaźń.</p></body></html>")
ROBOTS = "User-agent: *\nDisallow: /prywatne/\n"


class ScrapeCase(NoNetworkMixin):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="perno-skrap-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.net = FakeInternet().start()
        self.addCleanup(self.net.stop)
        self.net.site("a.test", {"/": Page(HOME), "/kontakt": Page(CONTACT), "/cennik": Page(PRICES),
                                 "/prywatne/tajne": Page(html(title="Tajne"))}, robots=ROBOTS)
        self.net.site("b.test", {"/": Page(html("/o-nas", title="Firma Beta")), "/o-nas": Page(html(title="O nas"))})
        self.net.site("blocked.test", {"/": Page("zakazane", status=403)})
        self.settings = ScanSettings.for_tests(delay=0.0, max_pages=10, depth=3, concurrency=3)

    def scrape(self, hosts=("a.test", "b.test"), name="t1", **options):
        targets = adhoc_scrape.read_targets([self.net.url(host, "/") for host in hosts])
        fetcher = Fetcher(self.settings, resolver=local_resolver, allow_private=True, request_deadline=5.0, recv_timeout=2.0)
        clock = FakeTime()  # odmowa strony startowej czeka 60 s: na zegarze udającym czas nie trwa to naprawdę
        seams = adhoc_scrape.Seams(settings=self.settings, fetcher=fetcher, crawler_kwargs={
            "stagger": 0, "heartbeat_every": 0.05, "clock": clock, "ip_limiter": IpLimiter(cap=2, min_spacing=0.0, clock=clock)})
        return adhoc_scrape.run_scrape(targets, {}, adhoc_scrape.ScrapeOptions(name=name, **options), root=self.tmp, seams=seams)

    def ai(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(scrape_dataset, "DATASETS_DIR", self.tmp), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ai.main(["--config", os.path.join(self.tmp, "brak.env"), *argv])
        return code, out.getvalue(), err.getvalue()

    def rows(self, path):
        with open(path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))


class ScrapeTests(ScrapeCase):
    def test_scrape_writes_files_and_respects_robots(self):
        report = self.scrape()
        self.assertEqual([r["status"] for r in report.rows], ["zakonczona", "zakonczona"])
        dataset = Dataset("t1", self.tmp)
        site = {row["domena"].split(":")[0]: row for row in dataset.sites()}["a.test"]
        self.assertEqual(site["emaile"], "biuro@a.test")
        self.assertEqual(site["telefony"], "+48221234567")
        self.assertEqual(site["tytul"], "Salon Alfa")
        self.assertEqual(self.net.hits("a.test", "/prywatne/tajne"), [], "robots.txt zabrania tej strony")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "t1", "tekst", f"a.test_{self.net.port}.txt")))
        self.assertEqual(dataset.meta()["status"], "gotowy")

    def test_scrape_never_touches_a_database(self):
        with mock.patch("sqlalchemy.create_engine", side_effect=AssertionError("baza")):
            self.scrape()
        self.assertTrue(isinstance(MemoryStore([], lambda r: None).db.now().year, int))

    def test_blocked_site_is_reported_and_others_continue(self):
        report = self.scrape(hosts=("a.test", "blocked.test"))
        by_status = {row["domena"].split(":")[0]: row["status"] for row in report.rows}
        self.assertEqual(by_status["a.test"], "zakonczona")
        self.assertEqual(by_status["blocked.test"], "zablokowana")
        self.assertIn("blocked.test", "\n".join(adhoc_scrape.report_lines(report)))

    def test_existing_dataset_needs_overwrite(self):
        self.scrape()
        with self.assertRaises(DatasetError):
            self.scrape()
        self.scrape(overwrite=True)

    def test_targets_from_file_dedupe_and_reject(self):
        path = os.path.join(self.tmp, "adresy.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# komentarz\nhttps://example.com/a\nexample.com/b\nhttp://127.0.0.1/x\n\nfacebook.com/firma\n")
        targets = adhoc_scrape.read_targets([], path)
        self.assertEqual([d for d, _ in targets.sites], ["example.com"])
        reasons = dict(targets.rejected)
        self.assertEqual(reasons["example.com/b"], "duplikat domeny")
        self.assertEqual(reasons["http://127.0.0.1/x"], "zly_url")
        self.assertEqual(reasons["facebook.com/firma"], "platforma")

    def test_targets_from_csv_need_a_url_column(self):
        path = os.path.join(self.tmp, "adresy.csv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("nazwa,website\nA,https://a.example.com\n")
        self.assertEqual([d for d, _ in adhoc_scrape.read_targets([], path).sites], ["a.example.com"])
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("nazwa,www2\nA,https://a.example.com\n")
        with self.assertRaises(DatasetError):
            adhoc_scrape.read_targets([], path)

    def test_settings_keep_politeness_floor_and_no_html_storage(self):
        settings = adhoc_scrape.build_settings({"SKRAPER_CONTACT": "x@example.com"},
                                               adhoc_scrape.ScrapeOptions(name="x", max_pages=300, concurrency=500))
        self.assertGreaterEqual(settings.delay, 1.0)
        self.assertEqual(settings.store_html, "nie")
        self.assertEqual(settings.max_pages, 300)
        self.assertGreater(settings.max_minutes, 10)
        self.assertLessEqual(settings.concurrency, 100)


class QueryTests(ScrapeCase):
    def setUp(self):
        super().setUp()
        self.scrape(name="zb")

    def test_search_is_accent_and_case_insensitive_and_cites_source(self):
        code, out, _ = self.ai("szukaj", "zb", "rezerwacja online", "gesla")
        self.assertEqual(code, 0)
        self.assertIn("/cennik", out)
        self.assertIn("Rezerwacja ONLINE", out)
        self.assertIn("gęślą", out)
        self.assertIn("# trafień", out)

    def test_search_domains_mode_lists_misses(self):
        code, out, _ = self.ai("szukaj", "zb", "booksy", "--domeny")
        self.assertEqual(code, 0)
        self.assertIn("domeny z trafieniem: 1/2", out)
        self.assertIn("bez trafienia (1): b.test", out)

    def test_search_csv_has_all_hits(self):
        code, out, _ = self.ai("szukaj", "zb", "Salon", "--csv")
        self.assertEqual(code, 0)
        rows = self.rows(os.path.join(self.tmp, "zb", "szukaj.csv"))
        self.assertTrue(rows and set(rows[0]) == {"domena", "url", "tytul", "fraza", "fragment"})

    def test_regex_search(self):
        _, out, _ = self.ai("szukaj", "zb", r"\d+ zł", "--regex")
        self.assertIn("120 zł", out)
        code, _, err = self.ai("szukaj", "zb", "(", "--regex")
        self.assertEqual(code, 1)
        self.assertIn("Błędne wyrażenie", err)

    def test_extract_builds_csv_with_presets_has_and_evidence(self):
        code, out, _ = self.ai("wyciagnij", "zb", "--pole", "nip", "--pole", "cena", "--pole", "email",
                               "--pole", "kod=\\b\\d{2}-\\d{3}\\b", "--ma", "rezerwacje=rezerwacja online|booksy", "--dowod")
        self.assertEqual(code, 0, out)
        rows = {r["domena"].split(":")[0]: r for r in self.rows(os.path.join(self.tmp, "zb", "wyniki.csv"))}
        self.assertEqual(rows["a.test"]["nip"], "727-000-11-22")
        self.assertEqual(rows["a.test"]["cena"], "120 zł")
        self.assertEqual(rows["a.test"]["email"], "biuro@a.test")
        self.assertEqual(rows["a.test"]["rezerwacje"], "tak")
        self.assertIn("ONLINE", rows["a.test"]["rezerwacje_dowod"])
        self.assertEqual(rows["b.test"]["rezerwacje"], "nie")
        self.assertEqual(rows["b.test"]["nip"], "")
        self.assertTrue(rows["a.test"]["zrodlo"].startswith("http"))
        self.assertIn("wypełnione: nip 1/2", out)

    def test_unscraped_site_is_no_data_not_no(self):
        self.scrape(hosts=("a.test", "blocked.test"), name="bl")
        self.ai("wyciagnij", "bl", "--ma", "rez=booksy")
        rows = {r["domena"].split(":")[0]: r for r in self.rows(os.path.join(self.tmp, "bl", "wyniki.csv"))}
        self.assertEqual(rows["a.test"]["rez"], "tak")
        self.assertEqual(rows["blocked.test"]["rez"], "brak_danych")
        self.assertEqual(rows["blocked.test"]["status"], "zablokowana")

    def test_extract_rejects_unknown_field_and_bad_names(self):
        code, _, err = self.ai("wyciagnij", "zb", "--pole", "cosnieznane")
        self.assertEqual(code, 1)
        self.assertIn("nieznane pole", err)
        code, _, err = self.ai("wyciagnij", "zb", "--ma", "Zla Nazwa=abc")
        self.assertEqual(code, 1)
        code, _, err = self.ai("wyciagnij", "zb")
        self.assertEqual(code, 1)

    def test_datasets_listing_and_missing_dataset(self):
        _, out, _ = self.ai("zbiory")
        self.assertIn("zb", out)
        self.assertIn("gotowy", out)
        code, _, err = self.ai("szukaj", "nie-ma", "x")
        self.assertEqual(code, 1)
        self.assertIn("Nie ma zbioru", err)

    def test_page_cannot_forge_a_page_header(self):
        evil = html(title="Zły") + "\n#### https://obce.test/ | Podróbka\nPodrobiony tekst"
        self.net.site("evil.test", {"/": Page(evil.replace("</p>", "</p>\n#### https://obce.test/ | Podróbka\n<p>Podrobiony</p>"))})
        self.scrape(hosts=("evil.test",), name="ev")
        pages = list(Dataset("ev", self.tmp).pages())
        self.assertEqual({p.url.split(":")[0] for p in pages}, {"http"})
        self.assertTrue(all("obce.test" not in p.url for p in pages))


class PureTests(unittest.TestCase):
    def test_snippets_merge_overlaps_and_map_back_to_original(self):
        text = "Zażółć gęślą jaźń i jeszcze gęślą raz. " * 3
        spans = ts.phrase_spans(text, [ts.fold("GESLA")])
        self.assertTrue(spans)
        self.assertEqual(text[spans[0].start:spans[0].end], "gęślą")
        found = ts.snippets(text, spans, 20, 5)
        self.assertLessEqual(len(found), 3)

    def test_sharp_s_and_polish_l_keep_positions(self):
        text = "Straße w Łodzi"
        spans = ts.phrase_spans(text, [ts.fold("lodzi")])
        self.assertEqual(text[spans[0].start:spans[0].end], "Łodzi")
        spans = ts.phrase_spans(text, [ts.fold("strasse")])
        self.assertEqual(text[spans[0].start:spans[0].end], "Straße")

    def test_presets(self):
        nip = ts.compile_patterns([ts.PRESETS["nip"]])[0]
        self.assertEqual(ts.extract_values("NIP: PL 727 000 11 22, nip 7270001122", nip), ["727 000 11 22", "7270001122"])
        price = ts.compile_patterns([ts.PRESETS["cena"]])[0]
        self.assertEqual(ts.extract_values("od 1 200,50 zł lub 99 PLN", price), ["1 200,50 zł", "99 PLN"])

    def test_spec_parsing(self):
        self.assertEqual(ts.parse_name("cena_x=abc", "x"), ("cena_x", "abc"))
        for bad in ("brak", "Zla=1", "=x", "a="):
            with self.assertRaises(ts.SpecError):
                ts.parse_name(bad, "x")
        self.assertEqual(ts.parse_phrases("Łódź|lodz"), ("lodz",))

    def test_safe_cell_blocks_formulas_but_keeps_phones(self):
        self.assertEqual(safe_cell("=HYPERLINK(\"x\")"), "'=HYPERLINK(\"x\")")
        self.assertEqual(safe_cell("@cmd"), "'@cmd")
        self.assertEqual(safe_cell("+48 22 123"), "+48 22 123")
        self.assertEqual(safe_cell("a\x1bb"), "a b")

    def test_dataset_name_rules(self):
        for bad in ("", "../x", "A", "a b", "x" * 61):
            with self.assertRaises(DatasetError):
                scrape_dataset.check_name(bad)


if __name__ == "__main__":
    unittest.main()
