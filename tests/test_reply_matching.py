"""Dopasowanie odpowiedzi do maili kampanii: nagłówki, adres, domena, skrzynki, zwroty i odrzucanie cudzej poczty."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from perno.domain import replies as rp
from perno.domain import reply_matching as rm
from perno.domain.reply_headers import Envelope
from perno.domain.reply_parse import BounceInfo

SENT = datetime(2026, 9, 29, 13, 0)
MAIN, SECOND = "roman@z3x.pl", "drugi@z3x.pl"


def mail(id_, email, subject="rezerwacje limuzyn, kraków", *, company=None, mailbox=MAIN, message_id="", site="", sent_at=SENT):
    return rm.SentMail(id_, company or id_, email, subject, sent_at, mailbox, message_id, site)


def env(*, sender="anna@mylimo.pl", subject="Re: Rezerwacje limuzyn, Kraków", date=SENT + timedelta(days=3), in_reply_to=(), references=(), to=()):
    return Envelope(sender=sender, to=to, subject=subject, date=date, in_reply_to=tuple(in_reply_to), references=tuple(references))


def index():
    return rm.ReplyIndex([
        mail("eventy-001", "anna@mylimo.pl", company="eventy-001", message_id="orig1@z3x.pl", site="mylimo.pl"),
        mail("eventy-001~2", "biuro@mylimo.pl", "inny temat dla biura", company="eventy-001", site="mylimo.pl"),
        mail("driverzy-005", "jan.k@gmail.com", "szkoła jazdy, gdańsk", mailbox=SECOND, message_id="orig5@z3x.pl"),
        mail("eventy-009", "kontakt@inna-firma.pl", company="eventy-009", site="inna-firma.pl"),
    ])


def match(e, **kw):
    kw.setdefault("mailbox", MAIN)
    return rm.match_message(e, index(), **kw)


class HeaderTests(unittest.TestCase):
    def test_in_reply_to_a_known_message_id_wins_whoever_writes_and_whatever_the_subject(self):
        result = match(env(sender="ktos-inny@gmail.com", subject="Zupełnie inny temat", in_reply_to=["orig1@z3x.pl"]))
        self.assertEqual((result.mail_id, result.company_id, result.method, result.weak), ("eventy-001", "eventy-001", "header", False))

    def test_a_reply_to_one_of_our_stored_replies_stays_in_its_thread(self):
        known = {"panel-odp-1@z3x.pl": "eventy-001"}
        result = match(env(sender="x@gmail.com", subject="b/d", in_reply_to=["panel-odp-1@z3x.pl"]), known=known)
        self.assertEqual((result.mail_id, result.method), ("eventy-001", "header"))

    def test_the_closest_ancestor_is_tried_first(self):
        e = env(sender="x@gmail.com", in_reply_to=["orig5@z3x.pl"], references=["orig1@z3x.pl", "orig5@z3x.pl"])
        self.assertEqual(match(e).mail_id, "driverzy-005")
        deeper = env(sender="x@gmail.com", in_reply_to=["nieznany@x"], references=["orig1@z3x.pl", "nieznany@x"])
        self.assertEqual(match(deeper).mail_id, "eventy-001")  # nieznany przodek jest pomijany, szukamy dalej

    def test_unknown_ids_fall_through_to_the_other_rules(self):
        result = match(env(in_reply_to=["nikt@nigdzie"]))
        self.assertEqual(result.method, "address")

    def test_a_known_thread_whose_origin_is_not_in_the_index_is_skipped_not_fatal(self):
        self.assertIsNone(match(env(sender="x@gmail.com", in_reply_to=["a@b"]), known={"a@b": "nie-ma-takiego"}))


class AddressTests(unittest.TestCase):
    def test_the_exact_address_with_the_same_subject_matches(self):
        result = match(env())
        self.assertEqual((result.mail_id, result.method, result.weak, result.other_mailbox), ("eventy-001", "address", False, False))

    def test_a_changed_subject_still_matches_inside_the_reply_window_only(self):
        for days, expected in ((3, True), (60, True), (61, False)):
            with self.subTest(days=days):
                result = match(env(subject="Zupełnie co innego", date=SENT + timedelta(days=days, minutes=-1) if days == 61 else SENT + timedelta(days=days)))
                self.assertEqual(result is not None, expected)

    def test_a_reply_dated_slightly_before_the_recorded_send_time_is_accepted_but_not_two_days_before(self):
        self.assertIsNotNone(match(env(subject="Co innego", date=SENT - timedelta(hours=1))))  # sent_at bywa przesunięte o godzinę
        self.assertIsNone(match(env(subject="Co innego", date=SENT - timedelta(days=2))))

    def test_the_clock_skew_boundary_is_exactly_one_day(self):
        self.assertIsNotNone(match(env(subject="Co innego", date=SENT - timedelta(days=1))))
        self.assertIsNone(match(env(subject="Co innego", date=SENT - timedelta(days=1, seconds=1))))

    def test_without_a_date_only_the_subject_can_tie_a_reply_to_a_mail(self):
        self.assertIsNotNone(match(env(date=None)))
        self.assertIsNone(match(env(subject="Co innego", date=None)))

    def test_the_address_comparison_is_exact_not_by_domain(self):
        result = match(env(sender="sekretariat@mylimo.pl", subject="Re: Rezerwacje limuzyn, Kraków"))
        self.assertEqual(result.method, "domain")  # nie „address”: to nie ten adres

    def test_the_company_second_address_matches_its_own_mail(self):
        result = match(env(sender="biuro@mylimo.pl", subject="Re: Inny temat dla biura"))
        self.assertEqual((result.mail_id, result.company_id), ("eventy-001~2", "eventy-001"))

    def test_with_several_candidates_the_same_mailbox_then_the_subject_then_the_latest_wins(self):
        sent = [mail("a-old", "x@firma.pl", "temat", mailbox=MAIN, sent_at=SENT),
                mail("a-new", "x@firma.pl", "temat", mailbox=MAIN, sent_at=SENT + timedelta(days=5)),
                mail("a-other-box", "x@firma.pl", "temat", mailbox=SECOND, sent_at=SENT + timedelta(days=9)),
                mail("a-other-subject", "x@firma.pl", "co innego", mailbox=MAIN, sent_at=SENT + timedelta(days=9))]
        idx = rm.ReplyIndex(sent)
        e = env(sender="x@firma.pl", subject="Re: Temat", date=SENT + timedelta(days=12))
        self.assertEqual(rm.match_message(e, idx, mailbox=MAIN).mail_id, "a-new")
        self.assertEqual(rm.match_message(e, idx, mailbox=SECOND).mail_id, "a-other-box")

    def test_a_reply_that_landed_in_another_mailbox_is_matched_and_flagged(self):
        result = match(env(), mailbox="trzecia@z3x.pl")
        self.assertEqual((result.mail_id, result.other_mailbox), ("eventy-001", True))
        self.assertFalse(match(env(), mailbox=MAIN).other_mailbox)

    def test_mail_sent_before_the_mailbox_was_recorded_counts_as_the_main_mailbox(self):
        row = {"id": "x-1", "company_id": None, "email": "A@B.pl", "subject": "Temat", "sent_at": SENT, "sent_from": None, "message_id": None, "website": ""}
        self.assertEqual(rm.sent_mail_from_row(row, MAIN).mailbox, MAIN)


class DomainTests(unittest.TestCase):
    def test_a_colleague_from_the_same_company_with_the_same_subject_is_a_weak_match(self):
        result = match(env(sender="sekretariat@mylimo.pl"))
        self.assertEqual((result.mail_id, result.company_id, result.method, result.weak), ("eventy-001", "eventy-001", "domain", True))

    def test_it_also_works_through_the_company_website_domain(self):
        sent = [mail("w-1", "kontakt@poczta-firmy.pl", "temat", site="mojafirma.pl")]
        e = env(sender="jan@mojafirma.pl", subject="Re: Temat")
        self.assertEqual(rm.match_message(e, rm.ReplyIndex(sent), mailbox=MAIN).method, "domain")

    def test_a_different_subject_does_not_match_by_domain(self):
        self.assertIsNone(match(env(sender="sekretariat@mylimo.pl", subject="Re: Faktura za wrzesień", date=SENT + timedelta(days=90))))

    def test_public_mail_domains_never_identify_a_company(self):
        self.assertIsNone(match(env(sender="inny.jan@gmail.com", subject="Re: Szkoła jazdy, Gdańsk", date=SENT + timedelta(days=90))))

    def test_two_companies_with_the_same_domain_and_subject_are_not_guessed(self):
        sent = [mail("c-1", "a@wspolna.pl", "temat", company="c-1"), mail("c-2", "b@wspolna.pl", "temat", company="c-2")]
        e = env(sender="ktos@wspolna.pl", subject="Re: Temat")
        self.assertIsNone(rm.match_message(e, rm.ReplyIndex(sent), mailbox=MAIN))

    def test_an_empty_subject_never_matches_by_domain(self):
        self.assertIsNone(match(env(sender="sekretariat@mylimo.pl", subject="Re:", date=SENT + timedelta(days=90))))


class DirectionTests(unittest.TestCase):
    def test_our_own_reply_in_the_sent_folder_is_matched_through_its_recipient(self):
        e = env(sender=MAIN, to=("anna@mylimo.pl",), subject="Re: Rezerwacje limuzyn, Kraków")
        result = match(e, direction=rp.DIRECTION_OUT)
        self.assertEqual((result.mail_id, result.method), ("eventy-001", "address"))

    def test_an_incoming_message_is_matched_through_the_sender_not_the_recipient(self):
        e = env(sender="obcy@gmail.com", to=("anna@mylimo.pl",), subject="Co innego", date=SENT + timedelta(days=90))
        self.assertIsNone(match(e))

    def test_counterpart_addresses(self):
        e = env(sender="a@b.pl", to=("c@d.pl", "e@f.pl"))
        self.assertEqual(rm.counterpart(e, rp.DIRECTION_IN), ("a@b.pl",))
        self.assertEqual(rm.counterpart(e, rp.DIRECTION_OUT), ("c@d.pl", "e@f.pl"))
        self.assertEqual(rm.counterpart(env(sender=""), rp.DIRECTION_IN), ())


class FilterTests(unittest.TestCase):
    def test_unrelated_work_mail_is_not_wanted(self):
        e = env(sender="newsletter@sklep.example", subject="Wielka wyprzedaż!", date=SENT + timedelta(days=1))
        self.assertIsNone(match(e))
        self.assertFalse(rm.wants_full(e, rp.KIND_HUMAN, None, index()))

    def test_a_matched_message_a_bounce_and_a_campaign_subject_are_wanted(self):
        idx = index()
        matched = rm.match_message(env(), idx, mailbox=MAIN)
        self.assertTrue(rm.wants_full(env(), rp.KIND_HUMAN, matched, idx))
        self.assertTrue(rm.wants_full(env(sender="mailer-daemon@x.pl", subject="Undelivered"), rp.KIND_BOUNCE, None, idx))
        stranger = env(sender="nieznany@trzecia.pl", subject="Re: Szkoła jazdy, Gdańsk", date=SENT + timedelta(days=90))
        self.assertIsNone(rm.match_message(stranger, idx, mailbox=MAIN))
        self.assertTrue(rm.is_campaign_subject(stranger, idx))
        self.assertTrue(rm.wants_full(stranger, rp.KIND_HUMAN, None, idx))

    def test_an_empty_subject_is_never_a_campaign_subject(self):
        self.assertFalse(rm.is_campaign_subject(env(subject=""), index()))
        self.assertFalse(rm.is_campaign_subject(env(subject="Re: "), index()))


class BounceTests(unittest.TestCase):
    def test_the_original_message_id_is_the_best_proof(self):
        found = rm.match_bounce(BounceInfo(("ktokolwiek@x.pl",), True, "5.1.1", "orig5@z3x.pl"), index())
        self.assertEqual([(m.mail_id, m.method) for m in found], [("driverzy-005", "header")])

    def test_without_it_the_failed_recipients_are_looked_up_by_address(self):
        found = rm.match_bounce(BounceInfo(("anna@mylimo.pl", "kontakt@inna-firma.pl", "nikt@nigdzie.pl"), True), index())
        self.assertEqual([(m.mail_id, m.method) for m in found], [("eventy-001", "address"), ("eventy-009", "address")])

    def test_an_unknown_bounce_matches_nothing(self):
        self.assertEqual(rm.match_bounce(BounceInfo(("nikt@nigdzie.pl",), True, "", "nieznany@x"), index()), [])
        self.assertEqual(rm.match_bounce(BounceInfo((), False), index()), [])

    def test_the_latest_mail_to_an_address_is_the_one_that_bounced(self):
        sent = [mail("s-1", "x@y.pl", sent_at=SENT), mail("s-2", "x@y.pl", sent_at=SENT + timedelta(days=7))]
        self.assertEqual(rm.match_bounce(BounceInfo(("x@y.pl",), True), rm.ReplyIndex(sent))[0].mail_id, "s-2")


class ValueObjectTests(unittest.TestCase):
    def test_a_match_is_strong_and_in_the_right_mailbox_unless_said_otherwise_and_cannot_be_changed(self):
        found = rm.Match("a", "b", rp.MATCH_HEADER)
        self.assertEqual((found.weak, found.other_mailbox), (False, False))
        with self.assertRaises(AttributeError):
            found.weak = True  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            mail("x", "a@b.pl").email = "c@d.pl"  # type: ignore[misc]
        self.assertEqual((mail("x", "a@b.pl").message_id, mail("x", "a@b.pl").site_domain), ("", ""))


class IndexTests(unittest.TestCase):
    def test_public_domains_and_missing_ids_are_not_indexed(self):
        idx = index()
        self.assertNotIn("gmail.com", idx.by_domain)
        self.assertEqual(sorted(idx.by_domain), ["inna-firma.pl", "mylimo.pl"])
        self.assertEqual(sorted(idx.by_message_id), ["orig1@z3x.pl", "orig5@z3x.pl"])
        self.assertIn("rezerwacje limuzyn, kraków", idx.subjects)
        self.assertEqual(len(idx.by_domain["mylimo.pl"]), 2)

    def test_rows_become_sent_mails_with_normalised_fields(self):
        row = {"id": "e-1", "company_id": "firma-7", "email": " Anna@MyLimo.PL ", "subject": "RE: Temat X", "sent_at": SENT,
               "sent_from": "Drugi@Z3X.pl", "message_id": "abc@z3x.pl", "website": "https://www.mylimo.pl/oferta"}
        self.assertEqual(rm.sent_mail_from_row(row, MAIN), rm.SentMail("e-1", "firma-7", "anna@mylimo.pl", "temat x", SENT, "drugi@z3x.pl", "abc@z3x.pl", "mylimo.pl"))

    def test_a_row_without_a_company_uses_its_own_id(self):
        row = {"id": "e-1", "email": "a@b.pl", "subject": "", "sent_at": None, "website": ""}
        self.assertEqual(rm.sent_mail_from_row(row, MAIN).company_id, "e-1")


if __name__ == "__main__":
    unittest.main()
