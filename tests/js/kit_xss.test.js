'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Wspólny interfejs list (kit.js + listmodel.js) dostaje od serwera
// wiersze, liczniki i podsumowania, w których siedzą dane firm (nazwy, segmenty, kraje): wrogie wartości nie mogą stać się
// znacznikami ani atrybutami. Skrypty działają bez zmian w piaskownicy `vm` z atrapą DOM (tests/js/support/dom.js).
const test = require('node:test');
const assert = require('node:assert/strict');
const {read, EVIL, createPage, attributeNames, settle, vm} = require('./support/dom.js');

const HOSTILE_FACETS = {
  statuses: {[EVIL.attr]: 1, do_wyslania: 2},
  segments: {[EVIL.script]: 3, eventy: 1},
  countries: {[EVIL.img]: 1, PL: 2, _foreign: 1, _none: 0},
};
const ROW = {
  id: EVIL.attr, company: EVIL.script, email: EVIL.img, website: EVIL.js, status: 'do_wyslania', country: EVIL.attr,
  city: EVIL.script, segment: EVIL.img, has_body: true,
};

function respond(url) {
  if (url.startsWith('/api/mailing/list')) {
    return {
      rows: [ROW], total: 1, page: 1, pages: 1, size: 100, overview: HOSTILE_FACETS,
      totals: {...HOSTILE_FACETS, all: 5}, presets: Array(12).fill(1),
    };
  }
  if (url.startsWith('/api/mailing/selection')) return {count: 1, ready: 1, foreign: {[EVIL.img]: 1}, in_view: 0};
  if (url.startsWith('/api/mailing/ids')) return {ids: [EVIL.attr], truncated: false};
  return {};
}

const PAGE_SCRIPT = `
  const ctx = PernoFilters.createContext({
    statusMeta: {do_wyslania: ['Do wysłania', '#2f6feb']}, sendable: ['do_wyslania'], countries: [['PL', 'Polska', '🇵🇱']],
  });
  const selection = PernoSelection.create();
  const el = (id) => document.getElementById(id);
  const view = PernoKit.createListView({
    key: 'test', ctx, selection, defaultSize: 100,
    filtersEl: el('filters'), tableEl: el('table'), tbodyEl: el('body'), pagerEl: el('pager'), selectEl: el('select'),
    emptyEl: el('empty'),
    cells: (row) => '<td>' + escapeHtml(row.company) + '</td>' + '<td>' + PernoKit.countryCell(row, ctx) + '</td>',
    selectItems: [{label: 'Zaznacz gotowe', run: async ({ids}) => selection.add((await ids({ready: true})).ids)}],
  });
`;

async function renderHostileList() {
  // zegar uruchamia zadanie zaraz po bieżącym: model sklejanych żądań potrzebuje timera, żeby wysłać pierwsze zapytanie
  const page = createPage(respond, {setTimeout: (fn) => { setImmediate(fn); return 1; }});
  const scripts = ['common.js', 'filters.js', 'selection.js', 'listmodel.js', 'kit.js'].map(read).join('\n');
  vm.runInContext(scripts + '\n' + PAGE_SCRIPT, page.context);
  vm.runInContext('view.start();', page.context);
  await settle();
  // zaznaczenie wiersza o wrogim id uruchamia podsumowanie z serwera i odrysowanie paska zaznaczania
  vm.runInContext(`selection.add([${JSON.stringify(ROW.id)}]); view.selectionChanged();`, page.context);
  await settle();
  return page.written.join('\n');
}

test('wrogie dane z serwera nie stają się znacznikami ani atrybutami w liście', async () => {
  const html = await renderHostileList();
  assert.ok(html.includes('&lt;script&gt;'), 'dane w ogóle się wyrenderowały (zeskapowane)');
  assert.ok(html.includes('&lt;img src=x'), 'znaczniki z danych są zeskapowane');
  assert.ok(!/<script/i.test(html), 'surowy <script> w HTML');
  assert.ok(!/<img/i.test(html), 'surowy <img> w HTML');
  assert.deepEqual(attributeNames(html).filter((name) => /^on/i.test(name)), [], 'atrybut zdarzenia (onerror, onmouseover…)');
});

test('wartości segmentów, krajów i statusów w polach formularza są zeskapowane', async () => {
  const html = await renderHostileList();
  const hostile = '&quot; onmouseover=&quot;window.PWNED=1&quot; x=&quot;';
  assert.ok(html.includes(`data-status="${hostile}"`), 'chip statusu');
  assert.ok(html.includes(`data-value="&lt;script&gt;window.PWNED=1&lt;/script&gt;"`), 'wartość segmentu w oknie wyboru');
  assert.ok(html.includes(`data-id="${hostile}"`), 'id wiersza w checkboxie');
});

test('lista rysuje liczniki z serwera: szybkie widoki, chipy i „x z y”', async () => {
  const html = await renderHostileList();
  assert.ok(/Wszystkie<span class="n">1<\/span>/.test(html), 'szybki widok z licznikiem');
  assert.ok(/Do wysłania<span class="n">2<\/span>/.test(html), 'chip statusu z licznikiem');
  assert.ok(html.includes('<span class="kit-count">1 <span class="muted">z 5</span></span>'), 'licznik widoku');
});
