'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Zakładka Leady pokazuje pola ze stron firm i z odpowiedzi osób trzecich: mają trafiać do DOM
// wyłącznie jako tekst (atrapa DOM rzuca błąd przy innerHTML), a zapis przy opuszczeniu pola, „Zatwierdź” i „Cofnij” mają wołać serwer z właściwymi danymi.
const test = require('node:test');
const assert = require('node:assert/strict');
const {createApp} = require('./support/mini_dom.js');

const IDS = {
  leads: {}, summary: {}, exportBtn: {tag: 'button'}, helpBtn: {tag: 'button'}, tabs: {}, q: {tag: 'input'}, stageSel: {tag: 'select'},
  segmentSel: {tag: 'select'}, sortSel: {tag: 'select'}, list: {}, scrim: {attrs: {hidden: ''}}, drawer: {tag: 'aside', attrs: {hidden: ''}},
};
const EVIL = '<img src=x onerror=window.PWNED=1><script>window.PWNED=1</script>';

const thread = (id, over) => Object.assign({id, rating: 'positive', rating_by: 'human', rating_note: 'pyta o cennik', optout: false, awaiting_reply: true,
  last_in_at: '2026-10-08T09:00:00', last_snippet: 'Proszę o cennik', last_from: 'Anna', mailbox: 'roman@z3x.pl'}, over || {});

const lead = (id, over) => Object.assign({id, name: 'Firma ' + id, website: 'https://' + id + '.example.pl/', contact: 'Anna Nowak', phone: '+48 600 100 200',
  email: id + '@example.pl', about: 'Wynajem limuzyn', note: null, stage: 'new', stage_label: 'Nowy', src: {}, pending: [], missing: [], complete: 5, active: true,
  optout: false, do_not_contact: false, rating: 'positive', rating_by: 'human', thread_id: id, thread_ids: [id], segment: 'eventy', country: 'PL',
  base_description: '', created_at: '2026-10-08T10:00:00', updated_at: '2026-10-08T10:00:00', updated_by: null, last_in_at: '2026-10-08T09:00:00',
  snippet: 'Proszę o cennik', last_from: 'Anna', awaiting_reply: true, threads: [thread(id)]}, over || {});

const COUNTS = {all: 2, missing: 1, ai: 1, withdrawn: 0};

async function boot(options) {
  const o = options || {};
  const state = {rows: o.rows || [lead('eventy-001', {src: {about: 'ai'}, pending: ['about']}), lead('eventy-002', {about: null, missing: ['about'], complete: 4})],
    counts: o.counts || COUNTS, leads: o.leads || {}, saved: []};
  const app = createApp({ids: IDS, search: o.search || '', respond: async (url, body) => {
    if (o.respond) { const custom = await o.respond(url, body, state); if (custom !== undefined) return custom; }
    if (url === '/api/leady/lista') return {tab: body.tab, rows: state.rows, total: state.rows.length, page: 1, size: 50, pages: 1, counts: state.counts, segments: ['eventy', 'kancelarie']};
    if (url === '/api/leady/lead') return {lead: state.leads[body.id] || state.rows.find((r) => r.id === body.id) || lead(body.id)};
    if (url === '/api/leady/zapisz') {
      const base = state.leads[body.id] || state.rows.find((r) => r.id === body.id);
      const changed = Object.assign({}, base, body.changes, {src: Object.assign({}, base.src, Object.fromEntries(Object.keys(body.changes).map((k) => [k, 'human']))), pending: []});
      state.leads[body.id] = changed;
      return {lead: changed, previous: Object.fromEntries(Object.keys(body.changes).map((k) => [k, {value: base[k] || null, src: null}]))};
    }
    if (url === '/api/leady/zatwierdz') {
      const base = state.leads[body.id] || state.rows.find((r) => r.id === body.id);
      const changed = Object.assign({}, base, {src: {}, pending: []});
      state.leads[body.id] = changed;
      return {lead: changed, approved: body.fields, previous: Object.fromEntries(body.fields.map((k) => [k, {value: base[k], src: 'ai'}]))};
    }
    if (url === '/api/leady/cofnij') return {lead: state.leads[body.id] || state.rows.find((r) => r.id === body.id)};
    if (url === '/api/leady/eksport') return 'Firma;Strona\r\n';
    return {};
  }});
  app.load(['common.js', 'leady.js']);
  await app.settle();
  return {app, state};
}

const rowNodes = (app) => app.all('.ld-row');
const rowNames = (app) => rowNodes(app).map((n) => n.querySelector('.ld-firm').textContent);
const attributeNames = (app) => app.all('*').flatMap((n) => Object.keys(n.attrs));
const field = (app, name) => app.el('f_' + name);
const byText = (app, selector, text) => app.all(selector).find((n) => n.textContent === text);
const toastTexts = (app) => app.all('.toast').map((n) => n.textContent);

async function type(app, name, value) {
  const input = field(app, name);
  input.focus();
  input.value = value;
  input.dispatch('blur');
  await app.settle();
}

test('start: lista z serwera, karty z licznikami, podsumowanie i filtry', async () => {
  const {app} = await boot();
  assert.deepEqual(app.callsTo('/api/leady/lista')[0].body, {tab: 'all', q: '', segments: [], stages: [], sort: 'recent', page: 1, size: 50});
  assert.deepEqual(rowNames(app), ['Firma eventy-001', 'Firma eventy-002']);
  const tabs = app.all('.kit-chip').map((n) => n.textContent);
  assert.deepEqual(tabs, ['Wszystkie2', 'Do uzupełnienia1', 'Do zatwierdzenia1', 'Wycofane']);
  assert.equal(app.el('summary').textContent, '2 leady · 1 kompletnych · 1 do zatwierdzenia');
  assert.deepEqual(app.el('segmentSel').children.map((n) => n.textContent), ['Wszystkie segmenty', 'eventy', 'kancelarie']);  // pierwsza opcja jest też w HTML strony
  assert.deepEqual(app.el('stageSel').children.map((n) => n.textContent), ['Nowy', 'W rozmowie', 'Umówione', 'Wygrany', 'Odpadł']);
});

test('wiersz pokazuje domenę, ocenę, brakujące pola jako „uzupełnij”, znacznik AI i kompletność', async () => {
  const {app} = await boot();
  const [first, second] = rowNodes(app);
  assert.equal(first.querySelector('.ld-domain').textContent, 'eventy-001.example.pl');
  assert.ok(first.querySelector('.ld-ai'), 'opis od AI jest oznaczony');
  assert.equal(second.querySelector('.c-about .ld-miss').textContent, 'uzupełnij');
  assert.equal(first.querySelector('.c-fields .ld-count').textContent, '5/5');
  assert.equal(second.querySelector('.c-fields .ld-count').textContent, '4/5');
  assert.equal(first.querySelectorAll('.ld-pip.is-ai').length, 1);
  assert.equal(second.querySelectorAll('.ld-pip.is-on').length, 4);
  assert.equal(first.querySelector('.badge.st-new').textContent, 'Nowy');
});

test('wrogi tekst w polach i wątkach trafia do DOM tylko jako tekst, a odnośniki tylko http(s)', async () => {
  const hostile = lead('eventy-001', {name: EVIL, contact: EVIL, email: EVIL, about: EVIL, website: 'javascript:window.PWNED=1', phone: EVIL,
    base_description: EVIL, threads: [thread('eventy-001', {last_snippet: EVIL, last_from: EVIL, rating_note: EVIL})]});
  const {app} = await boot({rows: [hostile]});
  rowNodes(app)[0].click();
  await app.settle();
  assert.ok(app.texts().includes(EVIL), 'hostile tekst jest widoczny jako tekst');
  assert.equal(app.all('img').length + app.all('script').length, 0, 'żadnego znacznika z danych');
  assert.deepEqual(attributeNames(app).filter((name) => /^on/i.test(name)), []);
  assert.deepEqual(app.all('a').filter((a) => !/^(https?:|\/odpowiedzi\?w=)/.test(a.getAttribute('href') || '')), [], 'żaden odnośnik poza http(s) i wątkiem');
  for (const node of app.all('*')) for (const value of Object.values(node.attrs)) assert.ok(!String(value).includes('<'), 'surowy znacznik w atrybucie: ' + value);
});

test('odnośniki w panelu: strona, mailto i tel tylko z poprawnych danych', async () => {
  const {app} = await boot({rows: [lead('eventy-001', {email: 'zly adres', phone: '12'})]});
  rowNodes(app)[0].click();
  await app.settle();
  const hrefs = app.all('.ld-quick a').map((a) => a.getAttribute('href'));
  assert.deepEqual(hrefs, ['https://eventy-001.example.pl/']);
  const {app: good} = await boot({rows: [lead('eventy-001')]});
  rowNodes(good)[0].click();
  await good.settle();
  assert.deepEqual(good.all('.ld-quick a').map((a) => a.getAttribute('href')), ['https://eventy-001.example.pl/', 'mailto:eventy-001@example.pl', 'tel:+48600100200']);
});

test('kliknięcie wiersza otwiera panel z polami, znacznikami AI i wątkami; Esc go zamyka', async () => {
  const {app} = await boot();
  rowNodes(app)[0].click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/leady/lead')[0].body, {id: 'eventy-001'});
  assert.ok(!app.el('drawer').hasAttribute('hidden'));
  assert.ok(app.el('leads').classList.contains('has-drawer'));
  assert.equal(field(app, 'name').value, 'Firma eventy-001');
  assert.equal(field(app, 'about').value, 'Wynajem limuzyn');
  assert.ok(app.first('[data-wrap="about"]').classList.contains('is-ai'));
  assert.ok(byText(app, 'button', 'Zatwierdź'), 'przy polu AI jest „Zatwierdź”');
  assert.equal(app.all('.ld-thread').length, 1);
  assert.equal(app.first('.ld-thread a').getAttribute('href'), '/odpowiedzi?w=eventy-001');
  assert.ok(app.history.replaced.some((url) => url.includes('l=eventy-001')));
  app.key('Escape');
  assert.ok(app.el('drawer').hasAttribute('hidden'));
  assert.ok(!app.el('leads').classList.contains('has-drawer'));
  assert.ok(app.history.replaced.at(-1) === '/odpowiedzi', 'adres bez leada');
});

test('opuszczenie pola zapisuje tylko to pole, bez zmiany nie woła serwera, a toast ma „Cofnij”', async () => {
  const {app} = await boot();
  rowNodes(app)[1].click();
  await app.settle();
  await type(app, 'about', 'Konferencje i eventy firmowe');
  assert.deepEqual(app.callsTo('/api/leady/zapisz').map((c) => c.body), [{id: 'eventy-002', changes: {about: 'Konferencje i eventy firmowe'}}]);
  assert.ok(toastTexts(app).some((text) => text.startsWith('Zapisano: Czym się zajmuje')));
  await type(app, 'about', 'Konferencje i eventy firmowe');
  assert.equal(app.callsTo('/api/leady/zapisz').length, 1, 'ta sama wartość nie jest wysyłana drugi raz');
  await type(app, 'name', '  Firma eventy-002  ');
  assert.equal(app.callsTo('/api/leady/zapisz').length, 1, 'spacje wokół niezmienionej wartości też nie są zmianą');
  assert.equal(rowNodes(app)[1].querySelector('.c-about .ld-about').textContent, 'Konferencje i eventy firmowe');
});

test('błąd zapisu pokazuje komunikat i przywraca poprzednią wartość pola', async () => {
  const {app} = await boot({respond: async (url) => (url === '/api/leady/zapisz' ? {__status: 400, body: {error: 'E-mail ma niepoprawny kształt.'}} : undefined)});
  rowNodes(app)[0].click();
  await app.settle();
  await type(app, 'email', 'nie-adres');
  assert.ok(toastTexts(app).includes('E-mail ma niepoprawny kształt.'));
  assert.equal(field(app, 'email').value, 'eventy-001@example.pl');
});

test('„Cofnij” w toaście wysyła poprzednie wartości razem z pochodzeniem', async () => {
  const {app} = await boot();
  rowNodes(app)[1].click();
  await app.settle();
  await type(app, 'contact', 'Bartek Sbe, dyrektor');
  app.first('.toast-action').click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/leady/cofnij')[0].body, {id: 'eventy-002', previous: {contact: {value: 'Anna Nowak', src: null}}});
  assert.ok(toastTexts(app).includes('Cofnięto.'));
});

test('„Zatwierdź” wysyła pole, a po odpowiedzi znika znacznik AI', async () => {
  const {app} = await boot();
  rowNodes(app)[0].click();
  await app.settle();
  byText(app, 'button', 'Zatwierdź').click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/leady/zatwierdz')[0].body, {id: 'eventy-001', fields: ['about']});
  assert.ok(!app.first('[data-wrap="about"]').classList.contains('is-ai'));
  assert.equal(byText(app, 'button', 'Zatwierdź'), undefined);
  assert.ok(toastTexts(app).some((text) => text.startsWith('Zatwierdzono 1 pole')));
});

test('zatwierdzanie wszystkiego pojawia się dopiero przy więcej niż jednym wpisie AI', async () => {
  const many = lead('eventy-001', {src: {about: 'ai', contact: 'ai'}, pending: ['contact', 'about']});
  const {app} = await boot({rows: [many]});
  rowNodes(app)[0].click();
  await app.settle();
  const all = app.first('.ld-approve-all');
  assert.ok(!all.hasAttribute('hidden'));
  assert.equal(all.textContent, 'Zatwierdź wszystkie wpisy AI (2)');
  all.click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/leady/zatwierdz')[0].body, {id: 'eventy-001', fields: ['contact', 'about']});
});

test('etap zmienia się jednym kliknięciem i jest zaznaczony w panelu', async () => {
  const {app} = await boot();
  rowNodes(app)[0].click();
  await app.settle();
  byText(app, '#stageGroup button', 'Umówione').click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/leady/zapisz')[0].body, {id: 'eventy-001', changes: {stage: 'meeting'}});
  assert.equal(byText(app, '#stageGroup button', 'Umówione').getAttribute('aria-pressed'), 'true');
  assert.equal(byText(app, '#stageGroup button', 'Nowy').getAttribute('aria-pressed'), 'false');
});

test('opis z bazy można użyć jednym przyciskiem', async () => {
  const {app} = await boot({rows: [lead('eventy-002', {about: null, base_description: 'Organizator konferencji.'})]});
  rowNodes(app)[0].click();
  await app.settle();
  byText(app, 'button', 'Użyj').click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/leady/zapisz')[0].body, {id: 'eventy-002', changes: {about: 'Organizator konferencji.'}});
  assert.equal(field(app, 'about').value, 'Organizator konferencji.');
});

test('Esc w polu z niezapisaną zmianą cofa ją, a bez zmiany zamyka panel', async () => {
  const {app} = await boot();
  rowNodes(app)[0].click();
  await app.settle();
  const input = field(app, 'about');
  input.focus();
  input.value = 'coś innego';
  app.key('Escape', {target: input});
  assert.equal(input.value, 'Wynajem limuzyn');
  assert.ok(!app.el('drawer').hasAttribute('hidden'), 'panel zostaje otwarty');
  app.key('Escape', {target: input});
  assert.ok(app.el('drawer').hasAttribute('hidden'));
  assert.equal(app.callsTo('/api/leady/zapisz').length, 0);
});

test('szukanie z opóźnieniem, etap, segment, sortowanie i karta wołają listę z nowym filtrem', async () => {
  const {app} = await boot();
  app.el('q').value = '  limuzyn ';
  app.el('q').dispatch('input');
  await app.flushTimers();
  app.el('stageSel').value = 'won';
  app.el('stageSel').dispatch('change');
  await app.settle();
  app.el('segmentSel').value = 'eventy';
  app.el('segmentSel').dispatch('change');
  await app.settle();
  app.el('sortSel').value = 'name';
  app.el('sortSel').dispatch('change');
  await app.settle();
  byText(app, '.kit-chip', 'Do uzupełnienia1').click();
  await app.settle();
  const last = app.callsTo('/api/leady/lista').at(-1).body;
  assert.deepEqual(last, {tab: 'missing', q: 'limuzyn', segments: ['eventy'], stages: ['won'], sort: 'name', page: 1, size: 50});
  assert.ok(app.history.replaced.at(-1).includes('tab=missing') && app.history.replaced.at(-1).includes('q=limuzyn'));
});

test('stan z adresu: karta, szukanie i otwarty lead wracają po odświeżeniu', async () => {
  const {app} = await boot({search: '?tab=ai&q=anna&stage=talking&sort=rating&l=eventy-001'});
  const body = app.callsTo('/api/leady/lista')[0].body;
  assert.deepEqual(body, {tab: 'ai', q: 'anna', segments: [], stages: ['talking'], sort: 'rating', page: 1, size: 50});
  assert.equal(app.el('q').value, 'anna');
  assert.deepEqual(app.callsTo('/api/leady/lead')[0].body, {id: 'eventy-001'});
  assert.ok(!app.el('drawer').hasAttribute('hidden'));
});

test('puste stany: brak leadów (z odnośnikiem do Odpowiedzi), brak wyników filtra i pusta karta', async () => {
  const {app} = await boot({rows: [], counts: {all: 0, missing: 0, ai: 0, withdrawn: 0}});
  assert.ok(app.texts().includes('Nie ma jeszcze leadów'));
  assert.equal(app.first('.ld-empty a').getAttribute('href'), '/odpowiedzi');
  const {app: filtered} = await boot({rows: [], search: '?q=nic'});
  assert.ok(filtered.texts().includes('Nic nie pasuje do filtrów'));
  const {app: tab} = await boot({rows: [], search: '?tab=missing'});
  assert.ok(tab.texts().includes('Pusto w tym widoku'));
});

test('wycofany lead i prośba o brak kontaktu są oznaczone w wierszu i w panelu', async () => {
  const withdrawn = lead('eventy-001', {active: false, optout: true, do_not_contact: true, rating: 'negative'});
  const {app} = await boot({rows: [withdrawn]});
  const flags = app.first('.ld-flags').textContent;
  assert.ok(['Negatywna', 'Prosił o brak kontaktu', 'Nie kontaktować', 'Wycofany'].every((text) => flags.includes(text)), flags);
  rowNodes(app)[0].click();
  await app.settle();
  assert.equal(app.all('.banner').length, 3);
});

test('kopiowanie e-maila nie otwiera panelu i zgłasza wynik', async () => {
  const {app} = await boot();
  const copied = [];
  app.context.navigator = {clipboard: {writeText: async (text) => { copied.push(text); }}};
  app.first('.ld-copy').click();
  await app.settle();
  assert.deepEqual(copied, ['eventy-001@example.pl']);
  assert.equal(app.callsTo('/api/leady/lead').length, 0);
  assert.ok(toastTexts(app).includes('Skopiowano e-mail.'));
});

test('eksport wysyła filtry bez stronicowania', async () => {
  const {app} = await boot({search: '?tab=ai&q=anna'});
  app.el('exportBtn').click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/leady/eksport')[0].body, {tab: 'ai', q: 'anna', segments: [], stages: [], sort: 'recent'});
});

test('j i k przesuwają fokus po wierszach, Enter na wierszu otwiera lead', async () => {
  const {app} = await boot();
  app.key('j');
  assert.equal(app.document.activeElement, rowNodes(app)[0]);
  app.key('j');
  assert.equal(app.document.activeElement, rowNodes(app)[1]);
  app.key('k');
  assert.equal(app.document.activeElement, rowNodes(app)[0]);
  rowNodes(app)[0].dispatch('keydown', {key: 'Enter'});
  await app.settle();
  assert.deepEqual(app.callsTo('/api/leady/lead')[0].body, {id: 'eventy-001'});
});

test('odświeżanie w tle nie przebudowuje listy, gdy nic się nie zmieniło', async () => {
  const {app} = await boot();
  const before = rowNodes(app)[0];
  await app.eval('loadList()');
  await app.settle();
  assert.equal(rowNodes(app)[0], before);
});
