'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Zakładka Automat przy firmach z wieloma adresami: liczba firm,
// maile odroczone przez odstęp i wstrzymane przez status innego adresu, wpływ odstępu na prognozę, pole w formularzu.
const test = require('node:test');
const assert = require('node:assert/strict');
const {read, createPage, settle, vm} = require('./support/dom.js');

const SETTINGS = {daily_cap: 50, weekdays: [0, 1, 2, 3, 4], window_start: '09:00', window_end: '16:00', delay_min: 45, delay_max: 120,
  segment: 'both', countries: [], horizon_days: 5, max_consecutive_errors: 3, company_gap_days: 2};
const STATE = {settings: SETTINGS, settings_ok: true, stamp: '2026-10-05T10:00:00', armed: false, armed_at: '', armed_until: '', paused: false,
  pause_reason: '', streak: 0, daemon: {running: true, heartbeat_age: 3, pid: 1, stats: {stan: 'wait', powod: 'company_gap', do: '2026-10-06T09:00:00'}},
  sent_today: 2, now: '2026-10-05T11:00:00', launchd: {installed: true, loaded: true, pid: 1}, power: {on_battery: false, percent: 100},
  channels: ['macos', 'ntfy'], events: []};
const PREVIEW = {queue: 5, companies: 3, held: 2, deferred_today: 1, gap_days: 2, limited_by_gap: true, foreign: 0, no_country: 0,
  countries: [['PL', 5]], sent_today: 2, send_days: 3, last_day: '2026-10-09', fits_in_horizon: true, until: '2026-10-10T00:00:00',
  plan: [['2026-10-06', 2], ['2026-10-07', 2]]};

const clone = (value) => JSON.parse(JSON.stringify(value));  // strona zmienia dane, które dostaje; testy nie mogą sobie przeszkadzać

async function load(preview) {
  const page = createPage((url) => clone(url.startsWith('/api/automat/status') ? STATE : url.startsWith('/api/automat/preview') ? preview : {}));
  vm.runInContext(read('common.js') + '\n' + read('automat.js'), page.context);
  await settle();
  return page;
}

test('prognoza pokazuje firmy, maile odroczone i wstrzymane oraz wpływ odstępu', async () => {
  const html = (await load(PREVIEW)).written.join('\n');
  assert.ok(html.includes('w <b>3</b> firmach'));
  assert.ok(html.includes('Dziś czeka na odstęp dla firmy: <b>1</b>'));
  assert.ok(html.includes('Wstrzymane przez status innego adresu firmy'));
  assert.ok(html.includes('<b>2</b> maili — nie wejdą do kolejki'));
  assert.ok(html.includes('Odstęp dla firmy (2 dn.) wydłuża plan.'));
});

test('bez firm z wieloma adresami prognoza wygląda jak dawniej', async () => {
  const plain = {...PREVIEW, companies: 5, held: 0, deferred_today: 0, limited_by_gap: false};
  const html = (await load(plain)).written.join('\n');
  assert.ok(!html.includes('firmach'));
  assert.ok(!html.includes('czeka na odstęp'));
  assert.ok(!html.includes('Wstrzymane przez'));
  assert.ok(!html.includes('wydłuża plan'));
  assert.ok(html.includes('W kolejce: <b>5</b> maili'));
});

test('powód „czeka na odstęp dla firmy” ma opis po polsku w stanie planu', async () => {
  const page = await load(PREVIEW);
  const text = vm.runInContext('statusText({armed: true, paused: false, daemon: {stats: {stan: "wait", powod: "company_gap", do: "2026-10-06T09:00:00"}}})', page.context);
  assert.equal(text, 'czeka na odstęp dla firmy (następna wysyłka 10-06 09:00)');
});

test('okno potwierdzenia Startu pokazuje odstęp dla firmy i liczbę firm', async () => {
  const page = await load(PREVIEW);
  const summary = vm.runInContext('confirmSummary()', page.context);
  assert.ok(summary.includes('odstęp dla firmy: 2 dn. między mailami do jednej firmy'));
  assert.ok(summary.includes('w kolejce: 5 maili (3 firm)'));
  const none = vm.runInContext('STATE.settings.company_gap_days = 0; confirmSummary()', page.context);
  assert.ok(none.includes('odstęp dla firmy: brak'));
});

test('pole odstępu jest częścią formularza i zmiana go oznacza niezapisane limity', async () => {
  const page = await load(PREVIEW);
  assert.equal(vm.runInContext('FIELDS.includes("company_gap_days")', page.context), true);
  assert.equal(vm.runInContext('el("f_company_gap_days").value', page.context), 2);
  assert.equal(vm.runInContext('readForm().company_gap_days', page.context), 2);
  assert.equal(vm.runInContext('normalized({company_gap_days: 1}) === normalized({company_gap_days: "1"})', page.context), true);
  assert.equal(vm.runInContext('normalized({company_gap_days: 1}) === normalized({company_gap_days: 2})', page.context), false);
});
