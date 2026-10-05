#!/usr/bin/env bash
# Tymczasowy, odizolowany serwer MySQL do testów i pomiarów: nie dotyka Twojej bazy, konfiguracji ani config.env.
#
#   scripts/scratch_mysql.sh start    # przy pierwszym użyciu inicjalizuje katalog danych i uruchamia serwer
#   scripts/scratch_mysql.sh url      # wypisuje adres dla PERNO_TEST_DB_URL
#   scripts/scratch_mysql.sh status   # czy serwer odpowiada
#   scripts/scratch_mysql.sh stop     # zatrzymuje serwer (dane zostają)
#   scripts/scratch_mysql.sh reset    # zatrzymuje serwer i kasuje katalog danych
#
# Przykład (test dymny na prawdziwym MySQL):
#   scripts/scratch_mysql.sh start
#   PERNO_TEST_DB_URL="$(scripts/scratch_mysql.sh url)" .venv/bin/python -m pytest tests/test_mysql_smoke.py
#
# Serwer słucha wyłącznie na 127.0.0.1, użytkownik `root` nie ma hasła, a dane są syntetyczne: nie trzymaj tu
# niczego prawdziwego. Używa `--no-defaults`, więc nie czyta ani nie psuje ustawień Twojego serwera.
# Zmienne: PERNO_SCRATCH_MYSQL_DIR (katalog danych), PERNO_SCRATCH_MYSQL_PORT (domyślnie 33307).
set -euo pipefail

DIR="${PERNO_SCRATCH_MYSQL_DIR:-${TMPDIR:-/tmp}/perno-scratch-mysql}"
PORT="${PERNO_SCRATCH_MYSQL_PORT:-33307}"
DATABASE="perno_maile_test"

MYSQLD="$(command -v mysqld || true)"
MYSQL="$(command -v mysql || true)"
MYSQLADMIN="$(command -v mysqladmin || true)"
if [ -z "$MYSQLD" ] || [ -z "$MYSQL" ] || [ -z "$MYSQLADMIN" ]; then
    echo "Brak mysqld/mysql/mysqladmin w PATH (macOS: brew install mysql)." >&2
    exit 1
fi
BASEDIR="$(dirname "$(dirname "$(readlink -f "$MYSQLD")")")"

client() { "$MYSQL" --no-defaults -h127.0.0.1 -P"$PORT" -uroot "$@"; }
alive() { "$MYSQLADMIN" --no-defaults -h127.0.0.1 -P"$PORT" -uroot ping >/dev/null 2>&1; }

start() {
    if alive; then
        echo "Serwer już działa na 127.0.0.1:$PORT."
        return
    fi
    mkdir -p "$DIR"
    if [ ! -d "$DIR/data/mysql" ]; then
        echo "Inicjalizacja katalogu danych w $DIR/data ..."
        "$MYSQLD" --no-defaults --initialize-insecure --basedir="$BASEDIR" --datadir="$DIR/data" \
            --log-error="$DIR/init.log" >/dev/null 2>&1 || { echo "Inicjalizacja nie powiodła się, zobacz $DIR/init.log" >&2; exit 1; }
    fi
    # Gniazdo podajemy jako ścieżkę względną (cd do $DIR): ścieżki gniazd mają limit ~100 znaków, a katalog bywa głęboko.
    # --mysqlx=OFF nie zajmuje portu 33060 (zwykle ma go już Twój serwer), a własne gniazdo nie koliduje z /tmp/mysql.sock.
    # W tle idzie wyłącznie mysqld z przekierowanymi deskryptorami; gdyby `&` objęło też `cd`, podpowłoka trzymałaby
    # standardowe wyjście wywołującego (np. narzędzia albo potoku) aż do zatrzymania serwera.
    (
        cd "$DIR" || exit 1
        nohup "$MYSQLD" --no-defaults --basedir="$BASEDIR" --datadir="$DIR/data" --port="$PORT" \
            --bind-address=127.0.0.1 --mysqlx=OFF --socket=m.sock --pid-file="$DIR/mysqld.pid" --log-error="$DIR/error.log" \
            --innodb-buffer-pool-size=512M --max-connections=300 --secure-file-priv=NULL >"$DIR/out.log" 2>&1 </dev/null &
    )
    for _ in $(seq 1 60); do
        if alive; then
            client -e "CREATE DATABASE IF NOT EXISTS $DATABASE CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            echo "Serwer działa: 127.0.0.1:$PORT, baza $DATABASE. Adres: $(url)"
            return
        fi
        sleep 0.5
    done
    echo "Serwer nie wystartował w 30 s, zobacz $DIR/error.log" >&2
    exit 1
}

url() { echo "mysql+pymysql://root@127.0.0.1:$PORT/$DATABASE"; }

stop() {
    if alive; then
        "$MYSQLADMIN" --no-defaults -h127.0.0.1 -P"$PORT" -uroot shutdown
        echo "Serwer zatrzymany."
    else
        echo "Serwer nie działa."
    fi
}

case "${1:-}" in
    start) start ;;
    url) url ;;
    status) if alive; then echo "działa ($(url))"; else echo "nie działa"; exit 1; fi ;;
    stop) stop ;;
    reset) stop; rm -rf "$DIR"; echo "Katalog danych usunięty." ;;
    *) sed -n '2,13p' "$0"; exit 2 ;;
esac
