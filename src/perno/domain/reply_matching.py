"""Dopasowanie wiadomości przychodzącej do maila z kampanii, na który odpowiada (czysta logika, bez bazy i sieci).

Od najpewniejszego: (1) nagłówki `In-Reply-To` / `References` wskazują na znany `Message-ID`, (2) nadawca to dokładnie adres,
do którego pisaliśmy, a temat albo data się zgadzają, (3) ta sama domena firmy (słabe: wymaga zgodnego tematu i jednej
firmy), (4) sam temat kampanii bez firmy (do ręcznego przypisania). Domena poczty publicznej (gmail.com…) nigdy nie
identyfikuje firmy. Reszta jest odrzucana, zanim pobierzemy jej treść.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Mapping, Optional, Sequence

from perno.domain import replies as rp
from perno.domain.free_mail import email_domain, is_public_domain, site_domain
from perno.domain.reply_headers import Envelope, normalize_subject
from perno.domain.reply_parse import BounceInfo

CLOCK_SKEW = timedelta(days=1)  # zapisana data wysłania bywa przesunięta o godzinę, a zegary serwerów o minuty


@dataclass(frozen=True)
class SentMail:
    """To, co wiemy o wysłanym mailu kampanii: po tym rozpoznajemy odpowiedź."""

    id: str
    company_id: str
    email: str  # małymi literami
    subject: str  # temat znormalizowany (`normalize_subject`)
    sent_at: Optional[datetime]
    mailbox: str  # skrzynka, z której wyszedł (małymi literami; mail sprzed wielu skrzynek: skrzynka główna)
    message_id: str = ""  # klucz z `message_ids.key`; pusty dla maili wysłanych przed modułem odpowiedzi
    site_domain: str = ""  # domena strony firmy


@dataclass(frozen=True)
class Match:
    """Wynik dopasowania: mail kampanii (= wątek), firma i pewność."""

    mail_id: str
    company_id: str
    method: str
    weak: bool = False
    other_mailbox: bool = False  # wiadomość przyszła na inną skrzynkę niż ta, z której poszedł mail


def sent_mail_from_row(row: Mapping, primary_mailbox: str) -> SentMail:
    """`SentMail` z wiersza bazy (`maile` z dołączoną firmą). Brak zapisanej skrzynki oznacza skrzynkę główną."""
    return SentMail(
        id=row["id"], company_id=row.get("company_id") or row["id"], email=(row.get("email") or "").strip().lower(),
        subject=normalize_subject(row.get("subject") or ""), sent_at=row.get("sent_at"),
        mailbox=(row.get("sent_from") or primary_mailbox or "").strip().lower(), message_id=row.get("message_id") or "",
        site_domain=site_domain(row.get("website") or ""),
    )


@dataclass
class ReplyIndex:
    """Indeksy wysłanych maili do szybkiego dopasowania (budowane raz na przebieg pobierania)."""

    sent: Sequence[SentMail]
    by_id: dict = field(init=False, default_factory=dict)
    by_message_id: dict = field(init=False, default_factory=dict)
    by_email: dict = field(init=False, default_factory=dict)
    by_domain: dict = field(init=False, default_factory=dict)
    subjects: set = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        for mail in self.sent:
            self.by_id[mail.id] = mail
            if mail.message_id:
                self.by_message_id[mail.message_id] = mail
            if mail.email:
                self.by_email.setdefault(mail.email, []).append(mail)
            for domain in {email_domain(mail.email), mail.site_domain} - {""}:
                if not is_public_domain(domain):
                    self.by_domain.setdefault(domain, []).append(mail)
            if mail.subject:
                self.subjects.add(mail.subject)


def _mailbox_flag(mail: SentMail, mailbox: str) -> bool:
    return bool(mail.mailbox) and bool(mailbox) and mail.mailbox != mailbox


def _in_window(env: Envelope, mail: SentMail) -> bool:
    if env.date is None or mail.sent_at is None:
        return False
    delta = env.date - mail.sent_at
    return -CLOCK_SKEW <= delta <= timedelta(days=rp.REPLY_WINDOW_DAYS)


def _best(candidates: Iterable[SentMail], mailbox: str, subject: str) -> Optional[SentMail]:
    """Najlepszy z kandydatów: z tej samej skrzynki, potem o zgodnym temacie, potem najświeższy."""
    ranked = sorted(candidates, key=lambda mail: (mail.mailbox == mailbox, mail.subject == subject,
                                                  mail.sent_at or datetime.min), reverse=True)
    return ranked[0] if ranked else None


def counterpart(env: Envelope, direction: str) -> tuple:
    """Adresy firmy w wiadomości: nadawca przy przychodzącej, odbiorcy przy napisanej przez nas (folder Wysłane)."""
    return (env.sender,) if direction == rp.DIRECTION_IN and env.sender else (env.to if direction == rp.DIRECTION_OUT else ())


def _by_header(env: Envelope, index: ReplyIndex, known: Mapping[str, str], mailbox: str) -> Optional[Match]:
    for key in env.references_all:
        origin = index.by_id.get(known[key]) if key in known else index.by_message_id.get(key)
        if origin is not None:
            return Match(origin.id, origin.company_id, rp.MATCH_HEADER, other_mailbox=_mailbox_flag(origin, mailbox))
    return None


def _by_address(env: Envelope, index: ReplyIndex, mailbox: str, direction: str) -> Optional[Match]:
    subject = normalize_subject(env.subject)
    pool = [mail for address in counterpart(env, direction) for mail in index.by_email.get(address, ())
            if (subject and mail.subject == subject) or _in_window(env, mail)]
    best = _best(pool, mailbox, subject)
    if best is None:
        return None
    return Match(best.id, best.company_id, rp.MATCH_ADDRESS, other_mailbox=_mailbox_flag(best, mailbox))


def _by_domain(env: Envelope, index: ReplyIndex, mailbox: str, direction: str) -> Optional[Match]:
    subject = normalize_subject(env.subject)
    if not subject:
        return None
    pool = {mail.id: mail for address in counterpart(env, direction)
            for mail in index.by_domain.get(email_domain(address), ()) if mail.subject == subject}
    if len({mail.company_id for mail in pool.values()}) != 1:
        return None  # kilka firm o tym samym temacie i domenie: nie zgadujemy
    best = _best(pool.values(), mailbox, subject)
    return Match(best.id, best.company_id, rp.MATCH_DOMAIN, weak=True, other_mailbox=_mailbox_flag(best, mailbox)) if best else None


def match_message(env: Envelope, index: ReplyIndex, *, mailbox: str, direction: str = rp.DIRECTION_IN,
                  known: Optional[Mapping[str, str]] = None) -> Optional[Match]:
    """Mail kampanii, na który odpowiada wiadomość, albo None. `known` to identyfikatory wiadomości już zapisanych w
    wątkach (klucz `Message-ID` → id wątku), żeby odpowiedź na odpowiedź trafiła do tego samego wątku."""
    return (_by_header(env, index, known or {}, mailbox) or _by_address(env, index, mailbox, direction)
            or _by_domain(env, index, mailbox, direction))


def is_campaign_subject(env: Envelope, index: ReplyIndex) -> bool:
    """Czy temat wiadomości to temat któregoś z naszych maili (po zdjęciu „Re:”); tylko taka wiadomość bez firmy trafia do
    „Nieprzypisanych”. Pusty temat nigdy nie pasuje."""
    subject = normalize_subject(env.subject)
    return bool(subject) and subject in index.subjects


def wants_full(env: Envelope, kind: str, match: Optional[Match], index: ReplyIndex) -> bool:
    """Decyzja etapu nagłówków: czy pobrać pełną treść. Tak dla zwrotu, wiadomości dopasowanej do wątku i takiej, która
    ma temat naszej kampanii; nie dla niczego innego (poczta robocza nie jest pobierana)."""
    return kind == rp.KIND_BOUNCE or match is not None or is_campaign_subject(env, index)


def match_bounce(bounce: BounceInfo, index: ReplyIndex) -> list:
    """Dopasowania zwrotu: po `Message-ID` oryginału, a gdy go brak, po adresach, na które nie dotarło (najświeższy mail
    pod dany adres). Zwraca listę, bo jeden zwrot może dotyczyć kilku adresów."""
    original = index.by_message_id.get(bounce.original_message_id) if bounce.original_message_id else None
    if original is not None:
        return [Match(original.id, original.company_id, rp.MATCH_HEADER)]
    found = []
    for address in bounce.recipients:
        latest = _best(index.by_email.get(address, ()), "", "")
        if latest is not None:
            found.append(Match(latest.id, latest.company_id, rp.MATCH_ADDRESS))
    return found
