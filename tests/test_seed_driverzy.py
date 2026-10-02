"""Seed Driverzy: wybór segmentu ICP3 i mechaniczne generowanie treści z arkusza."""

from __future__ import annotations

import csv
import os
import tempfile
import unittest

from perno.services.seed import driverzy as de


def sheet_row(name, email, site, notes, sells="Transfery", booking="Online przez formularz", pay="NIE") -> list:
    cells = [""] * 15
    cells[de.COL_NAZWA], cells[de.COL_EMAIL], cells[de.COL_STRONA], cells[de.COL_UWAGI] = name, email, site, notes
    cells[de.COL_CO_SPRZEDAJE], cells[de.COL_JAK_REZERWUJE], cells[de.COL_PLATNOSC_ONLINE] = sells, booking, pay
    cells[de.COL_TRANSFER_LOTNISKOWY], cells[de.COL_ROUND_TRIP], cells[de.COL_BEZPOSREDNIA_REZERWACJA] = "TAK", "NIE", "TAK"
    cells[de.COL_CROSS_SELL] = "Wycieczki"
    return cells


class PitchTests(unittest.TestCase):
    def test_no_online_payment_is_the_best_fit(self):
        obs, pitch, summary, fit = de.generate_pitch("Telefon lub e-mail", "nie")
        self.assertEqual(fit, "high")
        self.assertIn("przez telefon lub e-mail", obs)
        self.assertIn("nie widziałem możliwości zapłaty online", obs)
        self.assertEqual(pitch, "wprowadzić płatność online od razu przy rezerwacji przejazdu")
        self.assertEqual(summary, "ręcznie potwierdzać każdą rezerwację przez telefon lub e-mail")

    def test_existing_online_payment_is_a_low_fit_flagged_for_review(self):
        _, pitch, _, fit = de.generate_pitch("Formularz", "TAK")
        self.assertEqual(fit, "low")
        self.assertIn("NISKIE DOPASOWANIE", pitch)

    def test_unknown_payment_status_is_a_medium_fit(self):
        for value in ("DO WERYFIKACJI", "", "cokolwiek"):
            self.assertEqual(de.generate_pitch("Formularz", value)[3], "medium", value)

    def test_the_channel_text_is_cleaned_and_has_a_fallback(self):
        self.assertIn("przez platformę X", de.generate_pitch("Online przez platformę X.", "NIE")[0])
        self.assertNotIn("online przez platformę", de.generate_pitch("Online przez platformę X.", "NIE")[0])
        self.assertIn("formularz, telefon lub e-mail", de.generate_pitch("", "NIE")[0])


class BuildRecordsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.csv = os.path.join(tmp.name, "driverzy.csv")
        self.dossiers = os.path.join(tmp.name, "Driverzy")
        os.makedirs(self.dossiers)
        with open(os.path.join(self.dossiers, "003_trzecia-firma.md"), "w", encoding="utf-8") as handle:
            handle.write("# dossier\n")
        with open(self.csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["nagłówek"] * 15)
            writer.writerow(sheet_row("Pierwsza", "a@x.pl", "https://a.pl", "ICP 4 — SaaS"))
            writer.writerow(sheet_row("Druga", "b@x.pl", "https://b.pl", "x ICP 3 y", pay="TAK"))
            writer.writerow(sheet_row("  Trzecia  ", " c@x.pl ", " https://c.pl ", "ICP 3"))
            writer.writerow(sheet_row("Czwarta", "d@x.pl", "https://d.pl", "bez segmentu"))

    def test_classify_icp(self):
        self.assertEqual([de.classify_icp(text) for text in ("ICP 3", "ICP 4", "inne", "")], ["ICP3", "ICP4", "UNKNOWN", "UNKNOWN"])

    def test_only_icp3_rows_become_records_numbered_by_their_sheet_position(self):
        records = de.build_icp3_records(self.csv, self.dossiers)
        self.assertEqual([(r["row_number"], r["company_display_name"]) for r in records], [(2, "Druga"), (3, "Trzecia")])

    def test_record_fields_are_trimmed_and_carry_the_generated_copy(self):
        record = de.build_icp3_records(self.csv, self.dossiers)[1]
        self.assertEqual((record["email"], record["website"], record["icp_segment"]), ("c@x.pl", "https://c.pl", "ICP3"))
        self.assertEqual((record["fit_confidence"], record["mode"]), ("high", "Panstwo"))
        self.assertEqual(record["subject"], "Płatność online przy rezerwacji — Trzecia")
        self.assertEqual((record["airport_transfer"], record["round_trip"], record["direct_booking"]), ("TAK", "NIE", "TAK"))

    def test_the_dossier_file_is_matched_by_row_number(self):
        records = de.build_icp3_records(self.csv, self.dossiers)
        self.assertEqual([r["source_dossier_file"] for r in records], ["", "003_trzecia-firma.md"])


if __name__ == "__main__":
    unittest.main()
