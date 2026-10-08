"""Nagłówki wiadomości przychodzącej: odczyt do `Envelope` i rozpoznanie rodzaju (zwrot, autoodpowiedź, człowiek).

To pierwszy etap pobierania: z samych nagłówków (bez treści) wiadomości decydujemy, czy w ogóle jest nam potrzebna.
Czysta logika, tylko stdlib `email`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.message import Message
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Iterable, Optional

from perno.domain import message_ids
from perno.domain import replies as rp
from perno.domain.free_mail import email_domain
from perno.domain.reply_text import collapse

_PREFIXES = re.compile(
    r"^\s*((re|odp|sv|aw|ant|vs|wg|tr|rv|r|fw|fwd|fwd?|przekaż|wtr)(\[\d+\])?\s*:|"
    r"(automatic reply|auto(matic)?[- ]?reply|autoreply|auto|out of office|odpowiedź automatyczna|automatische antwort|"
    r"abwesenheitsnotiz|abwesenheit|respuesta automática|réponse automatique|nieobecność|poza biurem)\s*:)\s*", re.IGNORECASE)
_DAEMONS = frozenset({"mailer-daemon", "mail-daemon", "mailerdaemon", "postmaster", "mail.delivery.subsystem", "mailer_daemon"})
_BOUNCE_SUBJECT = re.compile(
    r"undeliver|delivery\s+(status\s+)?(notification|failure|failed)|mail\s+delivery\s+(failed|subsystem)|returned\s+mail|failure\s+notice|"
    r"nie\s+dostarczono|niedostarczon|nie\s+została\s+dostarczona|unzustellbar|nicht\s+zugestellt|non\s+remis|no\s+se\s+pudo\s+entregar",
    re.IGNORECASE)
_AUTO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto(matic)?[- ]?reply|autoreply|auto:|out of office|odpowiedź automatyczna|automatische antwort|"
    r"abwesenheitsnotiz|respuesta automática|réponse automatique|nieobecność|poza biurem)", re.IGNORECASE)
_AUTO_PRECEDENCE = frozenset({"auto_reply", "auto-reply", "bulk", "junk", "list"})


@dataclass(frozen=True)
class Envelope:
    """Nagłówki jednej wiadomości w postaci porównywalnej (adresy małymi literami, identyfikatory bez nawiasów)."""

    sender: str = ""
    sender_name: str = ""
    to: tuple = ()
    delivered_to: tuple = ()
    reply_to: tuple = ()
    subject: str = ""
    date: Optional[datetime] = None  # naiwny czas lokalny, jak w całej bazie
    message_id: str = ""
    in_reply_to: tuple = ()
    references: tuple = ()
    auto_submitted: str = ""
    precedence: str = ""
    autoreply_flag: bool = False  # X-Autoreply / X-Autorespond
    content_type: str = ""
    return_path: str = ""
    failed_recipients: tuple = ()

    @property
    def references_all(self) -> tuple:
        """`In-Reply-To` i `References` razem: od najbliższego przodka do najdalszego."""
        return tuple(dict.fromkeys((*self.in_reply_to, *reversed(self.references))))


def normalize_subject(subject: str) -> str:
    """Temat porównywalny: bez „Re:”, „Odp:”, „Sv:”, „AW:”, „Automatic reply:” itp. (także kilku z rzędu), małymi literami,
    ze zwiniętymi spacjami."""
    text = collapse(subject)
    while True:
        stripped = _PREFIXES.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return text.casefold()


def _addresses(values: Iterable) -> tuple:
    found = (address.strip().lower() for _, address in getaddresses([str(value) for value in values if value]) if "@" in address)
    return tuple(dict.fromkeys(found))


def _text(message: Message, name: str) -> str:
    try:
        value = message.get(name)
        return str(value) if value is not None else ""
    except (ValueError, TypeError, AttributeError, LookupError):  # nagłówek w zepsutym kodowaniu nie zatrzymuje pobierania
        return ""


def _moment(value: str) -> Optional[datetime]:
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def envelope_from_message(message: Message) -> Envelope:
    """Nagłówki z już sparsowanej wiadomości (wspólne dla etapu nagłówków i pełnej treści)."""
    senders = getaddresses([_text(message, "From")])
    sender_name, sender = next(((name, address.strip().lower()) for name, address in senders if "@" in address), ("", ""))
    content_type = _text(message, "Content-Type").lower().replace("\n", " ").replace("\r", "")
    return Envelope(
        sender=sender, sender_name=collapse(sender_name)[:200],
        to=_addresses(message.get_all("To", []) + message.get_all("Cc", [])),
        delivered_to=_addresses(message.get_all("Delivered-To", [])),
        reply_to=_addresses(message.get_all("Reply-To", [])),
        subject=collapse(_text(message, "Subject"))[:500],
        date=_moment(_text(message, "Date")),
        message_id=message_ids.key(_text(message, "Message-ID")),
        in_reply_to=tuple(message_ids.keys_in(_text(message, "In-Reply-To"))),
        references=tuple(message_ids.keys_in(_text(message, "References"))),
        auto_submitted=_text(message, "Auto-Submitted").strip().lower(),
        precedence=_text(message, "Precedence").strip().lower(),
        autoreply_flag=bool(_text(message, "X-Autoreply") or _text(message, "X-Autorespond")),
        content_type=collapse(content_type),
        return_path=next(iter(_addresses([_text(message, "Return-Path")])), ""),
        failed_recipients=_addresses(message.get_all("X-Failed-Recipients", [])),
    )


def parse_envelope(raw_headers: bytes) -> Envelope:
    """Nagłówki z surowych bajtów (wynik pobrania samych nagłówków)."""
    return envelope_from_message(BytesHeaderParser(policy=policy.default).parsebytes(raw_headers))


# ---- rodzaj wiadomości ------------------------------------------------------------------------

def looks_like_bounce(env: Envelope) -> bool:
    """Zwrot poczty: nadawca to daemon pocztowy albo raport doręczenia; temat „Undelivered…” liczy się tylko przy
    wiadomości wygenerowanej automatycznie (sam temat łatwo podrobić zwykłym mailem)."""
    if env.sender.partition("@")[0] in _DAEMONS or bool(env.failed_recipients):
        return True
    if "multipart/report" in env.content_type and "delivery-status" in env.content_type:
        return True
    return env.auto_submitted.startswith("auto-generated") and bool(_BOUNCE_SUBJECT.search(env.subject))


def looks_like_auto_reply(env: Envelope) -> bool:
    """Autoodpowiedź (nieobecność, potwierdzenie przyjęcia zgłoszenia): nagłówki automatu albo typowy temat."""
    if env.auto_submitted not in ("", "no") or env.autoreply_flag:
        return True
    return env.precedence in _AUTO_PRECEDENCE or bool(_AUTO_SUBJECT.match(env.subject))


def kind_of(env: Envelope) -> str:
    """Rodzaj wiadomości z samych nagłówków: zwrot, autoodpowiedź albo człowiek."""
    if looks_like_bounce(env):
        return rp.KIND_BOUNCE
    return rp.KIND_AUTO if looks_like_auto_reply(env) else rp.KIND_HUMAN


def is_ignored(env: Envelope, own: Iterable[str], ignore: Iterable[str]) -> bool:
    """Czy nadawca to my sami (nasze skrzynki) albo adres/domena z listy ignorowanych (np. przekazania wewnętrzne)."""
    own_set = {address.strip().lower() for address in own}
    rules = {rule.strip().lower() for rule in ignore if rule.strip()}
    domain = email_domain(env.sender)
    return bool(env.sender) and (env.sender in own_set or env.sender in rules or (bool(domain) and domain in rules))
