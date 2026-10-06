"""Które ustawienia z config.env można zmieniać w panelu i jak je sprawdzać (czysta tabela reguł, bez plików).

Świadomie brak tu `DB_*`: błędny adres, użytkownik albo hasło bazy uniemożliwiłyby następny start programu, a panel
działa na już otwartym połączeniu, więc nie miałby jak o tym ostrzec. Bazę ustawia się raz, ręcznie (README, sekcja 2).
Sekrety (`kind="secret"`) są tylko do zapisu: panel nigdy nie odsyła ich wartości do przeglądarki.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

KIND_TEXT = "text"
KIND_SECRET = "secret"  # noqa: S105 — nazwa rodzaju pola, nie hasło
KIND_BOOL = "bool"
KIND_INT = "int"
KIND_FLOAT = "float"
KIND_CHOICE = "choice"

RESTART_DEMON = "demon"  # klucz czytany przez demona automatu przy starcie (kanały powiadomień)

_HOST_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?")
_TOPIC_RE = re.compile(r"[A-Za-z0-9_\-]{8,64}")
_CONTACT_RE = re.compile(r"[A-Za-z0-9@._:/+\-]{3,200}")  # to samo co scraper.settings.CONTACT_RE (pilnuje tego test)


class ConfigValueError(ValueError):
    """Wartość ustawienia jest niepoprawna (komunikat po polsku, bez samej wartości: może być sekretem)."""


@dataclass(frozen=True)
class ConfigField:
    """Jedno ustawienie: klucz w pliku, opis dla człowieka i reguła sprawdzania."""
    key: str
    label: str
    group: str
    kind: str = KIND_TEXT
    help: str = ""
    default: str = ""  # co program przyjmuje, gdy klucza nie ma w pliku (tylko do pokazania)
    choices: tuple = ()
    low: Optional[float] = None
    high: Optional[float] = None
    restart: str = ""  # RESTART_DEMON albo pusty


GROUPS = (
    ("mail", "Poczta (SMTP)", "Skrzynka, z której wychodzą maile. Hasło jest tylko do zapisu: panel go nie pokazuje."),
    ("notify", "Powiadomienia automatu", "Dokąd automat wysyła alerty i podsumowania. Zmiany widzi demon po ponownym uruchomieniu."),
    ("scraper", "Skraper", "Domyślne ustawienia skanowania stron firm. Zasady uprzejmości (robots.txt, minimum 1 s) nie są do ustawienia."),
)

FIELDS = (
    ConfigField("SMTP_HOST", "Serwer SMTP", "mail", help="np. smtp.zenbox.pl"),
    ConfigField("SMTP_PORT", "Port SMTP", "mail", KIND_INT, "465 dla SSL, 587 dla STARTTLS.", low=1, high=65535),
    ConfigField("SMTP_USE_SSL", "Połączenie SSL", "mail", KIND_BOOL, "Tak = SSL (port 465), nie = STARTTLS.", default="true"),
    ConfigField("SMTP_USER", "Użytkownik (adres nadawcy)", "mail", help="Zwykle pełny adres skrzynki."),
    ConfigField("SMTP_PASSWORD", "Hasło", "mail", KIND_SECRET, "Zostaw puste, żeby nie zmieniać."),
    ConfigField("SENDER_NAME", "Nazwa nadawcy", "mail", help="Widoczna w nagłówku „Od” i jako podpis maila."),
    ConfigField("NOTIFY_MACOS", "Powiadomienia macOS", "notify", KIND_BOOL, "Okienka w Centrum powiadomień.", "true", restart=RESTART_DEMON),
    ConfigField("NTFY_TOPIC", "Temat ntfy (telefon)", "notify", KIND_SECRET,
                "Długi, losowy (8–64 znaków: litery, cyfry, - i _). To sekret: kto go zna, czyta i pisze. W aplikacji ntfy zasubskrybuj ten temat.",
                restart=RESTART_DEMON),
    ConfigField("NTFY_SERVER", "Serwer ntfy", "notify", help="Domyślnie https://ntfy.sh", default="https://ntfy.sh", restart=RESTART_DEMON),
    ConfigField("NOTIFY_EMAIL", "E-mail na podsumowania", "notify", help="Wysyłane tą samą skrzynką SMTP.", restart=RESTART_DEMON),
    ConfigField("HEALTHCHECK_URL", "Adres pingu (dead-man's switch)", "notify", KIND_SECRET,
                "np. z healthchecks.io; alarmuje, gdy Mac milczy w oknie wysyłki.", restart=RESTART_DEMON),
    ConfigField("SKRAPER_CONTACT", "Kontakt w nagłówku bota", "scraper", help="Adres e-mail albo URL widoczny dla właścicieli stron."),
    ConfigField("SKRAPER_PROFILE", "Profil budżetu na firmę", "scraper", KIND_CHOICE, choices=("zbalansowany", "kontekst"),
                help="Zbalansowany: do 500 stron na firmę. Kontekst: ok. 25.", default="zbalansowany"),
    ConfigField("SKRAPER_CONCURRENCY", "Domen naraz", "scraper", KIND_INT, low=1, high=100, default="30"),
    ConfigField("SKRAPER_DELAY", "Odstęp na domenę (s)", "scraper", KIND_FLOAT, "Minimum 1 s.", "2.5", low=1.0, high=60.0),
    ConfigField("SKRAPER_IP_CAP", "Żądań naraz do jednego IP", "scraper", KIND_INT, low=1, high=10, default="2"),
    ConfigField("SKRAPER_MAX_RPS", "Żądań na sekundę łącznie", "scraper", KIND_FLOAT, low=1.0, high=50.0, default="15"),
    ConfigField("SKRAPER_STORE_HTML", "Zapis surowego HTML", "scraper", KIND_CHOICE, choices=("klucz", "all", "nie"),
                help="klucz = tylko strony kluczowe, all = wszystkie, nie = żadne.", default="klucz"),
    ConfigField("SKRAPER_MAX_DEPTH", "Głębokość linków", "scraper", KIND_INT, "Puste = z profilu.", low=1, high=20),
    ConfigField("SKRAPER_MAX_PAGES", "Stron na firmę", "scraper", KIND_INT, "Puste = z profilu.", low=1, high=5000),
    ConfigField("SKRAPER_MAX_MB", "MB na firmę", "scraper", KIND_INT, "Puste = z profilu.", low=1, high=500),
    ConfigField("SKRAPER_MAX_MINUTES", "Minut na firmę", "scraper", KIND_INT, "Puste = z profilu.", low=1, high=240),
)

BY_KEY = {field.key: field for field in FIELDS}


def _number(field: ConfigField, raw: str) -> str:
    try:
        number = float(raw)
    except ValueError:
        raise ConfigValueError(f"{field.label}: wpisz liczbę.") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ConfigValueError(f"{field.label}: niedozwolona wartość.")
    if field.kind == KIND_INT and not number.is_integer():
        raise ConfigValueError(f"{field.label}: wpisz liczbę całkowitą.")
    if field.low is not None and number < field.low or field.high is not None and number > field.high:
        raise ConfigValueError(f"{field.label}: poza zakresem ({field.low:g}–{field.high:g}).")
    return str(int(number)) if field.kind == KIND_INT else raw


def _text(field: ConfigField, raw: str) -> str:
    key = field.key
    if key == "SMTP_HOST" and not _HOST_RE.fullmatch(raw):
        raise ConfigValueError(f"{field.label}: to nie wygląda na nazwę serwera.")
    if key == "NOTIFY_EMAIL" and ("@" not in raw or " " in raw):
        raise ConfigValueError(f"{field.label}: to nie wygląda na adres e-mail.")
    if key == "SMTP_USER" and (" " in raw or len(raw) > 200):
        raise ConfigValueError(f"{field.label}: bez spacji, najwyżej 200 znaków.")
    if key == "SENDER_NAME" and len(raw) > 100:
        raise ConfigValueError(f"{field.label}: najwyżej 100 znaków.")
    if key == "NTFY_SERVER" and not raw.startswith("https://"):
        raise ConfigValueError(f"{field.label}: adres musi zaczynać się od https://.")
    if key == "SKRAPER_CONTACT" and not _CONTACT_RE.fullmatch(raw):
        raise ConfigValueError(f"{field.label}: tylko litery, cyfry i znaki @ . _ : / + - (3–200 znaków).")
    return raw


def _secret(field: ConfigField, raw: str) -> str:
    if field.key == "NTFY_TOPIC" and not _TOPIC_RE.fullmatch(raw):
        raise ConfigValueError(f"{field.label}: 8–64 znaków, tylko litery, cyfry, „-” i „_”.")
    if field.key == "HEALTHCHECK_URL" and not raw.startswith("https://"):
        raise ConfigValueError(f"{field.label}: adres musi zaczynać się od https://.")
    if field.key == "SMTP_PASSWORD" and len(raw) > 500:
        raise ConfigValueError(f"{field.label}: za długie.")
    return raw


def validate(field: ConfigField, raw: str) -> str:
    """Wartość do zapisania w pliku (po przycięciu spacji) albo `ConfigValueError`. Pusty tekst obsługuje wołający."""
    value = raw.strip()
    if field.kind in (KIND_INT, KIND_FLOAT):
        return _number(field, value)
    if field.kind == KIND_BOOL:
        if value.lower() not in ("true", "false"):
            raise ConfigValueError(f"{field.label}: wybierz tak albo nie.")
        return value.lower()
    if field.kind == KIND_CHOICE:
        if value not in field.choices:
            raise ConfigValueError(f"{field.label}: dozwolone wartości to {', '.join(field.choices)}.")
        return value
    if any(ch in value for ch in "\r\n\x00"):
        raise ConfigValueError(f"{field.label}: bez znaków nowej linii.")
    return _secret(field, value) if field.kind == KIND_SECRET else _text(field, value)
