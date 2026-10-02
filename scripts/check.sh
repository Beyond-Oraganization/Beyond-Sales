#!/usr/bin/env bash
# Bramka jakości projektu: lint, typy, testy. Uruchamiaj z dowolnego katalogu:
#
#   scripts/check.sh            pełna kontrola (przed commitem fazy)
#   scripts/check.sh --fast     bez testów oznaczonych `slow` (szybka pętla przy refaktoryzacji)
#   scripts/check.sh --audit    dodatkowo audyt zależności (pip-audit; wymaga internetu)
#
# Testy JavaScript (tests/js) uruchamia sam zestaw Pythona (tests/test_js.py), więc nie ma osobnego kroku.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
    echo "Brak .venv — uruchom najpierw: uv sync" >&2
    exit 1
fi

fast=0
audit=0
for arg in "$@"; do
    case "$arg" in
        --fast) fast=1 ;;
        --audit) audit=1 ;;
        *) echo "Nieznana opcja: $arg (dozwolone: --fast, --audit)" >&2; exit 2 ;;
    esac
done

step() { printf '\n==> %s\n' "$1"; }

step "ruff"
.venv/bin/ruff check .

step "mypy"
.venv/bin/mypy

step "warstwy (import-linter)"
.venv/bin/lint-imports

step "testy"
if [ "$fast" = 1 ]; then
    .venv/bin/python -m pytest -n auto -m "not slow"
else
    .venv/bin/python -m pytest -n auto
fi

if [ "$audit" = 1 ]; then
    step "pip-audit"
    .venv/bin/pip-audit
fi

printf '\nOK: wszystkie kontrole przeszły.\n'
