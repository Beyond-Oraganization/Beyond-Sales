from __future__ import annotations

import contextlib
import csv
import io
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import sqlalchemy as sa

from perno import db as dbmod
from perno import settings as cfg
from perno.cli import firms as firmy
from perno.domain import statuses as st
from perno.services import firms_import as firms
from perno.services.firms_import import (
    FirmsError,
    assign_profiles,
    domain_key,
    import_firms,
    profile_report,
    read_import_csv,
    read_profile_csv,
)
from tests.fixtures import make_db, old_database, put, sample_row


def write_csv(path: str, header: list, rows: list, bom: bool = False) -> str:
    with open(path, "w", newline="", encoding="utf-8-sig" if bom else "utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


IMPORT_HEADER = ["company_display_name", "website", "what_they_sell"]


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perno-firms-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def path(self, name: str) -> str:
        return os.path.join(self.tmp, name)


class DomainKeyTests(unittest.TestCase):
    def test_variants_collapse_to_one_domain(self):
        for raw in ("https://www.Firma.pl/", "http://firma.pl", "firma.pl", "FIRMA.PL/kontakt?x=1", "https://firma.pl:8080/a"):
            self.assertEqual(domain_key(raw), "firma.pl", raw)

    def test_subdomains_and_paths_are_kept_distinct_where_it_matters(self):
        self.assertEqual(domain_key("https://taxi-vip.katowice.pl/"), "taxi-vip.katowice.pl")
        self.assertEqual(domain_key("https://www.sixt.pl/limousine/"), "sixt.pl")

    def test_garbage(self):
        for raw in ("", None, "   ", "http://", "https://:80/"):
            self.assertIsNone(domain_key(raw), repr(raw))


class ReadImportCsvTests(TempCase):
    def test_reads_rows_with_bom_quotes_and_blank_lines(self):
        path = write_csv(self.path("a.csv"), IMPORT_HEADER, [
            ["Krakow Direct", "https://krakowdirect.com/", 'Kraków: transfery, "wycieczki", busy'],
            [],
            ["  Spacje  ", " https://www.firma.pl/ ", ""],
        ], bom=True)
        rows = read_import_csv(path)
        self.assertEqual([r["company"] for r in rows], ["Krakow Direct", "Spacje"])
        self.assertEqual(rows[0]["description"], 'Kraków: transfery, "wycieczki", busy')
        self.assertEqual((rows[1]["website"], rows[1]["domain"]), ("https://www.firma.pl/", "firma.pl"))
        self.assertEqual(rows[0]["country"], "")

    def test_optional_country_city_address_columns(self):
        path = write_csv(self.path("a.csv"), IMPORT_HEADER + ["country", "city", "address"],
                         [["A", "https://a.pl", "", "UK", "Londyn", "1 Mall"]])
        row = read_import_csv(path)[0]
        self.assertEqual((row["country"], row["city"], row["address"]), ("GB", "Londyn", "1 Mall"))

    def test_schemeless_website_gets_https(self):
        rows = read_import_csv(write_csv(self.path("a.csv"), IMPORT_HEADER, [["A", "firma.pl", ""]]))
        self.assertEqual(rows[0]["website"], "https://firma.pl")

    def test_missing_columns(self):
        path = write_csv(self.path("a.csv"), ["name", "url"], [["A", "https://a.pl"]])
        with self.assertRaises(FirmsError) as ctx:
            read_import_csv(path)
        self.assertIn("company_display_name", str(ctx.exception))

    def test_all_problems_are_reported_at_once_with_line_numbers(self):
        path = write_csv(self.path("a.csv"), IMPORT_HEADER + ["country"], [
            ["", "https://a.pl", "", ""], ["B", "", "", ""], ["C", "https://c.pl", "", "Atlantyda"], ["Dobra", "https://d.pl", "", "PL"]])
        with self.assertRaises(FirmsError) as ctx:
            read_import_csv(path)
        message = str(ctx.exception)
        for fragment in ("wiersz 2: brak nazwy", "wiersz 3 (B)", "wiersz 4 (C): nieznany kraj"):
            self.assertIn(fragment, message)
        self.assertNotIn("Dobra", message)


class ImportFirmsTests(TempCase):
    def rows(self, *items):
        path = write_csv(self.path("in.csv"), IMPORT_HEADER, [[n, w, d] for n, w, d in items])
        return read_import_csv(path)

    def test_new_firms_get_their_own_ids_status_and_description(self):
        database = make_db()
        put(database, sample_row(1), sample_row(2))
        result = import_firms(database, self.rows(("Nowa A", "https://a-nowa.pl/", "Opis A"),
                                                  ("Nowa B", "https://www.b-nowa.com/", "Opis B")), "eventy")
        self.assertEqual([n["id"] for n in result.new], ["eventy-n001", "eventy-n002"])
        row = database.get("eventy-n001")
        self.assertEqual((row["company"], row["website"], row["description"], row["status"]),
                         ("Nowa A", "https://a-nowa.pl/", "Opis A", "pominieto_brak_email"))
        self.assertEqual((row["row_number"], row["email"], row["body_html"], row["sent_at"], row["retry_count"]),
                         ("", "", "", "", "0"))
        self.assertEqual(database.get("eventy-001")["company"], "Firma 1")  # istniejące wiersze nietknięte

    def test_import_is_idempotent_and_continues_the_sequence(self):
        database = make_db()
        put(database, sample_row(1))
        first = import_firms(database, self.rows(("A", "https://a.pl", ""), ("B", "https://b.pl", "")), "eventy")
        again = import_firms(database, self.rows(("A", "https://a.pl", ""), ("B", "https://b.pl", "")), "eventy")
        self.assertEqual((len(first.new), len(again.new), len(again.skipped_existing)), (2, 0, 2))
        more = import_firms(database, self.rows(("A", "https://a.pl", ""), ("C", "https://c.pl", "")), "eventy")
        self.assertEqual([n["id"] for n in more.new], ["eventy-n003"])
        self.assertEqual(database.count_rows(), 4)

    def test_numbering_from_dossiers_and_other_segments_does_not_interfere(self):
        database = make_db()
        put(database, sample_row(51), sample_row(60), sample_row(1, segment="driverzy"),
            sample_row(1, segment="driverzy", id="driverzy-n017", row_number=""))
        result = import_firms(database, self.rows(("A", "https://a.pl", "")), "eventy")
        self.assertEqual(result.new[0]["id"], "eventy-n001")
        result = import_firms(database, self.rows(("D", "https://d.pl", "")), "driverzy")
        self.assertEqual(result.new[0]["id"], "driverzy-n018")

    def test_existing_domain_is_skipped_whatever_the_segment_scheme_or_www(self):
        database = make_db()
        put(database, sample_row(1, website="https://www.istnieje.pl/oferta"),
            sample_row(2, segment="driverzy", website="http://inna.com/"))
        result = import_firms(database, self.rows(("Dup1", "http://istnieje.pl", ""), ("Dup2", "https://INNA.com", ""),
                                                  ("Nowa", "https://nowa.pl", "")), "eventy")
        self.assertEqual([n["company"] for n in result.new], ["Nowa"])
        self.assertEqual(sorted(name for name, _ in result.skipped_existing), ["Dup1", "Dup2"])
        self.assertEqual(dict(result.skipped_existing)["Dup2"], "driverzy-002")

    def test_duplicates_inside_the_file_are_imported_once(self):
        database = make_db()
        put(database, sample_row(1))
        result = import_firms(database, self.rows(("A", "https://a.pl", ""), ("A bis", "https://www.a.pl/x", "")), "eventy")
        self.assertEqual((len(result.new), result.duplicates_in_file), (1, [(3, "A bis")]))

    def test_dry_run_writes_nothing(self):
        database = make_db()
        put(database, sample_row(1))
        result = import_firms(database, self.rows(("A", "https://a.pl", "")), "eventy", dry_run=True)
        self.assertEqual(len(result.new), 1)
        self.assertEqual(database.count_rows(), 1)

    def test_one_bad_row_cancels_the_whole_import(self):
        database = make_db()
        put(database, sample_row(1))
        with self.assertRaises(FirmsError) as ctx:
            import_firms(database, self.rows(("Dobra", "https://ok.pl", ""), ("x" * 300, "https://dluga.pl", "")), "eventy")
        self.assertIn("za długie", str(ctx.exception))
        self.assertEqual(database.count_rows(), 1)

    def test_unknown_segment(self):
        with self.assertRaises(FirmsError):
            import_firms(make_db(), self.rows(("A", "https://a.pl", "")), "inny")

    def test_imported_firms_can_never_be_sent_by_accident(self):
        database = make_db()
        put(database, sample_row(1))
        import_firms(database, self.rows(("A", "https://a.pl", "")), "eventy")
        self.assertEqual([r["id"] for r in database.select_candidates(limit=None)], ["eventy-001"])
        self.assertIsNone(database.claim_for_sending("eventy-n001"))
        self.assertNotIn("pominieto_brak_email", st.SENDABLE_STATUSES)

    def test_imported_firms_sort_after_numbered_rows_of_their_segment(self):
        database = make_db()
        put(database, sample_row(1), sample_row(1, segment="driverzy"))
        import_firms(database, self.rows(("A", "https://a.pl", "")), "eventy")
        import_firms(database, self.rows(("D", "https://d.pl", "")), "driverzy")
        self.assertEqual([r["id"] for r in database.fetch_all()], ["eventy-001", "eventy-n001", "driverzy-001", "driverzy-n001"])

    def test_country_given_in_the_file_is_stored(self):
        database = make_db()
        path = write_csv(self.path("in.csv"), IMPORT_HEADER + ["country", "city"], [["A", "https://a.de", "", "Niemcy", "Berlin"]])
        import_firms(database, read_import_csv(path), "driverzy")
        row = database.get("driverzy-n001")
        self.assertEqual((row["country"], row["city"]), ("DE", "Berlin"))

    def test_a_row_can_get_an_email_later_and_then_follows_the_normal_flow(self):
        database = make_db()
        put(database, sample_row(1))
        import_firms(database, self.rows(("A", "https://a.pl", "")), "eventy")
        row = database.apply_edit("eventy-n001", {"email": "biuro@a.pl"})
        self.assertEqual(row["status"], "pominieto_brak_email")  # bez treści nadal nie jest „do wysłania"
        row = database.apply_edit("eventy-n001", {"body_html": "<p>Treść</p>", "subject": "Temat"})
        self.assertEqual(row["status"], "do_wyslania")


def profile_rows(*items):
    return [{"domain": d, "country": c, "city": city, "address": address, "source": ""} for d, c, city, address in items]


class AssignProfilesTests(TempCase):
    def setUp(self):
        super().setUp()
        self.database = make_db()
        put(self.database,
            sample_row(1, website="https://www.alfa.pl/"),
            sample_row(2, website="https://beta.co.uk/contact", status="wyslano", sent_at="2026-09-29T10:00:00"),
            sample_row(3, website="https://gamma.de", country="DE", city="Berlin"),
            sample_row(4, website="https://delta.pl"))

    def test_fills_empty_fields_matching_by_domain(self):
        result = assign_profiles(self.database, profile_rows(("alfa.pl", "PL", "Warszawa", ""), ("beta.co.uk", "GB", "Londyn", "1 Mall")))
        self.assertEqual({row_id for row_id, _ in result.updated}, {"eventy-001", "eventy-002"})
        one, two = self.database.get("eventy-001"), self.database.get("eventy-002")
        self.assertEqual((one["country"], one["city"], one["address"]), ("PL", "Warszawa", ""))
        self.assertEqual((two["country"], two["city"], two["address"]), ("GB", "Londyn", "1 Mall"))

    def test_only_profile_fields_are_touched_never_status_or_sent_data(self):
        before = self.database.get("eventy-002")
        assign_profiles(self.database, profile_rows(("beta.co.uk", "GB", "Londyn", "")))
        after = self.database.get("eventy-002")
        for name in dbmod.LEGACY_COLUMNS:
            self.assertEqual(after[name], before[name], name)
        self.assertEqual((after["status"], after["sent_at"]), ("wyslano", "2026-09-29T10:00:00"))

    def test_existing_values_are_kept_unless_overwrite(self):
        entries = profile_rows(("gamma.de", "AT", "Wiedeń", "Ring 1"))
        result = assign_profiles(self.database, entries)
        row = self.database.get("eventy-003")
        self.assertEqual((row["country"], row["city"], row["address"]), ("DE", "Berlin", "Ring 1"))  # tylko puste pole zostało wypełnione
        self.assertEqual({(r, f) for r, f, _, _ in result.kept}, {("eventy-003", "country"), ("eventy-003", "city")})
        assign_profiles(self.database, entries, overwrite=True)
        row = self.database.get("eventy-003")
        self.assertEqual((row["country"], row["city"]), ("AT", "Wiedeń"))

    def test_second_run_changes_nothing(self):
        entries = profile_rows(("alfa.pl", "PL", "Warszawa", ""))
        assign_profiles(self.database, entries)
        result = assign_profiles(self.database, entries)
        self.assertEqual((result.updated, result.unchanged), ([], 1))

    def test_dry_run_writes_nothing_but_reports_the_plan(self):
        result = assign_profiles(self.database, profile_rows(("alfa.pl", "PL", "", "")), dry_run=True)
        self.assertEqual([row_id for row_id, _ in result.updated], ["eventy-001"])
        self.assertEqual(self.database.get("eventy-001")["country"], "")

    def test_reports_unmatched_entries_and_rows_without_entry(self):
        result = assign_profiles(self.database, profile_rows(("alfa.pl", "PL", "", ""), ("nie-ma.pl", "PL", "", "")))
        self.assertEqual(result.unmatched_domains, ["nie-ma.pl"])
        self.assertEqual(sorted(result.rows_without_entry), ["eventy-002", "eventy-003", "eventy-004"])

    def test_profile_report(self):
        assign_profiles(self.database, profile_rows(("alfa.pl", "PL", "Warszawa", ""), ("beta.co.uk", "GB", "", "")))
        report = profile_report(self.database)
        self.assertEqual(report["total"], 4)
        self.assertEqual(report["by_country"], {"PL": {"eventy": 1}, "GB": {"eventy": 1}, "DE": {"eventy": 1}})
        self.assertEqual([r["id"] for r in report["without_country"]], ["eventy-004"])
        self.assertEqual(report["with_city"], 2)


class ReadProfileCsvTests(TempCase):
    def test_reads_and_normalizes(self):
        path = write_csv(self.path("k.csv"), ["domain", "country", "city", "address", "source"], [
            ["https://www.Alfa.pl/x", "Polska", "Kraków", "", "arkusz"], ["beta.co.uk", "UK", "", "", ""]])
        entries = read_profile_csv(path)
        self.assertEqual([(e["domain"], e["country"], e["city"]) for e in entries],
                         [("alfa.pl", "PL", "Kraków"), ("beta.co.uk", "GB", "")])

    def test_problems_are_collected(self):
        path = write_csv(self.path("k.csv"), ["domain", "country"],
                         [["a.pl", "PL"], ["a.pl", "PL"], ["b.pl", "Mordor"], ["", "PL"]])
        with self.assertRaises(FirmsError) as ctx:
            read_profile_csv(path)
        message = str(ctx.exception)
        for fragment in ("drugi raz", "nieznany kraj", "nieprawidłowa domena"):
            self.assertIn(fragment, message)

    def test_missing_columns(self):
        with self.assertRaises(FirmsError):
            read_profile_csv(write_csv(self.path("k.csv"), ["domain"], [["a.pl"]]))


class BackupTests(TempCase):
    def test_backup_of_an_old_shaped_table_has_exactly_its_columns_and_values(self):
        database = old_database(3)
        path = firms.backup_table(database, self.path("backup"), "testem", now=datetime(2026, 10, 1, 12, 30, 5))
        self.assertTrue(path.endswith("Maile_20261001_123005_przed-testem.csv"))
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 3)
        self.assertEqual(list(rows[0]), [c for c in dbmod.LEGACY_COLUMNS] + ["updated_at"])  # kolumny z tabeli, nie z kodu
        self.assertEqual((rows[0]["id"], rows[0]["sent_at"], rows[0]["body_html"]), ("eventy-001", "2026-09-29T10:00:00", "<p>Treść 1</p>"))

    def test_backups_made_in_the_same_second_never_overwrite_each_other(self):
        database = make_db()
        put(database, sample_row(1))
        moment = datetime(2026, 10, 1, 12, 0, 0)
        first = firms.backup_table(database, self.path("b"), "importem", now=moment)
        put(database, sample_row(2))
        second = firms.backup_table(database, self.path("b"), "importem", now=moment)
        third = firms.backup_table(database, self.path("b"), "importem", now=moment)
        self.assertEqual(len({first, second, third}), 3)
        self.assertTrue(second.endswith("_przed-importem_2.csv") and third.endswith("_przed-importem_3.csv"))
        with open(first, newline="", encoding="utf-8") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 1)  # pierwsza kopia nadal ma stan sprzed drugiego wiersza
        with open(second, newline="", encoding="utf-8") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 2)

    def test_backup_includes_new_columns_once_they_exist(self):
        database = make_db()
        put(database, sample_row(1, country="PL", city="Kraków"))
        path = firms.backup_table(database, self.path("b"), "x")
        with open(path, newline="", encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual((row["country"], row["city"]), ("PL", "Kraków"))


class CliTests(TempCase):
    def setUp(self):
        super().setUp()
        self.db_url = f"sqlite:///{self.path('cli.db')}"
        self.config = self.path("config.env")
        open(self.config, "w").close()
        self.backups = self.path("backup")
        self.index_html = self.path("index.html")
        patches = [mock.patch.dict(os.environ, {"PERNO_DB_URL": self.db_url}), mock.patch.object(cfg, "BACKUP_DIR", self.backups),
                   mock.patch.object(cfg, "INDEX_HTML", self.index_html)]  # testy nie ruszają prawdziwego index.html
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.database = old_database(2, url=self.db_url, clock=datetime.now)
        self.addCleanup(self.database.engine.dispose)
        self.new_csv = write_csv(self.path("nowe.csv"), IMPORT_HEADER, [
            ["Nowa A", "https://a-nowa.pl/", "Opis A"], ["Nowa B", "https://b-nowa.pl/", "Opis B"], ["Stara", "https://f1.example.com/", ""]])
        self.countries_csv = write_csv(self.path("kraje.csv"), ["domain", "country", "city"], [
            ["a-nowa.pl", "Polska", "Gdańsk"], ["b-nowa.pl", "Niemcy", "Berlin"], ["f1.example.com", "Polska", ""], ["nie-ma.pl", "UK", "Londyn"]])

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = firmy.main(["--config", self.config, *argv])
        return code, out.getvalue(), err.getvalue()

    def backup_files(self):
        return sorted(os.listdir(self.backups)) if os.path.isdir(self.backups) else []

    def test_import_dry_run_changes_nothing_not_even_the_schema(self):
        code, out, _ = self.cli("import", self.new_csv, "--segment", "eventy", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Do dodania: 2 firm do segmentu eventy", out)
        self.assertIn("Stara  (jako eventy-001)", out)
        self.assertIn("--dry-run: nic nie zapisano", out)
        self.assertEqual(self.backup_files(), [])
        self.assertEqual({c.name for c in self.database.missing_columns()}, set(dbmod.PROFILE_COLUMNS))
        count = self.database.engine.connect().execute(sa.text("SELECT COUNT(*) FROM maile")).scalar()
        self.assertEqual(count, 2)

    def test_import_makes_a_backup_upgrades_the_schema_and_inserts(self):
        code, out, _ = self.cli("import", self.new_csv, "--segment", "eventy")
        self.assertEqual(code, 0, out)
        self.assertIn("Kopia zapasowa bazy:", out)
        self.assertIn("Dodano do tabeli maile kolumny: country, city, address, description", out)
        self.assertIn("Dodano: 2 firm do segmentu eventy", out)
        self.assertIn("eventy-n001 … eventy-n002", out)
        self.assertEqual(len(self.backup_files()), 1)
        self.assertTrue(self.backup_files()[0].endswith("_przed-importem.csv"))
        with open(os.path.join(self.backups, self.backup_files()[0]), newline="", encoding="utf-8") as handle:
            saved = list(csv.DictReader(handle))
        self.assertEqual([r["id"] for r in saved], ["eventy-001", "eventy-002"])  # kopia sprzed zmiany: bez nowych firm
        self.assertNotIn("country", saved[0])
        self.assertEqual(self.database.count_rows(), 4)
        self.assertEqual(self.database.get("eventy-n002")["description"], "Opis B")

    def test_dashboard_is_refreshed_after_changes_but_not_after_a_dry_run(self):
        self.cli("import", self.new_csv, "--segment", "eventy", "--dry-run")
        self.assertFalse(os.path.exists(self.index_html))
        self.cli("import", self.new_csv, "--segment", "eventy")
        with open(self.index_html, encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("Nowa A", html)
        self.cli("kraje", self.countries_csv)
        with open(self.index_html, encoding="utf-8") as handle:
            self.assertIn('"country": "DE"', handle.read())

    def test_second_import_adds_nothing(self):
        self.cli("import", self.new_csv, "--segment", "eventy")
        code, out, _ = self.cli("import", self.new_csv, "--segment", "eventy")
        self.assertEqual(code, 0)
        self.assertIn("Dodano: 0 firm", out)
        self.assertIn("Pominięto — domena jest już w bazie (3)", out)
        self.assertEqual(self.database.count_rows(), 4)

    def test_no_backup_flag(self):
        self.cli("import", self.new_csv, "--segment", "eventy", "--no-backup")
        self.assertEqual(self.backup_files(), [])

    def test_countries_dry_run_needs_the_new_columns_and_explains_how_to_get_them(self):
        code, _, err = self.cli("kraje", self.countries_csv, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("python3 -m perno.db --upgrade", err)

    def test_countries_end_to_end(self):
        self.cli("import", self.new_csv, "--segment", "eventy")
        code, out, _ = self.cli("kraje", self.countries_csv, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Do uzupełnienia: 3 firm", out)
        self.assertEqual(self.database.get("eventy-n001")["country"], "")  # dry-run nic nie zapisał
        code, out, _ = self.cli("kraje", self.countries_csv)
        self.assertEqual(code, 0, out)
        self.assertIn("Uzupełniono: 3 firm", out)
        self.assertIn("Polska 2", out)
        self.assertIn("Niemcy 1", out)
        self.assertIn("Domeny z pliku, których nie ma w bazie (1)", out)
        self.assertIn("Firmy w bazie, których nie ma w pliku (1)", out)
        self.assertEqual(self.database.get("eventy-n002")["country"], "DE")
        self.assertEqual(self.database.get("eventy-001")["country"], "PL")
        self.assertEqual(len(self.backup_files()), 2)  # po jednej kopii przed importem i przed krajami
        again = self.cli("kraje", self.countries_csv)[1]
        self.assertIn("Uzupełniono: 0 firm", again)

    def test_report(self):
        self.cli("import", self.new_csv, "--segment", "eventy")
        self.cli("kraje", self.countries_csv)
        code, out, _ = self.cli("raport")
        self.assertEqual(code, 0)
        self.assertIn("Firm w bazie: 4; z krajem: 3", out)
        self.assertIn("Polska", out)
        self.assertIn("Firmy bez kraju (1)", out)
        self.assertIn("eventy-002", out)

    def test_bad_input_files_fail_cleanly_without_touching_anything(self):
        bad = write_csv(self.path("zle.csv"), IMPORT_HEADER, [["A", "", ""]])
        code, _, err = self.cli("import", bad, "--segment", "eventy")
        self.assertEqual(code, 1)
        self.assertIn("BŁĄD", err)
        self.assertEqual(self.backup_files(), [])
        code, _, err = self.cli("import", self.path("nie-ma.csv"), "--segment", "eventy")
        self.assertEqual((code, "BŁĄD pliku" in err), (1, True))
        self.assertEqual({c.name for c in self.database.missing_columns()}, set(dbmod.PROFILE_COLUMNS))


if __name__ == "__main__":
    unittest.main()
