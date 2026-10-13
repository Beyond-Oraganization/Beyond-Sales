"""Wspólne wyjście poleceń `ai.py`, które zapisują plan z pliku (oceny, szkice, leady): flagi, wypis planu i odmowa przy błędach.

Plan to obiekt z listami `items` (poprawne wpisy z polem `id`), `warnings` i `errors` (pary `(id, komunikat)`). Wypis nigdy nie
zawiera treści wpisów, tylko id i krótką etykietę podaną przez polecenie."""

from __future__ import annotations

import sys


def plan_flags(parser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Sprawdź i pokaż, co by się zapisało; nic nie zapisuje.")
    parser.add_argument("--czesciowo", action="store_true", help="Zapisz poprawne wpisy, wypisz odrzucone (domyślnie wszystko albo nic).")


def print_plan(plan, labels: dict) -> None:
    """`OK id etykieta` na stdout, `UWAGA id: …` na stdout, `BŁĄD id: …` na stderr."""
    for item in plan.items:
        print(f"  OK  {item['id']:<22} {labels.get(item['id'], '')}")
    for item_id, message in plan.warnings:
        print(f"  UWAGA {item_id}: {message}")
    for item_id, message in plan.errors:
        print(f"  BŁĄD {item_id}: {message}", file=sys.stderr)


def refused(plan, args) -> bool:
    """True (i komunikat), gdy plan ma błędy, a nie podano `--czesciowo`: wtedy nic się nie zapisuje."""
    if plan.errors and not args.czesciowo:
        print(f"Przerwano: {len(plan.errors)} błędów, nic nie zapisano (popraw plik albo użyj --czesciowo).", file=sys.stderr)
        return True
    return False
