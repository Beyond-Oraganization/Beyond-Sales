"""Pełna wiadomość przychodząca: treść, załączniki (tylko opis) i dane zwrotu poczty (czysta logika, stdlib `email`).

To drugi etap pobierania: wołany tylko dla wiadomości, które etap nagłówków uznał za odpowiedź albo zwrot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import Message
from typing import Any, Optional

from perno.domain import message_ids
from perno.domain import replies as rp
from perno.domain.reply_headers import Envelope, envelope_from_message, kind_of
from perno.domain.reply_text import body_text, collapse, strip_unsafe

MAX_BODY = 200_000  # znaków treści zapisywanych w bazie (dłuższe wiadomości to zwykle zapisy rozmów albo śmieci)
MAX_ATTACHMENTS = 20
_ADDRESS = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_STATUS = re.compile(r"\b([245])\.\d{1,3}\.\d{1,3}\b")
_HARD_PHRASES = re.compile(
    r"user\s+unknown|no\s+such\s+user|does\s+not\s+exist|doesn'?t\s+exist|mailbox\s+(unavailable|not\s+found|does\s+not)|"
    r"unknown\s+recipient|invalid\s+recipient|recipient\s+(address\s+)?rejected|address\s+rejected|account\s+(is\s+)?disabled|"
    r"nie\s+istnieje|nie\s+znaleziono|nieznany\s+odbiorca|unbekannter\s+empfänger|no\s+existe", re.IGNORECASE)


@dataclass(frozen=True)
class Attachment:
    """Opis załącznika (sam opis: pliku nie pobieramy ani nie otwieramy)."""

    name: str
    content_type: str
    size: int


@dataclass(frozen=True)
class BounceInfo:
    """Co wiadomo o zwrocie: których adresów dotyczy, czy trwale, i (jeśli serwer dołączył oryginał) jego Message-ID."""

    recipients: tuple
    hard: bool
    status: str = ""
    original_message_id: str = ""
    guessed: bool = False  # adresy wyłuskane z tekstu, bo wiadomość nie ma raportu doręczenia

    @property
    def kind(self) -> str:
        return rp.BOUNCE_HARD if self.hard else rp.BOUNCE_SOFT


@dataclass(frozen=True)
class ParsedMessage:
    """Wiadomość po pełnym odczycie: nagłówki, rodzaj, czysty tekst, załączniki i (dla zwrotu) dane raportu."""

    envelope: Envelope
    kind: str
    text: str
    attachments: tuple
    bounce: Optional[BounceInfo] = None


def _decode(part: Any) -> str:
    try:
        content = part.get_content()
        return content if isinstance(content, str) else ""
    except (LookupError, ValueError, AttributeError, KeyError, TypeError):  # nieznane kodowanie: bierzemy bajty „na ślepo”
        payload = part.get_payload(decode=True)
        return payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""


def _outer_parts(message: Message) -> list:
    """Części wiadomości z pominięciem tego, co leży wewnątrz dołączonych wiadomości (`message/rfc822`): treść i załączniki
    przekazanego maila nie są treścią ani załącznikami samej odpowiedzi."""
    inside: set = set()
    kept = []
    for part in message.walk():
        if id(part) in inside:
            continue
        kept.append(part)
        if part.get_content_type() == "message/rfc822":
            inside.update(id(child) for child in part.walk() if child is not part)
    return kept


def _body_parts(message: Message) -> tuple:
    plain = html = ""
    for part in _outer_parts(message):
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        kind = part.get_content_type()
        if kind == "text/plain" and not plain:
            plain = _decode(part)
        elif kind == "text/html" and not html:
            html = _decode(part)
    return plain, html


def _attachments(message: Message) -> tuple:
    found = []
    for part in _outer_parts(message):
        attached_message = part.get_content_type() == "message/rfc822"
        if part.is_multipart() and not attached_message:
            continue
        name = part.get_filename()
        if name is None and part.get_content_disposition() != "attachment":
            continue
        payload = None if part.is_multipart() else part.get_payload(decode=True)
        found.append(Attachment(collapse(name or "bez nazwy")[:120], part.get_content_type(), len(payload) if isinstance(payload, bytes) else 0))
        if len(found) >= MAX_ATTACHMENTS:
            break
    return tuple(found)


# ---- zwroty ------------------------------------------------------------------------------------

def _address_of(value: str) -> str:
    found = _ADDRESS.search(value or "")
    return found.group(0).lower() if found else ""


def _delivery_blocks(message: Message) -> list:
    blocks: list = []
    for part in message.walk():
        if part.get_content_type() == "message/delivery-status":
            payload = part.get_payload()
            blocks.extend(payload if isinstance(payload, list) else [])
    return blocks


def _original_message_id(message: Message) -> str:
    """Message-ID oryginału, który serwer dołączył do zwrotu (cały mail albo same nagłówki); pusty, gdy go nie ma."""
    for part in message.walk():
        kind = part.get_content_type()
        found = ""
        if kind == "message/rfc822":
            inner = part.get_payload()
            if isinstance(inner, list) and inner and isinstance(inner[0], Message):
                found = message_ids.key(str(inner[0].get("Message-ID", "")))
        elif kind == "text/rfc822-headers":
            match = re.search(r"(?im)^message-id:\s*(<[^>\s]+>)", _decode(part))
            found = message_ids.key(match.group(1)) if match else ""
        if found:
            return found
    return ""


def _from_report(blocks: list) -> tuple:
    recipients, hard, soft, status = [], False, False, ""
    for block in blocks:
        address = _address_of(str(block.get("Final-Recipient", "") or block.get("Original-Recipient", "")))
        if not address:
            continue
        recipients.append(address)
        code = str(block.get("Status", "")).strip()
        action = str(block.get("Action", "")).strip().lower()
        if action == "failed" or code.startswith("5"):
            hard, status = True, status or code
        elif action in ("delayed", "relayed", "delivered") or code.startswith("4"):
            soft, status = True, status or code
    return tuple(dict.fromkeys(recipients)), hard, soft, status


def parse_bounce(message: Message, envelope: Envelope, text: str) -> BounceInfo:
    """Dane zwrotu: z raportu doręczenia, a gdy go nie ma, z nagłówka `X-Failed-Recipients` albo z adresów w tekście."""
    recipients, hard, soft, status = _from_report(_delivery_blocks(message))
    guessed = False
    if not recipients:
        recipients = envelope.failed_recipients
    if not recipients:
        recipients, guessed = tuple(dict.fromkeys(a.lower() for a in _ADDRESS.findall(text))), True
    if not (hard or soft):
        code = _STATUS.search(text)
        hard = bool(_HARD_PHRASES.search(text)) or bool(code and code.group(1) == "5")
        status = code.group(0) if code else ""
    return BounceInfo(recipients, hard, status, _original_message_id(message), guessed)


def parse_message(raw: bytes) -> ParsedMessage:
    """Wiadomość z surowych bajtów. Nie rzuca wyjątku na zepsutej treści: kodowania i MIME bywają uszkodzone."""
    message = message_from_bytes(raw, policy=policy.default)
    envelope = envelope_from_message(message)
    plain, html = _body_parts(message)
    text = body_text(strip_unsafe(plain), strip_unsafe(html))[:MAX_BODY]
    kind = kind_of(envelope)
    bounce = parse_bounce(message, envelope, text) if kind == rp.KIND_BOUNCE else None
    return ParsedMessage(envelope, kind, text, _attachments(message), bounce)
