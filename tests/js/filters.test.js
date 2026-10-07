'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Sprawdzają stan filtrów, szybkie widoki, opis i adres URL
// (filters.js) — bez przeglądarki. Wiersze, wyszukiwanie, sortowanie i liczniki liczy serwer: patrz tests/test_listing.py.
const test = require('node:test');
const assert = require('node:assert/strict');
const F = require('../../src/perno/web/pages/assets/filters.js');
const vectors = require('../listing_cases.json');

const STATUS_META = {
  do_wyslania: ['Do wysłania', '#2f6feb'], wysylanie: ['Wysyłanie…', '#8e44ad'], wyslano: ['Wysłano', '#1a7f4b'],
  odpowiedzial: ['Odpowiedział', '#0e7490'], zainteresowany: ['Zainteresowany', '#be185d'], blad: ['Błąd', '#c0392b'],
  pominieto_brak_email: ['Brak e-maila', '#6b7280'], pominieto_klient: ['Już klient', '#8a5a00'],
  pominieto_dopasowanie: ['Nie pasuje', '#475569'],
  oczekuje_na_tresc: ['Czeka na treść', '#b8860b'], nie_kontaktowac: ['Nie kontaktować', '#7f1d1d']
};
const COUNTRIES = [['PL', 'Polska', '🇵🇱'], ['DE', 'Niemcy', '🇩🇪'], ['GB', 'Wielka Brytania', '🇬🇧'], ['IE', 'Irlandia', '🇮🇪']];
const ctx = F.createContext({statusMeta: STATUS_META, sendable: ['do_wyslania', 'blad'], countries: COUNTRIES, today: '2026-09-30'});

test('nextSort: brak → rosnąco → malejąco → domyślnie', () => {
  assert.equal(F.nextSort('default', 'company'), 'company');
  assert.equal(F.nextSort('company', 'company'), '-company');
  assert.equal(F.nextSort('-company', 'company'), 'default');
  assert.equal(F.nextSort('-email', 'company'), 'company');
  assert.equal(F.sortKeyOf('-sent_at'), 'sent_at');
  assert.equal(F.sortKeyOf('default'), 'default');
});

test('szybkie widoki: dwanaście, o niepowtarzalnych kluczach, każdy ze stanem, który serwer zrozumie', () => {
  assert.equal(F.PRESETS.length, 12);
  assert.equal(new Set(F.PRESETS.map((p) => p.key)).size, 12);
  F.PRESETS.forEach((p) => {
    const state = F.presetState(p.key, ctx);
    assert.deepEqual(F.normalizeState(state), state, p.key);
  });
});

test('szybkie widoki zgadzają się ze stanami, których użyły wektory serwera (tests/listing_cases.json)', () => {
  const vectorCtx = F.createContext({statusMeta: STATUS_META, sendable: ['do_wyslania', 'blad'], countries: COUNTRIES, today: vectors.today});
  assert.deepEqual(vectors.presets.map((p) => p.key), F.PRESETS.map((p) => p.key), 'kolejność liczników w odpowiedzi serwera');
  vectors.presets.forEach((p) => {
    assert.deepEqual(F.presetState(p.key, vectorCtx), F.normalizeState(p.state), p.key);
  });
});

test('„wysłane dziś” zależy od dnia z kontekstu', () => {
  const other = F.createContext({statusMeta: STATUS_META, sendable: [], countries: COUNTRIES, today: '2026-09-29'});
  const state = F.presetState('sent_today', other);
  assert.deepEqual([state.sentFrom, state.sentTo], ['2026-09-29', '2026-09-29']);
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
  assert.equal(F.normalizeState({q: 'a'.repeat(300)}).q.length, 200);
});

test('clearPart czyści tylko wskazaną część', () => {
  const s = F.normalizeState({q: 'a', qFields: 'email', statuses: ['blad'], sentFrom: '2026-09-01', sentTo: '2026-09-30', email: 'yes'});
  assert.deepEqual(F.clearPart(s, 'q'), {...s, q: '', qFields: 'all'});
  assert.deepEqual(F.clearPart(s, 'sent_range'), {...s, sentFrom: '', sentTo: ''});
  assert.deepEqual(F.clearPart(s, 'statuses').statuses, []);
  assert.equal(F.clearPart(s, 'statuses').email, 'yes');
  assert.deepEqual(F.clearPart(s, 'nie-ma'), s);
});

test('activeParts: sortowanie i same spacje nie są filtrem', () => {
  assert.deepEqual(F.activeParts(F.emptyState()), []);
  assert.deepEqual(F.activeParts({q: '   '}), []);
  assert.deepEqual(F.activeParts({sort: 'company'}), []);
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

test('kontekst: nazwy i flagi krajów, kolejność statusów', () => {
  assert.equal(ctx.countryNames.PL, 'Polska');
  assert.equal(ctx.countryFlags.DE, '🇩🇪');
  assert.deepEqual(ctx.statusOrder.slice(0, 3), ['do_wyslania', 'wysylanie', 'wyslano']);
  assert.equal(ctx.today, '2026-09-30');
});

// ---- firmy z wieloma adresami: filtry „adresy firmy” i „wstrzymane” oraz „grupuj wg firmy” --------------------------

test('filtry „adresy firmy” i „wstrzymane” są trójstanowe, a nieznane wartości wracają do „dowolnie”', () => {
  assert.equal(F.emptyState().multi, '');
  assert.equal(F.emptyState().held, '');
  const s = F.normalizeState({multi: 'yes', held: 'no'});
  assert.deepEqual([s.multi, s.held], ['yes', 'no']);
  const bad = F.normalizeState({multi: 'tak', held: 3});
  assert.deepEqual([bad.multi, bad.held], ['', '']);
});

test('„grupuj wg firmy” to tylko „yes” i nie jest filtrem', () => {
  assert.equal(F.emptyState().group, '');
  assert.equal(F.normalizeState({group: 'yes'}).group, 'yes');
  for (const value of ['', 'no', true, 1, 'YES']) assert.equal(F.normalizeState({group: value}).group, '', String(value));
  assert.deepEqual(F.activeParts({group: 'yes', sort: 'status'}), [], 'grupowanie nie liczy się do aktywnych filtrów');
  assert.equal(F.presetKeyFor({group: 'yes'}, ctx), 'all', 'grupowanie nie zmienia podświetlonego szybkiego widoku');
});

test('nowe filtry mają opis, część do usunięcia i ślad w adresie URL', () => {
  const state = {multi: 'yes', held: 'yes'};
  assert.deepEqual(F.activeParts(state), ['multi', 'held']);
  assert.deepEqual(F.describe(state, ctx).map((d) => d.text), ['Adresy firmy: więcej niż jeden', 'Wstrzymane: tak']);
  assert.deepEqual(F.describe({multi: 'no', held: 'no'}, ctx).map((d) => d.text), ['Adresy firmy: jeden', 'Wstrzymane: nie']);
  const cleared = F.clearPart(state, 'multi');
  assert.deepEqual([cleared.multi, cleared.held], ['', 'yes']);
  assert.equal(F.toQuery(state), 'multi=yes&held=yes');
  assert.deepEqual(F.fromQuery('multi=yes&held=no&group=yes&sort=status'), F.normalizeState({multi: 'yes', held: 'no', group: 'yes', sort: 'status'}));
});

test('adres URL zapamiętuje grupowanie razem z sortowaniem i odtwarza je wiernie', () => {
  const state = F.normalizeState({sort: '-status', group: 'yes', statuses: ['blad']});
  assert.equal(F.toQuery(state), 'status=blad&sort=-status&group=yes');
  assert.deepEqual(F.fromQuery(F.toQuery(state)), state);
  assert.equal(F.toQuery(F.emptyState()), '');
});
