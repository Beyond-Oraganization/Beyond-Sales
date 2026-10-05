'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Model listy firm bez przeglądarki: żądania do serwera są
// atrapą, a czas ręcznym zegarem, więc kolejność zdarzeń jest dokładnie taka, jaką ustawi test.
const test = require('node:test');
const assert = require('node:assert/strict');
const F = require('../../src/perno/web/pages/assets/filters.js');
const S = require('../../src/perno/web/pages/assets/selection.js');
const M = require('../../src/perno/web/pages/assets/listmodel.js');

const tick = () => new Promise((resolve) => setImmediate(resolve));

function manualTimers() {
  let next = 1;
  const pending = new Map();
  return {
    set(fn) { const id = next++; pending.set(id, fn); return id; },
    clear(id) { pending.delete(id); },
    flush() { const due = [...pending.values()]; pending.clear(); due.forEach((fn) => fn()); },
    count() { return pending.size; },
  };
}

function setup(options) {
  const calls = [];
  const timers = manualTimers();
  const selection = S.create();
  const ctx = F.createContext({statusMeta: {do_wyslania: ['Do wysłania', '#000']}, sendable: ['do_wyslania'], countries: []});
  const api = (url, payload) => new Promise((resolve, reject) => { calls.push({url, payload, resolve, reject}); });
  const model = M.create({api, filters: F, selection, ctx, timers, ...options});
  const events = [];
  model.subscribe((type) => events.push(type));
  return {model, calls, timers, selection, events};
}

const page = (extra) => ({ok: true, status: 200, data: {rows: [{id: 'a'}, {id: 'b'}], total: 2, page: 1, pages: 1, size: 100, ...extra}});
const FACETS = {
  overview: {statuses: {do_wyslania: 2}, segments: {eventy: 2}, countries: {PL: 2, _foreign: 0}},
  totals: {statuses: {do_wyslania: 2}, segments: {eventy: 2}, countries: {PL: 2}, all: 2},
  presets: [2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
};

async function loaded(env, extra) {
  const done = env.model.load();
  env.timers.flush();
  env.calls[0].resolve(page({...FACETS, ...extra}));
  await done;
  env.calls.length = 0;
  env.events.length = 0;
}

test('pierwsze wczytanie prosi o stronę, liczniki, sumy i szybkie widoki', async () => {
  const env = setup();
  const done = env.model.load();
  env.timers.flush();
  assert.equal(env.calls.length, 1);
  const {url, payload} = env.calls[0];
  assert.equal(url, '/api/mailing/list');
  assert.deepEqual([payload.page, payload.size, payload.counts, payload.totals], [1, 100, true, true]);
  assert.deepEqual(payload.state, F.emptyState());
  assert.equal(payload.presets.length, F.PRESETS.length);
  assert.deepEqual(payload.presets[1], F.presetState('ready', F.createContext({sendable: ['do_wyslania']})));
  assert.equal(payload.ids, undefined);
  assert.equal(env.model.isLoading(), true);
  env.calls[0].resolve(page(FACETS));
  await done;
  assert.deepEqual(env.model.rows().map((r) => r.id), ['a', 'b']);
  assert.equal(env.model.total(), 2);
  assert.deepEqual(env.model.facets().presets, FACETS.presets);
  assert.equal(env.model.isLoading(), false);
  assert.deepEqual(env.events.filter((e) => e !== 'summary'), ['loading', 'data']);
});

test('zmiana strony i sortowania nie prosi o liczniki, a stare liczniki zostają', async () => {
  const env = setup();
  await loaded(env);
  const next = env.model.goTo(2);
  env.timers.flush();
  assert.deepEqual([env.calls[0].payload.page, env.calls[0].payload.counts, env.calls[0].payload.totals, env.calls[0].payload.presets],
    [2, undefined, undefined, undefined]);
  env.calls[0].resolve(page({page: 2, pages: 3, total: 250}));
  await next;
  assert.equal(env.model.getPage(), 2);
  assert.equal(env.model.total(), 250);
  assert.deepEqual(env.model.facets().overview, FACETS.overview);   // odpowiedź bez liczników ich nie kasuje
  const sorted = env.model.setSort('company');
  env.timers.flush();
  assert.deepEqual([env.calls[1].payload.state.sort, env.calls[1].payload.page, env.calls[1].payload.counts], ['company', 1, undefined]);
  env.calls[1].resolve(page());
  await sorted;
});

test('zmiana filtra wraca na pierwszą stronę i prosi o liczniki, ale nie o sumy ani widoki', async () => {
  const env = setup();
  await loaded(env);
  env.model.goTo(3);
  env.timers.flush();
  env.calls[0].resolve(page({page: 3, pages: 5}));
  await tick();
  env.calls.length = 0;
  const done = env.model.patch({statuses: ['blad']});
  assert.equal(env.model.getPage(), 1);
  env.timers.flush();
  const payload = env.calls[0].payload;
  assert.deepEqual([payload.state.statuses, payload.page, payload.counts, payload.totals, payload.presets], [['blad'], 1, true, undefined, undefined]);
  env.calls[0].resolve(page());
  await done;
});

test('szybko po sobie idące zmiany sklejają się w jedno żądanie z ostatnim stanem', async () => {
  const env = setup();
  await loaded(env);
  const a = env.model.patch({q: 'a'});
  const b = env.model.patch({q: 'ab'});
  const c = env.model.patch({q: 'abc'});
  assert.equal(env.timers.count(), 1);
  env.timers.flush();
  assert.equal(env.calls.length, 1);
  assert.equal(env.calls[0].payload.state.q, 'abc');
  env.calls[0].resolve(page());
  await Promise.all([a, b, c]);   // każdy czekający dostaje odpowiedź
});

test('odpowiedź wyprzedzona nowszą zmianą widoku jest odrzucana, a jej potrzeby nie giną', async () => {
  const env = setup();
  await loaded(env);
  const first = env.model.patch({statuses: ['blad']});   // prosi o liczniki
  env.timers.flush();
  const second = env.model.goTo(2);                      // widok zmienił się, zanim przyszła odpowiedź na pierwsze
  assert.equal(env.model.getPage(), 2);
  env.calls[0].resolve(page({rows: [{id: 'stare'}]}));   // spóźniona
  await tick();
  assert.deepEqual(env.model.rows().map((r) => r.id), ['a', 'b'], 'stara odpowiedź nie zastępuje tego, co już widać');
  assert.equal(env.model.getPage(), 2, 'ani nie cofa strony');
  env.timers.flush();
  assert.equal(env.calls.length, 2);
  assert.deepEqual([env.calls[1].payload.page, env.calls[1].payload.counts], [2, true], 'liczniki, o które prosiło pierwsze żądanie, wracają');
  env.calls[1].resolve(page({rows: [{id: 'nowe'}], page: 2, pages: 2, total: 150}));
  await Promise.all([first, second]);
  assert.deepEqual(env.model.rows().map((r) => r.id), ['nowe']);
});

test('serwer może przyciąć stronę (np. po zawężeniu filtra)', async () => {
  const env = setup();
  await loaded(env);
  const done = env.model.goTo(9);
  env.timers.flush();
  env.calls[0].resolve(page({page: 4, pages: 4, total: 330}));
  await done;
  assert.deepEqual([env.model.getPage(), env.model.pages()], [4, 4]);
});

test('błąd serwera i błąd sieci dają komunikat, a następne żądanie prosi o to, czego zabrakło', async () => {
  const env = setup();
  await loaded(env);
  let done = env.model.patch({q: 'x'});
  env.timers.flush();
  env.calls[0].resolve({ok: false, status: 400, data: {error: 'Zły filtr.'}});
  await done;
  assert.equal(env.model.error(), 'Zły filtr.');
  assert.equal(env.model.isLoading(), false);
  assert.ok(env.events.includes('error'));
  done = env.model.goTo(2);
  env.timers.flush();
  assert.equal(env.calls[1].payload.counts, true, 'liczniki z nieudanego żądania są nadal należne');
  env.calls[1].reject(new Error('brak sieci'));
  await done;
  assert.match(env.model.error(), /połączyć z serwerem/);
  done = env.model.goTo(1);
  env.timers.flush();
  env.calls[2].resolve(page());
  await done;
  assert.equal(env.model.error(), '');
});

test('widok „tylko zaznaczone” wysyła id i wstrzymuje filtry, zostaje sortowanie', async () => {
  const env = setup();
  await loaded(env);
  env.model.patch({q: 'taxi', sort: 'company'});
  env.timers.flush();
  env.calls[0].resolve(page());
  await tick();
  env.calls.length = 0;
  env.selection.add(['x', 'y']);
  const done = env.model.setOnlySelected(true);
  env.timers.flush();
  const payload = env.calls[0].payload;
  assert.deepEqual(payload.ids, ['x', 'y']);
  assert.deepEqual(payload.state, {sort: 'company'});
  assert.equal(payload.counts, true);
  env.calls[0].resolve(page());
  await done;
  env.selection.add(['z']);
  const again = env.model.selectionChanged();   // widok zależy od zaznaczenia: trzeba go wczytać jeszcze raz
  env.timers.flush();
  const reload = env.calls.find((call) => call.url === '/api/mailing/list' && call.payload.ids && call.payload.ids.length === 3);
  assert.ok(reload);
  reload.resolve(page());
  await again;
});

test('podsumowanie zaznaczenia: puste bez pytania serwera, niepuste po zwłoce, spóźnione odrzucone', async () => {
  const env = setup();
  await loaded(env);
  env.model.selectionChanged();
  assert.deepEqual(env.model.summary(), {count: 0, ready: 0, foreign: {}, in_view: 0});
  assert.equal(env.calls.length, 0);
  env.selection.add(['a', 'b']);
  env.model.selectionChanged();
  assert.equal(env.model.summaryPending(), true);
  assert.equal(env.calls.length, 0, 'żądanie dopiero po chwili spokoju');
  env.selection.add(['c']);
  env.model.selectionChanged();   // kolejna zmiana kasuje poprzednie czekanie
  env.timers.flush();
  assert.equal(env.calls.length, 1);
  assert.equal(env.calls[0].url, '/api/mailing/selection');
  assert.deepEqual(env.calls[0].payload, {ids: ['a', 'b', 'c'], state: F.emptyState()});
  env.calls[0].resolve({ok: true, data: {count: 3, ready: 2, foreign: {DE: 1}, in_view: 3}});
  await tick();
  assert.deepEqual([env.model.summary().ready, env.model.summary().foreign, env.model.summaryPending()], [2, {DE: 1}, false]);
  env.selection.add(['d']);
  env.model.selectionChanged();
  env.timers.flush();
  const older = env.calls[1];
  env.selection.add(['e']);
  env.model.selectionChanged();
  env.timers.flush();
  env.calls[2].resolve({ok: true, data: {count: 5, ready: 5, foreign: {}, in_view: 5}});
  await tick();
  older.resolve({ok: true, data: {count: 4, ready: 0, foreign: {}, in_view: 4}});   // spóźnione
  await tick();
  assert.equal(env.model.summary().count, 5);
});

test('podsumowanie w widoku „tylko zaznaczone” nie wysyła filtra, a wszystkie zaznaczone są „w widoku”', async () => {
  const env = setup();
  await loaded(env);
  env.selection.add(['a']);
  env.model.setOnlySelected(true);
  env.timers.flush();
  env.calls[0].resolve(page());
  await tick();
  env.timers.flush();
  const call = env.calls.find((c) => c.url === '/api/mailing/selection');
  assert.deepEqual(call.payload, {ids: ['a']});
  call.resolve({ok: true, data: {count: 1, ready: 1, foreign: {}}});
  await tick();
  assert.equal(env.model.summary().in_view, 1);
});

test('viewIds pyta serwer o id widoku z dodatkowymi warunkami i zgłasza błąd', async () => {
  const env = setup();
  await loaded(env);
  env.model.patch({statuses: ['blad']});
  env.timers.flush();
  env.calls[0].resolve(page());
  await tick();
  env.calls.length = 0;
  const pending = env.model.viewIds({ready: true, limit: 10});
  assert.equal(env.calls[0].url, '/api/mailing/ids');
  assert.deepEqual(env.calls[0].payload, {state: env.model.getState(), ready: true, limit: 10});
  env.calls[0].resolve({ok: true, data: {ids: ['a'], truncated: false}});
  assert.deepEqual(await pending, {ids: ['a'], truncated: false});
  const failing = env.model.viewIds();
  env.calls[1].resolve({ok: false, status: 400, data: {error: 'Za dużo.'}});
  await assert.rejects(failing, /Za dużo\./);
  const broken = env.model.viewIds();
  env.calls[2].reject(new Error('x'));
  await assert.rejects(broken, /połączyć z serwerem/);
});

test('exportScope: stan widoku albo, przy „tylko zaznaczone”, id i sortowanie', async () => {
  const env = setup();
  await loaded(env);
  env.model.patch({q: 'x', sort: '-company'});
  assert.deepEqual(env.model.exportScope(), {state: env.model.getState()});
  env.selection.add(['a', 'b']);
  env.model.setOnlySelected(true);
  assert.deepEqual(env.model.exportScope(), {ids: ['a', 'b'], sort: '-company'});
});

test('restore ustawia stan bez pytania serwera i ignoruje złe wartości', () => {
  const env = setup();
  env.model.restore({state: {statuses: ['blad'], sort: 'company'}, page: 3, size: 200});
  assert.deepEqual([env.model.getState().statuses, env.model.getPage(), env.model.getSize()], [['blad'], 3, 200]);
  env.model.restore({state: null, page: -4, size: 77});
  assert.deepEqual([env.model.getPage(), env.model.getSize()], [3, 200]);
  assert.equal(env.calls.length, 0);
  assert.equal(env.timers.count(), 0);
});

test('zmiana rozmiaru strony wraca na pierwszą; rozmiar spoza listy jest odrzucany', async () => {
  const env = setup();
  await loaded(env);
  env.model.goTo(4);
  env.timers.flush();
  env.calls[0].resolve(page({page: 4}));
  await tick();
  env.calls.length = 0;
  const done = env.model.setSize(200);
  env.timers.flush();
  assert.deepEqual([env.calls[0].payload.size, env.calls[0].payload.page], [200, 1]);
  env.calls[0].resolve(page({size: 200}));
  await done;
  await env.model.setSize(7);
  env.timers.flush();
  assert.equal(env.calls.length, 1, 'rozmiar spoza listy nic nie robi');
  assert.deepEqual(M.SIZES, [50, 100, 200, 500]);
});

test('refresh prosi o wszystko: dane zmieniły się poza listą', async () => {
  const env = setup();
  await loaded(env);
  const done = env.model.refresh();
  env.timers.flush();
  const payload = env.calls[0].payload;
  assert.deepEqual([payload.counts, payload.totals, Array.isArray(payload.presets)], [true, true, true]);
  env.calls[0].resolve(page(FACETS));
  await done;
});

test('dzień szybkich widoków liczony jest w chwili żądania', async () => {
  const env = setup();
  const done = env.model.load();
  env.timers.flush();
  const today = F.localDay();
  const sentToday = env.calls[0].payload.presets[F.PRESETS.findIndex((p) => p.key === 'sent_today')];
  assert.deepEqual([sentToday.sentFrom, sentToday.sentTo], [today, today]);
  env.calls[0].resolve(page(FACETS));
  await done;
});
