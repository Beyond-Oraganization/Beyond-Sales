"""ai.py: szablony, znane, dodaj, status, brief, zapisz, pomin, skan (sqlite, bez sieci i bez prawdziwego SMTP)."""

from __future__ import annotations

import contextlib
import csv
import io
import os
import unittest
from unittest import mock

import sqlalchemy as sa

from perno.cli import ai
from perno.scraper.schema import firmy
from perno.services import ai_workflow as flow
from perno.services import scan_control
from tests import test_enrichment as base

GOOD = {
    "subject": "Wesela i bankiety, Łódź", "mode": "Panstwo",
    "obs": "Szukałem sal na wesela. Zauważyłem, że mają Państwo osobny cennik pakietów weselnych. "
           "Czy rezerwacja terminu odbywa się wyłącznie telefonicznie?",
    "pitch": "przy pakiecie weselnym wprowadzić natychmiastową płatność online",
    "channel_summary": "potwierdzać rezerwacji telefonicznie",
}


def good(row_id="eventy-001", **overrides) -> dict:
    return {"id": row_id, **GOOD, **overrides}


class AiCase(base.EnrichmentCase):
    def setUp(self):
        super().setUp()
        with open(self.config, "a", encoding="utf-8") as handle:
            handle.write("SKRAPER_CONTACT=test@example.com\n")
        self.update_firm("eventy-007", skan_status="zakonczona", stron_pobranych=12)  # druga polska firma ze skanem

    def ai(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ai.main(["--config", self.config, *argv])
        return code, out.getvalue(), err.getvalue()

    def write_csv(self, rows, name="nowe.csv"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["company_display_name", "website", "what_they_sell", "country", "city"])
            writer.writerows(rows)
        return path

    def row(self, row_id):
        return self.database.get(row_id)


class TemplateAndKnownTests(AiCase):
    def test_templates_listing_shows_fields_languages_and_guide(self):
        code, out, _ = self.ai("szablony")
        self.assertEqual(code, 0)
        self.assertIn("icebreaker: języki pl,en,de,es | pola: channel_summary, obs, pitch, [season]", out)
        self.assertIn("ai.py szablony --opis icebreaker", out)

    def test_template_guide_is_printed(self):
        code, out, _ = self.ai("szablony", "--opis", "icebreaker")
        self.assertEqual(code, 0)
        self.assertIn("Mail icebreaker", out)

    def test_known_domains_are_listed_and_filtered_by_segment(self):
        _, out, _ = self.ai("znane")
        self.assertIn("alfa.example.pl", out)
        self.assertIn("echo.example.de", out)
        _, out, _ = self.ai("znane", "--segment", "driverzy")
        self.assertIn("echo.example.de", out)
        self.assertNotIn("alfa.example.pl", out)


class AddFirmsTests(AiCase):
    ROWS = [("Kancelaria A", "https://ka.example.pl/", "Prawo spółdzielcze", "PL", "Łódź"),
            ("Duplikat Alfy", "https://www.alfa.example.pl/x", "to samo co Alfa", "PL", "")]

    def test_dry_run_changes_nothing(self):
        before = self.database.count_rows()
        code, out, _ = self.ai("dodaj", self.write_csv(self.ROWS), "--segment", "Kancelarie", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("DRY-RUN segment=kancelarie: nowych 1, już w bazie 1", out)
        self.assertEqual(self.database.count_rows(), before)
        self.assertEqual(self.backup_files(), [])

    def test_add_creates_rows_queues_them_in_the_scraper_and_backs_up(self):
        code, out, _ = self.ai("dodaj", self.write_csv(self.ROWS), "--segment", "kancelarie")
        self.assertEqual(code, 0, out)
        row = self.row("kancelarie-n001")
        self.assertEqual((row["company"], row["segment"], row["status"]), ("Kancelaria A", "kancelarie", "pominieto_brak_email"))
        with self.database.engine.connect() as conn:
            queued = conn.execute(sa.select(firmy.c.skan_status).where(firmy.c.domena == "ka.example.pl")).scalar()
        self.assertEqual(queued, "nieskanowana")
        self.assertEqual(len(self.backup_files()), 1)

    def test_scan_starts_only_for_the_new_firms_with_the_small_profile(self):
        spawned = []

        def fake_spawn(run_id, config_path):
            spawned.append((run_id, config_path))
            return 4242, os.path.join(self.tmp, "run.log")

        config = {"SKRAPER_CONTACT": "test@example.com"}
        request = flow.ScanRequest(config, self.config, spawn=fake_spawn)
        report = flow.add_firms(self.database, self.store, self.write_csv(self.ROWS), "kancelarie", dry_run=False, scan=request)
        self.assertEqual(report.scan.queued, 1)
        self.assertEqual(spawned, [(report.scan.run_id, self.config)])
        self.assertTrue(any("strony" in line.lower() or "firm" in line.lower() for line in report.scan_plan))
        run = self.store.active_run()
        self.assertEqual(run["parametry"]["ustawienia"]["profile"], flow.SCAN_PROFILE)
        self.assertEqual(len(run["parametry"]["firma_ids"]), 1)

    def test_too_many_firms_in_one_call_are_refused(self):
        rows = [(f"Firma {n}", f"https://firma{n}.example.pl/", "x", "PL", "") for n in range(flow.MAX_FIRMS_PER_ADD + 1)]
        code, _, err = self.ai("dodaj", self.write_csv(rows), "--segment", "kancelarie")
        self.assertEqual(code, 1)
        self.assertIn("najwyżej", err)

    def test_a_segment_name_that_is_not_an_identifier_is_refused(self):
        with self.assertRaises(SystemExit):
            self.ai("dodaj", self.write_csv(self.ROWS), "--segment", "!!")


class StatusTests(AiCase):
    def test_status_lists_segments_with_scan_mail_and_ready_counts(self):
        code, out, _ = self.ai("status")
        self.assertEqual(code, 0)
        eventy = next(line for line in out.splitlines() if line.startswith("eventy:"))
        self.assertIn("gotowe do analizy: 4", eventy)  # eventy-001, -002, -003 (FR bez szablonu, ale skan jest) i -007
        self.assertIn("aktywny skan: brak", out)

    def test_status_can_be_limited_to_one_segment(self):
        _, out, _ = self.ai("status", "--segment", "driverzy")
        self.assertTrue(out.splitlines()[0].startswith("driverzy:"))
        self.assertNotIn("eventy:", out)


class BriefTests(AiCase):
    def test_brief_is_compact_and_has_address_snippets_and_template_fields(self):
        code, out, _ = self.ai("brief", "--segment", "eventy", "--id", "eventy-001")
        self.assertEqual(code, 0)
        self.assertIn("## eventy-001 | Alfa Sp. z o.o. | PL", out)
        self.assertIn("adres: biuro@alfa.example.pl (wybrany ze skanu)", out)
        self.assertIn("tel: +48 22 123 45 67", out)
        self.assertIn("szablon: icebreaker/pl, pola: channel_summary, obs, pitch, [season]", out)
        self.assertIn("/cennik", out)
        self.assertIn("5000 zł", out)
        self.assertLess(len(out), 1800)

    def test_blocked_or_unscanned_firms_are_not_offered(self):
        _, out, _ = self.ai("brief", "--limit", "50")
        self.assertNotIn("eventy-005", out)  # nieskanowana
        self.assertNotIn("eventy-006", out)  # zablokowana
        self.assertNotIn("eventy-004", out)  # już wysłana

    def test_cursor_gives_the_next_batch(self):
        _, first, _ = self.ai("brief", "--segment", "eventy", "--limit", "1")
        self.assertIn("eventy-001", first)
        self.assertIn("następna paczka: --po eventy-001", first)
        _, second, _ = self.ai("brief", "--segment", "eventy", "--limit", "1", "--po", "eventy-001")
        self.assertIn("eventy-002", second)
        self.assertNotIn("eventy-001 |", second)

    def test_batch_budget_stops_before_the_limit(self):
        _, out, _ = self.ai("brief", "--limit", "10", "--budzet", "50")
        self.assertEqual(out.count("## "), 1)

    def test_a_firm_that_already_uses_payevento_is_flagged(self):
        self.add_page("eventy-002", "https://beta.example.co.uk/bilety", "Bilety", "", "Kup bilet: payevento.com/event/5/tickets", 1)
        _, out, _ = self.ai("brief", "--id", "eventy-002")
        self.assertIn("STOP=PayEvento", out)


class SaveTests(AiCase):
    def test_dry_run_checks_everything_and_writes_nothing(self):
        code, out, _ = self.ai("zapisz", self.write_json([good()]), "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("OK  eventy-001", out)
        self.assertEqual(self.row("eventy-001")["status"], "pominieto_brak_email")
        self.assertEqual(self.backup_files(), [])

    def test_the_address_is_picked_from_the_scan_and_the_mail_is_rendered_from_the_template(self):
        code, out, _ = self.ai("zapisz", self.write_json([good()]))
        self.assertEqual(code, 0, out)
        row = self.row("eventy-001")
        self.assertEqual((row["status"], row["email"], row["subject"]), ("do_wyslania", "biuro@alfa.example.pl", GOOD["subject"]))
        self.assertIn(GOOD["obs"], row["body_html"])
        self.assertIn("Razem z firmą z3x Tech-Care Group", row["body_html"])
        self.assertIn("Roman", row["body_text"])
        self.assertNotIn("{{", row["body_html"])
        self.assertEqual(len(self.backup_files()), 1)

    def test_season_sentence_lands_in_the_mail(self):
        self.ai("zapisz", self.write_json([good(season="Najbliższy turnus startuje w lipcu.")]))
        self.assertIn("Najbliższy turnus startuje w lipcu.", self.row("eventy-001")["body_html"])

    def test_rule_errors_block_everything_by_default(self):
        bad = good("eventy-007", obs="Zauważyłem coś. Czy tak?", subject="Za długi temat który ma bardzo wiele słów")
        code, _, err = self.ai("zapisz", self.write_json([good(), bad]))
        self.assertEqual(code, 1)
        self.assertIn("eventy-007:", err)
        self.assertEqual(self.row("eventy-001")["status"], "pominieto_brak_email")

    def test_partial_mode_writes_the_valid_entries_and_reports_the_rest(self):
        bad = good("eventy-007", pitch="wprowadzić płatność")
        code, out, err = self.ai("zapisz", self.write_json([good(), bad]), "--czesciowo")
        self.assertEqual(code, 1)
        self.assertIn("Zapisano 1 z 1", out)
        self.assertIn("eventy-007: pitch", err)
        self.assertEqual(self.row("eventy-001")["status"], "do_wyslania")
        self.assertEqual(self.row("eventy-007")["status"], "pominieto_brak_email")

    def test_polish_text_for_an_english_firm_is_refused(self):
        code, _, err = self.ai("zapisz", self.write_json([good("eventy-002", email="hello@beta.example.co.uk")]))
        self.assertEqual(code, 1)
        self.assertTrue("polskie litery" in err or "Szukałem" in err or "szablon" in err, err)

    def test_unknown_template_and_unsupported_template_are_refused(self):
        code, _, err = self.ai("zapisz", self.write_json([good(szablon="nie_ma_takiego")]))
        self.assertEqual(code, 1)
        self.assertIn("nie ma szablonu", err)

    def test_a_row_that_is_not_a_candidate_is_refused(self):
        code, _, err = self.ai("zapisz", self.write_json([good("eventy-004")]))  # już wysłana
        self.assertEqual(code, 1)
        self.assertIn("nie kwalifikuje się", err)

    def test_an_address_that_is_not_from_the_scan_is_refused(self):
        code, _, err = self.ai("zapisz", self.write_json([good(email="wymyslony@alfa.example.pl")]))
        self.assertEqual(code, 1)
        self.assertIn("nie występuje wśród kontaktów ze skanu", err)

    def test_wrong_address_form_is_caught_by_the_rules(self):
        code, _, err = self.ai("zapisz", self.write_json([good(mode="Pani")]))  # tekst mówi „Państwo”
        self.assertEqual(code, 1)
        self.assertIn("nie pasuje do formy Pani", err)


class SkipTests(AiCase):
    def test_skip_marks_the_row_and_keeps_the_reason(self):
        code, out, _ = self.ai("pomin", "eventy-001", "--powod", "sprzedaje tylko stacjonarnie")
        self.assertEqual(code, 0, out)
        row = self.row("eventy-001")
        self.assertEqual(row["status"], "pominieto_dopasowanie")
        self.assertTrue(row["description"].startswith("Nie pasuje: sprzedaje tylko stacjonarnie."))
        _, listing, _ = self.ai("brief", "--id", "eventy-001")
        self.assertNotIn("## eventy-001", listing)

    def test_a_sent_row_is_never_touched(self):
        code, _, err = self.ai("pomin", "eventy-004", "--powod", "x")
        self.assertEqual(code, 1)
        self.assertIn("nie ruszam", err)
        self.assertEqual(self.row("eventy-004")["status"], "wyslano")

    def test_unknown_row(self):
        code, _, err = self.ai("pomin", "nie-ma-1", "--powod", "x")
        self.assertEqual(code, 1)
        self.assertIn("nie ma takiego wiersza", err)


class ScanControlTests(AiCase):
    def test_stop_without_an_active_run_says_so(self):
        code, out, _ = self.ai("skan", "stop")
        self.assertEqual(code, 0)
        self.assertIn("Żaden przebieg nie jest aktywny", out)

    def test_resume_uses_the_small_profile(self):
        started = scan_control.StartedScan(7, 3)
        with mock.patch.object(scan_control, "start_scan", return_value=started) as start:
            code, out, _ = self.ai("skan", "wznow")
        self.assertEqual(code, 0, out)
        self.assertIn("przebieg #7", out)
        self.assertEqual(start.call_args.args[1].profile, flow.SCAN_PROFILE)


if __name__ == "__main__":
    unittest.main()
