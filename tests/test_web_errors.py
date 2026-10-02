"""Zamiana wyjątków na odpowiedzi HTTP: jedna tabela dla całej aplikacji."""

from __future__ import annotations

import json
import unittest

from perno.db import DbError, EmptyDatabase, InvalidValue, RowLocked, RowNotFound
from perno.scraper.settings import SettingsError
from perno.scraper.store import DomainTaken, FirmaBusy, InvalidFirma, NotFound, RunActive
from perno.services.bulk import AckRequired, Plan
from perno.services.sending import DailyCapReached, NotSendable
from perno.settings import ConfigError
from perno.web import errors
from perno.web.http import HttpError


def body(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


class StatusTableTests(unittest.TestCase):
    def check(self, exc, status):
        resp = errors.to_response(exc)
        self.assertIsNotNone(resp, type(exc).__name__)
        self.assertEqual(resp.status, status, type(exc).__name__)
        self.assertEqual(body(resp)["error"], str(exc))

    def test_missing_things_are_404(self):
        for exc in (RowNotFound("brak wiersza"), NotFound("brak firmy")):
            self.check(exc, 404)

    def test_conflicts_are_409(self):
        for exc in (EmptyDatabase("pusta baza"), RunActive("trwa przebieg"), FirmaBusy("firma zajęta"), DomainTaken("domena zajęta")):
            self.check(exc, 409)

    def test_bad_input_and_configuration_are_400(self):
        for exc in (InvalidValue("zła wartość"), ConfigError("brak konfiguracji"), InvalidFirma("brak_www", "zły adres"),
                    SettingsError("zła liczba"), NotSendable("nie wolno")):
            self.check(exc, 400)

    def test_exhausted_daily_cap_is_429(self):
        self.check(DailyCapReached("limit"), 429)


class SpecialCasesTests(unittest.TestCase):
    def test_http_error_keeps_its_status_message_and_extras(self):
        resp = errors.to_response(HttpError(418, "czajnik", row={"id": "x"}))
        self.assertEqual((resp.status, body(resp)), (418, {"error": "czajnik", "row": {"id": "x"}}))

    def test_locked_row_is_409_and_carries_the_row(self):
        resp = errors.to_response(RowLocked("wysyłany", {"id": "eventy-001"}))
        self.assertEqual((resp.status, body(resp)["row"]), (409, {"id": "eventy-001"}))

    def test_unconfirmed_bulk_warnings_are_409_with_the_plan(self):
        plan = Plan(requested=3, found=3)
        resp = errors.to_response(AckRequired(plan))
        data = body(resp)
        self.assertEqual(resp.status, 409)
        self.assertEqual((data["requested"], data["needs_ack"]), (3, []))

    def test_unknown_and_generic_database_errors_are_not_mapped(self):
        self.assertIsNone(errors.to_response(RuntimeError("niespodzianka")))
        self.assertIsNone(errors.to_response(DbError("ogólny błąd bazy")))


if __name__ == "__main__":
    unittest.main()
