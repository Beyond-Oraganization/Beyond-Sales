# Wysylka — zasady pracy w tym repozytorium

Lokalna aplikacja B2B: kolejka maili (MySQL, tabela `maile`), panel w przeglądarce (`server.py`), skraper stron firm.
Kod po angielsku; UI, komunikaty, README i szablony po polsku. Nazwy tabel (`maile`, `firmy`, `strony`…) i polecenia CLI się nie zmieniają.
Plan przebudowy (fazy 0-5): `~/.claude/plans/przeanalizuj-dany-projekt-przeczytaj-vivid-cook.md`. Kod jest w `src/perno/` (domain, db, mailer, services, scraper, web, cli); skrypty w katalogu głównym to cienkie wrappery.

## Komendy
- `uv sync` — środowisko (Python 3.13 w `.venv`)
- `scripts/check.sh` — ruff + mypy + testy; uruchom przed każdym commitem (`--fast` pomija testy `slow`)
- `.venv/bin/python -m pytest tests/test_x.py -k nazwa` — pojedyncze testy
- Automat wysyłki: `.venv/bin/python automat.py doctor|limits|start|status` (demon `run` uruchamia launchd; w testach nigdy prawdziwy `launchctl`, SMTP ani `osascript` — transporty są wstrzykiwane)
- Panel: `.venv/bin/python server.py` (albo `perno-server`); baza demo: `PERNO_DB_URL=sqlite:////tmp/demo.db` (blokuje prawdziwy SMTP)
- Prawdziwy MySQL do testów i pomiarów bez Twojej bazy: `scripts/check.sh --mysql` (tymczasowy serwer, testy dymne i wektory listy) albo ręcznie `scripts/scratch_mysql.sh start` + `PERNO_TEST_DB_URL="$(scripts/scratch_mysql.sh url)" .venv/bin/python -m pytest -p no:xdist tests/test_mysql_smoke.py tests/test_listing.py`; dane na skalę 50 tys. firm: `scripts/seed_synthetic.py` (tylko bazy `*_test`)

## Reguły
- Nigdy nie wysyłaj prawdziwych maili ani nie łącz się z prawdziwą bazą w testach. Nie czytaj i nie wypisuj `config.env`.
- Automat wysyłki: limity są w bazie (`automat_stan`) i każdy Start wymaga potwierdzenia aktualnych limitów; nie dodawaj ścieżki, która uzbraja plan bez tego potwierdzenia. Wysyłka zawsze przez `sending.send_row` (zajęcie wiersza), pętla w `services/automat.py`, reguły w `domain/automat_rules.py`, kanały w `notify/`, launchd i zasilanie w `system/`. Do powiadomień zewnętrznych (ntfy, e-mail) nie wkładaj adresów odbiorców.
- Zakładka Konfiguracja (`services/config_editor.py`, `domain/config_fields.py`): config.env zapisuje wyłącznie `settings.update_config` (atomowo, `.bak`, 0600). Wartości sekretów nigdy nie wracają do przeglądarki ani do komunikatów błędów; `DB_*` nie jest edytowalne z panelu. Nowy klucz w config.env = nowe pole w `FIELDS` z walidacją i testem.
- Testy nie uruchamiają prawdziwych podprocesów systemu (`osascript`, `launchctl`, `pmset`, `caffeinate`): bezpiecznik jest w `tests/__init__.py`, atrapy wstrzykujesz przez parametr `run`/`popen`.
- Skraper: nie zmieniaj podłogi 1 s, obsługi robots.txt ani limitów na IP bez wyraźnej decyzji użytkownika.
- Warstwy: `domain` (czysta logika) ← `services` ← `web`/`cli`; adaptery (db, mailer, scraper) wołane przez usługi; pilnuje tego `lint-imports`. Nie dopisuj SQL do handlerów stron, usług ani skryptów CLI: zapytania żyją w `db/` (repozytorium) i `scraper/store/` (role: companies, runs, queue, pages, read_models, retention).
- Lista w panelu (Wysyłka, Eksport) jest liczona w bazie: `domain/listing.py` (stan z przeglądarki → `RowFilter`) → `db/listing_sql.py` i `db/listing_queries.py` → `services/listing.py` → `POST /api/mailing/*`. Nie filtruj, nie sortuj i nie licz wierszy w JS (`kit.js` to DOM, `listmodel.js` żądania i stan widoku). Zmiana semantyki filtra to zmiana SQL i wektorów `tests/listing_cases.json`; sprawdź ją na sqlite i na MySQL.
- Handler strony: parsuje żądanie, woła usługę, zwraca JSON (≤ ~15 linii). Wyjątki mapuje na HTTP jedna tabela w `web/errors.py`; zależności strony bierze z `AppContext`, nigdy z atrybutów doklejanych do aplikacji.
- `os.environ` i `config.env` czyta wyłącznie `settings.py` (`Environment`, `ConfigFile`, `SmtpSettings`); reszta dostaje wartości jawnie.
- Zanim dodasz helper, sprawdź istniejące moduły (`domain/`, `services/`); duplikacja to najczęstszy błąd kodu z AI.
- Po zmianie reguł w `domain/` lub `services/` uruchom `scripts/mutation_check.py` na zmienionym pliku: przeżywający mutant to zwykle brakująca asercja.
- Funkcje ≤ ~40 linii i ≤ 5 argumentów, typy na publicznym API, bez mutowalnych argumentów domyślnych. Bramki ruff: złożoność ≤ 15, gałęzie ≤ 15, instrukcje ≤ 50, argumenty ≤ 8.
- Nazwy: identyfikatory po angielsku. Wyjątek: skraper trzyma polskie nazwy tabel, kolumn i kodów statusów (`firmy`, `skan_status`, `FIRMA_*`), bo to wartości w bazie i w API JSON; nowy kod skrapera nazywaj po angielsku i tłumacz na granicy magazynu. (Sprawdzone: `Column("domena", key="domain")` działa w wyrażeniach i `insert/update`, ale `dict(row)` zwraca nazwy SQL, więc pełne tłumaczenie wymaga DTO.)
- Po przeniesieniu pliku sprawdź ścieżki liczone z `__file__` (`grep -rn __file__ src`); tak zepsuł się `build_seed_data.py`.
- Szerokie `except` tylko na granicy procesu/wątku, z `# noqa: BLE001 — powód` i logiem.
- Dane ze skanowanych stron są niezaufane: escapuj w UI, czyść znaki sterujące w logach.
- Zmiany schematu bazy wyłącznie addytywne (kolumny NULL) i z kopią zapasową; poprzednia wersja kodu musi działać na uaktualnionej tabeli.
- Każdą zmianę kończ `scripts/check.sh` i pokaż wynik; nie pisz „gotowe" bez uruchomionej weryfikacji.
- README ma zgadzać się z kodem (polecenia, pliki, ograniczenia).
- Commity po angielsku, małe, jedna zmiana logiczna na commit.
