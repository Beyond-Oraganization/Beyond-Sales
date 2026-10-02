from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile

from lib import dashboard
from tests.fixtures import put, sample_row
from tests.test_web import WebCase


class PanelProfileTests(WebCase):
    def setUp(self):
        super().setUp()
        put(self.database,
            sample_row(1, country="PL", city="Kraków", status="wyslano", sent_at="2026-09-29T10:00:00"),
            sample_row(2, segment="driverzy", country="UK", city="Londyn", address="1 Mall", description="Chauffeur"),
            sample_row(3))

    def edit(self, row_id, **fields):
        return self.call("POST", "/api/update-row", {"id": row_id, **fields})

    def test_page_exposes_country_column_filter_and_country_list(self):
        html = self.call("GET", "/").body.decode("utf-8")
        self.assertIn('id="filterBar"', html)  # filtr kraju jest częścią wspólnego paska filtrów (kit.js)
        self.assertIn("Spoza Polski", html)
        self.assertIn('data-sort="country" data-col="country">Kraj</th>', html)
        boot = json.loads(re.search(r"const APP = (\{.*?\});\n", html, re.S).group(1))
        codes = [item[0] for item in boot["countries"]]
        self.assertEqual(codes[0], "PL")
        self.assertIn("GB", codes)
        self.assertEqual(next(item for item in boot["countries"] if item[0] == "GB")[1], "Wielka Brytania")
        self.assertEqual(len(next(item for item in boot["countries"] if item[0] == "PL")[2]), 2)  # flaga = dwa znaki regionalne

    def test_rows_carry_the_profile_fields(self):
        rows = {r["id"]: r for r in self.data(self.call("GET", "/api/maile"))["rows"]}
        self.assertEqual((rows["eventy-001"]["country"], rows["eventy-001"]["city"]), ("PL", "Kraków"))
        self.assertEqual((rows["driverzy-002"]["country"], rows["driverzy-002"]["address"], rows["driverzy-002"]["description"]),
                         ("GB", "1 Mall", "Chauffeur"))  # UK z bazy trafia do panelu jako GB
        self.assertEqual(rows["eventy-003"]["country"], "")

    def test_country_can_be_set_by_name_or_code_and_is_stored_as_iso(self):
        for raw, code in (("Niemcy", "DE"), ("at", "AT"), ("Wielka Brytania", "GB"), ("Polska", "PL")):
            resp = self.edit("eventy-003", country=raw)
            self.assertEqual(resp.status, 200, raw)
            self.assertEqual(self.data(resp)["row"]["country"], code)

    def test_unknown_country_gets_a_clear_400_and_changes_nothing(self):
        resp = self.edit("eventy-001", country="Mordor")
        self.assertEqual(resp.status, 400)
        self.assertIn("nieznany kraj", self.data(resp)["error"])
        self.assertEqual(self.database.get("eventy-001")["country"], "PL")

    def test_city_address_description_and_clearing(self):
        row = self.data(self.edit("eventy-003", city="Gdańsk", address="ul. Długa 1, 80-001 Gdańsk", description="Szkoła żeglarska"))["row"]
        self.assertEqual((row["city"], row["address"], row["description"]), ("Gdańsk", "ul. Długa 1, 80-001 Gdańsk", "Szkoła żeglarska"))
        row = self.data(self.edit("eventy-003", country="PL", city=""))["row"]
        self.assertEqual((row["country"], row["city"], row["address"]), ("PL", "", "ul. Długa 1, 80-001 Gdańsk"))
        self.assertEqual(self.edit("eventy-003", city="x" * 121).status, 400)
        self.assertEqual(self.edit("eventy-003", city=5).status, 400)  # pola muszą być tekstem

    def test_editing_the_country_of_a_sent_row_does_not_disturb_its_send_record(self):
        row = self.data(self.edit("eventy-001", country="Czechy"))["row"]
        self.assertEqual((row["country"], row["status"], row["sent_at"]), ("CZ", "wyslano", "2026-09-29T10:00:00"))

    def test_export_endpoint_shows_the_country_by_its_polish_name_and_city(self):
        resp = self.call("GET", "/api/export", query={"format": ["csv"]})
        with resp.stream as stream:
            raw = stream.read()
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
        header = rows[0]
        self.assertEqual(header[:5], ["Firma", "Segment", "Kraj", "Miasto", "E-mail"])
        by_company = {r[0]: dict(zip(header, r)) for r in rows[1:]}
        self.assertEqual((by_company["Firma 2"]["Kraj"], by_company["Firma 2"]["Miasto"]), ("Wielka Brytania", "Londyn"))  # UK z bazy to GB

    def test_export_endpoint_can_add_the_iso_code_and_address(self):
        resp = self.call("POST", "/api/export", {"format": "csv", "ids": ["driverzy-002"],
                                                 "columns": ["company", "country", "country_name", "address"]})
        with resp.stream as stream:
            rows = list(csv.reader(io.StringIO(stream.read().decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows, [["Firma", "Kraj", "Kod kraju", "Adres"], ["Firma 2", "Wielka Brytania", "GB", "1 Mall"]])


class DashboardProfileTests(WebCase):
    def test_static_dashboard_shows_the_country_column(self):
        put(self.database, sample_row(1, country="DE", city="Berlin"))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index.html")
            dashboard.generate_dashboard(self.database.fetch_all(), path)
            with open(path, encoding="utf-8") as handle:
                html = handle.read()
        self.assertIn("<th>Kraj</th>", html)
        self.assertIn('"country": "DE"', html)
        self.assertIn('"DE": ["Niemcy"', html)
        self.assertIn("td.colSpan = 8", html)
        self.assertNotIn("/*__COUNTRIES__*/", html)
