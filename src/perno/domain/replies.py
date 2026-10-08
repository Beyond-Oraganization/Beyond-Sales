"""Stałe modułu odpowiedzi: rodzaje wiadomości, oceny, stany i sposoby dopasowania (jedno źródło dla bazy, panelu i `ai.py`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Rodzaj wiadomości przychodzącej. Ocenia się wyłącznie rozmowy z ludźmi; autoodpowiedzi i zwroty to osobne kubełki.
KIND_HUMAN = "human"
KIND_AUTO = "auto"  # autoodpowiedź (nieobecność, potwierdzenie przyjęcia zgłoszenia)
KIND_BOUNCE = "bounce"  # zwrot poczty od serwera (adres nie istnieje, skrzynka pełna…)
KINDS = (KIND_HUMAN, KIND_AUTO, KIND_BOUNCE)

DIRECTION_IN = "in"  # od firmy do nas
DIRECTION_OUT = "out"  # od nas do firmy (odpowiedź z panelu albo napisana w zwykłym kliencie poczty)

# Ocena odpowiedzi (jedna na wątek): ustawia ją człowiek w panelu albo AI przez `ai.py`.
RATING_POSITIVE = "positive"
RATING_NEUTRAL = "neutral"
RATING_NEGATIVE = "negative"
RATINGS = (RATING_POSITIVE, RATING_NEUTRAL, RATING_NEGATIVE)
RATING_LABELS = {RATING_POSITIVE: "Pozytywna", RATING_NEUTRAL: "Neutralna", RATING_NEGATIVE: "Negatywna"}

BY_HUMAN = "human"
BY_AI = "ai"

# Stan wiadomości: przychodzące są zawsze „received”; odpowiedź z panelu przechodzi szkic → wysyłanie → wysłana (albo błąd).
STATE_RECEIVED = "received"
STATE_DRAFT = "draft"
STATE_SENDING = "sending"
STATE_SENT = "sent"
STATE_FAILED = "failed"

# Jak wiadomość trafiła do wątku (od najpewniejszego).
MATCH_HEADER = "header"  # In-Reply-To / References wskazują na znany Message-ID
MATCH_ADDRESS = "address"  # nadawca to dokładnie adres, do którego pisaliśmy
MATCH_DOMAIN = "domain"  # ta sama domena firmy (słabe: wątek oznaczony „sprawdź przypisanie”)
MATCH_MANUAL = "manual"  # człowiek przypisał ręcznie

BOUNCE_HARD = "hard"  # adres nie istnieje: wysyłanie na niego nie ma sensu
BOUNCE_SOFT = "soft"  # tymczasowo (pełna skrzynka, opóźnienie): adres może jeszcze działać

REPLY_WINDOW_DAYS = 60  # odpowiedź przychodząca później niż tyle dni po wysłaniu nie jest dopasowywana samym adresem


@dataclass(frozen=True)
class NewMessage:
    """Wiadomość do zapisania w bazie (przychodząca, napisana przez nas w kliencie poczty albo odpowiedź z panelu)."""

    direction: str
    kind: str
    mailbox: str  # skrzynka, na której ją znaleziono albo z której wyszła (małymi literami)
    identity: str  # Message-ID albo skrót zastępczy (`message_ids.identity`): po nim wykrywamy duplikaty
    from_addr: str = ""
    from_name: str = ""
    to_addr: str = ""
    subject: str = ""
    date_at: Optional[datetime] = None
    body_text: str = ""
    snippet: str = ""
    message_id: str = ""  # klucz z `message_ids.key` (pusty, gdy wiadomość go nie miała)
    in_reply_to: str = ""
    references: str = ""
    thread_id: Optional[str] = None  # id maila kampanii; None = nieprzypisana
    company_id: Optional[str] = None
    state: str = STATE_RECEIVED
    author: str = BY_HUMAN
    folder: str = ""
    uidvalidity: int = 0
    uid: int = 0
    attachments: str = ""  # JSON: [{name, content_type, size}]
    match_method: str = ""
    match_weak: bool = False
    other_mailbox: bool = False
    bounce_type: str = ""
    optout_hint: bool = False
    thread_mailbox: str = ""  # skrzynka wątku (z maila kampanii), do filtra „Skrzynka”
