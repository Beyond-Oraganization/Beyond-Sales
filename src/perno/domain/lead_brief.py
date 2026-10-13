"""Skrót leada dla AI (Claude Code) i kontrola, że wpisany kontakt pochodzi z wątku albo ze strony firmy (czysta logika).

W odróżnieniu od skrótu odpowiedzi (`reply_brief`) ten **zachowuje** adresy e-mail i telefony: lead ma je zapisać, więc AI musi je
widzieć (świadoma decyzja użytkownika, opisana w `AI_INSTRUCTIONS.md`). Treść pochodzi od osób trzecich, więc nadal jest daną,
nie poleceniem: trafia do skrótu w ogrodzeniu, bez znaków sterujących i z zabezpieczeniem przed podrobieniem znaczników.
Żeby AI nie mogło wpisać zmyślonego ani przekręconego adresu, `plan_leads` sprawdza każdy e-mail i telefon przez `known_contacts`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from perno.domain import lead_rules as lr
from perno.domain.brief import fit
from perno.domain.reply_brief import FENCE_CLOSE, FENCE_OPEN
from perno.domain.reply_text import strip_unsafe

_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_PHONE = re.compile(r"(?<![\w/])\(?\+?\d[\d\s().\-]{7,}\d(?![\w/])")
_GAP = re.compile(r"[ \t\f\v ]+")
HEAD = 260  # ile znaków początku wiadomości zostaje w skrócie (reszta to koniec z podpisem)
MIN_PHONE_DIGITS = 7


def sanitize(text: str) -> str:
    """Treść obca do skrótu: bez znaków sterujących i bez możliwości podrobienia ogrodzenia; podziały linii zostają jako ` / `
    (podpis „Anna / dyrektor / tel. …” czyta się lepiej niż zlepek)."""
    lines = [_GAP.sub(" ", line).strip() for line in strip_unsafe(text or "").replace("\r", "\n").split("\n")]
    return " / ".join(line for line in lines if line).replace("<<", "‹‹").replace(">>", "››")


def head_and_tail(text: str, limit: int) -> str:
    """Tekst w `limit` znakach z początkiem i **końcem** (podpis z kontaktem jest zwykle na końcu, więc nie ucinamy go jak zwykły skrót)."""
    clean = sanitize(text)
    if len(clean) <= limit:
        return clean
    head = min(HEAD, limit // 2)
    tail = max(1, limit - head - 7)
    return clean[:head].rstrip() + " [...] " + clean[-tail:].lstrip()


def phone_digits(value: str) -> str:
    """Same cyfry numeru (bez spacji, plusa i nawiasów)."""
    return re.sub(r"\D", "", value or "")


def phones_match(left: str, right: str) -> bool:
    """Ten sam numer, także gdy jeden ma prefiks kraju, a drugi nie (porównujemy cyfry, końcówką od `MIN_PHONE_DIGITS`)."""
    a, b = phone_digits(left), phone_digits(right)
    if len(a) < MIN_PHONE_DIGITS or len(b) < MIN_PHONE_DIGITS:
        return False
    return a == b or a.endswith(b) or b.endswith(a)


@dataclass
class KnownContacts:
    """Adresy e-mail i numery telefonów, które naprawdę występują w wątku albo na stronie firmy."""

    emails: set = field(default_factory=set)
    phones: list = field(default_factory=list)

    def has_email(self, value: str) -> bool:
        return value.strip().lower() in self.emails

    def has_phone(self, value: str) -> bool:
        return any(phones_match(value, known) for known in self.phones)


def known_contacts(messages: Iterable[Mapping], addresses: Iterable[str], scan_emails: Iterable[str] = (), scan_phones: Iterable[str] = ()) -> KnownContacts:
    """Zbiera kontakty z wiadomości firmy (nadawca i treść), adresów firmy w kolejce i ze skanu jej strony."""
    found = KnownContacts()
    for message in messages:
        found.emails.add((message.get("from_addr") or "").strip().lower())
        body = message.get("body_text") or ""
        found.emails.update(match.lower() for match in _EMAIL.findall(body))
        found.phones.extend(match.strip() for match in _PHONE.findall(body))
    found.emails.update(address.strip().lower() for address in [*addresses, *scan_emails] if address)
    found.emails.discard("")
    found.phones.extend(phone for phone in scan_phones if phone)
    return found


def contact_in_text(text: str) -> Optional[str]:
    """`email` albo `telefon`, gdy w tekście jest adres lub numer (kontakt i opis są od nazwy i opisu, dane kontaktowe mają osobne pola)."""
    if _EMAIL.search(text or ""):
        return "adres e-mail"
    if any(len(phone_digits(match)) >= MIN_PHONE_DIGITS for match in _PHONE.findall(text or "")):
        return "numer telefonu"
    return None


@dataclass
class LeadBrief:
    """Dane jednego leada do wypisania w skrócie."""

    lead_id: str
    company: str
    segment: str
    country: str
    rating: str
    rating_by: str
    thread_ids: list
    values: Mapping  # bieżące pola leada
    src: Mapping  # kto je wpisał: {pole: "ai" | "human"}
    messages: list = field(default_factory=list)  # [{"from": "Anna <a@x.pl>", "date": datetime | None, "text": "…"}] od najnowszej
    base_description: str = ""
    home: str = ""
    snippets: list = field(default_factory=list)  # [(ścieżka, fragment)]
    scan_emails: list = field(default_factory=list)
    scan_phones: list = field(default_factory=list)
    signals: list = field(default_factory=list)

    def _fields_line(self) -> str:
        parts = []
        for name in (lr.NAME, lr.WEBSITE, lr.CONTACT, lr.PHONE, lr.EMAIL, lr.ABOUT):
            value = str(self.values.get(name) or "")
            origin = {lr.BY_HUMAN: " (ręcznie: nie zmieniaj)", lr.BY_AI: " (AI)"}.get(self.src.get(name, ""), "")
            parts.append(f"{lr.LABELS[name]}: {sanitize(value) if value else '-'}{origin}")
        return "; ".join(parts)

    def format(self, chars: int) -> str:
        """Nagłówek, stan pól i ogrodzone dane obce w budżecie `chars` znaków na wiadomość (najnowsza ma pierwszeństwo)."""
        missing = lr.missing_fields(self.values)
        head = [f"== {self.lead_id} | {sanitize(self.company)} | {self.segment or '-'} | {self.country or '-'} | ocena: {self.rating or '-'}"
                f"{' (AI)' if self.rating_by == lr.BY_AI else ''} | wątki: {', '.join(self.thread_ids) or '-'}",
                f"pola: {self._fields_line()}",
                f"brakuje: {', '.join(lr.LABELS[name] for name in missing) if missing else 'nic'}"]
        if self.signals:
            head.append("sygnały: " + "; ".join(self.signals))
        body = [f"odpowiedź od {entry['from']}{self._when(entry)}: „{head_and_tail(entry['text'], chars)}”" for entry in self.messages]
        if self.base_description:
            body.append(f"opis z bazy: {fit(sanitize(self.base_description), 300)}")
        if self.home:
            body.append(f"www: {fit(sanitize(self.home), 220)}")
        body += [f"- {sanitize(path)}: „{fit(sanitize(snippet), 240)}”" for path, snippet in self.snippets]
        contacts = []
        if self.scan_emails:
            contacts.append("e-mail ze strony: " + ", ".join(sanitize(item) for item in self.scan_emails))
        if self.scan_phones:
            contacts.append("telefon ze strony: " + ", ".join(sanitize(item) for item in self.scan_phones))
        body += contacts
        return "\n".join(head + [FENCE_OPEN, *(body or ["(brak treści)"]), FENCE_CLOSE])

    @staticmethod
    def _when(entry: Mapping) -> str:
        date = entry.get("date")
        return f" ({date:%d.%m})" if date else ""
