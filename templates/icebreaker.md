# Mail icebreaker - zasady pisania treści

Jeden szablon pierwszego maila do zimnego kontaktu dla wszystkich segmentów (eventy, driverzy, spółdzielnie…):
`icebreaker.html` (polski) oraz `icebreaker.en.html`, `icebreaker.de.html`, `icebreaker.es.html`. Język wynika z kraju firmy
(`src/perno/domain/localize.py`). Ten plik opisuje, jak napisać pola, które szablon podstawia; sam szablon ma tylko stałe zdania.

## Jak pola ze wzorca trafiają do `tresci.json` (`wypelnij.py zapisz`)

| we wzorcu | pole | uwagi |
|---|---|---|
| Temat | `subject` | jeden konkretny detal, 3-4 słowa |
| FORMA ADRESU | `mode` | `Pan`, `Pani` albo `Panstwo`; szablon sam odmienia resztę |
| „Szukałem [KATEGORIA]. Zauważyłem, że [FAKT]. Czy [PYTANIE]?” | `obs` | jedno pole, całe trzy zdania |
| „przy [SUGESTIA] [CEL]” | `pitch` | zaczyna się od „przy…”, kończy bezokolicznikiem; szablon dopisuje „Jeśli tak, może chciał(a)by…” |
| KANAŁ_DO_ODCIĄŻENIA | `channel_summary` | bezokolicznik w dopełniaczu, bez płatności (szablon dopisuje „i osobno rozliczać płatności”) |
| OPCJONALNIE - SEZON | `season` | jedno zdanie; pole można pominąć, wtedy akapitu nie ma w mailu |
| imię i nazwisko nadawcy | - | `SENDER_NAME` z `config.env` |

## Wzorzec (treść pól)

Temat: [KONKRETNY_DETAL - z kolumny „Jak wygląda obecnie sprzedaż": cena, nazwa wydarzenia/kursu albo miasto, ewentualnie z kolumny „Czy korzysta z konkurencji" nazwa użytego systemu (Datasport, KupBilecik, Feblik, ActiveNow, Fitssey, Konfeo, easy.tools, Bilety24). Jeden konkretny element, 3-4 słowa]

[FORMA ADRESU - wybierz raz na cały mail i stosuj konsekwentnie wszędzie, gdzie niżej stoi Pan/Pani/Państwo. „Pan"/„Pani" (liczba pojedyncza), jeśli w danych jest wprost nazwana jedna konkretna osoba. Typowo to JDG, gdzie imię i nazwisko właściciela jest w kolumnie z formą prawną (np. „Andrzej Kasiński Nurkomania"); rodzaj ustal po imieniu. „Państwo" (liczba mnoga), jeśli piszemy do spółki, fundacji, stowarzyszenia, spółki cywilnej prowadzonej przez dwie osoby albo gdy nie ma nazwanej osoby. Powitanie jest zawsze „Dzień dobry," niezależnie od formy. Forma adresu dotyczy tylko treści]

Dzień dobry,

Szukałem [KATEGORIA - ogólna nazwa tego, czego szukałby ktoś, kto trafia na tę stronę, w dopełniaczu liczby mnogiej: „kursów survivalowych", „spływów kajakowych", „warsztatów ceramicznych". Wynika z oferty firmy, nie z jej nazwy. „Szukałem" i „Zauważyłem" zostają zawsze w rodzaju męskim, bo to głos nadawcy, nie odbiorcy; nie odmieniaj ich razem z Pan/Pani/Państwo]. Zauważyłem, że [FAKT - jedno zdanie, pozytywne lub neutralne, wyłącznie z faktów widocznych w tabeli: złóż je z kolumn „Jak wygląda obecnie sprzedaż" (kanał, cena, sposób zapisu) i „Czy korzysta z konkurencji" (własne narzędzie vs. nazwana platforma zewnętrzna). Np. cena jest jawna, mają już system rezerwacji, sprzedają przez konkretną platformę. Bez własnych domysłów o tym, jak to u nich działa w praktyce. STOP: jeśli w „Czy korzysta z konkurencji" widnieje „PayEvento", to nie konkurent, tylko nasz własny produkt, a firma jest już klientem (Mikrowyprawy, UX w Lesie). Takiej firmy nie wysyłaj jako zimny kontakt]. Czy [PYTANIE - dotychczasowy „problem" przerobiony na pytanie, nie stwierdzenie. Sprawdź „Czy sprzedaż odbywa się lub może odbywać się online": przy „NIE" zapytaj, czy zapis jest tylko telefoniczny/mailowy; przy „TAK" zapytaj o konkretne tarcie w używanym narzędziu (przekierowanie na cudzą domenę, dwa równoległe kanały, coś dopasowywane ręcznie).

Ton dobierz do formy prawnej i kolumny „Przychody firmy": JDG albo spółka cywilna (wpis „JDG" lub „niepotwierdzone - brak obowiązku sprawozdawczego") → czas jednej osoby, która ręcznie obsługuje zgłoszenia; sp. z o.o. z ujawnioną kwotą → skala (liczba zgłoszeń, lokalizacji), nie sama kwota przychodu, a w Uwagach sprawdź, czy kwota dotyczy marki, bo kilka razy dotyczy całej spółki; świeżo zarejestrowany podmiot (Przychody i Rok puste, w Uwagach rejestracja 2024/2025) → dobry moment na ustawienie procesu od razu poprawnie.

Zasady: (1) mechanizm ich procesu opisuj jako neutralną obserwację, nie zarzut, i dlatego pytaniem („Czy zapis…?", nie „muszą Państwo ręcznie…"). (2) Wszystko, co dotyczy ich klientów lub sprzedaży (że ktoś zrezygnuje, że coś się zgubi), to nasze przypuszczenie, więc tylko w pytaniu albo z hedgem, nigdy jako pewnik. (3) Przy wypełnianiu wielu maili pod rząd (np. całej listy firm) rotuj konstrukcje zamiast powtarzać to samo słowo: „prawdopodobnie", „istnieje ryzyko, że…", „nie można wykluczyć, że…", „zdarza się, że…", „bywa, że…", „łatwo o…", „taki proces sprzyja…". Jedna na mail]?

Jeśli tak, może [chciał(a)by Pan/Pani/chcieliby Państwo] przy [SUGESTIA - konkretny, wąski element ich oferty: jeden kurs, jeden wariant, jedna lokalizacja, taki, który faktycznie występuje w tabeli. W miejscowniku, bo stoi po „przy": „przy kursie I stopnia", „przy jednej z wypraw na Bałkany"] [CEL - bezokolicznik: co konkretnie wprowadzić lub zmienić, zgodnie z luką z PYTANIA: „wprowadzić natychmiastową płatność online" / „przenieść sprzedaż pod własną domenę" / „połączyć dwa kanały w jeden" / „zautomatyzować rozliczanie wpłat". Sprawdź kolumnę „Czy sprzedaż odbywa się lub może odbywać się online": „wprowadzić natychmiastową płatność online" pasuje tylko do firm bez żadnej sprzedaży online. Jeśli firma już ma jakąś płatność online (własny sklep, WooCommerce, zewnętrzna platforma), CEL musi dotyczyć jej realnego problemu, inaczej zdanie jest nieprawdziwe], a później doprecyzować szczegóły zamówienia z klientem? To podnosi konwersję sprzedaży.

Razem z firmą z3x Tech-Care Group (z3x.io) zrobiliśmy proste rozwiązanie Payevento, które:

* uruchamia płatność na Pana/Pani/Państwa stronie www,
* kosztuje 20 zł miesięcznie + 3% od sprzedanych przez nas biletów (2x mniej niż rozwiązania typu eventim),
* zbiera dane uczestników zgodnie z polityką prywatności Pana/Pani/Państwa strony - dane zostają u Pana/Pani/Państwa do późniejszego wykorzystania.

Nie musi już Pan/Nie musi już Pani/Nie muszą już Państwo [KANAŁ_DO_ODCIĄŻENIA - bezokolicznik w dopełniaczu po zaprzeczeniu („zbierać zgłoszeń z telefonu i maila", „ręcznie dopasowywać wpłat do zgłoszeń"). Krótkie odwołanie do tego, co dziś robią ręcznie, wyłącznie z faktów już użytych w FAKT i PYTANIU, bez nowych twierdzeń. Ma opisywać ulgę, nie winę] i osobno rozliczać płatności.

Z rozwiązania korzystają już niezależne firmy takie jak „UX w Lesie" czy Mikrowyprawy - Łukasz Długowski, oraz cykliczne wydarzenie Fintech Trends Poland.

Linki: uxwlesie.pl, payevento.com/event/3/tickets/, mikrowyprawy.com.pl, fintechtrends.pl (w szablonie jest też zrzut ekranu UX w Lesie)

Podłączenie płatności realizujemy automatycznie w kilka minut.

[OPCJONALNIE - SEZON: jedno zdanie o sezonowości, jeśli ją widać w „Jak wygląda obecnie sprzedaż": kalendarz szkoleń, sezon spływów/wypraw, kolejna edycja konferencji czy festiwalu, kolejny turnus. Kolumna „4. Czy wydarzenie ma określoną datę / limit miejsc lub wymaga rejestracji" jest prawie zawsze „TAK", więc zwykle jest termin do przywołania. Przy bardzo młodym podmiocie (Uwagi: rejestracja 2024/2025) odwołaj się zamiast tego do tego, że dopiero układają proces sprzedaży. Jeśli nic konkretnego nie znajdziesz, pomiń całe zdanie.]

[Chciałby Pan / Chciałaby Pani / Chcieliby Państwo] porozmawiać? Proszę o wiadomość zwrotną. Wystarczy nam 20-30 minut.

Pozdrawiam,
[IMIĘ I NAZWISKO nadawcy]
