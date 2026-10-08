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
    messages: [message()], ratings: [], suggestion: null, statuses: [{code: 'zainteresowany', label: 'Zainteresowany', color: '#be185d'}]}, over || {});
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
