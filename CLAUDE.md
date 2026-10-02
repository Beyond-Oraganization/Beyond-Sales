# Wysylka — zasady pracy w tym repozytorium

Lokalna aplikacja B2B: kolejka maili (MySQL, tabela `maile`), panel w przeglądarce (`server.py`), skraper stron firm.
Kod po angielsku; UI, komunikaty, README i szablony po polsku. Nazwy tabel (`maile`, `firmy`, `strony`…) i polecenia CLI się nie zmieniają.
Plan przebudowy (fazy 0-5): `~/.claude/plans/przeanalizuj-dany-projekt-przeczytaj-vivid-cook.md`. Kod jest w `src/perno/` (domain, db, mailer, services, scraper, web, cli); skrypty w katalogu głównym to cienkie wrappery.

## Komendy
- `uv sync` — środowisko (Python 3.13 w `.venv`)
- `scripts/check.sh` — ruff + mypy + testy; uruchom przed każdym commitem (`--fast` pomija testy `slow`)
- `.venv/bin/python -m pytest tests/test_x.py -k nazwa` — pojedyncze testy
- Panel: `.venv/bin/python server.py` (albo `perno-server`); baza demo: `PERNO_DB_URL=sqlite:////tmp/demo.db` (blokuje prawdziwy SMTP)

## Reguły
- Nigdy nie wysyłaj prawdziwych maili ani nie łącz się z prawdziwą bazą w testach. Nie czytaj i nie wypisuj `config.env`.
- Skraper: nie zmieniaj podłogi 1 s, obsługi robots.txt ani limitów na IP bez wyraźnej decyzji użytkownika.
- Warstwy: `domain` (czysta logika) ← `services` ← `web`/`cli`; adaptery (db, mailer, scraper) wołane przez usługi; pilnuje tego `lint-imports`. Nie dopisuj SQL do handlerów stron ani skryptów CLI.
- Zanim dodasz helper, sprawdź istniejące moduły (`domain/`, `services/`); duplikacja to najczęstszy błąd kodu z AI.
- Funkcje ≤ ~40 linii i ≤ 5 argumentów, typy na publicznym API, bez mutowalnych argumentów domyślnych.
- Szerokie `except` tylko na granicy procesu/wątku, z `# noqa: BLE001 — powód` i logiem.
- Dane ze skanowanych stron są niezaufane: escapuj w UI, czyść znaki sterujące w logach.
- Zmiany schematu bazy wyłącznie addytywne (kolumny NULL) i z kopią zapasową; poprzednia wersja kodu musi działać na uaktualnionej tabeli.
- Każdą zmianę kończ `scripts/check.sh` i pokaż wynik; nie pisz „gotowe" bez uruchomionej weryfikacji.
- README ma zgadzać się z kodem (polecenia, pliki, ograniczenia).
- Commity po angielsku, małe, jedna zmiana logiczna na commit.
