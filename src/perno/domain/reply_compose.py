"""Składanie odpowiedzi do firmy: temat, adresat, skrzynka, nagłówki wątku, podpis i cytat (czysta logika, bez poczty i bazy).

Odpowiedź wychodzi zawsze ze skrzynki, na którą przyszła ostatnia wiadomość firmy, i tylko na adres, z którego firma pisała
(albo na adres maila kampanii, gdy wątek ma wyłącznie nasze wiadomości). Nie ma Cc ani wolnego pola „Do”.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional, Sequence

from perno.domain import message_ids
from perno.domain import replies as rp

MAX_BODY = 20_000
MAX_SUBJECT = 300
MAX_REFERENCES = 20  # dłuższy łańcuch References nie ma sensu (serwery i tak go skracają)
MAX_QUOTE_LINES = 40
_REPLY_PREFIX = re.compile(r"^\s*(re|odp|aw|sv|ant)\s*:", re.IGNORECASE)
_ADDRESS = re.compile(r"^[^@\s<>,;\"'\\]+@[^@\s<>,;\"'\\]+\.[^@\s<>,;\"'\\]+$")
_HEADER_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")

ACK_OPTOUT = "optout"


class ComposeError(ValueError):
    """Odpowiedź nie może zostać złożona (komunikat po polsku, dla użytkownika)."""


@dataclass(frozen=True)
class Quote:
    """Cytowana wiadomość firmy pod naszą odpowiedzią."""

    sender: str
    date: Optional[datetime]
    text: str


def reply_subject(subject: str) -> str:
    """Temat odpowiedzi: ten sam z jednym „Re: ” na początku (bez „Re: Re:”); pusty temat daje samo „Re:”."""
    clean = _HEADER_UNSAFE.sub(" ", subject or "").strip()
    if _REPLY_PREFIX.match(clean):
        return clean
    return ("Re: " + clean).strip()


def check_subject(subject: object) -> str:
    """Temat do wysłania: tekst w jednej linii, niepusty, do `MAX_SUBJECT` znaków (znaki sterujące to próba wstrzyknięcia nagłówka)."""
    if not isinstance(subject, str) or not subject.strip():
        raise ComposeError("Temat nie może być pusty.")
    if _HEADER_UNSAFE.search(subject):
        raise ComposeError("Temat nie może zawierać znaków nowej linii ani sterujących.")
    if len(subject.strip()) > MAX_SUBJECT:
        raise ComposeError(f"Temat jest za długi (najwyżej {MAX_SUBJECT} znaków).")
    return subject.strip()


def check_body(body: object) -> str:
    """Treść odpowiedzi: niepusta, do `MAX_BODY` znaków, bez znaków NUL; końce linii zamienione na \\n."""
    if not isinstance(body, str):
        raise ComposeError("Treść musi być tekstem.")
    text = body.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()
    if not text:
        raise ComposeError("Treść odpowiedzi jest pusta.")
    if len(text) > MAX_BODY:
        raise ComposeError(f"Treść jest za długa (najwyżej {MAX_BODY} znaków).")
    return text


def check_address(address: object) -> str:
    """Adres adresata: jeden adres, bez nazw, przecinków i znaków sterujących."""
    value = address.strip() if isinstance(address, str) else ""
    if not value or _HEADER_UNSAFE.search(value) or not _ADDRESS.match(value):
        raise ComposeError("Adres adresata jest niepoprawny.")
    return value.lower()


def _inbound(messages: Sequence[Mapping]) -> list:
    return [m for m in messages if m["direction"] == rp.DIRECTION_IN and m["kind"] == rp.KIND_HUMAN]


def last_inbound(messages: Sequence[Mapping]) -> Optional[Mapping]:
    """Ostatnia wiadomość człowieka z firmy (listę podajemy od najstarszej)."""
    inbound = _inbound(messages)
    return inbound[-1] if inbound else None


def reply_recipient(messages: Sequence[Mapping], origin_email: str) -> str:
    """Adres, na który piszemy: nadawca ostatniej wiadomości firmy, a gdy jej nie ma, adres maila kampanii."""
    last = last_inbound(messages)
    return check_address((last["from_addr"] if last else "") or origin_email)


def reply_mailbox(thread_mailbox: str, messages: Sequence[Mapping]) -> str:
    """Skrzynka, z której wyjdzie odpowiedź: ta, na którą przyszła ostatnia wiadomość firmy; bez niej skrzynka wątku."""
    last = last_inbound(messages)
    return ((last["mailbox"] if last else "") or thread_mailbox or "").strip().lower()


def _wrap(key: str) -> str:
    return f"<{key}>"


def thread_headers(origin_message_id: str, messages: Sequence[Mapping]) -> tuple:
    """`(in_reply_to, references)` jako gotowe nagłówki (z nawiasami kątowymi): odpowiadamy na ostatnią wiadomość firmy, a
    `References` niesie cały łańcuch (nasz mail kampanii, jego odpowiedzi, nasze odpowiedzi) bez powtórzeń."""
    last = last_inbound(messages)
    chain: list = [origin_message_id] if origin_message_id else []
    for message in messages:
        chain.extend(message_ids.keys_in(message.get("references_hdr") or ""))
        if message.get("message_id"):
            chain.append(message["message_id"])
    unique = list(dict.fromkeys(key for key in chain if key))[-MAX_REFERENCES:]
    in_reply_to = (last or {}).get("message_id") or (unique[-1] if unique else "")
    return (_wrap(in_reply_to) if in_reply_to else "", " ".join(_wrap(key) for key in unique))


def attribution(quote: Quote) -> str:
    """Wiersz „W dniu … napisał(a):” nad cytatem."""
    when = quote.date.strftime("%d.%m.%Y %H:%M") if quote.date else ""
    return f"W dniu {when} {quote.sender} napisał(a):".replace("  ", " ")


def quote_text(quote: Quote) -> str:
    """Cytat w postaci tekstowej: wiersz autora i treść po „> ” (najwyżej `MAX_QUOTE_LINES` linii)."""
    lines = [line for line in quote.text.replace("\r\n", "\n").split("\n")][:MAX_QUOTE_LINES]
    return attribution(quote) + "\n" + "\n".join(("> " + line).rstrip() for line in lines)


def with_signature(body: str, sender_name: str) -> str:
    """Treść z nazwą nadawcy w osobnej linii na końcu, o ile jej tam jeszcze nie ma (po zamknięciu „Pozdrawiam” człowiek zwykle
    pisze sam, system dokłada tylko imię i nazwisko ze wspólnego ustawienia)."""
    name = sender_name.strip()
    if not name or name.lower() in body.lower():
        return body
    return f"{body}\n{name}"


def compose_text(body: str, sender_name: str, quote: Optional[Quote] = None) -> str:
    """Pełna treść tekstowa: odpowiedź, nazwa nadawcy i (opcjonalnie) cytat."""
    text = with_signature(body, sender_name)
    return text + ("\n\n" + quote_text(quote) if quote and quote.text.strip() else "")


def compose_html(text: str) -> str:
    """Prosty HTML z tekstu (wszystko zeskapowane): akapity w <div>, cytat jako <blockquote>."""
    def block(lines: list) -> str:
        return "<br>\n".join(html.escape(line) for line in lines)

    parts = text.split("\n")
    quoted = [index for index, line in enumerate(parts) if line.startswith("> ") or line == ">"]
    if not quoted:
        return f"<div>{block(parts)}</div>"
    start = quoted[0] - 1 if quoted[0] > 0 and parts[quoted[0] - 1].startswith("W dniu ") else quoted[0]
    head, tail = parts[:start], [line[2:] if line.startswith("> ") else line.lstrip(">") for line in parts[quoted[0]:]]
    attribution_line = parts[start] if start < quoted[0] else ""
    return (f"<div>{block(head)}</div>\n" + (f"<div>{html.escape(attribution_line)}</div>\n" if attribution_line else "")
            + f"<blockquote>{block(tail)}</blockquote>")


def acks_needed(optout: bool, mail_status: str) -> list:
    """Potwierdzenia, które człowiek musi dać przed wysłaniem: prośba o brak kontaktu (albo status „Nie kontaktować”)."""
    return [ACK_OPTOUT] if optout or mail_status == "nie_kontaktowac" else []
