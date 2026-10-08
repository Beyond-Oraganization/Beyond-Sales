"""Budowa wiadomości e-mail (text/plain + text/html z osadzonym obrazem cid)."""

from __future__ import annotations

import mimetypes
import os
from email.message import EmailMessage
from typing import Sequence, cast

INLINE_IMAGE_CID = "ux_w_lesie_screenshot"


def build_message(
    *,
    from_addr: str,
    from_name: str,
    to_addr: str,
    subject: str,
    html_body: str,
    text_body: str,
    inline_images: Sequence = (),
    attachment_paths: Sequence = (),
) -> EmailMessage:
    """`inline_images`: iterable of (cid, bytes, subtype) osadzanych w treści
    HTML przez Content-ID. `attachment_paths`: zwykłe załączniki (pusty
    w wersji 1 — hak na przyszłość, obraz Eventy jest osadzany, nie
    dołączany jako osobny plik)."""
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject

    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    html_part = cast(list, msg.get_payload())[1]  # multipart/alternative: [text/plain, text/html]
    for cid, data, subtype in inline_images:
        html_part.add_related(data, maintype="image", subtype=subtype, cid=f"<{cid}>")

    for path in attachment_paths:
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(
                f.read(), maintype=maintype, subtype=subtype,
                filename=os.path.basename(path),
            )
    return msg


_THREAD_HEADERS = {"message_id": "Message-ID", "date": "Date", "in_reply_to": "In-Reply-To", "references": "References"}


def add_thread_headers(message: EmailMessage, *, message_id: str = "", date: str = "", in_reply_to: str = "",
                       references: str = "") -> EmailMessage:
    """Dopisuje do gotowej wiadomości nagłówki potrzebne do dopasowania odpowiedzi; puste wartości są pomijane (wtedy
    `Message-ID` i `Date` dopisze serwer SMTP, ale my nie poznamy identyfikatora wysłanego maila)."""
    values = {"message_id": message_id, "date": date, "in_reply_to": in_reply_to, "references": references}
    for key, value in values.items():
        if value:
            message[_THREAD_HEADERS[key]] = value
    return message
