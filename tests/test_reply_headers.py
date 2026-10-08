"""Nagłówki przychodzące: odczyt do Envelope, normalizacja tematu i rozpoznanie rodzaju wiadomości."""

from __future__ import annotations

import unittest
from datetime import datetime

from perno.domain import replies as rp
from perno.domain import reply_headers as rh
from tests import eml_samples as eml


def env(raw_message: bytes) -> rh.Envelope:
    return rh.parse_envelope(raw_message)


class EnvelopeTests(unittest.TestCase):
    def test_a_plain_reply_is_read_into_comparable_fields(self):
        e = env(eml.plain_reply(in_reply_to="<Orig1@Z3X.pl>", references="<first@z3x.pl> <Orig1@Z3X.pl>", extra="Reply-To: Biuro <Biuro@MyLimo.pl>"))
        self.assertEqual((e.sender, e.sender_name, e.to), ("anna@mylimo.pl", "Anna Nowak", ("roman@z3x.pl",)))
        self.assertEqual((e.subject, e.message_id), ("Re: Rezerwacje limuzyn, Kraków", "odp1@mylimo.pl"))
        self.assertEqual((e.in_reply_to, e.references), (("orig1@z3x.pl",), ("first@z3x.pl", "orig1@z3x.pl")))
        self.assertEqual(e.reply_to, ("biuro@mylimo.pl",))

    def test_the_date_becomes_a_naive_local_time(self):
        e = env(eml.plain_reply(date="Fri, 02 Oct 2026 12:00:00 +0200"))
        expected = datetime.fromisoformat("2026-10-02T12:00:00+02:00").astimezone().replace(tzinfo=None)
        self.assertEqual(e.date, expected)
        self.assertIsNone(e.date.tzinfo)

    def test_an_unparsable_or_missing_date_is_none(self):
        self.assertIsNone(env(eml.plain_reply(date="wczoraj wieczorem")).date)
        self.assertIsNone(env(b"From: a@b.pl\r\nSubject: x\r\n\r\n").date)

    def test_encoded_words_are_decoded_and_names_are_cleaned(self):
        e = env(eml.multipart_reply_with_attachment())
        self.assertEqual(e.sender_name, "Zbigniew Wiśniewski")
        self.assertEqual(e.subject, "Re: Temat z ą ę")

    def test_hostile_header_text_is_made_safe(self):
        e = env(b"From: =?UTF-8?B?4oCuZXZpbOKArA==?= <a@b.pl>\r\nSubject: sztuczka\x00\x07 \xe2\x80\xaeodwr\xe2\x80\xac\r\n\r\n")
        self.assertNotIn("‮", e.subject + e.sender_name)
        self.assertNotIn("\x00", e.subject)

    def test_defaults_describe_a_message_without_any_special_header(self):
        e = rh.Envelope()
        self.assertEqual((e.autoreply_flag, e.sender, e.to, e.date, e.references_all), (False, "", (), None, ()))
        with self.assertRaises(AttributeError):
            e.sender = "x"  # type: ignore[misc]

    def test_very_long_names_and_subjects_are_cut(self):
        e = env(f"From: {'N' * 300} <a@b.pl>\r\nSubject: {'T' * 600}\r\n\r\n".encode())
        self.assertEqual((len(e.sender_name), len(e.subject)), (200, 500))

    def test_missing_and_garbled_headers_give_empty_fields_not_errors(self):
        e = env(b"Subject: tylko temat\r\n\r\n")
        self.assertEqual((e.sender, e.to, e.message_id, e.in_reply_to, e.references), ("", (), "", (), ()))
        self.assertEqual(env(b"").subject, "")
        self.assertEqual(env(b"From: <<<\r\nTo: ;;;\r\nMessage-ID: bez-nawiasow\r\n\r\n").message_id, "bez-nawiasow")

    def test_several_recipients_are_collected_with_copies(self):
        e = env(b"From: a@b.pl\r\nTo: X <x@y.pl>, z@y.pl\r\nCc: w@y.pl, X <X@Y.pl>\r\n\r\n")
        self.assertEqual(e.to, ("x@y.pl", "z@y.pl", "w@y.pl"))

    def test_references_all_lists_the_closest_ancestor_first_without_repeats(self):
        e = env(eml.plain_reply(in_reply_to="<c@x>", references="<a@x> <b@x> <c@x>"))
        self.assertEqual(e.references_all, ("c@x", "b@x", "a@x"))


class NormalizeSubjectTests(unittest.TestCase):
    def test_reply_and_forward_prefixes_in_many_languages_are_dropped_even_when_stacked(self):
        for subject in ("Re: Temat", "RE: Temat", "Odp: Temat", "Sv: Temat", "AW: Temat", "Re[2]: Temat", "Fwd: Re: Temat",
                        "re:re: Temat", "  Odp:   Temat  ", "Automatic reply: Temat", "Odpowiedź automatyczna: Temat",
                        "Out of Office: Temat", "Abwesenheitsnotiz: Re: Temat", "TR: Temat"):
            with self.subTest(subject=subject):
                self.assertEqual(rh.normalize_subject(subject), "temat")

    def test_only_leading_prefixes_go_and_words_stay(self):
        self.assertEqual(rh.normalize_subject("Rezerwacje limuzyn, Kraków"), "rezerwacje limuzyn, kraków")
        self.assertEqual(rh.normalize_subject("Re: Dotyczy re: oferty"), "dotyczy re: oferty")
        self.assertEqual(rh.normalize_subject("Restauracja Re: Pod Lipą"), "restauracja re: pod lipą")
        self.assertEqual(rh.normalize_subject("Revolut: płatności"), "revolut: płatności")

    def test_empty_and_odd_subjects(self):
        self.assertEqual(rh.normalize_subject(""), "")
        self.assertEqual(rh.normalize_subject("Re:"), "")
        self.assertEqual(rh.normalize_subject("Re: ‮X‬"), "x")


class KindTests(unittest.TestCase):
    def test_ordinary_replies_are_human(self):
        for sample in (eml.plain_reply(), eml.html_only_reply(), eml.multipart_reply_with_attachment()):
            self.assertEqual(rh.kind_of(env(sample)), rp.KIND_HUMAN)

    def test_auto_replies_are_recognised_by_headers_and_by_subject(self):
        self.assertEqual(rh.kind_of(env(eml.auto_reply())), rp.KIND_AUTO)
        for extra in ("Auto-Submitted: auto-generated", "X-Autoreply: yes", "X-Autorespond: yes", "Precedence: auto_reply", "Precedence: bulk"):
            with self.subTest(extra=extra):
                self.assertEqual(rh.kind_of(env(eml.plain_reply(extra=extra))), rp.KIND_AUTO)
        for subject in ("Automatic reply: x", "Odpowiedź automatyczna: x", "Out of Office: x", "Abwesenheitsnotiz: x", "AUTO: x"):
            with self.subTest(subject=subject):
                self.assertEqual(rh.kind_of(env(eml.plain_reply(subject=subject))), rp.KIND_AUTO)

    def test_auto_submitted_no_is_a_person(self):
        self.assertEqual(rh.kind_of(env(eml.plain_reply(extra="Auto-Submitted: no"))), rp.KIND_HUMAN)

    def test_bounces_are_recognised_by_daemon_sender_report_type_or_failed_recipients(self):
        for sample in (eml.dsn_bounce(), eml.text_bounce(), eml.soft_bounce()):
            self.assertEqual(rh.kind_of(env(sample)), rp.KIND_BOUNCE)
        self.assertEqual(rh.kind_of(env(eml.plain_reply(sender="postmaster@firma.pl"))), rp.KIND_BOUNCE)
        report = eml.plain_reply(extra="Content-Type: multipart/report; report-type=delivery-status; boundary=x")
        self.assertEqual(rh.kind_of(env(report.replace(b"Content-Type: text/plain; charset=utf-8", b"X-A: b"))), rp.KIND_BOUNCE)

    def test_other_multipart_reports_such_as_read_receipts_are_not_bounces(self):
        receipt = eml.plain_reply(extra="Content-Type: multipart/report; report-type=disposition-notification; boundary=x")
        self.assertNotEqual(rh.kind_of(env(receipt.replace(b"Content-Type: text/plain; charset=utf-8", b"X-A: b"))), rp.KIND_BOUNCE)

    def test_a_subject_that_merely_looks_like_a_bounce_is_not_one_for_a_person(self):
        self.assertEqual(rh.kind_of(env(eml.plain_reply(subject="Undelivered Mail Returned to Sender"))), rp.KIND_HUMAN)
        generated = eml.plain_reply(subject="Undelivered Mail Returned to Sender", extra="Auto-Submitted: auto-generated")
        self.assertEqual(rh.kind_of(env(generated)), rp.KIND_BOUNCE)

    def test_a_bounce_wins_over_an_auto_reply(self):
        self.assertEqual(rh.kind_of(env(eml.dsn_bounce())), rp.KIND_BOUNCE)  # ma też Auto-Submitted: auto-replied


class IgnoredTests(unittest.TestCase):
    def test_our_own_mailboxes_and_listed_addresses_or_domains_are_ignored(self):
        e = env(eml.internal_forward())
        self.assertTrue(rh.is_ignored(e, ["jakub@z3x.io"], []))
        self.assertTrue(rh.is_ignored(e, [], ["Jakub@Z3X.io"]))
        self.assertTrue(rh.is_ignored(e, [], ["z3x.io"]))
        self.assertTrue(rh.is_ignored(e, [], ["  z3x.io  "]))

    def test_other_senders_are_not(self):
        e = env(eml.plain_reply())
        self.assertFalse(rh.is_ignored(e, ["roman@z3x.pl"], ["z3x.io", "jakub@z3x.io", ""]))
        self.assertFalse(rh.is_ignored(env(b"Subject: x\r\n\r\n"), [""], [""]))  # brak nadawcy nie pasuje do pustej reguły


if __name__ == "__main__":
    unittest.main()
