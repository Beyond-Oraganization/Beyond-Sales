"""Odpowiedzi w panelu: lista, widok wątku, ocena i stan wątku (usługa nad magazynem, bez SQL).

Każda operacja zmieniająca coś, co człowiek zobaczy, jest odwracalna po stronie panelu („Cofnij”): odpowiedzi zawierają
poprzednią wartość. Ocena AI nigdy nie nadpisuje oceny człowieka, a status maila zmienia wyłącznie człowiek.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Optional

from perno import settings as cfg
from perno.db import Database
from perno.db.replies_store import ReplyStore
from perno.domain import replies as rp
from perno.domain import reply_listing as rl
from perno.domain import reply_rules, reply_text
from perno.domain import statuses as st


class ThreadNotFound(LookupError):
    """Nie ma takiego wątku albo wiadomości."""


def iso(value: Any) -> Any:
    """Data jako tekst ISO (do JSON); inne wartości bez zmian."""
    return value.strftime("%Y-%m-%dT%H:%M:%S") if isinstance(value, datetime) else value


def _row(row: Mapping) -> dict:
    """Wiersz listy w JSON: daty jako tekst, flagi jako prawdziwe wartości logiczne, plus pola wyliczone dla widoku."""
    out: dict[str, Any] = {key: iso(value) for key, value in row.items()}
    for flag in ("optout", "awaiting_reply", "weak", "other_mailbox", "has_bounce", "has_auto"):
        out[flag] = bool(row.get(flag))
    out["unread"] = row.get("read_at") is None and bool(row.get("human_in_count"))
    out["closed"] = row.get("closed_at") is not None
    return out


def list_replies(store: ReplyStore, payload: Mapping) -> dict:
    """Strona wybranej zakładki z licznikami wszystkich zakładek: `{rows, total, page, size, pages, counts, tab}`."""
    request = rl.parse_list_request(payload)
    rows, total = store.list_page(request.flt, request.page, request.size)
    converted = [_unassigned(row) for row in rows] if request.flt.tab == rl.TAB_UNASSIGNED else [_row(row) for row in rows]
    return {"tab": request.flt.tab, "rows": converted, "total": total, "page": request.page, "size": request.size,
            "pages": max(1, -(-total // request.size)), "counts": store.tab_counts(request.flt)}


def _unassigned(row: Mapping) -> dict:
    return {**{key: iso(value) for key, value in row.items()}, "unassigned": True}


def _attachments(raw: Optional[str]) -> list:
    try:
        items = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _message(row: Mapping) -> dict:
    split = reply_text.split_quote(row["body_text"] or "")
    return {"id": row["id"], "direction": row["direction"], "kind": row["kind"], "state": row["state"], "author": row["author"],
            "mailbox": row["mailbox"], "from_addr": row["from_addr"], "from_name": row["from_name"], "to_addr": row["to_addr"],
            "subject": row["subject"], "date": iso(row["date_at"]), "new": split.new, "quoted": split.quoted,
            "attachments": _attachments(row["attachments"]), "match_method": row["match_method"], "match_weak": bool(row["match_weak"]),
            "bounce_type": row["bounce_type"], "optout_hint": bool(row["optout_hint"]), "error": row["error"]}


def thread_view(store: ReplyStore, thread_id: str) -> dict:
    """Wszystko do widoku wątku: wiersz listy, nasz mail kampanii, wiadomości (z cytatem rozdzielonym od nowego tekstu), historia
    ocen, podpowiedź statusu i statusy, które można ustawić jednym kliknięciem."""
    detail = store.thread_detail(thread_id)
    if detail is None:
        raise ThreadNotFound(f"Nie ma wątku {thread_id}.")
    thread, origin = _row(detail["thread"]), detail["origin"] or {}
    origin_view = {**{key: iso(value) for key, value in origin.items() if key != "body_text"},
                   "text": reply_text.normalize(origin.get("body_text") or "")}
    mail_status = thread["mail_status"] or ""
    return {
        "thread": thread, "origin": origin_view, "messages": [_message(row) for row in detail["messages"]],
        "ratings": [{**{key: iso(value) for key, value in row.items()}} for row in detail["ratings"]],
        "suggestion": reply_rules.status_suggestion(thread["rating"], thread["optout"], mail_status),
        "statuses": [{"code": code, "label": st.STATUS_META[code][0], "color": st.STATUS_META[code][1]} for code in reply_rules.SETTABLE_STATUSES],
    }


def unassigned_view(store: ReplyStore, message_id: int) -> dict:
    """Wiadomość bez wątku do obejrzenia i ręcznego przypisania."""
    row = store.unassigned_message(message_id)
    if row is None:
        raise ThreadNotFound(f"Nie ma nieprzypisanej wiadomości {message_id}.")
    return {"message": _message(row)}


def candidates(store: ReplyStore, query: object) -> dict:
    """Maile kampanii pasujące do wpisanego tekstu (firma, adres, temat), do przypisania wiadomości: najwyżej 20."""
    words = rl.split_words(query)
    if not words:
        return {"mails": []}
    return {"mails": [{**{key: iso(value) for key, value in row.items()}} for row in store.search_sent_mails(words)]}


def rate(store: ReplyStore, thread_id: str, rating: object, *, note: object = "", by: str = rp.BY_HUMAN, now: datetime,
         restore: bool = False) -> dict:
    """Ustawia ocenę wątku (None czyści). Zwraca `{previous}` do „Cofnij”. Ocena AI nie nadpisuje oceny człowieka, chyba że
    to `restore`: przywrócenie poprzedniej oceny przez człowieka, który kliknął „Cofnij” (wtedy wraca także jej autor)."""
    value, text = reply_rules.check_rating(rating), reply_rules.check_note(note)
    if by == rp.BY_AI and value is None:
        raise reply_rules.RuleError("AI nie czyści ocen.")
    previous = store.set_rating(thread_id, value, by, text, now, force=restore)
    if previous is None:
        if not store.thread_exists(thread_id):
            raise ThreadNotFound(f"Nie ma wątku {thread_id}.")
        raise reply_rules.RuleError("Ta odpowiedź ma ocenę ustawioną przez człowieka; AI jej nie nadpisuje.")
    return {"previous": {"rating": previous["rating"], "by": previous["rating_by"], "note": previous["rating_note"] or ""}}


def set_state(store: ReplyStore, thread_id: str, action: str, now: datetime) -> dict:
    """Prosta zmiana stanu wątku: przeczytany / nieprzeczytany, zamknięty / otwarty, prośba o wypisanie tak / nie."""
    changes: dict[str, dict[str, Any]] = {
        "read": {"read_at": now}, "unread": {"read_at": None}, "close": {"closed_at": now}, "reopen": {"closed_at": None},
        "optout": {"optout": True}, "no_optout": {"optout": False}}
    if action not in changes:
        raise reply_rules.RuleError(f"Nieznana akcja: {action}.")
    if not store.update_thread(thread_id, **changes[action]):
        raise ThreadNotFound(f"Nie ma wątku {thread_id}.")
    return {"ok": True, "action": action}


def set_mail_status(db: Database, store: ReplyStore, thread_id: str, status: object) -> dict:
    """Status maila kampanii z zakładki Odpowiedzi (jedno kliknięcie po podpowiedzi). Zwraca poprzedni status do „Cofnij”."""
    code = reply_rules.check_settable_status(status)
    if not store.thread_exists(thread_id):
        raise ThreadNotFound(f"Nie ma wątku {thread_id}.")
    before = store.origin_info(thread_id)
    if before is None:
        raise ThreadNotFound(f"Nie ma maila {thread_id}.")
    db.apply_edit(thread_id, {"status": code})
    return {"previous": before["status"], "status": code}


def assign(store: ReplyStore, message_id: int, mail_id: str, primary_mailbox: str, now: datetime) -> dict:
    """Ręczne przypisanie wiadomości bez wątku do maila kampanii (wątek powstaje, jeśli go nie było). Skrzynką wątku jest ta,
    z której poszedł mail (mail sprzed wielu skrzynek: główna)."""
    mail = store.origin_info(mail_id)
    if mail is None:
        raise ThreadNotFound(f"Nie ma maila {mail_id}.")
    mailbox = (mail["sent_from"] or primary_mailbox or "").lower()
    if not store.assign_message(message_id, mail_id, mail["company_id"] or mail_id, mailbox, now):
        raise ThreadNotFound(f"Nie ma wiadomości {message_id} albo ma już wątek.")
    return {"ok": True, "thread": mail_id}


def panel_state(store: ReplyStore, config: Mapping, running: bool) -> dict:
    """Stan do paska w panelu: liczniki zakładek, nieprzeczytane, ostatnie pobieranie i **tylko skrzynki z problemem**
    (przy wielu skrzynkach lista wszystkich byłaby szumem). `running`: czy ten serwer właśnie pobiera."""
    last = store.last_run()
    states = {item["mailbox"]: item for item in store.mailbox_states()}
    boxes = [box.user.lower() for box in cfg.configured_mailboxes(config)]
    receiving = {box.address.lower() for box in _imap_boxes(config)}
    problems = [{"mailbox": mailbox, "kind": state["kind"] or "protocol", "error": state["error"]}
                for mailbox, state in sorted(states.items()) if state["error"] and mailbox in receiving]
    problems += [{"mailbox": mailbox, "kind": "no_imap", "error": ""} for mailbox in boxes if mailbox not in receiving]
    return {
        "counts": store.tab_counts(rl.ReplyFilter()), "unread": store.unread_total(),
        "fetch": {"running": running or last["running"], "finished_at": iso(last["finished_at"]), "started_at": iso(last["started_at"]),
                  "summary": last["summary"]},
        "mailboxes": {"configured": len(boxes), "receiving": len(receiving & set(boxes)), "problems": problems, "names": boxes},
        "auto_minutes": _auto_minutes(config),
    }


def _imap_boxes(config: Mapping) -> list:
    """Skrzynki z kompletnym IMAP; niekompletny wpis jednej nie wyłącza pozostałych (pokazujemy ją jako „bez IMAP”)."""
    boxes = []
    for slot in cfg.imap_slot_numbers(config):
        try:
            boxes.append(cfg.ImapSettings.from_config(config, slot))
        except cfg.ConfigError:
            continue
    return boxes


def _auto_minutes(config: Mapping) -> int:
    """Co ile minut panel pobiera sam, gdy karta jest otwarta (0 = nie pobiera); domyślnie 5."""
    try:
        value = int(config.get("REPLIES_AUTO_FETCH_MIN") or 5)
    except ValueError:
        return 5
    return max(0, min(value, 1440))
