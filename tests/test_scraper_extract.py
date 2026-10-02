from __future__ import annotations

import subprocess
import sys
import unittest

from perno.scraper import extract
from perno.scraper.extract import nofollow_requested, parse_html

BASE = "https://firma.pl/kontakt/"


def parse(body: str, base: str = BASE):
    return parse_html(body, base)


class TextAndMetadataTests(unittest.TestCase):
    def test_title_description_canonical_lang_robots(self):
        data = parse("""<html lang="PL-pl"><head><title>  Firma  –
            Kontakt </title><meta name="Description" content=" Opis   firmy ">
            <link rel="canonical" href="/kontakt"><meta name="robots" content="Noindex, NoFollow"></head>
            <body><p>Treść</p></body></html>""")
        self.assertEqual(data.title, "Firma – Kontakt")
        self.assertEqual(data.description, "Opis firmy")
        self.assertEqual(data.canonical, "https://firma.pl/kontakt")
        self.assertEqual(data.lang, "pl-pl")
        self.assertEqual(data.meta_robots, frozenset({"noindex", "nofollow"}))

    def test_title_is_read_like_a_browser_even_when_it_contains_markup(self):
        for body, expected in (
            ("<title><script>alert(1)</script> Tytuł</title>", "<script>alert(1)</script> Tytuł"),
            ('<title>"><img src=x onerror=alert(1)></title>', '"><img src=x onerror=alert(1)>'),
            ("<title>A &amp; B &lt;3</title>", "A & B <3"),
            ("<title>\n  Wielo\n linijkowy </title>", "Wielo linijkowy"),
        ):
            self.assertEqual(parse(f"<html><head>{body}</head><body>x</body></html>").title, expected, body)

    def test_open_graph_fallbacks(self):
        data = parse('<html><head><meta property="og:title" content="OG tytuł"><meta property="og:description" content="OG opis"></head></html>')
        self.assertEqual((data.title, data.description), ("OG tytuł", "OG opis"))

    def test_text_drops_scripts_styles_and_keeps_block_structure(self):
        data = parse("""<html><head><title>T</title><style>.a{color:red}</style></head><body>
            <script>var x = "info@ukryty.pl";</script><noscript>no js</noscript><svg><text>ikona</text></svg>
            <h1>Nagłówek</h1><p>Pierwszy   akapit</p><div>Drugi<br>wiersz</div>
            <ul><li>Jeden</li><li>Dwa</li></ul><iframe src="/x">w ramce</iframe></body></html>""")
        self.assertEqual(data.text, "Nagłówek\nPierwszy akapit\nDrugi\nwiersz\nJeden\nDwa")

    def test_inline_elements_are_glued_so_split_emails_survive(self):
        data = parse('<p>Napisz: <span>info</span>@<span>firma.pl</span> lub <b>zadzwoń</b></p>')
        self.assertEqual(data.text, "Napisz: info@firma.pl lub zadzwoń")

    def test_entities_are_decoded_and_nbsp_collapsed(self):
        data = parse("<p>Tom&nbsp;&amp;&nbsp;Jerry&#64;x.pl &lt;3</p>")
        self.assertEqual(data.text, "Tom & Jerry@x.pl <3")

    def test_text_is_capped(self):
        data = parse("<p>" + "a" * (extract.MAX_TEXT_CHARS + 500) + "</p>")
        self.assertEqual(len(data.text), extract.MAX_TEXT_CHARS)
        self.assertTrue(data.truncated)

    def test_garbage_and_empty_input_never_raise(self):
        for body in ("", "   ", "<", "<<<>>>", "\x00\x01<p", "plain text only", "<html><body><div>" * 50):
            data = parse(body)
            self.assertIsInstance(data.text, str)

    def test_very_deep_dom_is_handled_without_crashing(self):
        data = parse("<div>" * 3000 + "treść" + "</div>" * 3000)
        self.assertIsInstance(data.text, str)  # albo tekst, albo błąd zbyt_gleboki_dom — ale bez wyjątku


class LinkTests(unittest.TestCase):
    def test_links_resolve_against_base_and_skip_non_navigation_schemes(self):
        data = parse("""<body>
            <a href="/o-nas">O nas</a><a href="zespol">Zespół</a><a href="https://inna.pl/x">Obca</a>
            <a href="#top">Góra</a><a href="javascript:void(0)">js</a><a href="mailto:a@b.pl">mail</a>
            <a href="tel:+48221234567">tel</a><a href="data:text/html,x">d</a><a href="">pusty</a>
            <a name="kotwica">bez href</a><area href="/mapa-strony" alt="m"></body>""")
        self.assertEqual([link.href for link in data.links],
                         ["https://firma.pl/o-nas", "https://firma.pl/kontakt/zespol", "https://inna.pl/x",
                          "https://firma.pl/mapa-strony"])
        self.assertEqual(data.mailtos, ["mailto:a@b.pl"])
        self.assertEqual(data.tels, ["tel:+48221234567"])

    def test_base_tag_changes_resolution(self):
        data = parse('<head><base href="https://cdn.firma.pl/baza/"></head><a href="x">x</a>')
        self.assertEqual(data.links[0].href, "https://cdn.firma.pl/baza/x")

    def test_unsafe_base_tag_is_ignored(self):
        data = parse('<head><base href="javascript:alert(1)"></head><a href="/x">x</a>')
        self.assertEqual(data.links[0].href, "https://firma.pl/x")

    def test_location_nofollow_and_anchor_text(self):
        data = parse("""<body><header><a href="/a">Start</a></header>
            <nav><a href="/b">  Oferta
            i cennik </a></nav><div role="navigation"><a href="/c">R</a></div>
            <main><a href="/d" rel="nofollow noopener">Treść</a></main>
            <div class="site-footer"><a href="/e">Stopka</a></div><footer><nav><a href="/f">F</a></nav></footer></body>""")
        by_href = {link.href.rsplit("/", 1)[1]: link for link in data.links}
        self.assertEqual({k: v.where for k, v in by_href.items()},
                         {"a": "header", "b": "nav", "c": "nav", "d": "body", "e": "footer", "f": "nav"})
        self.assertEqual(by_href["b"].text, "Oferta i cennik")
        self.assertTrue(by_href["d"].nofollow)
        self.assertFalse(by_href["a"].nofollow)

    def test_link_cap(self):
        body = "".join(f'<a href="/p{i}">p</a>' for i in range(extract.MAX_LINKS_PER_PAGE + 50))
        data = parse(body)
        self.assertEqual(len(data.links), extract.MAX_LINKS_PER_PAGE)
        self.assertTrue(data.truncated)


class ContactSignalTests(unittest.TestCase):
    def test_cloudflare_signals_are_collected(self):
        data = parse('<a href="/cdn-cgi/l/email-protection#4a2b2e272e6f">x</a><span class="__cf_email__" data-cfemail="1f7e7b727b3a">[email&#160;protected]</span>'
                     '<a href="/cdn-cgi/l/email-protection#zz">zły</a>')
        self.assertEqual(sorted(data.cfemails), ["1f7e7b727b3a", "4a2b2e272e6f"])

    def test_embeds_are_recorded_before_iframes_are_stripped(self):
        data = parse('<iframe src="https://tally.so/embed/abc"></iframe><iframe src="/local"></iframe>')
        self.assertEqual(data.embeds, ["https://tally.so/embed/abc", "https://firma.pl/local"])

    def test_forms_are_described(self):
        data = parse("""<form id="wpcf7-f1" class="wpcf7-form" action="/wp-json/x"><input type="text" name="your-name">
            <input type="email" name="your-email"><textarea name="your-message"></textarea></form>
            <form role="search" action="/"><input type="search" name="s"></form>
            <form class="login"><input type="email" name="user"><input type="password" name="pw"></form>""")
        contact, search, login = data.forms
        self.assertTrue(contact.has_email and contact.has_textarea and not contact.is_search)
        self.assertIn("wpcf7", contact.ident)
        self.assertTrue(search.is_search)
        self.assertTrue(login.has_password)

    def test_jsonld_graph_is_flattened_and_bad_blocks_skipped(self):
        data = parse("""<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
              {"@type":"Organization","name":"Firma","email":"biuro@firma.pl"},{"@type":"WebSite","url":"https://firma.pl"}]}</script>
            <script type="application/ld+json">{ to nie jest json }</script>
            <script type="application/ld+json">[{"@type":"Person","telephone":"+48 22 123 45 67"}]</script>""")
        types = sorted(node.get("@type") for node in data.jsonld)
        self.assertEqual(types, ["Organization", "Person", "WebSite"])


class RobotsDirectiveTests(unittest.TestCase):
    def test_meta_tokens(self):
        self.assertTrue(nofollow_requested({"nofollow"}))
        self.assertTrue(nofollow_requested({"NONE"}))
        self.assertFalse(nofollow_requested({"noindex"}))
        self.assertFalse(nofollow_requested(set()))

    def test_x_robots_tag(self):
        self.assertTrue(nofollow_requested(set(), "noindex, nofollow"))
        self.assertTrue(nofollow_requested(set(), "none"))
        self.assertFalse(nofollow_requested(set(), "noindex"))
        self.assertFalse(nofollow_requested(set(), "googlebot: nofollow"))  # dyrektywa dla innego bota
        self.assertTrue(nofollow_requested(set(), "PernoContextBot: nofollow"))
        self.assertTrue(nofollow_requested(set(), "googlebot: noindex\n*: nofollow"))
        self.assertFalse(nofollow_requested(set(), "unavailable_after: 25 Jun 2030 15:00:00 PST"))


class ImportIsolationTests(unittest.TestCase):
    def test_importing_extract_and_pages_does_not_import_bs4(self):
        code = ("import sys; import perno.scraper.extract, perno.scraper.contacts; "
                "assert 'bs4' not in sys.modules, 'bs4 zaimportowany przy imporcie'; "
                "import perno.web.pages; assert 'bs4' not in sys.modules")
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
