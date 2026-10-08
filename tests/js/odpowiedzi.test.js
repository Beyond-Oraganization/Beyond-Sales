'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Strona Odpowiedzi pokazuje treść pisaną przez osoby trzecie: ma trafiać do DOM
// wyłącznie jako tekst (atrapa DOM rzuca błąd przy innerHTML), a kliknięcia, skróty i „Cofnij” mają wołać serwer z właściwymi danymi.
const test = require('node:test');
const assert = require('node:assert/strict');
const {createApp} = require('./support/mini_dom.js');

const IDS = {
  replies: {}, tabs: {}, q: {tag: 'input'}, filterBtn: {tag: 'button'}, filterPop: {attrs: {hidden: ''}}, mailboxFind: {tag: 'input'}, mailboxList: {},
  segmentSel: {tag: 'select'}, countrySel: {tag: 'select'}, fromDay: {tag: 'input'}, toDay: {tag: 'input'}, clearFilters: {tag: 'button'},
  fetchBtn: {tag: 'button'}, fetchInfo: {}, helpBtn: {tag: 'button'}, mailStatus: {attrs: {hidden: ''}}, split: {attrs: {'data-view': 'list'}}, list: {}, detail: {},
  viewMail: {tag: 'button'}, viewReport: {tag: 'button'}, report: {attrs: {hidden: '', class: 'rp-report'}},
};
const EVIL = '<img src=x onerror=window.PWNED=1><script>window.PWNED=1</script>';

const row = (id, over) => Object.assign({id, company: 'Firma ' + id, segment: 'eventy', country: 'PL', mailbox: 'roman@z3x.pl', rating: null, rating_by: null,
  optout: false, unread: true, closed: false, awaiting_reply: true, weak: false, other_mailbox: false, last_snippet: 'Proszę o cennik', last_from: 'Anna',
  last_direction: 'in', last_activity_at: '2026-10-08T10:00:00', mail_status: 'odpowiedzial'}, over || {});

const message = (over) => Object.assign({id: 1, direction: 'in', kind: 'human', state: 'received', from_addr: 'anna@x.pl', from_name: 'Anna', date: '2026-10-08T10:00:00',
  new: 'Proszę o cennik.', quoted: 'Nasz stary mail\nwiersz 2', attachments: [], bounce_type: '', optout_hint: false}, over || {});

function view(id, over) {
  const thread = row(id, over && over.thread);
  return Object.assign({thread, origin: {id, subject: 'Temat kampanii', text: 'Treść naszego maila', sent_at: '2026-09-29T13:00:00', website: 'https://firma.pl', email: 'a@firma.pl'},
    messages: [message()], ratings: [], suggestion: null, statuses: [{code: 'zainteresowany', label: 'Zainteresowany', color: '#be185d'}],
    compose: {can_reply: true, mailbox: 'roman@z3x.pl', mailbox_ok: true, mailbox_problem: '', to: 'anna@x.pl', subject: 'Re: Temat', needs_ack: [], signature: 'Roman', draft: null}},
  over || {});
}

function overview(over) {
  return Object.assign({counts: {todo: 2, all: 2, positive: 0, neutral: 0, negative: 0, unrated: 2, machine: 0, unassigned: 1}, unread: 2,
    fetch: {running: false, finished_at: null, started_at: null, summary: {}}, auto_minutes: 0,
    mailboxes: {configured: 3, receiving: 2, names: ['roman@z3x.pl', 'drugi@z3x.pl', 'trzeci@z3x.pl'], problems: [{mailbox: 'trzeci@z3x.pl', kind: 'no_imap', error: ''}]}}, over || {});
}

async function boot(options) {
  const o = options || {};
  const state = {rows: o.rows || [row('eventy-001'), row('eventy-002', {unread: false, rating: 'negative', rating_by: 'human'})], views: o.views || {}, overview: o.overview || overview()};
  const app = createApp({ids: IDS, search: o.search || '', respond: async (url, body) => {
    if (o.respond) { const custom = await o.respond(url, body, state); if (custom !== undefined) return custom; }
    if (url === '/api/odpowiedzi/lista') return {tab: body.tab, rows: state.rows, total: state.rows.length, page: 1, size: 50, pages: 1, counts: state.overview.counts};
    if (url === '/api/odpowiedzi/stan') return state.overview;
    if (url === '/api/odpowiedzi/watek') return state.views[body.id] || view(body.id);
    if (url === '/api/odpowiedzi/ocena') return {previous: {rating: null, by: null, note: ''}};
    if (url === '/api/odpowiedzi/akcja') return {ok: true, action: body.action};
    if (url === '/api/odpowiedzi/szkic') return {saved: true, id: 5};
    if (url === '/api/odpowiedzi/szkic-usun') return {discarded: true};
    if (url === '/api/odpowiedzi/wyslij') return {sent: true, message_id: 'nowy@z3x.pl', warning: ''};
    return {};
  }});
  app.load(['common.js', 'odpowiedzi.js']);
  await app.settle();
  return {app, state};
}

const rowNodes = (app) => app.all('.rp-row');
const rowCompanies = (app) => rowNodes(app).map((n) => n.querySelector('.rp-company').textContent);
const attributeNames = (app) => app.all('*').flatMap((n) => Object.keys(n.attrs));

test('start: lista i stan z serwera, zakładki z licznikami, nieprzeczytane wyróżnione', async () => {
  const {app} = await boot();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/lista')[0].body, {tab: 'todo', q: '', mailboxes: [], segments: [], countries: [], from: '', to: '', page: 1, size: 50});
  assert.deepEqual(rowCompanies(app), ['Firma eventy-001', 'Firma eventy-002']);
  assert.ok(rowNodes(app)[0].classList.contains('is-unread'));
  assert.ok(!rowNodes(app)[1].classList.contains('is-unread'));
  const tabs = app.all('.kit-chip').map((n) => n.textContent);
  assert.ok(tabs.includes('Do odpowiedzi2') && tabs.includes('Nieprzypisane1'), tabs.join('|'));
  assert.equal(app.document.title, '(2) Perno — Odpowiedzi');
});

test('wrogi tekst w firmie, nadawcy, temacie i treści trafia do DOM tylko jako tekst', async () => {
  const hostile = row('eventy-001', {company: EVIL, last_from: EVIL, last_snippet: EVIL});
  const {app} = await boot({rows: [hostile], views: {'eventy-001': view('eventy-001', {thread: hostile, origin: {id: 'eventy-001', subject: EVIL, text: EVIL, sent_at: '2026-09-29T13:00:00', website: 'javascript:window.PWNED=1', email: EVIL},
    messages: [message({from_name: EVIL, new: EVIL, quoted: EVIL, attachments: [{name: EVIL, size: 10}]})]})}});
  rowNodes(app)[0].click();
  await app.settle();
  assert.ok(app.texts().includes(EVIL), 'hostile tekst jest widoczny jako tekst');
  assert.equal(app.all('img').length + app.all('script').length, 0, 'żadnego znacznika z danych');
  assert.deepEqual(attributeNames(app).filter((name) => /^on/i.test(name)), []);
  assert.deepEqual(app.all('a').filter((a) => /^javascript:/i.test(a.getAttribute('href') || '')), [], 'link do strony tylko http(s)');
  for (const node of app.all('*')) for (const value of Object.values(node.attrs)) assert.ok(!value.includes('<'), 'surowy znacznik w atrybucie: ' + value);
});

test('kliknięcie wiersza otwiera wątek, oznacza go jako przeczytany i zapisuje adres', async () => {
  const {app} = await boot();
  rowNodes(app)[0].click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/watek')[0].body, {id: 'eventy-001'});
  assert.deepEqual(app.callsTo('/api/odpowiedzi/akcja')[0].body, {id: 'eventy-001', action: 'read'});
  assert.equal(app.el('split').getAttribute('data-view'), 'detail');
  assert.ok(app.history.pushed[0].endsWith('?w=eventy-001'), app.history.pushed.join());
  assert.ok(app.first('.rp-thread h2').textContent.includes('Firma eventy-001'));
  assert.equal(app.all('.rp-rate button').length, 3);
  assert.equal(app.first('.rp-row[data-id="eventy-001"]').getAttribute('aria-current'), 'true');
});

test('cytat jest zwinięty pod „···” i rozwija się na żądanie, a nasz mail jest zwinięty u góry', async () => {
  const {app} = await boot();
  rowNodes(app)[0].click();
  await app.settle();
  const quote = app.first('.rp-quote');
  assert.ok(quote.hasAttribute('hidden'));
  const toggle = app.first('.rp-quote-toggle');
  assert.ok(toggle.textContent.includes('cytat (2 linii)'), toggle.textContent);
  toggle.click();
  assert.ok(!quote.hasAttribute('hidden'));
  assert.ok(app.first('.rp-msg').classList.contains('is-collapsed'), 'nasz mail zwinięty');
  const cards = app.all('.rp-msg');
  assert.ok(!cards[cards.length - 1].classList.contains('is-collapsed'), 'ostatnia wiadomość rozwinięta');
});

test('ocena kliknięciem: wysyła wybraną wartość, pokazuje „Cofnij” i cofnięcie przywraca poprzednią ocenę z autorem', async () => {
  const {app} = await boot({respond: async (url, body) => {
    if (url === '/api/odpowiedzi/ocena' && body.rating === 'positive') return {previous: {rating: 'neutral', by: 'ai', note: 'AI: ogólnik'}};
  }});
  rowNodes(app)[0].click();
  await app.settle();
  app.all('.rp-rate button')[0].click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/ocena')[0].body, {id: 'eventy-001', rating: 'positive'});
  const undo = app.first('.toast-action');
  assert.ok(undo, 'toast z przyciskiem Cofnij');
  undo.click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/ocena')[1].body, {id: 'eventy-001', rating: 'neutral', note: 'AI: ogólnik', restore_by: 'ai'});
});

test('ponowne kliknięcie zaznaczonej oceny ją usuwa', async () => {
  const rated = row('eventy-001', {rating: 'neutral', rating_by: 'human'});
  const {app} = await boot({rows: [rated], views: {'eventy-001': view('eventy-001', {thread: rated})}});
  rowNodes(app)[0].click();
  await app.settle();
  const pressed = app.all('.rp-rate button').filter((b) => b.getAttribute('aria-pressed') === 'true');
  assert.deepEqual(pressed.map((b) => b.textContent), ['Neutralna']);
  pressed[0].click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/ocena')[0].body, {id: 'eventy-001', rating: null});
});

test('ocena AI ma odznakę i przycisk „Zatwierdź”, który zapisuje ją jako ocenę człowieka z tą samą notatką', async () => {
  const ai = row('eventy-001', {rating: 'neutral', rating_by: 'ai', rating_note: 'ogólne podziękowanie'});
  const {app} = await boot({rows: [ai], views: {'eventy-001': view('eventy-001', {thread: ai})}});
  assert.ok(app.first('.rp-row .is-ai'), 'odznaka AI na liście');
  rowNodes(app)[0].click();
  await app.settle();
  assert.ok(app.texts().includes('Oceniła AI: ogólne podziękowanie'));
  const approve = app.all('button').find((b) => b.textContent === 'Zatwierdź');
  approve.click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/ocena')[0].body, {id: 'eventy-001', rating: 'neutral', note: 'ogólne podziękowanie'});
});

test('skróty: 1/2/3/0 oceniają, j/k przechodzą po liście, / szuka, a w polu tekstowym nie działają', async () => {
  const {app} = await boot();
  app.key('j');
  await app.settle();
  assert.equal(app.callsTo('/api/odpowiedzi/watek')[0].body.id, 'eventy-001');
  app.key('j');
  await app.settle();
  assert.equal(app.callsTo('/api/odpowiedzi/watek')[1].body.id, 'eventy-002');
  app.key('k');
  await app.settle();
  assert.equal(app.callsTo('/api/odpowiedzi/watek')[2].body.id, 'eventy-001');
  app.key('2');
  await app.settle();
  assert.equal(app.callsTo('/api/odpowiedzi/ocena')[0].body.rating, 'neutral');
  app.key('0');
  await app.settle();
  assert.equal(app.callsTo('/api/odpowiedzi/ocena')[1].body.rating, null);
  const typing = app.key('3', {target: app.el('q')});
  assert.equal(typing.defaultPrevented, false, 'w polu tekstowym skrót nie działa');
  assert.equal(app.callsTo('/api/odpowiedzi/ocena').length, 2);
  app.key('/');
  assert.equal(app.document.activeElement, app.el('q'));
});

test('skróty nie działają przy otwartym oknie dialogowym, a „?” otwiera ściągę', async () => {
  const {app} = await boot();
  app.key('?');
  const dialog = app.first('dialog');
  assert.ok(dialog && dialog.opened, 'ściąga w arkuszu');
  assert.ok(app.texts().includes('następna / poprzednia rozmowa'));
  const before = app.callsTo('/api/odpowiedzi/watek').length;
  app.key('j');
  await app.settle();
  assert.equal(app.callsTo('/api/odpowiedzi/watek').length, before);
});

test('przełączenie zakładki prosi serwer o tę zakładkę od pierwszej strony i zapisuje ją w adresie', async () => {
  const {app} = await boot();
  app.all('.kit-chip').find((n) => n.textContent.startsWith('Negatywne')).click();
  await app.settle();
  const last = app.callsTo('/api/odpowiedzi/lista').pop();
  assert.equal(last.body.tab, 'negative');
  assert.equal(last.body.page, 1);
  assert.ok(app.history.replaced.pop().includes('tab=negative'));
});

test('stan z adresu: zakładka, szukany tekst, filtry i otwarty wątek wracają po odświeżeniu', async () => {
  const {app} = await boot({search: '?tab=all&q=cennik&mb=roman%40z3x.pl&seg=eventy&cty=PL&from=2026-10-01&to=2026-10-08&w=eventy-002'});
  const body = app.callsTo('/api/odpowiedzi/lista')[0].body;
  assert.deepEqual([body.tab, body.q, body.mailboxes, body.segments, body.countries, body.from, body.to],
    ['all', 'cennik', ['roman@z3x.pl'], ['eventy'], ['PL'], '2026-10-01', '2026-10-08']);
  assert.equal(app.callsTo('/api/odpowiedzi/watek')[0].body.id, 'eventy-002');
  assert.equal(app.el('q').value, 'cennik');
  assert.ok(app.all('button').some((b) => b.textContent === 'Filtry · 5' || b.textContent.startsWith('Filtry')));
});

test('nieznana zakładka w adresie wraca do domyślnej', async () => {
  const {app} = await boot({search: '?tab=hakerska'});
  assert.equal(app.callsTo('/api/odpowiedzi/lista')[0].body.tab, 'todo');
});

test('wyszukiwanie czeka na koniec pisania i szuka od pierwszej strony', async () => {
  const {app} = await boot();
  app.el('q').value = 'cennik';
  app.el('q').dispatch('input');
  app.el('q').value = 'cennik oferta';
  app.el('q').dispatch('input');
  const before = app.callsTo('/api/odpowiedzi/lista').length;
  await app.flushTimers();
  assert.equal(app.callsTo('/api/odpowiedzi/lista').length, before + 1);
  assert.equal(app.callsTo('/api/odpowiedzi/lista').pop().body.q, 'cennik oferta');
});

test('przestarzała odpowiedź listy nie nadpisuje nowszej', async () => {
  let release = null;
  const {app, state} = await boot({respond: async (url, body) => {
    if (url === '/api/odpowiedzi/lista' && body.tab === 'all') await new Promise((resolve) => { release = resolve; });
  }});
  app.all('.kit-chip').find((n) => n.textContent.startsWith('Wszystkie')).click();  // wolna odpowiedź
  await app.settle();
  state.rows = [row('eventy-099')];
  app.all('.kit-chip').find((n) => n.textContent.startsWith('Pozytywne')).click();  // szybka, nowsza
  await app.settle();
  assert.deepEqual(rowCompanies(app), ['Firma eventy-099']);
  release();
  await app.settle();
  assert.deepEqual(rowCompanies(app), ['Firma eventy-099'], 'spóźniona odpowiedź zignorowana');
});

test('pusta lista mówi, dlaczego jest pusta', async () => {
  const {app} = await boot({rows: []});
  assert.equal(app.first('.rp-empty').textContent, 'Nic nie czeka na odpowiedź.');
  app.el('q').value = 'xyz';
  app.el('q').dispatch('input');
  await app.flushTimers();
  assert.equal(app.first('.rp-empty').textContent, 'Nic nie pasuje do filtrów.');
});

test('pasek skrzynek pokazuje tylko te z problemem, z powodem po ludzku', async () => {
  const {app} = await boot({overview: overview({mailboxes: {configured: 50, receiving: 47, names: [], problems: [
    {mailbox: 'a@z3x.pl', kind: 'auth', error: 'Authentication failed'}, {mailbox: 'b@z3x.pl', kind: 'no_imap', error: ''}, {mailbox: 'c@z3x.pl', kind: 'network', error: 'timed out'}]}})});
  const status = app.el('mailStatus');
  assert.ok(!status.hasAttribute('hidden'));
  assert.ok(status.textContent.startsWith('Odbiór: 47 z 50 skrzynek · 3 z problemem'), status.textContent);
  const items = status.querySelectorAll('li').map((li) => li.textContent);
  assert.deepEqual(items, ['a@z3x.pl błąd logowania — Authentication failed', 'b@z3x.pl brak ustawienia IMAP', 'c@z3x.pl brak połączenia — timed out']);
  assert.ok(status.querySelector('.rp-problems').hasAttribute('hidden'), 'lista zwinięta do czasu kliknięcia');
});

test('bez skrzynek pasek jest ukryty, a etykieta skrzynki w wierszu pojawia się dopiero przy kilku skrzynkach', async () => {
  const {app} = await boot({overview: overview({mailboxes: {configured: 0, receiving: 0, names: [], problems: []}})});
  assert.ok(app.el('mailStatus').hasAttribute('hidden'));
  assert.equal(app.all('.rp-meta .badge').filter((b) => b.textContent === 'roman@z3x.pl').length, 0);
  const many = await boot();
  assert.ok(many.app.all('.rp-meta .badge').some((b) => b.textContent === 'roman@z3x.pl'));
});

test('wzrost liczby nieprzeczytanych pokazuje komunikat i odświeża listę, a licznik w menu i tytule idzie za nim', async () => {
  const {app, state} = await boot();
  const lists = app.callsTo('/api/odpowiedzi/lista').length;
  state.overview = overview({unread: 5});
  await app.eval('refreshOverview()');
  await app.settle();
  assert.ok(app.texts().includes('3 nowe odpowiedzi'));
  assert.equal(app.callsTo('/api/odpowiedzi/lista').length, lists + 1);
  assert.equal(app.document.title, '(5) Perno — Odpowiedzi');
});

test('samo pobieranie: rusza, gdy minął czas, nie rusza w trakcie pobierania ani przy wyłączonym odświeżaniu', async () => {
  const old = '2026-01-01T10:00:00';
  const started = await boot({overview: overview({auto_minutes: 5, fetch: {running: false, finished_at: old, started_at: old, summary: {}}})});
  assert.equal(started.app.callsTo('/api/odpowiedzi/pobierz').length, 1);
  const running = await boot({overview: overview({auto_minutes: 5, fetch: {running: true, finished_at: old, started_at: old, summary: {}}})});
  assert.equal(running.app.callsTo('/api/odpowiedzi/pobierz').length, 0);
  const off = await boot({overview: overview({auto_minutes: 0, fetch: {running: false, finished_at: old, started_at: old, summary: {}}})});
  assert.equal(off.app.callsTo('/api/odpowiedzi/pobierz').length, 0);
  const none = await boot({overview: overview({auto_minutes: 5, mailboxes: {configured: 1, receiving: 0, names: [], problems: []}})});
  assert.equal(none.app.callsTo('/api/odpowiedzi/pobierz').length, 0);
});

test('przycisk „Pobierz teraz” startuje pobieranie i czeka na koniec', async () => {
  let running = true;
  const {app, state} = await boot({respond: async (url) => {
    if (url === '/api/odpowiedzi/pobierz') return {started: true, reason: ''};
    if (url === '/api/odpowiedzi/stan') return Object.assign({}, state_overview(), {fetch: {running, finished_at: running ? null : '2026-10-08T10:00:00', started_at: null, summary: {stored: 2}}});
    return undefined;
  }});
  function state_overview() { return overview(); }
  app.el('fetchBtn').click();
  await app.settle();
  assert.equal(app.callsTo('/api/odpowiedzi/pobierz').length, 1);
  assert.equal(app.el('fetchInfo').textContent, 'Pobieram…');
  running = false;
  await app.flushTimers();
  assert.ok(app.texts().includes('Pobrano: 2 nowych wiadomości.'), app.texts().slice(0, 300));
  assert.ok(state);
});

test('nieprzypisana wiadomość: podgląd, szukanie maila kampanii i przypisanie z liczbami całkowitymi', async () => {
  const unassigned = {id: 7, unassigned: true, from_addr: 'obcy@x.pl', from_name: 'Obcy', subject: 'Re: Temat', snippet: 'Kto to?', date_at: '2026-10-08T09:00:00', mailbox: 'roman@z3x.pl', kind: 'human'};
  const {app} = await boot({rows: [unassigned], respond: async (url, body) => {
    if (url === '/api/odpowiedzi/wiadomosc') return {message: message({id: 7, from_name: 'Obcy', new: 'Kto to?', quoted: ''})};
    if (url === '/api/odpowiedzi/kandydaci') return {mails: [{id: 'eventy-004', company: 'Czwarta', email: 'c@x.pl', subject: 'Temat 4', sent_at: '2026-09-29T13:00:00', status: 'wyslano'}]};
    if (url === '/api/odpowiedzi/przypisz') return {ok: true, thread: body.mail_id};
  }});
  assert.equal(app.first('.rp-company').textContent, 'Obcy');
  rowNodes(app)[0].click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/wiadomosc')[0].body, {id: 7});
  const search = app.first('.rp-assign input');
  search.value = 'czwarta';
  search.dispatch('input');
  await app.flushTimers();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/kandydaci')[0].body, {q: 'czwarta'});
  app.first('.rp-assign-results button').click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/przypisz')[0].body, {message_id: 7, mail_id: 'eventy-004'});
  assert.equal(app.callsTo('/api/odpowiedzi/watek').pop().body.id, 'eventy-004', 'po przypisaniu otwiera się wątek');
});

test('podpowiedź statusu i prośba o wypisanie wołają ustawienie statusu, a „Cofnij” wraca do poprzedniego', async () => {
  const suggested = view('eventy-001', {suggestion: 'zainteresowany'});
  const {app} = await boot({views: {'eventy-001': suggested}, respond: async (url, body) => {
    if (url === '/api/odpowiedzi/status') return {previous: 'odpowiedzial', status: body.status};
  }});
  rowNodes(app)[0].click();
  await app.settle();
  const set = app.all('.banner button').find((b) => b.textContent === 'Ustaw');
  set.click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/status')[0].body, {id: 'eventy-001', status: 'zainteresowany'});
  app.first('.toast-action').click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/status')[1].body, {id: 'eventy-001', status: 'odpowiedzial'});
});

test('prośba o wypisanie pokazuje baner z przyciskiem „Nie kontaktować”, a gdy status już taki jest, bez przycisku', async () => {
  const optout = view('eventy-001', {thread: row('eventy-001', {optout: true, mail_status: 'odpowiedzial'})});
  const first = await boot({views: {'eventy-001': optout}});
  rowNodes(first.app)[0].click();
  await first.app.settle();
  assert.ok(first.app.all('.banner button').some((b) => b.textContent === 'Ustaw: Nie kontaktować'));
  const done = view('eventy-001', {thread: row('eventy-001', {optout: true, mail_status: 'nie_kontaktowac'})});
  const second = await boot({views: {'eventy-001': done}});
  rowNodes(second.app)[0].click();
  await second.app.settle();
  assert.ok(!second.app.all('.banner button').some((b) => b.textContent === 'Ustaw: Nie kontaktować'));
});

test('zamknięcie wątku skrótem „e” przechodzi do następnej rozmowy, gdy ta zniknęła z zakładki', async () => {
  const {app, state} = await boot({respond: async (url, body, st) => {
    if (url === '/api/odpowiedzi/akcja' && body.action === 'close') st.rows = st.rows.filter((r) => r.id !== body.id);
  }});
  app.key('j');
  await app.settle();
  app.key('e');
  await app.settle();
  assert.equal(app.callsTo('/api/odpowiedzi/akcja').find((c) => c.body.action === 'close').body.id, 'eventy-001');
  assert.equal(app.callsTo('/api/odpowiedzi/watek').pop().body.id, 'eventy-002', 'otwarta następna rozmowa');
  assert.ok(state.rows.length === 1);
});

test('na telefonie przycisk „Lista” wraca do listy i czyści wątek z adresu', async () => {
  const {app} = await boot({search: '?w=eventy-001'});
  assert.equal(app.el('split').getAttribute('data-view'), 'detail');
  app.all('.split-back')[0].click();
  assert.equal(app.el('split').getAttribute('data-view'), 'list');
  assert.ok(!app.history.replaced.pop().includes('w='));
  assert.equal(app.el('detail').children.length, 0);
});

test('błąd serwera przy otwarciu wątku jest komunikatem, nie pustym ekranem', async () => {
  const {app} = await boot({respond: async (url) => (url === '/api/odpowiedzi/watek' ? {__status: 404, body: {error: 'Nie ma wątku eventy-001.'}} : undefined)});
  rowNodes(app)[0].click();
  await app.settle();
  assert.equal(app.first('.rp-empty').textContent, 'Nie ma wątku eventy-001.');
});

// ---- edytor odpowiedzi ----------------------------------------------------------------------------------------------

async function openFirst(options) {
  const booted = await boot(options);
  rowNodes(booted.app)[0].click();
  await booted.app.settle();
  return booted;
}

const type = (app, text) => { const area = app.first('.rp-draft'); area.value = text; area.dispatch('input'); return area; };
const button = (app, text) => app.all('button').find((b) => b.textContent === text);

test('edytor: zamknięty pokazuje „Odpowiedz”, a po kliknięciu od, do i temat oraz pole tekstowe', async () => {
  const {app} = await openFirst();
  assert.ok(!app.first('.rp-draft'));
  button(app, 'Odpowiedz').click();
  await app.settle();
  assert.ok(app.first('.rp-draft'));
  const head = app.first('.rp-compose-head').textContent;
  assert.ok(head.includes('Od: roman@z3x.pl') && head.includes('Do: anna@x.pl') && head.includes('Re: Temat'), head);
  assert.equal(app.first('.rp-draft').getAttribute('placeholder'), 'Napisz odpowiedź…');
});

test('edytor: pisanie zapisuje szkic po chwili (jeden zapis na serię znaków), a wyczyszczenie go usuwa', async () => {
  const {app} = await openFirst();
  button(app, 'Odpowiedz').click();
  await app.settle();
  type(app, 'Dzi');
  type(app, 'Dziękuję za wiadomość.');
  assert.equal(app.callsTo('/api/odpowiedzi/szkic').length, 0, 'zapis czeka na koniec pisania');
  await app.flushTimers();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/szkic').map((c) => c.body), [{id: 'eventy-001', body: 'Dziękuję za wiadomość.'}]);
  assert.equal(app.first('.rp-compose-foot .rp-hint').textContent, 'Szkic zapisany');
  type(app, '   ');
  await app.flushTimers();
  assert.equal(app.callsTo('/api/odpowiedzi/szkic-usun').length, 1);
});

test('edytor: istniejący szkic jest wczytany, szkic AI ma ostrzeżenie, a nieudana wysyłka pokazuje powód', async () => {
  const draft = (over) => view('eventy-001', {compose: Object.assign(view('x').compose, {draft: Object.assign({id: 9, body: 'Treść szkicu', author: 'human', state: 'draft', error: ''}, over)})});
  const human = await openFirst({views: {'eventy-001': draft()}});
  assert.equal(human.app.first('.rp-draft').value, 'Treść szkicu');
  assert.ok(!human.app.texts().includes('Claude Code'));
  const ai = await openFirst({views: {'eventy-001': draft({author: 'ai'})}});
  assert.ok(ai.app.texts().includes('Szkic przygotował Claude Code'));
  const failed = await openFirst({views: {'eventy-001': draft({state: 'failed', error: '550 odrzucone'})}});
  assert.ok(failed.app.texts().includes('Ostatnia próba wysłania nie udała się: 550 odrzucone'));
});

test('edytor: skrzynka bez SMTP i wątek bez odpowiedzi człowieka nie dają pola do pisania, tylko powód', async () => {
  const noBox = await openFirst({views: {'eventy-001': view('eventy-001', {compose: {can_reply: true, mailbox: 'x@z3x.pl', mailbox_ok: false, mailbox_problem: 'Skrzynka x@z3x.pl nie jest skonfigurowana (SMTP).'}})}});
  assert.ok(noBox.app.texts().includes('nie jest skonfigurowana'));
  assert.ok(!noBox.app.first('.rp-draft') && !button(noBox.app, 'Odpowiedz'));
  const none = await openFirst({views: {'eventy-001': view('eventy-001', {compose: {can_reply: false, reason: 'W tym wątku nie ma odpowiedzi człowieka.'}})}});
  assert.ok(none.app.texts().includes('W tym wątku nie ma odpowiedzi człowieka.'));
});

test('wysyłka: po kliknięciu nic nie wychodzi od razu, jest „Cofnij”, a po sześciu sekundach rusza z właściwymi danymi', async () => {
  const {app} = await openFirst();
  button(app, 'Odpowiedz').click();
  await app.settle();
  type(app, 'Dziękuję, wysyłam cennik.');
  button(app, 'Wyślij').click();
  await app.settle();
  assert.equal(app.callsTo('/api/odpowiedzi/wyslij').length, 0);
  assert.ok(app.callsTo('/api/odpowiedzi/szkic').length >= 1, 'szkic zapisany przed wysyłką');
  assert.ok(app.first('.toast-action'), 'przycisk Cofnij');
  assert.ok(app.all('.rp-composer button').every((b) => b.disabled), 'edytor zablokowany na czas odliczania');
  await app.flushTimers();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/wyslij')[0].body, {id: 'eventy-001', ack: [], quote: true, close: false});
  assert.ok(app.texts().includes('Wysłano.'));
});

test('wysyłka: „Cofnij” anuluje wysyłkę i zwalnia edytor, a szkic zostaje', async () => {
  const {app} = await openFirst();
  button(app, 'Odpowiedz').click();
  await app.settle();
  type(app, 'Treść');
  button(app, 'Wyślij').click();
  await app.settle();
  app.first('.toast-action').click();
  await app.settle();
  await app.flushTimers();
  assert.equal(app.callsTo('/api/odpowiedzi/wyslij').length, 0);
  assert.ok(app.all('.rp-composer button').every((b) => !b.disabled));
  assert.equal(app.first('.rp-draft').value, 'Treść');
});

test('wysyłka: „Wyślij i zamknij” i odznaczony cytat trafiają do serwera', async () => {
  const {app} = await openFirst();
  button(app, 'Odpowiedz').click();
  await app.settle();
  type(app, 'Treść');
  app.first('#quoteChk').checked = false;
  button(app, 'Wyślij i zamknij').click();
  await app.settle();
  await app.flushTimers();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/wyslij')[0].body, {id: 'eventy-001', ack: [], quote: false, close: true});
});

test('wysyłka przy prośbie o brak kontaktu: arkusz z potwierdzeniem, bez zgody nic nie wychodzi, ze zgodą idzie z kodem', async () => {
  const optout = () => view('eventy-001', {compose: Object.assign(view('x').compose, {needs_ack: ['optout']})});
  const declined = await openFirst({views: {'eventy-001': optout()}});
  button(declined.app, 'Odpowiedz').click();
  await declined.app.settle();
  type(declined.app, 'Potwierdzamy wypisanie.');
  button(declined.app, 'Wyślij').click();
  await declined.app.settle();
  const sheet = declined.app.first('dialog');
  assert.ok(sheet && sheet.opened && sheet.textContent.includes('zaprzestanie kontaktu'), 'arkusz ostrzeżenia');
  sheet.close('cancel');
  await declined.app.settle();
  await declined.app.flushTimers();
  assert.equal(declined.app.callsTo('/api/odpowiedzi/wyslij').length, 0);
  const accepted = await openFirst({views: {'eventy-001': optout()}});
  button(accepted.app, 'Odpowiedz').click();
  await accepted.app.settle();
  type(accepted.app, 'Potwierdzamy wypisanie.');
  button(accepted.app, 'Wyślij').click();
  await accepted.app.settle();
  accepted.app.first('dialog').close('ok');
  await accepted.app.settle();
  await accepted.app.flushTimers();
  assert.deepEqual(accepted.app.callsTo('/api/odpowiedzi/wyslij')[0].body.ack, ['optout']);
});

test('wysyłka: błąd serwera jest komunikatem z powodem, a edytor wraca do pracy', async () => {
  const {app} = await openFirst({respond: async (url) => (url === '/api/odpowiedzi/wyslij' ? {__status: 502, body: {error: 'Nie udało się wysłać: 550 odrzucone'}} : undefined)});
  button(app, 'Odpowiedz').click();
  await app.settle();
  type(app, 'Treść');
  button(app, 'Wyślij').click();
  await app.settle();
  await app.flushTimers();
  assert.ok(app.texts().includes('Nie udało się wysłać: 550 odrzucone'));
  assert.ok(app.all('.rp-composer button').every((b) => !b.disabled));
});

test('wysyłka bez treści nie rusza, a pole nie wysyła pustej odpowiedzi', async () => {
  const {app} = await openFirst();
  button(app, 'Odpowiedz').click();
  await app.settle();
  button(app, 'Wyślij').click();
  await app.settle();
  assert.ok(app.texts().includes('Najpierw napisz odpowiedź.'));
  assert.equal(app.callsTo('/api/odpowiedzi/wyslij').length, 0);
});

test('skróty edytora: r otwiera i ustawia fokus, Ctrl+Enter w polu wysyła, a poza polem nie', async () => {
  const {app} = await openFirst();
  app.key('r');
  await app.settle();
  const area = app.first('.rp-draft');
  assert.ok(area, 'edytor otwarty');
  assert.equal(app.document.activeElement, area);
  type(app, 'Treść');
  app.key('Enter', {ctrlKey: true, target: area});
  await app.settle();
  assert.ok(app.first('.toast-action'), 'rozpoczęto wysyłkę');
  const {app: other} = await openFirst();
  other.key('Enter', {ctrlKey: true});
  await other.settle();
  assert.ok(!other.first('.toast-action'));
});

test('niezapisany szkic jest zapisywany przed przeładowaniem wątku po zmianie oceny', async () => {
  const {app} = await openFirst();
  button(app, 'Odpowiedz').click();
  await app.settle();
  type(app, 'Napisane, ale jeszcze nie zapisane');
  app.all('.rp-rate button')[0].click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/szkic').map((c) => c.body.body), ['Napisane, ale jeszcze nie zapisane']);
});

test('wrogi adres i temat w nagłówku edytora są tekstem, a treść szkicu jest wartością pola, nie znacznikami', async () => {
  const hostile = view('eventy-001', {compose: Object.assign(view('x').compose, {to: EVIL, subject: EVIL, mailbox: EVIL, draft: {id: 1, body: EVIL, author: 'human', state: 'draft', error: ''}})});
  const {app} = await openFirst({views: {'eventy-001': hostile}});
  assert.ok(app.first('.rp-compose-head').textContent.includes(EVIL));
  assert.equal(app.first('.rp-draft').value, EVIL);
  assert.equal(app.all('img').length + app.all('script').length, 0);
});

// ---- raport -------------------------------------------------------------------------------------------------------------

const row2 = (key, over) => Object.assign({key, sent: 30, bounced: 3, delivered: 27, replied: 8, positive: 3, neutral: 3, negative: 2, unrated: 0, optout: 1, awaiting: 2,
  reply_rate: 8 / 27, positive_rate: 3 / 8, small_sample: false}, over || {});

const REPORT = () => ({period: '2026-09-29 – dziś', replied_companies: 7, auto_threads: 2, soft_bounces: 1, awaiting_since: null, generated_at: '2026-10-08T10:00:00',
  total: row2('razem', {sent: 60, bounced: 6, delivered: 54, replied: 12, positive: 4, neutral: 4, negative: 3, unrated: 1, optout: 2, awaiting: 3, reply_rate: 12 / 54, positive_rate: 4 / 11}),
  latency: {n: 12, median_hours: 30.5, buckets: {'do 1 dnia': 5, '2–3 dni': 4, '4–7 dni': 2, 'powyżej 7 dni': 1}},
  coverage: {configured: 3, receiving: 2, blind_mailboxes: ['trzeci@z3x.pl'], blind_mails: 10, complete: false},
  warnings: ['Odsetek odpowiedzi jest zaniżony: 10 maili wyszło ze skrzynek bez IMAP.'],
  groups: {segment: [row2('eventy'), row2('driverzy', {small_sample: true, sent: 5})], country: [row2('PL'), row2('DE')], mailbox: [row2('roman@z3x.pl')], day: []}});

async function openReport(options) {
  const o = options || {};
  const booted = await boot({search: o.search || '', respond: async (url, body) => {
    if (url === '/api/odpowiedzi/raport') return typeof o.report === 'function' ? o.report(body) : (o.report || REPORT());
    if (o.respond) return o.respond(url, body);
    return undefined;
  }});
  if (!o.search) { booted.app.el('viewReport').click(); await booted.app.settle(); }
  return booted;
}

test('raport: przełącznik pokazuje raport zamiast listy, woła serwer z okresem i zapisuje widok w adresie', async () => {
  const {app} = await openReport();
  assert.ok(app.el('split').hasAttribute('hidden'));
  assert.ok(!app.el('report').hasAttribute('hidden'));
  assert.equal(app.el('viewReport').getAttribute('aria-pressed'), 'true');
  assert.deepEqual(app.callsTo('/api/odpowiedzi/raport')[0].body, {from: '', to: '', segments: [], countries: [], mailboxes: []});
  assert.ok(app.history.replaced.pop().includes('widok=raport'));
  app.el('viewMail').click();
  assert.ok(!app.el('split').hasAttribute('hidden') && app.el('report').hasAttribute('hidden'));
  assert.ok(!app.history.replaced.pop().includes('widok'));
});

test('raport: kafelki z liczbami, odsetki z przecinkiem, ostrzeżenia o pokryciu i małej próbie', async () => {
  const {app} = await openReport();
  const text = app.el('report').textContent;
  for (const needle of ['Wysłano60', 'Dostarczono546 twardych zwrotów', 'Odpowiedzi123 czeka na nas', 'Odsetek odpowiedzi22,2%', 'Pozytywne436,4% ocenionych', 'Mediana czasu30.5 h',
    'Odsetek odpowiedzi jest zaniżony', 'Okres: 2026-09-29 – dziś', 'Nieocenione: 1', 'Prośby o wypisanie: 2', 'Lejek', 'Oceny odpowiedzi', 'Czas do pierwszej odpowiedzi']) {
    assert.ok(text.includes(needle), needle + ' w: ' + text.slice(0, 400));
  }
  assert.ok(app.all('.rp-report .banner.warn').length === 1);
});

test('raport: tabele po segmencie, kraju z nazwą po polsku i skrzynce; mała próba jest oznaczona, a pusty wymiar pominięty', async () => {
  const {app} = await openReport();
  const titles = app.all('.rp-report .card h2').map((n) => n.textContent);
  assert.deepEqual(titles.filter((t) => ['Segment', 'Kraj', 'Skrzynka', 'Dzień wysyłki'].includes(t)), ['Segment', 'Kraj', 'Skrzynka']);
  const cells = app.all('.rp-report tbody tr').map((tr) => tr.textContent);
  assert.ok(cells.some((c) => c.startsWith('Polska (PL)30272') || c.startsWith('Polska (PL)3027')), cells.join(' | '));
  assert.ok(cells.some((c) => c.startsWith('driverzy mała próba')), cells.join(' | '));
});

test('raport: długie zestawienie pokazuje początek i „Pokaż wszystkie”', async () => {
  const many = REPORT();
  many.groups.mailbox = Array.from({length: 40}, (_, i) => row2('skrzynka' + i + '@z3x.pl'));
  const {app} = await openReport({report: many});
  const mailboxTable = app.all('.rp-report .card').find((c) => c.querySelector('h2').textContent === 'Skrzynka');
  assert.equal(mailboxTable.querySelectorAll('tbody tr').length, 15);
  const more = mailboxTable.querySelector('button');
  assert.equal(more.textContent, 'Pokaż wszystkie (40)');
  more.click();
  assert.equal(mailboxTable.querySelectorAll('tbody tr').length, 40);
  assert.ok(!mailboxTable.querySelector('button'));
});

test('raport: „Przelicz” wysyła okres i filtry, a okres wraca do adresu', async () => {
  const {app} = await openReport();
  app.el('repFrom').value = '2026-09-29';
  app.el('repTo').value = '2026-10-01';
  app.all('button').find((b) => b.textContent === 'Przelicz').click();
  await app.settle();
  assert.deepEqual(app.callsTo('/api/odpowiedzi/raport').pop().body, {from: '2026-09-29', to: '2026-10-01', segments: [], countries: [], mailboxes: []});
  const url = app.history.replaced.pop();
  assert.ok(url.includes('rfrom=2026-09-29') && url.includes('rto=2026-10-01'), url);
});

test('raport: widok i okres wracają z adresu po odświeżeniu strony', async () => {
  const {app} = await openReport({search: '?widok=raport&rfrom=2026-09-01&rto=2026-09-30&seg=eventy'});
  assert.ok(!app.el('report').hasAttribute('hidden'));
  assert.deepEqual(app.callsTo('/api/odpowiedzi/raport')[0].body, {from: '2026-09-01', to: '2026-09-30', segments: ['eventy'], countries: [], mailboxes: []});
  assert.equal(app.el('repFrom').getAttribute('value'), '2026-09-01');
});

test('raport: „Pobierz CSV” prosi o plik tym samym filtrem, a skróty listy w raporcie nie działają', async () => {
  const {app} = await openReport();
  app.all('button').find((b) => b.textContent === 'Pobierz CSV').click();
  await app.settle();
  const call = app.callsTo('/api/odpowiedzi/raport-plik')[0];
  assert.deepEqual(call.body, {format: 'csv', from: '', to: '', segments: [], countries: [], mailboxes: []});
  const before = app.callsTo('/api/odpowiedzi/watek').length;
  app.key('j');
  await app.settle();
  assert.equal(app.callsTo('/api/odpowiedzi/watek').length, before);
});

test('raport: błąd serwera jest komunikatem, a wrogie dane w nazwach grup są tekstem', async () => {
  const failing = await openReport({report: () => ({__status: 400, body: {error: 'Data początkowa jest późniejsza niż końcowa.'}})});
  assert.ok(failing.app.texts().includes('Data początkowa jest późniejsza niż końcowa.'));
  const hostile = REPORT();
  hostile.groups.segment = [row2(EVIL)];
  const {app} = await openReport({report: hostile});
  assert.ok(app.el('report').textContent.includes(EVIL));
  assert.equal(app.all('img').length + app.all('script').length, 0);
});
