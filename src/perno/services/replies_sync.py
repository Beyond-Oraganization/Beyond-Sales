"""Pobieranie odpowiedzi: IMAP każdej skrzynki → dopasowanie → zapis wątków i wiadomości.

Dwa etapy na wiadomość: najpierw same nagłówki (z nich `domain` rozstrzyga, czy wiadomość w ogóle nas dotyczy), potem pełna
treść tylko kandydatów. Poczta robocza w tej samej skrzynce nie jest więc pobierana. Skrzynki idą równolegle (mała pula
wątków), ale **baza jest zapisywana tylko z wątku koordynującego**, a wątki robocze pracują na migawce (indeks wysłanych maili i
znane identyfikatory). Kursor folderu przesuwa się dopiero po zapisie, więc przerwany przebieg po prostu wznowi pracę.
Odczyt jest wyłącznie do odczytu (EXAMINE + PEEK): nic nie jest oznaczane jako przeczytane, przenoszone ani usuwane.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Optional

from perno import settings as cfg
from perno.db import Database
from perno.db.replies_store import Cursor, ReplyStore
from perno.domain import imap_folders, message_ids
from perno.domain import replies as rp
from perno.domain import reply_headers as rh
from perno.domain import reply_matching as rm
from perno.domain import reply_parse as pp
from perno.domain import reply_text as rt
from perno.domain import statuses as st
from perno.mailer.imap_client import ImapError

logger = logging.getLogger(__name__)

FIRST_RUN_MARGIN = timedelta(days=1)  # pierwszy przebieg szuka od doby przed pierwszą wysyłką
DEFAULT_BUDGET = timedelta(minutes=4)
MAX_PER_FOLDER = 5000  # najwięcej nowych wiadomości z jednego folderu w jednym przebiegu (reszta w następnym)


@dataclass(frozen=True)
class SyncOptions:
    """Ustawienia przebiegu (z config.env i z wywołania)."""

    ignore: tuple = ()  # adresy i domeny wewnętrzne (np. jakub@z3x.io), których wiadomości nie są odpowiedziami
    folders: tuple = ()  # nazwy folderów; puste = wykryj (Odebrane, Wysłane, Spam)
    since: Optional[date] = None  # nadpisuje „od doby przed pierwszą wysyłką” (tylko dla folderów bez kursora)
    workers: int = 4
    budget: timedelta = DEFAULT_BUDGET
    only: str = ""  # adres jednej skrzynki do pobrania (puste = wszystkie)
    dry_run: bool = False


@dataclass
class Found:
    """Wiadomość uznana za naszą: gotowa do zapisu."""

    folder: str
    uid: int
    uidvalidity: int
    direction: str
    parsed: pp.ParsedMessage
    match: Optional[rm.Match]
    bounce_type: str = ""


@dataclass
class FolderScan:
    """Wynik przeglądu jednego folderu: ile nowych wiadomości, które z nich nas dotyczą i dokąd przesunąć kursor."""

    folder: str
    cursor: Optional[Cursor] = None  # None: nie udało się dojść do końca (kursora nie ruszamy)
    seen: int = 0
    found: list = field(default_factory=list)
    error: str = ""


@dataclass
class MailboxReport:
    """Wynik jednej skrzynki do podsumowania (bez adresów osób trzecich i bez treści)."""

    mailbox: str
    slot: int
    ok: bool = True
    error: str = ""
    kind: str = ""
    seen: int = 0
    candidates: int = 0
    stored: int = 0
    duplicates: int = 0
    unassigned: int = 0
    kinds: Counter = field(default_factory=Counter)
    folders: list = field(default_factory=list)
    postponed: bool = False


@dataclass
class SyncReport:
    """Podsumowanie przebiegu: raporty skrzynek, zmiany statusów i powód, dla którego przebieg się nie odbył."""

    mailboxes: list = field(default_factory=list)
    replied: int = 0  # maile, którym zmieniono status na „odpowiedział”
    bounced: int = 0  # maile, którym zmieniono status na „zwrot”
    skipped: str = ""  # dlaczego przebieg się nie odbył
    dry_run: bool = False

    def totals(self) -> dict:
        return {"mailboxes": len(self.mailboxes), "failed": sum(1 for m in self.mailboxes if not m.ok),
                "seen": sum(m.seen for m in self.mailboxes), "candidates": sum(m.candidates for m in self.mailboxes),
                "stored": sum(m.stored for m in self.mailboxes), "duplicates": sum(m.duplicates for m in self.mailboxes),
                "unassigned": sum(m.unassigned for m in self.mailboxes), "replied": self.replied, "bounced": self.bounced,
                "kinds": dict(sum((m.kinds for m in self.mailboxes), Counter())), "dry_run": self.dry_run, "skipped": self.skipped}


@dataclass(frozen=True)
class Snapshot:
    """To, co wątki robocze wiedzą o świecie (tylko do odczytu)."""

    index: rm.ReplyIndex
    known: Mapping
    own: tuple
    ignore: tuple
    since: Optional[date]
    deadline: datetime
    clock: Callable[[], datetime]


# ---- etap odczytu (wątki robocze: tylko IMAP i czysta logika) ----------------------------------------

def _direction(role: str) -> str:
    return rp.DIRECTION_OUT if role == imap_folders.ROLE_SENT else rp.DIRECTION_IN


def _judge(env: rh.Envelope, direction: str, mailbox: str, snap: Snapshot) -> Optional[tuple]:
    """Decyzja etapu nagłówków: (rodzaj, dopasowanie) albo None, gdy wiadomość nas nie dotyczy."""
    if direction == rp.DIRECTION_IN and rh.is_ignored(env, snap.own, snap.ignore):
        return None
    kind = rp.KIND_HUMAN if direction == rp.DIRECTION_OUT else rh.kind_of(env)
    match = rm.match_message(env, snap.index, mailbox=mailbox, direction=direction, known=snap.known)
    if not rm.wants_full(env, kind, match, snap.index):
        return None
    if direction == rp.DIRECTION_OUT and match is None:
        return None  # nasza wiadomość do kogoś spoza kampanii
    return kind, match


def _full(imap: Any, uid: int, direction: str, match: Optional[rm.Match], snap: Snapshot) -> Optional[tuple]:
    """Etap pełnej treści: parsowanie i końcowa decyzja. Zwrot dopasowuje się po raporcie doręczenia (nagłówki wiedzą o nim
    za mało), a pozostałe wiadomości zostają przy dopasowaniu z nagłówków."""
    parsed = pp.parse_message(imap.fetch_message(uid))
    if direction == rp.DIRECTION_OUT:
        return replace_kind(parsed, rp.KIND_HUMAN), match
    if parsed.kind == rp.KIND_BOUNCE and parsed.bounce is not None:
        found = rm.match_bounce(parsed.bounce, snap.index)
        return (parsed, found[0]) if found else None
    return parsed, match


def replace_kind(parsed: pp.ParsedMessage, kind: str) -> pp.ParsedMessage:
    """Ta sama wiadomość z innym rodzajem (nasze własne wiadomości z folderu Wysłane to zawsze „człowiek”)."""
    return pp.ParsedMessage(parsed.envelope, kind, parsed.text, parsed.attachments, parsed.bounce)


def scan_folder(imap: Any, folder: str, role: str, cursor: Optional[Cursor], ctx: tuple) -> FolderScan:
    """Jeden folder: nowe wiadomości od kursora, nagłówki → decyzja → pełna treść kandydatów."""
    mailbox, snap = ctx
    direction = _direction(role)
    state = imap.select(folder)
    after = cursor.last_uid if cursor and cursor.uidvalidity == state.uidvalidity else 0
    uids = imap.search_uids(since=snap.since, after_uid=after)[:MAX_PER_FOLDER]
    scan = FolderScan(folder, Cursor(state.uidvalidity, max(uids, default=after)), seen=len(uids))
    for uid, raw in sorted(imap.fetch_headers(uids).items()) if uids else ():
        env = rh.parse_envelope(raw)
        judged = _judge(env, direction, mailbox, snap)
        if judged is None:
            continue
        outcome = _full(imap, uid, direction, judged[1], snap)
        if outcome is None:
            continue
        parsed, match = outcome
        unassigned = match is None and rm.is_campaign_subject(parsed.envelope, snap.index) and direction == rp.DIRECTION_IN
        if match is not None or unassigned:
            scan.found.append(Found(folder, uid, state.uidvalidity, direction, parsed, match,
                                    parsed.bounce.kind if parsed.bounce else ""))
    return scan


def scan_mailbox(settings: cfg.ImapSettings, factory: Callable, folders: tuple, get_cursor: Callable, snap: Snapshot) -> tuple:
    """Wszystkie foldery jednej skrzynki; zwraca (raport, listę `FolderScan`). Błąd skrzynki nie przerywa pozostałych."""
    report = MailboxReport(settings.address.lower(), settings.slot)
    scans: list = []
    try:
        with factory(settings) as imap:
            listing = imap.list_folders()
            wanted = list(folders) or imap_folders.default_folders(listing)
            roles = {item.name: item.role for item in listing}
            for folder in wanted:
                if snap.clock() >= snap.deadline:
                    report.postponed = True
                    break
                scans.append(scan_folder(imap, folder, roles.get(folder, imap_folders.ROLE_OTHER), get_cursor(folder), (report.mailbox, snap)))
    except ImapError as exc:
        report.ok, report.error, report.kind = False, str(exc), exc.kind
    except OSError as exc:  # sieć, TLS, limit czasu
        report.ok, report.error, report.kind = False, f"{type(exc).__name__}: {str(exc)[:200]}", "network"
    except Exception as exc:  # granica wątku roboczego: błąd jednej skrzynki (sieć, parser) nie zatrzymuje pozostałych
        logger.exception("Pobieranie poczty skrzynki %s nie powiodło się", settings.slot)
        report.ok, report.error, report.kind = False, f"{type(exc).__name__}: {str(exc)[:200]}", "protocol"
    return report, scans


# ---- zapis (wątek koordynujący) ----------------------------------------------------------------------

def _new_message(found: Found, mailbox: str, sent_by_id: Mapping) -> rp.NewMessage:
    parsed, env = found.parsed, found.parsed.envelope
    origin = sent_by_id.get(found.match.mail_id) if found.match else None
    new_text = rt.split_quote(parsed.text).new
    identity = message_ids.identity(env.message_id, env.sender, env.date.isoformat() if env.date else "", env.subject, parsed.text)
    return rp.NewMessage(
        direction=found.direction, kind=parsed.kind, mailbox=mailbox, identity=identity, from_addr=env.sender,
        from_name=env.sender_name, to_addr=", ".join(env.to), subject=env.subject, date_at=env.date, body_text=parsed.text,
        snippet=rt.snippet(new_text or parsed.text), message_id=env.message_id, in_reply_to=" ".join(env.in_reply_to),
        references=" ".join(env.references), thread_id=found.match.mail_id if found.match else None,
        company_id=found.match.company_id if found.match else None, folder=found.folder, uidvalidity=found.uidvalidity, uid=found.uid,
        attachments=json.dumps([{"name": a.name, "content_type": a.content_type, "size": a.size} for a in parsed.attachments]) if parsed.attachments else "",
        match_method=found.match.method if found.match else "", match_weak=bool(found.match and found.match.weak),
        other_mailbox=bool(found.match and found.match.other_mailbox), bounce_type=found.bounce_type,
        optout_hint=parsed.kind == rp.KIND_HUMAN and found.direction == rp.DIRECTION_IN and rt.optout_hint(new_text),
        thread_mailbox=origin.mailbox if origin else mailbox,
    )


def _apply_status(db: Database, message: rp.NewMessage, report: SyncReport) -> None:
    """Skutek odpowiedzi na status maila: pierwsza odpowiedź człowieka → „odpowiedział”, twardy zwrot → „zwrot”. Tylko z
    „wysłano” (nie nadpisuje tego, co człowiek ustawił), a auto-odpowiedź i miękki zwrot niczego nie zmieniają."""
    if not message.thread_id or message.direction != rp.DIRECTION_IN:
        return
    if message.kind == rp.KIND_HUMAN and db.transition_status(message.thread_id, [st.STATUS_SENT], st.STATUS_REPLIED):
        report.replied += 1
    elif message.kind == rp.KIND_BOUNCE and message.bounce_type == rp.BOUNCE_HARD \
            and db.transition_status(message.thread_id, [st.STATUS_SENT], st.STATUS_BOUNCED):
        report.bounced += 1


def _persist(db: Database, store: ReplyStore, mailbox_report: MailboxReport, scans: list, now: datetime, report: SyncReport,
             sent_by_id: Mapping) -> None:
    """Zapisuje znalezione wiadomości i dopiero potem przesuwa kursory folderów (przy `dry_run` tylko liczy)."""
    dry = report.dry_run
    for scan in scans:
        mailbox_report.seen += scan.seen
        mailbox_report.folders.append({"folder": scan.folder, "seen": scan.seen, "found": len(scan.found)})
        for found in scan.found:
            message = _new_message(found, mailbox_report.mailbox, sent_by_id)
            mailbox_report.candidates += 1
            mailbox_report.kinds[f"{found.direction}:{message.kind}"] += 1
            mailbox_report.unassigned += message.thread_id is None
            if (store.has_identity(message.identity) if dry else store.save_message(message, now) is None):
                mailbox_report.duplicates += 1
                continue
            mailbox_report.stored += 1
            if not dry:
                _apply_status(db, message, report)
        if scan.cursor is not None and not dry:
            store.save_cursor(mailbox_report.mailbox, scan.folder, scan.cursor, now)


# ---- przebieg ----------------------------------------------------------------------------------------------

def options_from_config(config: Mapping, *, since: Optional[date] = None, only: str = "", dry_run: bool = False) -> SyncOptions:
    """Opcje przebiegu z `config.env` (REPLIES_*): wspólne dla `ai.py odpowiedzi pobierz` i dla panelu."""
    settings = cfg.ReplySettings.from_config(config)
    return SyncOptions(ignore=settings.ignore, folders=settings.folders, since=since, workers=settings.workers, only=only, dry_run=dry_run)


def _snapshot(db: Database, store: ReplyStore, config: Mapping, options: SyncOptions, now: datetime,
              clock: Callable[[], datetime]) -> Optional[Snapshot]:
    first = store.first_sent_at()
    if first is None:
        return None
    primary = (config.get("SMTP_USER") or "").strip().lower()
    index = rm.ReplyIndex([rm.sent_mail_from_row(row, primary) for row in store.sent_mails()])
    own = tuple(box.user.lower() for box in cfg.configured_mailboxes(config))
    since = options.since or (first - FIRST_RUN_MARGIN).date()
    return Snapshot(index, store.all_known_threads(), own, tuple(options.ignore), since, now + options.budget, clock)


def _mailbox_settings(config: Mapping, only: str) -> tuple:
    """(skrzynki gotowe do odbioru, raporty błędów konfiguracji): niekompletna skrzynka nie wyłącza pozostałych."""
    ready: list = []
    broken: list = []
    for slot in cfg.imap_slot_numbers(config):
        try:
            settings = cfg.ImapSettings.from_config(config, slot)
        except cfg.ConfigError as exc:
            broken.append(MailboxReport(f"skrzynka {slot}", slot, ok=False, error=str(exc), kind="config"))
            continue
        if not only or settings.address.lower() == only.lower():
            ready.append(settings)
    return ready, broken


def fetch_replies(db: Database, store: ReplyStore, config: Mapping, factory: Callable, *, options: SyncOptions = SyncOptions(),  # noqa: B008 — niemutowalny
                  now: Optional[datetime] = None, clock: Optional[Callable[[], datetime]] = None) -> SyncReport:
    """Jeden przebieg pobierania odpowiedzi ze wszystkich skrzynek z IMAP. `factory(ImapSettings)` daje sesję (menedżer
    kontekstu); w testach atrapa. Z `options.dry_run` nic nie jest zapisywane, tylko liczone."""
    now = now or db.now()
    report = SyncReport(dry_run=options.dry_run)
    ready, report.mailboxes = _mailbox_settings(config, options.only)
    if not ready:
        report.skipped = report.skipped or ("brak skrzynek z ustawionym IMAP" if not report.mailboxes else "")
        return report
    token = uuid.uuid4().hex
    if not options.dry_run and not store.acquire_run(token, now):
        report.skipped = "inny przebieg pobierania trwa"
        return report
    try:
        snap = _snapshot(db, store, config, options, now, clock or db.now)
        if snap is None:
            report.skipped = "nie wysłano jeszcze żadnego maila"
            return report
        _run(db, store, ready, factory, (snap, options, now), report)
    finally:
        if not options.dry_run:
            store.finish_run(token, db.now(), report.totals())
    return report


def _run(db: Database, store: ReplyStore, ready: list, factory: Callable, context: tuple, report: SyncReport) -> None:
    snap, options, now = context
    sent_by_id = snap.index.by_id
    with ThreadPoolExecutor(max_workers=max(1, options.workers)) as pool:
        futures = [pool.submit(scan_mailbox, settings, factory, options.folders, _cursor_reader(store, settings), snap)
                   for settings in ready]
        for future in futures:
            mailbox_report, scans = future.result()
            _persist(db, store, mailbox_report, scans, now, report, sent_by_id)
            if not options.dry_run:
                _remember_outcome(store, mailbox_report, now)
            report.mailboxes.append(mailbox_report)


def _cursor_reader(store: ReplyStore, settings: cfg.ImapSettings) -> Callable:
    """Odczyt kursora folderu tej skrzynki (wołany z wątku roboczego; sam odczyt)."""
    mailbox = settings.address.lower()
    return lambda folder: store.get_cursor(mailbox, folder)


def _remember_outcome(store: ReplyStore, mailbox_report: MailboxReport, now: datetime) -> None:
    if mailbox_report.ok:
        store.save_cursor(mailbox_report.mailbox, "", Cursor(0, 0), now)  # czyści błąd skrzynki
    else:
        store.record_error(mailbox_report.mailbox, mailbox_report.error, mailbox_report.kind, now)
