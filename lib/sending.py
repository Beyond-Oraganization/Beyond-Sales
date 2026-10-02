"""Budowa wiadomości i wysyłka pojedynczego wiersza (wspólne dla CLI i panelu).

SMTP jest wstrzykiwany (`smtp_factory`), dzięki czemu testy używają atrapy i
nigdy nie wysyłają prawdziwych maili.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable, Mapping, Optional

from lib import config as cfg
from lib import image_prep, mime_builder, smtp_client
from lib import statuses as st
from lib.config import ConfigError
from lib.db import DB_ERRORS, Database


def real_smtp_allowed(environ: Mapping = os.environ) -> bool:
    return not environ.get("PERNO_DB_URL") or environ.get("PERNO_ALLOW_REAL_SMTP") == "1"


def smtp_factory_from_config(config: Mapping) -> Callable:
    if not real_smtp_allowed():
        raise ConfigError(
            "Ustawiono PERNO_DB_URL (baza testowa/demo), więc prawdziwa wysyłka SMTP jest zablokowana. "
            "Jeśli naprawdę chcesz wysyłać, ustaw też PERNO_ALLOW_REAL_SMTP=1."
        )

    def factory():
        return smtp_client.SmtpClient(
            config["SMTP_HOST"], int(config["SMTP_PORT"]), config["SMTP_USER"],
            config["SMTP_PASSWORD"], config.get("SMTP_USE_SSL", "true").lower() == "true",
        )

    return factory


def get_inline_image() -> tuple:
    return image_prep.prepare_inline_image(cfg.INLINE_IMAGE_SRC, cfg.INLINE_IMAGE_CACHE_BASE)


def build_message_for_row(row: Mapping, to_addr: str, from_addr: str, from_name: str) -> EmailMessage:
    cid = mime_builder.INLINE_IMAGE_CID
    inline_images = []
    if f"cid:{cid}" in row["body_html"]:
        data, subtype = get_inline_image()
        inline_images.append((cid, data, subtype))
    return mime_builder.build_message(
        from_addr=from_addr, from_name=from_name, to_addr=to_addr,
        subject=row["subject"], html_body=row["body_html"], text_body=row["body_text"],
        inline_images=inline_images,
    )


@dataclass
class SendResult:
    outcome: str  # "sent" | "error" | "skipped" | "sent_unrecorded"
    row: Optional[dict]
    message: str = ""


def send_row(
    db: Database, row_id: str, *, smtp_factory: Callable, config: Mapping, sender_name: str,
    allowed_from: tuple = (st.STATUS_PENDING, st.STATUS_ERROR),
) -> SendResult:
    claimed = db.claim_for_sending(row_id, allowed_from)
    if claimed is None:
        return SendResult("skipped", db.get(row_id),
                          "zmieniony status lub brak e-maila/treści — nic nie wysłano")
    try:
        message = build_message_for_row(claimed, claimed["email"], config["SMTP_USER"], sender_name)
        with smtp_factory() as client:
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
    print(warning, file=sys.stderr)
    return SendResult("sent_unrecorded", claimed, warning)
