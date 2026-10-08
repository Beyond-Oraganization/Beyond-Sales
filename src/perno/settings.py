"""Ustawienia programu: ścieżki projektu, config.env i zmienne środowiska (bez zewnętrznych zależności).

To jedyny moduł, który czyta `config.env` i `os.environ`. Reszta programu dostaje gotowe wartości
(`Environment`, `SmtpSettings`, słownik z `ConfigFile.load()`), więc zależności są jawne, a testy nie muszą
podmieniać globalnego stanu procesu.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Mapping, Optional

from perno.domain.mailboxes import imap_prefix, slots_in_keys, smtp_prefix

# Korzeń projektu (katalog z config.env, data/, templates/): src/perno/settings.py leży trzy poziomy niżej.
# Zakłada instalację edytowalną (`uv sync`); przy zwykłej instalacji z wheela ścieżki danych trzeba by podać jawnie.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PERNO_DIR = os.path.dirname(BASE_DIR)

DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.env")
EVENTY_SEED = os.path.join(BASE_DIR, "data", "eventy.csv")
DRIVERZY_SEED = os.path.join(BASE_DIR, "data", "driverzy.csv")
ICEBREAKER_TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "icebreaker.html")  # jeden szablon dla wszystkich segmentów
INDEX_HTML = os.path.join(BASE_DIR, "index.html")
LOG_DIR = os.path.join(BASE_DIR, "logs")
BACKUP_DIR = os.path.join(BASE_DIR, "backup")
LEGACY_CSV = os.path.join(BASE_DIR, "Maile.csv")

INLINE_IMAGE_SRC = os.path.join(
    PERNO_DIR, "Maile_GTM_Eventy", "Załączniki",
    "img-75b68155-913f-4961-8a6c-b04d9ac4ed6e.jpg.png",
)
INLINE_IMAGE_CACHE_BASE = os.path.join(BASE_DIR, "assets", "inline", "ux_w_lesie_screenshot")

# Zmienne środowiska (poza config.env). Służą głównie do pracy na bazie demo/testowej i do testów skrapera.
ENV_DB_URL = "PERNO_DB_URL"  # baza testowa/demo zamiast tej z config.env; blokuje prawdziwą wysyłkę SMTP
ENV_ALLOW_REAL_SMTP = "PERNO_ALLOW_REAL_SMTP"  # =1 pozwala wysyłać naprawdę mimo ustawionego PERNO_DB_URL
ENV_LOG_DIR = "PERNO_LOG_DIR"  # katalog logów skrapera (domyślnie logs/)
ENV_SCRAPER_ALLOW_PRIVATE = "PERNO_SKRAPER_ALLOW_PRIVATE"  # =1 wyłącza ochronę SSRF (tylko testy z lokalnym „internetem")
ENV_SCRAPER_HOSTS = "PERNO_SKRAPER_HOSTS"  # a.test=127.0.0.1,b.test=127.0.0.1 — działa tylko z ALLOW_PRIVATE
ENV_SSL_CERT_FILE = "SSL_CERT_FILE"  # własny pakiet certyfikatów CA

SMTP_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD")
SMTP_REQUIRED = ("HOST", "PORT", "USER", "PASSWORD")  # końcówki kluczy skrzynki (przedrostek: SMTP, SMTP2 …)


class ConfigError(Exception):
    """Brak lub niepełna konfiguracja (komunikat po polsku, bez haseł)."""


def _parse_host_map(raw: str) -> dict:
    pairs = (item.partition("=") for item in raw.split(",") if "=" in item)
    return {name.strip().lower(): ip.strip() for name, _, ip in pairs if name.strip() and ip.strip()}


@dataclass(frozen=True)
class Environment:
    """Migawka zmiennych środowiska programu z chwili wywołania `from_environ`."""
    db_url: str = ""
    allow_real_smtp: bool = False
    log_dir: str = ""
    ssl_cert_file: str = ""
    scraper_allow_private: bool = False
    scraper_hosts: Mapping[str, str] = field(default_factory=dict)  # nazwa hosta → IP; puste bez scraper_allow_private

    @classmethod
    def from_environ(cls, environ: Optional[Mapping[str, str]] = None) -> "Environment":
        source = os.environ if environ is None else environ
        allow_private = source.get(ENV_SCRAPER_ALLOW_PRIVATE) == "1"
        return cls(
            db_url=source.get(ENV_DB_URL) or "",
            allow_real_smtp=source.get(ENV_ALLOW_REAL_SMTP) == "1",
            log_dir=source.get(ENV_LOG_DIR) or "",
            ssl_cert_file=source.get(ENV_SSL_CERT_FILE) or "",
            scraper_allow_private=allow_private,
            scraper_hosts=_parse_host_map(source.get(ENV_SCRAPER_HOSTS, "")) if allow_private else {},
        )

    @property
    def real_smtp_allowed(self) -> bool:
        """Baza testowa/demo (PERNO_DB_URL) blokuje prawdziwą wysyłkę, chyba że świadomie ją odblokowano."""
        return not self.db_url or self.allow_real_smtp


def load_config(path: str) -> dict:
    """Czyta plik `KEY=value` (bez interpolacji i cudzysłowów); brak pliku to pusta konfiguracja."""
    config: dict[str, str] = {}
    if not os.path.exists(path):
        return config
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip()
    return config


_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_WRITE_LOCK = threading.Lock()


def _check_config_entry(key: str, value: Optional[str]) -> None:
    if not _KEY_RE.fullmatch(key):
        raise ConfigError(f"Nieprawidłowa nazwa ustawienia: {key!r}.")
    if value is not None and (value != value.strip() or any(ch in value for ch in "\r\n\x00")):
        raise ConfigError(f"Wartość ustawienia {key} nie może zaczynać się ani kończyć spacją ani zawierać znaków nowej linii.")


def _line_key(line: str, *, commented: bool) -> Optional[str]:
    """Nazwa ustawienia z linii `KEY=…` (albo `# KEY=…`, gdy `commented`); None, gdy to nie ta postać."""
    body = line.strip()
    if commented:
        if not body.startswith("#"):
            return None
        body = body[1:].strip()
    elif body.startswith("#"):
        return None
    name, sep, _ = body.partition("=")
    name = name.strip()
    return name if sep and _KEY_RE.fullmatch(name) else None


def _edit_config_lines(lines: list, changes: Mapping[str, Optional[str]]) -> list:
    """Linie pliku po zmianach: istniejące ustawienie jest podmieniane w miejscu (duplikaty znikają, bo program czyta
    ostatnie wystąpienie), zakomentowany wzór `# KEY=…` zostaje odkomentowany, a nowe klucze lądują na końcu."""
    lines = list(lines)
    for key, value in changes.items():
        active = [i for i, line in enumerate(lines) if _line_key(line, commented=False) == key]
        if value is None:
            lines = [line for i, line in enumerate(lines) if i not in active]
            continue
        new_line = f"{key}={value}"
        if active:
            lines[active[0]] = new_line
            lines = [line for i, line in enumerate(lines) if i not in active[1:]]
            continue
        commented = next((i for i, line in enumerate(lines) if _line_key(line, commented=True) == key), None)
        if commented is not None:
            lines[commented] = new_line
        else:
            lines.append(new_line)
    return lines


def update_config(path: str, changes: Mapping[str, Optional[str]]) -> list:
    """Zmienia ustawienia w pliku `KEY=value`: `None` usuwa klucz, tekst ustawia wartość. Komentarze i kolejność zostają.
    Zapis jest atomowy (plik tymczasowy + zamiana), poprzednia wersja ląduje w `<plik>.bak`, a prawa dostępu to 0600
    (plik zawiera hasła). Zwraca listę kluczy, których wartość faktycznie się zmieniła. To jedyne miejsce, które pisze config.env."""
    for key, value in changes.items():
        _check_config_entry(key, value)
    with _WRITE_LOCK:
        before = load_config(path)
        exists = os.path.exists(path)
        text = open(path, encoding="utf-8").read() if exists else ""  # noqa: SIM115 — jednorazowy odczyt całego pliku
        edited = _edit_config_lines(text.splitlines(), changes)
        new_text = "\n".join(edited) + ("\n" if edited else "")
        if new_text == text:
            return []
        directory = os.path.dirname(os.path.abspath(path))
        if exists:
            backup = path + ".bak"
            shutil.copyfile(path, backup)
            os.chmod(backup, 0o600)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(new_text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        after = load_config(path)
    return sorted(key for key in changes if before.get(key) != after.get(key))


class ConfigFile:
    """config.env z odczytem zapamiętywanym według mtime i rozmiaru: zmiana pliku (np. hasła SMTP) działa bez
    restartu serwera, a kolejne żądania nie czytają dysku, dopóki plik się nie zmieni."""

    def __init__(self, path: str = DEFAULT_CONFIG):
        self.path = path
        self._lock = threading.Lock()
        self._stamp: Optional[tuple] = None
        self._values: dict = {}

    def load(self) -> dict:
        try:
            stat = os.stat(self.path)
        except OSError:
            return {}
        stamp = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if stamp != self._stamp:
                self._values = load_config(self.path)
                self._stamp = stamp
            return dict(self._values)

    def exists(self) -> bool:
        return os.path.exists(self.path)


@dataclass(frozen=True)
class SmtpSettings:
    """Dane połączenia SMTP jednej skrzynki z config.env (hasło nie trafia do `repr`)."""

    host: str
    port: int
    user: str
    password: str = field(repr=False)  # nigdy w logach ani w komunikatach
    use_ssl: bool = True
    slot: int = 1  # 1 = SMTP_*, 2 = SMTP2_* …

    @classmethod
    def from_config(cls, config: Mapping[str, str], slot: int = 1) -> "SmtpSettings":
        prefix = smtp_prefix(slot)
        keys = [f"{prefix}_{name}" for name in SMTP_REQUIRED]
        missing = [key for key in keys if not config.get(key)]
        if missing:
            raise ConfigError(f"W config.env brakuje: {', '.join(missing)}.")
        try:
            port = int(config[f"{prefix}_PORT"])
        except ValueError:
            raise ConfigError(f"{prefix}_PORT w config.env nie jest liczbą.") from None
        return cls(config[f"{prefix}_HOST"], port, config[f"{prefix}_USER"], config[f"{prefix}_PASSWORD"],
                   config.get(f"{prefix}_USE_SSL", "true").lower() == "true", slot)


IMAP_KEY_NAMES = ("HOST", "PORT", "USE_SSL", "USER", "PASSWORD")  # końcówki kluczy IMAP skrzynki (IMAP, IMAP2 …)


@dataclass(frozen=True)
class ImapSettings:
    """Dane odbioru poczty jednej skrzynki. Skrzynka nazywa się swoim adresem nadawczym (`address` = `SMTPn_USER`), bo to
    on trafia do `maile.sent_from`; login i hasło IMAP bywają inne, a gdy ich nie podano, są takie jak w SMTP."""

    host: str
    port: int
    user: str  # login do serwera IMAP
    password: str = field(repr=False)  # nigdy w logach ani w komunikatach
    use_ssl: bool = True
    slot: int = 1
    address: str = ""  # adres skrzynki (SMTPn_USER): jej tożsamość w programie

    @classmethod
    def from_config(cls, config: Mapping[str, str], slot: int = 1) -> "ImapSettings":
        prefix, smtp = imap_prefix(slot), smtp_prefix(slot)
        if not config.get(f"{prefix}_HOST"):
            raise ConfigError(f"W config.env brakuje: {prefix}_HOST.")
        address = config.get(f"{smtp}_USER", "")
        if not address:
            raise ConfigError(f"{prefix}_HOST wymaga {smtp}_USER: skrzynka jest rozpoznawana po adresie nadawcy.")
        password = config.get(f"{prefix}_PASSWORD") or config.get(f"{smtp}_PASSWORD", "")
        if not password:
            raise ConfigError(f"W config.env brakuje hasła: ustaw {prefix}_PASSWORD albo {smtp}_PASSWORD.")
        use_ssl = config.get(f"{prefix}_USE_SSL", "true").lower() == "true"
        raw_port = config.get(f"{prefix}_PORT") or ("993" if use_ssl else "143")
        try:
            port = int(raw_port)
        except ValueError:
            raise ConfigError(f"{prefix}_PORT w config.env nie jest liczbą.") from None
        return cls(config[f"{prefix}_HOST"], port, config.get(f"{prefix}_USER") or address, password, use_ssl, slot, address)


def imap_slot_numbers(config: Mapping[str, str]) -> list:
    """Numery skrzynek, które mają jakikolwiek wpis IMAP (rosnąco, skrzynka 1 pierwsza)."""
    return [slot for slot in (1, *slots_in_keys(config))
            if any(config.get(f"{imap_prefix(slot)}_{name}") for name in IMAP_KEY_NAMES)]


def imap_slots(config: Mapping[str, str]) -> list:
    """Skrzynki z odbiorem poczty. Slot z jakimkolwiek wpisem IMAP musi być kompletny (inaczej `ConfigError`)."""
    return [ImapSettings.from_config(config, slot) for slot in imap_slot_numbers(config)]


def extra_smtp_slots(config: Mapping[str, str]) -> list:
    """Dodatkowe skrzynki (SMTP2_*, SMTP3_* … bez ograniczenia liczby) w kolejności numerów. Slot z jakimkolwiek wpisem
    musi być kompletny (inaczej `ConfigError`), więc połowicznie wpisana skrzynka nie wypadnie po cichu z rotacji."""
    found = []
    for slot in slots_in_keys(config):
        prefix = smtp_prefix(slot)
        if any(config.get(f"{prefix}_{name}") for name in SMTP_REQUIRED):
            found.append(SmtpSettings.from_config(config, slot))
    return found


def configured_mailboxes(config: Mapping[str, str]) -> list:
    """Skrzynki z kompletnymi danymi, do pokazania w panelu (niekompletne i powtórzone są pomijane bez błędu — rotację
    zabezpiecza `smtp_slots`, która takie przypadki zgłasza)."""
    found: list[SmtpSettings] = []
    for slot in (1, *slots_in_keys(config)):
        try:
            candidate = SmtpSettings.from_config(config, slot)
        except ConfigError:
            continue
        if all(candidate.user.casefold() != other.user.casefold() for other in found):
            found.append(candidate)
    return found


def reject_duplicate_users(users: Mapping[int, str]) -> None:
    """Dwie skrzynki z tym samym adresem rozjechałyby rotację i limity (liczone po adresie) — odmawiamy."""
    seen: dict[str, int] = {}
    for slot, user in sorted(users.items()):
        key = user.strip().casefold()
        if key and key in seen:
            raise ConfigError(f"Skrzynki {seen[key]} i {slot} mają ten sam adres ({smtp_prefix(slot)}_USER) — każda musi być inna.")
        seen.setdefault(key, slot)


def smtp_slots(config: Mapping[str, str]) -> list:
    """Wszystkie skrzynki do rotacji: główna (SMTP_*) i dodatkowe. Rzuca `ConfigError` przy niekompletnej albo powtórzonej."""
    slots = [SmtpSettings.from_config(config), *extra_smtp_slots(config)]
    reject_duplicate_users({slot.slot: slot.user for slot in slots})
    return slots


def check_mailboxes(config: Mapping[str, str]) -> None:
    """Sprawdza skrzynki przed zapisem z panelu: dodatkowe kompletne i żadnych powtórzeń adresu (skrzynki głównej
    nie wymagamy, bo świeży plik może jej jeszcze nie mieć)."""
    extras = extra_smtp_slots(config)
    users = {slot.slot: slot.user for slot in extras}
    if config.get("SMTP_USER"):
        users[1] = config["SMTP_USER"]
    reject_duplicate_users(users)
    imap_slots(config)  # niekompletny wpis IMAP to błąd już przy zapisie, nie dopiero przy pierwszym pobieraniu


_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")


@dataclass(frozen=True)
class NotifySettings:
    """Powiadomienia automatu z config.env: wszystkie klucze są opcjonalne, a sekrety nie trafiają do `repr`."""

    macos: bool = True  # NOTIFY_MACOS=false wyłącza powiadomienia systemowe
    ntfy_topic: str = field(default="", repr=False)  # NTFY_TOPIC: długi, losowy sekret (kto go zna, ten czyta i pisze)
    ntfy_server: str = "https://ntfy.sh"  # NTFY_SERVER
    email_to: str = ""  # NOTIFY_EMAIL: dokąd wysyłać podsumowania i alerty
    healthcheck_url: str = field(default="", repr=False)  # HEALTHCHECK_URL: ping dead-man's switch (np. healthchecks.io)

    @classmethod
    def from_config(cls, config: Mapping[str, str]) -> "NotifySettings":
        topic = config.get("NTFY_TOPIC", "").strip()
        if topic and not _TOPIC_RE.fullmatch(topic):
            raise ConfigError("NTFY_TOPIC w config.env musi mieć 8-64 znaków: litery, cyfry, „-” i „_”.")
        server = config.get("NTFY_SERVER", "").strip() or "https://ntfy.sh"
        health = config.get("HEALTHCHECK_URL", "").strip()
        email = config.get("NOTIFY_EMAIL", "").strip()
        for name, value in (("NTFY_SERVER", server), ("HEALTHCHECK_URL", health)):
            if value and not value.startswith("https://"):
                raise ConfigError(f"{name} w config.env musi zaczynać się od https://.")
        if email and ("@" not in email or " " in email):
            raise ConfigError("NOTIFY_EMAIL w config.env nie wygląda na adres e-mail.")
        return cls(config.get("NOTIFY_MACOS", "true").strip().lower() != "false", topic, server, email, health)

    def configured_channels(self) -> list:
        """Nazwy kanałów, które mają komplet ustawień (e-mail dodatkowo wymaga SMTP, co sprawdza wołający)."""
        return [name for name, on in (("macos", self.macos), ("ntfy", bool(self.ntfy_topic)),
                                      ("email", bool(self.email_to))) if on]


def require_smtp(config: dict, path: str = DEFAULT_CONFIG) -> dict:
    """Sprawdza, że dane SMTP są kompletne; zwraca `config`, żeby dało się użyć w wyrażeniu."""
    if not os.path.exists(path) and "SMTP_HOST" not in config:
        raise ConfigError(
            "Brak config.env. Skopiuj config.example.env do config.env i uzupełnij "
            "dane SMTP (host/port/użytkownik/hasło) — patrz komentarze w tym pliku."
        )
    missing = [key for key in SMTP_KEYS if not config.get(key)]
    if missing:
        raise ConfigError(f"W config.env brakuje: {', '.join(missing)}.")
    return config


def smtp_configured(config: Mapping[str, str]) -> bool:
    """Czy w konfiguracji są wszystkie klucze SMTP (bez sprawdzania poprawności wartości)."""
    return all(config.get(key) for key in SMTP_KEYS)
