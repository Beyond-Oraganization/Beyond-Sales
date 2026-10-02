"""Statusy skrapera: firm, stron, przebiegów oraz przyczyny zatrzymania."""

from __future__ import annotations

# ---- firmy.skan_status ---------------------------------------------------
FIRMA_NIESKANOWANA = "nieskanowana"
FIRMA_W_KOLEJCE = "w_kolejce"
FIRMA_SKANOWANIE = "skanowanie"
FIRMA_ZAKONCZONA = "zakonczona"
FIRMA_LIMIT = "zakonczona_limit"
FIRMA_ZABLOKOWANA = "zablokowana"
FIRMA_BLAD = "blad"
FIRMA_POMINIETA = "pominieta"

FIRMA_META = {
    FIRMA_NIESKANOWANA: ("Nieskanowana", "#6b7280"),
    FIRMA_W_KOLEJCE: ("W kolejce", "#2f6feb"),
    FIRMA_SKANOWANIE: ("Skanowanie…", "#8e44ad"),
    FIRMA_ZAKONCZONA: ("Zakończona", "#1a7f4b"),
    FIRMA_LIMIT: ("Zakończona (limit)", "#3d8b5e"),
    FIRMA_ZABLOKOWANA: ("Zablokowana", "#c0392b"),
    FIRMA_BLAD: ("Błąd", "#d35400"),
    FIRMA_POMINIETA: ("Pominięta", "#8a5a00"),
}
FIRMA_STATUSY = frozenset(FIRMA_META)

# Statusy, z których start przebiegu zajmuje firmy domyślnie (zablokowane i
# zakończone z limitem tylko przez jawne „Ponów").
FIRMA_DOMYSLNIE_DO_SKANU = (FIRMA_NIESKANOWANA, FIRMA_W_KOLEJCE, FIRMA_BLAD)
FIRMA_PONAWIALNE = (FIRMA_BLAD, FIRMA_ZABLOKOWANA, FIRMA_LIMIT, FIRMA_ZAKONCZONA)

# ---- strony.status -------------------------------------------------------
STRONA_OCZEKUJE = "oczekuje"
STRONA_POBRANO = "pobrano"
STRONA_DUPLIKAT = "duplikat"
STRONA_NIEHTML = "niehtml"
STRONA_ZA_DUZE = "za_duze"
STRONA_BLAD = "blad"
STRONA_ZABLOKOWANO = "zablokowano"
STRONA_POMINIETO_ROBOTS = "pominieto_robots"
STRONA_POMINIETO_LIMIT = "pominieto_limit"
STRONA_PRZEKIEROWANIE = "przekierowanie"

STRONA_STATUSY = frozenset({
    STRONA_OCZEKUJE, STRONA_POBRANO, STRONA_DUPLIKAT, STRONA_NIEHTML, STRONA_ZA_DUZE,
    STRONA_BLAD, STRONA_ZABLOKOWANO, STRONA_POMINIETO_ROBOTS, STRONA_POMINIETO_LIMIT,
    STRONA_PRZEKIEROWANIE,
})

# ---- skan_przebiegi.status ---------------------------------------------------
RUN_URUCHAMIANIE = "uruchamianie"
RUN_DZIALA = "dziala"
RUN_ZAKONCZONY = "zakonczony"
RUN_ZATRZYMANY = "zatrzymany"
RUN_PRZERWANY = "przerwany"
RUN_BLAD = "blad"

RUN_AKTYWNE = (RUN_URUCHAMIANIE, RUN_DZIALA)
RUN_STATUSY = frozenset({RUN_URUCHAMIANIE, RUN_DZIALA, RUN_ZAKONCZONY, RUN_ZATRZYMANY, RUN_PRZERWANY, RUN_BLAD})

# ---- firmy.skan_przyczyna ----------------------------------------------------
PRZYCZYNY = {
    "robots_disallow": "robots.txt zabrania wejścia",
    "robots_odmowa": "robots.txt niedostępny (401/403) — traktujemy jak odmowę",
    "robots_niedostepny": "robots.txt niedostępny (błąd serwera/sieci)",
    "http_403": "serwer odmawia dostępu (401/403/406/451)",
    "http_429": "serwer ogranicza tempo (429/503)",
    "retry_after": "serwer prosi o długą przerwę (Retry-After)",
    "bot_wall": "strona wyświetla blokadę dla botów",
    "http_5xx": "serwer zwraca błędy 5xx",
    "dns": "domena nie istnieje lub nie odpowiada (DNS)",
    "tls": "błąd TLS (np. serwer wymaga TLS 1.3)",
    "timeout": "serwer nie odpowiada (timeout)",
    "siec": "błędy połączenia",
    "przekierowanie": "strona przekierowuje na inną domenę",
    "limit_stron": "osiągnięto limit stron na firmę",
    "limit_bajtow": "osiągnięto limit danych na firmę",
    "limit_czasu": "osiągnięto limit czasu na firmę",
    "stop_uzytkownika": "zatrzymano na żądanie",
    "zly_url": "nieprawidłowy lub niedozwolony adres startowy",
    "platforma": "platforma społecznościowa — nie skanujemy",
    "brak_www": "brak adresu strony",
    "domena_docelowa_zajeta": "domena docelowa jest już skanowana jako inna firma",
    "ssrf": "domena wskazuje na adres prywatny/lokalny — pominięta",
    "brak_strony_glownej": "strona główna nie istnieje lub zwraca błąd",
    "limit_przebiegu": "osiągnięto maksymalny czas przebiegu",
    "baza": "utracono połączenie z bazą danych",
}

# Przyczyna zatrzymania firmy → status firmy.
PRZYCZYNA_STATUS = {
    "dns": FIRMA_BLAD, "tls": FIRMA_BLAD, "timeout": FIRMA_BLAD, "siec": FIRMA_BLAD, "http_5xx": FIRMA_BLAD,
    "robots_niedostepny": FIRMA_BLAD, "brak_strony_glownej": FIRMA_BLAD,
    "http_403": FIRMA_ZABLOKOWANA, "http_429": FIRMA_ZABLOKOWANA, "retry_after": FIRMA_ZABLOKOWANA,
    "bot_wall": FIRMA_ZABLOKOWANA, "robots_odmowa": FIRMA_ZABLOKOWANA, "robots_disallow": FIRMA_ZABLOKOWANA,
    "ssrf": FIRMA_POMINIETA, "przekierowanie": FIRMA_POMINIETA, "zly_url": FIRMA_POMINIETA, "platforma": FIRMA_POMINIETA,
}
