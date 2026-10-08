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

const NEW_CARD = {
  key: 'mail2', mailbox: 2, title: EVIL.script, description: EVIL.img, fields: [
    {key: 'SMTP2_HOST', label: EVIL.img, kind: 'text', help: EVIL.script, default: EVIL.attr, value: '', set: false, choices: [], restart: ''},
    {key: 'SMTP2_PASSWORD', label: 'Hasło', kind: 'secret', help: '', default: '', set: false, choices: [], restart: ''},
  ],
};

async function pageWithAddedMailbox() {
  const requested = [];
  const page = createPage((url) => {
    requested.push(url);
    return url.startsWith('/api/config/skrzynka') ? NEW_CARD : CONFIG;
  });
  vm.runInContext(read('common.js') + '\n' + read('konfiguracja.js'), page.context);
  await settle();
  await vm.runInContext('addMailbox()', page.context);
  await settle();
  return {page, requested};
}

test('dodanie skrzynki prosi serwer o kartę kolejnego numeru i nie wpuszcza znaczników', async () => {
  const {page, requested} = await pageWithAddedMailbox();
  assert.ok(requested.includes('/api/config/skrzynka?slot=2'), requested.join(', '));
  const html = page.written.join('\n');
  assert.ok(html.includes('id="card_mail2"'), 'karta nowej skrzynki jest w DOM');
  assert.ok(!/<script/i.test(html) && !/<img/i.test(html), 'surowy znacznik z danych serwera');
  assert.deepEqual(attributeNames(html).filter((name) => /^on/i.test(name)), []);
});

test('nowa skrzynka wysyła tylko to, co człowiek wpisał, a usunięta przed zapisem znika bez śladu', async () => {
  const {page} = await pageWithAddedMailbox();
  const typed = fill(page, {...UNTOUCHED, c_SMTP2_HOST: {value: 'smtp2.example.com'}, c_SMTP2_PASSWORD: {value: 'haslo-drugie'}});
  assert.deepEqual(typed, {values: {SMTP2_HOST: 'smtp2.example.com', SMTP2_PASSWORD: 'haslo-drugie'}, clear: []});
  vm.runInContext("removeMailbox('2')", page.context);
  assert.deepEqual(fill(page, {...UNTOUCHED, c_SMTP2_HOST: {value: ''}, c_SMTP2_PASSWORD: {value: ''}}), {values: {}, clear: []});
});

function mailboxCard(slot, extra) {
  const key = (name) => (slot === 1 ? name : name.replace('_', slot + '_'));
  const field = (name, kind, value) => ({key: key(name), label: name, kind: kind || 'text', help: '', default: '', value: value || '', set: false, choices: [], restart: ''});
  return {key: slot === 1 ? 'mail' : 'mail' + slot, mailbox: slot, title: 'Skrzynka ' + slot, description: '', fields: [
    field('SMTP_HOST', 'text', extra && extra.host), field('SMTP_PORT', 'text', '465'), field('SMTP_USE_SSL', 'bool', 'true'),
    field('SMTP_USER', 'text', extra && extra.user), field('SMTP_PASSWORD', 'secret'), field('IMAP_HOST', 'text', extra && extra.imap),
    field('IMAP_PORT', 'text'), field('IMAP_USE_SSL', 'bool', 'true'), field('IMAP_USER', 'text'), field('IMAP_PASSWORD', 'secret'),
  ]};
}

async function pageWithMailboxes(count) {
  const cards = [];
  for (let slot = 1; slot <= count; slot++) cards.push(mailboxCard(slot, {user: 'box' + slot + '@x.pl', imap: slot % 2 ? 'imap.x.pl' : '', host: 'smtp.zenbox.pl'}));
  const requested = [];
  const page = createPage((url) => {
    requested.push(url);
    const asked = /slot=(\d+)/.exec(url);
    if (asked) return mailboxCard(Number(asked[1]));
    return {file_exists: true, groups: cards};
  });
  vm.runInContext(read('common.js') + '\n' + read('konfiguracja.js'), page.context);
  await settle();
  return {page, requested};
}

const run = (page, code) => vm.runInContext(code, page.context);

test('wklejone linie: adres;hasło, hasło może zawierać średnik, złe linie są nazwane', async () => {
  const {page} = await pageWithMailboxes(1);
  const parsed = JSON.parse(run(page, `JSON.stringify(parseMailboxLines("a@x.pl;h1\\n\\n  b@x.pl\\ttab;haslo ze ; srednikiem  \\nbez-malpy;h\\nc@x.pl;\\nd@x.pl"))`));
  assert.deepEqual(parsed.entries, [{user: 'a@x.pl', password: 'h1'}, {user: 'b@x.pl', password: 'tab;haslo ze ; srednikiem'}]);
  assert.deepEqual(parsed.problems, ['linia 4: oczekiwano „adres;hasło”', 'linia 5: oczekiwano „adres;hasło”', 'linia 6: oczekiwano „adres;hasło”']);
});

test('naraz najwyżej 200 skrzynek', async () => {
  const {page} = await pageWithMailboxes(1);
  const many = JSON.parse(run(page, `JSON.stringify(parseMailboxLines(Array.from({length: 201}, (_, i) => 'u' + i + '@x.pl;h').join('\\n')))`));
  assert.equal(many.entries.length, 201);
  assert.deepEqual(many.problems, ['najwyżej 200 skrzynek naraz']);
});

test('dodanie wielu skrzynek tworzy karty po kolei i wpisuje adres oraz hasło', async () => {
  const {page, requested} = await pageWithMailboxes(1);
  run(page, `document.getElementById('c_SMTP_HOST').value = 'smtp.zenbox.pl'; document.getElementById('c_IMAP_HOST').value = 'imap.zenbox.pl'`);
  run(page, `document.getElementById('bulkMailboxText').value = 'a@x.pl;h1\\nb@x.pl;h2\\nc@x.pl;h3'`);
  assert.equal(await run(page, 'addManyMailboxes()'), true);
  await settle();
  assert.deepEqual(requested.filter((url) => url.includes('slot=')), ['/api/config/skrzynka?slot=2', '/api/config/skrzynka?slot=3', '/api/config/skrzynka?slot=4']);
  const result = JSON.parse(run(page, 'JSON.stringify(collect())'));
  assert.equal(result.values.SMTP2_USER, 'a@x.pl');
  assert.equal(result.values.SMTP3_PASSWORD, 'h2');
  assert.equal(result.values.SMTP4_USER, 'c@x.pl');
  assert.equal(result.values.SMTP2_HOST, 'smtp.zenbox.pl', 'serwer SMTP podpowiedziany ze skrzynki głównej');
  assert.equal(result.values.IMAP4_HOST, 'imap.zenbox.pl', 'serwer IMAP też');
  assert.equal(result.values.IMAP3_USER, undefined, 'login IMAP zostaje pusty (wtedy jak w SMTP)');
});

test('niepoprawna linia zatrzymuje dodawanie i nic nie jest zgłaszane do serwera', async () => {
  const {page, requested} = await pageWithMailboxes(1);
  run(page, `document.getElementById('bulkMailboxText').value = 'a@x.pl;h1\\nbez-malpy;h2'`);
  assert.equal(await run(page, 'addManyMailboxes()'), false);
  assert.equal(requested.filter((url) => url.includes('slot=')).length, 0);
  assert.match(run(page, "document.getElementById('bulkMailboxProblems').textContent"), /linia 2/);
});

test('od czterech skrzynek dodatkowe karty są zwinięte, a główna nie', async () => {
  const few = await pageWithMailboxes(3);
  assert.ok(!few.page.written.join('\n').includes('is-collapsed'), 'trzy skrzynki mieszczą się rozwinięte');
  const many = await pageWithMailboxes(4);
  const html = many.page.written.join('\n');
  assert.ok(/id="card_mail"/.test(html) && !/class="card is-collapsed" id="card_mail"/.test(html), 'główna zostaje rozwinięta');
  for (const key of ['mail2', 'mail3', 'mail4']) assert.ok(html.includes('class="card is-collapsed" id="card_' + key + '"'), key);
  assert.ok(html.includes('aria-expanded="false"'));
});

test('wyszukiwarka skrzynek pojawia się dopiero przy kilku skrzynkach', async () => {
  const one = await pageWithMailboxes(1);
  assert.equal(run(one.page, "document.getElementById('mailboxSearchWrap').hidden"), true);
  const four = await pageWithMailboxes(4);
  assert.equal(run(four.page, "document.getElementById('mailboxSearchWrap').hidden"), false);
});
