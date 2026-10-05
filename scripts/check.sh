#!/usr/bin/env bash
# Bramka jakości projektu: lint, typy, testy. Uruchamiaj z dowolnego katalogu:
#
#   scripts/check.sh            pełna kontrola (przed commitem fazy)
#   scripts/check.sh --fast     bez testów oznaczonych `slow` (szybka pętla przy refaktoryzacji)
#   scripts/check.sh --audit    dodatkowo audyt zależności (pip-audit; wymaga internetu)
#   scripts/check.sh --mysql    dodatkowo testy na prawdziwym MySQL: tymczasowy serwer (scripts/scratch_mysql.sh), testy dymne
#                               bazy i wektory listy; serwer uruchomiony przez ten krok jest na końcu zatrzymywany
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
mysql=0
for arg in "$@"; do
    case "$arg" in
        --fast) fast=1 ;;
        --audit) audit=1 ;;
        --mysql) mysql=1 ;;
        *) echo "Nieznana opcja: $arg (dozwolone: --fast, --audit, --mysql)" >&2; exit 2 ;;
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
    # Pełny przebieg pilnuje też pokrycia (stan z fazy 3: 93%; próg 90% przepuszcza drobne wahania, łapie dziury).
    .venv/bin/python -m pytest -n auto --cov=perno --cov-report=term:skip-covered --cov-fail-under=90
fi

if [ "$mysql" = 1 ]; then
    step "MySQL (tymczasowy serwer: testy dymne i wektory listy)"
    started_here=0
    scripts/scratch_mysql.sh status >/dev/null 2>&1 || started_here=1
    scripts/scratch_mysql.sh start
    result=0
    PERNO_TEST_DB_URL="$(scripts/scratch_mysql.sh url)" .venv/bin/python -m pytest -p no:xdist -p no:cacheprovider \
        tests/test_mysql_smoke.py tests/test_listing.py || result=$?
    [ "$started_here" = 1 ] && scripts/scratch_mysql.sh stop
    [ "$result" = 0 ] || exit "$result"
fi

if [ "$audit" = 1 ]; then
    step "pip-audit"
    .venv/bin/pip-audit
fi

printf '\nOK: wszystkie kontrole przeszły.\n'
