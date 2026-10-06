'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Rozwinięty wiersz strony Wysyłka pokazuje adresy firmy (z bazy, więc
// także z cudzych stron) i pozwala je dodać lub usunąć: wrogie wartości nie mogą stać się znacznikami, a wywołania idą pod
// właściwe adresy API z właściwym ładunkiem.
const test = require('node:test');
const assert = require('node:assert/strict');
const {read, EVIL, createPage, attributeNames, settle, vm} = require('./support/dom.js');

const APP = {
  statusMeta: {do_wyslania: ['Do wysłania', '#2f6feb'], wyslano: ['Wysłano', '#1a7f4b']}, manualStatuses: ['do_wyslania', 'wyslano'],
  sendable: ['do_wyslania'], countries: [['PL', 'Polska', '🇵🇱']], segmentOrder: ['eventy'], keepsSentAt: ['wyslano'],
};
const ROW = {id: 'eventy-001', company: 'Firma A', email: 'a@firma.pl', status: 'wyslano', segment: 'eventy', has_body: true,
  company_id: 'eventy-001', position: '1', mail_count: '2', hold: '', country: 'PL', city: '', website: ''};
const MAILS = [
  {id: 'eventy-001', position: '1', email: 'a@firma.pl', status: 'wyslano', subject: 'T', sent_at: '2026-10-01T10:00:00', deletable: false},
  {id: 'eventy-001~2', position: '2', email: EVIL.script, status: EVIL.attr, subject: EVIL.img, sent_at: EVIL.attr, deletable: true},
];

function boot() {
  const calls = [];
  const respond = (url) => {
    if (url.startsWith('/api/mailing/list')) return {rows: [ROW], total: 1, page: 1, pages: 1, size: 100, overview: {statuses: {}, segments: {}, countries: {}},
      totals: {statuses: {}, segments: {}, countries: {}, all: 1}, presets: Array(12).fill(0)};
    if (url.startsWith('/api/maile/row')) return {row: {...ROW, body_html: '<p>x</p>', body_text: 'x', error: '', description: ''}, mails: MAILS};
    if (url.startsWith('/api/bulk-log')) return {operations: [], limit: 20};
    if (url.startsWith('/api/status')) return {smtp_configured: true, smtp_user: 'x', sent_today: 0};
    if (url.startsWith('/api/mailing/mails')) { return {row: ROW, mails: MAILS, warnings: []}; }
    return {};
  };
  const page = createPage(respond, {
    setTimeout: (fn) => { setImmediate(fn); return 1; }, APP,
    fetch: async (url, init) => {
      if (init && init.body) calls.push({url: String(url), body: JSON.parse(init.body)});
      return {ok: true, status: 200, json: async () => respond(String(url))};
    },
  });
  const scripts = ['common.js', 'filters.js', 'selection.js', 'listmodel.js', 'kit.js', 'bulk.js', 'wysylka.js'].map(read).join('\n');
  vm.runInContext(scripts, page.context);
  return {page, calls, run: (code) => vm.runInContext(code, page.context)};
}

async function expanded() {
  const env = boot();
  await settle();
  env.run("expandedId = 'eventy-001'; view.redraw();");
  await settle();
  return env;
}

test('adresy firmy w rozwiniętym wierszu: wrogie dane są zeskapowane', async () => {
  const env = await expanded();
  const html = env.page.written.join('\n');
  assert.ok(html.includes('Adresy tej firmy (2)'));
  assert.ok(html.includes('&lt;script&gt;'), 'adres z danych jest widoczny jako tekst');
  assert.ok(!/<script/i.test(html) && !/<img/i.test(html));
  assert.deepEqual(attributeNames(html).filter((name) => /^on/i.test(name)), []);
});

test('przycisk Usuń tylko przy adresie, który serwer uznał za usuwalny', async () => {
  const env = await expanded();
  const html = env.page.written.join('\n');
  assert.equal((html.match(/data-delete=/g) || []).length, 1);
  assert.ok(html.includes('data-delete="eventy-001~2"'));
});

test('dodanie i usunięcie adresu wołają serwer z właściwym ładunkiem', async () => {
  const env = await expanded();
  env.page.context.__td = {querySelector: (selector) => selector === '[data-new-email]' ? {value: '  nowy@firma.pl ', blur() {}} : {checked: false}};
  await env.run('addAddress(detailCache["eventy-001"], __td)');
  await settle();
  const add = env.calls.find((call) => call.url === '/api/mailing/mails');
  assert.deepEqual(add.body, {company_id: 'eventy-001', email: 'nowy@firma.pl', copy_text: false});
  env.page.context.showSheet = async () => true;
  await env.run('deleteAddress(detailCache["eventy-001"] || {id: "eventy-001"}, "eventy-001~2")');
  await settle();
  const removal = env.calls.find((call) => call.url === '/api/mailing/mails/delete');
  assert.deepEqual(removal.body, {id: 'eventy-001~2'});
});

test('pusty adres nie idzie do serwera', async () => {
  const env = await expanded();
  env.page.context.__td = {querySelector: (selector) => selector === '[data-new-email]' ? {value: '   ', blur() {}} : {checked: true}};
  await env.run('addAddress(detailCache["eventy-001"] || {id: "eventy-001", company_id: "eventy-001"}, __td)');
  assert.equal(env.calls.filter((call) => call.url === '/api/mailing/mails').length, 0);
});
