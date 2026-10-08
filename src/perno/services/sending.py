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
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate
from typing import Callable, Collection, Mapping, Optional, Sequence

from perno import settings as cfg
from perno.db import DB_ERRORS, Database, RowNotFound
from perno.domain import company_rules, mail_rules, mailboxes, message_ids
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


def _require_real_smtp(env: Optional[Environment]) -> None:
    if not (env if env is not None else Environment.from_environ()).real_smtp_allowed:
        raise ConfigError(
            "Ustawiono PERNO_DB_URL (baza testowa/demo), więc prawdziwa wysyłka SMTP jest zablokowana. "
            "Jeśli naprawdę chcesz wysyłać, ustaw też PERNO_ALLOW_REAL_SMTP=1."
        )


def _factory_for(smtp: SmtpSettings) -> Callable:
    def factory() -> smtp_client.SmtpClient:
        return smtp_client.SmtpClient(smtp.host, smtp.port, smtp.user, smtp.password, smtp.use_ssl)

    return factory


def smtp_factory_from_config(config: Mapping, env: Optional[Environment] = None) -> Callable:
    """Fabryka klienta SMTP skrzynki głównej; odmawia, gdy baza jest testowa (PERNO_DB_URL) bez jawnego odblokowania."""
    _require_real_smtp(env)
    return _factory_for(SmtpSettings.from_config(config))


def build_mailer(config: Mapping, config_path: str = cfg.DEFAULT_CONFIG, *,
                 smtp_factory: Optional[Callable] = None, env: Optional[Environment] = None) -> Mailer:
    """Skrzynka główna z konfiguracji (rzuca `ConfigError`, gdy dane są niekompletne). `smtp_factory` podmienia klienta SMTP.
    Do wysyłki w rotacji służy `build_mailers`; tu zostają powiadomienia, test połączenia i terminal."""
    require_smtp(dict(config), config_path)
    sender = Sender(config["SMTP_USER"], config.get("SENDER_NAME", ""))
    return Mailer(smtp_factory or smtp_factory_from_config(config, env), sender)


def build_mailers(config: Mapping, config_path: str = cfg.DEFAULT_CONFIG, *,
                  smtp_factory: Optional[Callable] = None, env: Optional[Environment] = None) -> list:
    """Wszystkie skrzynki do rotacji w kolejności z konfiguracji (pierwsza to główna). Rzuca `ConfigError`, gdy któraś jest
    niekompletna albo dwie mają ten sam adres. `smtp_factory` (testy) obsługuje wtedy każdą skrzynkę."""
    require_smtp(dict(config), config_path)
    slots = cfg.smtp_slots(config)
    if smtp_factory is None:
        _require_real_smtp(env)
    name = config.get("SENDER_NAME", "")
    return [Mailer(smtp_factory or _factory_for(slot), Sender(slot.user, name)) for slot in slots]


def mailbox_usage(db: Database, addresses: Sequence[str], now: Optional[datetime] = None) -> list:
    """Ile maili każda skrzynka wysłała dziś: `[{"address", "sent_today"}]` w kolejności konfiguracji (do paneli)."""
    if not addresses:
        return []
    use = mailboxes.attribute_legacy(db.sent_by_sender_today(now), addresses[0])
    return [{"address": address, "sent_today": use.get(address.casefold(), mailboxes.DayUse()).sent} for address in addresses]


def pick_mailer(db: Database, mailers: Sequence[Mailer], daily_cap: Optional[int], *, now: Optional[datetime] = None,
                unavailable: Collection[str] = ()) -> Optional[Mailer]:
    """Skrzynka, z której pójdzie następny mail: po kolei, z dziennym limitem osobnym dla każdej (None = bez limitu).
    Zwraca None, gdy każda skrzynka wyczerpała limit albo jest wyłączona (`unavailable`: adresy)."""
    if not mailers:
        return None
    use = mailboxes.attribute_legacy(db.sent_by_sender_today(now), mailers[0].sender.address)
    address = mailboxes.choose_mailbox([m.sender.address for m in mailers], use, daily_cap, unavailable)
    return next((m for m in mailers if m.sender.address == address), None)


def get_inline_image() -> tuple:
    """Obraz osadzany w treści maila (przeskalowany, z cache)."""
    return image_prep.prepare_inline_image(cfg.INLINE_IMAGE_SRC, cfg.INLINE_IMAGE_CACHE_BASE)


def build_message_for_row(row: Mapping, to_addr: str, sender: Sender, message_id: str = "") -> EmailMessage:
    """Buduje wiadomość MIME z wiersza; obraz jest osadzany tylko wtedy, gdy treść go używa. Z `message_id` mail dostaje
    własny `Message-ID` i `Date` (po nich dopasowujemy odpowiedzi); bez niego nagłówki dopisze serwer SMTP."""
    cid = mime_builder.INLINE_IMAGE_CID
    inline_images = []
    if f"cid:{cid}" in row["body_html"]:
        data, subtype = get_inline_image()
        inline_images.append((cid, data, subtype))
    message = mime_builder.build_message(
        from_addr=sender.address, from_name=sender.name, to_addr=to_addr,
        subject=row["subject"], html_body=row["body_html"], text_body=row["body_text"],
        inline_images=inline_images,
    )
    return mime_builder.add_thread_headers(message, message_id=message_id, date=formatdate(localtime=True) if message_id else "")


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
    message_id = message_ids.new_message_id(mailer.sender.address)
    try:
        message = build_message_for_row(claimed, claimed["email"], mailer.sender, message_id)
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
            return SendResult("sent", db.mark_sent(row_id, mailer.sender.address, message_ids.key(message_id)))
        except DB_ERRORS:
            time.sleep(0.5 * (attempt + 1))
    warning = (f"UWAGA: mail do {claimed['email']} ({row_id}) WYSŁANO, ale nie udało się zapisać statusu "
               "w bazie. Wiersz zostanie oznaczony jako błąd po 10 min — nie wysyłaj go ponownie.")
    logger.error(warning)
    return SendResult("sent_unrecorded", claimed, warning)


def send_one(db: Database, row_id: str, mailers: Callable[[], list], *, daily_cap: Optional[int] = None) -> SendResult:
    """Wysyłka jednego wiersza z panelu. Kolejność kontroli: wiersz istnieje i wolno go wysłać → konfiguracja SMTP
    (`mailers()` rzuca `ConfigError`) → dzienny limit skrzynki → zajęcie wiersza i wysyłka z kolejnej skrzynki w rotacji.
    `mailers` to fabryka, żeby błędy konfiguracji nie przesłaniały problemów z samym wierszem."""
    row = db.get(row_id)
    if row is None:
        raise RowNotFound(f"Nie znaleziono wiersza {row_id}.")
    problem = mail_rules.sendability_problem(row)
    if problem:
        raise NotSendable(problem)
    resolved = mailers()
    chosen = pick_mailer(db, resolved, daily_cap)
    if chosen is None:
        where = " na każdej skrzynce" if len(resolved) > 1 else ""
        raise DailyCapReached(f"Dzienny limit wysyłki ({daily_cap}) jest wyczerpany{where}.")
    return send_row(db, row_id, chosen, allowed_from=(st.STATUS_PENDING, st.STATUS_ERROR))


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
