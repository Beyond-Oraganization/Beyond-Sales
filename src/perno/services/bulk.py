"""Zbiorowa zmiana statusu i danych firm: podgląd (dry-run), zapis w jednej transakcji i cofanie.

Reguły statusów są te same co przy edycji pojedynczej (`statuses.resolve_sent_at`). Operacja nigdy nie zmienia
wiersza „Wysyłanie…" (ten należy do programu) i nie ustawia „Do wysłania" firmie bez e-maila lub treści.
Ostrzeżenia dotyczą zmian, których skutków nie widać na pierwszy rzut oka (np. ponowny mail do firmy, której
już pisaliśmy) — zapis ich wymaga jawnego potwierdzenia; bez niego nic się nie zmienia.
"""

from __future__ import annotations

import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional

import sqlalchemy as sa

from perno import db as dbmod
from perno.db import Database, DbError, InvalidValue, maile
from perno.domain import countries
from perno.domain import statuses as st

MAX_BULK_IDS = 2000
UNDO_LIMIT = 20
DATA_FIELDS = ("country", "city", "address")
LISTED_IDS = 20  # tyle id pokazujemy przy każdym ostrzeżeniu i pominięciu (reszta tylko w liczbie)

SKIP_TEXT = {
    "not_found": "nie ma takiej firmy w bazie",
    "locked": "mail jest właśnie wysyłany — poczekaj na wynik",
    "not_ready": "brak e-maila lub treści, więc nie może mieć statusu „Do wysłania”",
}

# kod ostrzeżenia → opis; komunikat dla użytkownika to opis + liczba firm
WARNING_TEXT = {
    "resend_risk": "Te firmy wrócą na listę wysyłki, a zapisana data wysłania zniknie — program może wysłać do nich maila ponownie",
    "drops_sent_at": "Tym firmom zniknie zapisana data wysłania",
    "manual_sent": "Te firmy zostaną oznaczone jako wysłane (z datą „teraz”), choć program niczego do nich nie wysłał",
    "reopens_blocked": "Te firmy miały status „Nie kontaktować” — po zmianie wrócą do puli wysyłkowej",
}


class AckRequired(DbError):
    """Zapis wymaga potwierdzenia ostrzeżeń, a ich nie potwierdzono. Niesie wynik analizy (`plan`)."""

    def __init__(self, plan: "Plan"):
        super().__init__("Potwierdź ostrzeżenia, zanim zmienię dane.")
        self.plan = plan


@dataclass(frozen=True)
class BulkRequest:
    ids: tuple
    status: Optional[str] = None
    data: Mapping = field(default_factory=dict)  # kraj/miasto/adres → nowa wartość (None = wyczyść); tylko pola do zmiany
    acknowledged: frozenset = frozenset()


def parse_request(payload: Mapping) -> BulkRequest:
    """Waliduje treść żądania. `changes`: `status` i/lub `country`/`city`/`address`; brak pola = bez zmian,
    pusty tekst = wyczyść. Błędy zgłasza jako `InvalidValue` (panel pokazuje komunikat)."""
    ids = payload.get("ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids):
        raise InvalidValue("Pole ids musi być listą identyfikatorów firm.")
    unique = tuple(dict.fromkeys(ids))
    if not unique:
        raise InvalidValue("Zaznacz przynajmniej jedną firmę.")
    if len(unique) > MAX_BULK_IDS:
        raise InvalidValue(f"Za dużo firm naraz (maks. {MAX_BULK_IDS}) — zawęź zaznaczenie.")

    changes = payload.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise InvalidValue("Podaj, co zmienić (pole changes).")
    unknown = [name for name in changes if name != "status" and name not in DATA_FIELDS]
    if unknown:
        raise InvalidValue(f"Nie można zmieniać pola: {unknown[0]}.")

    status = None
    if "status" in changes:
        status = changes["status"]
        if not isinstance(status, str) or status not in st.VALID_STATUSES:
            raise InvalidValue(f"Nieprawidłowy status: {status!r}.")
        if status == st.STATUS_SENDING:
            raise InvalidValue("Statusu „Wysyłanie…” nie można ustawić ręcznie.")

    data = {}
    for name in DATA_FIELDS:
        if name not in changes:
            continue
        value = changes[name]
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise InvalidValue(f"Pole {name} musi być tekstem.")
        data[name] = dbmod.from_api({name: value.strip()}, partial=True)[name]  # kraj → kod ISO, pusty tekst → None

    acknowledged = payload.get("acknowledge", [])
    if not isinstance(acknowledged, list) or not all(isinstance(code, str) for code in acknowledged):
        raise InvalidValue("Pole acknowledge musi być listą kodów ostrzeżeń.")
    return BulkRequest(unique, status, data, frozenset(acknowledged))


def describe(request: BulkRequest, count: int) -> str:
    """Krótki opis operacji do przycisku „Cofnij": status „Odpowiedział”, kraj Polska (37)."""
    parts = []
    if request.status is not None:
        parts.append(f"status „{st.STATUS_META[request.status][0]}”")
    names = {"country": "kraj", "city": "miasto", "address": "adres"}
    cleared = {"country": "kraj wyczyszczony", "city": "miasto wyczyszczone", "address": "adres wyczyszczony"}
    for name, value in request.data.items():
        if value is None:
            parts.append(cleared[name])
        elif name == "country":
            parts.append(f"kraj {countries.country_name(value) or value}")
        else:
            parts.append(f"{names[name]} „{value}”")
    return f"{', '.join(parts)} ({count})"


# ---- analiza -----------------------------------------------------------------------------------

@dataclass
class RowChange:
    id: str
    before: dict  # tylko pola, które operacja zmienia
    after: dict


@dataclass
class Plan:
    requested: int = 0
    found: int = 0
    changes: list = field(default_factory=list)  # [RowChange]
    unchanged: list = field(default_factory=list)  # id wierszy, które już są takie, jak chce operacja
    skipped: list = field(default_factory=list)  # [(id, kod powodu)]
    warnings: dict = field(default_factory=dict)  # kod → [id]
    by_status: Counter = field(default_factory=Counter)  # statusy wybranych firm przed zmianą

    @property
    def needs_ack(self) -> list:
        return [code for code in WARNING_TEXT if self.warnings.get(code)]

    def to_json(self) -> dict:
        skipped = Counter(code for _, code in self.skipped)
        return {
            "requested": self.requested,
            "found": self.found,
            "changed": len(self.changes),
            "unchanged": len(self.unchanged),
            "skipped_count": len(self.skipped),
            "skipped_by_reason": dict(skipped),
            "skipped": [{"id": row_id, "reason": code, "message": SKIP_TEXT[code]}
                        for row_id, code in self.skipped[:LISTED_IDS]],
            "warnings": [{"code": code, "count": len(self.warnings[code]), "ids": self.warnings[code][:LISTED_IDS],
                          "message": f"{WARNING_TEXT[code]}. Dotyczy firm: {len(self.warnings[code])}."}
                         for code in self.needs_ack],
            "needs_ack": self.needs_ack,
            "by_status": dict(self.by_status),
        }


_HAS_BODY = sa.case((sa.and_(maile.c.body_html.is_not(None), maile.c.body_html != ""), 1), else_=0).label("has_body")


def _load(conn, ids, lock: bool) -> dict:
    """Wiersze o podanych id (tylko pola potrzebne do analizy — bez treści maili), czytane partiami w kolejności id.
    Przy `lock` zakładają blokady wierszy (SELECT … FOR UPDATE) zawsze w tej samej kolejności, więc dwie jednoczesne
    operacje nie zakleszczą się nawzajem."""
    columns = [maile.c.id, maile.c.status, maile.c.sent_at, maile.c.email, maile.c.country, maile.c.city,
               maile.c.address, _HAS_BODY]
    found = {}
    ordered = sorted(ids)
    for start in range(0, len(ordered), dbmod.ID_CHUNK):
        stmt = sa.select(*columns).where(maile.c.id.in_(ordered[start:start + dbmod.ID_CHUNK])).order_by(maile.c.id)
        if lock:
            stmt = stmt.with_for_update()
        for row in conn.execute(stmt).mappings():
            found[row["id"]] = dict(row)
    return found


def analyze(found: Mapping, request: BulkRequest, now: datetime) -> Plan:
    """Co zmieniłaby operacja na wczytanych wierszach — bez zapisu. Każdy wiersz trafia do dokładnie jednej grupy:
    zmieniany, bez zmian albo pominięty (z powodem); ostrzeżenia zbierane są tylko dla zmienianych."""
    plan = Plan(requested=len(request.ids), found=len(found))
    for row_id in request.ids:
        row = found.get(row_id)
        if row is None:
            plan.skipped.append((row_id, "not_found"))
            continue
        plan.by_status[row["status"]] += 1
        if row["status"] == st.STATUS_SENDING:
            plan.skipped.append((row_id, "locked"))
            continue
        if request.status == st.STATUS_PENDING and row["status"] != st.STATUS_PENDING \
                and not (row["email"] and row["has_body"]):
            plan.skipped.append((row_id, "not_ready"))
            continue

        before, after = {}, {}
        if request.status is not None and request.status != row["status"]:
            sent_at = st.resolve_sent_at(request.status, row["sent_at"], now)
            before["status"], after["status"] = row["status"], request.status
            if sent_at != row["sent_at"]:
                before["sent_at"], after["sent_at"] = row["sent_at"], sent_at
        for name, value in request.data.items():
            if (row[name] or None) != value:
                before[name], after[name] = row[name], value
        if not after:
            plan.unchanged.append(row_id)
            continue
        plan.changes.append(RowChange(row_id, before, after))
        for code in _warnings_for(row, after):
            plan.warnings.setdefault(code, []).append(row_id)
    return plan


def _warnings_for(row: Mapping, after: Mapping) -> list:
    codes = []
    new_status = after.get("status")
    if new_status is None:
        return codes
    if row["sent_at"] is not None and "sent_at" in after and after["sent_at"] is None:
        codes.append("resend_risk" if new_status in st.SENDABLE_STATUSES else "drops_sent_at")
    if new_status == st.STATUS_SENT and row["sent_at"] is None:
        codes.append("manual_sent")
    if row["status"] == st.STATUS_DO_NOT_CONTACT and new_status in st.SENDABLE_STATUSES:
        codes.append("reopens_blocked")
    return codes


def preview(db: Database, request: BulkRequest) -> Plan:
    """Dry-run: analiza na bieżących danych, bez blokad i bez zapisu."""
    with db.engine.connect() as conn:
        return analyze(_load(conn, request.ids, lock=False), request, db.now())


# ---- zapis i cofanie ---------------------------------------------------------------------------

@dataclass
class Operation:
    id: str
    at: datetime
    label: str
    changes: list  # [RowChange]


class UndoLog:
    """Ostatnie zbiorcze zmiany (tylko w pamięci serwera — znikają przy jego restarcie)."""

    def __init__(self, limit: int = UNDO_LIMIT):
        self._limit = limit
        self._items: list = []
        self._lock = threading.Lock()
        self._undo_lock = threading.Lock()

    def record(self, operation: Operation) -> None:
        with self._lock:
            self._items.append(operation)
            del self._items[:-self._limit]

    def get(self, operation_id: Optional[str] = None) -> Optional[Operation]:
        with self._lock:
            if operation_id is None:
                return self._items[-1] if self._items else None
            return next((item for item in self._items if item.id == operation_id), None)

    def discard(self, operation_id: str) -> None:
        with self._lock:
            self._items = [item for item in self._items if item.id != operation_id]

    def listing(self) -> list:
        """Najnowsze pierwsze: `[{id, at, label, count}]`."""
        with self._lock:
            return [{"id": item.id, "at": item.at.strftime("%Y-%m-%dT%H:%M:%S"), "label": item.label,
                     "count": len(item.changes)} for item in reversed(self._items)]

    @contextmanager
    def exclusive_undo(self):
        """Cofanie jednej operacji naraz — dwa równoległe kliknięcia nie cofną tego samego dwa razy."""
        with self._undo_lock:
            yield


def apply(db: Database, request: BulkRequest, log: UndoLog) -> dict:
    """Zapisuje zmianę w jednej transakcji. Brak potwierdzenia któregoś z ostrzeżeń → `AckRequired` i zero zmian."""
    now = db.now()
    with db.engine.begin() as conn:
        plan = analyze(_load(conn, request.ids, lock=True), request, now)
        if any(code not in request.acknowledged for code in plan.needs_ack):
            raise AckRequired(plan)
        for change in plan.changes:
            conn.execute(sa.update(maile).where(maile.c.id == change.id).values(**change.after, updated_at=now))
    result = plan.to_json()
    if plan.changes:
        operation = Operation(uuid.uuid4().hex[:12], now, describe(request, len(plan.changes)), plan.changes)
        log.record(operation)
        result["operation"] = {"id": operation.id, "at": now.strftime("%Y-%m-%dT%H:%M:%S"),
                               "label": operation.label, "count": len(plan.changes)}
    return result


def undo(db: Database, log: UndoLog, operation_id: Optional[str] = None) -> dict:
    """Cofa operację (domyślnie najnowszą). Przywraca tylko te pola, które nadal mają wartość ustawioną przez nią —
    późniejsze zmiany (ręczna edycja, wysyłka, inna operacja) zostają, a wiersz trafia do `conflicts`.
    Status i data wysłania wracają razem albo wcale, żeby nie powstał stan, którego nikt nie ustawił."""
    with log.exclusive_undo():
        operation = log.get(operation_id)
        if operation is None:
            raise dbmod.RowNotFound("Nie ma takiej operacji do cofnięcia (dziennik jest w pamięci serwera i znika po jego restarcie).")
        now = db.now()
        restored, conflicts = 0, []
        with db.engine.begin() as conn:
            found = _load(conn, [change.id for change in operation.changes], lock=True)
            for change in operation.changes:
                row = found.get(change.id)
                if row is None:
                    conflicts.append({"id": change.id, "message": "nie ma już tej firmy w bazie"})
                    continue
                restore, blocked = {}, []
                if "status" in change.after:
                    unit = ["status"] + (["sent_at"] if "sent_at" in change.after else [])
                    if all(row[name] == change.after[name] for name in unit):
                        restore.update({name: change.before[name] for name in unit})
                    else:
                        blocked.append("status")
                for name in DATA_FIELDS:
                    if name in change.after:
                        if (row[name] or None) == change.after[name]:
                            restore[name] = change.before[name]
                        else:
                            blocked.append(name)
                if blocked:
                    conflicts.append({"id": change.id, "message": f"zmienione po tej operacji: {', '.join(blocked)}"})
                if restore:
                    conn.execute(sa.update(maile).where(maile.c.id == change.id).values(**restore, updated_at=now))
                    restored += 1
        log.discard(operation.id)
        return {"label": operation.label, "total": len(operation.changes), "restored": restored,
                "conflicts": conflicts[:LISTED_IDS], "conflict_count": len(conflicts)}
