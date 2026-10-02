'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Sprawdzają czystą logikę filters.js — bez przeglądarki.
const test = require('node:test');
const assert = require('node:assert/strict');
const F = require('../../src/perno/web/pages/assets/filters.js');
const shared = require('../filter_cases.json');

const STATUS_META = {
  do_wyslania: ['Do wysłania', '#2f6feb'], wysylanie: ['Wysyłanie…', '#8e44ad'], wyslano: ['Wysłano', '#1a7f4b'],
  odpowiedzial: ['Odpowiedział', '#0e7490'], zainteresowany: ['Zainteresowany', '#be185d'], blad: ['Błąd', '#c0392b'],
  pominieto_brak_email: ['Brak e-maila', '#6b7280'], pominieto_klient: ['Już klient', '#8a5a00'],
  oczekuje_na_tresc: ['Czeka na treść', '#b8860b'], nie_kontaktowac: ['Nie kontaktować', '#7f1d1d']
};
const COUNTRIES = [['PL', 'Polska', '🇵🇱'], ['DE', 'Niemcy', '🇩🇪'], ['GB', 'Wielka Brytania', '🇬🇧'], ['IE', 'Irlandia', '🇮🇪']];
const ctx = F.createContext({statusMeta: STATUS_META, sendable: ['do_wyslania', 'blad'], countries: COUNTRIES, today: '2026-09-30'});
const ROWS = shared.rows;
const ids = (rows) => rows.map((r) => r.id);
const find = (state) => ids(F.filterRows(ROWS, F.normalizeState(state), ctx));

test('fold: małe litery, bez znaków diakrytycznych, pojedyncze spacje', () => {
  assert.equal(F.fold('Zażółć gęślą jaźń'), 'zazolc gesla jazn');
  assert.equal(F.fold('ŁÓDŹ'), 'lodz');
  assert.equal(F.fold('Müller  Straße\tKöln'), 'muller strasse koln');
  assert.equal(F.fold('  Ærø   Øl '), 'aero ol');
  assert.equal(F.fold(null), '');
  assert.equal(F.fold(undefined), '');
  assert.equal(F.fold(12), '12');
});

test('parseQuery: słowa, frazy, wykluczenia', () => {
  const q = (text) => F.parseQuery(text);
  assert.deepEqual(q(''), {include: [], exclude: []});
  assert.deepEqual(q('   '), {include: [], exclude: []});
  assert.deepEqual(q('Taxi  Berlin'), {include: ['taxi', 'berlin'], exclude: []});
  assert.deepEqual(q('"Taxi Berlin" -pies -"stary kot"'), {include: ['taxi berlin'], exclude: ['pies', 'stary kot']});
  assert.deepEqual(q('"taxi ber'), {include: ['taxi ber'], exclude: []});       // niedomknięty cudzysłów = reszta tekstu
  assert.deepEqual(q('- taxi-berlin -'), {include: ['taxi-berlin'], exclude: []});  // sam „-” znika, myślnik w środku zostaje
  assert.deepEqual(q('ŁÓDŹ -Ząb'), {include: ['lodz'], exclude: ['zab']});
  assert.deepEqual(q('""  -""'), {include: [], exclude: []});
});

test('wyszukiwarka: wszystkie słowa muszą pasować, wykluczenia odrzucają, litery bez znaczenia', () => {
  assert.deepEqual(find({q: 'lodz'}).sort(), ['driverzy-012', 'eventy-001']);               // „Łódź” po polsku w mieście
  assert.deepEqual(find({q: 'taxi berlin'}), ['driverzy-005']);
  assert.deepEqual(find({q: '"taxi berlin"'}), ['driverzy-005']);
  assert.deepEqual(find({q: '"berlin taxi"'}), []);                                         // fraza zachowuje kolejność
  assert.deepEqual(find({q: 'berlin -taxi'}), ['eventy-009']);
  assert.deepEqual(find({q: '"taxi ber'}), ['driverzy-005']);
  assert.equal(find({q: '-taxi'}).length, 11);
  assert.deepEqual(find({q: 'DRIVERZY-006'}), ['driverzy-006']);                            // po identyfikatorze
  assert.equal(find({q: '   '}).length, 13);
  assert.deepEqual(find({q: 'zzz'}), []);
});

test('wyszukiwarka: po nazwie kraju (polskiej) i kodzie', () => {
  assert.deepEqual(find({q: 'niemcy'}).sort(), ['eventy-004', 'eventy-009', 'eventy-013']);
  assert.deepEqual(find({q: 'wielka brytania'}).sort(), ['eventy-002', 'eventy-003']);
  assert.deepEqual(find({q: 'irlandia'}), ['driverzy-007']);
});

test('wyszukiwarka: wybór pola', () => {
  assert.deepEqual(find({q: 'a5@', qFields: 'email'}), ['driverzy-005']);
  assert.deepEqual(find({q: 'a5@', qFields: 'company'}), []);
  assert.deepEqual(find({q: 'berlin', qFields: 'company'}), ['driverzy-005']);              // w nazwie, nie w mieście
  assert.deepEqual(find({q: 'berlin', qFields: 'place'}), ['eventy-009']);
  assert.deepEqual(find({q: 'kraków floriańska', qFields: 'place'}), ['driverzy-006']);
  assert.deepEqual(find({q: 'example.de', qFields: 'website'}).sort(), ['eventy-004', 'eventy-009', 'eventy-013']);
  assert.deepEqual(find({q: 'transfery', qFields: 'description'}), ['driverzy-006']);
  assert.deepEqual(find({q: 'transfery', qFields: 'nie-ma-takiego'}), ['driverzy-006']);   // zły klucz wraca do „wszędzie”
});

test('wyszukiwarka: osoba kontaktowa wchodzi do pola „firma”', () => {
  const rows = [{id: 'x-1', company: 'Alfa', contact_person: 'Jan Kowalski', status: 'wyslano', segment: 'eventy'}];
  assert.equal(F.filterRows(rows, F.normalizeState({q: 'kowalski', qFields: 'company'}), ctx).length, 1);
});

test('wspólne wektory (te same sprawdza RowFilter po stronie serwera)', () => {
  assert.ok(shared.cases.length >= 20);
  shared.cases.forEach((c) => {
    assert.deepEqual(find(c.filter).sort(), c.expect.slice().sort(), c.name);
  });
});

test('„spoza Polski” to kraj ustawiony i różny od PL; „bez kraju” to pusty', () => {
  assert.deepEqual(find({countries: [F.FOREIGN]}).sort(),
    ['eventy-002', 'eventy-003', 'eventy-004', 'driverzy-007', 'eventy-009', 'eventy-013'].sort());
  assert.deepEqual(find({countries: [F.FOREIGN, F.NO_COUNTRY]}).length, 8);
});

test('trójstanowe: miasto, adres, wysłano', () => {
  assert.deepEqual(find({city: 'no'}).sort(), ['driverzy-005', 'eventy-011']);
  assert.equal(find({city: 'yes'}).length, 11);
  assert.deepEqual(find({address: 'yes'}).sort(), ['driverzy-006', 'eventy-001']);
  assert.deepEqual(find({sent: 'yes'}).sort(), ['driverzy-006', 'driverzy-007', 'eventy-001', 'eventy-009', 'eventy-013']);
  assert.equal(find({sent: 'no'}).length, 8);
});

test('pochodzenie: id z importu kończy się na -nNNN', () => {
  const rows = [{id: 'eventy-051'}, {id: 'eventy-n001'}, {id: 'driverzy-n150'}, {id: 'driverzy-007'}].map((r) => ({...r, status: 'x', segment: 's'}));
  const pick = (origin) => ids(F.filterRows(rows, F.normalizeState({origin}), ctx));
  assert.deepEqual(pick('import'), ['eventy-n001', 'driverzy-n150']);
  assert.deepEqual(pick('dossier'), ['eventy-051', 'driverzy-007']);
  assert.equal(pick('').length, 4);
});

test('zakres dat wygenerowania działa jak wysłania', () => {
  assert.deepEqual(find({generatedFrom: '2026-09-29'}).sort(), ['driverzy-012', 'eventy-010', 'eventy-011', 'eventy-013']);
  assert.equal(find({generatedTo: '2026-09-28'}).length, 9);
  assert.deepEqual(find({generatedFrom: '2026-09-28', generatedTo: '2026-09-28', segments: ['driverzy']}).sort(),
    ['driverzy-005', 'driverzy-006', 'driverzy-007', 'driverzy-008']);
});

test('treść: wiersz z pełnym body_html liczy się tak samo jak has_body', () => {
  const full = [{id: 'a', status: 'x', segment: 's', body_html: '<p>x</p>'}, {id: 'b', status: 'x', segment: 's', body_html: ''}];
  assert.deepEqual(ids(F.filterRows(full, F.normalizeState({body: 'yes'}), ctx)), ['a']);
  assert.deepEqual(ids(F.filterRows(full, F.normalizeState({body: 'no'}), ctx)), ['b']);
});

test('filterRows nie zmienia danych wejściowych i zachowuje ich kolejność', () => {
  const copy = JSON.stringify(ROWS);
  const out = F.filterRows(ROWS, F.normalizeState({countries: ['PL', 'DE']}), ctx);
  assert.equal(JSON.stringify(ROWS), copy);
  const order = ids(ROWS);
  assert.deepEqual(ids(out), order.filter((id) => ids(out).includes(id)));
});

test('sortowanie: polskie litery, puste na końcu w obu kierunkach, stabilność', () => {
  const rows = ['Żabka', 'Zebra', 'Łódź', 'Lublin', 'Mielec', '', 'Ćma', 'Cena'].map((company, i) => ({id: 'r' + i, company}));
  const names = (sort) => F.sortRows(rows, sort, ctx).map((r) => r.company);
  assert.deepEqual(names('company'), ['Cena', 'Ćma', 'Lublin', 'Łódź', 'Mielec', 'Zebra', 'Żabka', '']);
  assert.deepEqual(names('-company'), ['Żabka', 'Zebra', 'Mielec', 'Łódź', 'Lublin', 'Ćma', 'Cena', '']);
  assert.deepEqual(names('default'), rows.map((r) => r.company));
  assert.deepEqual(names('nie-ma'), rows.map((r) => r.company));
  const same = [1, 2, 3, 4].map((n) => ({id: 'p' + n, company: 'Ta sama'}));
  assert.deepEqual(ids(F.sortRows(same, 'company', ctx)), ['p1', 'p2', 'p3', 'p4']);
  assert.deepEqual(ids(F.sortRows(same, '-company', ctx)), ['p1', 'p2', 'p3', 'p4']);
});

test('sortowanie: segment, status, kraj i daty', () => {
  const sorted = (sort) => ids(F.sortRows(ROWS, sort, ctx));
  assert.deepEqual(sorted('segment').slice(0, 8).every((id) => id.startsWith('eventy')), true);  // eventy przed driverzy, choć alfabetycznie odwrotnie
  assert.deepEqual(sorted('-segment').slice(0, 5).every((id) => id.startsWith('driverzy')), true);
  const byStatus = F.sortRows(ROWS, 'status', ctx).map((r) => r.status);
  const order = Object.keys(STATUS_META);
  assert.deepEqual(byStatus, byStatus.slice().sort((a, b) => order.indexOf(a) - order.indexOf(b)));
  assert.deepEqual(F.sortRows(ROWS, 'country', ctx).map((r) => r.country).filter(Boolean).filter((c, i, a) => a.indexOf(c) === i),
    ['IE', 'DE', 'PL', 'GB']);                                                                  // Irlandia, Niemcy, Polska, Wielka Brytania
  assert.equal(F.sortRows(ROWS, 'country', ctx).slice(-2).every((r) => !r.country), true);
  assert.deepEqual(sorted('sent_at').slice(0, 5), ['eventy-013', 'eventy-001', 'driverzy-006', 'driverzy-007', 'eventy-009']);
  assert.deepEqual(sorted('-sent_at').slice(0, 5), ['eventy-009', 'driverzy-007', 'driverzy-006', 'eventy-001', 'eventy-013']);
  assert.equal(F.sortRows(ROWS, '-sent_at', ctx).slice(5).every((r) => !r.sent_at), true);
});

test('nextSort: brak → rosnąco → malejąco → domyślnie', () => {
  assert.equal(F.nextSort('default', 'company'), 'company');
  assert.equal(F.nextSort('company', 'company'), '-company');
  assert.equal(F.nextSort('-company', 'company'), 'default');
  assert.equal(F.nextSort('-email', 'company'), 'company');
});

test('liczniki pól liczą przy pozostałych filtrach, ale bez filtra własnego pola', () => {
  const all = F.facetCounts(ROWS, F.emptyState(), ctx, 'statuses');
  assert.equal(all.wyslano, 3);
  assert.equal(all.do_wyslania, 3);
  const onlySent = F.facetCounts(ROWS, F.normalizeState({statuses: ['wyslano']}), ctx, 'statuses');
  assert.deepEqual(onlySent, all);                                                           // własny filtr nie zawęża własnych liczników
  const polish = F.facetCounts(ROWS, F.normalizeState({countries: ['PL']}), ctx, 'statuses');
  assert.equal(polish.wyslano, 1);
  assert.equal(polish.do_wyslania, 1);
  assert.equal(polish.blad, undefined);
  const countries = F.facetCounts(ROWS, F.emptyState(), ctx, 'countries');
  assert.equal(countries.PL, 5);
  assert.equal(countries[F.NO_COUNTRY], 2);
  assert.equal(countries[F.FOREIGN], 6);
  const segments = F.facetCounts(ROWS, F.normalizeState({email: 'yes'}), ctx, 'segments');
  assert.deepEqual(segments, {eventy: 5, driverzy: 4});
});

test('szybkie widoki: liczby na przykładowych danych', () => {
  const counts = Object.fromEntries(F.presetCounts(ROWS, ctx).map((p) => [p.key, p.count]));
  assert.deepEqual(counts, {
    all: 13, ready: 2, errors: 1, no_email: 4, email_no_body: 1, sent_today: 1, sent_abroad: 3,
    replies: 2, do_not_contact: 1, imported: 0, no_country: 2, no_city: 2
  });
  assert.equal(F.PRESETS.length, 12);
});

test('szybki widok „gotowe do wysłania” to dokładnie to, co można wysłać', () => {
  const ready = F.filterRows(ROWS, F.presetState('ready', ctx), ctx);
  assert.deepEqual(ids(ready).sort(), ['driverzy-005', 'eventy-002']);
  ROWS.forEach((row) => assert.equal(F.isReady(row, ctx), ids(ready).includes(row.id), row.id));
});

test('„wysłane dziś” zależy od dnia z kontekstu', () => {
  const other = F.createContext({statusMeta: STATUS_META, sendable: [], countries: COUNTRIES, today: '2026-09-29'});
  assert.deepEqual(ids(F.filterRows(ROWS, F.presetState('sent_today', other), other)).sort(), ['driverzy-006', 'eventy-001']);
});

test('presetKeyFor: podświetla widok tylko wtedy, gdy filtry mu odpowiadają', () => {
  assert.equal(F.presetKeyFor(F.emptyState(), ctx), 'all');
  assert.equal(F.presetKeyFor(F.presetState('replies', ctx), ctx), 'replies');
  assert.equal(F.presetKeyFor({...F.presetState('replies', ctx), statuses: ['zainteresowany', 'odpowiedzial']}, ctx), 'replies');  // kolejność bez znaczenia
  assert.equal(F.presetKeyFor({...F.presetState('replies', ctx), q: 'taxi'}, ctx), '');
  assert.equal(F.presetKeyFor({...F.presetState('replies', ctx), sort: 'company', qFields: 'email'}, ctx), 'replies');  // sortowanie nie jest filtrem
  assert.equal(F.presetKeyFor(F.normalizeState({statuses: ['blad', 'wyslano']}), ctx), '');
});

test('normalizeState odrzuca wszystko, co nie ma sensu', () => {
  const s = F.normalizeState({
    q: 5, qFields: 'zzz', statuses: 'wyslano', segments: ['eventy', 3, '', 'eventy', null], countries: ['PL'],
    email: 'maybe', body: 'yes', sentFrom: 'wczoraj', sentTo: '2026-09-30', origin: 'kosmos', sort: ';;', extra: 1
  });
  assert.deepEqual(s, {...F.emptyState(), segments: ['eventy'], countries: ['PL'], body: 'yes', sentTo: '2026-09-30'});
  assert.deepEqual(F.normalizeState(null), F.emptyState());
  assert.deepEqual(F.normalizeState('x'), F.emptyState());
  assert.equal(F.normalizeState({statuses: ['x'.repeat(65)]}).statuses.length, 0);
  assert.equal(F.normalizeState({statuses: Array.from({length: 300}, (_, i) => 's' + i)}).statuses.length, 100);
});

test('clearPart czyści tylko wskazaną część', () => {
  const s = F.normalizeState({q: 'a', qFields: 'email', statuses: ['blad'], sentFrom: '2026-09-01', sentTo: '2026-09-30', email: 'yes'});
  assert.deepEqual(F.clearPart(s, 'q'), {...s, q: '', qFields: 'all'});
  assert.deepEqual(F.clearPart(s, 'sent_range'), {...s, sentFrom: '', sentTo: ''});
  assert.deepEqual(F.clearPart(s, 'statuses').statuses, []);
  assert.equal(F.clearPart(s, 'statuses').email, 'yes');
  assert.deepEqual(F.clearPart(s, 'nie-ma'), s);
});

test('activeParts i hasFilters', () => {
  assert.deepEqual(F.activeParts(F.emptyState()), []);
  assert.equal(F.hasFilters(F.emptyState()), false);
  assert.equal(F.hasFilters({q: '   '}), false);
  assert.equal(F.hasFilters({sort: 'company'}), false);   // sortowanie to nie filtr
  assert.deepEqual(F.activeParts({q: 'x', countries: ['PL'], sentTo: '2026-09-30'}), ['q', 'countries', 'sent_range']);
});

test('opis aktywnych filtrów po polsku', () => {
  const text = (state) => F.describe(F.normalizeState(state), ctx).map((d) => d.text);
  assert.deepEqual(text({statuses: ['wyslano', 'blad']}), ['Status: Wysłano, Błąd']);
  assert.deepEqual(text({countries: [F.FOREIGN, F.NO_COUNTRY, 'PL']}), ['Kraj: spoza Polski, bez kraju, Polska']);
  assert.deepEqual(text({email: 'no', body: 'yes', city: 'no', sent: 'yes'}),
    ['E-mail: brak', 'Treść maila: jest', 'Miasto: brak', 'Wysłano: tak']);
  assert.deepEqual(text({sentFrom: '2026-09-01', sentTo: '2026-09-30'}), ['Data wysłania: 2026-09-01 – 2026-09-30']);
  assert.deepEqual(text({sentFrom: '2026-09-01', sentTo: '2026-09-01'}), ['Data wysłania: 2026-09-01']);
  assert.deepEqual(text({generatedFrom: '2026-09-01'}), ['Data wygenerowania od 2026-09-01']);
  assert.deepEqual(text({generatedTo: '2026-09-01'}), ['Data wygenerowania do 2026-09-01']);
  assert.deepEqual(text({q: ' taxi ', qFields: 'company'}), ['Szukaj (firma): taxi']);
  assert.deepEqual(text({origin: 'import', segments: ['eventy']}), ['Segment: eventy', 'Pochodzenie: zaimportowane']);
  assert.deepEqual(text({}), []);
  assert.deepEqual(text({statuses: ['nieznany']}), ['Status: nieznany']);
});

test('adres URL: pusty dla stanu domyślnego i wiernie odtwarzany', () => {
  assert.equal(F.toQuery(F.emptyState()), '');
  const state = F.normalizeState({
    q: '"taxi berlin" -pies, ala ma kota & więcej=1', qFields: 'company', statuses: ['wyslano', 'blad'],
    segments: ['driverzy'], countries: ['PL', '_none', '_foreign'], email: 'yes', body: 'no', city: 'no', address: 'yes',
    sent: 'yes', origin: 'import', sentFrom: '2026-09-01', sentTo: '2026-09-30', generatedFrom: '2026-08-01',
    generatedTo: '2026-08-31', sort: '-sent_at'
  });
  const query = F.toQuery(state);
  assert.ok(query.includes('status=wyslano,blad'));
  assert.ok(query.includes('country=PL,_none,_foreign'));
  assert.ok(!query.includes('&&'));
  assert.deepEqual(F.fromQuery(query), state);
  assert.deepEqual(F.fromQuery('?' + query), state);
  assert.deepEqual(F.fromQuery(''), F.emptyState());
});

test('adres URL: śmieci są ignorowane, nie psują strony', () => {
  const s = F.fromQuery('status=,,wyslano,&email=maybe&sent_from=wczoraj&sort=%00&qf=hack&foo=bar&country=PL');
  assert.deepEqual(s, {...F.emptyState(), statuses: ['wyslano'], countries: ['PL']});
});

test('localDay i shiftDay liczą w czasie lokalnym', () => {
  assert.equal(F.localDay(new Date(2026, 8, 30, 23, 59)), '2026-09-30');
  assert.equal(F.localDay(new Date(2026, 0, 5, 0, 1)), '2026-01-05');
  assert.equal(F.shiftDay('2026-09-30', 1), '2026-10-01');
  assert.equal(F.shiftDay('2026-03-01', -1), '2026-02-28');
  assert.equal(F.shiftDay('2026-01-01', -7), '2025-12-25');
});
