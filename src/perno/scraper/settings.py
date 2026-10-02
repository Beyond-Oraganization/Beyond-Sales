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
}
PARAM_ALIASES = {
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
        """Pierwszeństwo: params (UI/CLI) > config.env > wartości domyślne."""
        values: dict = {}
        contact_source = ""

        for name, key in CONFIG_KEYS.items():
            raw = (config or {}).get(key)
            if raw not in (None, ""):
                values[name] = raw
                if name == "contact":
                    contact_source = "config"
        for raw_name, raw in (params or {}).items():
            name = PARAM_ALIASES.get(raw_name, raw_name)
            if name in CONFIG_KEYS and raw not in (None, ""):
                values[name] = raw
                if name == "contact":
                    contact_source = "param"

        if "contact" not in values and (config or {}).get("SMTP_USER"):
            values["contact"] = config["SMTP_USER"]
            contact_source = "smtp_user"

        clean: dict = {}
        for name, raw in values.items():
            if name in LIMITS:
                clean[name] = cls._number(name, raw)
            elif name == "store_html":
                if raw not in STORE_HTML_MODES:
                    raise SettingsError(f"store_html musi być jednym z: {', '.join(STORE_HTML_MODES)}.")
                clean[name] = raw
            elif name == "contact":
                contact = str(raw).strip()
                if not CONTACT_RE.match(contact):
                    raise SettingsError("Kontakt (SKRAPER_CONTACT) może zawierać tylko litery, cyfry i znaki @ . _ : / + - (3–200 znaków).")
                clean[name] = contact
        return cls(contact_source=contact_source if clean.get("contact") else "", **clean)

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
