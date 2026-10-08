"""Odpowiedzi do firm z panelu: widok edytora, szkic i wysyłka ze skrzynki wątku.

To **osobna** ścieżka od wysyłki kampanii (`sending.send_row`): odpowiedź nie jest wierszem kolejki, nie liczy się do limitów
Automatu i nie zmienia statusu maila. Reguły: tylko człowiek (żadnej drogi z `ai.py` ani CLI), jeden adresat z wątku, skrzynka
to ta, na którą przyszła wiadomość firmy (bez rotacji), szkic jest przejmowany atomowo (podwójne kliknięcie nie wyśle dwa razy),
przy prośbie o brak kontaktu trzeba potwierdzić. Po wysłaniu kopia trafia do folderu Wysłane tej samej skrzynki (jeśli ma IMAP).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import formatdate
from typing import Any, Callable, Mapping, Optional, Sequence

from perno import settings as cfg
from perno.db import DB_ERRORS
from perno.db.replies_store import ReplyStore
from perno.domain import message_ids, reply_text
from perno.domain import replies as rp
from perno.domain import reply_compose as rc
from perno.domain.imap_folders import ROLE_SENT
from perno.mailer import mime_builder
from perno.services.sending import Mailer

logger = logging.getLogger(__name__)


class SendRefused(ValueError):
    """Odpowiedzi nie wolno wysłać (komunikat po polsku, dla użytkownika)."""


class AckRequired(Exception):
    """Wysyłka wymaga potwierdzenia ostrzeżeń (np. prośba o brak kontaktu). Niesie ich kody."""

    def __init__(self, codes: Sequence[str]):
        super().__init__("Potwierdź ostrzeżenie przed wysłaniem.")
        self.codes = list(codes)


class SendFailed(RuntimeError):
    """Serwer SMTP odrzucił wysyłkę albo połączenie zawiodło; nic nie wyszło, szkic został do ponowienia."""


def _inbound_context(detail: Mapping) -> tuple:
    messages = detail["messages"]
    thread, origin = detail["thread"], detail["origin"] or {}
    return thread, origin, messages, rc.last_inbound(messages)


def compose_view(detail: Mapping, config: Mapping, draft: Optional[Mapping]) -> dict:
    """Co pokazać w edytorze wątku: czy wolno odpowiadać, od kogo, do kogo, jaki temat, ostrzeżenia i szkic."""
    thread, origin, messages, inbound = _inbound_context(detail)
    if inbound is None:
        return {"can_reply": False, "reason": "W tym wątku nie ma odpowiedzi człowieka."}
    try:
        recipient = rc.reply_recipient(messages, origin.get("email") or "")
    except rc.ComposeError as exc:
        return {"can_reply": False, "reason": str(exc)}
    mailbox = rc.reply_mailbox(thread["mailbox"], messages)
    configured = {box.user.lower() for box in cfg.configured_mailboxes(config)}
    return {
        "can_reply": True, "mailbox": mailbox, "mailbox_ok": mailbox in configured,
        "mailbox_problem": "" if mailbox in configured else f"Skrzynka {mailbox or '(nieznana)'} nie jest skonfigurowana (SMTP), więc nie da się z niej odpisać.",
        "to": recipient, "subject": rc.reply_subject(inbound["subject"] or origin.get("subject") or ""),
        "needs_ack": rc.acks_needed(bool(thread["optout"]), thread["mail_status"] or ""), "signature": config.get("SENDER_NAME", ""),
        "draft": ({"id": draft["id"], "body": draft["body_text"] or "", "author": draft["author"], "state": draft["state"], "error": draft["error"] or ""}
                  if draft else None),
    }


def save_draft(store: ReplyStore, config: Mapping, thread_id: str, body: object, *, author: str, now: datetime, keep_human: bool = False) -> dict:
    """Zapisuje szkic odpowiedzi (treść z edytora albo od AI). Zwraca `{saved, id}`; `saved=False`, gdy szkic człowieka chroni
    się przed AI albo właśnie trwa wysyłka."""
    text = rc.check_body(body)
    detail = store.thread_detail(thread_id)
    if detail is None:
        raise SendRefused(f"Nie ma wątku {thread_id}.")
    view = compose_view(detail, config, None)
    if not view["can_reply"]:
        raise SendRefused(view["reason"])
    draft_id = store.save_draft(thread_id, body=text, subject=view["subject"], to_addr=view["to"], mailbox=view["mailbox"], author=author,
                                now=now, keep_human=keep_human)
    return {"saved": draft_id is not None, "id": draft_id}


def _quote_of(inbound: Mapping) -> Optional[rc.Quote]:
    split = reply_text.split_quote(inbound.get("body_text") or "")
    text = split.new or inbound.get("body_text") or ""
    sender = f"{inbound['from_name']} <{inbound['from_addr']}>" if inbound.get("from_name") else inbound["from_addr"] or ""
    date = inbound["date_at"] if isinstance(inbound.get("date_at"), datetime) else None
    return rc.Quote(sender, date, text) if text.strip() else None


def _build(mailer: Mailer, *, to: str, subject: str, text: str, headers: tuple, message_id: str) -> Any:
    message = mime_builder.build_message(from_addr=mailer.sender.address, from_name=mailer.sender.name, to_addr=to, subject=subject,
                                         html_body=rc.compose_html(text), text_body=text)
    return mime_builder.add_thread_headers(message, message_id=message_id, date=formatdate(localtime=True), in_reply_to=headers[0], references=headers[1])


def _append_to_sent(config: Mapping, factory: Optional[Callable], mailbox: str, message: Any, now: datetime) -> str:
    """Kopia wysłanej odpowiedzi do folderu Wysłane tej samej skrzynki (jedyny zapis na serwerze poczty). Zwraca ostrzeżenie
    albo pusty tekst; błąd kopii nigdy nie unieważnia wysłanej wiadomości."""
    if factory is None:
        return "Kopii nie zapisano w folderze Wysłane (odbiór poczty jest tu wyłączony)."
    try:
        slot = next((box for box in cfg.imap_slots(config) if box.address.lower() == mailbox), None)
        if slot is None:
            return "Kopii nie zapisano w folderze Wysłane (ta skrzynka nie ma ustawionego IMAP)."
        with factory(slot) as imap:
            folders = imap.list_folders()
            sent = next((folder.name for folder in folders if folder.role == ROLE_SENT), "")
            if not sent:
                return "Kopii nie zapisano: nie znaleziono folderu Wysłane."
            imap.append(sent, message.as_bytes(), now)
    except Exception as exc:  # noqa: BLE001 — kopia jest dodatkiem po udanej wysyłce: każdy błąd tylko ostrzega
        logger.warning("Kopia odpowiedzi do Wysłanych nie powiodła się: %s", type(exc).__name__)
        return f"Wysłano, ale kopii nie zapisano w folderze Wysłane ({type(exc).__name__})."
    return ""


def _record(store: ReplyStore, draft: Mapping, sent: rp.NewMessage, now: datetime) -> bool:
    for attempt in range(3):
        try:
            return store.finish_sent(draft["id"], sent, now)
        except DB_ERRORS:
            time.sleep(0.5 * (attempt + 1))
    return False


@dataclass(frozen=True)
class SendOptions:
    """Jak wysłać: potwierdzone ostrzeżenia, czy dołączyć cytat, czy zamknąć wątek i skąd wziąć sesję IMAP do kopii w Wysłanych."""

    acks: Sequence[str] = ()
    quote: bool = True
    close: bool = False
    imap_factory: Optional[Callable] = None


@dataclass(frozen=True)
class _Plan:
    view: Mapping
    detail: Mapping
    draft: Mapping
    mailer: Mailer
    body: str
    text: str
    headers: tuple


def _plan(store: ReplyStore, config: Mapping, mailers: Sequence[Mailer], thread_id: str, options: SendOptions) -> _Plan:
    """Sprawdza, czy wolno wysłać, i składa wszystko, co potrzebne (nic jeszcze nie zmienia)."""
    detail = store.thread_detail(thread_id)
    draft = store.get_draft(thread_id)
    if detail is None or draft is None or not (draft["body_text"] or "").strip():
        raise SendRefused("Nie ma szkicu odpowiedzi do wysłania.")
    view = compose_view(detail, config, draft)
    if not view["can_reply"]:
        raise SendRefused(view["reason"])
    missing = [code for code in view["needs_ack"] if code not in options.acks]
    if missing:
        raise AckRequired(missing)
    mailer = next((m for m in mailers if m.sender.address.lower() == view["mailbox"]), None)
    if mailer is None:
        raise SendRefused(view["mailbox_problem"] or "Nie ma skrzynki, z której można odpisać.")
    _, origin, messages, inbound = _inbound_context(detail)
    body = rc.check_body(draft["body_text"])
    text = rc.compose_text(body, mailer.sender.name, _quote_of(inbound) if options.quote and inbound else None)
    return _Plan(view, detail, draft, mailer, body, text, rc.thread_headers(origin.get("message_id") or "", messages))


def _deliver(store: ReplyStore, plan: _Plan) -> tuple:
    """Przejmuje szkic i wysyła; zwraca (wiadomość, jej Message-ID). Błąd SMTP oddaje szkic do edycji i rzuca `SendFailed`."""
    if not store.claim_draft(plan.draft["id"]):
        raise SendRefused("Ta odpowiedź jest już wysyłana albo została wysłana.")
    message_id = message_ids.new_message_id(plan.view["mailbox"])
    message = _build(plan.mailer, to=plan.view["to"], subject=plan.view["subject"], text=plan.text, headers=plan.headers, message_id=message_id)
    try:
        with plan.mailer.smtp_factory() as client:
            client.send(message)
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        store.release_draft(plan.draft["id"], reason)
        raise SendFailed(f"Nie udało się wysłać: {reason}") from exc
    return message, message_id


def send_reply(store: ReplyStore, config: Mapping, mailers: Sequence[Mailer], thread_id: str, options: SendOptions, now: datetime) -> dict:
    """Wysyła szkic wątku. Zwraca `{sent, message_id, warning}`. Rzuca `SendRefused` (nie wolno), `AckRequired` (potwierdź),
    `SendFailed` (SMTP; szkic zostaje do ponowienia)."""
    plan = _plan(store, config, mailers, thread_id, options)
    message, message_id = _deliver(store, plan)
    key = message_ids.key(message_id)
    view, thread = plan.view, plan.detail["thread"]
    sent = rp.NewMessage(
        direction=rp.DIRECTION_OUT, kind=rp.KIND_HUMAN, mailbox=view["mailbox"], identity=key, message_id=key, from_addr=view["mailbox"],
        to_addr=view["to"], subject=view["subject"], date_at=now, body_text=rc.with_signature(plan.body, plan.mailer.sender.name),
        snippet=reply_text.snippet(plan.body), in_reply_to=message_ids.key(plan.headers[0]), references=" ".join(message_ids.keys_in(plan.headers[1])),
        thread_id=thread_id, company_id=thread.get("company_id"), state=rp.STATE_SENT, author=plan.draft["author"] or rp.BY_HUMAN,
        match_method=rp.MATCH_MANUAL, thread_mailbox=view["mailbox"])
    warnings = [] if _record(store, plan.draft, sent, now) else [
        "UWAGA: odpowiedź WYSŁANO, ale nie udało się zapisać jej w bazie. Nie wysyłaj jej ponownie; sprawdź folder Wysłane."]
    if options.close:
        store.update_thread(thread_id, closed_at=now)
    if config.get("REPLIES_SAVE_SENT", "true").strip().lower() != "false":
        warnings.append(_append_to_sent(config, options.imap_factory, view["mailbox"], message, now))
    return {"sent": True, "message_id": key, "warning": " ".join(part for part in warnings if part)}
