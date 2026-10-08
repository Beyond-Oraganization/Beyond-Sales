"""Dostęp do poczty przychodzącej: fabryka sesji IMAP z konfiguracji (z blokadą bazy demo) i próba połączenia.

Wzór: `services.sending` dla SMTP. Fabryka jest wstrzykiwana (`AppContext.imap_factory`), więc testy używają
`tests.fake_imap.FakeImap`, a baza demo (`PERNO_DB_URL`) nie dotyka prawdziwej poczty bez jawnego odblokowania.
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional

from perno import settings as cfg
from perno.domain import imap_folders
from perno.mailer.imap_client import ImapClient, ImapError
from perno.notify import scrub
from perno.settings import ConfigError, Environment, ImapSettings

ImapFactory = Callable[[ImapSettings], object]  # ImapSettings → menedżer kontekstu zwracający ImapSession


def _client(settings: ImapSettings) -> ImapClient:
    return ImapClient(settings.host, settings.port, settings.user, settings.password, settings.use_ssl)


def real_factory(env: Optional[Environment] = None) -> ImapFactory:
    """Fabryka prawdziwych połączeń; odmawia, gdy baza jest testowa (PERNO_DB_URL) bez jawnego odblokowania."""
    if not (env if env is not None else Environment.from_environ()).real_imap_allowed:
        raise ConfigError(
            "Ustawiono PERNO_DB_URL (baza testowa/demo), więc pobieranie prawdziwej poczty jest zablokowane. "
            "Jeśli naprawdę chcesz pobierać, ustaw też PERNO_ALLOW_REAL_IMAP=1."
        )
    return _client


def _secrets(config: Mapping[str, str]) -> list:
    return [value for key, value in config.items() if value and key.endswith("_PASSWORD")]


def check_mailbox(settings: ImapSettings, factory: ImapFactory, secrets: list) -> dict:
    """Łączy się, loguje i sprawdza foldery jednej skrzynki; nic nie zmienia na serwerze. Wynik zawsze jest słownikiem
    (błąd połączenia to wynik próby, nie wyjątek), a hasła nie trafiają do komunikatu."""
    base = {"slot": settings.slot, "address": settings.address}
    try:
        with factory(settings) as imap:  # type: ignore[attr-defined]
            folders = imap.list_folders()
            chosen = imap_folders.default_folders(folders)
            exists = imap.select(chosen[0]).exists if chosen else 0
    except ImapError as exc:
        return {**base, "ok": False, "kind": exc.kind, "message": scrub(str(exc), secrets)}
    except Exception as exc:  # noqa: BLE001 — każdy błąd próby połączenia (sieć, TLS, atrapa) to wynik próby, nie awaria programu
        return {**base, "ok": False, "kind": "network", "message": scrub(f"{type(exc).__name__}: {str(exc)[:200]}", secrets)}
    where = ", ".join(chosen) if chosen else "brak rozpoznanych folderów"
    return {**base, "ok": True, "kind": "", "message": f"Zalogowano jako {settings.user}: {exists} wiadomości w „{chosen[0] if chosen else '-'}”. "
                                                          f"Przeszukiwane foldery: {where}."}


def test_imap(config: Mapping[str, str], factory_for: Callable[[], ImapFactory]) -> dict:
    """Próba połączenia z każdą skrzynką, która ma serwer IMAP: `{"ok", "message", "results"}` (jak `test_smtp`)."""
    try:
        slots = cfg.imap_slots(config)
        if not slots:
            return {"ok": False, "message": "Żadna skrzynka nie ma ustawionego serwera IMAP.", "results": []}
        factory = factory_for()
    except ConfigError as exc:
        return {"ok": False, "message": str(exc), "results": []}
    secrets = _secrets(config)
    results = [check_mailbox(slot, factory, secrets) for slot in slots]
    message = results[0]["message"] if len(results) == 1 else "; ".join(
        f"{r['address']}: {'OK' if r['ok'] else r['message']}" for r in results)
    return {"ok": all(r["ok"] for r in results), "message": message, "results": results}
