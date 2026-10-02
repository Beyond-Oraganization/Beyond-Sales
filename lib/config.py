"""Ścieżki projektu i wczytywanie config.env (bez zewnętrznych zależności)."""

from __future__ import annotations

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


class ConfigError(Exception):
    """Brak lub niepełna konfiguracja (komunikat po polsku, bez haseł)."""


def load_config(path: str) -> dict:
    config = {}
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


def require_smtp(config: dict, path: str = DEFAULT_CONFIG) -> dict:
    if not os.path.exists(path) and "SMTP_HOST" not in config:
        raise ConfigError(
            "Brak config.env. Skopiuj config.example.env do config.env i uzupełnij "
            "dane SMTP (host/port/użytkownik/hasło) — patrz komentarze w tym pliku."
        )
    missing = [k for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD") if not config.get(k)]
    if missing:
        raise ConfigError(f"W config.env brakuje: {', '.join(missing)}.")
    return config


def smtp_configured(config: dict) -> bool:
    return all(config.get(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"))
