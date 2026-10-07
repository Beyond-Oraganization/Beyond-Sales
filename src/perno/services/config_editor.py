"""Edycja config.env z panelu: opis pól dla formularza, zapis zmian po sprawdzeniu i próba połączenia z SMTP.

Sekrety (hasło SMTP, temat ntfy, adres pingu) są tylko do zapisu: `describe` mówi, czy są ustawione, ale nigdy nie
zwraca ich wartości. Pole sekretu pozostawione puste oznacza „bez zmian”; usunięcie sekretu wymaga jawnego `clear`.
"""

from __future__ import annotations

from typing import Callable, Iterable, Mapping

from perno import settings as cfg
from perno.domain import config_fields as cf
from perno.notify import scrub
from perno.services import sending


def describe(config: Mapping, *, file_exists: bool) -> dict:
    """Pola do formularza, pogrupowane. Wartość pokazujemy tylko dla pól jawnych; sekret to wyłącznie `set: true/false`."""
    groups = []
    for key, title, description in cf.GROUPS:
        fields = []
        for field in (f for f in cf.FIELDS if f.group == key):
            raw = config.get(field.key)
            item = {"key": field.key, "label": field.label, "kind": field.kind, "help": field.help,
                    "default": field.default, "choices": list(field.choices), "low": field.low, "high": field.high,
                    "restart": field.restart, "set": bool(raw)}
            if field.kind != cf.KIND_SECRET:
                item["value"] = raw if raw is not None else ""
            fields.append(item)
        groups.append({"key": key, "title": title, "description": description, "fields": fields,
                       "mailbox": cf.mailbox_slot(key)})
    return {"groups": groups, "file_exists": file_exists}


def _known(key: object) -> cf.ConfigField:
    field = cf.BY_KEY.get(key) if isinstance(key, str) else None
    if field is None:
        raise cf.ConfigValueError(f"Nieznane ustawienie: {str(key)[:40]!r}.")
    return field


def build_changes(values: Mapping, clear: list) -> dict:
    """Zmiany do zapisu (`klucz -> tekst albo None`) po sprawdzeniu. Pusty tekst usuwa zwykłe ustawienie (wraca domyślne)
    i nie rusza sekretu; `clear` usuwa także sekret."""
    changes: dict = {}
    for key, raw in values.items():
        field = _known(key)
        if not isinstance(raw, str):
            raise cf.ConfigValueError(f"{field.label}: wartość musi być tekstem.")
        if not raw.strip():
            if field.kind != cf.KIND_SECRET:
                changes[key] = None
            continue
        changes[key] = cf.validate(field, raw)
    for key in clear:
        field = _known(key)
        if key in values and values[key] and str(values[key]).strip():
            raise cf.ConfigValueError(f"{field.label}: nie można jednocześnie ustawić i usunąć.")
        changes[key] = None
    return changes


def _check_mailboxes(path: str, changes: Mapping) -> None:
    """Skrzynki po zmianach muszą być kompletne i różne (rzuca `ConfigError`); plik się wtedy nie zmienia."""
    if not any(cf.mailbox_slot(cf.BY_KEY[key].group) is not None for key in changes):
        return
    merged = dict(cfg.load_config(path))
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    cfg.check_mailboxes(merged)


def apply_changes(path: str, values: Mapping, clear: list) -> dict:
    """Zapisuje zmiany w config.env. Zwraca klucze, które faktycznie się zmieniły, i te z nich, które wymagają
    ponownego uruchomienia demona automatu."""
    changes = build_changes(values, clear)
    _check_mailboxes(path, changes)
    changed = cfg.update_config(path, changes) if changes else []
    return {"changed": changed, "restart_demon": [key for key in changed if cf.BY_KEY[key].restart == cf.RESTART_DEMON]}


def _test_mailbox(mailer: sending.Mailer, passwords: Iterable[str]) -> dict:
    address = mailer.sender.address
    try:
        with mailer.smtp_factory():
            pass
    except Exception as exc:  # noqa: BLE001 — każdy błąd połączenia (sieć, TLS, logowanie) to wynik próby, nie awaria programu
        return {"address": address, "ok": False, "message": scrub(f"{type(exc).__name__}: {str(exc)[:200]}", list(passwords))}
    return {"address": address, "ok": True, "message": f"Połączono i zalogowano jako {address}."}


def test_smtp(mailers: Callable, passwords: Iterable[str] = ()) -> dict:
    """Łączy się z serwerem każdej skrzynki i loguje (bez wysyłania maila). Zwraca `{"ok", "message", "results"}`, gdzie
    `results` to wynik dla każdej skrzynki; hasła nie trafiają do komunikatów."""
    try:
        resolved = mailers()
    except cfg.ConfigError as exc:
        return {"ok": False, "message": str(exc), "results": []}
    results = [_test_mailbox(mailer, passwords) for mailer in resolved]
    if len(results) == 1:
        message = results[0]["message"]
    else:
        message = "; ".join(f"{r['address']}: {'OK' if r['ok'] else r['message']}" for r in results)
    return {"ok": all(r["ok"] for r in results), "message": message, "results": results}
