"""Budowa data/eventy.csv: ścieżki w katalogu projektu oraz łączenie tablicy JS z dossier."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from perno import settings as cfg
from perno.cli import seed

INDEX_HTML = """<html><script>
const companies = [
  { name: "Alfa Event (Jan Kowalski)", website: "https://alfa.pl", email: "biuro@alfa.pl", tag: "t1", mode: "Panstwo",
    subject: "Temat A", obs: "obs A", pitch: "pitch A", channelSummary: "ch A" },
  { name: "Beta Studio", website: "https://beta.pl", email: "", tag: "t2", mode: "Firma",
    subject: "Temat B", obs: "obs B", pitch: "pitch B", channelSummary: "ch B" },
  { name: "Gamma Klient", website: "https://gamma.pl", alreadyCustomer: true, note: "Klient od 2024", tag: "t3" },
];
</script></html>
"""


def dossier(number: int, name: str, website: str, email: str) -> str:
    return (f"# {number}. {name} — analiza firmy\n\n## 0. Karta firmy\n| Pole | Wartość |\n|---|---|\n"
            f"| Strona WWW | {website} |\n| Kontakt publiczny | {email} |\n\n## 1. Reszta\ntekst z jan.kowalski@gmail.com\n")


class SeedPathsTests(unittest.TestCase):
    def test_outputs_go_to_the_project_data_directory(self):
        self.assertEqual(seed.EVENTY_OUT, cfg.EVENTY_SEED)
        self.assertEqual(seed.DRIVERZY_OUT, cfg.DRIVERZY_SEED)

    def test_sources_are_looked_up_next_to_the_project_not_inside_the_package(self):
        self.assertEqual(seed.EVENTY_HTML, os.path.join(cfg.PERNO_DIR, "Maile_GTM_Eventy", "index.html"))
        self.assertEqual(seed.EVENTY_DOSSIERS_DIR, os.path.join(cfg.PERNO_DIR, "Firmy", "Eventy"))
        self.assertEqual(seed.DRIVERZY_CSV, os.path.join(cfg.PERNO_DIR, "PERNO - GTM - Driverzy .csv"))
        self.assertNotIn(os.path.join("src", "perno"), seed.EVENTY_HTML)


class BuildEventyRowsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        html_path = os.path.join(tmp.name, "index.html")
        dossiers = os.path.join(tmp.name, "Eventy")
        os.makedirs(dossiers)
        with open(html_path, "w", encoding="utf-8") as handle:
            handle.write(INDEX_HTML)
        for name, text in (("001_alfa-event.md", dossier(1, "Alfa Event", "https://alfa.pl", "biuro@alfa.pl")),
                           ("002_beta-studio.md", dossier(2, "Beta Studio", "https://beta.pl", "kontakt@beta.pl")),
                           ("003_delta-bez-wpisu.md", dossier(3, "Delta Bez Wpisu", "https://delta.pl/oferta).", "delta@delta.pl")),
                           ("00_METODOLOGIA.md", "# metodologia\n")):
            with open(os.path.join(dossiers, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        for patcher in (mock.patch.object(seed, "EVENTY_HTML", html_path), mock.patch.object(seed, "EVENTY_DOSSIERS_DIR", dossiers)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.rows = seed.build_eventy_rows()

    def by_name(self, name):
        return next(row for row in self.rows if row["company_display_name"] == name)

    def test_rows_are_sorted_by_dossier_number_with_unmatched_ones_last(self):
        self.assertEqual([row["company_display_name"] for row in self.rows],
                         ["Alfa Event", "Beta Studio", "Delta Bez Wpisu", "Gamma Klient"])
        self.assertEqual([row["row_number"] for row in self.rows], [1, 2, 3, ""])

    def test_a_prospect_with_a_dossier_and_its_own_email(self):
        row = self.by_name("Alfa Event")
        self.assertEqual((row["name_suffix_raw"], row["contact_person_guess"], row["contact_person_confidence"]),
                         ("Jan Kowalski", "Jan Kowalski", "high"))
        self.assertEqual((row["email"], row["email_source"], row["source_dossier_file"]), ("biuro@alfa.pl", "js", "001_alfa-event.md"))
        self.assertEqual((row["subject"], row["obs"], row["pitch"], row["channel_summary"]), ("Temat A", "obs A", "pitch A", "ch A"))
        self.assertEqual((row["already_customer"], row["needs_manual_copy"], row["mode"], row["tag"]), ("false", "false", "Panstwo", "t1"))

    def test_a_missing_email_is_backfilled_from_the_dossier_card_only(self):
        row = self.by_name("Beta Studio")
        self.assertEqual((row["email"], row["email_source"], row["mode"]), ("kontakt@beta.pl", "dossier", "Firma"))

    def test_an_existing_customer_gets_a_row_without_copy(self):
        row = self.by_name("Gamma Klient")
        self.assertEqual((row["already_customer"], row["already_customer_note"], row["email"], row["subject"], row["mode"]),
                         ("true", "Klient od 2024", "", "", ""))
        self.assertEqual((row["row_number"], row["source_dossier_file"], row["contact_person_confidence"]), ("", "", "none"))

    def test_a_dossier_without_a_js_entry_awaits_manual_copy(self):
        row = self.by_name("Delta Bez Wpisu")
        self.assertEqual((row["needs_manual_copy"], row["already_customer"], row["mode"]), ("true", "false", "Panstwo"))
        self.assertEqual((row["website"], row["email"], row["email_source"]), ("https://delta.pl/oferta", "delta@delta.pl", "dossier"))
        self.assertEqual((row["subject"], row["tag"], row["source_dossier_file"]), ("", "", "003_delta-bez-wpisu.md"))

    def test_every_row_has_all_columns(self):
        for row in self.rows:
            self.assertEqual(set(seed.EVENTY_COLUMNS) - set(row), set())


if __name__ == "__main__":
    unittest.main()
