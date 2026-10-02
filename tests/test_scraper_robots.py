from __future__ import annotations

import unittest
import urllib.robotparser

from lib.scraper import robots
from lib.scraper.robots import parse_robots


def allowed(text, path, token="PernoContextBot"):
    return parse_robots(text, token).allowed(path)


class MatchingTests(unittest.TestCase):
    def test_longest_match_wins_and_allow_wins_ties(self):
        text = "User-agent: *\nDisallow: /a/\nAllow: /a/b/\n"
        self.assertTrue(allowed(text, "/a/b/x"))
        self.assertFalse(allowed(text, "/a/x"))
        self.assertTrue(allowed("User-agent: *\nDisallow: /x\nAllow: /x\n", "/x"))
        self.assertTrue(allowed("User-agent: *\nAllow: /x\nDisallow: /x\n", "/x"))

    def test_allow_after_disallow_wordpress_regression(self):
        text = "User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php\n"
        self.assertTrue(allowed(text, "/wp-admin/admin-ajax.php"))
        self.assertFalse(allowed(text, "/wp-admin/options.php"))
        # regresja: stdlib robotparser myli się w tym przypadku
        stdlib = urllib.robotparser.RobotFileParser()
        stdlib.parse(text.splitlines())
        self.assertFalse(stdlib.can_fetch("PernoContextBot", "/wp-admin/admin-ajax.php"))

    def test_wildcards_and_end_anchor(self):
        text = "User-agent: *\nDisallow: /*?replytocom\nDisallow: /*.pdf$\nDisallow: /kat/*/tajne\n"
        self.assertFalse(allowed(text, "/post/?replytocom=5"))
        self.assertFalse(allowed(text, "/a/b.pdf"))
        self.assertTrue(allowed(text, "/a/b.pdf?x=1"))
        self.assertFalse(allowed(text, "/kat/xx/tajne/plik"))
        self.assertTrue(allowed(text, "/kat/tajne"))
        stdlib = urllib.robotparser.RobotFileParser()
        stdlib.parse(text.splitlines())
        self.assertTrue(stdlib.can_fetch("PernoContextBot", "/post/?replytocom=5"))  # stdlib ignoruje wildcard

    def test_query_is_part_of_the_matched_target(self):
        text = "User-agent: *\nDisallow: /search?\n"
        self.assertFalse(allowed(text, "/search?q=1"))
        self.assertTrue(allowed(text, "/search"))

    def test_percent_encoding_equivalence(self):
        self.assertFalse(allowed("User-agent: *\nDisallow: /caf%c3%a9\n", "/caf%C3%A9/menu"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /café\n", "/caf%C3%A9"))

    def test_robots_txt_itself_is_always_allowed(self):
        self.assertTrue(allowed("User-agent: *\nDisallow: /\n", "/robots.txt"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /\n", "/"))

    def test_empty_rules(self):
        self.assertTrue(allowed("User-agent: *\nDisallow:\n", "/cokolwiek"))
        self.assertTrue(allowed("User-agent: *\nAllow:\nDisallow: /x\n", "/y"))
        self.assertTrue(allowed("", "/x"))


class GroupTests(unittest.TestCase):
    def test_our_group_is_exclusive(self):
        text = "User-agent: *\nDisallow: /\n\nUser-agent: PernoContextBot\nDisallow: /private/\n"
        self.assertTrue(allowed(text, "/"))
        self.assertFalse(allowed(text, "/private/x"))
        self.assertFalse(allowed(text, "/x", token="InnyBot"))

    def test_token_matching_is_equality_not_substring(self):
        self.assertTrue(allowed("User-agent: bot\nDisallow: /\n", "/x"))
        self.assertTrue(allowed("User-agent: Context\nDisallow: /\n", "/x"))
        self.assertFalse(allowed("User-agent: pernocontextbot\nDisallow: /\n", "/x"))
        self.assertFalse(allowed("USER-AGENT: PernoContextBot/1.0\nDISALLOW: /\n", "/x"))
        stdlib = urllib.robotparser.RobotFileParser()
        stdlib.parse(["User-agent: bot", "Disallow: /"])
        self.assertFalse(stdlib.can_fetch("PernoContextBot", "/x"))  # stdlib dopasowuje po podciągu

    def test_groups_with_the_same_agent_are_merged(self):
        text = "User-agent: *\nDisallow: /a/\n\nUser-agent: Googlebot\nDisallow: /g/\n\nUser-agent: *\nDisallow: /b/\n"
        self.assertFalse(allowed(text, "/a/x"))
        self.assertFalse(allowed(text, "/b/x"))
        self.assertTrue(allowed(text, "/g/x"))

    def test_several_user_agent_lines_share_one_group(self):
        text = "User-agent: Googlebot\nUser-agent: PernoContextBot\nDisallow: /x/\n"
        self.assertFalse(allowed(text, "/x/y"))

    def test_rules_before_any_user_agent_are_ignored(self):
        self.assertTrue(allowed("Disallow: /\nUser-agent: *\nDisallow: /a/\n", "/b"))


class CrawlDelayAndSitemapTests(unittest.TestCase):
    def test_crawl_delay_parsing(self):
        for value, expected in (("10", 10.0), ("2.5", 2.5), ("2,5", 2.5), ("abc", None), ("-3", None), ("nan", None), ("inf", None)):
            rules = parse_robots(f"User-agent: *\nCrawl-delay: {value}\n")
            self.assertEqual(rules.crawl_delay, expected, value)
        stdlib = urllib.robotparser.RobotFileParser()
        stdlib.parse(["User-agent: *", "Crawl-delay: 2.5"])
        self.assertIsNone(stdlib.crawl_delay("PernoContextBot"))  # regresja wady stdlib

    def test_delay_comes_from_the_chosen_group_only(self):
        text = "User-agent: *\nCrawl-delay: 30\n\nUser-agent: PernoContextBot\nDisallow: /x/\n"
        self.assertIsNone(parse_robots(text).crawl_delay)
        text2 = "User-agent: *\nCrawl-delay: 30\n\nUser-agent: PernoContextBot\nCrawl-delay: 4\n"
        self.assertEqual(parse_robots(text2).crawl_delay, 4.0)
        self.assertEqual(parse_robots("User-agent: *\nCrawl-delay: 30\n").crawl_delay, 30.0)

    def test_sitemap_lines_are_global_absolute_only(self):
        text = ("Sitemap: https://a.pl/sitemap.xml\nUser-agent: *\nSitemap: http://a.pl/s2.xml\n"
                "Sitemap: /relative.xml\nDisallow: /x\n")
        self.assertEqual(parse_robots(text).sitemaps, ["https://a.pl/sitemap.xml", "http://a.pl/s2.xml"])

    def test_sitemap_cap(self):
        text = "".join(f"Sitemap: https://a.pl/s{i}.xml\n" for i in range(50))
        self.assertEqual(len(parse_robots(text).sitemaps), robots.MAX_SITEMAPS_IN_ROBOTS)


class TextRobustnessTests(unittest.TestCase):
    def test_bom_line_endings_comments_and_case(self):
        for newline in ("\n", "\r\n", "\r"):
            text = f"﻿# komentarz{newline}User-agent: *  # inline{newline}Disallow: /x # tu też{newline}"
            self.assertFalse(allowed(text, "/x"), repr(newline))
            self.assertTrue(allowed(text, "/y"))

    def test_bytes_input_and_size_cap(self):
        self.assertFalse(parse_robots(b"User-agent: *\nDisallow: /x\n").allowed("/x"))
        huge = "User-agent: *\nDisallow: /late\n".encode() + b"#" * (robots.ROBOTS_MAX_BYTES * 2)
        late = b"#" * (robots.ROBOTS_MAX_BYTES + 10) + b"\nUser-agent: *\nDisallow: /beyond\n"
        self.assertFalse(parse_robots(huge).allowed("/late"))
        self.assertTrue(parse_robots(late).allowed("/beyond"))  # poza limitem rozmiaru nie czytamy

    def test_garbage_lines_do_not_break_parsing(self):
        text = "<html><body>404</body></html>\nDisallow /bez-dwukropka\n::::\nUser-agent: *\nDisallow: /a\n"
        self.assertFalse(allowed(text, "/a"))
        self.assertTrue(allowed(text, "/b"))


class InterpretTests(unittest.TestCase):
    def test_status_mapping(self):
        table = {200: robots.OK, 204: robots.OK, 404: robots.BRAK, 410: robots.BRAK, 400: robots.BRAK,
                 451: robots.BRAK, 401: robots.ODMOWA, 403: robots.ODMOWA, 429: robots.BACKOFF,
                 500: robots.NIEDOSTEPNY, 502: robots.NIEDOSTEPNY, 503: robots.NIEDOSTEPNY, 504: robots.NIEDOSTEPNY}
        for status, expected in table.items():
            self.assertEqual(robots.interpret(status, b"").status, expected, status)

    def test_ok_parses_body_and_html_means_no_rules(self):
        outcome = robots.interpret(200, b"User-agent: *\nDisallow: /x\n", "text/plain")
        self.assertFalse(outcome.rules.allowed("/x"))
        html = robots.interpret(200, b"<html>Disallow: /x</html>", "text/html; charset=utf-8")
        self.assertEqual(html.status, robots.OK)
        self.assertTrue(html.rules.allowed("/x"))
        self.assertTrue(robots.interpret(404, b"User-agent: *\nDisallow: /").rules.allowed("/x"))


class SitemapTests(unittest.TestCase):
    def test_urlset_with_cdata_and_entities(self):
        xml = ("<?xml version='1.0'?><urlset><url><loc>https://a.pl/a?x=1&amp;y=2</loc></url>"
               "<url><loc><![CDATA[https://a.pl/b]]></loc></url><url><loc> https://a.pl/c </loc></url>"
               "<url><loc>ftp://a.pl/nie</loc></url></urlset>")
        pages, children = robots.parse_sitemap(xml)
        self.assertEqual(pages, ["https://a.pl/a?x=1&y=2", "https://a.pl/b", "https://a.pl/c"])
        self.assertEqual(children, [])

    def test_sitemap_index_returns_children(self):
        xml = ("<sitemapindex><sitemap><loc>https://a.pl/s1.xml</loc></sitemap>"
               "<sitemap><loc>https://a.pl/s2.xml</loc></sitemap></sitemapindex>")
        self.assertEqual(robots.parse_sitemap(xml), ([], ["https://a.pl/s1.xml", "https://a.pl/s2.xml"]))

    def test_cap_and_malformed(self):
        xml = "<urlset>" + "".join(f"<url><loc>https://a.pl/{i}</loc></url>" for i in range(50)) + "</urlset>"
        self.assertEqual(len(robots.parse_sitemap(xml, max_urls=10)[0]), 10)
        self.assertEqual(robots.parse_sitemap("<urlset><loc>https://a.pl/x"), ([], []))
        self.assertEqual(robots.parse_sitemap(b"\xff\xfe<urlset><loc>https://a.pl/x</loc>")[0], ["https://a.pl/x"])
        self.assertEqual(robots.parse_sitemap("")[0], [])


if __name__ == "__main__":
    unittest.main()
