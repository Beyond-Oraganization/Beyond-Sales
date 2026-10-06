'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Firma z wieloma adresami na liście: znacznik „2/3”, odznaka wstrzymania
// i akcja „Dodaj pozostałe adresy zaznaczonych firm”. Liczby i status pochodzą z serwera: nic z tego nie może stać się znacznikiem.
const test = require('node:test');
const assert = require('node:assert/strict');
const {read, EVIL, createPage, attributeNames, settle, vm} = require('./support/dom.js');

const STATUS_META = {odpowiedzial: ['Odpowiedział', '#0e7490'], do_wyslania: ['Do wysłania', '#2f6feb']};

function kit() {
  const page = createPage(() => ({}));
  vm.runInContext(['common.js', 'filters.js', 'selection.js', 'listmodel.js', 'kit.js'].map(read).join('\n'), page.context);
  return {page, run: (code) => vm.runInContext(code, page.context)};
}

test('znacznik adresu pokazuje, który to adres z ilu, i tylko przy firmie z wieloma adresami', () => {
  const {run} = kit();
  assert.equal(run('PernoKit.addressMarker({position: "1", mail_count: "1"})'), '');
  assert.equal(run('PernoKit.addressMarker({})'), '');
  const marker = run('PernoKit.addressMarker({position: "2", mail_count: "3"})');
  assert.ok(marker.includes('>2/3<'));
  assert.ok(marker.includes('Adres 2 z 3 tej firmy'));
});

test('kolejny adres to ten z numerem większym niż 1', () => {
  const {run} = kit();
  assert.equal(run('PernoKit.isSibling({position: "1"})'), false);
  assert.equal(run('PernoKit.isSibling({position: "2"})'), true);
  assert.equal(run('PernoKit.isSibling({})'), false);
  assert.equal(run('PernoKit.isSibling({position: "x"})'), false);
});

test('odznaka wstrzymania pokazuje status innego adresu i znika bez blokady', () => {
  const {run} = kit();
  const ctx = `{statusMeta: ${JSON.stringify(STATUS_META)}}`;
  assert.equal(run(`PernoKit.holdBadge({hold: ""}, ${ctx})`), '');
  assert.equal(run(`PernoKit.holdBadge({}, ${ctx})`), '');
  const badge = run(`PernoKit.holdBadge({hold: "odpowiedzial"}, ${ctx})`);
  assert.ok(badge.includes('wstrzymany: Odpowiedział'));
  assert.ok(badge.includes('Inny adres tej firmy ma status „Odpowiedział”'));
});

test('wrogie wartości w znaczniku i odznace nie stają się znacznikami ani atrybutami', () => {
  const {run} = kit();
  const evil = JSON.stringify(EVIL.script);
  const marker = run(`PernoKit.addressMarker({position: ${evil}, mail_count: ${evil}})`);
  assert.equal(marker, '');  // nie liczby: żadnego znacznika
  const meta = JSON.stringify({[EVIL.attr]: [EVIL.script, '#000']});
  const badge = run(`PernoKit.holdBadge({hold: ${JSON.stringify(EVIL.attr)}}, {statusMeta: ${meta}})`);
  assert.ok(!/<script/i.test(badge));
  assert.deepEqual(attributeNames(badge).filter((name) => /^on/i.test(name)), []);
  assert.ok(badge.includes('&lt;script&gt;'));
  const unknown = run(`PernoKit.holdBadge({hold: ${JSON.stringify(EVIL.img)}}, {statusMeta: {}})`);
  assert.ok(!/<img/i.test(unknown));
});

test('widok listy wystawia pobranie adresów firm zaznaczonych adresów', async () => {
  const page = createPage((url) => (url.startsWith('/api/mailing/siblings') ? {ids: ['a', 'a~2'], truncated: false} : {rows: [], total: 0, page: 1, pages: 1, size: 100}),
    {setTimeout: (fn) => { setImmediate(fn); return 1; }});
  vm.runInContext(['common.js', 'filters.js', 'selection.js', 'listmodel.js', 'kit.js'].map(read).join('\n') + `
    const ctx = PernoFilters.createContext({statusMeta: {}, sendable: [], countries: []});
    const selection = PernoSelection.create(); selection.add(['a']);
    const el = (id) => document.getElementById(id);
    const view = PernoKit.createListView({key: 't', ctx, selection, defaultSize: 100, filtersEl: el('f'), tableEl: el('t'), tbodyEl: el('b'),
      pagerEl: el('p'), selectEl: el('s'), emptyEl: el('e'), cells: () => ''});
  `, page.context);
  await settle();
  const found = await vm.runInContext('view.siblingIds()', page.context);
  assert.deepEqual(JSON.parse(JSON.stringify(found)), {ids: ['a', 'a~2'], truncated: false});
});
