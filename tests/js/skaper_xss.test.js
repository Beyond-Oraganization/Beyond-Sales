'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Wrogie dane z zeskanowanych stron (tytuły, adresy, treść robots.txt,
// komunikaty serwerów) nie mogą stać się znacznikami ani atrybutami w zakładce Skraper. Skrypt strony działa bez zmian
// w piaskownicy `vm` z atrapą DOM, a my zbieramy każdy HTML, który zapisał.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ASSETS = path.join(__dirname, '../../src/perno/web/pages/assets');
const read = (name) => fs.readFileSync(path.join(ASSETS, name), 'utf8');

const EVIL = {
  script: '<script>window.PWNED=1</script>',
  img: '<img src=x onerror=window.PWNED=1>',
  attr: '" onmouseover="window.PWNED=1" x="',
  js: 'javascript:window.PWNED=1',
};

const META = {
  statuses: [{code: 'zakonczona', label: 'Zakończona', color: '#1a7f4b', count: 1}],
  defaults: {}, limits: {}, floor: 1, contact: {ok: true, value: 'ops@example.com', source: 'config'},
  settings_error: EVIL.script, bs4_ok: true, maile_count: 1, firmy_count: 1,
  default_statuses: [], retryable: ['blad'], reasons: {},
  profile: EVIL.attr,
  profiles: [{code: EVIL.attr, label: EVIL.script, values: {depth: 1, max_pages: 1, max_mb: 1, max_minutes: 1}}],
};
const ROW = {
  id: 1, domena: EVIL.img, website: EVIL.js, nazwa: EVIL.script, skan_status: 'zakonczona', skan_przyczyna: EVIL.attr,
  ostatni_blad: EVIL.attr, stron_pobranych: 3, bajty_pobranych: 1048576, glebokosc_max: 2, emaile: 1, telefony: 0,
  skan_end_at: EVIL.img, domena_docelowa: EVIL.script, nastepna_proba_po: null,
};
const STATUS = {
  aktywny: false, heartbeat_age: null, nieodpowiada: false, przeglad: {},
  run: {id: 7, status: EVIL.img, start_at: EVIL.script, end_at: EVIL.script, stop_requested: false,
    statystyki: {firmy_wynik: {[EVIL.img]: 2}}},
  log: [EVIL.script, EVIL.img],
};
const CONTACT = {w_domenie: true, liczba_stron: 1, rola: EVIL.img, zrodlo_typ: EVIL.attr, zrodlo_url: EVIL.js};
const DETAIL = {
  firma: {website: EVIL.js, skan_przyczyna: EVIL.attr, ostatni_blad: EVIL.script, domena_docelowa: EVIL.img,
    nastepna_proba_po: EVIL.script, robots_status: EVIL.img, crawl_delay: EVIL.script, robots_txt: EVIL.script,
    'robots_obcięty': true},
  strony: [{url: EVIL.js, status: EVIL.img, http_status: 200, tytul: EVIL.script, blad: EVIL.attr}],
  strony_razem: 1,
  kontakty: [
    {...CONTACT, typ: 'email', wartosc: EVIL.script},
    {...CONTACT, typ: 'social', wartosc: EVIL.js},
    {...CONTACT, typ: 'formularz', wartosc: EVIL.attr},
    {...CONTACT, typ: 'telefon', wartosc: EVIL.img},
  ],
};

function respond(url) {
  if (url.startsWith('/api/skaper/meta')) return META;
  if (url.startsWith('/api/skaper/firmy')) return {rows: [ROW], total: 1};
  if (url.startsWith('/api/skaper/status')) return STATUS;
  if (url.startsWith('/api/skaper/firma?')) return DETAIL;
  return {};
}

// Atrapa DOM: każdy element to Proxy, które zapamiętuje HTML zapisany przez innerHTML / insertAdjacentHTML.
function createPage() {
  const written = [];
  const elements = new Map();
  const makeElement = () => {
    const base = {
      dataset: {}, style: {}, classList: {add() {}, remove() {}, toggle() {}}, value: '', checked: false,
      disabled: false, textContent: '', className: '', children: [],
    };
    return new Proxy(base, {
      get(target, prop) {
        if (prop === 'insertAdjacentHTML') return (_where, html) => { written.push(String(html)); };
        if (prop in target) return target[prop];
        if (typeof prop === 'symbol') return undefined;
        return () => makeElement();  // appendChild(), remove(), addEventListener(), closest() …
      },
      set(target, prop, value) {
        if (prop === 'innerHTML' || prop === 'outerHTML') written.push(String(value));
        target[prop] = value;
        return true;
      },
    });
  };
  const cached = (key) => { if (!elements.has(key)) elements.set(key, makeElement()); return elements.get(key); };
  const document = {
    getElementById: (id) => cached('#' + id),
    querySelector: (selector) => cached(selector),
    createElement: () => makeElement(),
    addEventListener() {},
    body: makeElement(),
  };
  const sandbox = {
    document, console, URLSearchParams, JSON, Math, Number, String, Set, Map, Object, Array, Promise,
    setTimeout: () => 0, clearTimeout() {}, confirm: () => false,
    fetch: async (url) => ({ok: true, status: 200, json: async () => respond(String(url))}),
  };
  return {written, context: vm.createContext(sandbox)};
}


// Nazwy atrybutów prawdziwych znaczników (z pominięciem tego, co siedzi w cudzysłowach wartości): zeskapowany tekst
// `" onmouseover="…` wewnątrz title="…" nie jest atrybutem, a gdyby ucieczka zawiodła, byłby nim.
const TAG = /<([a-z][a-z0-9-]*)((?:\s+[^\s"'<>\/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*)\s*\/?>/gi;
const ATTRIBUTE = /([^\s"'<>\/=]+)(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?/g;
function attributeNames(html) {
  const names = [];
  for (const tag of html.matchAll(TAG)) {
    for (const attribute of tag[2].matchAll(ATTRIBUTE)) names.push(attribute[1]);
  }
  return names;
}

async function settle() {
  for (let i = 0; i < 30; i++) await new Promise((resolve) => setImmediate(resolve));
}

async function renderHostilePage() {
  const page = createPage();
  vm.runInContext(read('common.js') + '\n' + read('skaper.js'), page.context);
  await settle();
  vm.runInContext('expandedId = 1; renderRows();', page.context);  // rozwija szczegóły firmy (loadDetail)
  await settle();
  return page.written.join('\n');
}

test('wrogie dane nie stają się znacznikami ani atrybutami', async () => {
  const html = await renderHostilePage();
  assert.ok(html.includes('&lt;script&gt;'), 'dane w ogóle się wyrenderowały (zeskapowane)');
  assert.ok(html.includes('&lt;img src=x'), 'znaczniki z danych są zeskapowane');
  assert.ok(html.includes('<option value="&quot; onmouseover=&quot;window.PWNED=1&quot; x=&quot;">&lt;script&gt;'),
    'profil w liście wyboru jest zeskapowany');
  assert.ok(!/<script/i.test(html), 'surowy <script> w HTML');
  assert.ok(!/<img/i.test(html), 'surowy <img> w HTML');
  assert.deepEqual(attributeNames(html).filter((name) => /^on/i.test(name)), [], 'atrybut zdarzenia (onerror, onmouseover…)');
});

test('adresy nie-http(s) nie trafiają do href', async () => {
  const html = await renderHostilePage();
  assert.ok(!/href\s*=\s*["']?\s*javascript:/i.test(html), 'javascript: w href');
  assert.ok(html.includes('href=""'), 'niebezpieczny adres został zastąpiony pustym href');
});

test('bezpieczne adresy http(s) dostają rel=noopener', async () => {
  const page = createPage();
  vm.runInContext(read('common.js') + '\n' + read('skaper.js'), page.context);
  await settle();
  const link = vm.runInContext("linkTo('https://firma.example/kontakt?a=1&b=2', 'źródło')", page.context);
  assert.equal(link, '<a href="https://firma.example/kontakt?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">źródło</a>');
  assert.equal(vm.runInContext("linkTo('javascript:alert(1)', 'x')", page.context), 'x');
  assert.equal(vm.runInContext("linkTo('data:text/html,<script>', 'x')", page.context), 'x');
});

test('escapeHtml: pięć znaków specjalnych i wartości puste', () => {
  const page = createPage();
  vm.runInContext(read('common.js'), page.context);
  const escape = (value) => vm.runInContext(`escapeHtml(${JSON.stringify(value)})`, page.context);
  assert.equal(escape('<a href="x">\'&\'</a>'), '&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;');
  assert.equal(vm.runInContext('escapeHtml(null)', page.context), '');
  assert.equal(vm.runInContext('escapeHtml(undefined)', page.context), '');
});
