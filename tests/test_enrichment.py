"""Uzupełnianie maili treścią z zakończonych skanów (wypelnij.py): lista, kontekst, walidacja i zapis."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import sqlalchemy as sa

from perno import settings as cfg
from perno.cli import enrich
from perno.db.schema import maile
from perno.scraper.schema import firmy, kontakty, strony, strony_tresc
from perno.scraper.store import Store
from perno.services import enrichment as enrichment_service
from perno.services import firms_import
from tests.fixtures import FakeClock, make_db, put, sample_row

SCAN_DONE, SCAN_BLOCKED, SCAN_NEW = "zakonczona", "zablokowana", "nieskanowana"

TEXTS = {
    "pl": dict(subject="Rezerwacje u Alfy", obs="prowadzą Państwo rezerwacje przez telefon",
               pitch="wprowadzić płatność online", channel_summary="potwierdzać rezerwacji"),
    "en": dict(subject="Bookings at Beta", obs="you take bookings by phone",
               pitch="introduce online payment", channel_summary="confirm bookings"),
    "de": dict(subject="Buchungen bei Echo", obs="Sie nehmen Buchungen telefonisch an",
               pitch="Online-Zahlung einführen", channel_summary="Buchungen bestätigen"),
}


def entry(row_id: str, email: str, lang: str = "pl", **overrides) -> dict:
    return {"id": row_id, "email": email, "mode": "Panstwo", "tag": "test", **TEXTS[lang], **overrides}


class EnrichmentCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.url = f"sqlite:///{os.path.join(self.tmp, 'enrich.db')}"
        self.clock = FakeClock(datetime(2026, 10, 1, 9, 0, 0))
        self.database = make_db(self.clock, url=self.url)
        self.addCleanup(self.database.engine.dispose)
        self.backups = os.path.join(self.tmp, "backup")
        self.config = os.path.join(self.tmp, "config.env")
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write("SENDER_NAME=Roman\n")
        for patcher in (mock.patch.dict(os.environ, {"PERNO_DB_URL": self.url}),
                        mock.patch.object(cfg, "BACKUP_DIR", self.backups)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.build_world()

    # ---- dane: tabela maile + wynik skanu ------------------------------------------------------
    def build_world(self):
        blank = dict(email="", body_html="", body_text="", subject="")
        put(self.database,
            sample_row(1, company="Alfa Sp. z o.o.", website="https://alfa.example.pl/", country="PL",
                       status="pominieto_brak_email", **blank),
            sample_row(2, company="Beta Ltd", website="https://beta.example.co.uk/", country="GB",
                       status="oczekuje_na_tresc", **blank),
            sample_row(3, company="Gamma SARL", website="https://gamma.example.fr/", country="FR",
                       status="pominieto_brak_email", **blank),
            sample_row(4, company="Delta", website="https://delta.example.pl/", country="PL", status="wyslano",
                       sent_at="2026-09-29T10:00:00"),
            sample_row(5, company="Foxtrot", website="https://foxtrot.example.pl/", country="PL",
                       status="pominieto_brak_email", **blank),
            sample_row(6, company="Sixt", website="https://six.example.pl/", country="PL",
                       status="pominieto_brak_email", **blank),
            sample_row(7, company="Siedem", website="https://siedem.example.pl/", country="PL",
                       status="pominieto_brak_email", **blank),
            sample_row(8, company="Osiem", website="https://osiem.example.pl/", country="PL", status="do_wyslania"),
            sample_row(1, segment="driverzy", company="Echo GmbH", website="https://echo.example.de/", country="DE",
                       status="pominieto_brak_email", **blank))
        self.store = Store(self.database)
        self.store.ensure_schema()
        self.store.seed_from_maile()
        scans = {"eventy-001": (SCAN_DONE, 12), "eventy-002": (SCAN_DONE, 12), "eventy-003": (SCAN_DONE, 12),
                 "eventy-004": (SCAN_DONE, 12), "eventy-005": (SCAN_NEW, 0), "eventy-006": (SCAN_BLOCKED, 15),
                 "eventy-007": (SCAN_BLOCKED, 3), "eventy-008": (SCAN_DONE, 12), "driverzy-001": (SCAN_DONE, 12)}
        for mail_id, (status, pages) in scans.items():
            self.update_firm(mail_id, skan_status=status, stron_pobranych=pages)
        self.add_contact("eventy-001", "email", "biuro@alfa.example.pl", w_domenie=True, rola="kontakt", pages=5)
        self.add_contact("eventy-001", "email", "jan@gmail.com", pages=1)
        self.add_contact("eventy-001", "telefon", "+48 22 123 45 67")
        self.add_contact("eventy-001", "formularz", "https://alfa.example.pl/kontakt")
        self.add_contact("eventy-002", "email", "hello@beta.example.co.uk", w_domenie=True)
        self.add_contact("eventy-003", "email", "contact@gamma.example.fr", w_domenie=True)
        self.add_contact("eventy-005", "email", "x@foxtrot.example.pl", w_domenie=True)
        self.add_contact("eventy-006", "email", "x@six.example.pl", w_domenie=True)
        self.add_contact("eventy-007", "email", "x@siedem.example.pl", w_domenie=True)
        self.add_contact("eventy-008", "email", "x@osiem.example.pl", w_domenie=True)
        self.add_contact("driverzy-001", "email", "info@echo.example.de", w_domenie=True)
        self.add_page("eventy-001", "https://alfa.example.pl/", "Alfa — wesela", "Strona główna", "Alfa organizuje wesela i bankiety.", 0)
        self.add_page("eventy-001", "https://alfa.example.pl/cennik", "Cennik", "Ceny", "Pakiet weselny od 5000 zł.", 1)
        self.add_page("eventy-001", "https://alfa.example.pl/blog/wpis-1", "Wpis", "", "Długi wpis o niczym.", 2)

    def firm_id(self, mail_id):
        with self.database.engine.connect() as conn:
            return conn.execute(sa.text("SELECT firma_id FROM firmy_maile WHERE maile_id = :id"), {"id": mail_id}).scalar()

    def update_firm(self, mail_id, **values):
        with self.database.engine.begin() as conn:
            conn.execute(sa.update(firmy).where(firmy.c.id == self.firm_id(mail_id)).values(**values))

    def add_contact(self, mail_id, kind, value, w_domenie=False, rola=None, pages=1):
        with self.database.engine.begin() as conn:
            conn.execute(sa.insert(kontakty).values(
                firma_id=self.firm_id(mail_id), typ=kind, wartosc=value, zrodlo_url="https://x.example/kontakt",
                zrodlo_typ="mailto", liczba_stron=pages, w_domenie=w_domenie, rola=rola))

    def add_page(self, mail_id, url, title, description, text, depth):
        import hashlib
        with self.database.engine.begin() as conn:
            page_id = conn.execute(sa.insert(strony).values(
                firma_id=self.firm_id(mail_id), url=url, url_hash=hashlib.sha1(url.encode(), usedforsecurity=False).hexdigest(),
                glebokosc=depth, status="pobrano", tytul=title, opis=description)).inserted_primary_key[0]
            conn.execute(sa.insert(strony_tresc).values(strona_id=page_id, tekst=text))

    # ---- uruchamianie CLI ------------------------------------------------------------------------
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = enrich.main(["--config", self.config, *argv])
        return code, out.getvalue(), err.getvalue()

    def write_json(self, entries):
        path = os.path.join(self.tmp, "tresci.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, ensure_ascii=False)
        return path

    def zapisz(self, entries, *flags):
        return self.cli("zapisz", self.write_json(entries), *flags)

    def row(self, row_id):
        return self.database.get(row_id)

    def backup_files(self):
        """Kopie tabeli maile (obok każdej powstaje też kopia firm, `Podmioty_*`)."""
        return sorted(name for name in os.listdir(self.backups) if name.startswith("Maile_")) if os.path.isdir(self.backups) else []


class ListTests(EnrichmentCase):
    def test_lists_only_finished_scans_of_rows_that_can_be_filled(self):
        code, out, _ = self.cli("lista")
        self.assertEqual(code, 0)
        self.assertIn("Kwalifikuje się 4 firm", out)
        for row_id in ("eventy-001", "eventy-002", "eventy-003", "driverzy-001"):
            self.assertIn(row_id, out)
        for row_id in ("eventy-004", "eventy-005", "eventy-006", "eventy-007", "eventy-008"):
            self.assertNotIn(row_id, out)

    def test_each_line_shows_country_language_and_scanned_addresses(self):
        _, out, _ = self.cli("lista")
        alfa = next(line for line in out.splitlines() if "eventy-001" in line)
        self.assertIn("kraj=PL", alfa)
        self.assertIn("język=pl", alfa)
        self.assertIn("e-maile ze skanu=2", alfa)
        self.assertIn("stron=12", alfa)
        self.assertIn("język=BRAK", next(line for line in out.splitlines() if "eventy-003" in line))  # FR: brak szablonu


class ContextTests(EnrichmentCase):
    def run_context(self, *extra):
        out_dir = os.path.join(self.tmp, "kontekst")
        code, out, _ = self.cli("kontekst", "--out", out_dir, *extra)
        return code, out, out_dir

    def test_writes_one_markdown_file_per_candidate(self):
        code, out, out_dir = self.run_context()
        self.assertEqual(code, 0)
        self.assertIn("Zapisano 4 plików", out)
        self.assertEqual(sorted(os.listdir(out_dir)), ["driverzy-001.md", "eventy-001.md", "eventy-002.md", "eventy-003.md"])

    def test_file_has_header_contacts_page_list_and_page_text(self):
        _, _, out_dir = self.run_context()
        with open(os.path.join(out_dir, "eventy-001.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertTrue(text.startswith("# eventy-001 — Alfa Sp. z o.o."))
        self.assertIn("strona: https://alfa.example.pl/", text)
        self.assertIn("kraj: PL  (język maila: pl)", text)
        self.assertIn("- biuro@alfa.example.pl  (w domenie: True, rola: kontakt, źródło: https://x.example/kontakt)", text)
        self.assertLess(text.index("biuro@alfa.example.pl"), text.index("jan@gmail.com"))  # w domenie pierwsze
        self.assertIn("## Telefon\n- +48 22 123 45 67", text)
        self.assertIn("## Formularz\n- https://alfa.example.pl/kontakt", text)
        self.assertIn("- https://alfa.example.pl/cennik | Cennik | Ceny", text)
        self.assertIn("### https://alfa.example.pl/\nAlfa organizuje wesela i bankiety.", text)
        self.assertIn("Pakiet weselny od 5000 zł.", text)

    def test_deep_pages_without_key_words_are_listed_but_their_text_is_skipped(self):
        _, _, out_dir = self.run_context()
        with open(os.path.join(out_dir, "eventy-001.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("- https://alfa.example.pl/blog/wpis-1 | Wpis |", text)
        self.assertNotIn("Długi wpis o niczym.", text)

    def test_character_budget_limits_the_page_text(self):
        _, _, out_dir = self.run_context("--chars", "20")
        with open(os.path.join(out_dir, "eventy-001.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("### https://alfa.example.pl/\nAlfa organizuje wese\n", text)  # 20 znaków i budżet się kończy
        self.assertNotIn("wesela i bankiety", text)
        self.assertNotIn("Pakiet weselny", text)

    def test_ids_filter_limits_the_files(self):
        code, out, out_dir = self.run_context("--id", "eventy-002")
        self.assertIn("Zapisano 1 plików", out)
        self.assertEqual(os.listdir(out_dir), ["eventy-002.md"])


class ValidationTests(EnrichmentCase):
    def assert_nothing_written(self):
        self.assertEqual(self.row("eventy-001")["status"], "pominieto_brak_email")
        self.assertEqual(self.row("eventy-001")["body_html"], "")
        self.assertEqual(self.backup_files(), [])

    def test_a_correct_file_is_accepted_by_a_dry_run_without_writing(self):
        code, out, _ = self.zapisz([entry("eventy-001", "biuro@alfa.example.pl")], "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("OK  eventy-001", out)
        self.assertIn("[pl]", out)
        self.assertIn("--dry-run: 1 wierszy gotowych do zapisu, nic nie zapisano.", out)
        self.assert_nothing_written()

    def test_missing_required_fields_are_named(self):
        bad = entry("eventy-001", "biuro@alfa.example.pl")
        del bad["pitch"]
        bad["subject"] = "   "
        code, _, err = self.zapisz([bad])
        self.assertEqual(code, 1)
        self.assertIn("eventy-001: brak pól: subject, pitch", err)
        self.assert_nothing_written()

    def test_a_repeated_id_is_refused(self):
        code, _, err = self.zapisz([entry("eventy-001", "biuro@alfa.example.pl"), entry("eventy-001", "biuro@alfa.example.pl")])
        self.assertEqual(code, 1)
        self.assertIn("powtórzony id w pliku", err)

    def test_rows_that_do_not_qualify_are_refused(self):
        for row_id, email in (("eventy-004", "x@delta.example.pl"), ("eventy-005", "x@foxtrot.example.pl"),
                              ("eventy-008", "x@osiem.example.pl"), ("nie-ma", "x@y.pl")):
            code, _, err = self.zapisz([entry(row_id, email)])
            self.assertEqual(code, 1, row_id)
            self.assertIn("nie kwalifikuje się", err, row_id)

    def test_a_country_without_a_language_template_is_refused(self):
        code, _, err = self.zapisz([entry("eventy-003", "contact@gamma.example.fr", lang="en")])
        self.assertEqual(code, 1)
        self.assertIn("kraj „FR” bez szablonu językowego", err)

    def test_an_address_that_the_scan_did_not_find_is_refused(self):
        code, _, err = self.zapisz([entry("eventy-001", "wymyslony@alfa.example.pl")])
        self.assertEqual(code, 1)
        self.assertIn("adres wymyslony@alfa.example.pl nie występuje wśród kontaktów ze skanu", err)

    def test_the_address_is_matched_case_insensitively(self):
        code, _, _ = self.zapisz([entry("eventy-001", "BIURO@Alfa.Example.pl")], "--dry-run")
        self.assertEqual(code, 0)

    def test_polish_letters_in_a_foreign_language_mail_are_refused(self):
        bad = entry("eventy-002", "hello@beta.example.co.uk", lang="en", obs="prowadzą rezerwacje")
        code, _, err = self.zapisz([bad])
        self.assertEqual(code, 1)
        self.assertIn("treść ma polskie litery, a mail ma być po angielski", err)

    def test_one_bad_entry_stops_the_whole_batch(self):
        code, out, err = self.zapisz([entry("eventy-001", "biuro@alfa.example.pl"), entry("eventy-004", "x@delta.example.pl")])
        self.assertEqual(code, 1)
        self.assertIn("OK  eventy-001", out)
        self.assertIn("Przerwano: 1 błędów, nic nie zapisano", err)
        self.assert_nothing_written()


class WriteTests(EnrichmentCase):
    def test_saves_rendered_content_status_and_a_backup(self):
        entries = [entry("eventy-001", "biuro@alfa.example.pl", city="Gdańsk", description="Wesela i bankiety"),
                   entry("eventy-002", "hello@beta.example.co.uk", lang="en"),
                   entry("driverzy-001", "info@echo.example.de", lang="de")]
        code, out, _ = self.zapisz(entries)
        self.assertEqual(code, 0, out)
        self.assertIn("Zapisano 3 z 3 wierszy", out)
        self.assertIn("Kopia zapasowa bazy:", out)
        self.assertEqual(len(self.backup_files()), 1)
        self.assertIn("przed-uzupelnieniem-ze-skanu", self.backup_files()[0])

        alfa = self.row("eventy-001")
        self.assertEqual((alfa["status"], alfa["email"], alfa["subject"]), ("do_wyslania", "biuro@alfa.example.pl", "Rezerwacje u Alfy"))
        self.assertIn("prowadzą Państwo rezerwacje przez telefon", alfa["body_html"])
        self.assertIn("wprowadzić płatność online", alfa["body_html"])
        self.assertIn("Roman", alfa["body_html"])  # podpis z SENDER_NAME
        self.assertTrue(alfa["body_text"].strip())
        self.assertEqual((alfa["city"], alfa["description"], alfa["tag"]), ("Gdańsk", "Wesela i bankiety", "test"))
        self.assertRegex(alfa["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        self.assertEqual((alfa["sent_at"], alfa["error"]), ("", ""))
        beta = self.row("eventy-002")
        self.assertIn("you take bookings by phone", beta["body_html"])
        self.assertNotIn("rezerwacje", beta["body_html"])  # szablon angielski, nie polski
        self.assertIn("Online-Zahlung einführen", self.row("driverzy-001")["body_html"])

    def test_rows_outside_the_file_are_untouched(self):
        others = ("eventy-002", "eventy-003", "eventy-004", "eventy-008", "driverzy-001")
        before = {row_id: self.row(row_id) for row_id in others}
        self.zapisz([entry("eventy-001", "biuro@alfa.example.pl")])
        self.assertEqual({row_id: self.row(row_id) for row_id in others}, before)

    def test_existing_profile_data_is_not_overwritten(self):
        self.database.apply_edit("eventy-001", {"city": "Sopot"})
        self.zapisz([entry("eventy-001", "biuro@alfa.example.pl", city="Gdańsk", address="ul. Długa 1")])
        row = self.row("eventy-001")
        self.assertEqual((row["city"], row["address"]), ("Sopot", "ul. Długa 1"))  # istniejące zostaje, puste się uzupełnia

    def test_a_row_that_was_sent_meanwhile_is_not_overwritten(self):
        path = self.write_json([entry("eventy-001", "biuro@alfa.example.pl")])
        real_backup = firms_import.backup_table

        def backup_then_mark_sent(*args, **kwargs):
            self.database.apply_edit("eventy-001", {"status": "wyslano"})
            return real_backup(*args, **kwargs)

        with mock.patch.object(firms_import, "backup_table", backup_then_mark_sent):
            code, out, _ = self.cli("zapisz", path)
        self.assertEqual(code, 0)
        self.assertIn("Zapisano 0 z 1 wierszy", out)
        self.assertEqual(self.row("eventy-001")["status"], "wyslano")

    def test_pending_rows_are_recomputed_only_with_the_flag(self):
        row = entry("eventy-008", "x@osiem.example.pl")
        code, _, err = self.zapisz([row])
        self.assertEqual(code, 1)
        self.assertIn("nie kwalifikuje się", err)
        code, out, _ = self.zapisz([row], "--ponownie")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.row("eventy-008")["subject"], "Rezerwacje u Alfy")

    def test_interrupted_scans_count_only_with_the_flag_and_enough_pages(self):
        for flags, row_id, email, expected in (
                ((), "eventy-006", "x@six.example.pl", 1),
                (("--takze-przerwane",), "eventy-006", "x@six.example.pl", 0),
                (("--takze-przerwane",), "eventy-007", "x@siedem.example.pl", 1)):  # tylko 3 strony: za mało
            code, _, _ = self.zapisz([entry(row_id, email)], "--dry-run", *flags)
            self.assertEqual(code, expected, (row_id, flags))

    def test_list_shows_interrupted_scans_never_without_the_flag(self):
        _, out, _ = self.cli("lista")
        self.assertNotIn("eventy-006", out)


if __name__ == "__main__":
    unittest.main()


class AddressTests(EnrichmentCase):
    """`wypelnij.py adresy`: adres ze skanu do wierszy „Brak e-maila”, bez treści i bez możliwości wysyłki."""

    def set_row(self, row_id, **values):
        with self.database.engine.begin() as conn:
            conn.execute(sa.update(maile).where(maile.c.id == row_id).values(**values))

    def test_dry_run_shows_the_choice_and_writes_nothing(self):
        code, out, _ = self.cli("adresy", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("eventy-001", out)
        self.assertIn("biuro@alfa.example.pl", out)  # ogólna skrzynka w domenie, nie jan@gmail.com
        self.assertNotIn("jan@gmail.com", out)
        self.assertIn("nic nie zapisano", out)
        self.assertEqual(self.row("eventy-001")["email"], "")
        self.assertEqual(self.backup_files(), [])

    def test_saves_the_address_with_status_awaiting_copy_and_a_backup(self):
        code, out, _ = self.cli("adresy")
        self.assertEqual(code, 0)
        self.assertIn("Zapisano 3 wierszy", out)
        row = self.row("eventy-001")
        self.assertEqual((row["email"], row["status"]), ("biuro@alfa.example.pl", "oczekuje_na_tresc"))
        self.assertEqual(self.row("driverzy-001")["email"], "info@echo.example.de")
        self.assertEqual(len(self.backup_files()), 1)
        self.assertIn("adresami-ze-skanu", self.backup_files()[0])

    def test_the_row_is_not_sendable_without_content(self):
        self.cli("adresy")
        row = self.row("eventy-001")
        self.assertNotEqual(row["status"], "do_wyslania")
        self.assertFalse(row["body_html"])

    def test_rows_that_are_already_awaiting_copy_or_not_scanned_stay_as_they_are(self):
        self.cli("adresy")
        self.assertEqual(self.row("eventy-002")["email"], "")  # „Czeka na treść”: wybór adresu należy do autora treści
        self.assertEqual(self.row("eventy-005")["email"], "")  # skan niezakończony
        self.assertEqual(self.row("eventy-004")["status"], "wyslano")

    def test_an_address_typed_by_hand_is_not_replaced(self):
        self.set_row("eventy-001", email="reczny@alfa.example.pl")
        code, out, _ = self.cli("adresy")
        self.assertEqual(code, 0)
        self.assertEqual(self.row("eventy-001")["email"], "reczny@alfa.example.pl")
        self.assertNotIn("eventy-001", out)

    def test_firms_without_any_scanned_address_are_reported_not_filled(self):
        with self.database.engine.begin() as conn:
            conn.execute(sa.delete(kontakty).where(kontakty.c.firma_id == self.firm_id("eventy-003")))
        _, out, _ = self.cli("adresy", "--dry-run")
        line = next(line for line in out.splitlines() if "eventy-003" in line)
        self.assertIn("skan nie znalazł adresów", line)
        self.assertIn("Adres wybrany dla 2 firm, bez adresu zostaje 1", out)

    def test_a_row_edited_between_plan_and_write_is_not_overwritten(self):
        plan = enrichment_service.plan_address_adoption(self.store)
        self.set_row("eventy-001", email="reczny@alfa.example.pl")
        written = enrichment_service.write_addresses(self.database, plan)
        self.assertEqual(written, 2)
        self.assertEqual(self.row("eventy-001")["email"], "reczny@alfa.example.pl")

    def test_a_second_run_changes_nothing(self):
        self.cli("adresy")
        code, out, _ = self.cli("adresy")
        self.assertEqual(code, 0)
        self.assertIn("Adres wybrany dla 0 firm", out)
        self.assertIn("Nie ma czego zapisać", out)
        self.assertEqual(len(self.backup_files()), 1)


class SharedTemplateTests(EnrichmentCase):
    def test_the_template_name_in_an_entry_must_be_a_plain_identifier(self):
        for bad in ("../x", "a b", "A", ""):
            code, _, err = self.zapisz([entry("eventy-001", "biuro@alfa.example.pl", szablon=bad or "../")], "--dry-run")
            self.assertEqual(code, 1, bad)
            self.assertIn("nazwa to małe litery", err)

    def test_an_entry_can_pick_another_template_from_the_folder(self):
        code, _, err = self.zapisz([entry("eventy-001", "biuro@alfa.example.pl", szablon="nie_ma")], "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("brak szablonu nie_ma.html", err)

    def test_a_segment_without_its_own_template_uses_the_shared_one(self):
        with self.database.engine.begin() as conn:
            conn.execute(sa.text("UPDATE maile SET segment = 'spoldzielnie' WHERE id = 'eventy-001'"))
        code, out, _ = self.zapisz([entry("eventy-001", "biuro@alfa.example.pl")], "--dry-run")
        self.assertEqual(code, 0, out)

    def test_a_missing_language_file_is_refused_with_a_message_instead_of_a_crash(self):
        def missing(name, language):
            return os.path.join(os.path.dirname(__file__), "no-such-dir", f"{name}.{language}.html")

        with mock.patch("perno.domain.localize.template_path", missing):
            code, _, err = self.zapisz([entry("eventy-001", "biuro@alfa.example.pl")], "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("eventy-001: brak szablonu icebreaker", err)
        self.assertEqual(self.backup_files(), [])
