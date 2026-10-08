"""services/scrape_query.py na ręcznie zbudowanym zbiorze (bez sieci i crawlera): liczniki, limity wypisu, kolumny tabeli."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from perno.domain import text_search as ts
from perno.scraper import statuses as st
from perno.scraper.memory_store import PageEntry, SiteResult
from perno.services import scrape_query as sq
from perno.services.scrape_dataset import Dataset, DatasetError


def build(root: str, sites: dict, name: str = "zb", status: str = st.FIRMA_ZAKONCZONA) -> Dataset:
    dataset = Dataset(name, root)
    dataset.create({"domen": len(sites)})
    for domain, pages in sites.items():
        entries = [PageEntry(index, url, 0, 0, "link", status=st.STRONA_POBRANO, http_status=200, title=title, text=text)
                   for index, (url, title, text) in enumerate(pages, start=1)]
        dataset.append_site(SiteResult(domain, f"https://{domain}/", status if pages else st.FIRMA_ZABLOKOWANA, pages=entries))
    return dataset


SITES = {
    "a.pl": [("https://a.pl/", "A", "Cennik: strzyżenie 50 zł. Rezerwacja online. Rezerwacja online przez telefon."),
             ("https://a.pl/cennik", "Cennik A", "Farbowanie 120 zł, 130 zł, 140 zł, 150 zł. NIP 727-000-11-22"),
             ("https://a.pl/kontakt", "Kontakt A", "Zadzwoń. Rezerwacja online dostępna.")],
    "b.pl": [("https://b.pl/", "B", "Nic o tym tutaj.")],
    "c.pl": [],
}


class QueryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perno-query-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dataset = build(self.tmp, SITES)


class SearchTests(QueryCase):
    def test_counts_per_domain_pages_and_first_url(self):
        result = sq.search(self.dataset, ["rezerwacja online"])
        self.assertEqual(result.domains, ["a.pl", "b.pl", "c.pl"])
        self.assertEqual(result.per_domain, {"a.pl": 2})  # sąsiednie trafienia na stronie głównej scalają się w jeden fragment
        self.assertEqual(result.pages_hit, {"a.pl": 2})
        self.assertEqual(result.first_url, {"a.pl": "https://a.pl/"})

    def test_fragments_per_page_are_capped(self):
        text = " ".join(f"zapis{n} " + "x" * 300 for n in range(6))
        dataset = build(self.tmp, {"d.pl": [("https://d.pl/", "D", text)]}, name="duzo")
        self.assertEqual(sq.search(dataset, ["zapis"], width=10).per_domain["d.pl"], 2)
        self.assertEqual(sq.search(dataset, ["zapis"], width=10, per_page=4).per_domain["d.pl"], 4)

    def test_domain_filter_limits_domains_and_pages(self):
        result = sq.search(self.dataset, ["online"], domain="b.pl")
        self.assertEqual(result.domains, ["b.pl"])
        self.assertEqual(result.hits, [])

    def test_terms_are_literal_phrases_unless_regex_is_requested(self):
        self.assertEqual(sq.search(self.dataset, ["(brak"]).hits, [])  # nawias nie jest wzorcem
        self.assertEqual(len(sq.search(self.dataset, [r"\d+ zł"], regex=True).hits), 2)

    def test_no_terms_is_an_error(self):
        with self.assertRaises(DatasetError):
            sq.search(self.dataset, [])

    def test_domain_rows_include_domains_without_hits(self):
        rows = sq.domain_rows(sq.search(self.dataset, ["rezerwacja online"]))
        self.assertEqual(rows[1], ["b.pl", 0, 0, ""])
        self.assertEqual(rows[0], ["a.pl", 2, 2, "https://a.pl/"])


class HitLineTests(QueryCase):
    def setUp(self):
        super().setUp()
        pages = [(f"https://m.pl/p{n}", f"M{n}", f"trafienie {n} " + "x" * 200) for n in range(5)]
        pages2 = [(f"https://n.pl/p{n}", f"N{n}", f"trafienie {n} " + "x" * 200) for n in range(5)]
        self.result = sq.search(build(self.tmp, {"m.pl": pages, "n.pl": pages2}, name="dwa"), ["trafienie"], width=10)

    def test_per_domain_cap_limit_and_skip(self):
        lines, shown = sq.hit_lines(self.result, limit=100, per_domain=2)
        self.assertEqual((shown, len(lines)), (4, 4))
        self.assertEqual(sum(line.startswith("[m.pl]") for line in lines), 2)
        lines, shown = sq.hit_lines(self.result, limit=3, per_domain=5)
        self.assertEqual(shown, 3)
        later, _ = sq.hit_lines(self.result, limit=3, per_domain=5, skip=3)
        self.assertEqual(len(later), 3)
        self.assertFalse(set(lines) & set(later))

    def test_line_format_has_domain_path_and_snippet(self):
        lines, _ = sq.hit_lines(self.result, limit=1, per_domain=1)
        self.assertRegex(lines[0], r"^\[m\.pl\] /p0 — .*trafienie 0")

    def test_header_reports_totals(self):
        header = sq.search_header(self.result, 4, 2)
        self.assertIn("trafień 10 w 2/2 domenach", header)
        self.assertIn("pokazano 4 (do 2 na domenę)", header)

    def test_control_characters_are_removed_from_output(self):
        self.assertEqual(sq.clean("a\x1b[31mb\x07"), "a [31mb ")


class ExtractTests(QueryCase):
    def test_regex_values_accumulate_across_pages_up_to_the_limit(self):
        columns = sq.build_columns(["cena"], [])
        header, rows, filled = sq.extract_table(self.dataset, columns)
        by_domain = {row[0]: row for row in rows}
        self.assertEqual(header, ["domena", "status", "cena", "zrodlo"])
        self.assertEqual(by_domain["a.pl"][2], "50 zł | 120 zł | 130 zł")  # najwyżej 3 wartości
        self.assertEqual(by_domain["a.pl"][3], "https://a.pl/")
        self.assertEqual(by_domain["b.pl"][2:], ["", ""])
        self.assertEqual(filled, {"cena": 1})

    def test_custom_regex_field_uses_group_one(self):
        columns = sq.build_columns([r"nip_dash=NIP (\d{3})-"], [])
        _, rows, _ = sq.extract_table(self.dataset, columns)
        self.assertEqual({row[0]: row[2] for row in rows}["a.pl"], "727")

    def test_source_is_the_first_page_that_matched_anything(self):
        columns = sq.build_columns(["nip"], [])
        _, rows, _ = sq.extract_table(self.dataset, columns)
        self.assertEqual({row[0]: row[3] for row in rows}["a.pl"], "https://a.pl/cennik")

    def test_has_columns_say_tak_nie_and_no_data(self):
        columns = sq.build_columns([], ["rez=rezerwacja online|booksy", "bk=booksy"])
        header, rows, filled = sq.extract_table(self.dataset, columns)
        self.assertEqual(header, ["domena", "status", "rez", "bk", "zrodlo"])
        by_domain = {row[0]: row for row in rows}
        self.assertEqual(by_domain["a.pl"][2:4], ["tak", "nie"])
        self.assertEqual(by_domain["b.pl"][2:4], ["nie", "nie"])
        self.assertEqual(by_domain["c.pl"][1:4], ["zablokowana", sq.NO_DATA, sq.NO_DATA])
        self.assertEqual(filled, {"rez": 1, "bk": 0})

    def test_evidence_adds_a_snippet_column_only_when_asked(self):
        columns = sq.build_columns([], ["rez=rezerwacja online"])
        header, rows, _ = sq.extract_table(self.dataset, columns, evidence=True)
        self.assertEqual(header[2:4], ["rez", "rez_dowod"])
        self.assertIn("Rezerwacja online", {row[0]: row for row in rows}["a.pl"][3])
        header, _, _ = sq.extract_table(self.dataset, columns)
        self.assertNotIn("rez_dowod", header)

    def test_site_columns_come_from_the_scan_table(self):
        columns = sq.build_columns(["tytul"], [])
        _, rows, filled = sq.extract_table(self.dataset, columns)
        self.assertEqual({row[0]: row[2] for row in rows}["a.pl"], "A")
        self.assertEqual(filled, {"tytul": 2})

    def test_column_spec_errors(self):
        for fields, has in (([], []), (["nip", "nip"], []), (["nieznane"], []), ([], ["zla nazwa=x"]), (["x=("], [])):
            with self.assertRaises((DatasetError, ts.SpecError), msg=(fields, has)):
                sq.build_columns(fields, has)

    def test_tables_are_written_with_formula_guard(self):
        path = os.path.join(self.tmp, "t.csv")
        sq.write_table(path, ["a", "b"], [["=SUM(1)", "ok"]])
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read().splitlines(), ["a,b", "'=SUM(1),ok"])


if __name__ == "__main__":
    unittest.main()
