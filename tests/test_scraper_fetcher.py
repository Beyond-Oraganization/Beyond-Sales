from __future__ import annotations

import http.client
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import unittest
from unittest import mock

from perno import settings as cfg
from perno.scraper import fetcher
from perno.scraper.fetcher import Fetcher, decode_html, is_public_ip
from perno.scraper.settings import ROBOTS_MAX_BYTES, ScanSettings
from tests.fake_sites import FakeInternet, NoNetworkMixin, Page, html, local_resolver

HOST = "site.test"


class FetcherCase(NoNetworkMixin):
    def setUp(self):
        super().setUp()
        self.net = FakeInternet().start()
        self.addCleanup(self.net.stop)
        self.settings = ScanSettings.for_tests()

    def fetcher(self, **kwargs):
        kwargs.setdefault("resolver", local_resolver)
        kwargs.setdefault("allow_private", True)
        return Fetcher(self.settings, **kwargs)

    def get(self, path="/", kind="html", host=HOST, **kwargs):
        return self.fetcher(**kwargs).fetch_once(self.net.url(host, path), kind)


class RequestShapeTests(FetcherCase):
    def test_polite_identifying_headers_and_no_cookies(self):
        self.net.site(HOST, {"/": Page(html(title="A"))})
        result = self.get("/")
        self.assertEqual(result.status, 200)
        entry = self.net.hits(HOST, "/")[0]
        self.assertEqual(entry["ua"], "PernoContextBot/1.0 (+test@example.com)")
        self.assertEqual(entry["from"], "test@example.com")
        self.assertEqual(entry["connection"], "close")
        self.assertEqual(entry["accept_encoding"], "gzip, deflate")
        self.assertIsNone(entry["cookie"])
        self.assertTrue(entry["accept"].startswith("text/html"))
        self.assertEqual(entry["host_header"], f"{HOST}:{self.net.port}")

    def test_from_header_is_omitted_when_contact_is_a_url(self):
        self.net.site(HOST, {"/": Page(html())})
        self.settings = ScanSettings.for_tests(contact="https://z3x.pl/bot")
        self.get("/")
        self.assertIsNone(self.net.hits(HOST, "/")[0]["from"])
        self.assertIn("(+https://z3x.pl/bot)", self.net.hits(HOST, "/")[0]["ua"])

    def test_accept_header_depends_on_kind(self):
        self.net.site(HOST, {"/s.xml": Page("<urlset/>", content_type="application/xml")}, robots="User-agent: *\n")
        self.get("/robots.txt", "robots")
        self.get("/s.xml", "sitemap")
        self.assertTrue(self.net.hits(HOST, "/robots.txt")[0]["accept"].startswith("text/plain"))
        self.assertTrue(self.net.hits(HOST, "/s.xml")[0]["accept"].startswith("application/xml"))

    def test_query_string_is_sent_and_set_cookie_never_stored(self):
        self.net.site(HOST, {"/a?x=1&y=2": Page(html(), headers={"Set-Cookie": "sid=1", "X-Test": "ok"})})
        result = self.get("/a?x=1&y=2")
        self.assertEqual(result.status, 200)
        self.assertNotIn("set-cookie", result.headers)
        self.assertEqual(result.headers["x-test"], "ok")


class BodyHandlingTests(FetcherCase):
    def test_plain_gzip_deflate_raw_deflate_and_chunked(self):
        body = html("/x", title="Zażółć gęślą jaźń")
        variants = {"/plain": Page(body), "/gz": Page(body, encoding="gzip"), "/df": Page(body, encoding="deflate"),
                    "/raw": Page(body, encoding="raw-deflate"), "/ch": Page(body, chunked=True),
                    "/chgz": Page(body, encoding="gzip", chunked=True)}
        self.net.site(HOST, variants)
        for path in variants:
            result = self.get(path)
            self.assertIsNone(result.error, path)
            self.assertEqual(result.body, body.encode("utf-8"), path)
            self.assertEqual(result.content_type, "text/html", path)

    def test_unsupported_encoding_is_an_error_not_garbage(self):
        self.net.site(HOST, {"/br": Page("<html>x</html>", encoding="br")})
        self.assertEqual(self.get("/br").error, "kodowanie")

    def test_declared_length_over_limit_is_rejected_before_reading(self):
        self.net.site(HOST, {"/big": Page("<html>" + "a" * 50_000 + "</html>")})
        result = self.get("/big", max_body=10_000)
        self.assertEqual(result.error, "za_duze")
        self.assertEqual(result.body, b"")

    def test_chunked_body_over_limit_is_cut_off(self):
        self.net.site(HOST, {"/big": Page("<html>" + "a" * 50_000 + "</html>", chunked=True)})
        self.assertEqual(self.get("/big", max_body=10_000).error, "za_duze")

    def test_gzip_bomb_is_stopped_by_decompressed_size(self):
        bomb = b"<html>" + b"\0" * (8 * 1024 * 1024)
        self.net.site(HOST, {"/bomb": Page(bomb, encoding="gzip")})
        result = self.get("/bomb", max_body=100_000)
        self.assertEqual(result.error, "za_duze")
        self.assertLess(len(result.body), 100_001)

    def test_content_length_check_is_relaxed_only_for_compressed_bodies(self):
        text = "<html>" + "ab" * 6_000 + "</html>"  # 12 KB po dekompresji, kilkadziesiąt bajtów po kompresji
        self.net.site(HOST, {"/gz": Page(text, encoding="gzip")})
        self.assertEqual(self.get("/gz", max_body=20_000).body, text.encode())
        self.assertEqual(self.get("/gz", max_body=5_000).error, "za_duze")

    def test_non_html_is_rejected_without_reading_a_trickled_body(self):
        self.net.site(HOST, {"/f.pdf": Page(b"%PDF" + b"x" * 200_000, content_type="application/pdf", trickle=(200, 0.05))})
        result = self.get("/f.pdf")
        self.assertEqual(result.error, "niehtml")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b"")
        self.assertLess(result.elapsed, 2.0)

    def test_missing_content_type_is_sniffed(self):
        self.net.site(HOST, {"/ok": Page("<!DOCTYPE html><html><body>Cześć</body></html>", content_type=None),
                             "/bin": Page(b"\x89PNG\r\n\x1a\n" + b"\0" * 500, content_type=None)})
        self.assertIsNone(self.get("/ok").error)
        self.assertEqual(self.get("/bin").error, "niehtml")

    def test_xhtml_is_accepted(self):
        self.net.site(HOST, {"/x": Page("<html/>", content_type="application/xhtml+xml; charset=utf-8")})
        self.assertIsNone(self.get("/x").error)

    def test_redirect_returns_location_without_following(self):
        self.net.site(HOST, {"/old": Page("moved", status=301, location="/new"), "/new": Page(html())})
        result = self.get("/old")
        self.assertEqual((result.status, result.location, result.body), (301, "/new", b""))
        self.assertEqual(len(self.net.hits(HOST, "/new")), 0)

    def test_error_status_keeps_a_bounded_body_for_bot_wall_detection(self):
        self.net.site(HOST, {"/wall": Page("Just a moment... " + "x" * 300_000, status=403),
                             "/slow": Page("later", status=429, headers={"Retry-After": "120"}, content_type="text/plain")})
        wall = self.get("/wall")
        self.assertEqual(wall.status, 403)
        self.assertIsNone(wall.error)
        self.assertTrue(wall.body.startswith(b"Just a moment"))
        self.assertLessEqual(len(wall.body), fetcher.SMALL_ERROR_BODY)
        slow = self.get("/slow")
        self.assertEqual((slow.status, slow.headers["retry-after"]), (429, "120"))

    def test_truncated_response_is_a_network_error_not_a_short_success(self):
        self.net.site(HOST, {"/cut": Page("<html>" + "x" * 100, content_length=5_000)})
        self.assertEqual(self.get("/cut").error, "siec")

    def test_robots_body_is_truncated_not_rejected_and_html_type_is_tolerated(self):
        huge = "User-agent: *\n" + "# " + "x" * (ROBOTS_MAX_BYTES + 5_000)
        self.net.site(HOST, robots=Page(huge, content_type="text/html"))
        result = self.get("/robots.txt", "robots")
        self.assertIsNone(result.error)
        self.assertEqual(len(result.body), ROBOTS_MAX_BYTES)

    def test_sitemap_kind_accepts_xml_and_rejects_images(self):
        self.net.site(HOST, {"/s.xml": Page("<urlset/>", content_type="text/xml"),
                             "/s.png": Page(b"\x89PNG", content_type="image/png")})
        self.assertIsNone(self.get("/s.xml", "sitemap").error)
        self.assertEqual(self.get("/s.png", "sitemap").error, "niehtml")

    def test_missing_page_is_a_plain_404(self):
        self.net.site(HOST, {})
        result = self.get("/nie-ma")
        self.assertEqual((result.status, result.error), (404, None))


class DeadlineTests(FetcherCase):
    def test_trickled_body_hits_the_total_deadline(self):
        self.net.site(HOST, {"/slow": Page("<html>" + "x" * 20_000, trickle=(100, 0.1))})
        result = self.get("/slow", request_deadline=1.0, recv_timeout=0.5)
        self.assertEqual(result.error, "timeout")
        self.assertLess(result.elapsed, 3.0)
        self.assertEqual(result.body, b"")  # nigdy nie oddajemy ściętej treści jako sukcesu

    def test_slow_response_headers_time_out(self):
        self.net.site(HOST, {"/slow": Page(html(), header_delay=3.0)})
        result = self.get("/slow", request_deadline=1.0, recv_timeout=0.5)
        self.assertEqual(result.error, "timeout")
        self.assertLess(result.elapsed, 2.9)

    def test_connection_refused_is_a_network_error(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        result = self.fetcher().fetch_once(f"http://{HOST}:{port}/")
        self.assertEqual(result.error, "siec")
        self.assertIsNone(result.status)


class ResolverAndSsrfTests(FetcherCase):
    def test_dns_failure_is_reported_and_cached(self):
        calls = []

        def failing(host, port):
            calls.append(host)
            raise socket.gaierror(8, "nodename nor servname provided")

        instance = self.fetcher(resolver=failing)
        for _ in range(2):
            self.assertEqual(instance.fetch_once(f"http://{HOST}/").error, "dns")
        self.assertEqual(calls, [HOST])

    def test_empty_answer_is_dns_error(self):
        self.assertEqual(self.fetcher(resolver=lambda h, p: []).fetch_once(f"http://{HOST}/").error, "dns")

    def test_non_global_addresses_are_refused_without_connecting(self):
        self.net.site(HOST, {"/": Page(html())})
        for address in ("127.0.0.1", "10.1.2.3", "192.168.0.10", "172.16.5.5", "169.254.169.254", "::1", "fe80::1", "0.0.0.0"):
            instance = Fetcher(self.settings, resolver=lambda h, p, a=address: [a], allow_private=False)
            result = instance.fetch_once(self.net.url(HOST, "/"))
            self.assertEqual(result.error, "ssrf", address)
        self.assertEqual(self.net.log, [])

    def test_explicit_ip_is_also_checked(self):
        instance = Fetcher(self.settings, resolver=local_resolver, allow_private=False)
        result = instance.fetch_once(self.net.url(HOST, "/"), ip="127.0.0.1")
        self.assertEqual(result.error, "ssrf")
        self.assertEqual(self.net.log, [])

    def test_mixed_answer_keeps_only_public_addresses(self):
        instance = Fetcher(self.settings, resolver=lambda h, p: ["10.0.0.1", "93.184.216.34"], allow_private=False)
        self.assertEqual(instance.resolve(HOST), ["93.184.216.34"])

    def test_environment_switch_enables_private_addresses(self):
        self.net.site(HOST, {"/": Page(html())})
        with mock.patch.dict(os.environ, {cfg.ENV_SCRAPER_ALLOW_PRIVATE: "1"}):
            instance = Fetcher(self.settings, resolver=local_resolver)
        self.assertEqual(instance.fetch_once(self.net.url(HOST, "/")).status, 200)
        with mock.patch.dict(os.environ, {cfg.ENV_SCRAPER_ALLOW_PRIVATE: ""}):
            self.assertFalse(Fetcher(self.settings, resolver=local_resolver).allow_private)

    def test_is_public_ip(self):
        self.assertTrue(is_public_ip("93.184.216.34"))
        for address in ("127.0.0.1", "10.0.0.1", "100.64.0.1", "not-an-ip", ""):
            self.assertFalse(is_public_ip(address), address)


class PinnedConnectionTests(FetcherCase):
    def test_connection_goes_to_the_resolved_ip_while_host_header_keeps_the_name(self):
        self.net.site("pinned.test", {"/": Page(html())})
        seen = []
        real = socket.create_connection

        def spy(address, *args, **kwargs):
            seen.append(address)
            return real(address, *args, **kwargs)

        with mock.patch("socket.create_connection", spy):
            result = self.get("/", host="pinned.test")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.ip, "127.0.0.1")
        self.assertEqual(seen, [("127.0.0.1", self.net.port)])
        self.assertEqual(self.net.hits("pinned.test")[0]["host_header"], f"pinned.test:{self.net.port}")

    def test_http_client_still_exposes_the_private_hook_we_rely_on(self):
        # Przypięcie IP opiera się na prywatnym atrybucie; po aktualizacji Pythona ten test ma krzyczeć.
        conn = http.client.HTTPConnection("example.invalid")
        self.assertTrue(callable(getattr(conn, "_create_connection", None)))
        conn.close()


class DecodeTests(unittest.TestCase):
    TEXT = "Zażółć gęślą jaźń, Polskie Stowarzyszenie Łączności, ul. Świętojańska 5, Kraków"

    def test_utf8_bom_and_plain_utf8(self):
        self.assertEqual(decode_html(b"\xef\xbb\xbf" + self.TEXT.encode("utf-8")), (self.TEXT, "utf-8-sig"))
        self.assertEqual(decode_html(self.TEXT.encode("utf-8"))[0], self.TEXT)

    def test_utf16_bom(self):
        text, name = decode_html(self.TEXT.encode("utf-16"))
        self.assertEqual((text, name), (self.TEXT, "utf-16"))

    def test_http_header_charset_wins(self):
        body = self.TEXT.encode("iso8859_2")
        self.assertEqual(decode_html(body, "text/html; charset=ISO-8859-2"), (self.TEXT, "iso8859-2"))

    def test_meta_charset_is_used_when_header_is_silent(self):
        page = '<html><head><meta charset="windows-1250"></head><body>' + self.TEXT + "</body></html>"
        self.assertEqual(decode_html(page.encode("cp1250"), "text/html")[0], page)
        old_style = '<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-2">' + self.TEXT
        self.assertEqual(decode_html(old_style.encode("iso8859_2"))[0], old_style)

    def test_unknown_declared_charset_falls_back_to_utf8(self):
        self.assertEqual(decode_html(self.TEXT.encode("utf-8"), "text/html; charset=klingon"), (self.TEXT, "utf-8"))

    def test_wrong_utf8_declaration_on_legacy_bytes_never_raises_and_keeps_ascii_and_shared_letters(self):
        body = ("<html><body>" + (self.TEXT + " ") * 5 + "</body></html>").encode("cp1250")
        text, name = decode_html(body, "text/html; charset=utf-8")
        self.assertIn(name, ("cp1250", "iso8859_2", "cp1252", "latin_1"))
        self.assertIn("Krak", text)
        self.assertNotIn("�", text)

    def test_garbage_never_raises(self):
        text, _ = decode_html(bytes(range(256)) * 8)
        self.assertIsInstance(text, str)
        self.assertEqual(decode_html(b""), ("", "utf-8"))


@unittest.skipUnless(os.path.exists("/usr/bin/openssl") or shutil.which("openssl"), "brak openssl")
class TlsTests(NoNetworkMixin):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp(prefix="perno-tls-")
        cls.cert, cls.key = os.path.join(cls.directory, "c.pem"), os.path.join(cls.directory, "k.pem")
        openssl = shutil.which("openssl") or "/usr/bin/openssl"
        proc = subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", cls.key,
                               "-out", cls.cert, "-days", "2", "-subj", "/CN=secure.test"],
                              capture_output=True, timeout=60)
        cls.ok = proc.returncode == 0 and os.path.exists(cls.cert)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.directory, ignore_errors=True)

    def setUp(self):
        super().setUp()
        if not self.ok:
            self.skipTest("nie udało się wygenerować certyfikatu")
        self.net = FakeInternet(tls=(self.cert, self.key)).start()
        self.addCleanup(self.net.stop)
        self.net.site("secure.test", {"/": Page(html(title="TLS"))})

    def test_untrusted_certificate_is_a_tls_error_with_default_context(self):
        instance = Fetcher(ScanSettings.for_tests(), resolver=local_resolver, allow_private=True)
        result = instance.fetch_once(self.net.url("secure.test", "/"))
        self.assertEqual(result.error, "tls")
        self.assertIsNone(result.status)

    def test_default_context_verifies_and_requires_tls12(self):
        context = fetcher._make_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_trusted_certificate_works_with_pinned_ip_and_sni_name(self):
        context = ssl.create_default_context(cafile=self.cert)
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT  # Python 3.13+ domyślnie ścisły; certyfikat testowy jest minimalny
        instance = Fetcher(ScanSettings.for_tests(), resolver=local_resolver, allow_private=True, ssl_context=context)
        result = instance.fetch_once(self.net.url("secure.test", "/"))
        self.assertIsNone(result.error)
        self.assertEqual(result.status, 200)

    def test_certificate_for_another_name_is_rejected_even_if_chain_is_trusted(self):
        context = ssl.create_default_context(cafile=self.cert)
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT  # Python 3.13+ domyślnie ścisły; certyfikat testowy jest minimalny
        instance = Fetcher(ScanSettings.for_tests(), resolver=local_resolver, allow_private=True, ssl_context=context)
        self.assertEqual(instance.fetch_once(self.net.url("other.test", "/")).error, "tls")


if __name__ == "__main__":
    unittest.main()
