'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Edytor przedziałów odstępu w zakładce Automat: wiersze (od, do, jednostka,
// prawdopodobieństwo), suma 100%, przeliczanie jednostek, zapis w sekundach i brak przebudowy wierszy przy odświeżaniu stanu.
const test = require('node:test');
const assert = require('node:assert/strict');
const {createApp, El} = require('./support/mini_dom.js');

// Strona Automat składa część widoku przez innerHTML (kafle, prognoza); atrapa DOM tego zabrania, więc tu zapamiętujemy zapis.
Object.defineProperty(El.prototype, 'innerHTML', {configurable: true, set(value) { this.html = String(value); }, get() { return this.html || ''; }});

const IDS = {};
for (const id of ['banners', 'statusBox', 'doctorBox', 'bandsBox', 'bandsList', 'bandsSum', 'forecastBox', 'eventsBox', 'savedInfo', 'daysBox',
  'confirmBody', 'toasts']) IDS[id] = {};
for (const id of ['startBtn', 'pauseBtn', 'resumeBtn', 'stopBtn', 'daemonInstallBtn', 'daemonRestartBtn', 'daemonRemoveBtn', 'doctorBtn',
  'notifyBtn', 'saveBtn', 'addBandBtn', 'evenBandsBtn', 'confirmOk', 'confirmCancel']) IDS[id] = {tag: 'button'};
for (const id of ['f_daily_cap', 'f_window_start', 'f_window_end', 'f_horizon_days', 'f_max_consecutive_errors', 'f_company_gap_days',
  'f_countries', 'ackBox', 'd0', 'd1', 'd2', 'd3', 'd4', 'd5', 'd6']) IDS[id] = {tag: 'input'};
IDS.f_segment = {tag: 'select'};
IDS.confirmDlg = {tag: 'dialog'};

const BANDS = [{min: 60, max: 120, percent: 50}, {min: 300, max: 600, percent: 40}, {min: 1200, max: 3600, percent: 10}];
const SETTINGS = {daily_cap: 50, weekdays: [0, 1, 2, 3, 4], window_start: '09:00', window_end: '16:00', delay_min: 60, delay_max: 3600,
  delay_bands: BANDS, segment: 'both', countries: [], horizon_days: 5, max_consecutive_errors: 3, company_gap_days: 1};
const STATE = {settings: SETTINGS, settings_ok: true, stamp: '2026-10-05T10:00:00', armed: false, armed_at: '', armed_until: '', paused: false,
  pause_reason: '', streak: 0, daemon: {running: true, heartbeat_age: 3, pid: 1, stats: {}}, sent_today: 0, capacity: 50, mailboxes: [],
  now: '2026-10-05T11:00:00', launchd: {installed: true, loaded: true, pid: 1}, power: {on_battery: false, percent: 100},
  channels: ['macos', 'ntfy'], events: []};
const PREVIEW = {queue: 5, companies: 5, held: 0, deferred_today: 0, gap_days: 1, limited_by_gap: false, foreign: 0, no_country: 0, countries: [],
  sent_today: 0, send_days: 1, last_day: '2026-10-06', fits_in_horizon: true, until: '2026-10-10T00:00:00', plan: [['2026-10-06', 5]],
  delay_mean: 465, window_mails: 55};

const clone = (value) => JSON.parse(JSON.stringify(value));

async function boot(over) {
  const state = clone({...STATE, ...(over && over.state)});
  const preview = clone({...PREVIEW, ...(over && over.preview)});
  const app = createApp({ids: IDS, respond: async (url) => {
    if (url.startsWith('/api/automat/status')) return state;
    if (url.startsWith('/api/automat/preview')) return preview;
    return {};
  }});
  app.load(['common.js', 'automat.js']);
  await app.settle();
  return app;
}

const rows = (app) => app.all('.band-row');
const field = (row, caption) => row.querySelectorAll('label').find((label) => label.textContent.startsWith(caption)).descendants()
  .find((node) => node.tag === 'input' || node.tag === 'select');
const values = (app) => rows(app).map((row) => [field(row, 'Od').value, field(row, 'Do').value, field(row, 'Jednostka').value,
  field(row, 'Prawdopodobieństwo').value]);
const read = (app) => JSON.parse(app.eval('JSON.stringify(readBands())'));  // obiekty z piaskownicy vm mają inny prototyp
const type = (node, value) => { node.value = value; node.dispatch('input'); };

test('zapisane przedziały trafiają do wierszy z jednostką dobraną do wartości', async () => {
  const app = await boot();
  assert.deepEqual(values(app), [['1', '2', 'min', '50'], ['5', '10', 'min', '40'], ['20', '60', 'min', '10']]);
  assert.equal(app.el('bandsSum').textContent, 'Suma: 100%');
  assert.equal(app.el('evenBandsBtn').hidden, true);
  assert.equal(app.eval('isDirty()'), false);
});

test('przedział w sekundach, który nie jest pełną minutą, zostaje w sekundach', async () => {
  const app = await boot({state: {settings: {...SETTINGS, delay_bands: [{min: 45, max: 120, percent: 100}]}}});
  assert.deepEqual(values(app), [['45', '120', 's', '100']]);
});

test('zapis sprzed przedziałów (sam zakres min–max) to jeden przedział na 100%', async () => {
  const legacy = {...SETTINGS, delay_min: 45, delay_max: 120};
  delete legacy.delay_bands;
  const app = await boot({state: {settings: legacy}});
  assert.deepEqual(values(app), [['45', '120', 's', '100']]);
  assert.equal(app.eval('isDirty()'), false);
});

test('suma różna od 100% jest widoczna, a przycisk wyrównania pojawia się', async () => {
  const app = await boot();
  type(field(rows(app)[2], 'Prawdopodobieństwo'), '20');
  assert.equal(app.el('bandsSum').textContent, 'Suma: 110% — musi wynosić 100%');
  assert.ok(app.el('bandsSum').classList.contains('is-off'));
  assert.equal(app.el('evenBandsBtn').hidden, false);
  assert.equal(app.eval('isDirty()'), true);
});

test('wyrównanie skaluje prawdopodobieństwa do dokładnie 100%', async () => {
  const app = await boot();
  type(field(rows(app)[2], 'Prawdopodobieństwo'), '20');
  app.el('evenBandsBtn').click();
  const percents = values(app).map((row) => Number(row[3]));
  assert.deepEqual(percents, [45.45, 36.36, 18.19]);
  assert.equal(Math.round(percents.reduce((a, b) => a + b, 0) * 100) / 100, 100);
  assert.equal(app.el('bandsSum').textContent, 'Suma: 100%');
  assert.equal(app.el('evenBandsBtn').hidden, true);
});

test('dodany przedział dostaje brakującą resztę, a przy pełnej sumie zostaje pusty', async () => {
  const app = await boot({state: {settings: {...SETTINGS, delay_bands: [{min: 60, max: 120, percent: 70}]}}});
  app.el('addBandBtn').click();
  assert.deepEqual(values(app), [['1', '2', 'min', '70'], ['1', '2', 'min', '30']]);
  app.el('addBandBtn').click();
  assert.equal(values(app)[2][3], '');
  assert.equal(app.el('evenBandsBtn').hidden, true, 'puste pole nie pozwala wyrównać');
});

test('usunąć można każdy przedział poza ostatnim', async () => {
  const app = await boot();
  rows(app)[1].querySelector('button').click();
  assert.equal(rows(app).length, 2);
  assert.equal(app.el('bandsSum').textContent, 'Suma: 60% — musi wynosić 100%');
  rows(app)[1].querySelector('button').click();
  assert.equal(rows(app).length, 1);
  assert.equal(rows(app)[0].querySelector('button').disabled, true);
});

test('zmiana jednostki przelicza wpisane liczby i nie zmienia przedziału', async () => {
  const app = await boot();
  const row = rows(app)[0];
  const unit = field(row, 'Jednostka');
  unit.value = 's';
  unit.dispatch('change');
  assert.deepEqual([field(row, 'Od').value, field(row, 'Do').value], ['60', '120']);
  assert.deepEqual(read(app)[0], {min: 60, max: 120, percent: 50});
  unit.value = 'min';
  unit.dispatch('change');
  assert.deepEqual([field(row, 'Od').value, field(row, 'Do').value], ['1', '2']);
  assert.equal(app.eval('isDirty()'), false);
});

test('ułamek minuty daje sekundy, a puste pole błąd do pokazania przez serwer', async () => {
  const app = await boot();
  const row = rows(app)[0];
  type(field(row, 'Od'), '1.5');
  assert.equal(read(app)[0].min, 90);
  type(field(row, 'Do'), '');
  assert.equal(read(app)[0].max, null);
});

test('zapis wysyła przedziały w sekundach i procentach', async () => {
  const app = await boot();
  type(field(rows(app)[0], 'Od'), '2');
  type(field(rows(app)[0], 'Do'), '3');
  await app.eval('saveLimits()');
  const [call] = app.callsTo('/api/automat/limits');
  assert.deepEqual(call.body.delay_bands, [{min: 120, max: 180, percent: 50}, {min: 300, max: 600, percent: 40}, {min: 1200, max: 3600, percent: 10}]);
  assert.equal('delay_min' in call.body, false, 'dawne pola nie idą razem z przedziałami');
});

test('odświeżenie stanu nie przebudowuje wierszy, które pokazują te same przedziały', async () => {
  const app = await boot();
  const before = rows(app);
  app.eval('fillForm(STATE.settings)');
  assert.ok(rows(app).every((row, index) => row === before[index]), 'te same węzły (fokus i kursor zostają)');
  app.eval('fillForm({...STATE.settings, delay_bands: [{min: 10, max: 20, percent: 100}]})');
  assert.equal(rows(app).length, 1, 'inne przedziały z serwera zastępują wiersze');
});

test('okno potwierdzenia Startu wypisuje przedziały i przepustowość okna', async () => {
  const app = await boot();
  const summary = app.eval('confirmSummary()');
  assert.ok(summary.includes('odstęp między mailami: 1 min – 2 min (50%), 5 min – 10 min (40%), 20 min – 60 min (10%)'));
  assert.ok(summary.includes('średni odstęp: 7 min 45 s, w oknie zmieści się ok. 55 maili dziennie'));
  const single = app.eval('STATE.settings.delay_bands = [{min: 45, max: 120, percent: 100}]; confirmSummary()');
  assert.ok(single.includes('odstęp między mailami: 45 s – 2 min<'));
});

test('prognoza ostrzega, gdy w oknie zmieści się mniej maili niż wynosi dzienny limit', async () => {
  const app = await boot();
  const html = app.el('forecastBox').html;
  assert.ok(html.includes('Średni odstęp między mailami: <b>7 min 45 s</b>'));
  assert.ok(html.includes('ok. <b>55</b> maili dziennie'));
  assert.ok(!html.includes('To mniej niż dzienny limit'), '55 maili wystarcza na limit 50');
  const tight = await boot({preview: {window_mails: 11}});
  assert.ok(tight.el('forecastBox').html.includes('To mniej niż dzienny limit (50) — limit nie zostanie wyczerpany.'));
});

test('czas w opisach: sekundy, minuty i minuty z sekundami', async () => {
  const app = await boot();
  const text = (seconds) => app.eval(`fmtDuration(${seconds})`);
  assert.deepEqual([45, 60, 90, 120, 3600].map(text), ['45 s', '1 min', '1 min 30 s', '2 min', '60 min']);
  assert.equal(app.eval('fmtDuration("abc")'), '—');
});

const warning = (app) => app.el('banners').html || '';

test('demon bez znacznika przedziałów dostaje ostrzeżenie, gdy ustawiono kilka przedziałów', async () => {
  const old = await boot({state: {daemon: {running: true, heartbeat_age: 3, pid: 1, stats: {v: 2}}}});
  assert.ok(warning(old).includes('Demon działa na starszej wersji i nie zna przedziałów odstępu'));
  assert.ok(warning(old).includes('1 min – 60 min'), 'mówi, z jakiego zakresu losuje stary demon');
  const current = await boot({state: {daemon: {running: true, heartbeat_age: 3, pid: 1, stats: {v: 2, delay_bands: true}}}});
  assert.ok(!warning(current).includes('starszej wersji'));
});

test('pojedynczy zakres nie wymaga nowego demona, a zatrzymany demon ma własne ostrzeżenie', async () => {
  const single = await boot({state: {settings: {...SETTINGS, delay_bands: [{min: 45, max: 120, percent: 100}]},
    daemon: {running: true, heartbeat_age: 3, pid: 1, stats: {v: 2}}}});
  assert.ok(!warning(single).includes('starszej wersji'));
  const stopped = await boot({state: {daemon: {running: false, heartbeat_age: null, pid: null, stats: {}}}});
  assert.ok(!warning(stopped).includes('starszej wersji'));
});
