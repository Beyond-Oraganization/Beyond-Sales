"""Atrapy poczty przychodzącej: nigdy nie łączą się z siecią.

`FakeConnection` udaje obiekt `imaplib.IMAP4` (do testów samego klienta `ImapClient`), a `FakeImap` udaje całą sesję
`ImapSession` (do testów pobierania odpowiedzi). Obie zapamiętują wywołania, żeby testy mogły sprawdzić, że odczyt
niczego nie zmienia na serwerze i że treść niepowiązanych wiadomości nie jest w ogóle pobierana.
"""

from __future__ import annotations

import imaplib
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Optional, Sequence

from perno.domain import imap_folders
from perno.mailer.imap_client import HEADER_FIELDS, FolderState, ImapError

WRITE_COMMANDS = {"STORE", "COPY", "MOVE", "EXPUNGE", "DELETE", "CREATE", "RENAME"}


def make_eml(*, sender: str = "klient@firma.pl", to: str = "roman@z3x.pl", subject: str = "Re: Temat", body: str = "Dzień dobry",
             message_id: str = "", in_reply_to: str = "", references: str = "", when: Optional[datetime] = None,
             html: str = "", headers: Optional[dict] = None, sender_name: str = "") -> bytes:
    """Przykładowa wiadomość przychodząca jako bajty RFC 5322 (do wstawienia do atrapy skrzynki)."""
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = format_datetime((when or datetime(2026, 10, 2, 12, 0)).astimezone())
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    for name, value in (headers or {}).items():
        msg[name] = value
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


class FakeConnection:
    """Minimalna atrapa `imaplib.IMAP4`: odpowiada z góry zadanymi danymi i zapisuje każde polecenie."""

    def __init__(self, *, list_lines: Sequence[bytes] = (), exists: bytes = b"3", uidvalidity: bytes = b"7", uidnext: bytes = b"100",
                 login_error: Optional[Exception] = None, search_result: bytes = b"", fetch_reply: Optional[list] = None,
                 status: str = "OK"):
        self.list_lines, self.exists, self.state = list(list_lines), exists, {"UIDVALIDITY": uidvalidity, "UIDNEXT": uidnext}
        self.login_error, self.search_result, self.fetch_reply, self.status = login_error, search_result, fetch_reply or [], status
        self.calls: list = []

    def login(self, user, password):
        self.calls.append(("login", user))
        if self.login_error is not None:
            raise self.login_error
        return "OK", [b"logged in"]

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b"bye"]

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def list(self):
        self.calls.append(("list",))
        return self.status, list(self.list_lines)

    def select(self, mailbox="INBOX", readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return self.status, [self.exists]

    def response(self, code):
        return code, [self.state[code]]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return self.status, [self.search_result]
        return self.status, list(self.fetch_reply)

    def append(self, mailbox, flags, date_time, message):
        self.calls.append(("append", mailbox, flags, message))
        return self.status, [b"appended"]

    def commands(self) -> list:
        """Nazwy wywołanych poleceń IMAP (do sprawdzenia, że nie było żadnego zapisu)."""
        return [str(call[1] if call[0] == "uid" else call[0]).upper() for call in self.calls]


class FakeImap:
    """Atrapa sesji `ImapSession` z foldery → {uid: bajty wiadomości}. Zapisuje, co pobrano i co dopisano."""

    def __init__(self, folders: Optional[dict] = None, *, roles: Optional[dict] = None, uidvalidity: int = 1,
                 fail_with: Optional[Exception] = None):
        self.folders = {name: dict(items) for name, items in (folders or {"INBOX": {}}).items()}
        self.roles = roles or {}
        self.uidvalidity = uidvalidity
        self.fail_with = fail_with
        self.selected: Optional[str] = None
        self.header_fetches: list = []
        self.full_fetches: list = []
        self.searches: list = []
        self.appended: list = []
        self.opened_readonly = True

    def __enter__(self) -> "FakeImap":
        if self.fail_with is not None:
            raise self.fail_with
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def add(self, folder: str, uid: int, raw: bytes) -> None:
        self.folders.setdefault(folder, {})[uid] = raw

    def list_folders(self) -> list:
        return [imap_folders.Folder(name, (), self.roles.get(name, imap_folders.classify(name))) for name in self.folders]

    def select(self, folder: str) -> FolderState:
        if folder not in self.folders:
            raise ImapError(f"Brak folderu {folder}.")
        self.selected = folder
        items = self.folders[folder]
        return FolderState(self.uidvalidity, max(items, default=0) + 1, len(items))

    def search_uids(self, *, since: Optional[date] = None, after_uid: int = 0) -> list:
        self.searches.append((self.selected, since, after_uid))
        return sorted(uid for uid in self.folders[self.selected or "INBOX"] if uid > after_uid)

    def fetch_headers(self, uids: Sequence[int]) -> dict:
        self.header_fetches.append(list(uids))
        out = {}
        for uid in uids:
            message = _parse(self.folders[self.selected or "INBOX"][uid])
            wanted = {name.lower() for name in HEADER_FIELDS}
            out[uid] = b"".join(f"{name}: {value}\r\n".encode() for name, value in message.items() if name.lower() in wanted) + b"\r\n"
        return out

    def fetch_message(self, uid: int) -> bytes:
        self.full_fetches.append(uid)
        return self.folders[self.selected or "INBOX"][uid]

    def append(self, folder: str, message: bytes, when: datetime) -> None:
        self.appended.append((folder, message, when))


def _parse(raw: bytes):
    from email import message_from_bytes, policy

    return message_from_bytes(raw, policy=policy.default)


class FakeAccounts:
    """Fabryka sesji dla `fetch_replies`: jedna atrapa na skrzynkę (adres), a próba otwarcia nieznanej to błąd."""

    def __init__(self, sessions: dict):
        self.sessions = sessions
        self.opened: list = []

    def __call__(self, settings) -> FakeImap:
        self.opened.append(settings.address)
        return self.sessions[settings.address]


def auth_failure() -> imaplib.IMAP4.error:
    return imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
