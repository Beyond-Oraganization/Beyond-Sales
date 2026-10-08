"""ai.py odpowiedzi: pobierz, doctor, stan (sqlite i atrapa poczty, bez sieci)."""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import sqlalchemy as sa

from perno import settings as cfg
from perno.cli import ai, ai_replies
from perno.db.replies_store import ReplyStore
from perno.db.schema import maile
from perno.domain import config_fields
from perno.services import replies
from tests import eml_samples as eml
from tests.fake_imap import FakeAccounts, FakeImap
from tests.fixtures import FakeClock, make_db, put, sample_row

CONFIG = ("SMTP_HOST=s\nSMTP_PORT=465\nSMTP_USER=roman@z3x.pl\nSMTP_PASSWORD=p\nIMAP_HOST=imap.example.com\n"
          "SMTP2_HOST=s\nSMTP2_PORT=465\nSMTP2_USER=drugi@z3x.pl\nSMTP2_PASSWORD=p2\nIMAP2_HOST=imap2.example.com\n"
          "SMTP3_HOST=s\nSMTP3_PORT=465\nSMTP3_USER=trzeci@z3x.pl\nSMTP3_PASSWORD=p3\n"
          "REPLIES_IGNORE=z3x.io\n")


class CliCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.url = f"sqlite:///{os.path.join(self.tmp, 'replies.db')}"
        self.database = make_db(FakeClock(datetime(2026, 10, 8, 10, 0)), url=self.url)
        self.addCleanup(self.database.engine.dispose)
        self.config = os.path.join(self.tmp, "config.env")
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(CONFIG)
        self.backups = os.path.join(self.tmp, "backup")
        for patcher in (mock.patch.dict(os.environ, {"PERNO_DB_URL": self.url}), mock.patch.object(cfg, "BACKUP_DIR", self.backups)):
            patcher.start()
            self.addCleanup(patcher.stop)
        put(self.database, sample_row(1, status="wyslano", sent_at="2026-09-29T13:00:00", email="anna@mylimo.pl", subject="Rezerwacje limuzyn, Kraków"))
        with self.database.engine.begin() as conn:
            conn.execute(sa.update(maile).values(sent_from="roman@z3x.pl", message_id="orig1@z3x.pl"))
        first = FakeImap({"INBOX": {}, "Sent": {}}, roles={"Sent": "sent"})
        first.add("INBOX", 1, eml.plain_reply(in_reply_to="<orig1@z3x.pl>", body="Dzień dobry, proszę o cennik."))
        first.add("INBOX", 2, eml.newsletter())
        self.sessions = FakeAccounts({"roman@z3x.pl": first, "drugi@z3x.pl": FakeImap({"INBOX": {}})})
        patch = mock.patch.object(ai_replies, "imap_factory", lambda: self.sessions)
        patch.start()
        self.addCleanup(patch.stop)

    def ai(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ai.main(["--config", self.config, "odpowiedzi", *argv])
        return code, out.getvalue(), err.getvalue()

    def status(self):
        return self.database.get("eventy-001")["status"]


class FetchTests(CliCase):
    def test_a_trial_run_reports_and_writes_nothing_not_even_a_backup(self):
        code, out, _ = self.ai("pobierz", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("PRÓBA", out)
        self.assertIn("dotyczą nas 1", out)
        self.assertEqual((self.status(), os.path.exists(self.backups)), ("wyslano", False))
        self.assertNotIn("Kopia zapasowa", out)

    def test_a_real_run_makes_a_backup_stores_the_reply_and_marks_the_company(self):
        code, out, _ = self.ai("pobierz")
        self.assertEqual(code, 0)
        self.assertIn("Kopia zapasowa:", out)
        self.assertIn("zapisano 1", out)
        self.assertIn("„odpowiedział” 1", out)
        self.assertEqual(self.status(), "odpowiedzial")
        self.assertTrue(any(name.startswith("Maile_") for name in os.listdir(self.backups)))

    def test_the_output_never_names_third_parties_or_quotes_their_text(self):
        _, out, _ = self.ai("pobierz")
        for secret in ("anna@mylimo.pl", "proszę o cennik", "Anna Nowak", "newsletter"):
            self.assertNotIn(secret, out)

    def test_a_second_run_finds_nothing_new(self):
        self.ai("pobierz")
        _, out, _ = self.ai("pobierz")
        self.assertIn("nowych wiadomości 0", out)
        self.assertIn("zapisano 0", out)

    def test_the_backup_covers_the_reply_tables_after_the_first_run(self):
        self.ai("pobierz")
        self.ai("pobierz")
        names = os.listdir(self.backups)
        self.assertTrue(any(n.startswith("OdpWiadomosci_") for n in names), names)
        self.assertTrue(any(n.startswith("OdpWatki_") for n in names), names)

    def test_a_failing_mailbox_gives_a_nonzero_exit_and_a_readable_line(self):
        self.sessions.sessions["drugi@z3x.pl"].fail_with = TimeoutError("timed out")
        code, out, _ = self.ai("pobierz", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("drugi@z3x.pl: BŁĄD (network)", out)
        self.assertIn("roman@z3x.pl: OK", out)

    def test_one_mailbox_can_be_chosen(self):
        self.ai("pobierz", "--dry-run", "--skrzynka", "drugi@z3x.pl")
        self.assertEqual(self.sessions.opened, ["drugi@z3x.pl"])

    def test_a_bad_date_is_a_clear_error(self):
        code, out, err = self.ai("pobierz", "--od", "wczoraj")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--od", err)

    def test_a_demo_database_refuses_real_connections_when_no_fake_is_injected(self):
        with mock.patch.object(ai_replies, "imap_factory", ai_replies.imap_access.real_factory):
            code, _, err = self.ai("pobierz", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("PERNO_ALLOW_REAL_IMAP", err)


class DoctorAndStateTests(CliCase):
    def test_doctor_checks_every_imap_mailbox_and_names_the_ones_without(self):
        code, out, _ = self.ai("doctor")
        self.assertEqual(code, 0)
        self.assertIn("roman@z3x.pl: OK", out)
        self.assertIn("drugi@z3x.pl: OK", out)
        self.assertIn("Skrzynki bez IMAP (ich odpowiedzi nie są pobierane): 1, np. trzeci@z3x.pl", out)

    def test_doctor_fails_when_a_login_fails(self):
        self.sessions.sessions["roman@z3x.pl"].fail_with = TimeoutError("timed out")
        code, out, _ = self.ai("doctor")
        self.assertEqual(code, 1)
        self.assertIn("roman@z3x.pl: BŁĄD", out)

    def test_state_before_and_after_a_fetch(self):
        _, before, _ = self.ai("stan")
        self.assertIn("Wątki z odpowiedzią człowieka: 0", before)
        self.assertIn("Odbiór poczty: 2 z 3 skrzynek ma ustawiony IMAP.", before)
        self.assertIn("Ostatni przebieg: nigdy.", before)
        self.ai("pobierz")
        _, after, _ = self.ai("stan")
        self.assertIn("Wątki z odpowiedzią człowieka: 1 (czeka na odpowiedź 1, nieprzeczytane 1, nieocenione 1)", after)
        self.assertIn("roman@z3x.pl: ostatnio", after)
        self.assertNotIn("Ostatni przebieg: nigdy", after)

    def test_the_help_lists_the_new_commands_without_breaking_the_old_ones(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            ai.main(["--help"])
        self.assertIn("odpowiedzi", out.getvalue())
        self.assertIn("pobierz", out.getvalue())
        self.assertIn("brief", out.getvalue())


class AiCommandCase(CliCase):
    """Po pobraniu: jeden wątek z odpowiedzią człowieka (eventy-001), jeszcze bez oceny."""

    def setUp(self):
        super().setUp()
        self.ai("pobierz")
        shutil.rmtree(self.backups)  # kopie z pobrania nie mają mieszać się z kopiami sprawdzanymi w testach
        self.store = ReplyStore(self.database)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def thread(self):
        return self.store.thread_state("eventy-001")


class BriefCommandTests(AiCommandCase):
    def test_the_brief_shows_unrated_threads_in_a_fence_without_contact_data(self):
        code, out, _ = self.ai("brief")
        self.assertEqual(code, 0)
        self.assertIn("# wątków bez oceny: 1, w tej paczce: 1, to ostatnia paczka", out)
        self.assertIn("== eventy-001 | Firma 1 | eventy", out)
        self.assertIn("proszę o cennik", out)
        self.assertIn("<<DANE OBCE", out)
        for secret in ("anna@mylimo.pl", "Anna Nowak"):
            self.assertNotIn(secret, out)

    def test_a_rated_thread_is_no_longer_offered_unless_named(self):
        self.ai("ocen", "--id", "eventy-001", "--ocena", "neutral")
        self.assertIn("wątków bez oceny: 0", self.ai("brief")[1])
        self.assertIn("== eventy-001", self.ai("brief", "--id", "eventy-001")[1])

    def test_rated_threads_can_be_listed_by_their_rating(self):
        self.ai("ocen", "--id", "eventy-001", "--ocena", "positive")
        code, out, _ = self.ai("brief", "--ocena", "positive")
        self.assertEqual(code, 0)
        self.assertIn("# wątków z oceną positive: 1, w tej paczce: 1", out)
        self.assertIn("ocena AI: positive", out)
        self.assertIn("wątków z oceną negative: 0", self.ai("brief", "--ocena", "negative")[1])

    def test_the_brief_changes_nothing(self):
        self.ai("brief")
        self.assertEqual((self.thread()["rating"], os.path.exists(self.backups)), (None, False))


class RateCommandTests(AiCommandCase):
    def test_a_single_rating_is_stored_as_an_ai_rating_after_a_backup(self):
        code, out, _ = self.ai("ocen", "--id", "eventy-001", "--ocena", "positive", "--notatka", "pyta o cennik")
        self.assertEqual(code, 0)
        self.assertIn("Zapisano 1 z 1 ocen", out)
        self.assertIn("Kopia:", out)
        self.assertEqual((self.thread()["rating"], self.thread()["rating_by"]), ("positive", "ai"))
        self.assertTrue(any(name.startswith("OdpWatki_") for name in os.listdir(self.backups)))

    def test_a_rating_never_changes_the_company_status(self):
        self.ai("ocen", "--id", "eventy-001", "--ocena", "positive", "--wypisanie")
        self.assertTrue(self.thread()["optout"])
        self.assertEqual(self.status(), "odpowiedzial")

    def test_a_file_is_validated_first_and_a_dry_run_writes_nothing(self):
        path = self.write("oceny.json", [{"id": "eventy-001", "rating": "negative", "note": "odmowa"}])
        code, out, _ = self.ai("ocen", path, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("--dry-run: 1 ocen gotowych do zapisu", out)
        self.assertEqual((self.thread()["rating"], os.path.exists(self.backups)), (None, False))

    def test_one_error_stops_everything_unless_partial_is_allowed(self):
        path = self.write("oceny.json", [{"id": "eventy-001", "rating": "positive"}, {"id": "eventy-404", "rating": "positive"}])
        code, _, err = self.ai("ocen", path)
        self.assertEqual((code, self.thread()["rating"]), (1, None))
        self.assertIn("eventy-404: nie ma takiego wątku", err)
        self.assertIn("nic nie zapisano", err)
        code, out, _ = self.ai("ocen", path, "--czesciowo")
        self.assertEqual((code, self.thread()["rating"]), (1, "positive"))
        self.assertIn("Zapisano 1 z 1 ocen", out)

    def test_a_human_rating_is_left_alone_and_reported(self):
        replies.rate(self.store, "eventy-001", "negative", by="human", now=datetime(2026, 10, 8, 10, 0))
        code, out, _ = self.ai("ocen", "--id", "eventy-001", "--ocena", "positive")
        self.assertEqual((code, self.thread()["rating"]), (0, "negative"))
        self.assertIn("ocena ustawiona przez człowieka", out)
        self.assertIn("Zapisano 0 z 0", out)

    def test_exactly_one_source_is_required(self):
        path = self.write("oceny.json", [])
        for argv in (("ocen",), ("ocen", path, "--id", "eventy-001")):
            with self.subTest(argv=argv):
                code, _, err = self.ai(*argv)
                self.assertEqual(code, 1)
                self.assertIn("plik JSON albo --id", err)

    def test_a_file_that_is_not_a_list_of_objects_is_a_clear_error(self):
        code, _, err = self.ai("ocen", self.write("zle.json", ["tekst"]))
        self.assertEqual(code, 1)
        self.assertIn("listę obiektów", err)


class DraftCommandTests(AiCommandCase):
    def draft_file(self, body="Dziękuję za wiadomość, cennik wyślę jutro."):
        return self.write("szkice.json", [{"id": "eventy-001", "body": body}])

    def test_a_draft_is_stored_for_approval_and_nothing_is_sent(self):
        code, out, _ = self.ai("szkic", self.draft_file())
        self.assertEqual(code, 0)
        self.assertIn("Wysyła człowiek z panelu", out)
        draft = self.store.get_draft("eventy-001")
        self.assertEqual((draft["author"], draft["state"], draft["body_text"]), ("ai", "draft", "Dziękuję za wiadomość, cennik wyślę jutro."))
        self.assertEqual((self.status(), self.store.thread_detail("eventy-001")["thread"]["last_direction"]), ("odpowiedzial", "in"))

    def test_a_dry_run_validates_without_saving(self):
        code, out, _ = self.ai("szkic", self.draft_file(), "--dry-run")
        self.assertEqual((code, self.store.get_draft("eventy-001")), (0, None))
        self.assertIn("1 szkiców gotowych do zapisu", out)

    def test_a_placeholder_or_a_long_dash_is_refused(self):
        for body, expected in (("Dzień dobry [imię]", "znacznik zastępczy"), ("Cennik — w załączniku", "długi myślnik")):
            with self.subTest(body=body):
                code, _, err = self.ai("szkic", self.draft_file(body))
                self.assertEqual((code, self.store.get_draft("eventy-001")), (1, None))
                self.assertIn(expected, err)

    def test_no_draft_for_a_company_that_asked_to_stop(self):
        self.store.update_thread("eventy-001", optout=True)
        code, _, err = self.ai("szkic", self.draft_file())
        self.assertEqual((code, self.store.get_draft("eventy-001")), (1, None))
        self.assertIn("brak kontaktu", err)

    def test_a_draft_written_by_a_human_is_kept(self):
        self.store.save_draft("eventy-001", body="Moja wersja", subject="Re: x", to_addr="anna@mylimo.pl", mailbox="roman@z3x.pl",
                              author="human", now=datetime(2026, 10, 8, 10, 0))
        code, out, _ = self.ai("szkic", self.draft_file())
        self.assertEqual(code, 0)
        self.assertIn("jest szkic człowieka", out)
        self.assertEqual(self.store.get_draft("eventy-001")["body_text"], "Moja wersja")


class ReportCommandTests(AiCommandCase):
    def test_the_report_is_printed_as_markdown_without_third_party_data(self):
        code, out, _ = self.ai("raport")
        self.assertEqual(code, 0)
        self.assertIn("# Raport odpowiedzi", out)
        self.assertIn("Odpowiedzi człowieka: 1", out)
        self.assertNotIn("anna@mylimo.pl", out)

    def test_the_report_can_be_narrowed_to_a_period_and_a_segment(self):
        _, out, _ = self.ai("raport", "--od", "2026-10-01", "--segment", "eventy")
        self.assertIn("Wysłano maili: 0", out)
        _, out, _ = self.ai("raport", "--do", "2026-09-30", "--segment", "eventy")
        self.assertIn("Wysłano maili: 1", out)

    def test_a_reversed_period_is_a_clear_error(self):
        code, _, err = self.ai("raport", "--od", "2026-10-05", "--do", "2026-10-01")
        self.assertEqual(code, 1)
        self.assertIn("późniejsza niż końcowa", err)

    def test_saving_writes_the_file_into_the_ignored_reports_directory(self):
        with mock.patch.object(cfg, "BASE_DIR", self.tmp):
            code, out, _ = self.ai("raport", "--zapisz")
        path = os.path.join(self.tmp, "raporty", "odpowiedzi_20261008.md")
        self.assertEqual(code, 0)
        self.assertIn(f"Zapisano: {path}", out)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("# Raport odpowiedzi", handle.read())


class BoundaryTests(unittest.TestCase):
    """AI ocenia i pisze szkice, ale `ai.py` nie ma drogi do wysyłki: jego moduły i `services/ai_replies` nie sięgają po SMTP ani ścieżkę wysyłki."""

    FORBIDDEN = ("send_reply", "send_row", "send_one", "send_batch", "build_mailers", "build_mailer", "SmtpClient", "smtp_client", "smtplib", "SendOptions")

    def sources(self):
        cli = os.path.dirname(os.path.abspath(ai_replies.__file__))
        files = [os.path.join(cli, name) for name in ("ai.py", "ai_replies.py", "ai_scrape.py", "entries.py")]
        files.append(os.path.join(os.path.dirname(cli), "services", "ai_replies.py"))
        return files

    def test_no_command_line_module_can_send_mail(self):
        files = self.sources()
        self.assertTrue(all(os.path.exists(path) for path in files))
        for path in files:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            tree = ast.parse(source)
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            names |= {alias.name.split(".")[-1] for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
            with self.subTest(path=os.path.relpath(path)):
                self.assertEqual(sorted(names & set(self.FORBIDDEN)), [])


class DocsTests(unittest.TestCase):
    """Dokumentacja zgadza się z poleceniami: nic nie jest opisane, czego nie ma, i nic nie ma, czego nie opisano."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def read(self, name):
        with open(os.path.join(self.ROOT, name), encoding="utf-8") as handle:
            return handle.read()

    def subcommands(self):
        top = next(action for action in ai.build_parser()._actions if isinstance(action, argparse._SubParsersAction))
        root = top.choices["odpowiedzi"]
        actions = next(action for action in root._actions if isinstance(action, argparse._SubParsersAction))
        return actions.choices

    def test_every_subcommand_and_flag_is_in_the_instructions_the_readme_and_the_usage(self):
        commands = self.subcommands()
        self.assertEqual(sorted(commands), ["brief", "doctor", "ocen", "pobierz", "raport", "stan", "szkic"])
        texts = {name: self.read(name) for name in ("AI_INSTRUCTIONS.md", "README.md")}
        texts["ai.py usage"] = ai.__doc__
        for name, parser in commands.items():
            flags = [option for action in parser._actions for option in action.option_strings if option.startswith("--") and option != "--help"]
            for source, text in texts.items():
                with self.subTest(command=name, source=source):
                    self.assertIn(f"odpowiedzi {name}", text)
                    if source == "AI_INSTRUCTIONS.md":
                        self.assertEqual([flag for flag in flags if flag not in text], [])

    def test_every_documented_command_exists(self):
        known = set(self.subcommands())
        for source in ("AI_INSTRUCTIONS.md", "README.md", "CLAUDE.md"):
            for found in re.findall(r"ai\.py odpowiedzi ([a-z]+)", self.read(source)):
                with self.subTest(source=source, command=found):
                    self.assertIn(found, known)

    def test_the_user_commands_link_to_headings_that_exist(self):
        text = self.read("AI_INSTRUCTIONS.md")
        anchors = {re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-") for heading in re.findall(r"^#+ (.+)$", text, flags=re.M)}
        links = re.findall(r"\]\(#([^)]+)\)", text)
        self.assertGreaterEqual(len(links), 10)
        self.assertEqual([link for link in links if link not in anchors], [])
        for command in ("POBIERZ ODPOWIEDZI", "OCEŃ odpowiedzi", "PRZYGOTUJ szkice odpowiedzi", "POKAŻ RAPORT odpowiedzi"):
            self.assertIn(f"| `{command}", text)

    def test_the_example_config_names_every_reply_key_the_panel_knows(self):
        example = self.read("config.example.env")
        keys = [field.key for field in config_fields.FIELDS if field.group == "replies"]
        self.assertEqual(len(keys), 5)
        self.assertEqual([key for key in keys if key not in example], [])
        self.assertIn("IMAP_HOST", example)
        self.assertIn("IMAP2_HOST", example)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_parsing(self):
        self.assertEqual(cfg.ReplySettings.from_config({}), cfg.ReplySettings((), (), 4))
        got = cfg.ReplySettings.from_config({"REPLIES_IGNORE": " Jakub@Z3X.io, z3x.io ,", "REPLIES_FOLDERS": "INBOX, Wysłane", "REPLIES_FETCH_WORKERS": "8"})
        self.assertEqual((got.ignore, got.folders, got.workers), (("jakub@z3x.io", "z3x.io"), ("INBOX", "Wysłane"), 8))

    def test_bad_worker_counts_are_config_errors(self):
        for value in ("x", "0", "17", "-1"):
            with self.subTest(value=value), self.assertRaises(cfg.ConfigError):
                cfg.ReplySettings.from_config({"REPLIES_FETCH_WORKERS": value})
        for value in ("1", "16"):
            self.assertEqual(cfg.ReplySettings.from_config({"REPLIES_FETCH_WORKERS": value}).workers, int(value))


if __name__ == "__main__":
    unittest.main()
