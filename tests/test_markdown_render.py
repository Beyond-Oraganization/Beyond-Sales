"""Markdown instrukcji jako HTML podstrony: kotwice jak na GitHubie, bezpieczne odnośniki, brak surowego HTML i obrazków."""

from __future__ import annotations

import re
import unittest
from urllib.parse import unquote

from perno.services import docs
from perno.services import markdown_render as md

ROUTES = {"README.md": "/dokumentacja", "AI_INSTRUCTIONS.md": "/instrukcje"}


def render(text: str):
    return md.render(text, ROUTES)


class SlugTests(unittest.TestCase):
    def test_slug_follows_the_github_rule_and_keeps_polish_letters(self):
        self.assertEqual(md.slugify("1. WYSZUKAJ [ilość] firm typu [typ] i ZAPISZ ich do skrapera"),
                         "1-wyszukaj-ilość-firm-typu-typ-i-zapisz-ich-do-skrapera")
        self.assertEqual(md.slugify("3. POKAŻ STAN, ZATRZYMAJ SKAN, WZNÓW SKAN, ODRZUĆ"), "3-pokaż-stan-zatrzymaj-skan-wznów-skan-odrzuć")
        self.assertEqual(md.slugify("5a. Automat wysyłki (`automat.py` i zakładka „Automat”)"), "5a-automat-wysyłki-automatpy-i-zakładka-automat")

    def test_slug_is_normalised_so_composed_and_decomposed_letters_match(self):
        self.assertEqual(md.slugify("Ścieżka"), md.slugify("Ścieżka"))

    def test_repeated_headings_get_numbered_ids(self):
        out = render("## Kroki\n\n## Kroki\n\n## Kroki\n")
        self.assertEqual([h.slug for h in out.headings], ["kroki", "kroki-1", "kroki-2"])
        self.assertIn('<h2 id="kroki-2">', out.html)


class HeadingTests(unittest.TestCase):
    def test_the_first_h1_becomes_the_title_and_leaves_the_body(self):
        out = render("# Tytuł dokumentu\n\nAkapit.\n\n# Drugi h1\n")
        self.assertEqual(out.title, "Tytuł dokumentu")
        self.assertNotIn("Tytuł dokumentu", out.html)
        self.assertIn("<h1>Drugi h1</h1>", out.html)

    def test_headings_get_an_id_and_a_hover_anchor(self):
        out = render("## Skraper `scrape.py`\n")
        self.assertIn('<h2 id="skraper-scrapepy">Skraper <code>scrape.py</code>', out.html)
        self.assertIn('<a class="doc-anchor" href="#skraper-scrapepy" aria-label="Odnośnik do tej sekcji">#</a>', out.html)

    def test_toc_has_second_and_third_levels_only(self):
        out = render("# T\n\n## A\n\n### B\n\n#### C\n\n## D\n")
        self.assertEqual([(h.level, h.text) for h in out.toc()], [(2, "A"), (3, "B"), (2, "D")])
        self.assertEqual([h.level for h in out.headings], [2, 3, 4, 2])
        self.assertIn('<h4 id="c">', out.html)  # h4 ma id (odnośnik), ale nie ma go w spisie

    def test_a_heading_with_a_hostile_text_is_escaped_in_the_anchor_and_the_toc_data(self):
        out = render('## <img src=x onerror=alert(1)> "q"\n')
        self.assertNotIn("<img", out.html)
        self.assertNotIn("onerror=alert(1)>", out.html.replace("&lt;img src=x onerror=alert(1)&gt;", ""))


class AnchorTests(unittest.TestCase):
    def test_the_anchor_escapes_its_id_even_though_slugs_never_contain_quotes(self):
        from markdown_it.token import Token

        inline = Token("inline", "", 0)
        inline.children = []
        md._add_anchor(inline, 'a"><x')
        self.assertEqual(inline.children[0].content, '<a class="doc-anchor" href="#a&quot;&gt;&lt;x" aria-label="Odnośnik do tej sekcji">#</a>')


class LinkTests(unittest.TestCase):
    def test_classify_covers_every_kind(self):
        cases = {
            "#sekcja": ("anchor", "#sekcja"),
            "https://example.com/a": ("external", "https://example.com/a"),
            "http://example.com": ("external", "http://example.com"),
            "README.md": ("doc", "/dokumentacja"),
            "docs/README.md#5-skraper": ("doc", "/dokumentacja#5-skraper"),
            "src/perno/web/page.py": ("none", ""),
            "mailto:ktos@example.com": ("none", ""),
            "javascript:alert(1)": ("none", ""),
        }
        for href, expected in cases.items():
            with self.subTest(href=href):
                self.assertEqual(md.classify_link(href, ROUTES), expected)

    def test_external_links_open_in_a_new_tab_without_opener(self):
        out = render("[uv](https://docs.astral.sh/uv/)")
        self.assertIn('<a href="https://docs.astral.sh/uv/" target="_blank" rel="noopener noreferrer">uv</a>', out.html)

    def test_links_to_other_docs_point_at_their_pages(self):
        out = render("Zobacz [instrukcję](AI_INSTRUCTIONS.md#4-zeskrapuj).")
        self.assertIn('<a href="/instrukcje#4-zeskrapuj">instrukcję</a>', out.html)

    def test_links_to_files_the_panel_does_not_serve_stay_plain_text(self):
        out = render("Plik [page.py](src/perno/web/page.py) i [zły](javascript:alert(1)) oraz [bez](mailto:a@b.pl).")
        self.assertNotIn("<a ", out.html.replace('class="doc-anchor"', ""))
        self.assertNotIn("href=", out.html)
        self.assertIn("<span>page.py</span>", out.html)
        self.assertNotIn("javascript:", out.html.replace("[zły](javascript:alert(1))", ""))

    def test_a_plain_link_followed_by_a_real_one_keeps_the_real_one(self):
        out = render("[a](src/x.py) i [b](#cel)")
        self.assertIn("<span>a</span>", out.html)
        self.assertIn('<a href="#cel">b</a>', out.html)

    def test_in_page_anchor_links_with_polish_letters_resolve_to_heading_ids(self):
        out = render("[1](#1-wyszukaj-ilość-firm)\n\n## 1. WYSZUKAJ [ilość] firm\n")
        target = re.search(r'<a href="#([^"]+)">1</a>', out.html).group(1)
        self.assertIn(unquote(target), {h.slug for h in out.headings})


class SafetyTests(unittest.TestCase):
    def test_raw_html_is_escaped(self):
        out = render("Tekst <script>alert(1)</script> i <b onclick=x>pogrubienie</b>.\n\n<div>blok</div>\n")
        self.assertNotIn("<script", out.html)
        self.assertNotIn("<b ", out.html)
        self.assertNotIn("<div>", out.html.replace('<div class="', ""))
        self.assertIn("&lt;script&gt;", out.html)

    def test_images_are_not_rendered(self):
        out = render("![logo](https://example.com/x.png) i ![lokalny](img/a.png)")
        self.assertNotIn("<img", out.html)

    def test_code_is_escaped(self):
        out = render("```html\n<script>alert('x')</script>\n```\n")
        self.assertIn("&lt;script&gt;alert(", out.html)
        self.assertNotIn("<script", out.html)


class BlocksTests(unittest.TestCase):
    def test_tables_are_wrapped_for_horizontal_scrolling_and_keep_escaped_pipes(self):
        out = render("| a | b |\n|---|---|\n| `x\\|y` | tekst |\n")
        self.assertIn('<div class="table-wrap doc-table"><table>', out.html)
        self.assertIn("</table></div>", out.html)
        self.assertIn("<code>x|y</code>", out.html)

    def test_code_blocks_get_a_hidden_copy_button(self):
        out = render("```bash\nuv sync\n```\n")
        self.assertIn('<div class="doc-code"><pre><code class="language-bash">uv sync\n</code></pre>', out.html)
        self.assertIn('<button type="button" class="btn doc-copy" data-copy-code hidden aria-label="Kopiuj blok kodu">Kopiuj</button></div>', out.html)

    def test_lists_nested_lists_and_quotes_render(self):
        out = render("- a\n  - zagnieżdżony\n- b\n\n1. jeden\n2. dwa\n\n> cytat\n")
        for needle in ("<ul>", "<ol>", "<blockquote>", "zagnieżdżony"):
            self.assertIn(needle, out.html)

    def test_words_count_prose_and_code(self):
        self.assertEqual(render("Jeden dwa trzy.\n\n```\nczte ry\n```\n").words, 5)
        self.assertEqual(render("").words, 0)

    def test_the_parser_is_reusable_across_calls(self):
        first, second = render("## A\n"), render("## A\n")
        self.assertEqual(first.html, second.html)
        self.assertEqual([h.slug for h in second.headings], ["a"])  # numeracja powtórzeń nie przecieka między dokumentami


class RealDocumentsTests(unittest.TestCase):
    """Pliki projektu: ich odnośniki wewnętrzne mają prowadzić do istniejących nagłówków."""

    def test_every_in_page_link_in_every_document_resolves(self):
        from perno import settings

        for spec in docs.DOCS:
            with self.subTest(file=spec.file):
                document = docs.load(spec, settings.BASE_DIR)
                self.assertIsNotNone(document, spec.file)
                ids = {h.slug for h in document.rendered.headings}
                hrefs = re.findall(r'<a href="#([^"]+)"', document.rendered.html)
                broken = [h for h in hrefs if unquote(h) not in ids]
                self.assertEqual(broken, [])

    def test_the_instructions_have_the_sections_the_home_page_links_to(self):
        from perno import settings

        document = docs.load(docs.spec_for(docs.INSTRUCTIONS_KEY), settings.BASE_DIR)
        self.assertGreaterEqual(document.sections, 11)
        self.assertTrue(document.rendered.title)


if __name__ == "__main__":
    unittest.main()
