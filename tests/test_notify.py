"""Kanały powiadomień i rozsyłanie: transporty są atrapami, nic nie dotyka sieci ani systemu."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from perno.notify import Notification, Notifier, NotifyError, scrub
from perno.notify.email_channel import EmailChannel
from perno.notify.factory import build_notifier, build_pinger
from perno.notify.healthcheck import HealthcheckPinger
from perno.notify.macos import MacosChannel
from perno.notify.ntfy import NtfyChannel
from perno.settings import ConfigError, NotifySettings
from tests.fixtures import FakeSmtp

NOTE = Notification("Tytuł „ś”", 'Firma "X" \\ koniec', "warn")


class MacosTests(unittest.TestCase):
    def test_passes_text_as_arguments_not_inside_the_script(self):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            return SimpleNamespace(returncode=0, stderr="")

        MacosChannel(run=run, platform="darwin").send(NOTE)
        cmd, kw = calls[0]
        self.assertEqual(cmd[0], "/usr/bin/osascript")
        self.assertEqual(cmd[-3:], ["--", NOTE.message, NOTE.title])
        self.assertTrue(all('Firma "X"' not in part for part in cmd[1:-3]))  # treść nie jest wklejona do AppleScriptu
        self.assertEqual((kw["timeout"], kw["check"]), (10, False))

    def test_not_on_macos(self):
        with self.assertRaisesRegex(NotifyError, "macOS"):
            MacosChannel(run=lambda *a, **k: None, platform="linux").send(NOTE)

    def test_nonzero_exit_and_missing_binary(self):
        failing = MacosChannel(run=lambda *a, **k: SimpleNamespace(returncode=1, stderr="odmowa"), platform="darwin")
        with self.assertRaisesRegex(NotifyError, "kod 1.*odmowa"):
            failing.send(NOTE)

        def boom(*a, **k):
            raise FileNotFoundError("brak")

        with self.assertRaisesRegex(NotifyError, "nie wystartował"):
            MacosChannel(run=boom, platform="darwin").send(NOTE)

    def test_long_text_is_trimmed(self):
        seen = []
        MacosChannel(run=lambda cmd, **k: seen.append(cmd) or SimpleNamespace(returncode=0, stderr=""),
                     platform="darwin").send(Notification("t" * 300, "m" * 900))
        self.assertEqual((len(seen[0][-2]), len(seen[0][-1])), (500, 100))


class NtfyTests(unittest.TestCase):
    def test_publishes_json_with_priority_and_utf8(self):
        sent = []
        NtfyChannel("sekretny-temat-1", "https://ntfy.example/", post=lambda *a: sent.append(a)).send(NOTE)
        url, data, headers = sent[0]
        body = json.loads(data.decode("utf-8"))
        self.assertEqual(url, "https://ntfy.example")
        self.assertEqual(headers, {"Content-Type": "application/json"})
        self.assertEqual((body["topic"], body["title"], body["priority"], body["tags"]),
                         ("sekretny-temat-1", "Tytuł „ś”", 4, ["warning"]))

    def test_priority_by_level(self):
        sent = []
        channel = NtfyChannel("sekretny-temat-1", post=lambda *a: sent.append(json.loads(a[1])))
        for level in ("info", "warn", "crit", "dziwny"):
            channel.send(Notification("t", "m", level))
        self.assertEqual([m["priority"] for m in sent], [3, 4, 5, 3])

    def test_topic_is_a_secret(self):
        self.assertEqual(NtfyChannel("sekretny-temat-1").secrets, ("sekretny-temat-1",))


class EmailTests(unittest.TestCase):
    def test_sends_escaped_html_to_the_configured_address(self):
        smtp = FakeSmtp()
        EmailChannel(lambda: smtp, "ja@example.com", "ja@example.org").send(Notification("Podsumowanie", "<b>x</b>\nlinia 2"))
        message = smtp.sent[0]
        self.assertEqual((message["To"], message["Subject"]), ("ja@example.org", "[Perno automat] Podsumowanie"))
        html = message.get_body(("html",)).get_content()
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn("<b>x</b>", html)

    def test_smtp_failure_becomes_notify_error_without_details(self):
        channel = EmailChannel(lambda: FakeSmtp(fail_with=RuntimeError("hasło=tajne")), "a@b.pl", "c@d.pl")
        with self.assertRaises(NotifyError) as ctx:
            channel.send(NOTE)
        self.assertNotIn("tajne", str(ctx.exception))


class HealthcheckTests(unittest.TestCase):
    def test_throttles_pings(self):
        urls, clock = [], [0.0]
        pinger = HealthcheckPinger("https://hc.example/abc/", get=urls.append, clock=lambda: clock[0], min_interval=300)
        self.assertTrue(pinger.ping())
        clock[0] = 299
        self.assertFalse(pinger.ping())
        clock[0] = 300
        self.assertTrue(pinger.ping())
        self.assertEqual(urls, ["https://hc.example/abc", "https://hc.example/abc"])

    def test_fail_goes_through_immediately(self):
        urls = []
        pinger = HealthcheckPinger("https://hc.example/abc", get=urls.append, clock=lambda: 0.0)
        pinger.ping()
        pinger.fail()
        self.assertEqual(urls[-1], "https://hc.example/abc/fail")

    def test_failed_ping_is_not_counted_as_sent(self):
        calls = []

        def get(url):
            calls.append(url)
            if len(calls) == 1:
                raise NotifyError("sieć")

        pinger = HealthcheckPinger("https://hc.example/abc", get=get, clock=lambda: 0.0)
        with self.assertRaises(NotifyError):
            pinger.ping()
        self.assertTrue(pinger.ping())  # pierwsza próba nie zablokowała kolejnej


class FakeChannel:
    def __init__(self, name, error=None, secrets=()):
        self.name, self.error, self.secrets, self.got = name, error, secrets, []

    def send(self, notification):
        if self.error:
            raise self.error
        self.got.append(notification)


class NotifierTests(unittest.TestCase):
    def test_failure_of_one_channel_does_not_block_others(self):
        a, b = FakeChannel("macos", RuntimeError("zepsute")), FakeChannel("ntfy")
        result = Notifier({"macos": a, "ntfy": b}).dispatch(NOTE, ("macos", "ntfy"))
        self.assertEqual(result.sent, ["ntfy"])
        self.assertEqual(list(result.failed), ["macos"])
        self.assertIn("zepsute", result.failed["macos"])

    def test_unconfigured_channel_is_skipped_not_failed(self):
        result = Notifier({"macos": FakeChannel("macos")}).dispatch(NOTE, ("macos", "email"))
        self.assertEqual((result.sent, result.skipped, result.failed), (["macos"], ["email"], {}))

    def test_only_requested_channels_are_used(self):
        a, b = FakeChannel("macos"), FakeChannel("ntfy")
        Notifier({"macos": a, "ntfy": b}).dispatch(NOTE, ("ntfy",))
        self.assertEqual((a.got, b.got), ([], [NOTE]))

    def test_secrets_are_scrubbed_from_error_text(self):
        channel = FakeChannel("ntfy", RuntimeError("POST https://ntfy.sh/sekretny-temat-1 nie wyszło"), ("sekretny-temat-1",))
        result = Notifier({"ntfy": channel}).dispatch(NOTE, ("ntfy",))
        self.assertNotIn("sekretny-temat-1", result.failed["ntfy"])
        self.assertIn("***", result.failed["ntfy"])

    def test_scrub_ignores_empty_secret(self):
        self.assertEqual(scrub("tekst", ("",)), "tekst")


class SettingsAndFactoryTests(unittest.TestCase):
    def test_defaults(self):
        s = NotifySettings.from_config({})
        self.assertEqual((s.macos, s.ntfy_topic, s.ntfy_server, s.email_to, s.healthcheck_url),
                         (True, "", "https://ntfy.sh", "", ""))
        self.assertEqual(s.configured_channels(), ["macos"])

    def test_full_config_and_repr_hides_secrets(self):
        s = NotifySettings.from_config({"NTFY_TOPIC": "bardzo-tajny-temat", "NOTIFY_EMAIL": "ja@example.com",
                                        "HEALTHCHECK_URL": "https://hc-ping.com/abc", "NOTIFY_MACOS": "FALSE"})
        self.assertEqual(s.configured_channels(), ["ntfy", "email"])
        self.assertNotIn("tajny", repr(s))
        self.assertNotIn("hc-ping", repr(s))

    def test_invalid_values_are_rejected_without_echoing_them(self):
        for config in ({"NTFY_TOPIC": "krótki"}, {"NTFY_TOPIC": "z spacją i polskimi znakami"},
                       {"NTFY_SERVER": "http://ntfy.sh"}, {"HEALTHCHECK_URL": "http://x"}, {"NOTIFY_EMAIL": "bez-małpy"}):
            with self.subTest(config=config), self.assertRaises(ConfigError) as ctx:
                NotifySettings.from_config(config)
            self.assertNotIn(next(iter(config.values())), str(ctx.exception))

    def test_factory_builds_only_configured_channels(self):
        s = NotifySettings.from_config({"NTFY_TOPIC": "bardzo-tajny-temat", "NOTIFY_EMAIL": "ja@example.com"})
        names = set(build_notifier(s, smtp_factory=lambda: FakeSmtp(), from_addr="a@b.pl").channels)
        self.assertEqual(names, {"macos", "ntfy", "email"})
        self.assertEqual(set(build_notifier(s).channels), {"macos", "ntfy"})  # bez SMTP nie ma kanału e-mail
        off = NotifySettings.from_config({"NOTIFY_MACOS": "false"})
        self.assertEqual(build_notifier(off).channels, {})

    def test_pinger_only_with_url(self):
        self.assertIsNone(build_pinger(NotifySettings()))
        self.assertIsNotNone(build_pinger(NotifySettings(healthcheck_url="https://hc.example/x")))


if __name__ == "__main__":
    unittest.main()
