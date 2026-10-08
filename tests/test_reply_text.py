"""Treść odpowiedzi: czyszczenie, HTML → tekst, podział na nowy tekst i cytat, skrót i prośba o wypisanie."""

from __future__ import annotations

import unittest

from perno.domain import reply_text as rt


class CleaningTests(unittest.TestCase):
    def test_control_direction_and_zero_width_characters_are_removed(self):
        dirty = "Cześć‮evil‬​‍﻿\x00\x07\x1b[31m\x85 koniec⁦x⁩؜"
        self.assertEqual(rt.strip_unsafe(dirty), "Cześćevil[31m koniecx")

    def test_line_breaks_and_non_breaking_spaces_are_normalised(self):
        self.assertEqual(rt.strip_unsafe("a\r\nb\rc d\te"), "a\nb\nc d\te")

    def test_normalize_trims_lines_and_squeezes_blank_runs(self):
        self.assertEqual(rt.normalize("  Dzień dobry  \n\n\n\n\nPozdrawiam   \n\n"), "Dzień dobry\n\nPozdrawiam")

    def test_plain_text_wins_and_html_is_the_fallback(self):
        self.assertEqual(rt.body_text("Zwykły tekst", "<p>Inny</p>"), "Zwykły tekst")
        self.assertEqual(rt.body_text("", "<p>Tylko <b>HTML</b></p>"), "Tylko HTML")
        self.assertEqual(rt.body_text("  \n ", "<p>HTML</p>"), "HTML")
        self.assertEqual(rt.body_text("", ""), "")

    def test_collapse_gives_one_safe_line(self):
        self.assertEqual(rt.collapse("a\n\n b\t‮ c\x00"), "a b c")


class HtmlToTextTests(unittest.TestCase):
    def test_scripts_styles_and_hidden_parts_never_reach_the_text(self):
        html = ("<html><head><title>Tytuł</title><style>p{color:red}</style></head><body>"
                "<script>alert('x')</script><p>Widoczny</p><noscript>nie</noscript><iframe src='http://x'>w ramce</iframe>"
                "<svg><text>obrazek</text></svg></body></html>")
        self.assertEqual(rt.html_to_text(html), "Widoczny")

    def test_blocks_become_lines_and_entities_are_decoded(self):
        html = "<div>Dzień dobry,</div><div>proszę o ofertę &amp; cennik&nbsp;&mdash; dziękuję</div><p>Jan</p>"
        self.assertEqual(rt.html_to_text(html), "Dzień dobry,\nproszę o ofertę & cennik — dziękuję\nJan")

    def test_line_breaks_lists_and_whitespace(self):
        html = "Linia 1<br>Linia   2<br/><ul><li>pierwszy</li><li>drugi</li></ul>"
        self.assertEqual(rt.html_to_text(html), "Linia 1\nLinia 2\n- pierwszy\n- drugi")

    def test_preformatted_text_keeps_its_spacing(self):
        self.assertEqual(rt.html_to_text("<pre>a   b\n  c</pre>"), "a   b\n  c")

    def test_a_blockquote_becomes_quoted_lines_so_the_quote_can_be_found(self):
        html = "<div>Moja odpowiedź</div><blockquote><div>Cytowany tekst</div><blockquote>Głębiej</blockquote></blockquote>"
        self.assertEqual(rt.html_to_text(html), "Moja odpowiedź\n> Cytowany tekst\n> > Głębiej")

    def test_a_double_break_is_a_blank_line_and_a_single_one_is_not(self):
        self.assertEqual(rt.html_to_text("a<br>b"), "a\nb")
        self.assertEqual(rt.html_to_text("a<br><br>b"), "a\n\nb")
        self.assertEqual(rt.html_to_text("<div>a</div>b"), "a\nb")  # koniec bloku kończy linię

    def test_preformatted_blocks_keep_indentation_and_normal_text_after_them_is_collapsed_again(self):
        self.assertEqual(rt.html_to_text("<div>x</div><pre>  wcięte\n   bardziej</pre><div>c   d</div>"), "x\n  wcięte\n   bardziej\nc d")

    def test_text_after_a_closed_blockquote_or_skipped_tag_is_not_swallowed_or_quoted(self):
        self.assertEqual(rt.html_to_text("<blockquote>q</blockquote>po"), "> q\npo")
        self.assertEqual(rt.html_to_text("<head><style>x</style>LEAK</head>tekst"), "tekst")
        self.assertEqual(rt.html_to_text("<script>a</script><p>widać</p>"), "widać")

    def test_broken_html_does_not_raise(self):
        for html in ("<div><p>niedomknięte", "</div></p></blockquote>tekst", "<<<>>>", "<p a='b", "&#xZZ; &bogus;"):
            with self.subTest(html=html):
                self.assertIsInstance(rt.html_to_text(html), str)

    def test_hostile_markup_is_text_not_markup(self):
        text = rt.html_to_text("<p><img src=x onerror=alert(1)>&lt;script&gt;alert(1)&lt;/script&gt;</p>")
        self.assertEqual(text, "<script>alert(1)</script>")  # jako tekst: panel i tak pokazuje go przez textContent


class SplitQuoteTests(unittest.TestCase):
    def test_a_message_without_a_quote_is_all_new(self):
        result = rt.split_quote("Dzień dobry,\n\nproszę o ofertę.\n\nPozdrawiam")
        self.assertEqual((result.new, result.quoted, result.has_quote), ("Dzień dobry,\n\nproszę o ofertę.\n\nPozdrawiam", "", False))

    def test_a_reply_above_gmail_style_quote(self):
        text = ("Proszę o cennik.\n\nW dniu pon., 5 paź 2026 o 10:00 Roman Patlai <roman@z3x.pl> napisał(a):\n\n"
                "> Dzień dobry,\n> piszę w sprawie płatności.\n>\n> Roman")
        result = rt.split_quote(text)
        self.assertEqual(result.new, "Proszę o cennik.")
        self.assertIn("piszę w sprawie płatności", result.quoted)
        self.assertTrue(result.quoted.startswith("W dniu pon."))

    def test_a_reply_below_the_quote_is_not_lost(self):  # odpowiedź Impromanii: odpisane pod cytatem
        text = ("W dniu pon., 5 paź 2026 o 10:00 Roman Patlai <roman@z3x.pl> napisał(a):\n"
                "> Czy rezerwacje przyjmują Państwo tylko telefonicznie?\n>\n> Roman\n\n"
                "Tak, tylko telefonicznie. Chętnie zobaczymy ofertę.\n\nPozdrawiam, Anna")
        result = rt.split_quote(text)
        self.assertEqual(result.new, "Tak, tylko telefonicznie. Chętnie zobaczymy ofertę.\n\nPozdrawiam, Anna")
        self.assertIn("Czy rezerwacje przyjmują", result.quoted)

    def test_interleaved_answers_keep_every_answer(self):
        text = "> Pytanie pierwsze?\nOdpowiedź pierwsza.\n> Pytanie drugie?\nOdpowiedź druga."
        result = rt.split_quote(text)
        self.assertEqual(result.new, "Odpowiedź pierwsza.\nOdpowiedź druga.")
        self.assertEqual(result.quoted, "> Pytanie pierwsze?\n> Pytanie drugie?")

    def test_attributions_in_several_languages(self):
        for attribution in ("On Mon, Oct 5, 2026 at 10:00 AM Roman <roman@z3x.pl> wrote:",
                            "Am 05.10.2026 um 10:00 schrieb Roman <roman@z3x.pl>:",
                            "El lun, 5 oct 2026 a las 10:00, Roman <roman@z3x.pl> escribió:",
                            "Le lun. 5 oct. 2026 à 10:00, Roman <roman@z3x.pl> a écrit :",
                            "Dnia 5 października 2026 10:00 Roman <roman@z3x.pl> napisał:"):
            with self.subTest(attribution=attribution):
                result = rt.split_quote(f"Nowa treść\n\n{attribution}\n> stary tekst")
                self.assertEqual((result.new, result.quoted.endswith("> stary tekst")), ("Nowa treść", True))

    def test_an_attribution_wrapped_onto_two_lines(self):
        text = "Nowa treść\n\nOn Mon, Oct 5, 2026 at 10:00 AM Roman Patlai <roman@z3x.pl>\nwrote:\n> stary tekst"
        result = rt.split_quote(text)
        self.assertEqual(result.new, "Nowa treść")
        self.assertEqual(result.quoted.count("wrote:"), 1)
        self.assertTrue(result.quoted.endswith("> stary tekst"))

    def test_an_unmarked_quote_after_an_attribution_is_quoted_to_the_end(self):
        text = "Dziękuję, zainteresowani.\n\nOn Mon, Oct 5, 2026 at 10:00 AM Roman <roman@z3x.pl> wrote:\nDzień dobry,\npiszę w sprawie płatności."
        result = rt.split_quote(text)
        self.assertEqual(result.new, "Dziękuję, zainteresowani.")
        self.assertTrue(result.quoted.endswith("piszę w sprawie płatności."))

    def test_outlook_header_blocks_start_the_quote(self):
        for block in ("Od: Roman Patlai\nWysłano: poniedziałek, 5 października 2026 10:00\nDo: Jan\nTemat: Płatności\n\nTreść naszego maila",
                      "From: Roman\nSent: Monday, October 5, 2026 10:00 AM\nTo: Jan\nSubject: x\n\nBody",
                      "-----Original Message-----\nFrom: Roman\nBody",
                      "________________________________\nOd: Roman\nWysłano: wczoraj\nBody"):
            with self.subTest(block=block[:20]):
                result = rt.split_quote(f"Zainteresowani. Proszę o kontakt.\n\n{block}")
                self.assertEqual(result.new, "Zainteresowani. Proszę o kontakt.")
                self.assertTrue(result.has_quote)

    def test_a_sentence_that_merely_starts_like_an_attribution_stays_new_text(self):
        for line in ("On Tuesday we are closed:", "Dnia 5 października Roman napisał do nas w sprawie płatności i prosimy o ofertę",
                     "Am Montag sind wir nicht da"):
            with self.subTest(line=line):
                result = rt.split_quote(f"{line}\n> stary tekst")
                self.assertEqual(result.new, line)

    def test_an_intro_line_at_the_very_end_does_not_break_the_split(self):
        self.assertEqual(rt.split_quote("Dziękuję\nOn Monday").new, "Dziękuję\nOn Monday")

    def test_the_sent_line_must_follow_the_from_line_closely(self):
        near = "Od: Roman\nDo: Jan\nDW: ktoś\nWysłano: wczoraj\nTreść"
        far = "Od: Roman\nDo: Jan\nDW: ktoś\nTemat: x\nWysłano: wczoraj\nTreść"
        self.assertTrue(rt.split_quote(f"Odp.\n\n{near}").has_quote)
        self.assertFalse(rt.split_quote(f"Odp.\n\n{far}").has_quote)

    def test_the_split_result_is_immutable(self):
        with self.assertRaises(AttributeError):
            rt.split_quote("x").new = "y"  # type: ignore[misc]

    def test_a_from_line_alone_is_not_a_header_block(self):
        result = rt.split_quote("Od: poniedziałku pracujemy inaczej, proszę dzwonić.\n\nPozdrawiam")
        self.assertEqual((result.new, result.quoted), ("Od: poniedziałku pracujemy inaczej, proszę dzwonić.\n\nPozdrawiam", ""))

    def test_a_message_that_is_only_a_quote_is_returned_whole_as_new_text(self):
        result = rt.split_quote("> to jest cytat\n> i nic więcej")
        self.assertEqual((result.new, result.quoted), ("> to jest cytat\n> i nic więcej", ""))

    def test_blank_lines_inside_a_quote_stay_with_the_quote(self):
        result = rt.split_quote("Odp.\n\n> akapit 1\n\n> akapit 2")
        self.assertEqual((result.new, result.quoted), ("Odp.", "> akapit 1\n\n> akapit 2"))

    def test_empty_text(self):
        result = rt.split_quote("")
        self.assertEqual((result.new, result.quoted, result.has_quote), ("", "", False))

    def test_the_html_route_gives_the_same_split(self):
        html = "<div>Proszę o ofertę.</div><div>W dniu pon. Roman &lt;roman@z3x.pl&gt; napisał(a):</div><blockquote>Nasz mail</blockquote>"
        result = rt.split_quote(rt.html_to_text(html))
        self.assertEqual(result.new, "Proszę o ofertę.")
        self.assertIn("Nasz mail", result.quoted)


class SnippetTests(unittest.TestCase):
    def test_short_text_is_unchanged_and_whitespace_is_flattened(self):
        self.assertEqual(rt.snippet("Dzień dobry,\n\n  proszę o ofertę."), "Dzień dobry, proszę o ofertę.")

    def test_long_text_is_cut_with_an_ellipsis_at_the_limit(self):
        out = rt.snippet("słowo " * 100, 20)
        self.assertEqual(len(out), 20)
        self.assertTrue(out.endswith("…"))
        self.assertEqual(rt.snippet("x" * 160), "x" * 160)
        self.assertEqual(len(rt.snippet("x" * 161)), 160)


class OptOutTests(unittest.TestCase):
    def test_requests_in_four_languages_are_flagged(self):
        for text in ("Proszę o wypisanie nas z listy.", "Nie życzymy sobie dalszych wiadomości.", "Proszę nie wysyłać więcej maili.",
                     "Proszę usunąć nas z waszej bazy, RODO.", "Please unsubscribe me.", "Remove us from your list.", "Do not contact us again.",
                     "Bitte abmelden, keine weiteren E-Mails.", "Por favor, darse de baja.", "Zaprzestańcie wysyłania."):
            with self.subTest(text=text):
                self.assertTrue(rt.optout_hint(text))

    def test_ordinary_replies_are_not_flagged(self):
        for text in ("Dziękuję za wiadomość, proszę o cennik.", "Chętnie porozmawiamy we wtorek.", "Mamy już własny system rezerwacji.",
                     "Czy można zapisać się na demo?", ""):
            with self.subTest(text=text):
                self.assertFalse(rt.optout_hint(text))

    def test_the_quoted_original_is_not_what_gets_checked(self):
        split = rt.split_quote("Dziękuję, zainteresowani.\n\nOn Mon, Oct 5, 2026 Roman <r@z3x.pl> wrote:\n> Jeśli nie życzą sobie Państwo wiadomości, wypisz się.")
        self.assertFalse(rt.optout_hint(split.new))
        self.assertTrue(rt.optout_hint(split.quoted))


if __name__ == "__main__":
    unittest.main()
