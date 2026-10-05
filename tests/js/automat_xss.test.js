'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Dziennik zdarzeń automatu zawiera nazwy firm z bazy (czasem z cudzych
// stron), a powód pauzy i komunikaty błędów SMTP pochodzą z serwerów pocztowych: nic z tego nie może stać się znacznikiem.
const test = require('node:test');
const assert = require('node:assert/strict');
const {read, EVIL, createPage, attributeNames, settle, vm} = require('./support/dom.js');

const STATE = {
  settings: {daily_cap: 50, weekdays: [0, 1], window_start: '09:00', window_end: '16:00', delay_min: 45, delay_max: 120,
    segment: 'both', countries: [EVIL.attr], horizon_days: 5, max_consecutive_errors: 3},
  settings_ok: true, stamp: EVIL.script, armed: true, armed_at: EVIL.img, armed_until: EVIL.script, paused: true,
  pause_reason: EVIL.script, streak: 1,
  daemon: {running: true, heartbeat_age: 3, pid: 1, stats: {stan: EVIL.img, powod: EVIL.script, do: EVIL.attr}},
  sent_today: 2, now: EVIL.img, launchd: {installed: true, loaded: true, pid: 1},
  power: {on_battery: true, percent: EVIL.script}, channels: ['macos'],
  events: [{ts: EVIL.img, poziom: EVIL.attr, tresc: 'Wysłano: ' + EVIL.script}, {ts: '2026-10-05T10:00:00', poziom: 'crit', tresc: EVIL.img}],
};
const PREVIEW = {
  queue: 3, foreign: 1, no_country: 0, countries: [[EVIL.script, 2], ['_none', 1]], sent_today: 0, send_days: 1,
  last_day: EVIL.img, fits_in_horizon: false, until: EVIL.attr, plan: [[EVIL.script, 3]],
};

function respond(url) {
  if (url.startsWith('/api/automat/status')) return STATE;
  if (url.startsWith('/api/automat/preview')) return PREVIEW;
  return {};
}

async function renderHostilePage() {
  const page = createPage(respond);
  vm.runInContext(read('common.js') + '\n' + read('automat.js'), page.context);
  await settle();
  vm.runInContext('el("confirmBody").innerHTML = confirmSummary();', page.context);
  return page.written.join('\n');
}

test('wrogie dane nie stają się znacznikami ani atrybutami', async () => {
  const html = await renderHostilePage();
  assert.ok(html.includes('&lt;script&gt;'), 'dane w ogóle się wyrenderowały (zeskapowane)');
  assert.ok(html.includes('&lt;img src=x'), 'znaczniki z danych są zeskapowane');
  assert.ok(!/<script/i.test(html), 'surowy <script> w HTML');
  assert.ok(!/<img/i.test(html), 'surowy <img> w HTML');
  assert.deepEqual(attributeNames(html).filter((name) => /^on/i.test(name)), [], 'atrybut zdarzenia (onerror, onmouseover…)');
});

test('poziom zdarzenia z serwera nie wychodzi z atrybutu class', async () => {
  const html = await renderHostilePage();
  assert.ok(!/class="lvl-"\s+onmouseover/i.test(html));
  assert.ok(html.includes('lvl-&quot; onmouseover=&quot;window.PWNED=1&quot; x=&quot;'), 'poziom jest zeskapowany w atrybucie');
});

test('formularz porównuje zapisane i wpisane wartości (nie wysyła nieprzyjętych zmian)', async () => {
  const page = createPage(respond);
  vm.runInContext(read('common.js') + '\n' + read('automat.js'), page.context);
  await settle();
  const dirty = (code) => vm.runInContext(code, page.context);
  assert.equal(dirty('normalized({daily_cap: 50, countries: ["PL"], weekdays: [1, 0]}) === normalized({daily_cap: "50", countries: ["PL"], weekdays: [1, 0]})'), true);
  assert.equal(dirty('normalized({daily_cap: 50, weekdays: [0]}) === normalized({daily_cap: 51, weekdays: [0]})'), false);
});
