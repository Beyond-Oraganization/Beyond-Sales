"""Składanie odpowiedzi: temat, adresat, skrzynka, nagłówki wątku, podpis, cytat i walidacja (czysta logika)."""

from __future__ import annotations

import unittest
from datetime import datetime

from perno.domain import reply_compose as rc


def msg(direction="in", kind="human", *, sender="anna@mylimo.pl", mailbox="roman@z3x.pl", message_id="", references="", date="2026-10-05T10:00:00"):
    return {"direction": direction, "kind": kind, "from_addr": sender, "mailbox": mailbox, "message_id": message_id, "references_hdr": references, "date": date}


class SubjectTests(unittest.TestCase):
    def test_a_single_re_is_added_and_never_doubled(self):
        for given, expected in (("Rezerwacje", "Re: Rezerwacje"), ("Re: Rezerwacje", "Re: Rezerwacje"), ("RE: x", "RE: x"), ("Odp: x", "Odp: x"),
                                ("AW: x", "AW: x"), ("  spacje  ", "Re: spacje"), ("", "Re:"), ("Revolut", "Re: Revolut")):
            with self.subTest(given=given):
                self.assertEqual(rc.reply_subject(given), expected)

    def test_control_characters_in_a_subject_are_flattened_so_no_header_can_be_injected(self):
        self.assertEqual(rc.reply_subject("Temat\r\nBcc: ktos@x.pl"), "Re: Temat  Bcc: ktos@x.pl")

    def test_the_subject_check_refuses_injection_empty_and_huge_values(self):
        self.assertEqual(rc.check_subject("  Re: Temat "), "Re: Temat")
        for bad in ("", "   ", "a\r\nBcc: x@y.pl", "a\nb", "a\x00b", None, 5, "x" * 301):
            with self.subTest(bad=bad), self.assertRaises(rc.ComposeError):
                rc.check_subject(bad)
        self.assertEqual(len(rc.check_subject("x" * 300)), 300)


class BodyAndAddressTests(unittest.TestCase):
    def test_a_body_is_trimmed_normalised_and_bounded(self):
        self.assertEqual(rc.check_body("  Dzień dobry\r\n\r\nDziękuję\x00  "), "Dzień dobry\n\nDziękuję")
        self.assertEqual(len(rc.check_body("x" * 20_000)), 20_000)
        for bad in ("", "  \n ", None, 5, "x" * 20_001, "\x00"):
            with self.subTest(bad=bad), self.assertRaises(rc.ComposeError):
                rc.check_body(bad)

    def test_a_recipient_is_one_plain_address(self):
        self.assertEqual(rc.check_address(" Anna@MyLimo.PL "), "anna@mylimo.pl")
        for bad in ("", "bez-malpy", "a@b", "a@b.pl, c@d.pl", "Anna <a@b.pl>", "a@b.pl\r\nBcc: x@y.pl", "a b@c.pl", None, "a@b.pl;c@d.pl", '"a"@b.pl'):
            with self.subTest(bad=bad), self.assertRaises(rc.ComposeError):
                rc.check_address(bad)


class RecipientAndMailboxTests(unittest.TestCase):
    def test_the_recipient_is_the_last_human_sender_not_a_machine_or_us(self):
        messages = [msg(sender="anna@mylimo.pl"), msg(kind="auto", sender="biuro@mylimo.pl"), msg(direction="out", sender="roman@z3x.pl"),
                    msg(kind="bounce", sender="mailer-daemon@mx"), msg(sender="Szef@MyLimo.pl")]
        self.assertEqual(rc.reply_recipient(messages, "origin@mylimo.pl"), "szef@mylimo.pl")

    def test_without_an_inbound_message_the_campaign_address_is_used(self):
        self.assertEqual(rc.reply_recipient([msg(direction="out")], "Origin@MyLimo.pl"), "origin@mylimo.pl")
        self.assertEqual(rc.reply_recipient([], "origin@mylimo.pl"), "origin@mylimo.pl")

    def test_an_unusable_address_is_an_error_not_a_guess(self):
        with self.assertRaises(rc.ComposeError):
            rc.reply_recipient([msg(sender="")], "")

    def test_the_mailbox_is_the_one_the_last_human_message_arrived_at(self):
        messages = [msg(mailbox="roman@z3x.pl"), msg(mailbox="Drugi@Z3X.pl"), msg(kind="auto", mailbox="trzeci@z3x.pl")]
        self.assertEqual(rc.reply_mailbox("roman@z3x.pl", messages), "drugi@z3x.pl")

    def test_without_inbound_mail_the_threads_own_mailbox_is_used_and_nothing_gives_empty(self):
        self.assertEqual(rc.reply_mailbox("Roman@Z3X.pl", [msg(direction="out")]), "roman@z3x.pl")
        self.assertEqual(rc.reply_mailbox("", []), "")
        self.assertEqual(rc.reply_mailbox(None, []), "")  # type: ignore[arg-type]


class HeaderTests(unittest.TestCase):
    def test_we_answer_the_last_message_and_references_carry_the_whole_chain_once(self):
        messages = [msg(message_id="odp1@x", references="orig@z3x.pl"), msg(direction="out", message_id="nasza@z3x.pl", references="orig@z3x.pl odp1@x"),
                    msg(message_id="odp2@x", references="orig@z3x.pl odp1@x nasza@z3x.pl")]
        in_reply_to, references = rc.thread_headers("orig@z3x.pl", messages)
        self.assertEqual(in_reply_to, "<odp2@x>")
        self.assertEqual(references, "<orig@z3x.pl> <odp1@x> <nasza@z3x.pl> <odp2@x>")

    def test_we_answer_the_last_message_of_the_company_even_when_we_wrote_after_it(self):
        messages = [msg(message_id="odp1@x"), msg(direction="out", message_id="nasza@z3x.pl")]
        self.assertEqual(rc.thread_headers("orig@z3x.pl", messages)[0], "<odp1@x>")

    def test_the_campaign_message_id_starts_the_chain_and_may_be_unknown(self):
        self.assertEqual(rc.thread_headers("orig@z3x.pl", [msg(message_id="odp1@x")]), ("<odp1@x>", "<orig@z3x.pl> <odp1@x>"))
        self.assertEqual(rc.thread_headers("", [msg(message_id="odp1@x")]), ("<odp1@x>", "<odp1@x>"))

    def test_a_thread_without_any_ids_gives_no_headers(self):
        self.assertEqual(rc.thread_headers("", [msg()]), ("", ""))
        self.assertEqual(rc.thread_headers("", []), ("", ""))

    def test_a_very_long_chain_keeps_only_the_newest_ids(self):
        messages = [msg(message_id=f"m{n}@x") for n in range(50)]
        _, references = rc.thread_headers("orig@z3x.pl", messages)
        ids = references.split()
        self.assertEqual((len(ids), ids[0], ids[-1]), (20, "<m30@x>", "<m49@x>"))

    def test_when_the_last_inbound_has_no_id_we_reply_to_the_last_known_id(self):
        self.assertEqual(rc.thread_headers("orig@z3x.pl", [msg()])[0], "<orig@z3x.pl>")


class TextTests(unittest.TestCase):
    QUOTE = rc.Quote("Anna Nowak <anna@mylimo.pl>", datetime(2026, 10, 5, 10, 0), "Proszę o cennik.\n\nPozdrawiam")

    def test_the_sender_name_is_added_once_on_its_own_line(self):
        self.assertEqual(rc.with_signature("Dziękuję.\nPozdrawiam", "Roman Patlai"), "Dziękuję.\nPozdrawiam\nRoman Patlai")
        self.assertEqual(rc.with_signature("Dziękuję, Roman Patlai", "Roman Patlai"), "Dziękuję, Roman Patlai")
        self.assertEqual(rc.with_signature("Dziękuję, roman patlai", "Roman Patlai"), "Dziękuję, roman patlai")
        self.assertEqual(rc.with_signature("Dziękuję", "  "), "Dziękuję")

    def test_the_quote_has_an_attribution_and_prefixed_lines_and_is_bounded(self):
        text = rc.quote_text(self.QUOTE)
        self.assertEqual(text, "W dniu 05.10.2026 10:00 Anna Nowak <anna@mylimo.pl> napisał(a):\n> Proszę o cennik.\n>\n> Pozdrawiam")
        long = rc.quote_text(rc.Quote("x@y.pl", None, "\n".join(f"w{n}" for n in range(100))))
        self.assertEqual(len(long.split("\n")), 1 + rc.MAX_QUOTE_LINES)
        self.assertTrue(long.startswith("W dniu x@y.pl napisał(a):"))

    def test_the_full_text_has_body_name_then_the_quote_and_the_quote_is_optional(self):
        full = rc.compose_text("Wysyłam cennik.", "Roman", self.QUOTE)
        self.assertEqual(full.split("\n\n")[0], "Wysyłam cennik.\nRoman")
        self.assertTrue(full.endswith("> Pozdrawiam"))
        self.assertEqual(rc.compose_text("Wysyłam cennik.", "Roman"), "Wysyłam cennik.\nRoman")
        self.assertEqual(rc.compose_text("x", "", rc.Quote("a@b.pl", None, "   ")), "x")

    def test_html_is_escaped_and_the_quote_becomes_a_blockquote(self):
        text = rc.compose_text("Cena: <b>20 zł</b> & więcej", "Roman", self.QUOTE)
        out = rc.compose_html(text)
        self.assertIn("&lt;b&gt;20 zł&lt;/b&gt; &amp; więcej", out)
        self.assertNotIn("<b>", out)
        self.assertIn("<blockquote>Proszę o cennik.<br>\n<br>\nPozdrawiam</blockquote>", out)
        self.assertIn("<div>W dniu 05.10.2026 10:00 Anna Nowak &lt;anna@mylimo.pl&gt; napisał(a):</div>", out)
        self.assertEqual(rc.compose_html("a\nb"), "<div>a<br>\nb</div>")


class HtmlShapeTests(unittest.TestCase):
    def test_a_quote_without_an_attribution_line_and_a_quote_at_the_very_start(self):
        self.assertEqual(rc.compose_html("Odp.\n> cytat\n> dalej"), "<div>Odp.</div>\n<blockquote>cytat<br>\ndalej</blockquote>")
        self.assertEqual(rc.compose_html("> tylko cytat"), "<div></div>\n<blockquote>tylko cytat</blockquote>")

    def test_the_attribution_belongs_to_the_quote_only_when_it_stands_right_above_it(self):
        out = rc.compose_html("W dniu 05.10.2026 10:00 Anna napisał(a):\n> cytat")
        self.assertEqual(out, "<div></div>\n<div>W dniu 05.10.2026 10:00 Anna napisał(a):</div>\n<blockquote>cytat</blockquote>")
        unrelated = rc.compose_html("W dniu wtorku dzwonię.\n\n> cytat")
        self.assertIn("<div>W dniu wtorku dzwonię.<br>\n</div>", unrelated)

    def test_a_bare_angle_bracket_line_inside_a_quote_stays_in_it(self):
        self.assertEqual(rc.compose_html("a\n> x\n>\n> y"), "<div>a</div>\n<blockquote>x<br>\n<br>\ny</blockquote>")


class AckTests(unittest.TestCase):
    def test_an_opt_out_or_do_not_contact_needs_a_confirmation(self):
        self.assertEqual(rc.acks_needed(True, "odpowiedzial"), ["optout"])
        self.assertEqual(rc.acks_needed(False, "nie_kontaktowac"), ["optout"])
        self.assertEqual(rc.acks_needed(False, "odpowiedzial"), [])
        self.assertEqual(rc.acks_needed(False, ""), [])


if __name__ == "__main__":
    unittest.main()
