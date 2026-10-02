from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

import sqlalchemy as sa

from perno.scraper import runner
from perno.scraper import statuses as st
from perno.scraper.contacts import Contact
from perno.scraper.schema import firmy
from perno.scraper.store import LinkRecord, PageRecord, Store
from perno.scraper.urls import url_hash
from perno.services import scan_control
from perno.settings import ConfigFile
from tests.fixtures import put, sample_row
from tests.test_web import SECRET, WebCase, write_config

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_config_with(directory: str, extra: str) -> str:
    path = write_config(directory)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(extra)
    return path


class SkaperCase(WebCase):
    def setUp(self):
        super().setUp()
        self.spawned = []
        self.logs = os.path.join(self.tmp, "logs")
        os.makedirs(self.logs)
        env = mock.patch.dict(os.environ, {"PERNO_LOG_DIR": self.logs})
        env.start()
        self.addCleanup(env.stop)

        def fake_spawn(run_id, config_path):
            self.spawned.append((run_id, config_path))
            return os.getpid(), os.path.join(self.logs, f"skraper_test_{run_id}.log")  # żywy pid: start nie odzyska własnego przebiegu

        patcher = mock.patch.object(runner, "spawn_detached", side_effect=fake_spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = self.app.ctx.scan_store.get()
        self.app.ctx.config = ConfigFile(write_config_with(self.tmp, "SKRAPER_CONTACT=ops@example.com\n"))

    def seed(self, count=3):
        put(self.database, *[sample_row(n) for n in range(1, count + 1)])
        return self.data(self.call("POST", "/api/skaper/seed", {}))

    def firm_ids(self):
        return [row["id"] for row in self.data(self.call("GET", "/api/skaper/firmy"))["rows"]]

    def start(self, **payload):
        return self.call("POST", "/api/skaper/start", payload)


class PageAndMetaTests(SkaperCase):
    def test_page_is_in_the_menu_and_renders(self):
        resp = self.call("GET", "/skraper")
        html = resp.body.decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn('<a class="topnav-link" href="/skraper" aria-current="page">Skraper</a>', html)
        self.assertIn('href="/">Wysyłka</a>', html)
        self.assertIn('id="startBtn"', html)
        self.assertIn('id="s_delay"', html)
        self.assertIn("Zasady uprzejmości", html)

    def test_meta(self):
        self.seed(2)
        data = self.data(self.call("GET", "/api/skaper/meta"))
        self.assertEqual(data["floor"], 1.0)
        self.assertEqual((data["firmy_count"], data["maile_count"], data["bs4_ok"]), (2, 2, True))
        self.assertEqual(data["contact"], {"ok": True, "value": "ops@example.com", "source": "config"})
        self.assertEqual(data["defaults"]["concurrency"], 30)
        self.assertEqual(data["defaults"]["delay"], 2.5)
        self.assertEqual(data["limits"]["delay"], [1.0, 60.0])
        self.assertEqual(data["profile"], "zbalansowany")
        profiles = {p["code"]: p for p in data["profiles"]}
        self.assertEqual(set(profiles), {"zbalansowany", "kontekst"})
        self.assertEqual(profiles["kontekst"]["values"], {"depth": 4, "max_pages": 25, "max_mb": 15, "max_minutes": 10})
        self.assertEqual({k: data["defaults"][k] for k in profiles["zbalansowany"]["values"]}, profiles["zbalansowany"]["values"])
        counts = {s["code"]: s["count"] for s in data["statuses"]}
        self.assertEqual(counts[st.FIRMA_NIESKANOWANA], 2)
        self.assertIn("robots_disallow", data["reasons"])
        self.assertEqual(data["default_statuses"], list(st.FIRMA_DOMYSLNIE_DO_SKANU))

    def test_meta_reports_smtp_user_fallback_and_invalid_settings_without_failing(self):
        self.app.ctx.config = ConfigFile(write_config(self.tmp))  # tylko SMTP_USER
        contact = self.data(self.call("GET", "/api/skaper/meta"))["contact"]
        self.assertEqual((contact["ok"], contact["source"], contact["value"]), (True, "smtp_user", "demo@example.com"))
        self.app.ctx.config = ConfigFile(write_config_with(self.tmp, "SKRAPER_CONCURRENCY=dużo\n"))
        data = self.data(self.call("GET", "/api/skaper/meta"))
        self.assertIn("nie jest liczbą", data["settings_error"])

    def test_meta_never_leaks_secrets(self):
        body = self.call("GET", "/api/skaper/meta").body.decode("utf-8")
        self.assertNotIn(SECRET, body)
        self.assertNotIn("SMTP_PASSWORD", body)

    def test_bs4_missing_is_reported(self):
        with mock.patch.object(scan_control, "parser_available", return_value=False):
            self.assertFalse(self.data(self.call("GET", "/api/skaper/meta"))["bs4_ok"])


class CompaniesApiTests(SkaperCase):
    def test_seed_and_add(self):
        result = self.seed(3)
        self.assertEqual((result["dodano"], result["razem"]), (3, 3))
        self.assertEqual(self.data(self.call("POST", "/api/skaper/seed", {}))["dodano"], 0)
        added = self.data(self.call("POST", "/api/skaper/dodaj", {"url": "https://nowa.example.com/kontakt", "nazwa": "Nowa"}))
        self.assertEqual((added["domena"], added["nowa"]), ("nowa.example.com", True))
        self.assertFalse(self.data(self.call("POST", "/api/skaper/dodaj", {"url": "nowa.example.com"}))["nowa"])

    def test_add_validation(self):
        for payload, fragment in (({}, "adres"), ({"url": "  "}, "adres"), ({"url": 5}, "adres"),
                                  ({"url": "https://twitter.com/x"}, "platforma"), ({"url": "http://127.0.0.1"}, "niedozwolony"),
                                  ({"url": "ok.pl", "nazwa": 5}, "tekstem")):
            resp = self.call("POST", "/api/skaper/dodaj", payload)
            self.assertEqual(resp.status, 400, payload)
            self.assertIn(fragment, self.data(resp)["error"].lower(), payload)

    def test_list_filter_search_and_validation(self):
        self.seed(5)
        ids = self.firm_ids()
        self.store.enqueue([ids[0]])
        listing = self.data(self.call("GET", "/api/skaper/firmy", query={"status": [st.FIRMA_W_KOLEJCE]}))
        self.assertEqual((listing["total"], [r["id"] for r in listing["rows"]]), (1, [ids[0]]))
        self.assertEqual(self.data(self.call("GET", "/api/skaper/firmy", query={"q": ["FIRMA3"]}))["total"], 1)
        self.assertEqual(self.data(self.call("GET", "/api/skaper/firmy", query={"limit": ["2"], "offset": ["1"]}))["rows"][0]["id"], ids[1])
        for query in ({"status": ["zmyslony"]}, {"limit": ["0"]}, {"limit": ["501"]}, {"offset": ["-1"]}, {"offset": ["x"]}):
            self.assertEqual(self.call("GET", "/api/skaper/firmy", query=query).status, 400, query)

    def test_detail_with_contacts_and_pages(self):
        self.seed(1)
        firma_id = self.firm_ids()[0]
        self.store.enqueue([firma_id])
        run_id = self.store.start_run({})
        self.store.claim_next_firma(run_id)
        url = "https://firma1.example.com/kontakt"
        self.store.save_result(firma_id, run_id, PageRecord(
            url=url, url_hash=url_hash(url), depth=1, priority=10, source="link", status="pobrano", http_status=200,
            title="Kontakt", text="tekst", contacts=[Contact("email", "biuro@firma1.example.com", "mailto", True, "ogolny")],
            links=[LinkRecord("https://firma1.example.com/x", url_hash("https://firma1.example.com/x"), 2, 100)]))
        detail = self.data(self.call("GET", "/api/skaper/firma", query={"id": [str(firma_id)]}))
        self.assertEqual(detail["strony_razem"], 2)
        self.assertEqual(detail["kontakty"][0]["wartosc"], "biuro@firma1.example.com")
        self.assertEqual(detail["firma"]["stron_pobranych"], 1)
        self.assertNotIn("tekst", detail["strony"][0])  # treść stron nie wychodzi przez API szczegółów
        self.assertEqual(self.call("GET", "/api/skaper/firma", query={"id": ["9999"]}).status, 404)
        self.assertEqual(self.call("GET", "/api/skaper/firma", query={"id": ["x"]}).status, 400)
        self.assertEqual(self.call("GET", "/api/skaper/firma", query={"id": ["0"]}).status, 400)

    def test_attacker_controlled_text_is_returned_as_json_data_only(self):
        evil = "<script>alert(1)</script>"
        self.store.add_manual("https://xss.example.com/", evil)
        resp = self.call("GET", "/api/skaper/firmy")
        self.assertTrue(resp.content_type.startswith("application/json"))
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.data(resp)["rows"][0]["nazwa"], evil)
        self.assertNotIn(evil, self.call("GET", "/skraper").body.decode("utf-8"))  # strona sama nie zawiera danych


class RunLifecycleTests(SkaperCase):
    def test_start_reserves_the_slot_queues_firms_and_spawns_a_detached_process(self):
        self.seed(3)
        resp = self.start(ustawienia={"concurrency": 5, "delay": 3, "max_pages": 40})
        self.assertEqual(resp.status, 202)
        data = self.data(resp)
        self.assertEqual(data["queued"], 3)
        self.assertEqual(self.spawned, [(data["run_id"], self.app.ctx.config.path)])
        run = self.store.get_run(data["run_id"])
        self.assertEqual((run["pid"], run["status"], run["aktywny"]), (os.getpid(), st.RUN_URUCHAMIANIE, 1))
        self.assertTrue(run["log_path"].startswith(self.logs))
        stored = run["parametry"]["ustawienia"]
        self.assertEqual((stored["concurrency"], stored["delay"], stored["max_pages"], stored["contact"]), (5, 3.0, 40, "ops@example.com"))
        self.assertEqual({r["skan_status"] for r in self.data(self.call("GET", "/api/skaper/firmy"))["rows"]}, {st.FIRMA_W_KOLEJCE})

    def test_start_with_the_context_profile_stores_its_budget(self):
        self.seed(1)
        run_id = self.data(self.start(ustawienia={"profile": "kontekst", "concurrency": 5}))["run_id"]
        stored = self.store.get_run(run_id)["parametry"]["ustawienia"]
        self.assertEqual((stored["profile"], stored["max_pages"], stored["depth"], stored["max_discovered"], stored["concurrency"]),
                         ("kontekst", 25, 4, 500, 5))
        self.assertEqual((stored["delay"], stored["floor"]), (2.5, 1.0))  # profil nie dotyka uprzejmości

    def test_unknown_profile_is_rejected_without_blocking_the_slot(self):
        self.seed(1)
        resp = self.start(ustawienia={"profile": "pelny"})
        self.assertEqual(resp.status, 400)
        self.assertIn("Profil musi być jednym z", self.data(resp)["error"])
        self.assertIsNone(self.store.active_run())
        self.assertEqual(self.start().status, 202)

    def test_ui_cannot_lower_the_delay_floor_or_exceed_limits(self):
        self.seed(1)
        run_id = self.data(self.start(ustawienia={"delay": 0, "concurrency": 99999, "depth": -3, "max_pages": 10**9}))["run_id"]
        stored = self.store.get_run(run_id)["parametry"]["ustawienia"]
        self.assertEqual((stored["delay"], stored["concurrency"], stored["depth"], stored["max_pages"]), (1.0, 100, 1, 5000))
        self.assertEqual(stored["floor"], 1.0)

    def test_only_one_run_at_a_time(self):
        self.seed(2)
        self.assertEqual(self.start().status, 202)
        second = self.start()
        self.assertEqual(second.status, 409)
        self.assertIn("już działa", self.data(second)["error"])
        self.assertEqual(len(self.spawned), 1)

    def test_start_for_selected_firms_only(self):
        self.seed(4)
        ids = self.firm_ids()
        data = self.data(self.start(firma_ids=[ids[1], ids[3]]))
        self.assertEqual(data["queued"], 2)
        self.assertEqual(self.store.get_run(data["run_id"])["parametry"]["firma_ids"], [ids[1], ids[3]])
        statuses = {r["id"]: r["skan_status"] for r in self.data(self.call("GET", "/api/skaper/firmy"))["rows"]}
        self.assertEqual((statuses[ids[0]], statuses[ids[1]]), (st.FIRMA_NIESKANOWANA, st.FIRMA_W_KOLEJCE))

    def test_start_validation(self):
        self.seed(2)
        ids = self.firm_ids()
        for payload in ({"firma_ids": "1,2"}, {"firma_ids": [True]}, {"firma_ids": ["1"]}, {"firma_ids": [999999]},
                        {"firma_ids": list(range(1, 502))}, {"statuses": []}, {"statuses": ["zmyslony"]},
                        {"statuses": "blad"}, {"ustawienia": {"depth": {"a": 1}}}, {"ustawienia": []},
                        {"ustawienia": {"concurrency": "abc"}}, {"ustawienia": {"contact": "zły adres ze spacjami"}},
                        {"ustawienia": {"store_html": "wszystko"}}):
            resp = self.start(**payload)
            self.assertEqual(resp.status, 400, payload)
            self.assertIsNone(self.store.active_run(), payload)  # nieudany start nie blokuje slotu
        self.assertEqual(self.spawned, [])
        self.assertEqual(ids, self.firm_ids())

    def test_start_without_firms_to_process_releases_the_slot(self):
        resp = self.start()
        self.assertEqual(resp.status, 400)
        self.assertIn("Brak firm", self.data(resp)["error"])
        self.assertIsNone(self.store.active_run())
        self.seed(1)
        self.assertEqual(self.start().status, 202)

    def test_start_requires_contact_and_bs4(self):
        self.seed(1)
        empty = os.path.join(self.tmp, "pusty.env")
        open(empty, "w").close()
        self.app.ctx.config = ConfigFile(empty)
        resp = self.start()
        self.assertEqual(resp.status, 400)
        self.assertIn("SKRAPER_CONTACT", self.data(resp)["error"])
        self.app.ctx.config = ConfigFile(write_config_with(self.tmp, "SKRAPER_CONTACT=ops@example.com\n"))
        with mock.patch.object(scan_control, "parser_available", return_value=False):
            resp = self.start()
        self.assertEqual(resp.status, 400)
        self.assertIn("beautifulsoup4", self.data(resp)["error"])
        self.assertIsNone(self.store.active_run())

    def test_failed_spawn_releases_the_slot_and_marks_the_run(self):
        self.seed(1)
        runner.spawn_detached.side_effect = OSError("brak pliku")
        resp = self.start()
        self.assertEqual(resp.status, 500)
        self.assertIsNone(self.store.active_run())
        self.assertEqual(self.store.latest_run()["status"], st.RUN_BLAD)
        runner.spawn_detached.side_effect = lambda run_id, config: (os.getpid(), os.path.join(self.logs, "x.log"))
        self.assertEqual(self.start().status, 202)

    def test_start_recovers_a_dead_previous_run_first(self):
        self.seed(1)
        dead = self.store.start_run({}, pid=2_000_000_000)
        self.assertEqual(self.start().status, 202)
        self.assertEqual(self.store.get_run(dead)["status"], st.RUN_PRZERWANY)

    def test_stop(self):
        self.assertEqual(self.data(self.call("POST", "/api/skaper/stop", {})), {"ok": False, "odzyskano": []})
        self.seed(1)
        run_id = self.data(self.start())["run_id"]
        self.store.attach_run(run_id, pid=os.getpid())
        self.assertTrue(self.data(self.call("POST", "/api/skaper/stop", {}))["ok"])
        self.assertTrue(self.store.get_run(run_id)["stop_requested"])
        self.store.finish_run(run_id, st.RUN_ZATRZYMANY)
        dead = self.store.start_run({}, pid=2_000_000_000)
        result = self.data(self.call("POST", "/api/skaper/stop", {}))
        self.assertEqual(result["odzyskano"], [dead])

    def test_status_endpoint(self):
        idle = self.data(self.call("GET", "/api/skaper/status"))
        self.assertEqual((idle["aktywny"], idle["run"], idle["log"]), (False, None, []))
        self.seed(2)
        run_id = self.data(self.start())["run_id"]
        active = self.data(self.call("GET", "/api/skaper/status"))
        self.assertTrue(active["aktywny"])
        self.assertEqual(active["run"]["id"], run_id)
        self.assertEqual(active["przeglad"]["kolejka"], 2)
        self.assertFalse(active["nieodpowiada"])
        self.database._clock.advance(seconds=200)
        stale = self.data(self.call("GET", "/api/skaper/status"))
        self.assertTrue(stale["nieodpowiada"])
        self.assertGreater(stale["heartbeat_age"], 90)
        self.assertNotIn(SECRET, self.call("GET", "/api/skaper/status").body.decode("utf-8"))

    def test_status_log_tail_cannot_read_files_outside_the_log_directory(self):
        self.seed(1)
        run_id = self.data(self.start())["run_id"]
        self.store.attach_run(run_id, log_path=os.path.join(self.logs, "..", "config.example.env"))
        self.assertEqual(self.data(self.call("GET", "/api/skaper/status"))["log"], [])
        path = os.path.join(self.logs, "skraper_web_test.log")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("pierwsza\ndruga\n")
        self.store.attach_run(run_id, log_path=path)
        self.assertEqual(self.data(self.call("GET", "/api/skaper/status"))["log"], ["pierwsza", "druga"])


class AdminApiTests(SkaperCase):
    def test_retry_validation_and_effect(self):
        self.seed(2)
        ids = self.firm_ids()
        for payload in ({}, {"firma_ids": []}, {"firma_ids": ["a"]}):
            self.assertEqual(self.call("POST", "/api/skaper/ponow", payload).status, 400, payload)
        with self.database.engine.begin() as conn:
            conn.execute(sa.update(firmy).where(firmy.c.id == ids[0]).values(skan_status=st.FIRMA_ZABLOKOWANA, skan_przyczyna="bot_wall"))
        self.assertEqual(self.data(self.call("POST", "/api/skaper/ponow", {"firma_ids": ids}))["zmieniono"], 1)

    def test_use_target_domain_and_wipe_error_mapping(self):
        self.seed(1)
        firma_id = self.firm_ids()[0]
        self.assertEqual(self.call("POST", "/api/skaper/uzyj-domeny", {"firma_id": firma_id}).status, 400)  # brak domeny docelowej
        self.assertEqual(self.call("POST", "/api/skaper/uzyj-domeny", {"firma_id": 9999}).status, 404)
        self.assertEqual(self.call("POST", "/api/skaper/uzyj-domeny", {"firma_id": "x"}).status, 400)
        self.assertEqual(self.call("POST", "/api/skaper/usun-dane", {"firma_id": 9999}).status, 404)
        with self.database.engine.begin() as conn:
            conn.execute(sa.update(firmy).where(firmy.c.id == firma_id).values(skan_status=st.FIRMA_SKANOWANIE, domena_docelowa="inna.example.com"))
        self.assertEqual(self.call("POST", "/api/skaper/usun-dane", {"firma_id": firma_id}).status, 409)
        self.assertEqual(self.call("POST", "/api/skaper/uzyj-domeny", {"firma_id": firma_id}).status, 409)
        with self.database.engine.begin() as conn:
            conn.execute(sa.update(firmy).where(firmy.c.id == firma_id).values(skan_status=st.FIRMA_POMINIETA))
        self.assertEqual(self.data(self.call("POST", "/api/skaper/uzyj-domeny", {"firma_id": firma_id}))["domena"], "inna.example.com")
        self.assertTrue(self.data(self.call("POST", "/api/skaper/usun-dane", {"firma_id": firma_id}))["ok"])
        other = self.store.add_manual("https://zajeta.example.com/")["id"]
        with self.database.engine.begin() as conn:
            conn.execute(sa.update(firmy).where(firmy.c.id == firma_id).values(domena_docelowa="zajeta.example.com"))
        self.assertEqual(self.call("POST", "/api/skaper/uzyj-domeny", {"firma_id": firma_id}).status, 409)
        self.assertIsNotNone(other)


class HttpProtectionTests(SkaperCase):
    def test_post_routes_keep_the_application_level_protections(self):
        self.assertEqual(self.call("POST", "/api/skaper/seed", {}, headers={"content-type": "text/plain"}).status, 415)
        self.assertEqual(self.call("POST", "/api/skaper/seed", {}, headers={"host": "evil.example.com"}).status, 403)
        self.assertEqual(self.call("POST", "/api/skaper/start", {}, headers={"origin": "http://evil.example.com"}).status, 403)
        self.assertEqual(self.call("GET", "/api/skaper/start").status, 404)  # start tylko przez POST
        self.assertEqual(self.call("POST", "/api/skaper/firmy", {}).status, 404)  # a listing tylko przez GET

    def test_server_error_paths_never_leak_internals(self):
        with mock.patch.object(Store, "status_overview", side_effect=RuntimeError("sekret " + SECRET)):
            resp = self.call("GET", "/api/skaper/meta")
        self.assertEqual(resp.status, 500)
        self.assertNotIn(SECRET, resp.body.decode("utf-8"))


class StartupHelpersTests(SkaperCase):
    def test_prepare_recovers_dead_runs_and_reports(self):
        self.seed(1)
        dead = self.store.start_run({}, pid=2_000_000_000)
        lines = []
        self.assertEqual(scan_control.recover_on_start(self.app.ctx.scan_store.get, log=lines.append), [dead])
        self.assertIn("odzyskano przerwane przebiegi", lines[0])

    def test_prepare_swallows_errors_so_the_mailing_panel_still_starts(self):
        lines = []
        with mock.patch.object(Store, "recover_dead_runs", side_effect=RuntimeError("tabela nie istnieje")):
            self.assertEqual(scan_control.recover_on_start(self.app.ctx.scan_store.get, log=lines.append), [])
        self.assertIn("Panel wysyłki działa normalnie", lines[0])

    def test_shutdown_hint(self):
        self.assertIsNone(scan_control.shutdown_hint(self.app.ctx.scan_store.get))
        self.store.start_run({}, pid=os.getpid())
        self.assertIn("python3 scrape.py stop", scan_control.shutdown_hint(self.app.ctx.scan_store.get))


class ImportIsolationTests(unittest.TestCase):
    def test_panel_modules_never_import_bs4_or_the_crawler(self):
        code = ("import sys; import perno.cli.server, perno.web.pages, perno.web.pages.scraper; "
                "bad = [m for m in ('bs4', 'perno.scraper.crawler', 'perno.scraper.extract', 'perno.scraper.scheduler', 'perno.scraper.company_crawl', 'perno.scraper.page_fetch', 'perno.scraper.page_pipeline') if m in sys.modules]; "
                "assert not bad, bad")
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=BASE)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
