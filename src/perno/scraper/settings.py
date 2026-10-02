"""Ustawienia skrapera z twardymi limitami uprzejmości.

Jedyna droga do ustawień używanych w produkcji to `ScanSettings.from_params`
(CLI, UI i config.env) — ona przycina wartości do dozwolonych zakresów, w tym
do twardej podłogi 1,0 s odstępu między żądaniami do tej samej domeny.
`for_tests` omija podłogę wyłącznie na potrzeby testów z zegarem fałszywym.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from typing import Mapping, Optional

USER_AGENT_PRODUCT = "PernoContextBot/1.0"
HARD_DELAY_FLOOR = 1.0

# Stałe (nie do ustawienia przez użytkownika)
CONNECT_TIMEOUT = 10.0
RECV_TIMEOUT = 15.0
REQUEST_DEADLINE = 30.0
MAX_BODY_BYTES = 2 * 1024 * 1024
ROBOTS_MAX_BYTES = 512 * 1024
MAX_REDIRECTS = 5
MAX_DISCOVERED_PER_FIRM = 20_000
FRONTIER_MEMORY_CAP = 5_000
HEARTBEAT_EVERY = 5.0
STALE_AFTER = 90.0

# nazwa -> (min, max)
LIMITS = {
    "concurrency": (1, 100),
    "delay": (HARD_DELAY_FLOOR, 60.0),
    "depth": (1, 20),
    "max_pages": (1, 5000),
    "max_mb": (1, 500),
    "max_minutes": (1, 240),
    "ip_cap": (1, 10),
    "max_rps": (1.0, 50.0),
}
_FLOAT_FIELDS = {"delay", "max_rps"}

# Profil wybiera BUDŻET NA FIRMĘ (ile stron, jak głęboko, ile danych i czasu, ile adresów trzymać w kolejce). Uprzejmość
# wobec hostów (odstęp, podłoga, robots.txt, limity na IP) od profilu nie zależy. „kontekst" bierze ok. 25 stron z
# kolejki ułożonej priorytetami (strona główna, kontakt, o nas, oferta): w danych z pierwszych skanów pierwszy adres
# e-mail leżał w ≤10 pobranych stronach u 99,7% firm, a trzykrotnie mniej stron to mniej ruchu i mniejsza baza.
DEFAULT_PROFILE = "zbalansowany"
PROFILES = {
    "zbalansowany": {"depth": 10, "max_pages": 500, "max_mb": 50, "max_minutes": 30, "max_discovered": MAX_DISCOVERED_PER_FIRM},
    "kontekst": {"depth": 4, "max_pages": 25, "max_mb": 15, "max_minutes": 10, "max_discovered": 500},
}
PROFILE_LABELS = {
    "zbalansowany": "Zbalansowany (do 500 stron na firmę)",
    "kontekst": "Kontekst (ok. 25 stron na firmę)",
}
BUDGET_KEYS = ("depth", "max_pages", "max_mb", "max_minutes")

CONFIG_KEYS = {
    "concurrency": "SKRAPER_CONCURRENCY",
    "delay": "SKRAPER_DELAY",
    "depth": "SKRAPER_MAX_DEPTH",
    "max_pages": "SKRAPER_MAX_PAGES",
    "max_mb": "SKRAPER_MAX_MB",
    "max_minutes": "SKRAPER_MAX_MINUTES",
    "ip_cap": "SKRAPER_IP_CAP",
    "max_rps": "SKRAPER_MAX_RPS",
    "store_html": "SKRAPER_STORE_HTML",
    "contact": "SKRAPER_CONTACT",
    "profile": "SKRAPER_PROFILE",
}
PARAM_ALIASES = {
    "profil": "profile",
    "wspolbieznosc": "concurrency",
    "opoznienie": "delay",
    "glebokosc": "depth",
    "max_stron": "max_pages",
    "max_minut": "max_minutes",
}
STORE_HTML_MODES = ("all", "klucz", "nie")
CONTACT_RE = re.compile(r"^[A-Za-z0-9@._:/+\-]{3,200}$")


class SettingsError(ValueError):
    pass


def _present(values: Mapping) -> dict:
    """Tylko niepuste wartości (puste pole formularza i brak klucza w config.env oznaczają „nie ustawiono")."""
    return {name: raw for name, raw in values.items() if raw not in (None, "")}


@dataclass(frozen=True)
class ScanSettings:
    concurrency: int = 30
    delay: float = 2.5
    depth: int = 10
    max_pages: int = 500
    max_mb: int = 50
    max_minutes: int = 30
    ip_cap: int = 2
    max_rps: float = 15.0
    store_html: str = "klucz"
    profile: str = DEFAULT_PROFILE
    max_discovered: int = MAX_DISCOVERED_PER_FIRM  # ile adresów jednej firmy w ogóle zapamiętujemy (kolejka + pobrane)
    contact: str = ""
    contact_source: str = ""  # "param" | "config" | "smtp_user" | ""
    run_max_hours: float = 6.0
    floor: float = HARD_DELAY_FLOOR

    @property
    def max_bytes(self) -> int:
        return self.max_mb * 1024 * 1024

    def user_agent(self) -> str:
        return f"{USER_AGENT_PRODUCT} (+{self.contact})" if self.contact else USER_AGENT_PRODUCT

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_params(cls, params: Optional[Mapping] = None, config: Optional[Mapping] = None) -> "ScanSettings":
        """Pierwszeństwo: params (UI/CLI) > config.env > profil > wartości domyślne.

        Profil (`profile`) ustawia budżet na firmę (`BUDGET_KEYS`). Liczby z config.env nadpisują profil z config.env,
        ale profil podany w params wygrywa z liczbami z config.env (jawny wybór w UI/CLI jest ważniejszy niż plik)."""
        config = config or {}
        config_values = _present({name: config.get(key) for name, key in CONFIG_KEYS.items()})
        aliased = {PARAM_ALIASES.get(name, name): raw for name, raw in (params or {}).items()}
        param_values = _present({name: aliased[name] for name in CONFIG_KEYS if name in aliased})

        profile = cls._profile(param_values.get("profile", config_values.get("profile", DEFAULT_PROFILE)))
        values: dict = dict(PROFILES[profile])
        for name, raw in config_values.items():
            if name != "profile" and not (name in BUDGET_KEYS and "profile" in param_values):
                values[name] = raw
        values.update((name, raw) for name, raw in param_values.items() if name != "profile")

        contact_source = "param" if "contact" in param_values else "config" if "contact" in config_values else ""
        if "contact" not in values and config.get("SMTP_USER"):
            values["contact"] = config["SMTP_USER"]
            contact_source = "smtp_user"
        clean = cls._validated(values)
        return cls(profile=profile, contact_source=contact_source if clean.get("contact") else "", **clean)

    @staticmethod
    def _profile(raw: object) -> str:
        if not isinstance(raw, str) or raw not in PROFILES:
            raise SettingsError(f"Profil musi być jednym z: {', '.join(PROFILES)}.")
        return raw

    @classmethod
    def _validated(cls, values: Mapping) -> dict:
        """Wartości po sprawdzeniu i przycięciu do dozwolonych zakresów (`max_discovered` pochodzi tylko z profilu)."""
        clean: dict = {}
        for name, raw in values.items():
            if name in LIMITS:
                clean[name] = cls._number(name, raw)
            elif name == "max_discovered":
                clean[name] = int(raw)
            elif name == "store_html":
                if raw not in STORE_HTML_MODES:
                    raise SettingsError(f"store_html musi być jednym z: {', '.join(STORE_HTML_MODES)}.")
                clean[name] = raw
            elif name == "contact":
                contact = str(raw).strip()
                if not CONTACT_RE.match(contact):
                    raise SettingsError("Kontakt (SKRAPER_CONTACT) może zawierać tylko litery, cyfry i znaki @ . _ : / + - (3–200 znaków).")
                clean[name] = contact
        return clean

    @staticmethod
    def _number(name: str, raw):
        low, high = LIMITS[name]
        try:
            number = float(raw) if name in _FLOAT_FIELDS else int(float(raw))
        except (TypeError, ValueError):
            raise SettingsError(f"Ustawienie {name}: {raw!r} nie jest liczbą.") from None
        if number != number:  # NaN
            raise SettingsError(f"Ustawienie {name}: niedozwolona wartość.")
        return max(low, min(high, number))

    @classmethod
    def for_tests(cls, **overrides) -> "ScanSettings":
        """Bez podłogi odstępu i z krótkimi opóźnieniami — WYŁĄCZNIE do testów."""
        base = cls(delay=0.0, floor=0.0, contact="test@example.com", contact_source="param")
        return replace(base, **overrides)
