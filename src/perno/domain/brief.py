"""Skrót firmy dla autora treści maila: czysta logika bez bazy i sieci.

Zamiast kilku tysięcy znaków tekstu stron dajemy to, co potrzebne do napisania maila: krótkie fragmenty wokół słów
o zapisach, cenach i płatnościach oraz sygnały policzone regułami (znane platformy sprzedażowe, bramki płatności,
„to już nasz klient”). Dzięki temu autor nie musi czytać całych stron."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

KEYWORDS = ("zapis", "rezerw", "umów", "cennik", "cena", "zł", "termin", "online", "płatn", "koszyk", "bilet", "kalendarz",
            "booking", "reserv", "price", "ticket")
# Platformy do sprzedaży biletów i zapisów (konkurencja Payevento) oraz bramki płatności i systemy rezerwacji.
PLATFORMS = {
    "Datasport": r"datasport", "KupBilecik": r"kupbilecik", "Feblik": r"feblik", "ActiveNow": r"activenow",
    "Fitssey": r"fitssey", "Konfeo": r"konfeo", "easy.tools": r"easy\.tools", "Bilety24": r"bilety24", "DoStartu": r"dostartu",
    "Eventim": r"eventim", "GoOut": r"goout", "Booksy": r"booksy", "Calendly": r"calendly",
}
PAYMENTS = {
    "Przelewy24": r"przelewy24", "PayU": r"payu\b", "Tpay": r"tpay\b", "BLIK": r"\bblik\b", "Stripe": r"stripe",
    "PayPal": r"paypal", "WooCommerce": r"woocommerce|koszyk",
}
CLIENT_MARK = re.compile(r"payevento", re.IGNORECASE)  # to nasz produkt: firma jest już klientem
_SPACES = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")  # sterujące i kierunkowe: dane ze stron są niezaufane


@dataclass(frozen=True)
class Signals:
    """Co wykryto na stronach firmy: platformy sprzedażowe, bramki płatności i czy firma już korzysta z Payevento."""
    platforms: tuple = ()
    payments: tuple = ()
    client: bool = False


@dataclass
class FirmBrief:
    """Dane jednej firmy do wypisania w skrócie; `format` zmieści je w podanym budżecie znaków."""
    row_id: str
    company: str
    place: str
    language: str
    pages: int
    address: str
    phone: str = ""
    form: str = ""
    description: str = ""
    home: str = ""
    template: str = ""
    signals: Signals = field(default_factory=Signals)
    snippets: list = field(default_factory=list)  # [(ścieżka, fragment)]

    def format(self, budget: int) -> str:
        """Nagłówek, adres, kontakt, sygnały i fragmenty; fragmenty obcinane jako pierwsze, gdy brakuje miejsca."""
        head = [f"## {self.row_id} | {self.company} | {self.place} | {self.language} | stron: {self.pages}",
                f"adres: {self.address}"]
        contact = " | ".join(part for part in (f"tel: {self.phone}" if self.phone else "",
                                               f"formularz: {self.form}" if self.form else "") if part)
        sig = describe_signals(self.signals)
        lines = head + [line for line in (
            contact, f"opis: {fit(self.description, 220)}" if self.description else "",
            f"www: {fit(self.home, 200)}" if self.home else "", f"sygnały: {sig}" if sig else "",
            f"szablon: {self.template}" if self.template else "") if line]
        text = "\n".join(lines)
        for path, snippet in self.snippets:
            line = f"- {path}: „{snippet}”"
            if len(text) + len(line) + 1 > budget:
                break
            text += "\n" + line
        return fit(text, budget) if len(text) > budget else text


def collapse(text: str) -> str:
    """Tekst w jednej linii, bez wielokrotnych odstępów."""
    return _SPACES.sub(" ", _CONTROL.sub("", text or "")).strip()


def fit(text: str, limit: int) -> str:
    """Skraca do `limit` znaków (z wielokropkiem), nie przerywając słowa, jeśli da się tego uniknąć."""
    text = collapse(text) if "\n" not in text else text
    if len(text) <= limit:
        return text
    cut = text[:max(0, limit - 1)]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,.;:") + "…"


def snippets(text: str, keywords: Sequence[str] = KEYWORDS, width: int = 110, limit: int = 3) -> list:
    """Do `limit` fragmentów (±`width` znaków) wokół pierwszych wystąpień słów kluczowych; zachodzące na siebie łączone."""
    flat = collapse(text)
    lower = flat.lower()
    spans: list = []
    for word in keywords:
        start = lower.find(word)
        if start < 0:
            continue
        span = (max(0, start - width), min(len(flat), start + len(word) + width))
        if all(span[0] >= other[1] or span[1] <= other[0] for other in spans):
            spans.append(span)
        if len(spans) >= limit:
            break
    return [flat[a:b].strip() for a, b in sorted(spans)]


def _found(table: dict, blob: str) -> tuple:
    return tuple(name for name, pattern in table.items() if re.search(pattern, blob))


def detect(texts: Iterable[str]) -> Signals:
    """Platformy, bramki płatności i znacznik klienta wykryte w tekstach i adresach stron firmy."""
    blob = "\n".join(texts).lower()
    return Signals(_found(PLATFORMS, blob), _found(PAYMENTS, blob), bool(CLIENT_MARK.search(blob)))


def describe_signals(signals: Signals) -> str:
    """Jedna linia sygnałów; `STOP=PayEvento` znaczy, że firma już korzysta z Payevento i nie jest zimnym kontaktem."""
    parts = []
    if signals.client:
        parts.append("STOP=PayEvento (już klient)")
    if signals.platforms:
        parts.append("platformy=" + ",".join(signals.platforms))
    if signals.payments:
        parts.append("płatności=" + ",".join(signals.payments))
    return "; ".join(parts)


def home_line(title: Optional[str], description: Optional[str]) -> str:
    """„Tytuł | opis” strony głównej."""
    return " | ".join(collapse(part) for part in (title, description) if part and collapse(part))
