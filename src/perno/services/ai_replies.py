"""Odpowiedzi dla Claude Code: skrót do oceny, walidacja i zapis ocen oraz szkiców (wzór: `ai_workflow`).

Granice (ważniejsze niż wygoda): AI **ocenia i pisze szkice**, ale nic nie wysyła i nie zmienia statusu maila. Ocena AI nie
nadpisuje oceny człowieka, szkic AI nie nadpisuje szkicu człowieka, a do firmy, która prosiła o brak kontaktu, szkicu nie ma.
Treść odpowiedzi trafia do skrótu w ogrodzeniu i bez danych kontaktowych (`domain/reply_brief`).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from perno.db import Database
from perno.db.replies_store import ReplyStore
from perno.domain import replies as rp
from perno.domain import reply_brief as rb
from perno.domain import reply_compose as rc
from perno.domain import reply_rules, reply_text
from perno.services import firms_import, replies, replies_send

RATING_FIELDS = {"id", "rating", "note", "optout"}
DRAFT_FIELDS = {"id", "body"}
MAX_AI_DRAFT = 3000  # szkic AI to krótka odpowiedź, nie elaborat
_PLACEHOLDER = re.compile(r"\[[^\]\n]{1,40}\]|\{\{|\}\}|<<|>>")
_DASHES = re.compile("[—–]")


@dataclass
class BriefBatch:
    """Paczka skrótów: teksty, ile wątków czeka (od `after`) i od jakiego id zacząć następną paczkę ('' = koniec)."""

    texts: list
    pending: int
    next_after: str


@dataclass
class WritePlan:
    """Wynik walidacji (nic jeszcze nie zapisano): poprawne wpisy, błędy `(id, komunikat)` i ostrzeżenia `(id, komunikat)`."""

    items: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _signals(thread: Mapping, draft: Optional[Mapping]) -> list:
    out = []
    if thread["awaiting_reply"]:
        out.append("czeka na naszą odpowiedź")
    if thread["optout"]:
        out.append("możliwa prośba o brak kontaktu (słowa w treści)")
    if thread["rating"] and thread["rating_by"] == rp.BY_AI:
        out.append(f"ocena AI: {thread['rating']} (do przejrzenia)")
    if draft:
        out.append(f"jest szkic odpowiedzi ({'AI' if draft['author'] == rp.BY_AI else 'człowieka'})")
    return out


def brief_for(store: ReplyStore, thread_id: str) -> Optional[rb.ReplyBrief]:
    """Skrót jednego wątku (None, gdy go nie ma): ostatnie dwie wiadomości firmy bez cytatu naszego maila."""
    detail = store.thread_detail(thread_id)
    if detail is None:
        return None
    thread, origin, messages = detail["thread"], detail["origin"] or {}, detail["messages"]
    inbound = [m for m in messages if m["direction"] == rp.DIRECTION_IN and m["kind"] == rp.KIND_HUMAN]
    texts = [reply_text.split_quote(m["body_text"] or "").new or (m["body_text"] or "") for m in reversed(inbound)][:2]
    return rb.ReplyBrief(
        thread_id=thread_id, company=thread["company"] or thread_id, segment=thread["segment"] or "", country=thread["country"] or "",
        campaign_subject=origin.get("subject") or "", sent_at=origin.get("sent_at"), first_reply_at=inbound[0]["date_at"] if inbound else None,
        form=origin.get("mode") or "",
        from_company=len(inbound), ours=sum(1 for m in messages if m["direction"] == rp.DIRECTION_OUT and m["state"] == rp.STATE_SENT),
        signals=_signals(thread, store.get_draft(thread_id)), messages=texts)


def build_briefs(store: ReplyStore, *, limit: int, after: str = "", ids: Sequence[str] = (), chars: int = 800, budget: int = 10_000,
                 rating: Optional[str] = None) -> BriefBatch:
    """Skróty nieocenionych wątków (albo z oceną `rating`, albo wskazanych `ids`): najwyżej `limit` naraz i w budżecie `budget`
    znaków, ale zawsze co najmniej jeden. `next_after` to id, od którego zaczyna się następna paczka ('' = to ostatnia, także
    przy wskazanych `ids`)."""
    pending = list(ids) if ids else store.thread_ids(after, rating)
    texts: list = []
    used = 0
    consumed = 0
    for thread_id in pending[:max(1, limit)]:
        brief = brief_for(store, thread_id)
        text = brief.format(chars) if brief else ""
        if text and texts and used + len(text) > budget:
            break
        consumed += 1
        if text:
            texts.append(text)
            used += len(text)
    more = bool(consumed) and consumed < len(pending) and not ids
    return BriefBatch(texts, len(pending), pending[consumed - 1] if more else "")


# ---- oceny --------------------------------------------------------------------------------------------------------------

def _entry_id(entry: Any) -> str:
    return str(entry.get("id") or "") if isinstance(entry, dict) else ""


def _shape_problem(entry: Any, allowed: set, seen: set) -> str:
    """Co jest nie tak z kształtem wpisu (obiekt z id, id nie powtórzone, tylko znane pola); '' = w porządku. Powtórzone id to
    błąd także wtedy, gdy pierwszy wpis z tym id był niepoprawny."""
    thread_id = _entry_id(entry)
    if not thread_id:
        return "wpis musi być obiektem z polem id"
    duplicate = thread_id in seen
    seen.add(thread_id)
    unknown = sorted(set(entry) - allowed)
    if duplicate:
        return "id powtórzone w pliku"
    return f"nieznane pola: {', '.join(unknown)} (dozwolone: {', '.join(sorted(allowed))})" if unknown else ""


def plan_ratings(store: ReplyStore, entries: Sequence[Any]) -> WritePlan:
    """Sprawdza wpisy `{id, rating, note?, optout?}`; nic nie zapisuje. Wątek z oceną człowieka jest pominięty z ostrzeżeniem."""
    plan = WritePlan()
    seen: set = set()
    for entry in entries:
        thread_id = _entry_id(entry)
        label = thread_id or "?"
        problem = _shape_problem(entry, RATING_FIELDS, seen)
        verdict = ("error", problem) if problem else _rating_problem(store, thread_id, entry)
        if verdict is None:
            plan.items.append({"id": thread_id, "rating": entry["rating"], "note": reply_rules.check_note(entry.get("note", "")), "optout": entry.get("optout") is True})
        else:
            (plan.warnings if verdict[0] == "skip" else plan.errors).append((label, verdict[1]))
    return plan


def _rating_problem(store: ReplyStore, thread_id: str, entry: Mapping) -> Optional[tuple]:
    state = store.thread_state(thread_id)
    if state is None:
        return ("error", "nie ma takiego wątku")
    if not state["human_in_count"]:
        return ("error", "ten wątek nie ma odpowiedzi człowieka (autoodpowiedzi i zwroty się nie oceniają)")
    try:
        if reply_rules.check_rating(entry.get("rating")) is None:
            return ("error", "pole rating jest wymagane (positive, neutral albo negative)")
        reply_rules.check_note(entry.get("note", ""))
    except reply_rules.RuleError as exc:
        return ("error", str(exc))
    if "optout" in entry and not isinstance(entry["optout"], bool):
        return ("error", "pole optout musi być true albo false")
    if not reply_rules.ai_may_rate(state["rating_by"]):
        return ("skip", "ocena ustawiona przez człowieka, pomijam")
    return None


def write_ratings(db: Database, store: ReplyStore, plan: WritePlan, now: datetime, backup_dir: str) -> tuple:
    """Zapisuje poprawne oceny jako oceny AI (po kopii zapasowej tabel). Zwraca `(liczba, ścieżka kopii)`. Znacznik
    `optout: true` tylko włącza ostrzeżenie w panelu; status maila zmienia wyłącznie człowiek."""
    if not plan.items:
        return 0, ""
    backup = firms_import.backup_table(db, backup_dir, "przed-ocenami-ai", replies=True)
    done = 0
    for item in plan.items:
        try:
            replies.rate(store, item["id"], item["rating"], note=item["note"], by=rp.BY_AI, now=now)
        except reply_rules.RuleError:
            continue  # człowiek zdążył ocenić w międzyczasie
        if item["optout"]:
            store.update_thread(item["id"], optout=True)
        done += 1
    return done, backup


# ---- szkice ---------------------------------------------------------------------------------------------------------------

def plan_drafts(store: ReplyStore, config: Mapping, entries: Sequence[Any]) -> WritePlan:
    """Sprawdza wpisy `{id, body}`: wątek ma odpowiedź człowieka i działającą skrzynkę, firma nie prosiła o brak kontaktu, treść
    nie ma znaczników zastępczych ani długich myślników. Nic nie zapisuje."""
    plan = WritePlan()
    seen: set = set()
    for entry in entries:
        thread_id = _entry_id(entry)
        problem = _shape_problem(entry, DRAFT_FIELDS, seen) or _draft_problem(store, config, thread_id, entry.get("body"))
        if problem:
            plan.errors.append((thread_id or "?", problem))
        else:
            plan.items.append({"id": thread_id, "body": rc.check_body(entry["body"])})
    return plan


def _draft_problem(store: ReplyStore, config: Mapping, thread_id: str, body: object) -> str:
    detail = store.thread_detail(thread_id)
    if detail is None:
        return "nie ma takiego wątku"
    view = replies_send.compose_view(detail, config, None)
    if not view["can_reply"]:
        return view["reason"]
    if not view["mailbox_ok"]:
        return view["mailbox_problem"]
    if view["needs_ack"]:
        return "firma prosiła o brak kontaktu (albo ma status „Nie kontaktować”): szkicu nie przygotowuję"
    try:
        text = rc.check_body(body)
    except rc.ComposeError as exc:
        return str(exc)
    if len(text) > MAX_AI_DRAFT:
        return f"szkic jest za długi (najwyżej {MAX_AI_DRAFT} znaków)"
    if _PLACEHOLDER.search(text):
        return "w treści został znacznik zastępczy ([…], {{…}}) albo ogrodzenie „<<”: wpisz prawdziwą treść"
    if _DASHES.search(text):
        return "długi myślnik (— –): użyj zwykłego „-”"
    return ""


def write_drafts(db: Database, store: ReplyStore, config: Mapping, plan: WritePlan, now: datetime, backup_dir: str) -> tuple:
    """Zapisuje szkice jako szkice AI (po kopii zapasowej). Szkic człowieka nie jest nadpisywany. Zwraca `(zapisane, pominięte, kopia)`."""
    if not plan.items:
        return 0, [], ""
    backup = firms_import.backup_table(db, backup_dir, "przed-szkicami-ai", replies=True)
    saved, skipped = 0, []
    for item in plan.items:
        result = replies_send.save_draft(store, config, item["id"], item["body"], author=rp.BY_AI, now=now, keep_human=True)
        if result["saved"]:
            saved += 1
        else:
            skipped.append(item["id"])
    return saved, skipped, backup


def report_path(base_dir: str, now: datetime) -> str:
    """Gdzie zapisać raport z `--zapisz`: `raporty/odpowiedzi_RRRRMMDD.md` (katalog poza gitem)."""
    return os.path.join(base_dir, "raporty", f"odpowiedzi_{now:%Y%m%d}.md")


__all__ = ["BriefBatch", "WritePlan", "brief_for", "build_briefs", "plan_drafts", "plan_ratings", "report_path", "write_drafts", "write_ratings"]
