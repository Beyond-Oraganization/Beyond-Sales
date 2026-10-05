"""Ustawienia programu: ścieżki projektu, config.env i zmienne środowiska (bez zewnętrznych zależności).

To jedyny moduł, który czyta `config.env` i `os.environ`. Reszta programu dostaje gotowe wartości
(`Environment`, `SmtpSettings`, słownik z `ConfigFile.load()`), więc zależności są jawne, a testy nie muszą
podmieniać globalnego stanu procesu.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Mapping, Optional

# Korzeń projektu (katalog z config.env, data/, templates/): src/perno/settings.py leży trzy poziomy niżej.
# Zakłada instalację edytowalną (`uv sync`); przy zwykłej instalacji z wheela ścieżki danych trzeba by podać jawnie.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PERNO_DIR = os.path.dirname(BASE_DIR)

DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.env")
EVENTY_SEED = os.path.join(BASE_DIR, "data", "eventy.csv")
DRIVERZY_SEED = os.path.join(BASE_DIR, "data", "driverzy.csv")
EVENTY_TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "eventy.html")
DRIVERZY_TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "driverzy.html")
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
    """Dane połączenia SMTP z config.env (hasło nie trafia do `repr`)."""

    host: str
    port: int
    user: str
    password: str = field(repr=False)  # nigdy w logach ani w komunikatach
    use_ssl: bool = True

    @classmethod
    def from_config(cls, config: Mapping[str, str]) -> "SmtpSettings":
        missing = [key for key in SMTP_KEYS if not config.get(key)]
        if missing:
            raise ConfigError(f"W config.env brakuje: {', '.join(missing)}.")
        try:
            port = int(config["SMTP_PORT"])
        except ValueError:
            raise ConfigError("SMTP_PORT w config.env nie jest liczbą.") from None
        return cls(config["SMTP_HOST"], port, config["SMTP_USER"], config["SMTP_PASSWORD"],
                   config.get("SMTP_USE_SSL", "true").lower() == "true")


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
