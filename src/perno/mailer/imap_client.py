"""Cienki wrapper nad imaplib: odczyt poczty bez żadnych zmian na serwerze oraz dopisanie kopii wysłanej odpowiedzi.

Odczyt jest wyłącznie `EXAMINE` + `BODY.PEEK`, więc wiadomości zostają nieprzeczytane, a flagi i foldery nietknięte.
Jedyny zapis to `append` (kopia odpowiedzi z panelu do folderu Wysłane). Klient nie zna bazy ani reguł dopasowania.
"""

from __future__ import annotations

import base64
import imaplib
import re
import ssl
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Optional, Protocol, Sequence

from perno.domain import imap_folders

TIMEOUT = 60  # sekundy na jedną operację sieciową
CHUNK = 100  # tyle wiadomości naraz w jednym FETCH
HEADER_FIELDS = (
    "FROM", "TO", "CC", "REPLY-TO", "DELIVERED-TO", "SUBJECT", "DATE", "MESSAGE-ID", "IN-REPLY-TO", "REFERENCES",
    "AUTO-SUBMITTED", "X-AUTOREPLY", "X-AUTORESPOND", "PRECEDENCE", "CONTENT-TYPE", "RETURN-PATH", "X-FAILED-RECIPIENTS",
)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # bez locale: IMAP wymaga angielskich
_UID = re.compile(rb"UID (\d+)")

KIND_AUTH = "auth"
KIND_NETWORK = "network"
KIND_PROTOCOL = "protocol"


class ImapError(Exception):
    """Błąd odbioru poczty. Komunikat nie zawiera haseł; `kind` odróżnia złe logowanie od awarii sieci."""

    def __init__(self, message: str, kind: str = KIND_PROTOCOL):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class FolderState:
    """Stan otwartego folderu: `uidvalidity` zmienia się, gdy serwer przenumerował wiadomości (kursor jest wtedy nieważny)."""

    uidvalidity: int
    uidnext: int
    exists: int


class ImapSession(Protocol):
    """To, czego potrzebuje reszta programu od połączenia IMAP (atrapa w testach implementuje to samo)."""

    def list_folders(self) -> list: ...
    def select(self, folder: str) -> FolderState: ...
    def search_uids(self, *, since: Optional[date] = None, after_uid: int = 0) -> list: ...
    def fetch_headers(self, uids: Sequence[int]) -> dict: ...
    def fetch_message(self, uid: int) -> bytes: ...
    def append(self, folder: str, message: bytes, when: datetime) -> None: ...


# ---- nazwy folderów: zmodyfikowany UTF-7 (RFC 3501, 5.1.3) ------------------------------------

def encode_folder(name: str) -> str:
    """Nazwa folderu na drut: znaki spoza ASCII jako `&base64-`, sam `&` jako `&-`."""
    out: list = []
    pending: list = []

    def flush() -> None:
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            out.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(out)


def decode_folder(wire: str) -> str:
    """Odwrotność `encode_folder`; uszkodzony fragment zostaje bez zmian zamiast rzucać wyjątek."""
    def one(match: "re.Match[str]") -> str:
        body = match.group(1)
        if not body:
            return "&"
        try:
            padded = body.replace(",", "/") + "=" * (-len(body) % 4)
            return base64.b64decode(padded).decode("utf-16-be")
        except ValueError:
            return match.group(0)

    return re.sub(r"&([A-Za-z0-9+,]*)-", one, wire)


def _quote(name: str) -> str:
    return '"' + encode_folder(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_date(day: date) -> str:
    """Data w postaci wymaganej przez SEARCH (`08-Oct-2026`), zawsze z angielską nazwą miesiąca."""
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def search_criteria(since: Optional[date], after_uid: int) -> str:
    """Kryterium SEARCH: wiadomości po danym UID (kolejne przebiegi), od dnia (pierwszy przebieg) albo wszystkie."""
    if after_uid > 0:
        return f"UID {after_uid + 1}:*"
    return f"SINCE {imap_date(since)}" if since else "ALL"


# ---- odpowiedzi serwera --------------------------------------------------------------------

def parse_fetch(data: Sequence) -> dict:
    """UID → bajty z odpowiedzi FETCH. Niektóre serwery podają UID dopiero po literale, w osobnym elemencie."""
    out: dict = {}
    pending: Optional[bytes] = None
    for item in data:
        if isinstance(item, tuple):
            found = _UID.search(item[0])
            if found:
                out[int(found.group(1))] = item[1]
                pending = None
            else:
                pending = item[1]
        elif isinstance(item, bytes) and pending is not None:
            found = _UID.search(item)
            if found:
                out[int(found.group(1))] = pending
            pending = None
    return out


def _check(response: tuple, action: str) -> list:
    status, data = response
    if status != "OK":
        raise ImapError(f"Serwer IMAP odrzucił polecenie {action}.")
    return list(data or [])


def _failure(exc: Exception) -> ImapError:
    text = str(exc)
    upper = text.upper()
    if isinstance(exc, imaplib.IMAP4.error):
        kind = KIND_AUTH if ("AUTH" in upper or "LOGIN" in upper or "CREDENTIALS" in upper) else KIND_PROTOCOL
    else:
        kind = KIND_NETWORK
    return ImapError(f"{type(exc).__name__}: {text[:200]}", kind)


class ImapClient:
    """Połączenie z jedną skrzynką. Użycie: `with ImapClient(...) as imap: imap.select("INBOX")`."""

    def __init__(self, host: str, port: int, user: str, password: str, use_ssl: bool = True,
                 connect: Optional[Callable] = None):
        self.host, self.port, self.user, self.password, self.use_ssl = host, port, user, password, use_ssl
        self._connect = connect
        self._conn: Any = None  # imaplib.IMAP4 albo atrapa z testów

    def __enter__(self) -> "ImapClient":
        try:
            self._conn = self._open()
            self._conn.login(self.user, self.password)
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            self._drop()
            raise _failure(exc) from None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._drop()

    def _open(self):
        if self._connect is not None:
            return self._connect(self.host, self.port)
        if self.use_ssl:
            return imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
        conn = imaplib.IMAP4(self.host, self.port, timeout=TIMEOUT)
        conn.starttls(ssl.create_default_context())
        return conn

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except (imaplib.IMAP4.error, OSError):
                pass  # połączenie i tak zamykamy
            self._conn = None

    def _run(self, action: str, call: Callable):
        try:
            return call()
        except ImapError:
            raise
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise _failure(exc) from None

    # ---- odczyt (nic nie zmienia na serwerze) ---------------------------------------------------

    def list_folders(self) -> list:
        """Foldery skrzynki jako `imap_folders.Folder` (z rolą), w kolejności serwera."""
        lines = _check(self._run("LIST", lambda: self._conn.list()), "LIST")
        folders = []
        for raw in lines:
            parsed = imap_folders.parse_list_line(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw))
            if parsed is None:
                continue
            wire, flags, delimiter = parsed
            name = decode_folder(wire)
            folders.append(imap_folders.Folder(name, flags, imap_folders.classify(name, flags, delimiter)))
        return folders

    def select(self, folder: str) -> FolderState:
        """Otwiera folder tylko do odczytu (EXAMINE)."""
        data = _check(self._run("EXAMINE", lambda: self._conn.select(_quote(folder), readonly=True)), "EXAMINE")
        exists = int(data[0]) if data and data[0] and bytes(data[0]).isdigit() else 0
        return FolderState(self._response_int("UIDVALIDITY"), self._response_int("UIDNEXT"), exists)

    def _response_int(self, name: str) -> int:
        _, data = self._conn.response(name)
        return int(data[-1]) if data and data[-1] else 0

    def search_uids(self, *, since: Optional[date] = None, after_uid: int = 0) -> list:
        """UID wiadomości: nowsze niż `after_uid`, a gdy go nie ma, od dnia `since` (bez obu: wszystkie)."""
        criteria = search_criteria(since, after_uid)
        data = _check(self._run("SEARCH", lambda: self._conn.uid("SEARCH", None, criteria)), "SEARCH")
        found = [int(token) for token in (data[0] or b"").split()] if data else []
        return sorted(uid for uid in found if uid > after_uid)  # `n:*` zawsze zwraca ostatnią wiadomość, nawet starszą

    def fetch_headers(self, uids: Sequence[int]) -> dict:
        """UID → surowe nagłówki (tylko pola potrzebne do dopasowania; treść wiadomości nie jest pobierana)."""
        fields = " ".join(HEADER_FIELDS)
        out: dict = {}
        for start in range(0, len(uids), CHUNK):
            batch = ",".join(str(uid) for uid in uids[start:start + CHUNK])
            data = _check(self._run("FETCH", lambda b=batch: self._conn.uid(
                "FETCH", b, f"(UID BODY.PEEK[HEADER.FIELDS ({fields})])")), "FETCH")
            out.update(parse_fetch(data))
        return out

    def fetch_message(self, uid: int) -> bytes:
        """Cała wiadomość (BODY.PEEK, więc zostaje nieprzeczytana)."""
        data = _check(self._run("FETCH", lambda: self._conn.uid("FETCH", str(uid), "(UID BODY.PEEK[])")), "FETCH")
        found = parse_fetch(data)
        if uid not in found:
            raise ImapError(f"Serwer nie zwrócił wiadomości {uid}.")
        return found[uid]

    # ---- jedyny zapis ---------------------------------------------------------------------------

    def append(self, folder: str, message: bytes, when: datetime) -> None:
        """Dopisuje kopię wysłanej wiadomości do folderu (jako przeczytaną)."""
        stamp = imaplib.Time2Internaldate(when.astimezone() if when.tzinfo else when.timestamp())
        _check(self._run("APPEND", lambda: self._conn.append(_quote(folder), "(\\Seen)", stamp, message)), "APPEND")
