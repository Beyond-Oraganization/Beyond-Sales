"""Budowa wiadomości i wysyłka (wspólne dla CLI i panelu): jeden wiersz, wysyłka z panelu i paczka z terminala.

SMTP jest wstrzykiwany (`Mailer.smtp_factory`), dzięki czemu testy używają atrapy i nigdy nie wysyłają prawdziwych
maili. Reguły („czy wolno wysłać", dzienny limit) są w `perno.domain.mail_rules`, więc panel i terminal stosują
dokładnie te same.
"""

from __future__ import annotations

import logging
import random
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from email.message import EmailMessage
from typing import Callable, Mapping, Optional

from perno import settings as cfg
from perno.db import DB_ERRORS, Database, RowNotFound
from perno.domain import company_rules, mail_rules
from perno.domain import statuses as st
from perno.mailer import image_prep, mime_builder, smtp_client
from perno.settings import ConfigError, Environment, SmtpSettings, require_smtp

logger = logging.getLogger(__name__)


class NotSendable(Exception):
    """Wiersza nie wolno wysłać (status, brak e-maila lub treści). Komunikat jest dla użytkownika."""


class DailyCapReached(Exception):
    """Dzienny limit wysyłki jest wyczerpany."""


@dataclass(frozen=True)
class Sender:
    """Nadawca: adres (SMTP_USER) i nazwa wyświetlana (SENDER_NAME)."""
    address: str  # SMTP_USER
    name: str = ""  # SENDER_NAME — podpis w treści i nazwa nadawcy


@dataclass(frozen=True)
class Mailer:
    """Wszystko, czego trzeba do wysłania: jak się połączyć i kim się podpisać."""
    smtp_factory: Callable
    sender: Sender


@dataclass
class SendResult:
    """Wynik wysyłki jednego wiersza: rezultat, aktualny wiersz i komunikat."""
    outcome: str  # "sent" | "error" | "skipped" | "sent_unrecorded"
    row: Optional[dict]
    message: str = ""


def smtp_factory_from_config(config: Mapping, env: Optional[Environment] = None) -> Callable:
    """Fabryka klienta SMTP z konfiguracji; odmawia, gdy baza jest testowa (PERNO_DB_URL) bez jawnego odblokowania."""
    if not (env if env is not None else Environment.from_environ()).real_smtp_allowed:
        raise ConfigError(
            "Ustawiono PERNO_DB_URL (baza testowa/demo), więc prawdziwa wysyłka SMTP jest zablokowana. "
            "Jeśli naprawdę chcesz wysyłać, ustaw też PERNO_ALLOW_REAL_SMTP=1."
        )
    smtp = SmtpSettings.from_config(config)

    def factory() -> smtp_client.SmtpClient:
        return smtp_client.SmtpClient(smtp.host, smtp.port, smtp.user, smtp.password, smtp.use_ssl)

    return factory


def build_mailer(config: Mapping, config_path: str = cfg.DEFAULT_CONFIG, *,
                 smtp_factory: Optional[Callable] = None, env: Optional[Environment] = None) -> Mailer:
    """Dane wysyłki z konfiguracji (rzuca `ConfigError`, gdy są niekompletne). `smtp_factory` podmienia klienta SMTP."""
    require_smtp(dict(config), config_path)
    sender = Sender(config["SMTP_USER"], config.get("SENDER_NAME", ""))
    return Mailer(smtp_factory or smtp_factory_from_config(config, env), sender)


def get_inline_image() -> tuple:
    """Obraz osadzany w treści maila (przeskalowany, z cache)."""
    return image_prep.prepare_inline_image(cfg.INLINE_IMAGE_SRC, cfg.INLINE_IMAGE_CACHE_BASE)


def build_message_for_row(row: Mapping, to_addr: str, sender: Sender) -> EmailMessage:
    """Buduje wiadomość MIME z wiersza; obraz jest osadzany tylko wtedy, gdy treść go używa."""
    cid = mime_builder.INLINE_IMAGE_CID
    inline_images = []
    if f"cid:{cid}" in row["body_html"]:
        data, subtype = get_inline_image()
        inline_images.append((cid, data, subtype))
    return mime_builder.build_message(
        from_addr=sender.address, from_name=sender.name, to_addr=to_addr,
        subject=row["subject"], html_body=row["body_html"], text_body=row["body_text"],
        inline_images=inline_images,
    )


def send_row(
    db: Database, row_id: str, mailer: Mailer, *, allowed_from: tuple = (st.STATUS_PENDING, st.STATUS_ERROR),
) -> SendResult:
    """Wysyła jeden wiersz: zajmuje go w bazie, wysyła i zapisuje wynik; błąd SMTP kończy się statusem „Błąd” z komunikatem."""
    claimed = db.claim_for_sending(row_id, allowed_from)
    if claimed is None:
        row = db.get(row_id)
        reason = company_rules.hold_message(row["hold"]) if row and row.get("hold") else (
            "zmieniony status, brak e-maila/treści albo inny adres tej firmy jest właśnie wysyłany")
        return SendResult("skipped", row, f"{reason} — nic nie wysłano")
    try:
        message = build_message_for_row(claimed, claimed["email"], mailer.sender)
        with mailer.smtp_factory() as client:
            client.send(message)
    except KeyboardInterrupt:
        db.mark_error(row_id, "Przerwano (Ctrl+C) — sprawdź folder Wysłane, zanim ponowisz.")
        raise
    except Exception as exc:  # noqa: BLE001 — każdy błąd budowy/wysyłki wiadomości zapisujemy jako status „Błąd” z komunikatem
        reason = str(exc) or type(exc).__name__
        return SendResult("error", db.mark_error(row_id, reason), reason)

    for attempt in range(3):
        try:
            return SendResult("sent", db.mark_sent(row_id))
        except DB_ERRORS:
            time.sleep(0.5 * (attempt + 1))
    warning = (f"UWAGA: mail do {claimed['email']} ({row_id}) WYSŁANO, ale nie udało się zapisać statusu "
               "w bazie. Wiersz zostanie oznaczony jako błąd po 10 min — nie wysyłaj go ponownie.")
    logger.error(warning)
    return SendResult("sent_unrecorded", claimed, warning)


def send_one(db: Database, row_id: str, mailer: Callable[[], Mailer], *, daily_cap: Optional[int] = None) -> SendResult:
    """Wysyłka jednego wiersza z panelu. Kolejność kontroli: wiersz istnieje i wolno go wysłać → konfiguracja SMTP
    (`mailer()` rzuca `ConfigError`) → dzienny limit → zajęcie wiersza i wysyłka. `mailer` to fabryka, żeby
    błędy konfiguracji nie przesłaniały problemów z samym wierszem."""
    row = db.get(row_id)
    if row is None:
        raise RowNotFound(f"Nie znaleziono wiersza {row_id}.")
    problem = mail_rules.sendability_problem(row)
    if problem:
        raise NotSendable(problem)
    resolved = mailer()
    if daily_cap is not None and mail_rules.daily_quota_left(db.sent_today_count(), daily_cap) == 0:
        raise DailyCapReached(f"Dzienny limit wysyłki ({daily_cap}) jest wyczerpany.")
    return send_row(db, row_id, resolved, allowed_from=(st.STATUS_PENDING, st.STATUS_ERROR))


def send_batch(
    db: Database, mailer: Mailer, rows: list, *, delay: tuple = (45.0, 120.0),
    on_result: Optional[Callable[[dict, SendResult], None]] = None,
    sleep: Callable[[float], None] = time.sleep, jitter: Callable[[float, float], float] = random.uniform,
) -> list:
    """Wysyła wiersze (tylko „Do wysłania") jednym połączeniem SMTP, z losową przerwą `delay` (min, max) między mailami.
    Zwraca [(wiersz, SendResult)]. Ctrl+C przechodzi wyżej: wiersz w toku jest już oznaczony jako błąd przez `send_row`."""
    results = []
    with mailer.smtp_factory() as client:
        shared = replace(mailer, smtp_factory=lambda: nullcontext(client))
        for index, row in enumerate(rows):
            result = send_row(db, row["id"], shared, allowed_from=(st.STATUS_PENDING,))
            results.append((row, result))
            if on_result is not None:
                on_result(row, result)
            if index < len(rows) - 1:
                sleep(jitter(*delay))
    return results
