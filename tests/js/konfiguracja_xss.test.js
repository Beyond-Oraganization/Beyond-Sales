'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Wartości z config.env (nazwa nadawcy, host, kontakt) oraz teksty
// z serwera nie mogą stać się znacznikami ani atrybutami w formularzu konfiguracji, a wartość sekretu nigdy nie trafia do DOM.
const test = require('node:test');
const assert = require('node:assert/strict');
const {read, EVIL, createPage, attributeNames, settle, vm} = require('./support/dom.js');

const CONFIG = {
  file_exists: false,
  groups: [{
    key: 'mail', title: EVIL.script, description: EVIL.img, fields: [
      {key: 'SMTP_HOST', label: EVIL.img, kind: 'text', help: EVIL.script, default: EVIL.attr, value: EVIL.attr, set: true, choices: [], restart: ''},
      {key: 'SENDER_NAME', label: 'Nazwa', kind: 'text', help: '', default: '', value: EVIL.script, set: true, choices: [], restart: 'demon'},
      {key: 'SMTP_PASSWORD', label: 'Hasło', kind: 'secret', help: EVIL.script, default: '', set: true, choices: [], restart: ''},
      {key: 'SKRAPER_PROFILE', label: 'Profil', kind: 'choice', help: '', default: 'kontekst', value: EVIL.attr, set: true, choices: [EVIL.attr, 'kontekst'], restart: ''},
      {key: 'SMTP_USE_SSL', label: 'SSL', kind: 'bool', help: '', default: 'true', value: '', set: false, choices: [], restart: ''},
    ],
  }],
};

function respond(url) {
  if (url.startsWith('/api/config')) return CONFIG;
  return {};
}

async function renderHostilePage() {
  const page = createPage(respond);
  vm.runInContext(read('common.js') + '\n' + read('konfiguracja.js'), page.context);
  await settle();
  return {html: page.written.join('\n'), page};
}

test('wrogie dane nie stają się znacznikami ani atrybutami', async () => {
  const {html} = await renderHostilePage();
  assert.ok(html.includes('&lt;script&gt;'), 'dane w ogóle się wyrenderowały (zeskapowane)');
  assert.ok(html.includes('&lt;img src=x'), 'znaczniki z danych są zeskapowane');
  assert.ok(!/<script/i.test(html), 'surowy <script> w HTML');
  assert.ok(!/<img/i.test(html), 'surowy <img> w HTML');
  assert.deepEqual(attributeNames(html).filter((name) => /^on/i.test(name)), [], 'atrybut zdarzenia (onerror, onmouseover…)');
});

test('pole sekretu nie ma wartości, tylko podpowiedź o stanie', async () => {
  const {html} = await renderHostilePage();
  assert.match(html, /type="password" id="c_SMTP_PASSWORD" autocomplete="new-password" placeholder="ustawione/);
  assert.ok(!/id="c_SMTP_PASSWORD"[^>]*\bvalue=/.test(html), 'pole hasła nie może mieć atrybutu value');
});

function fill(page, values) {
  // atrapa DOM zwraca dla tego samego id ten sam obiekt, więc ustawiamy wartości pól tak, jak zrobiłby to człowiek
  vm.runInContext(`for (const [id, patch] of Object.entries(${JSON.stringify(values)})) Object.assign(document.getElementById(id), patch);`, page.context);
  return JSON.parse(vm.runInContext('JSON.stringify(collect())', page.context));
}

const UNTOUCHED = {c_SMTP_HOST: {value: EVIL.attr}, c_SENDER_NAME: {value: EVIL.script}, c_SMTP_PASSWORD: {value: ''},
  c_SKRAPER_PROFILE: {value: EVIL.attr}, c_SMTP_USE_SSL: {value: 'true'}};

test('bez zmian nic nie jest wysyłane', async () => {
  const {page} = await renderHostilePage();
  assert.deepEqual(fill(page, UNTOUCHED), {values: {}, clear: []});
});

test('wysyłamy tylko zmienione pola, a puste pole sekretu nie jest zmianą', async () => {
  const {page} = await renderHostilePage();
  const result = fill(page, {...UNTOUCHED, c_SENDER_NAME: {value: 'Nowy'}, x_SMTP_PASSWORD: {checked: true}});
  assert.deepEqual(result, {values: {SENDER_NAME: 'Nowy'}, clear: ['SMTP_PASSWORD']});
});

test('niepusty sekret trafia do values', async () => {
  const {page} = await renderHostilePage();
  assert.deepEqual(fill(page, {...UNTOUCHED, c_SMTP_PASSWORD: {value: 'nowe-haslo'}}), {values: {SMTP_PASSWORD: 'nowe-haslo'}, clear: []});
});

test('wyczyszczenie zwykłego pola jest zmianą (wraca domyślne)', async () => {
  const {page} = await renderHostilePage();
  assert.deepEqual(fill(page, {...UNTOUCHED, c_SMTP_HOST: {value: ''}}), {values: {SMTP_HOST: ''}, clear: []});
});
