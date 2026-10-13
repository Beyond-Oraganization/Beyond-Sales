"""Leady dla Claude Code: skrót do uzupełnienia, walidacja i zapis pól (wzór: `ai_replies`).

Granice (ważniejsze niż wygoda): AI **uzupełnia pola leada** (kontakt, telefon, e-mail, czym się zajmuje), ale nic nie wysyła, nie
zmienia statusów firm ani `podmioty` i nie rusza etapu i notatki (to człowiek). Pola wpisane przez człowieka są nietykalne.
Skrót zawiera prawdziwe kontakty z odpowiedzi firmy (świadomy wyjątek od zasady z `ai_replies`), więc wpisany e-mail i telefon
muszą występować w wątku albo na stronie firmy: inaczej plan odrzuca wpis (ochrona przed zmyśleniem i literówką).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from perno.db import Database
from perno.db.leads_store import LeadStore
from perno.domain import brief as brief_rules
from perno.domain import lead_brief as lb
from perno.domain import lead_rules as lr
from perno.domain import reply_text
from perno.domain.reply_rules import RuleError
from perno.scraper.store import Store
from perno.services import firms_import
from perno.services.ai_replies import BriefBatch, WritePlan, entry_id, shape_problem

LEAD_FIELDS = {"id", "contact", "phone", "email", "about"}
BRIEF_PAGES = 4
BRIEF_PAGE_CHARS = 240
MESSAGES_IN_BRIEF = 2
SCAN_KNOWN = 20  # tyle adresów i telefonów ze skanu strony uznajemy za źródło kontaktu


def _sender(message: Mapping) -> str:
    name, addr = lb.sanitize(message.get("from_name") or ""), lb.sanitize(message.get("from_addr") or "")
    return f"{name} <{addr}>" if name and addr else (name or addr or "nieznany nadawca")


def _new_text(message: Mapping) -> str:
    """Nowy tekst wiadomości firmy bez cytatu naszego maila (z cytatu wzięlibyśmy nasz własny adres i telefon)."""
    body = message.get("body_text") or ""
    return reply_text.split_quote(body).new or body


def _scan_info(scan: Optional[Store], mail_ids: list) -> dict:
    """Co o firmie wie skan jej strony: strona główna, początki podstron, adresy i telefony (pusto, gdy firmy nie ma w skanerze)."""
    empty: dict = {"home": "", "pages": [], "emails": [], "phones": []}
    firma_id = scan.firm_for_mails(mail_ids) if scan is not None else None
    if scan is None or firma_id is None:
        return empty
    pages = scan.brief_pages(firma_id, BRIEF_PAGES)
    first = pages[0] if pages else ("", "", "", "")
    return {"home": brief_rules.home_line(first[1], first[2]),
            "pages": [(url, brief_rules.fit(brief_rules.collapse(text or ""), BRIEF_PAGE_CHARS)) for url, _title, _desc, text in pages[1:] if text],
            "emails": [item["wartosc"] for item in scan.scraped_emails(firma_id)[:SCAN_KNOWN]],
            "phones": scan.contact_values(firma_id, "telefon", SCAN_KNOWN)}


def _signals(lead: Mapping) -> list:
    out = []
    if lead["optout"]:
        out.append("możliwa prośba o brak kontaktu (słowa w treści)")
    if lead["do_not_contact"]:
        out.append("status firmy: Nie kontaktować")
    if lead["rating_by"] == lr.BY_AI:
        out.append("ocena odpowiedzi od AI (do przejrzenia)")
    return out


def brief_for(leads: LeadStore, scan: Optional[Store], company_id: str) -> Optional[lb.LeadBrief]:
    """Skrót jednego leada (None, gdy go nie ma): pola, ostatnie dwie odpowiedzi firmy z podpisem i dane ze strony."""
    lead = leads.get(company_id)
    if lead is None:
        return None
    evidence = leads.evidence(company_id)
    newest = list(reversed(evidence["messages"]))[:MESSAGES_IN_BRIEF]
    info = _scan_info(scan, [company_id, *lead["thread_ids"]])
    return lb.LeadBrief(
        lead_id=company_id, company=lead["name"] or company_id, segment=lead["segment"] or "", country=lead["country"] or "",
        rating=lead["rating"] or "", rating_by=lead["rating_by"] or "", thread_ids=lead["thread_ids"], values=lead, src=lead["src"],
        messages=[{"from": _sender(m), "date": m["date_at"], "text": _new_text(m)} for m in newest],
        base_description=lead["base_description"] or "", home=info["home"], snippets=info["pages"][:3],
        scan_emails=info["emails"][:3], scan_phones=info["phones"][:3], signals=_signals(lead))


@dataclass(frozen=True)
class BriefOptions:
    """Które leady i ile: najwyżej `limit` naraz, od id większego niż `after`, albo wskazane `ids` (id leada lub jego wątku);
    `only_missing`: tylko niekompletne; `chars` znaków na odpowiedź firmy i `budget` znaków na całą paczkę."""

    limit: int = 10
    after: str = ""
    ids: Sequence[str] = ()
    only_missing: bool = True
    chars: int = 900
    budget: int = 12_000


def build_briefs(leads: LeadStore, scan: Optional[Store], options: BriefOptions, now: datetime) -> BriefBatch:
    """Skróty leadów do uzupełnienia w budżecie `options.budget` znaków, ale zawsze co najmniej jeden. Najpierw zakłada brakujące
    leady z ocenionych odpowiedzi. `next_after` to id, od którego zaczyna się następna paczka ('' = to ostatnia, także przy `ids`)."""
    leads.sync(now)
    if options.ids:
        resolved = [leads.resolve(item) for item in options.ids]
        pending = list(dict.fromkeys(item["company_id"] for item in resolved if item))
    else:
        pending = leads.lead_ids(options.after, only_missing=options.only_missing)
    texts: list = []
    used = consumed = 0
    for company_id in pending[:max(1, options.limit)]:
        brief = brief_for(leads, scan, company_id)
        text = brief.format(options.chars) if brief else ""
        if text and texts and used + len(text) > options.budget:
            break
        consumed += 1
        if text:
            texts.append(text)
            used += len(text)
    more = bool(consumed) and consumed < len(pending) and not options.ids
    return BriefBatch(texts, len(pending), pending[consumed - 1] if more else "")


def stats(leads: LeadStore, now: datetime) -> dict:
    """Liczby do `ai.py leady stan` (po zasiewie brakujących leadów): karty panelu i licznik braków. Bez danych osobowych."""
    leads.sync(now)
    return leads.stats()


# ---- zapis ---------------------------------------------------------------------------------------------------------------

def _changes(entry: Mapping) -> dict:
    """Pola do zapisu z wpisu: puste (`null`, `""`) znaczą „bez zmiany”, AI nie czyści pól."""
    return {name: value for name, value in entry.items() if name != "id" and value not in (None, "")}


def _known(leads: LeadStore, scan: Optional[Store], company_id: str, thread_ids: Sequence[str]) -> lb.KnownContacts:
    evidence = leads.evidence(company_id)
    info = _scan_info(scan, [company_id, *thread_ids])
    messages = [{"from_addr": m["from_addr"], "body_text": _new_text(m)} for m in evidence["messages"]]
    return lb.known_contacts(messages, evidence["addresses"], info["emails"], info["phones"])


def _text_problem(changes: Mapping) -> str:
    for name in (lr.CONTACT, lr.ABOUT):
        found = lb.contact_in_text(changes.get(name, ""))
        if found:
            return f"pole {name}: {found} wpisz w osobnym polu ({'email' if found.startswith('adres') else 'phone'}), nie w tekście"
    return ""


def _source_problem(changes: Mapping, known: lb.KnownContacts) -> str:
    if lr.EMAIL in changes and not known.has_email(changes[lr.EMAIL]):
        return "e-mail nie występuje w odpowiedzi firmy ani na jej stronie (wpisz adres, który tam jest; inny może dodać tylko człowiek w panelu)"
    if lr.PHONE in changes and not known.has_phone(changes[lr.PHONE]):
        return "telefon nie występuje w odpowiedzi firmy ani na jej stronie (wpisz numer, który tam jest; inny może dodać tylko człowiek w panelu)"
    return ""


def _entry_verdict(leads: LeadStore, scan: Optional[Store], item: str, entry: Mapping, taken: set) -> tuple:
    """`("ok", wpis, ostrzeżenia)`, `("skip", None, ostrzeżenia)` (nic do zapisania) albo `("error", komunikat, [])`."""
    target = leads.resolve(item)
    if target is None:
        return ("error", "to nie jest lead (id firmy albo wątku z oceną pozytywną lub neutralną)", [])
    if not target["active"]:
        return ("error", "lead jest wycofany (żaden wątek nie ma już oceny pozytywnej ani neutralnej)", [])
    company_id = target["company_id"]
    if company_id in taken:
        return ("error", "ten lead jest w pliku drugi raz (pod innym id)", [])
    raw = _changes(entry)
    if not raw:
        return ("error", "wpis nie ma żadnego pola do zapisu (contact, phone, email, about)", [])
    try:
        changes = lr.check_changes(raw, by=lr.BY_AI)
    except RuleError as exc:
        return ("error", str(exc), [])
    lead = leads.get(company_id) or {}
    problem = _text_problem(changes) or _source_problem(changes, _known(leads, scan, company_id, lead.get("thread_ids", [])))
    if problem:
        return ("error", problem, [])
    src = lead.get("src", {})
    kept = [name for name in changes if not lr.ai_may_write(src, name)]
    applied = {name: value for name, value in changes.items() if name not in kept}
    warnings = [f"pole {lr.LABELS[name]} wpisał człowiek, pomijam" for name in kept]
    taken.add(company_id)
    return ("ok", {"id": item, "company_id": company_id, "changes": applied}, warnings) if applied else ("skip", None, warnings)


def plan_leads(leads: LeadStore, scan: Optional[Store], entries: Sequence[Any], now: datetime) -> WritePlan:
    """Sprawdza wpisy `{id, contact?, phone?, email?, about?}`; nic nie zapisuje. Pola wpisane przez człowieka są pomijane
    z ostrzeżeniem, a e-mail i telefon muszą pochodzić z wątku albo strony firmy."""
    leads.sync(now)
    plan, seen, taken = WritePlan(), set[str](), set[str]()
    for entry in entries:
        item = entry_id(entry)
        problem = shape_problem(entry, LEAD_FIELDS, seen)
        verdict = ("error", problem, []) if problem else _entry_verdict(leads, scan, item, entry, taken)
        label = item or "?"
        plan.warnings.extend((label, text) for text in verdict[2])
        if verdict[0] == "ok":
            plan.items.append(verdict[1])
        elif verdict[0] == "error":
            plan.errors.append((label, verdict[1]))
    return plan


def write_leads(db: Database, leads: LeadStore, plan: WritePlan, now: datetime, backup_dir: str) -> tuple:
    """Zapisuje pola jako wpisy AI (po kopii zapasowej tabel). Zwraca `(liczba leadów, pominięte pola [(id, nazwa)], ścieżka kopii)`;
    pole, które człowiek zdążył wpisać w międzyczasie, jest pomijane (sprawdza to magazyn w transakcji)."""
    if not plan.items:
        return 0, [], ""
    backup = firms_import.backup_table(db, backup_dir, "leadami-ai", replies=True)
    done, skipped = 0, list[tuple]()
    for item in plan.items:
        result = leads.update_fields(item["company_id"], item["changes"], by=lr.BY_AI, now=now)
        if result is None:
            skipped.extend((item["id"], name) for name in item["changes"])
            continue
        skipped.extend((item["id"], name) for name in result["skipped"])
        done += 1 if result["previous"] else 0
    return done, skipped, backup


__all__ = ["LEAD_FIELDS", "BriefOptions", "brief_for", "build_briefs", "plan_leads", "stats", "write_leads"]
