# Wysyłka maili — Eventy + Driverzy (ICP3)

Program generuje i wysyła maile sprzedażowe. Baza maili (kolejka + trwały log wysyłki)
siedzi w **MySQL** (tabela `maile`); obsługuje ją panel w przeglądarce (`server.py`)
oraz terminal (`send_emails.py`) — oba mogą pracować jednocześnie.

Polecenia w katalogu głównym to cienkie skrypty uruchamiające pakiet `perno` (kod w `src/perno/`):

```
server.py            panel w przeglądarce: menu z zakładkami (Wysyłka, Eksport, Skraper, …)   → perno.cli.server
send_emails.py       to samo z terminala (paczki z opóźnieniami)                              → perno.cli.send
migrate_csv_to_sql.py  jednorazowa migracja dawnego Maile.csv do bazy                         → perno.cli.migrate
scrape.py            skraper stron firm (kontekst i kontakty) — z terminala; to samo robi zakładka Skraper → perno.cli.scrape
firmy.py             nowe firmy z CSV do bazy oraz przypisanie im kraju, miasta i adresu      → perno.cli.firms
wypelnij.py          uzupełnianie maili treścią z zakończonych skanów                          → perno.cli.enrich
build_seed_data.py   buduje data/*.csv (dane firm) ze źródeł                                  → perno.cli.seed
dashboard.py         statyczny podgląd index.html tylko do odczytu — powstaje na żądanie (bez treści maili; --with-bodies) → perno.cli.dashboard
index.html           wynik dashboard.py; nie jest odświeżany po każdej zmianie, bo przy tysiącach firm byłoby to kosztowne
```

Układ kodu (`src/perno/`); zależności idą tylko „w dół", pilnuje tego `lint-imports` w `scripts/check.sh`:

- `domain/` — czysta logika bez bazy i sieci: statusy, reguły wierszy (`mail_rules`), filtr (`rowfilter`), wyszukiwarka
  i polska kolejność (`search`), stan listy z panelu (`listing`), kraje, szablony;
- `db/` — tabela `maile`: schemat, kodek JSON, migracje (tylko dodawanie kolumn), repozytorium i fasada `Database`
  oraz zapytania listy w panelu (`listing_sql`: wyszukiwanie i sortowanie, `listing_queries`: strona, liczniki, id widoku);
- `mailer/` — SMTP i MIME;
- `scraper/` — skraper: `store/` (magazyn podzielony na role), `crawler` (fasada) z `scheduler`, `company_crawl`,
  `page_fetch`, `page_pipeline`, a także `fetcher`, `politeness`, `extract`, `contacts`, `urls`, `runner`;
- `services/` — reguły biznesowe wspólne dla panelu i terminala: wysyłka, generowanie i uzupełnianie treści,
  lista firm w panelu (`listing`), zmiany zbiorcze, eksport, import firm, sterowanie skanowaniem, podgląd statyczny;
- `web/` — serwer: `app` (routing i zabezpieczenia), `context` (zależności stron), `errors` (wyjątki → kody HTTP),
  `pages/` (strony panelu i ich pliki JS/CSS);
- `cli/` — punkty wejścia (argumenty i wypisywanie wyniku); `settings.py` — jedyny odczyt `config.env` i zmiennych środowiska.

## 1. Instalacja

Wymagany jest Python 3.12 lub nowszy; zalecany 3.13. Systemowy Python 3.9 jest po końcu wsparcia, a jego LibreSSL nie
obsługuje TLS 1.3 (skraper kończył takie strony błędem TLS). Projekt ma własne środowisko zarządzane przez
[uv](https://docs.astral.sh/uv/) (`brew install uv`):

```bash
cd Wysylka
uv sync                      # tworzy .venv z Pythonem 3.13 i pakietem perno (pyproject.toml, uv.lock)
.venv/bin/python server.py   # panel; to samo dotyczy pozostałych skryptów (albo `uv run server.py`)
scripts/check.sh             # ruff + mypy + kontrakty warstw + testy; --fast pomija wolne testy
```

Wszystkie polecenia `python3 …` w dalszej części README uruchamiaj interpreterem projektu (`.venv/bin/python …` albo
`uv run …`): systemowy `python3` nie zna pakietu `perno` (skrypty wypiszą wtedy podpowiedź). Bez uv:
`python3.13 -m venv .venv && .venv/bin/pip install -e .`.

## 2. Baza danych (raz)

W kliencie MySQL jako administrator (`mysql -u root -p` — wpisz polecenia interaktywnie, nie przez `-e`,
żeby hasło nie trafiło do historii powłoki):

```sql
CREATE DATABASE IF NOT EXISTS perno_maile CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
-- to samo hasło w obu liniach:
CREATE USER IF NOT EXISTS 'perno_app'@'localhost'  IDENTIFIED WITH caching_sha2_password BY 'TU_WPISZ_SWOJE_HASLO';
CREATE USER IF NOT EXISTS 'perno_app'@'127.0.0.1' IDENTIFIED WITH caching_sha2_password BY 'TU_WPISZ_SWOJE_HASLO';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX ON perno_maile.* TO 'perno_app'@'localhost', 'perno_app'@'127.0.0.1';
```

Konta tylko dla adresu lokalnego (nigdy `'%'`). Następnie skopiuj dane logowania do `config.env`
(wzór: `config.example.env`; hasło **bez cudzysłowów**) i zabezpiecz plik:

```bash
chmod 600 config.env
python3 -m perno.db --check      # połączenie, wersja serwera, tryb SQL, stan tabeli (bez haseł)
```

Gdyby połączenie TCP nie działało, ustaw w `config.env` `DB_SOCKET=/tmp/mysql.sock`.

> MySQL z Homebrew domyślnie nasłuchuje na wszystkich interfejsach (`*:3306`). Warto dopisać
> `bind-address=127.0.0.1` w `/opt/homebrew/etc/my.cnf` i zrestartować (`brew services restart mysql`).

## 3. Migracja z Maile.csv (raz)

Zatrzymaj `server.py` (jeśli działa), potem:

```bash
python3 migrate_csv_to_sql.py --check         # sama walidacja CSV, baza nietknięta
python3 migrate_csv_to_sql.py                 # migracja
python3 migrate_csv_to_sql.py --verify-only   # porównanie bazy z kopią CSV z backup/
```

Migracja wstawia wszystko w jednej transakcji, sprawdza (liczba wierszy, liczby per segment/status,
każde pole każdego wiersza, bajtową zgodność odtworzonego CSV, zakres `sent_at`, niezmienniki)
**przed** i **po** zatwierdzeniu, i dopiero wtedy przenosi `Maile.csv` do `backup/`. Odmawia pracy na
niepustej tabeli. **Powrót do starej aplikacji:** `python3 migrate_csv_to_sql.py --export-legacy Maile.csv`
(zapisuje bazę w starym formacie) i przywrócenie kodu z `backup/kod_przed_refaktorem_*.tar.gz`.

## 4. Panel (`python3 server.py`)

Otwiera `http://localhost:8765/` (serwer słucha wyłącznie na `127.0.0.1`). Menu u góry:

- **Wysyłka** — tabela maili z filtrami, zaznaczaniem i zbiorczymi zmianami (opis niżej). E-mail, temat i status
  edytujesz wprost w tabeli; klik w nazwę firmy rozwija edytowalną treść oraz dane o firmie (kraj, miasto, adres, opis);
  🔗 to strona firmy. „Wyślij gotowe” wysyła po kolei, z opóźnieniem i dziennym limitem (limit egzekwuje też serwer).
  „Odśwież dane” dogenerowuje nowe wiersze z `data/*.csv` (nie nadpisuje istniejących) i **odmawia działania na pustej
  bazie** (niezmigrowana historia = ryzyko ponownej wysyłki do wszystkich).
- **Eksport** — pobranie wybranych firm jako **XLSX** albo **CSV** (UTF-8 z BOM; separator średnik dla Excela PL lub
  przecinek). Te same filtry i to samo zaznaczanie co w panelu Wysyłka; do pliku trafia albo wszystko, co pasuje do
  filtrów, albo tylko zaznaczone wiersze (w kolejności z ekranu). **Plik jest przygotowany do czytania**: nagłówki po
  polsku, status jako etykieta („Wysłano”), kraj z nazwą, daty bez sekund, a w XLSX dodatkowo wyróżniony nagłówek, kolorowe
  statusy, klikalne strony firm, zawijanie tekstu w komórkach i wyrównanie do góry, zamrożony nagłówek z nazwą firmy, filtr w nagłówku i ustawienie do wydruku. Domyślnie
  tylko 9 kolumn (Firma, Segment, Kraj, Miasto, E-mail, Strona www, Status, Data wysłania, Temat maila); zestawy
  „Kontakty”, „Rozszerzony” (adres, osoba kontaktowa, opis, błąd…), „Minimalny” i „Wszystko” (z treścią maili oraz
  polami technicznymi: identyfikator, tag, liczba prób…) albo ręczny wybór. Komórki zaczynające się od `= + - @` są
  zabezpieczone przed wykonaniem jako formuły.

- **Skraper** — firmy do przeskanowania, start/stop przebiegu, postęp na żywo, a po kliknięciu w firmę: znalezione
  kontakty (e-maile, telefony, profile firmowe w social media, formularze), pobrane strony i `robots.txt`.
  Szczegóły w sekcji 5.

**Status „Wysyłanie…”** ustawia wyłącznie program, tuż przed wysłaniem (zajęcie wiersza) — dzięki temu
dwie karty przeglądarki albo panel i terminal nie wyślą tego samego maila dwa razy. Wiersze zawieszone
dłużej niż 10 minut (np. po awarii) przy starcie serwera/CLI wracają jako **Błąd** — przed ponowieniem
sprawdź folder „Wysłane”. Ręcznie można zmienić status w dowolnym momencie (lista w kolumnie Status).

### Statusy

| Status | Znaczenie | Wysyłka |
|---|---|---|
| Do wysłania | gotowy mail (jest e-mail i treść) | tak |
| Wysyłanie… | ustawia tylko program, na czas wysyłki | — |
| Wysłano | mail poszedł | nie |
| **Odpowiedział** | firma odpisała | nie |
| **Zainteresowany** | firma jest zainteresowana | nie |
| Błąd | wysyłka się nie udała (można ponowić) | tak |
| Brak e-maila / Już klient / Czeka na treść | pominięte | nie |
| **Nie kontaktować** | firmie nie piszemy | nie |

Statusy „Wysłano”, „Odpowiedział”, „Zainteresowany” i „Nie kontaktować” **zachowują datę wysłania** przy przechodzeniu
między sobą (oznaczenie firmy jako „Odpowiedział” nie kasuje informacji, kiedy do niej napisano); każdy inny status ją
czyści. Ponowne generowanie treści (`--regenerate-pending`, „Odśwież dane”) nie nadpisuje tych czterech statusów.

### Filtry, zaznaczanie i zbiorcze zmiany (Wysyłka i Eksport)

Lista liczy się **w bazie**, a przeglądarka dostaje jedną stronę wierszy i liczniki (`POST /api/mailing/list`), więc panel
działa tak samo płynnie przy 400 i przy 50 000 firm (zmierzone na MySQL 9.6 z 50 000 syntetycznych wierszy,
`scripts/seed_synthetic.py`: otwarcie panelu ok. 100 ms i 53 KB, zmiana strony lub sortowania 100–180 ms, filtr 150–210 ms,
wyszukiwanie ok. 250 ms; wcześniej cała tabela, 27 MB i 2–3 s przy każdym wczytaniu). Funkcje są te same co dawniej;
różnice są dwie: **liczby w tekście sortują się znak po znaku** („Firma 10” przed „Firma 2”, tak jak w MySQL), a nie jak liczby,
oraz wyszukiwarka na MySQL nie znajdzie pojedynczej litery ukrytej wewnątrz rzadkiego znaku (np. samo „e” w „Æ”, bo baza
trzyma `æ`, `œ`, `ß` i `þ` jako osobne litery; frazy „ae”, „oe”, „ss” i „th” działają). Losowy test porównawczy z dawną listą
w przeglądarce (3200 stanów filtrów na wierszach z `ß æ œ ø ł ü` i polskimi znakami) zgadzał się w 100% na sqlite i w 99,9% na MySQL.

Wyszukiwanie bez ogonków i wielkości liter opiera się na kolacji kolumn MySQL (`utf8mb4_0900_ai_ci`, tak jak tworzy je program);
gdyby tabela miała inną, `python3 -m perno.db --check` i start panelu ostrzegają, a polecenie naprawcze podaje komunikat.

- **Szybkie widoki z licznikami**: Wszystkie, Gotowe do wysłania, Błędy, Bez e-maila, Mają e-mail bez treści, Wysłane
  dziś, Wysłane do firm spoza PL, Odpowiedzi, Nie kontaktować, Zaimportowane, Bez kraju, Bez miasta.
- **Filtry**: chipy statusów (klik = dodaj/zdejmij, liczniki uwzględniają pozostałe filtry), segment i kraj (wiele naraz,
  „Spoza Polski”, „Bez kraju”), „Więcej filtrów”: e-mail / treść / miasto / adres / wysłano (jest–brak), pochodzenie
  (z dossier / zaimportowane), zakresy dat wysłania i wygenerowania (skróty: dziś, 7 dni, 30 dni).
- **Wyszukiwarka**: kilka słów = wszystkie muszą pasować (liczy się do 10 słów i do 10 wykluczeń), `-słowo` wyklucza, `"fraza dokładna"`; bez znaczenia wielkość liter
  i polskie znaki (`lodz` znajdzie „Łódź”); pole do wyboru (wszędzie / firma / e-mail / strona / kraj i adres / opis);
  kraj można wpisać po polsku („niemcy”).
- **Sortowanie** po nagłówkach (kolejne kliknięcie: rosnąco → malejąco → domyślnie; polskie litery, puste na końcu),
  **paginacja** (50 / 100 / 200 / 500 wierszy na stronie). Stan widoku jest w adresie strony (można go zapisać w zakładkach) i
  zapamiętywany w przeglądarce; **„Widoki ▾”** zapisują własne zestawy filtrów (wspólne dla Wysyłki i Eksportu).
- **Zaznaczanie**: checkboxy, Shift+klik (zakres), nagłówek (strona), menu „Zaznacz ▾” (strona, wszystkie pasujące,
  gotowe do wysłania, pierwsze N, odwróć, odznacz), „Pokaż tylko zaznaczone”; licznik „Zaznaczono 37 (5 poza widokiem)”.
  Zaznaczenie przeżywa zmianę filtrów, a „Zaznacz wszystkie pasujące” obejmuje cały widok (do 50 000 firm), nie tylko
  bieżącą stronę; ile zaznaczonych jest gotowych do wysyłki, liczy serwer.
- **Pasek akcji** (u dołu panelu Wysyłka): **Zmień status**, **Dane firmy…** (kraj / miasto / adres: „bez zmian”,
  ustaw albo wyczyść), **Kopiuj e-maile**, **Eksportuj** (zaznaczone albo cały widok: serwer wybiera wiersze po filtrach,
  więc plik nie jest ograniczony liczbą id), **Cofnij** i **Wyślij gotowe**
  (zaznaczone ∩ gotowe; przed wysyłką pada informacja, ile pominięto, oraz ostrzeżenie o firmach spoza Polski, bo szablony
  są po polsku).
- **Zbiorcza zmiana** (do 20 000 firm naraz) zawsze pokazuje najpierw podgląd: ile firm się zmieni, ile jest bez zmian,
  które pominięto i dlaczego (np. „Wysyłanie…” albo „Do wysłania” bez e-maila/treści). Zmiany, których skutków nie widać
  na pierwszy rzut oka, wymagają zaznaczenia „Rozumiem”: powrót firmy na listę wysyłki (data wysłania znika — możliwy
  ponowny mail), ręczne „Wysłano” bez daty, zdjęcie „Nie kontaktować”. Zapis jest atomowy (jedna transakcja, wiersze
  blokowane w kolejności id).
- **Cofnij** pamięta ostatnie 20 zbiorczych zmian (najwyżej 60 000 zmienionych wierszy łącznie, starsze odpadają pierwsze)
  **w pamięci serwera** (znikają po jego restarcie). Cofnięcie przywraca
  tylko pola, które nadal mają wartość ustawioną przez tę operację — późniejsze ręczne zmiany zostają i są zgłaszane.

### Kraj i dane o firmie

Każda firma w tabeli `maile` ma cztery opcjonalne pola: **kraj** (`country`, kod ISO, np. `PL`, `GB`, `DE`; w panelu i w
eksporcie widać go z polską nazwą i flagą), **miasto** (`city`, nazwa lokalna, np. `London`, `München`, `Wien`),
**adres** (`address`: ulica, kod pocztowy) i **opis działalności** (`description`). W panelu edytujesz je po kliknięciu
w nazwę firmy. Kraj wpisany ręcznie (po polsku lub angielsku, kodem albo „UK”) zapisuje się jako kod ISO, a nieznany
kraj jest odrzucany z komunikatem; listę krajów rozszerzysz w `src/perno/domain/countries.py`. Starsza baza dostaje te kolumny
sama przy starcie panelu i `send_emails.py` (albo ręcznie: `python3 -m perno.db --upgrade`) — istniejące dane się
nie zmieniają, a poprzednia wersja programu nadal działa na uaktualnionej tabeli.

**Nowe firmy z CSV — `firmy.py`.** Każde polecenie zmieniające bazę najpierw zapisuje jej kopię w `backup/`
(kolejne kopie nigdy się nie nadpisują); `--dry-run` pokazuje plan i niczego nie zapisuje.

```bash
python3 firmy.py import data/nowe_eventy.csv --segment eventy --dry-run   # plan; nic nie zapisuje
python3 firmy.py import data/nowe_eventy.csv --segment eventy
python3 firmy.py import data/nowe_driverzy.csv --segment driverzy
python3 firmy.py kraje data/kraje_firm.csv      # przypisanie kraju/miasta/adresu (tylko puste pola; --overwrite nadpisuje)
python3 firmy.py raport                         # ile firm w którym kraju i które nie mają jeszcze kraju
```

Plik do importu ma kolumny `company_display_name`, `website`, `what_they_sell` (+ opcjonalnie `country`, `city`, `address`).
Firma wchodzi do bazy ze statusem „Brak e-maila” (nie da się jej wysłać przez pomyłkę), z identyfikatorem
`eventy-n001`, `driverzy-n001`… (osobna numeracja, żeby nie zderzyć się z numerami z dossier) i bez numeru wiersza,
więc w panelu stoi na końcu segmentu. Firmy rozpoznajemy po domenie (bez `www.`) — ponowny import niczego nie dubluje.
Gdy dopiszesz e-mail i treść, wiersz przechodzi do zwykłego obiegu („Do wysłania”). Lista firm dla skrapera:
`python3 scrape.py seed`.

`data/kraje_firm.csv` (kolumny `domain`, `country`, `city`, `address`, `source`) to decyzje o kraju dla 400 firm
wraz ze źródłem każdej: kraje 100 pierwszych firm pochodzą z arkuszy GTM, a nowych z opisu firmy i domeny; 19 niejednoznacznych
sprawdzono na stronach firm (np. polskojęzyczna strona firmy z siedzibą w Alicante w Hiszpanii; `taxiberlin.pl` to firma polska).
Poprawki robisz w panelu albo w tym pliku; zmianę z pliku wprowadzisz poleceniem `firmy.py kraje data/kraje_firm.csv --overwrite` (bez `--overwrite` to, co już jest wpisane — np. poprawione ręcznie w panelu — zostaje).

**Wycofanie.** Stan sprzed dodania kraju i importu to pierwszy plik `backup/Maile_20261001_*_przed-importem.csv`. Nowe firmy to wiersze z `-n` w id: `DELETE FROM maile WHERE id LIKE '%-n%';`; kolumny usuwa `ALTER TABLE maile DROP COLUMN country, DROP COLUMN city, DROP COLUMN address, DROP COLUMN description;` (poprzednia wersja programu działa i bez nich, i z nimi).

**Uwaga o treści maili:** język maila zależy od ścieżki, którą powstał. Maile generowane z dossier (`send_emails.py`,
„Odśwież dane” w panelu; patrz „7. Treść maili”) są zawsze po polsku, także dla firm spoza Polski — filtr
„Wielka Brytania”/„Niemcy” w panelu pokazuje, do kogo polski mail już poszedł. Maile uzupełniane ze skanu
(`wypelnij.py`) dostają język kraju firmy (PL → polski, GB/IE → angielski, DE/AT → niemiecki, ES → hiszpański).

### Dodawanie nowej strony do menu

1. Utwórz `src/perno/web/pages/raporty.py` z obiektem `PAGE = Page(key="raporty", label="Raporty", path="/raporty", render=..., routes={("GET", "/api/raporty"): handler})`
   (wzór: `src/perno/web/pages/export.py`; HTML/CSS/JS trzymaj w `src/perno/web/pages/assets/`).
2. W `src/perno/web/pages/__init__.py` zaimportuj moduł i dopisz `raporty.PAGE` do `PAGES`.

Funkcja `render(ctx)` i handlery `handler(req, ctx)` dostają `AppContext` (`ctx.db`, `ctx.config`, `ctx.undo_log`, …);
logikę biznesową woła się z `services/`, a wyjątki zamienia na kody HTTP jedna tabela w `web/errors.py`.
Strona pojawi się w menu; `layout.render_page` dokłada zakładki, wspólny wygląd i `APP` (statusy) dla JS.
Wszystkie żądania POST z JS wysyłaj przez `api()` (`assets/common.js`) — serwer wymaga `Content-Type: application/json`.

## 5. Skraper (`scrape.py` i zakładka „Skraper”)

Uprzejmie pobiera strony firm (do głębokości 10 od strony głównej), żeby zebrać **kontekst** do analizy firmy i
**kontakty**. To nie jest narzędzie do obchodzenia zabezpieczeń: skraper robi mały, przewidywalny ruch i wycofuje
się, gdy serwer sobie tego nie życzy.

**Zasady, których nie da się wyłączyć** (ani z config.env, ani z panelu, ani z terminala):

| Zasada | Jak działa |
|---|---|
| `robots.txt` | pobierany zawsze jako pierwszy; własny parser RFC 9309 (najdłuższe dopasowanie, `*`, `$`, `Allow`); `Crawl-delay` (także ułamkowy) respektowany w całości; 401/403 = zakaz |
| Jedna domena = jedno żądanie naraz | odstęp co najmniej **1 s** (domyślnie 2,5 s + losowy dodatek), a dla wolnych serwerów 3× czas odpowiedzi |
| Równoległość tylko między domenami | domyślnie 30 domen naraz; limit na adres IP (hosting współdzielony), pas bezpieczeństwa 15 żądań/s łącznie |
| Odmowa = wycofanie | 429/503 z `Retry-After`: czekamy i ponawiamy (3 próby), dłużej niż 10 min: stop i zapis „wróć po…”; 403 na starcie: jedna próba po minucie; blokada dla botów (np. wyzwanie Cloudflare): stop. **Żadnych obejść** — bez proxy, bez udawania przeglądarki, bez omijania CAPTCHA |
| Jawna tożsamość | `User-Agent: PernoContextBot/1.0 (+kontakt)` i nagłówek `From`; bez cookies; bez JavaScriptu |
| Zakres | tylko ta sama domena (bez subdomen), nigdy platformy społecznościowe (linki do nich zapisujemy tylko jako kontakt firmowy), nigdy adresy prywatne/lokalne (ochrona SSRF) |
| Pułapki | limity na katalog/paginację/wzorzec adresu, odrzucanie kalendarzy, koszyków, `wp-json`, plików binarnych; duplikaty treści zapisywane raz |

```bash
python3 scrape.py seed                          # firmy z tabeli maile (jedna firma = jedna domena)
python3 scrape.py dodaj firma.pl --nazwa "Firma"  # pojedynczy adres
python3 scrape.py run --dry-run                 # plan i szacunek czasu — bez sieci, kolejka bez zmian
python3 scrape.py run --firma 3 --pages 30 --concurrency 1     # próba na jednej firmie
python3 scrape.py run                           # wszystkie nieskanowane / w kolejce / z błędem (Ctrl+C = łagodny stop)
python3 scrape.py status
python3 scrape.py stop                          # prośba o łagodne zatrzymanie z drugiego okna
python3 scrape.py purge --older-than 90         # usuń dane skanu firm zakończonych dawniej niż 90 dni
```

Ustawienia domyślne (`SKRAPER_*` w config.env, komentarze w `config.example.env`) są przycinane do bezpiecznych
zakresów; flagi `run` (`--profile --concurrency --delay --depth --pages --mb --minutes`) mają pierwszeństwo. **Przed
pierwszym uruchomieniem ustaw `SKRAPER_CONTACT`** (e-mail albo URL — właściciele stron zobaczą go w nagłówkach).
Bez niego użyty zostanie `SMTP_USER` z ostrzeżeniem.

**Profil budżetu na firmę** (`SKRAPER_PROFILE`, `--profile`, lista „Profil” w ustawieniach przebiegu) wybiera, ile skraper
bierze z jednej firmy; uprzejmość wobec hostów (odstęp, robots.txt, limity na IP) od profilu nie zależy:

| Profil | Stron | Głębokość | MB | Minut | Zapamiętane adresy | Kiedy |
|---|---|---|---|---|---|---|
| `zbalansowany` (domyślny) | 500 | 10 | 50 | 30 | 20 000 | pełniejszy obraz firmy; dotychczasowe zachowanie |
| `kontekst` | 25 | 4 | 15 | 10 | 500 | adres kontaktowy i kontekst do maila; kilka razy mniej ruchu i danych |

Kolejka jest ułożona priorytetami (strona główna, kontakt, o nas, regulamin, oferta), więc 25 stron to te najcenniejsze.
W danych z pierwszych skanów (394 firmy) pierwszy adres e-mail leżał w ≤10 pobranych stronach u 99,7% firm, w których w ogóle
go znaleziono, a w 25 stronach mieściło się ok. 72% wszystkich znalezionych adresów — profil `kontekst` zamienia resztę na
kilkukrotnie mniejszy ruch i bazę (szacunek proporcjonalny do liczby stron: przy 50 tys. firm rzędu 20 GB zamiast ok. 66 GB).
Firmę można później przeskanować głębiej: wybierz ją w panelu
i uruchom z profilem `zbalansowany` (limity są kumulatywne, a skan kontynuuje od zapisanej kolejki; w profilu `kontekst` jest ona
obcięta do 500 najważniejszych adresów). Liczby ustawione
osobno (`SKRAPER_MAX_PAGES` itd., pola w panelu, `--pages`) nadpisują profil; profil wskazany w panelu albo flagą
wygrywa z liczbami z config.env.

**Przebieg.** Skanowanie działa w osobnym, odłączonym procesie (`scrape.py run --run-id N`, log w `logs/skraper_*.log`),
więc przeżywa zamknięcie karty i restart serwera. Naraz działa co najwyżej jeden przebieg. Postęp jest zapisywany
po każdej stronie: **Stop** albo awaria (nawet `kill -9`) niczego nie traci — kolejne uruchomienie odzyskuje przerwany
przebieg i kontynuuje bez ponownego pobierania. Limity na firmę (strony, MB) są kumulatywne także po wznowieniach.
Firmy zatrzymane przez serwer (`zablokowana`, `blad`, `zakonczona_limit`) nie są ponawiane automatycznie — służy do
tego przycisk „Ponów”.

**Co zapisuje.** Tabele `firmy`, `firmy_maile`, `strony`, `strony_tresc`, `kontakty`, `skan_przebiegi` (osobny zestaw,
tabela `maile` jest nietknięta). Dla każdej strony: adres, status, tytuł, opis i wyciągnięty tekst; **pełny HTML tylko
dla stron kluczowych** (start, kontakt, o nas, regulamin/polityka, oferta; `SKRAPER_STORE_HTML=all|klucz|nie`).
Kontakty: e-maile (z `mailto:`, tekstu, JSON-LD, ochrony Cloudflare `data-cfemail` i zapisu `jan [at] firma.pl`; bez JS
i OCR), telefony (polskie formaty z walidacją; odrzucane NIP/REGON/KRS/IBAN), profile firmowe w social media oraz
formularze kontaktowe — każdy z adresem strony źródłowej i datą (ślad „skąd mamy dane”). Skraper **nie** dopisuje
niczego do tabeli `maile`.

**Dane osobowe.** Zebrane adresy e-mail często są danymi osobowymi. Kontakt służbowy do firmy ma inne podstawy niż
wiadomość do osoby prywatnej; wysyłka na zebrane adresy podlega RODO i prawu komunikacji elektronicznej — to
jest **poza zakresem tego programu i nie jest poradą prawną**. W v1 nie wyciągamy imion i nazwisk z podstron
zespołu ani adresów pocztowych. „Usuń dane” w panelu i `purge` kasują dane skanu.

**Ograniczenia.** Strony renderowane wyłącznie JavaScriptem dadzą mało tekstu (brak przeglądarki headless).
To środowisko ma Pythona z LibreSSL bez TLS 1.3 — strony wymagające wyłącznie TLS 1.3 zakończą się statusem „błąd TLS”
(weryfikacji certyfikatów nigdy nie wyłączamy). Przekierowanie strony głównej na obcą domenę nie jest śledzone:
firma dostaje status „Pominięta” z domeną docelową, a przycisk „Użyj domeny docelowej” przyjmuje ją i kolejkuje.

**Kopia zapasowa i wycofanie.** Zrzut bazy bez ciężkiej treści stron:
`mysqldump -u perno_app -p --single-transaction --no-tablespaces --skip-triggers --ignore-table=perno_maile.strony_tresc perno_maile`.
Usunięcie skrapera z bazy (tabela `maile` zostaje; wykonaj jako administrator — konto aplikacji nie ma uprawnienia `DROP`):
`DROP TABLE skan_przebiegi, kontakty, strony_tresc, strony, firmy_maile, firmy;`.
Tabele powstają same przy pierwszym użyciu (wystarczą uprawnienia, które konto aplikacji już ma: `CREATE`, `INDEX`).

**Pierwsza próba na prawdziwych stronach** — tylko na własnych domenach, jedna firma, mały limit:
`python3 scrape.py dodaj twoja-domena.pl` → `python3 scrape.py run --firma twoja-domena.pl --pages 30 --concurrency 1`
i przejrzyj log oraz szczegóły firmy w panelu. Pilotaż na firmach klientów dopiero po tej próbie.

**Testowanie bez internetu.** Zestaw testów używa lokalnego „internetu” (jeden serwer udający wiele witryn) i blokuje
każde połączenie poza `127.0.0.1`. Do uruchomień demo i testów procesów służą zmienne `PERNO_SKRAPER_ALLOW_PRIVATE=1`
(zezwala na adresy lokalne) i `PERNO_SKRAPER_HOSTS=a.test=127.0.0.1` (mapowanie nazw); drugiej nie da się użyć bez pierwszej.

## 6. Terminal (`send_emails.py`)

```bash
python3 send_emails.py --dry-run --segment both --limit 200   # tylko generuje, nic nie wysyła
python3 send_emails.py --test-send twoj@adres.pl --id eventy-003   # jeden mail na adres testowy, status bez zmian
python3 send_emails.py --limit 3                               # realna, mała paczka
python3 send_emails.py                                         # kolejne paczki: do 20 maili, limit dzienny 50
python3 send_emails.py --dry-run --regenerate-pending          # przelicz niewysłane po zmianie szablonu
python3 send_emails.py --country PL --limit 3                  # tylko firmy z Polski (kody ISO lub nazwy: PL,GB / Polska,Niemcy; none = bez kraju)
```

Każde uruchomienie: dogenerowuje brakujące wiersze, pyta o potwierdzenie (`--yes` pomija), wysyła z losowym
opóźnieniem 45–120 s i zapisuje status w bazie po każdym mailu. Wysłany mail (status `wyslano`) nigdy nie
jest wysyłany ponownie. Przed pytaniem o potwierdzenie program pokazuje rozbicie paczki na kraje i ostrzega, gdy część
maili jedzie do firm spoza Polski (szablony są po polsku). Świeża instalacja bez historii: `--allow-empty-db`.

## 7. Treść maili

- Szablony: `templates/eventy.html`, `templates/driverzy.html` (tokeny `{{obs}}`, `{{pitch}}`, `{{f_chcial}}` …).
  Po zmianie uruchom `--dry-run --regenerate-pending` (wysłane maile nigdy nie są nadpisywane). Wersje językowe
  (`eventy.en.html`, `eventy.de.html`, `eventy.es.html`, to samo dla `driverzy`) są używane tylko przez `wypelnij.py`.
- **Uzupełnianie ze skanu — `wypelnij.py`.** Dla firm z zakończonym skanem (zakładka Skraper albo `scrape.py`) przygotowuje
  treść maila z kontaktów znalezionych na stronach firmy:
  `python3 wypelnij.py lista` (które firmy się kwalifikują), `kontekst --out DIR` (po jednym pliku .md na firmę: opis,
  kontakty, tekst kluczowych stron — materiał do napisania `obs`/`pitch`/`channel_summary`),
  `zapisz tresci.json --dry-run` (walidacja bez zapisu), potem `zapisz tresci.json` (przed zapisem powstaje kopia w
  `backup/`; firma dostaje status „Do wysłania”, wysyłka nadal wymaga Twojej decyzji). Adres e-mail musi pochodzić ze
  skanu, a w mailu obcojęzycznym nie wolno zostawić polskich liter. `--ponownie` przelicza też wiersze „Do wysłania”,
  `--takze-przerwane` uwzględnia skan przerwany przez serwer (≥ 10 stron).
- Ręcznie pisana treść firm Eventy: tablica `const companies = [...]` w `../Maile_GTM_Eventy/index.html`,
  potem `python3 build_seed_data.py`.
- Obrazek w treści (UX w Lesie) jest osadzany tylko wtedy, gdy wyrenderowany HTML zawiera `cid:ux_w_lesie_screenshot`.
- Segment Driverzy ICP4 (platformy property-management) jest pominięty celowo.

## 8. SQL ręcznie, kopie zapasowe

Kolumna `row_number` to słowo zastrzeżone w MySQL 8/9 — w ręcznych zapytaniach pisz ją w backtickach:

```sql
SELECT id, company, email, status, sent_at FROM maile WHERE status = 'blad' ORDER BY `row_number`;
```

Kopia zapasowa (w razie błędu uprawnień wykonaj jako root):

```bash
mysqldump -u perno_app -p --single-transaction --no-tablespaces --skip-triggers perno_maile > backup/perno_maile_$(date +%Y%m%d_%H%M).sql
```

## 9. Testy

```bash
scripts/check.sh                                       # ruff + mypy + kontrakty warstw + testy z progiem pokrycia (ok. 1 min)
scripts/check.sh --fast                                # bez wolnych testów: szybka pętla przy refaktoryzacji
.venv/bin/python -m pytest tests/test_x.py -k nazwa    # pojedyncze testy
.venv/bin/python -m unittest discover -s tests -t . -v # to samo bez pytest
```

Testy używają sqlite i atrapy SMTP — nie dotykają prawdziwej bazy ani poczty (prawdziwe `smtplib` jest w testach zablokowane
w `tests/__init__.py`). Czy testy naprawdę pilnują logiki, sprawdza kontrola mutacyjna: zmienia w kopii projektu po jednym
operatorze i liczy, ile zmian wykryły testy (`.venv/bin/python scripts/mutation_check.py src/perno/domain/mail_rules.py
tests/test_mail_rules.py tests/test_db.py`). Stan z fazy 3: `mail_rules` 100%, `bulk` 88%, `sending` 89%; przeżywają tylko
zmiany bez wpływu na zachowanie (stałe limitów, flagi `frozen`) albo widoczne dopiero na MySQL. Filtry, wyszukiwarkę,
sortowanie i liczniki liczy serwer; `tests/listing_cases.json` (28 wierszy, 74 stany filtrów, liczniki i szybkie widoki,
wyniki wygenerowane z dawnej implementacji w przeglądarce) pilnuje, żeby SQL dawał dokładnie to samo na sqlite i na MySQL
(`tests/test_listing.py`). Stan filtrów, model listy (żądania, sklejanie, spóźnione odpowiedzi) i zaznaczanie
(`filters.js`, `listmodel.js`, `selection.js` w `src/perno/web/pages/assets/`) sprawdzają testy w JavaScript
(`tests/js/*.test.js`), które ten sam zestaw uruchamia przez Node.js 18+ (bez Node są pomijane; osobno:
`node --test tests/js/*.test.js`); `tests/js/skaper_xss.test.js` uruchamia skrypt zakładki Skraper z wrogimi danymi
(`<script>`, `onerror=`, `javascript:`) i pilnuje, żeby żadne nie stało się znacznikiem ani atrybutem; `tests/js/kit_xss.test.js`
robi to samo dla list (nazwy, segmenty i kraje w licznikach, wierszach i podsumowaniach z serwera). Dane do testów skali
(50 000 firm) tworzy `scripts/seed_synthetic.py` (tylko w bazie `*_test`). Opcjonalny test
na prawdziwym MySQL (tryb ścisły, utf8mb4, `FOR UPDATE`, strumieniowanie, zbiorcza zmiana z cofaniem i jednoczesne
operacje bez zakleszczeń, filtry z `TRIM`/`CASE`, oraz tabele i zapytania skrapera: kolumny ASCII, upserty, BLOB,
wyłączność dzierżawy między wątkami) wymaga bazy testowej. Najprościej uruchomić osobny, tymczasowy serwer MySQL, który
nie dotyka Twojej bazy ani haseł (słucha tylko na 127.0.0.1:33307, dane są w katalogu tymczasowym i syntetyczne):

```bash
scripts/scratch_mysql.sh start                     # pierwszy raz inicjalizuje katalog danych
PERNO_TEST_DB_URL="$(scripts/scratch_mysql.sh url)" .venv/bin/python -m pytest tests/test_mysql_smoke.py
scripts/scratch_mysql.sh stop                      # albo `reset`, żeby skasować dane
```

Albo na własnym serwerze, ze współdzielonymi danymi logowania z `config.env`:

```sql
CREATE DATABASE IF NOT EXISTS perno_maile_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
GRANT ALL PRIVILEGES ON perno_maile_test.* TO 'perno_app'@'localhost', 'perno_app'@'127.0.0.1';
```
```bash
PERNO_TEST_DB_NAME=perno_maile_test .venv/bin/python -m pytest tests/test_mysql_smoke.py
```

W obu wariantach nazwa bazy musi kończyć się na `_test` (test kasuje w niej tabele).

Zmienna `PERNO_DB_URL` (np. `sqlite:////tmp/demo.db`) podmienia bazę na demo/testową — wtedy prawdziwa
wysyłka SMTP jest zablokowana, dopóki nie ustawisz też `PERNO_ALLOW_REAL_SMTP=1`.
