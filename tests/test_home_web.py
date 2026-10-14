"""Strona główna „Beyond Sales” i podstrony z dokumentacją w panelu: liczniki, polecenia, odnośniki, escapowanie i menu."""

from __future__ import annotations

import html as htmllib
import os
import re
import tempfile
import unittest
from unittest import mock

from perno import settings
from perno.services import docs
from perno.web import layout
from perno.web.pages import home
from perno.web.pages.docs import MENU_KEY
from tests.fixtures import put, sample_row
from tests.test_web import WebCase


def hrefs(page_html: str) -> list:
    return re.findall(r'href="([^"]*)"', page_html)


class HomeCase(WebCase):
    def get(self, path="/"):
        resp = self.call("GET", path)
        self.assertEqual(resp.status, 200, resp.body[:300])
        return resp.body.decode("utf-8")

    def routes(self) -> set:
        return {path for method, path in self.app.routes if method == "GET"}


class HomePageTests(HomeCase):
    def test_the_page_shows_the_name_and_the_brand_links_home(self):
        page = self.get("/")
        self.assertIn("<title>Beyond Sales — Strona główna</title>", page)
        self.assertIn('<h1 class="hm-title">Beyond Sales</h1>', page)
        self.assertIn('<a class="topnav-brand" href="/" aria-current="page">Beyond Sales</a>', page)

    def test_the_brand_is_not_current_on_other_pages_and_every_page_carries_the_name(self):
        page = self.get("/wysylka")
        self.assertIn('<a class="topnav-brand" href="/">Beyond Sales</a>', page)
        self.assertIn("<title>Beyond Sales — Wysyłka</title>", page)

    def test_the_menu_has_no_segment_for_the_home_page_but_ends_with_help(self):
        page = self.get("/")
        self.assertNotIn('class="topnav-link" href="/"', page)
        labels = re.findall(r'class="topnav-link"[^>]*>([^<]+)', page)
        self.assertEqual(labels[0], "Wysyłka")
        self.assertEqual(labels[-1], "Pomoc")

    def test_every_internal_link_leads_to_a_registered_page(self):
        page = self.get("/")
        known = self.routes()
        internal = {href.split("#")[0] for href in hrefs(page) if href.startswith("/")}
        self.assertTrue(internal)
        self.assertEqual(sorted(internal - known), [])

    def test_the_five_steps_link_to_the_tabs_in_order(self):
        page = self.get("/")
        steps = re.findall(r'<a class="hm-step" href="([^"]+)"', page)
        self.assertEqual(steps, ["/skraper", "/wysylka", "/automat", "/odpowiedzi", "/leady"])
        self.assertEqual(re.findall(r'<span class="hm-num" aria-hidden="true">(\d)</span>', page), ["1", "2", "3", "4", "5"])

    def test_the_hero_buttons_lead_to_the_mailing_tab_and_the_instructions(self):
        page = self.get("/")
        self.assertIn('class="btn primary hm-btn" href="/wysylka"', page)
        self.assertIn('class="btn plain hm-btn" href="/instrukcje"', page)

    def test_a_doc_card_exists_for_every_document_with_its_size(self):
        page = self.get("/")
        for spec in docs.DOCS:
            self.assertIn(f'<a class="hm-doc" href="{spec.path}">', page)
            self.assertIn(f'<code class="hm-doc-file">{spec.file}</code>', page)
        self.assertRegex(page, r"\d+ sekc[a-z]+ · ok\. \d+ min")

    def test_the_page_works_without_script_and_data_comes_from_the_server(self):
        page = self.get("/")
        self.assertIn('data-copy="@', page)
        self.assertIn("bindCopyButtons();", page)


class FiguresTests(HomeCase):
    def figure(self, page: str, label: str) -> str:
        found = re.search(r'<span class="hm-fig([^"]*)"><b>([^<]*)</b><span>' + re.escape(label) + "</span>", page)
        self.assertIsNotNone(found, label)
        return found.group(2)

    def test_counters_read_the_queue(self):
        put(self.database, sample_row(1), sample_row(2), sample_row(3, status="oczekuje_na_tresc"),
            sample_row(4, status="wyslano", sent_at="2026-09-30T08:00:00"))
        page = self.get("/")
        self.assertEqual(self.figure(page, "firm w bazie"), "4")
        self.assertEqual(self.figure(page, "do wysłania"), "2")
        self.assertEqual(self.figure(page, "czeka na treść"), "1")
        self.assertEqual(self.figure(page, "wysłano dziś"), "1")
        self.assertIn('<span class="hm-fig"><b>4</b><span>firm w bazie</span>', page)  # zwykła liczba nie jest przygaszona ani „brak”

    def test_an_empty_database_shows_zeros_dimmed(self):
        page = self.get("/")
        self.assertEqual(self.figure(page, "do wysłania"), "0")
        self.assertIn('<span class="hm-fig is-zero"><b>0</b><span>do wysłania</span>', page)

    def test_thousands_use_a_non_breaking_space(self):
        self.assertEqual(home._number(12345), "12 345")
        self.assertEqual(home._number(0), "0")
        self.assertEqual(home._number(None), "—")

    def test_a_failing_counter_shows_a_dash_and_never_breaks_the_page(self):
        put(self.database, sample_row(1))
        with mock.patch.object(self.database, "status_counts", side_effect=RuntimeError("baza padła")):
            with self.assertLogs("perno.web.pages.home", level="WARNING") as logged:
                page = self.get("/")
        self.assertIn("statusy maili", logged.records[0].getMessage())
        self.assertIsNotNone(logged.records[0].exc_info)  # przyczyna awarii jest w logu
        self.assertEqual(self.figure(page, "do wysłania"), "—")
        self.assertEqual(self.figure(page, "czeka na treść"), "—")
        self.assertEqual(self.figure(page, "firm w bazie"), "1")  # pozostałe liczniki działają
        self.assertIn('<span class="hm-fig is-none">', page)

    def test_the_reply_and_lead_counters_come_from_their_stores(self):
        self.app.ctx.replies_store.get().unread_total = lambda: 7  # type: ignore[method-assign]
        self.app.ctx.leads_store.get().pending_total = lambda: 3  # type: ignore[method-assign]
        page = self.get("/")
        self.assertEqual(self.figure(page, "nieprzeczytanych"), "7")
        self.assertEqual(self.figure(page, "do uzupełnienia"), "3")


class CommandsTests(HomeCase):
    def test_every_command_of_the_instructions_is_listed_with_its_section_link(self):
        page = self.get("/")
        listed = re.findall(r'data-copy="([^"]+)" data-copied="Skopiowano polecenie\."', page)
        document = docs.load(docs.spec_for(docs.INSTRUCTIONS_KEY), settings.BASE_DIR)
        self.assertEqual([htmllib.unescape(item) for item in listed], [c.text for c in docs.commands(document.text)])
        self.assertIn('href="/instrukcje#1-wyszukaj-ilość-firm-typu-typ-i-zapisz-ich-do-skrapera"', page)

    def test_placeholders_are_highlighted(self):
        page = self.get("/")
        self.assertIn('<span class="hm-ph">[ilość]</span> firm typu <span class="hm-ph">[typ]</span>', page)
        self.assertIn("<code>POKAŻ STAN</code>", page)  # polecenie bez parametrów nie ma wyróżnień

    def test_the_attach_reference_is_relative_to_the_parent_folder(self):
        page = self.get("/")
        expected = "@" + os.path.relpath(os.path.join(settings.BASE_DIR, "AI_INSTRUCTIONS.md"), settings.PERNO_DIR)
        self.assertIn(f'data-copy="{expected}"', page)

    def test_hostile_text_in_the_instructions_is_escaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "AI_INSTRUCTIONS.md"), "w", encoding="utf-8") as handle:
                handle.write('# T\n\n| Komenda | Sekcja |\n|---|---|\n| `X "><img src=x onerror=alert(1)> [a]` | [1](#1-x) |\n\n## 1. X\n')
            with mock.patch.object(settings, "BASE_DIR", tmp):
                page = self.get("/")
        self.assertNotIn("<img", page)
        self.assertIn("&quot;&gt;&lt;img src=x onerror=alert(1)&gt;", page)

    def test_a_missing_instructions_file_leaves_a_notice_and_the_page_still_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(settings, "BASE_DIR", tmp):
                page = self.get("/")
        self.assertIn("Nie znaleziono tabeli poleceń", page)
        self.assertIn("nie znaleziono pliku", page)


class DocPageTests(HomeCase):
    def test_every_document_has_a_page_with_title_toc_and_content(self):
        for spec in docs.DOCS:
            with self.subTest(page=spec.path):
                page = self.get(spec.path)
                self.assertIn(f"<title>Beyond Sales — {spec.title}</title>", page)
                self.assertIn(f"<h1>{spec.title}</h1>", page)
                self.assertIn('<details class="doc-toc" id="docToc" open>', page)
                self.assertIn('<article class="doc-body" id="docBody">', page)

    def test_every_document_page_highlights_help_in_the_menu_and_only_the_first_document_has_a_menu_entry(self):
        for spec in docs.DOCS:
            with self.subTest(page=spec.path):
                page = self.get(spec.path)
                self.assertIn('<a class="topnav-link" href="/instrukcje" aria-current="page">Pomoc</a>', page)
                for other in docs.DOCS[1:]:
                    self.assertNotIn(f'class="topnav-link" href="{other.path}"', page)

    def test_the_tabs_switch_between_documents_and_mark_the_current_one(self):
        page = self.get("/dokumentacja")
        tabs = re.findall(r'<a class="doc-tab" href="([^"]+)"( aria-current="page")?>([^<]+)</a>', page)
        self.assertEqual([t[0] for t in tabs], [spec.path for spec in docs.DOCS])
        self.assertEqual([t[2] for t in tabs if t[1]], ["Dokumentacja"])

    def test_the_toc_links_match_the_heading_ids_in_the_content(self):
        page = self.get("/instrukcje")
        toc = re.findall(r'<a class="doc-toc-link[^"]*" href="#([^"]+)"', page)
        self.assertGreaterEqual(len(toc), 11)
        for slug in toc:
            self.assertIn(f'id="{htmllib.unescape(slug)}"', htmllib.unescape(page))

    def test_the_file_button_copies_the_reference_for_claude_code(self):
        page = self.get("/instrukcje")
        expected = "@" + os.path.relpath(os.path.join(settings.BASE_DIR, "AI_INSTRUCTIONS.md"), settings.PERNO_DIR)
        self.assertIn(f'<button type="button" class="doc-file" data-copy="{expected}"', page)

    def test_the_header_shows_sections_and_reading_time_with_polish_forms(self):
        page = self.get("/zasady-maili")
        self.assertRegex(page, r"<span>\d+ sekc(?:ja|je|ji)</span><span>ok\. \d+ min czytania</span>")

    def test_a_missing_file_is_a_404_with_the_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(settings, "BASE_DIR", tmp):
                resp = self.call("GET", "/instrukcje")
        self.assertEqual(resp.status, 404)
        self.assertIn("AI_INSTRUCTIONS.md", resp.body.decode("utf-8"))

    def test_the_menu_key_is_the_first_document(self):
        self.assertEqual(MENU_KEY, docs.DOCS[0].key)
        self.assertEqual(layout.HOME_KEY, "start")

    def test_an_edit_of_the_file_shows_up_without_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "AI_INSTRUCTIONS.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# T\n\n## Pierwsza\n")
            os.utime(path, (1_700_000_000, 1_700_000_000))
            with mock.patch.object(settings, "BASE_DIR", tmp):
                self.assertIn('id="pierwsza"', self.get("/instrukcje"))
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("# T\n\n## Druga\n")
                os.utime(path, (1_700_000_100, 1_700_000_100))
                self.assertIn('id="druga"', self.get("/instrukcje"))


class PageRegistryTests(unittest.TestCase):
    def test_the_home_page_is_the_root_and_the_mailing_tab_moved(self):
        from perno.web.pages import PAGES

        by_key = {page.key: page for page in PAGES}
        self.assertEqual(by_key["start"].path, "/")
        self.assertFalse(by_key["start"].in_nav)
        self.assertEqual(by_key["wysylka"].path, "/wysylka")

    def test_the_document_pages_are_not_in_the_menu_except_the_first(self):
        from perno.web.pages import PAGES

        in_menu = [page.key for page in PAGES if page.key in {spec.key for spec in docs.DOCS} and page.in_nav]
        self.assertEqual(in_menu, ["instrukcje"])


if __name__ == "__main__":
    unittest.main()
