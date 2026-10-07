"""Wybór adresu e-mail firmy spośród adresów znalezionych na jej stronach: czysta logika bez bazy i sieci.

Skraper zbiera wszystko, co wygląda jak adres: skrzynki ogólne, osobiste, działy (rekrutacja, IOD), adresy partnerów
i klientów z podstron. Do kolejki wysyłki trafia jeden adres na firmę, więc wybieramy ostrożnie: skrzynka firmy,
nie osoby; własna domena przed cudzą; remis albo brak pewnego adresu oznacza „nie wybieraj”, a nie zgadywanie.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

GENERIC_LOCALS = frozenset({
    "biuro", "kontakt", "kancelaria", "info", "sekretariat", "office", "contact", "mail", "poczta", "handlowy",
    "sprzedaz", "oferty", "zarzad", "bok", "administracja", "hello",
})
# Skrzynki wyspecjalizowane albo automatyczne: nie są adresem do pierwszego kontaktu handlowego.
BLOCKED_LOCALS = frozenset({
    "iod", "rodo", "inspektor", "rekrutacja", "praca", "kariera", "reklamacje", "appstore", "googleplay", "noreply",
    "no-reply", "donotreply", "webmaster", "abuse", "postmaster", "help", "help24", "pomoc", "support", "serwis",
    "helpdesk", "faktury",
})
# Adresy z szablonów stron („imie.nazwisko@firma.pl”) i przykładowe domeny.
PLACEHOLDER_DOMAINS = frozenset({"firma.pl", "twojafirma.pl", "domena.pl", "example.com", "example.pl", "mail.com"})
PLACEHOLDER_LOCALS = frozenset({"imie.nazwisko", "imie", "nazwisko", "name", "email", "adres"})
MIN_PAGES_FOREIGN_DOMAIN = 5  # adres spoza domeny firmy uznajemy za jej własny dopiero, gdy wraca na wielu stronach

TIER_GENERIC, TIER_BRAND, TIER_OTHER = 0, 1, 2


@dataclass(frozen=True)
class Candidate:
    """Adres z oceną: niższy `tier` jest lepszy, przy równym `tier` wygrywa adres z większej liczby stron."""
    email: str
    tier: int
    pages: int


def split_email(email: str) -> Optional[tuple]:
    """`(część lokalna, domena)` małymi literami albo None, gdy to nie jest jeden poprawny adres."""
    text = (email or "").strip().lower()
    local, _, domain = text.partition("@")
    if not local or "." not in domain or "@" in domain or " " in text:
        return None
    return local, domain


def brand_label(domain: str) -> str:
    """Pierwszy człon domeny („kancelariamistewicz.com.pl” → „kancelariamistewicz”); `www.` jest pomijane."""
    parts = [part for part in domain.lower().split(".") if part and part != "www"]
    return parts[0] if parts else ""


def _tier(local: str, label: str) -> int:
    if local in GENERIC_LOCALS:
        return TIER_GENERIC
    if local == label or local.replace("-", "") == label.replace("-", ""):
        return TIER_BRAND
    return TIER_OTHER


def _own_domain(domain: str, firm_domain: str, in_domain: bool) -> bool:
    """Czy adres należy do firmy: ta sama domena (także podrzędna) albo ta sama marka na innej końcówce (.org/.pl)."""
    return in_domain or domain == firm_domain or brand_label(domain) == brand_label(firm_domain)


def _candidates(rows: Sequence[Mapping], firm_domain: str) -> tuple:
    """`(własne, obce)` — adresy po odrzuceniu zablokowanych i przykładowych, z ocenami."""
    own: dict = {}
    foreign: dict = {}
    for row in rows:
        parts = split_email(str(row.get("wartosc") or ""))
        if parts is None:
            continue
        local, domain = parts
        if local in BLOCKED_LOCALS or local in PLACEHOLDER_LOCALS or domain in PLACEHOLDER_DOMAINS:
            continue
        if row.get("rola") == "prawny":
            continue
        email = f"{local}@{domain}"
        item = Candidate(email, _tier(local, brand_label(firm_domain)), int(row.get("liczba_stron") or 1))
        target = own if _own_domain(domain, firm_domain, bool(row.get("w_domenie"))) else foreign
        if email not in target or target[email].pages < item.pages:
            target[email] = item
    return list(own.values()), list(foreign.values())


def _best(pool: Sequence[Candidate]) -> Optional[Candidate]:
    """Najlepszy adres z puli albo None przy remisie (ten sam `tier` i ta sama liczba stron)."""
    ranked = sorted(pool, key=lambda c: (c.tier, -c.pages, c.email))
    if not ranked:
        return None
    if len(ranked) > 1 and (ranked[0].tier, ranked[0].pages) == (ranked[1].tier, ranked[1].pages):
        return None
    return ranked[0]


def pick_email(rows: Sequence[Mapping], firm_domain: str) -> Optional[str]:
    """Adres do kontaktu z firmą wg kontaktów skrapera (`wartosc`, `w_domenie`, `rola`, `liczba_stron`) albo None.

    Najpierw własna domena firmy; adres obcy tylko wtedy, gdy własnych brak, a on sam wraca na co najmniej
    `MIN_PAGES_FOREIGN_DOMAIN` stronach (adresy partnerów i klientów z podstron zwykle pojawiają się raz)."""
    own, foreign = _candidates(rows, firm_domain)
    found = _best(own) if own else _best([c for c in foreign if c.pages >= MIN_PAGES_FOREIGN_DOMAIN])
    return found.email if found else None
