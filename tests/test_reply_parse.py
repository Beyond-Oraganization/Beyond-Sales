"""Pełna wiadomość: treść, załączniki i dane zwrotu (raport doręczenia, zwrot bez raportu, zwrot trwały i tymczasowy)."""

from __future__ import annotations

import unittest

from perno.domain import replies as rp
from perno.domain import reply_parse as pp
from tests import eml_samples as eml


class BodyTests(unittest.TestCase):
    def test_a_plain_reply_keeps_its_text_and_is_a_human(self):
        parsed = pp.parse_message(eml.plain_reply(body="Dzień dobry,\n\nproszę o cennik.\n\nAnna"))
        self.assertEqual((parsed.kind, parsed.text), (rp.KIND_HUMAN, "Dzień dobry,\n\nproszę o cennik.\n\nAnna"))
        self.assertEqual((parsed.envelope.sender, parsed.attachments, parsed.bounce), ("anna@mylimo.pl", (), None))

    def test_an_html_only_message_is_turned_into_text_with_the_quote_marked(self):
        parsed = pp.parse_message(eml.html_only_reply())
        self.assertIn("Zainteresowani. Proszę o kontakt.", parsed.text)
        self.assertIn("> Dzień dobry, piszę w sprawie płatności.", parsed.text)
        self.assertNotIn("p{}", parsed.text)  # style nie wycieka

    def test_the_text_version_wins_over_the_html_one_in_a_multipart(self):
        parsed = pp.parse_message(eml.multipart_reply_with_attachment())
        self.assertEqual(parsed.text, "Wersja tekstowa odpowiedzi.")

    def test_attachments_are_only_described(self):
        parsed = pp.parse_message(eml.multipart_reply_with_attachment())
        self.assertEqual(parsed.attachments, (pp.Attachment("oferta.pdf", "application/pdf", 9),))

    def test_dangerous_characters_in_the_body_and_in_file_names_are_removed(self):
        parsed = pp.parse_message(eml.plain_reply(body="Tekst‮ z \x00 pułapką​"))
        self.assertEqual(parsed.text, "Tekst z  pułapką")
        raw = eml.multipart_reply_with_attachment().replace(b'filename="oferta.pdf"', b'filename="a\\u202eb.pdf"')
        self.assertTrue(all("‮" not in item.name for item in pp.parse_message(raw).attachments))

    def test_an_unknown_charset_does_not_stop_the_parse(self):
        raw = eml.plain_reply().replace(b"charset=utf-8", b"charset=x-nieznane")
        self.assertIn("proszę o cennik", pp.parse_message(raw).text)

    def test_garbage_and_empty_input_never_raise(self):
        for raw in (b"", b"\x00\x01\x02", b"To nie jest mail", b"From: a@b.pl\r\n\r\n", b"Content-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\n"):
            with self.subTest(raw=raw[:12]):
                self.assertIsInstance(pp.parse_message(raw).text, str)

    def test_very_long_bodies_are_cut(self):
        parsed = pp.parse_message(eml.plain_reply(body="a" * (pp.MAX_BODY + 5000)))
        self.assertEqual((len(parsed.text), pp.MAX_BODY), (200_000, 200_000))

    def test_the_number_of_listed_attachments_is_limited(self):
        parts = "".join(f'--B\nContent-Type: text/plain; name="f{n}.txt"\nContent-Disposition: attachment; filename="f{n}.txt"\n\nx\n' for n in range(40))
        raw = eml.raw("From: a@b.pl\nSubject: x\nMIME-Version: 1.0\nContent-Type: multipart/mixed; boundary=B\n\n--B\nContent-Type: text/plain\n\nTreść\n" + parts + "--B--")
        self.assertEqual((len(pp.parse_message(raw).attachments), pp.MAX_ATTACHMENTS), (20, 20))


    def test_an_attached_text_file_is_not_mistaken_for_the_body(self):
        raw = eml.raw("""From: a@b.pl
Subject: x
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary=B

--B
Content-Type: text/plain; name="notatka.txt"
Content-Disposition: attachment; filename="notatka.txt"

TO JEST ZAŁĄCZNIK
--B
Content-Type: text/plain; charset=utf-8

Prawdziwa treść
--B--""")
        parsed = pp.parse_message(raw)
        self.assertEqual(parsed.text, "Prawdziwa treść")
        self.assertEqual([a.name for a in parsed.attachments], ["notatka.txt"])

    def test_the_first_of_several_html_parts_is_used(self):
        raw = eml.raw("""From: a@b.pl
Subject: x
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary=B

--B
Content-Type: text/html; charset=utf-8

<p>pierwszy</p>
--B
Content-Type: text/html; charset=utf-8

<p>drugi</p>
--B--""")
        self.assertEqual(pp.parse_message(raw).text, "pierwszy")

    def test_inline_images_count_only_when_they_have_a_name_and_long_names_are_cut(self):
        raw = eml.raw(f"""From: a@b.pl
Subject: x
MIME-Version: 1.0
Content-Type: multipart/related; boundary=B

--B
Content-Type: text/plain; charset=utf-8

Treść
--B
Content-Type: image/png
Content-ID: <logo@x>
Content-Disposition: inline

AAAA
--B
Content-Type: image/png; name="{'d' * 300}.png"
Content-Disposition: inline; filename="{'d' * 300}.png"

BBBB
--B
Content-Type: message/rfc822
Content-Disposition: attachment; filename="przekazany.eml"

From: x@y.pl
Subject: stary

stary mail
--B--""")
        found = pp.parse_message(raw).attachments
        self.assertEqual([(len(a.name), a.content_type) for a in found], [(120, "image/png"), (14, "message/rfc822")])
        self.assertEqual(found[1].size, 0)  # sam opis: wiadomości w załączniku nie czytamy

    def test_the_body_and_attachments_of_a_forwarded_message_are_not_the_replys_own(self):
        raw = eml.raw("""From: a@b.pl
Subject: Fwd: x
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary=B

--B
Content-Type: message/rfc822
Content-Disposition: attachment; filename="przekazany.eml"

From: x@y.pl
Subject: stary
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary=I

--I
Content-Type: text/plain

TREŚĆ PRZEKAZANEGO
--I
Content-Type: application/pdf; name="wewnetrzny.pdf"
Content-Disposition: attachment; filename="wewnetrzny.pdf"

AAAA
--I--
--B--""")
        parsed = pp.parse_message(raw)
        self.assertEqual(parsed.text, "")
        self.assertEqual([a.name for a in parsed.attachments], ["przekazany.eml"])

    def test_the_value_objects_cannot_be_changed(self):
        parsed = pp.parse_message(eml.plain_reply())
        with self.assertRaises(AttributeError):
            parsed.text = "x"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            pp.Attachment("a", "b", 1).size = 2  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            pp.BounceInfo((), True).hard = False  # type: ignore[misc]
        self.assertFalse(pp.BounceInfo((), True).guessed)


class BounceTests(unittest.TestCase):
    def test_a_delayed_report_without_a_status_is_soft_and_not_a_failure(self):
        b = pp.parse_message(eml.dsn_bounce(status="", action="delayed")).bounce
        self.assertEqual((b.recipients, b.hard, b.kind), (("nieistnieje@firma.pl",), False, rp.BOUNCE_SOFT))

    def test_a_delivery_report_gives_the_recipient_the_status_and_the_original_id(self):
        parsed = pp.parse_message(eml.dsn_bounce(recipient="Nieistnieje@Firma.pl", original_id="<Orig1@Z3X.pl>"))
        self.assertEqual(parsed.kind, rp.KIND_BOUNCE)
        b = parsed.bounce
        self.assertEqual((b.recipients, b.hard, b.status, b.original_message_id, b.guessed, b.kind),
                         (("nieistnieje@firma.pl",), True, "5.1.1", "orig1@z3x.pl", False, rp.BOUNCE_HARD))

    def test_an_embedded_original_message_works_like_attached_headers(self):
        b = pp.parse_message(eml.dsn_bounce(as_message=True, original_id="<Wbudowany@z3x.pl>")).bounce
        self.assertEqual(b.original_message_id, "wbudowany@z3x.pl")

    def test_a_temporary_failure_is_soft(self):
        b = pp.parse_message(eml.dsn_bounce(status="4.2.2", action="delayed")).bounce
        self.assertEqual((b.hard, b.status, b.kind), (False, "4.2.2", rp.BOUNCE_SOFT))

    def test_a_failure_action_is_hard_even_without_a_five_status(self):
        self.assertTrue(pp.parse_message(eml.dsn_bounce(status="", action="failed")).bounce.hard)

    def test_a_bounce_without_a_report_uses_the_failed_recipients_header_and_the_phrases(self):
        b = pp.parse_message(eml.text_bounce(recipient="ktos@firma.pl")).bounce
        self.assertEqual((b.recipients, b.hard, b.guessed, b.original_message_id), (("ktos@firma.pl",), True, False, ""))

    def test_a_text_only_bounce_is_soft_unless_it_says_the_address_is_gone(self):
        soft = pp.parse_message(eml.soft_bounce("pelna@firma.pl")).bounce
        self.assertEqual((soft.hard, soft.status, soft.recipients, soft.guessed), (False, "4.2.2", ("pelna@firma.pl",), True))
        gone = pp.parse_message(eml.text_bounce(phrase="adres nie istnieje")).bounce
        self.assertTrue(gone.hard)
        vague = pp.parse_message(eml.text_bounce(phrase="coś poszło nie tak")).bounce
        self.assertFalse(vague.hard)

    def test_a_five_status_in_the_text_makes_it_hard(self):
        self.assertTrue(pp.parse_message(eml.text_bounce(phrase="Status 5.7.1 odrzucone")).bounce.hard)

    def test_several_failed_recipients_in_one_report(self):
        raw = eml.dsn_bounce(recipient="a@firma.pl").replace(
            b"Action: failed", b"Action: failed\r\nStatus: 5.1.1\r\n\r\nFinal-Recipient: rfc822; b@firma.pl\r\nAction: failed", 1)
        self.assertEqual(pp.parse_message(raw).bounce.recipients, ("a@firma.pl", "b@firma.pl"))

    def test_ordinary_messages_have_no_bounce_data(self):
        self.assertIsNone(pp.parse_message(eml.plain_reply()).bounce)
        self.assertIsNone(pp.parse_message(eml.auto_reply()).bounce)
        self.assertEqual(pp.parse_message(eml.auto_reply()).kind, rp.KIND_AUTO)


if __name__ == "__main__":
    unittest.main()
