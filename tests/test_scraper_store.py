from __future__ import annotations

import os
import shutil
import tempfile
import threading
import types
import unittest
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc

from lib.scraper import statuses as st
from lib.scraper.contacts import Contact
from lib.scraper.schema import TABLE_NAMES, firmy, firmy_maile, kontakty, skan_przebiegi, strony, strony_tresc
from lib.scraper.store import (DomainTaken, FirmaBusy, InvalidFirma, LeaseLost, LinkRecord, NotFound, PageRecord,
                               RunActive, Store, StoreError, ascii_or_none, clean_text, compress_html, pid_alive)
from lib.scraper.urls import url_hash
from tests.fixtures import FakeClock, make_db, put, sample_row


def link(path: str, depth: int = 1, priority: int = 100, host: str = "https://a.pl") -> LinkRecord:
    url = host + path
    return LinkRecord(url=url, url_hash=url_hash(url), depth=depth, priority=priority)


def page(path: str = "/", **kwargs) -> PageRecord:
    url = "https://a.pl" + path
    values = dict(url=url, url_hash=url_hash(url), depth=0, priority=0, source="start", status=st.STRONA_POBRANO,
                  http_status=200, content_type="text/html", bytes=1000, text="treść", title="Tytuł")
    values.update(kwargs)
    return PageRecord(**values)


class StoreCase(unittest.TestCase):
    file_db = False

    def setUp(self):
        self.clock = FakeClock()
        if self.file_db:
            self.tmp = tempfile.mkdtemp(prefix="perno-store-")
            self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
            self.db = make_db(self.clock, url=f"sqlite:///{os.path.join(self.tmp, 'store.db')}")
        else:
            self.db = make_db(self.clock)
        self.addCleanup(self.db.engine.dispose)
        self.store = Store(self.db)
        self.store.ensure_schema()

    def add_firms(self, count: int, host: str = "firma{n}.example.com") -> list:
        ids = []
        for n in range(1, count + 1):
            ids.append(self.store.add_manual("https://" + host.format(n=n) + "/")["id"])
        return ids

    def firma(self, firma_id: int) -> dict:
        return self.store.get_firma(firma_id)

    def claimed(self, firma_id: int | None = None, run_id: int | None = None):
        """Zajmij firmę w nowym przebiegu; zwraca (firma, run_id)."""
        run_id = run_id or self.store.start_run({})
        self.store.enqueue([firma_id] if firma_id else None)
        return self.store.claim_next_firma(run_id), run_id

    def count(self, table) -> int:
        with self.db.engine.connect() as conn:
            return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar()


class SchemaAndHelpersTests(StoreCase):
    def test_ensure_schema_creates_everything_and_is_idempotent(self):
        self.store.ensure_schema()
        names = set(sa.inspect(self.db.engine).get_table_names())
        self.assertTrue(set(TABLE_NAMES) <= names)
        self.assertIn("maile", names)

    def test_clean_text(self):
        self.assertEqual(clean_text("a\x00b\x01c\x7fd\te\nf"), "abcd\te\nf")
        self.assertEqual(clean_text("zażółć\ud800x"), "zażółćx")
        self.assertEqual(clean_text("abcdef", 3), "abc")
        self.assertEqual(clean_text("😀😀😀", 2), "😀😀")
        self.assertIsNone(clean_text(None))

    def test_ascii_or_none(self):
        self.assertEqual(ascii_or_none("https://a.pl/x", 100), "https://a.pl/x")
        self.assertIsNone(ascii_or_none("https://a.pl/zażółć", 100))
        self.assertIsNone(ascii_or_none("abcdef", 3))
        self.assertIsNone(ascii_or_none("a\x00b", 10))
        self.assertIsNone(ascii_or_none(None, 10))

    def test_pid_alive(self):
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(None))
        self.assertFalse(pid_alive(0))
        self.assertFalse(pid_alive(-5))
        self.assertFalse(pid_alive(2_000_000_000))

    def test_unsupported_backend_is_refused(self):
        class Fake:
            backend = "postgresql"
            engine = None

        with self.assertRaises(StoreError):
            Store(Fake())


class SeedTests(StoreCase):
    def test_seed_creates_one_firm_per_domain_and_links_every_mail_row(self):
        put(self.db, sample_row(1), sample_row(2),
            sample_row(3, website="https://www.Firma1.example.com/oferta"),  # ta sama domena co 1 (www, wielkość liter, ścieżka)
            sample_row(4, website="http://firma4.example.com"))
        result = self.store.seed_from_maile()
        self.assertEqual((result["dodano"], result["pominieto"], result["razem"]), (3, 0, 3))
        self.assertEqual(self.count(firmy), 3)
        self.assertEqual(self.count(firmy_maile), 4)
        first = self.store.find_firma("firma1.example.com")
        self.assertEqual((first["nazwa"], first["segment"], first["skan_status"], first["zrodlo"]),
                         ("Firma 1", "eventy", st.FIRMA_NIESKANOWANA, "maile"))
        self.assertEqual(first["website"], "https://firma1.example.com/")
        self.assertEqual(first["origin"], "https://firma1.example.com")

    def test_seed_is_idempotent_and_picks_up_new_mail_rows(self):
        put(self.db, sample_row(1), sample_row(2))
        self.store.seed_from_maile()
        again = self.store.seed_from_maile()
        self.assertEqual((again["dodano"], again["razem"]), (0, 2))
        put(self.db, sample_row(3))
        self.assertEqual(self.store.seed_from_maile()["dodano"], 1)
        self.assertEqual(self.count(firmy_maile), 3)

    def test_seed_skips_missing_invalid_and_platform_websites_and_reports_reasons(self):
        put(self.db, sample_row(1, website=""), sample_row(2, website="https://www.facebook.com/firma"),
            sample_row(3, website="http://192.168.0.1/"), sample_row(4, website="localhost"), sample_row(5))
        result = self.store.seed_from_maile()
        self.assertEqual((result["dodano"], result["pominieto"]), (1, 4))
        self.assertEqual(result["powody"], {"brak_www": 1, "platforma": 1, "zly_url": 2})

    def test_seed_does_not_touch_existing_firm_state(self):
        put(self.db, sample_row(1))
        self.store.seed_from_maile()
        firma_id = self.store.find_firma("firma1.example.com")["id"]
        self.store.enqueue([firma_id])
        self.store.seed_from_maile()
        self.assertEqual(self.firma(firma_id)["skan_status"], st.FIRMA_W_KOLEJCE)

    def test_add_manual(self):
        created = self.store.add_manual("Firma-X.pl/kontakt?utm_source=a", "Firma X")
        self.assertTrue(created["nowa"])
        self.assertEqual(created["domena"], "firma-x.pl")
        again = self.store.add_manual("https://www.firma-x.pl")
        self.assertEqual((again["id"], again["nowa"]), (created["id"], False))
        self.assertEqual(self.firma(created["id"])["zrodlo"], "reczna")
        for bad, code in (("", "brak_www"), ("not a url", "zly_url"), ("https://twitter.com/x", "platforma"),
                          ("http://127.0.0.1", "zly_url")):
            with self.assertRaises(InvalidFirma) as ctx:
                self.store.add_manual(bad)
            self.assertEqual(ctx.exception.code, code, bad)

    def test_find_firma_by_id_and_domain(self):
        firma_id = self.add_firms(1)[0]
        self.assertEqual(self.store.find_firma(str(firma_id))["id"], firma_id)
        self.assertEqual(self.store.find_firma("https://www.FIRMA1.example.com/x")["id"], firma_id)
        self.assertIsNone(self.store.find_firma("nie-ma.pl"))
        self.assertIsNone(self.store.find_firma("999"))


class RunTests(StoreCase):
    def test_only_one_active_run(self):
        first = self.store.start_run({"a": 1}, pid=123, log_path="logs/x.log")
        with self.assertRaises(RunActive):
            self.store.start_run({})
        self.assertEqual(self.store.active_run()["id"], first)
        self.assertEqual(self.store.active_run()["parametry"]["a"], 1)
        self.assertIn("host", self.store.active_run()["parametry"])
        self.store.finish_run(first, st.RUN_ZAKONCZONY, {"strony": 3})
        self.assertIsNone(self.store.active_run())
        done = self.store.get_run(first)
        self.assertEqual((done["status"], done["aktywny"], done["statystyki"]), (st.RUN_ZAKONCZONY, None, {"strony": 3}))
        second = self.store.start_run({})
        self.assertGreater(second, first)
        self.assertEqual(self.store.latest_run()["id"], second)

    def test_stop_request_is_seen_by_heartbeat_and_attach_updates_pid(self):
        run_id = self.store.start_run({})
        self.assertTrue(self.store.attach_run(run_id, pid=4242, log_path="logs/a.log"))
        run = self.store.get_run(run_id)
        self.assertEqual((run["status"], run["pid"], run["log_path"]), (st.RUN_DZIALA, 4242, "logs/a.log"))
        self.assertFalse(self.store.heartbeat(run_id, {"x": 1}))
        self.assertEqual(self.store.get_run(run_id)["statystyki"], {"x": 1})
        self.assertTrue(self.store.request_stop())
        self.assertTrue(self.store.request_stop())  # drugie kliknięcie: nadal jest co zatrzymywać
        self.assertTrue(self.store.heartbeat(run_id))
        self.store.finish_run(run_id, st.RUN_ZATRZYMANY)
        self.assertFalse(self.store.request_stop())
        self.assertTrue(self.store.heartbeat(run_id))  # nieaktywny przebieg = sygnał zakończenia
        self.assertFalse(self.store.attach_run(run_id, pid=1))

    def test_recover_dead_runs_requeues_leased_firms(self):
        a, b = self.add_firms(2)
        run_id = self.store.start_run({}, pid=999_999)
        self.store.attach_run(run_id, pid=999_999)
        self.store.enqueue()
        claimed = self.store.claim_next_firma(run_id)
        self.assertEqual(claimed["id"], a)
        self.clock.advance(seconds=30)
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: True), [])  # świeży i żyje
        self.clock.advance(seconds=120)
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: True), [])  # nieświeży, ale proces żyje
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: False), [run_id])
        self.assertEqual(self.firma(a)["skan_status"], st.FIRMA_W_KOLEJCE)
        self.assertEqual(self.firma(b)["skan_status"], st.FIRMA_W_KOLEJCE)
        self.assertIn("przerwany", self.firma(a)["ostatni_blad"])
        run = self.store.get_run(run_id)
        self.assertEqual((run["status"], run["aktywny"]), (st.RUN_PRZERWANY, None))
        self.assertIsNotNone(self.store.start_run({}))  # slot zwolniony

    def test_a_dead_process_is_enough_no_need_to_wait_for_the_heartbeat(self):
        run_id = self.store.start_run({}, pid=424242)
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: False), [run_id])  # heartbeat sprzed 0 s

    def test_a_run_without_pid_is_given_time_to_start_up(self):
        run_id = self.store.start_run({})  # UI zarezerwowało slot, podproces jeszcze się nie dołączył
        self.clock.advance(seconds=30)
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: False), [])
        self.clock.advance(seconds=100)
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: False), [run_id])

    def test_pid_from_another_computer_is_not_judged_by_the_local_process_table(self):
        run_id = self.store.start_run({}, pid=424242)
        with self.db.engine.begin() as conn:
            conn.execute(sa.update(skan_przebiegi).where(skan_przebiegi.c.id == run_id).values(parametry='{"host": "inny-komputer"}'))
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: False), [])  # świeży heartbeat, inny host
        self.clock.advance(seconds=200)
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: False), [run_id])

    def test_recover_ignores_a_heartbeat_from_the_future(self):
        """Po zmianie czasu (jesień) heartbeat bywa „z przyszłości" — to nie znaczy, że przebieg jest martwy."""
        run_id = self.store.start_run({}, pid=1)
        self.clock.advance(hours=-1)
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: True), [])
        self.assertEqual(self.store.active_run()["id"], run_id)

    def test_recover_hard_stale_even_if_pid_looks_alive_and_pid_none(self):
        run_id = self.store.start_run({}, pid=7)
        self.clock.advance(hours=1, minutes=5)  # pozorna godzina po zmianie czasu NIE uśmierca żyjącego przebiegu
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: True), [])
        self.clock.advance(hours=3)
        self.assertEqual(self.store.recover_dead_runs(is_alive=lambda pid: True), [run_id])
        second = self.store.start_run({})  # bez pid
        self.clock.advance(seconds=200)
        self.assertEqual(self.store.recover_dead_runs(), [second])

    def test_finish_run_requeues_firms_it_left_leased(self):
        a, b = self.add_firms(2)
        run_id = self.store.start_run({})
        self.store.enqueue()
        self.store.claim_next_firma(run_id)
        self.store.claim_next_firma(run_id)
        self.store.release_firma(b, run_id, st.FIRMA_ZAKONCZONA)
        self.store.finish_run(run_id, st.RUN_PRZERWANY)
        self.assertEqual(self.firma(a)["skan_status"], st.FIRMA_W_KOLEJCE)
        self.assertEqual(self.firma(b)["skan_status"], st.FIRMA_ZAKONCZONA)  # zamknięta firma nietknięta

    def test_recover_with_no_active_runs_is_a_noop(self):
        self.assertEqual(self.store.recover_dead_runs(), [])


class QueueAndLeaseTests(StoreCase):
    def test_enqueue_defaults_and_explicit_ids(self):
        a, b, c, d = self.add_firms(4)
        with self.db.engine.begin() as conn:
            conn.execute(sa.update(firmy).where(firmy.c.id == b).values(skan_status=st.FIRMA_ZAKONCZONA))
            conn.execute(sa.update(firmy).where(firmy.c.id == c).values(skan_status=st.FIRMA_BLAD))
            conn.execute(sa.update(firmy).where(firmy.c.id == d).values(skan_status=st.FIRMA_SKANOWANIE))
        self.assertEqual(self.store.enqueue(), 2)  # nieskanowana + blad
        self.assertEqual({self.firma(i)["skan_status"] for i in (a, c)}, {st.FIRMA_W_KOLEJCE})
        self.assertEqual(self.firma(b)["skan_status"], st.FIRMA_ZAKONCZONA)
        self.assertEqual(self.store.enqueue([b, d]), 1)  # skanowanej firmy nigdy nie ruszamy
        self.assertEqual(self.firma(d)["skan_status"], st.FIRMA_SKANOWANIE)
        self.assertEqual(self.store.enqueue([]), 0)

    def test_requeue_only_touches_retryable_statuses(self):
        a, b, c = self.add_firms(3)
        with self.db.engine.begin() as conn:
            conn.execute(sa.update(firmy).where(firmy.c.id == a).values(skan_status=st.FIRMA_ZABLOKOWANA, skan_przyczyna="bot_wall"))
            conn.execute(sa.update(firmy).where(firmy.c.id == b).values(skan_status=st.FIRMA_SKANOWANIE))
        self.assertEqual(self.store.requeue([a, b, c]), 1)
        self.assertEqual((self.firma(a)["skan_status"], self.firma(a)["skan_przyczyna"]), (st.FIRMA_W_KOLEJCE, None))
        self.assertEqual(self.firma(b)["skan_status"], st.FIRMA_SKANOWANIE)
        self.assertEqual(self.firma(c)["skan_status"], st.FIRMA_NIESKANOWANA)

    def test_claim_order_status_and_run_token(self):
        ids = self.add_firms(3)
        run_id = self.store.start_run({})
        self.assertIsNone(self.store.claim_next_firma(run_id))  # nic w kolejce
        self.store.enqueue()
        first = self.store.claim_next_firma(run_id)
        self.assertEqual(first["id"], ids[0])
        self.assertEqual((first["skan_status"], first["przebieg_id"]), (st.FIRMA_SKANOWANIE, run_id))
        self.assertEqual(self.store.claim_next_firma(run_id)["id"], ids[1])
        self.assertEqual(self.store.claim_next_firma(run_id)["id"], ids[2])
        self.assertIsNone(self.store.claim_next_firma(run_id))

    def test_claim_respects_only_ids_and_retry_not_before(self):
        a, b, c = self.add_firms(3)
        run_id = self.store.start_run({})
        self.store.enqueue()
        self.assertEqual(self.store.claim_next_firma(run_id, only_ids=[c])["id"], c)
        with self.db.engine.begin() as conn:
            conn.execute(sa.update(firmy).where(firmy.c.id == a).values(nastepna_proba_po=self.clock.now + timedelta(hours=1)))
        self.assertEqual(self.store.claim_next_firma(run_id)["id"], b)  # a ma jeszcze odpoczywać
        self.assertIsNone(self.store.claim_next_firma(run_id))
        self.clock.advance(hours=2)
        self.assertEqual(self.store.claim_next_firma(run_id)["id"], a)

    def test_release_is_fenced_by_run_token(self):
        firma_id = self.add_firms(1)[0]
        firm, run_id = self.claimed()
        self.assertFalse(self.store.release_firma(firma_id, run_id + 1, st.FIRMA_ZAKONCZONA))
        self.assertEqual(self.firma(firma_id)["skan_status"], st.FIRMA_SKANOWANIE)
        self.assertTrue(self.store.release_firma(firma_id, run_id, st.FIRMA_ZABLOKOWANA, "bot_wall", "x" * 900,
                                                 domena_docelowa="inna.pl"))
        done = self.firma(firma_id)
        self.assertEqual((done["skan_status"], done["skan_przyczyna"], done["domena_docelowa"]),
                         (st.FIRMA_ZABLOKOWANA, "bot_wall", "inna.pl"))
        self.assertEqual(len(done["ostatni_blad"]), 500)
        self.assertIsNotNone(done["skan_end_at"])
        self.assertFalse(self.store.release_firma(firma_id, run_id, st.FIRMA_ZAKONCZONA))  # już nie dzierżawiona

    def test_release_back_to_queue_clears_end_time(self):
        firma_id = self.add_firms(1)[0]
        _, run_id = self.claimed()
        self.assertTrue(self.store.release_firma(firma_id, run_id, st.FIRMA_W_KOLEJCE, "stop_uzytkownika"))
        self.assertIsNone(self.firma(firma_id)["skan_end_at"])

    def test_save_robots_is_fenced_and_stores_text(self):
        firma_id = self.add_firms(1)[0]
        _, run_id = self.claimed()
        self.store.save_robots(firma_id, run_id, "ok", "User-agent: *\nDisallow: /x\x00", 2.5)
        stored = self.firma(firma_id)
        self.assertEqual((stored["robots_status"], stored["robots_txt"], stored["crawl_delay"]), ("ok", "User-agent: *\nDisallow: /x", 2.5))
        self.assertIsNotNone(stored["robots_at"])
        with self.assertRaises(LeaseLost):
            self.store.save_robots(firma_id, run_id + 5, "ok", "", None)


class ThreadedClaimTests(StoreCase):
    file_db = True

    def test_every_firm_is_claimed_exactly_once_across_threads(self):
        self.add_firms(60)
        run_id = self.store.start_run({})
        self.store.enqueue()
        got, errors, lock = [], [], threading.Lock()

        def worker():
            try:
                while True:
                    firm = self.store.claim_next_firma(run_id)
                    if firm is None:
                        return
                    with lock:
                        got.append(firm["id"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(got), sorted(set(got)))
        self.assertEqual(len(got), 60)

    def test_two_store_instances_on_one_file_do_not_double_claim(self):
        """Dwa procesy (UI i CLI) to dwie instancje Store z osobnymi blokadami — dzierżawa musi trzymać na poziomie bazy."""
        self.add_firms(30)
        other = Store(make_db(self.clock, url=str(self.db.engine.url)))
        self.addCleanup(other.engine.dispose)
        run_id = self.store.start_run({})
        self.store.enqueue()
        got, lock = [], threading.Lock()

        def worker(store):
            while True:
                firm = store.claim_next_firma(run_id)
                if firm is None:
                    return
                with lock:
                    got.append(firm["id"])

        threads = [threading.Thread(target=worker, args=(s,)) for s in (self.store, other, self.store, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(len(got), 30)
        self.assertEqual(len(set(got)), 30)


class SaveResultTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.firma_id = self.add_firms(1)[0]
        _, self.run_id = self.claimed()

    def save(self, rec):
        return self.store.save_result(self.firma_id, self.run_id, rec)

    def rows(self, table, *where):
        with self.db.engine.connect() as conn:
            return [dict(r) for r in conn.execute(sa.select(table).where(*where)).mappings()]

    def test_insert_new_page_with_content_links_contacts_and_counters(self):
        rec = page("/", links=[link("/kontakt", 1, 20), link("/o-nas", 1, 30)],
                   contacts=[Contact("email", "biuro@firma.pl", "mailto", True, "ogolny"),
                             Contact("telefon", "+48221234567", "tel")],
                   html_gz=compress_html(b"<html>x</html>"), description="Opis", content_hash="h" * 40)
        page_id = self.save(rec)
        stored = self.rows(strony, strony.c.id == page_id)[0]
        self.assertEqual((stored["status"], stored["http_status"], stored["tytul"], stored["opis"], stored["glebokosc"]),
                         ("pobrano", 200, "Tytuł", "Opis", 0))
        self.assertIsNotNone(stored["pobrano_at"])
        content = self.rows(strony_tresc)[0]
        self.assertEqual((content["strona_id"], content["tekst"]), (page_id, "treść"))
        self.assertEqual(content["html_gz"][:2], b"\x1f\x8b")
        queued = self.rows(strony, strony.c.status == "oczekuje")
        self.assertEqual(sorted(r["url"] for r in queued), ["https://a.pl/kontakt", "https://a.pl/o-nas"])
        self.assertTrue(all(r["rodzic_id"] == page_id for r in queued))
        by_type = {r["typ"]: r for r in self.rows(kontakty)}
        self.assertEqual((by_type["email"]["w_domenie"], by_type["email"]["rola"], by_type["email"]["zrodlo_url"]),
                         (True, "ogolny", "https://a.pl/"))
        firm = self.firma(self.firma_id)
        self.assertEqual((firm["stron_pobranych"], firm["bajty_pobranych"], firm["glebokosc_max"]), (1, 1000, 0))

    def test_existing_queued_page_is_updated_not_duplicated(self):
        self.store.add_discovered(self.firma_id, self.run_id, [link("/kontakt", 2, 20)])
        before = self.rows(strony)[0]
        self.assertEqual(before["status"], "oczekuje")
        page_id = self.save(page("/kontakt", depth=2, priority=20, source="link", title="Kontakt"))
        after = self.rows(strony)
        self.assertEqual(len(after), 1)
        self.assertEqual((after[0]["id"], after[0]["id"] == before["id"], after[0]["status"], after[0]["tytul"]),
                         (page_id, True, "pobrano", "Kontakt"))
        self.assertEqual(after[0]["priorytet"], 20)
        self.assertEqual(self.firma(self.firma_id)["glebokosc_max"], 2)

    def test_links_are_deduplicated_and_non_ascii_urls_skipped(self):
        bad = LinkRecord(url="https://a.pl/zażółć", url_hash="b" * 40, depth=1, priority=1)
        self.save(page("/", links=[link("/x"), link("/x"), bad]))
        self.save(page("/y", depth=1, links=[link("/x"), link("/z")]))
        urls = sorted(r["url"] for r in self.rows(strony))
        self.assertEqual(urls, ["https://a.pl/", "https://a.pl/x", "https://a.pl/y", "https://a.pl/z"])

    def test_contact_seen_on_many_pages_counts_pages(self):
        for path in ("/", "/a", "/b"):
            self.save(page(path, contacts=[Contact("email", "biuro@firma.pl", "tekst", True, "ogolny")]))
        row = self.rows(kontakty)[0]
        self.assertEqual(row["liczba_stron"], 3)
        self.assertEqual(row["zrodlo_url"], "https://a.pl/")  # pierwsze źródło zostaje

    def test_resaving_a_page_replaces_its_content(self):
        page_id = self.save(page("/", text="stary"))
        self.assertEqual(self.save(page("/", text="nowy")), page_id)
        self.assertEqual([r["tekst"] for r in self.rows(strony_tresc)], ["nowy"])

    def test_pages_without_network_cost_do_not_touch_counters(self):
        self.save(page("/x", status=st.STRONA_POMINIETO_ROBOTS, counts=False, text=None, bytes=0, http_status=None))
        firm = self.firma(self.firma_id)
        self.assertEqual((firm["stron_pobranych"], firm["bajty_pobranych"], firm["glebokosc_max"]), (0, 0, None))
        stored = self.rows(strony)[0]
        self.assertIsNone(stored["pobrano_at"])
        self.assertEqual(self.rows(strony_tresc), [])

    def test_glebokosc_max_only_grows(self):
        self.save(page("/a", depth=4))
        self.save(page("/b", depth=2))
        self.assertEqual(self.firma(self.firma_id)["glebokosc_max"], 4)

    def test_hostile_text_is_sanitized(self):
        self.save(page("/", title="T\x00ytuł\ud800", text="a\x00b", error="e\x01rr"))
        stored = self.rows(strony)[0]
        self.assertEqual((stored["tytul"], stored["blad"]), ("Tytuł", "err"))
        self.assertEqual(self.rows(strony_tresc)[0]["tekst"], "ab")

    def test_non_ascii_page_url_is_refused(self):
        rec = page("/")
        rec.url = "https://a.pl/zażółć"
        with self.assertRaises(StoreError):
            self.save(rec)

    def test_failure_in_the_middle_rolls_back_the_whole_page(self):
        class Broken:
            typ, wartosc, zrodlo_typ, w_domenie, rola = None, "x@y.pl", "tekst", False, None

        before = self.firma(self.firma_id)
        with self.assertRaises(sa_exc.IntegrityError):
            self.save(page("/", links=[link("/x")], contacts=[Broken()]))
        self.assertEqual(self.rows(strony), [])
        self.assertEqual(self.rows(strony_tresc), [])
        self.assertEqual(self.rows(kontakty), [])
        after = self.firma(self.firma_id)
        self.assertEqual((after["stron_pobranych"], after["bajty_pobranych"]), (before["stron_pobranych"], before["bajty_pobranych"]))
        self.save(page("/"))  # i połączenie nadal działa
        self.assertEqual(len(self.rows(strony)), 1)

    def test_lease_lost_writes_nothing(self):
        with self.assertRaises(LeaseLost):
            self.store.save_result(self.firma_id, self.run_id + 1, page("/", links=[link("/x")]))
        self.assertEqual(self.rows(strony), [])
        self.store.release_firma(self.firma_id, self.run_id, st.FIRMA_ZAKONCZONA)
        with self.assertRaises(LeaseLost):
            self.save(page("/"))
        with self.assertRaises(LeaseLost):
            self.store.add_discovered(self.firma_id, self.run_id, [link("/x")])
        self.assertEqual(self.rows(strony), [])

    def test_a_new_run_takes_over_and_the_old_one_is_fenced_out(self):
        """Scenariusz „zombie": stary przebieg uśpiony, odzyskanie, nowy przebieg zajmuje firmę."""
        self.save(page("/"))
        self.store.finish_run(self.run_id, st.RUN_PRZERWANY)
        self.store.requeue([])  # nic
        with self.db.engine.begin() as conn:  # symulacja recover_dead_runs
            conn.execute(sa.update(firmy).where(firmy.c.id == self.firma_id).values(skan_status=st.FIRMA_W_KOLEJCE))
        new_run = self.store.start_run({})
        self.assertEqual(self.store.claim_next_firma(new_run)["id"], self.firma_id)
        with self.assertRaises(LeaseLost):
            self.save(page("/stary"))  # zombie z poprzedniego przebiegu
        self.assertEqual(self.store.save_result(self.firma_id, new_run, page("/nowy")) > 0, True)


class FrontierTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.firma_id = self.add_firms(1)[0]
        _, self.run_id = self.claimed()

    def test_frontier_is_ordered_limited_and_only_pending(self):
        self.store.add_discovered(self.firma_id, self.run_id,
                                  [link("/c", 1, 300), link("/a", 1, 100), link("/b", 2, 200), link("/d", 3, 400)])
        self.store.save_result(self.firma_id, self.run_id, page("/b", depth=2, priority=200, source="link"))
        frontier = self.store.load_frontier(self.firma_id, 2)
        self.assertEqual([f["url"] for f in frontier], ["https://a.pl/a", "https://a.pl/c"])
        self.assertEqual(self.store.pending_count(self.firma_id), 3)
        self.assertEqual(set(frontier[0]), {"id", "url", "url_hash", "glebokosc", "priorytet", "zrodlo"})

    def test_known_hashes_and_content_hashes(self):
        self.store.add_discovered(self.firma_id, self.run_id, [link("/a")])
        self.store.save_result(self.firma_id, self.run_id, page("/", content_hash="c" * 40))
        self.store.save_result(self.firma_id, self.run_id, page("/dup", status=st.STRONA_DUPLIKAT, content_hash="c" * 40))
        self.store.save_result(self.firma_id, self.run_id, page("/x", content_hash="d" * 40))
        self.assertEqual(len(self.store.known_url_hashes(self.firma_id)), 4)
        hashes = self.store.content_hashes(self.firma_id)
        self.assertEqual(set(hashes), {"c" * 40, "d" * 40})  # duplikat nie zasłania oryginału
        self.assertIn(url_hash("https://a.pl/a"), self.store.known_url_hashes(self.firma_id))

    def test_other_firms_are_isolated(self):
        other = self.store.add_manual("https://druga.example.com/")["id"]
        self.store.enqueue([other])
        self.store.claim_next_firma(self.run_id)
        self.store.add_discovered(other, self.run_id, [link("/o")])
        self.assertEqual(self.store.pending_count(self.firma_id), 0)
        self.assertEqual(self.store.pending_count(other), 1)


class AdminTests(StoreCase):
    def scanned(self, domain_n: int = 1):
        firma_id = self.add_firms(domain_n)[-1]
        firm, run_id = self.claimed(firma_id)
        self.store.save_result(firma_id, run_id, page("/", contacts=[Contact("email", "a@b.pl", "tekst")], links=[link("/x")]))
        return firma_id, run_id

    def test_delete_data_resets_the_firm(self):
        firma_id, run_id = self.scanned()
        with self.assertRaises(FirmaBusy):
            self.store.delete_firma_data(firma_id)
        self.store.release_firma(firma_id, run_id, st.FIRMA_ZAKONCZONA)
        self.store.delete_firma_data(firma_id)
        for table in (strony, strony_tresc, kontakty):
            self.assertEqual(self.count(table), 0)
        firm = self.firma(firma_id)
        self.assertEqual((firm["skan_status"], firm["stron_pobranych"], firm["glebokosc_max"], firm["przebieg_id"]),
                         (st.FIRMA_NIESKANOWANA, 0, None, None))
        with self.assertRaises(NotFound):
            self.store.delete_firma_data(9999)

    def test_delete_data_leaves_other_firms_alone(self):
        a, run_id = self.scanned()
        self.store.release_firma(a, run_id, st.FIRMA_ZAKONCZONA)
        b = self.store.add_manual("https://druga.example.com/")["id"]
        self.store.enqueue([b])
        self.store.claim_next_firma(run_id)
        self.store.save_result(b, run_id, PageRecord(url="https://druga.example.com/", url_hash=url_hash("https://druga.example.com/"),
                                                     depth=0, priority=0, source="start", status="pobrano", text="inna"))
        self.store.delete_firma_data(a)
        self.assertEqual(self.count(strony), 1)
        self.assertEqual([r.tekst for r in self.db.engine.connect().execute(sa.select(strony_tresc))], ["inna"])

    def test_use_target_domain(self):
        firma_id, run_id = self.scanned()
        self.store.release_firma(firma_id, run_id, st.FIRMA_POMINIETA, "przekierowanie", domena_docelowa="Nowa-Domena.PL")
        self.assertEqual(self.store.use_target_domain(firma_id), {"id": firma_id, "domena": "nowa-domena.pl"})
        firm = self.firma(firma_id)
        self.assertEqual((firm["domena"], firm["website"], firm["skan_status"], firm["domena_docelowa"]),
                         ("nowa-domena.pl", "https://nowa-domena.pl/", st.FIRMA_W_KOLEJCE, None))
        self.assertEqual(self.count(strony), 0)

    def test_use_target_domain_errors(self):
        a, run_id = self.scanned()
        with self.assertRaises(FirmaBusy):
            self.store.use_target_domain(a)
        self.store.release_firma(a, run_id, st.FIRMA_POMINIETA, "przekierowanie")
        with self.assertRaises(InvalidFirma):
            self.store.use_target_domain(a)  # brak domeny docelowej
        other = self.store.add_manual("https://zajeta.example.com/")["id"]
        with self.db.engine.begin() as conn:
            conn.execute(sa.update(firmy).where(firmy.c.id == a).values(domena_docelowa="zajeta.example.com"))
        with self.assertRaises(DomainTaken):
            self.store.use_target_domain(a)
        self.assertEqual(self.firma(other)["domena"], "zajeta.example.com")
        with self.assertRaises(NotFound):
            self.store.use_target_domain(9999)

    def test_purge_older_than_and_purge_html(self):
        a, run_a = self.scanned()
        self.store.release_firma(a, run_a, st.FIRMA_ZAKONCZONA)
        self.store.finish_run(run_a, st.RUN_ZAKONCZONY)
        b = self.add_firms(2)[-1]
        run_b = self.store.start_run({})
        self.store.enqueue([b])
        self.store.claim_next_firma(run_b)
        self.store.save_result(b, run_b, PageRecord(url="https://druga.example.com/", url_hash=url_hash("https://druga.example.com/"),
                                                    depth=0, priority=0, source="start", status="pobrano", text="t",
                                                    html_gz=compress_html(b"<html></html>")))
        self.store.release_firma(b, run_b, st.FIRMA_ZAKONCZONA)
        self.clock.advance(days=40)
        c = self.store.add_manual("https://trzecia.example.com/")["id"]
        self.assertEqual(self.store.purge_html(30), 1)
        self.assertEqual(self.count(strony), 3)
        self.assertEqual([r.html_gz for r in self.db.engine.connect().execute(sa.select(strony_tresc)) if r.html_gz], [])
        self.assertEqual(self.store.purge_older_than(30), 2)
        self.assertEqual(self.count(strony), 0)
        self.assertEqual(self.firma(c)["skan_status"], st.FIRMA_NIESKANOWANA)
        self.assertEqual(self.store.purge_older_than(30), 0)

    def test_list_overview_and_detail(self):
        a, run_id = self.scanned()
        b = self.add_firms(2)[-1]
        self.store.release_firma(a, run_id, st.FIRMA_ZAKONCZONA)
        rows, total = self.store.list_firmy()
        self.assertEqual(total, 2)
        first = rows[0]
        self.assertEqual((first["id"], first["emaile"], first["telefony"], first["skan_status"]), (a, 1, 0, st.FIRMA_ZAKONCZONA))
        self.assertRegex(first["skan_end_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertEqual([r["id"] for r in self.store.list_firmy(status=st.FIRMA_NIESKANOWANA)[0]], [b])
        self.assertEqual(self.store.list_firmy(q="FIRMA2")[1], 1)
        self.assertEqual(self.store.list_firmy(q="%")[1], 0)  # % to zwykły znak, nie wildcard
        self.assertEqual(self.store.list_firmy(q="_")[1], 0)
        self.assertEqual(len(self.store.list_firmy(limit=1)[0]), 1)
        overview = self.store.status_overview()
        self.assertEqual((overview["firmy"], overview["stron"], overview["kontakty"]), (2, 1, {"email": 1}))
        self.assertEqual(overview["firmy_wg_statusu"], {st.FIRMA_ZAKONCZONA: 1, st.FIRMA_NIESKANOWANA: 1})
        detail = self.store.firma_detail(a)
        self.assertEqual((detail["strony_razem"], len(detail["strony"]), len(detail["kontakty"])), (2, 2, 1))
        self.assertNotIn("tekst", detail["strony"][0])
        self.assertIsNone(self.store.firma_detail(9999))

    def test_detail_truncates_robots_for_display(self):
        firma_id = self.add_firms(1)[0]
        _, run_id = self.claimed(firma_id)
        self.store.save_robots(firma_id, run_id, "ok", "x" * 10_000, None)
        detail = self.store.firma_detail(firma_id)
        self.assertEqual(len(detail["firma"]["robots_txt"]), 4096)
        self.assertTrue(detail["firma"]["robots_obcięty"])


class RetryTests(StoreCase):
    def op_error(self, code: int):
        class Orig(Exception):
            pass

        return sa_exc.OperationalError("UPDATE x", {}, Orig(code, "mysql said no"))

    def test_deadlock_is_retried_in_a_fresh_transaction(self):
        calls = []

        def flaky(conn):
            calls.append(1)
            if len(calls) < 3:
                raise self.op_error(1213)
            return "ok"

        import lib.scraper.store as module
        original = module.time.sleep
        module.time.sleep = lambda s: None
        try:
            self.assertEqual(self.store._run(flaky), "ok")
        finally:
            module.time.sleep = original
        self.assertEqual(len(calls), 3)

    def test_non_retryable_and_exhausted_errors_propagate(self):
        calls = []

        def fatal(conn):
            calls.append(1)
            raise self.op_error(1064)

        with self.assertRaises(sa_exc.OperationalError):
            self.store._run(fatal)
        self.assertEqual(len(calls), 1)

        calls.clear()

        def always_deadlock(conn):
            calls.append(1)
            raise self.op_error(1205)

        import lib.scraper.store as module
        original = module.time.sleep
        module.time.sleep = lambda s: None
        try:
            with self.assertRaises(sa_exc.OperationalError):
                self.store._run(always_deadlock, retries=2)
        finally:
            module.time.sleep = original
        self.assertEqual(len(calls), 3)

    def test_lease_lost_is_never_retried(self):
        calls = []

        def lost(conn):
            calls.append(1)
            raise LeaseLost("x")

        with self.assertRaises(LeaseLost):
            self.store._run(lost)
        self.assertEqual(len(calls), 1)


class MysqlStatementTests(unittest.TestCase):
    """Składnia specyficzna dla MySQL (sqlite jej nie wykonuje) — kompilujemy ją bez serwera."""

    class RecordingConn:
        def __init__(self):
            self.sql = []

        def execute(self, stmt, *args, **kwargs):
            from sqlalchemy.dialects import mysql
            self.sql.append(str(stmt.compile(dialect=mysql.dialect())))
            return types.SimpleNamespace(first=lambda: object(), rowcount=1, scalar=lambda: None)

    def store(self):
        stub = types.SimpleNamespace(
            backend="mysql", engine=types.SimpleNamespace(url=sa.engine.make_url("mysql+pymysql://u:p@h/db")))
        return Store(stub)

    def test_insert_noop_uses_on_duplicate_key_update_not_insert_ignore(self):
        conn = self.RecordingConn()
        self.store()._insert_noop(conn, strony, [{"firma_id": 1, "url": "https://a.pl/", "url_hash": "h" * 40,
                                                 "glebokosc": 0, "priorytet": 1, "zrodlo": "link", "status": "oczekuje"}], "id")
        self.assertIn("ON DUPLICATE KEY UPDATE id = strony.id", conn.sql[0])
        self.assertNotIn("IGNORE", conn.sql[0])
        conn = self.RecordingConn()
        self.store()._insert_noop(conn, firmy_maile, [{"maile_id": "eventy-001", "firma_id": 1}], "maile_id")
        self.assertIn("ON DUPLICATE KEY UPDATE maile_id = firmy_maile.maile_id", conn.sql[0])

    def test_contact_upsert_increments_counter(self):
        conn = self.RecordingConn()
        self.store()._upsert_contact(conn, {"firma_id": 1, "typ": "email", "wartosc": "a@b.pl", "liczba_stron": 1})
        self.assertIn("ON DUPLICATE KEY UPDATE liczba_stron = (kontakty.liczba_stron + %s)", conn.sql[0])

    def test_lease_check_locks_the_row(self):
        conn = self.RecordingConn()
        Store._check_lease(conn, 1, 2)
        self.assertTrue(conn.sql[0].rstrip().endswith("FOR UPDATE"), conn.sql[0])


if __name__ == "__main__":
    unittest.main()
