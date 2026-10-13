'use strict';
// Zakładka „Leady”: firmy z pozytywną albo neutralną odpowiedzią, z kontaktem, e-mailem i opisem działalności. Lista (filtry, sortowanie,
// liczniki) pochodzi z serwera; tu jest tylko widok i edycja. Pola pochodzą ze stron firm i z odpowiedzi osób trzecich: do DOM trafiają
// WYŁĄCZNIE jako tekst (funkcja h z common.js), a odnośniki tylko po sprawdzeniu schematu.
const el = (id) => document.getElementById(id);
const TABS = [['all', 'Wszystkie'], ['missing', 'Do uzupełnienia'], ['ai', 'Do zatwierdzenia'], ['withdrawn', 'Wycofane']];
const STAGES = [['new', 'Nowy'], ['talking', 'W rozmowie'], ['meeting', 'Umówione'], ['won', 'Wygrany'], ['lost', 'Odpadł']];
const STAGE_LABEL = Object.fromEntries(STAGES);
const SORTS = [['recent', 'Najnowsze'], ['name', 'Firma A-Z'], ['rating', 'Ocena'], ['complete', 'Najmniej kompletne']];
const RATING_LABEL = {positive: 'Pozytywna', neutral: 'Neutralna', negative: 'Negatywna'};
const REQUIRED = [['name', 'Firma'], ['website', 'Strona'], ['contact', 'Kontakt'], ['email', 'E-mail'], ['about', 'Czym się zajmuje']];
const LABEL = {name: 'Firma', website: 'Strona', contact: 'Kontakt', phone: 'Telefon', email: 'E-mail', about: 'Czym się zajmuje', note: 'Notatka'};
// [pole, etykieta, element, szerokość, podpowiedź]
const FIELDS = [
  ['name', 'Firma', 'input', '', ''], ['website', 'Strona', 'input', '', 'https://…'],
  ['contact', 'Kontakt (osoba, stanowisko)', 'input', '', 'np. Anna Nowak, dyrektor'], ['phone', 'Telefon', 'input', '', '+48 …'],
  ['email', 'E-mail', 'input', 'wide', 'adres@firma.pl'], ['about', 'Czym się zajmuje', 'textarea', 'wide', '1-2 konkretne zdania'],
  ['note', 'Notatka', 'textarea', 'wide', 'Ustalenia, następny krok…'],
];
const LIMITS = {name: 255, website: 500, contact: 255, phone: 64, email: 320, about: 400, note: 2000};
const PAGE_SIZE = 50;
const POLL_MS = 60000;
const SHORTCUTS = [['j / k', 'następny / poprzedni lead'], ['Enter', 'otwórz zaznaczony lead'], ['Esc', 'zamknij panel (albo cofnij zmianę w polu)'], ['/', 'szukaj'],
  ['?', 'ta ściąga']];

const state = {tab: 'all', q: '', segment: '', stage: '', sort: 'recent', page: 1, rows: [], total: 0, pages: 1, counts: {}, segments: [],
  selected: '', lead: null, snapshot: ''};
let listVersion = 0;
let detailVersion = 0;
let reloadTimer = null;

// ---- pomocnicze ---------------------------------------------------------------------------------------------------

function plural(n, one, few, many) {
  if (n === 1) return one;
  const last = n % 10;
  return last >= 2 && last <= 4 && (n % 100 < 10 || n % 100 >= 20) ? few : many;
}

function hostOf(url) {
  const safe = safeUrl(url);
  if (!safe) return '';
  try { return new URL(safe).hostname.replace(/^www\./, ''); } catch (error) { return ''; }
}

const aiMark = () => h('span', {class: 'ld-ai', text: 'AI', title: 'Wpisane przez AI, czeka na zatwierdzenie'});

function ratingBadge(rating, by) {
  if (!rating) return null;
  return h('span', {class: 'is-' + rating + (by === 'ai' ? ' is-ai' : '')}, h('span', {class: 'badge', text: RATING_LABEL[rating] || rating}));
}

function stageBadge(stage) {
  return h('span', {class: 'badge st-' + stage, text: STAGE_LABEL[stage] || stage});
}

function mailtoHref(email) {
  return /^[^\s@<>"']+@[^\s@<>"']+\.[^\s@<>"']+$/.test(email || '') ? 'mailto:' + email : '';
}

function telHref(phone) {
  const digits = String(phone || '').replace(/[^\d+]/g, '');
  return digits.replace(/\D/g, '').length >= 7 ? 'tel:' + digits : '';
}

async function copyText(text, what) {
  try { await navigator.clipboard.writeText(text); showToast('Skopiowano ' + what + '.', 'ok'); }
  catch (error) { showToast('Przeglądarka nie pozwoliła skopiować.', 'error'); }
}

// ---- adres w pasku (stan widoku) ------------------------------------------------------------------------------------

function syncUrl() {
  const query = new URLSearchParams();
  if (state.tab !== 'all') query.set('tab', state.tab);
  if (state.q) query.set('q', state.q);
  if (state.segment) query.set('seg', state.segment);
  if (state.stage) query.set('stage', state.stage);
  if (state.sort !== 'recent') query.set('sort', state.sort);
  if (state.selected) query.set('l', state.selected);
  const url = location.pathname + (query.toString() ? '?' + query.toString() : '');
  try { if (history.replaceState) history.replaceState({}, '', url); } catch (error) { /* adres jest tylko ułatwieniem */ }
}

function restoreFromUrl() {
  const query = new URLSearchParams(location.search);
  const tab = query.get('tab');
  state.tab = TABS.some(([code]) => code === tab) ? tab : 'all';
  state.q = query.get('q') || '';
  state.segment = query.get('seg') || '';
  state.stage = STAGE_LABEL[query.get('stage')] ? query.get('stage') : '';
  state.sort = SORTS.some(([code]) => code === query.get('sort')) ? query.get('sort') : 'recent';
  state.selected = query.get('l') || '';
  el('q').value = state.q;
  el('stageSel').value = state.stage;
  el('sortSel').value = state.sort;
}

// ---- lista ------------------------------------------------------------------------------------------------------------

function listPayload(extra) {
  return Object.assign({tab: state.tab, q: state.q, segments: state.segment ? [state.segment] : [], stages: state.stage ? [state.stage] : [],
    sort: state.sort, page: state.page, size: PAGE_SIZE}, extra || {});
}

async function loadList() {
  const version = ++listVersion;
  const {ok, data} = await api('/api/leady/lista', listPayload());
  if (version !== listVersion) return;  // nowsze żądanie już trwa albo wróciło
  if (!ok) { showToast(data.error || 'Nie udało się wczytać leadów.', 'error'); return; }
  const snapshot = JSON.stringify([data.rows, data.counts, data.segments]);
  if (snapshot === state.snapshot) return;  // nic się nie zmieniło: nie burzymy listy pod palcami
  state.snapshot = snapshot;
  Object.assign(state, {rows: data.rows, total: data.total, pages: data.pages, counts: data.counts, segments: data.segments});
  renderSegments();
  renderTabs();
  renderSummary();
  renderList();
}

function reloadSoon() {
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(loadList, 400);
}

function renderSummary() {
  const all = state.counts.all || 0;
  const parts = [all + ' ' + plural(all, 'lead', 'leady', 'leadów'), (all - (state.counts.missing || 0)) + ' kompletnych'];
  if (state.counts.ai) parts.push(state.counts.ai + ' do zatwierdzenia');
  el('summary').textContent = parts.join(' · ');
}

function renderTabs() {
  setChildren(el('tabs'), TABS.map(([code, label]) => {
    const count = state.counts[code];
    return h('button', {type: 'button', class: 'kit-chip' + (count ? '' : ' is-zero'), role: 'tab', 'aria-pressed': state.tab === code ? 'true' : 'false',
      onclick: () => switchTab(code)}, label, count ? h('span', {class: 'n', text: String(count)}) : null);
  }));
}

function renderSegments() {
  const names = state.segments.includes(state.segment) || !state.segment ? state.segments : [...state.segments, state.segment];
  setChildren(el('segmentSel'), [h('option', {value: '', text: 'Wszystkie segmenty'}), ...names.map((name) => h('option', {value: name, text: name}))]);
  el('segmentSel').value = state.segment;
}

function fieldOrMissing(row, name, extra) {
  const value = row[name];
  if (!value) return [h('span', {class: 'ld-miss', text: 'uzupełnij'})];
  return [h('span', {class: 'ld-val', text: value}), row.src && row.src[name] === 'ai' ? aiMark() : null, extra];
}

function firmCell(row) {
  const host = hostOf(row.website);
  const link = safeUrl(row.website);
  const flags = [ratingBadge(row.rating, row.rating_by)];
  if (row.optout) flags.push(h('span', {class: 'badge is-negative', text: 'Prosił o brak kontaktu'}));
  if (row.do_not_contact) flags.push(h('span', {class: 'badge is-negative', text: 'Nie kontaktować'}));
  if (!row.active) flags.push(h('span', {class: 'badge', text: 'Wycofany'}));
  return h('td', {class: 'c-firm'}, h('span', {class: 'ld-firm', text: row.name || row.id}),
    host ? h('span', {class: 'ld-domain'}, host, link ? h('a', {class: 'site-link', href: link, target: '_blank', rel: 'noopener noreferrer',
      title: 'Otwórz stronę', 'aria-label': 'Otwórz stronę firmy', onclick: (event) => event.stopPropagation()}) : null) : null,
    h('span', {class: 'ld-flags'}, flags));
}

function emailCell(row) {
  const copy = row.email ? h('button', {type: 'button', class: 'btn plain ld-copy', text: 'Kopiuj', 'aria-label': 'Kopiuj e-mail',
    onclick: (event) => { event.stopPropagation(); copyText(row.email, 'e-mail'); }}) : null;
  return h('td', {class: 'c-email', 'data-label': 'E-mail'}, h('div', {class: 'ld-line'}, fieldOrMissing(row, 'email', copy)));
}

function pips(row) {
  const missing = row.missing || [];
  const label = (5 - missing.length) + ' z 5 pól' + (missing.length ? '. Brakuje: ' + missing.map((n) => LABEL[n]).join(', ') : '');
  return h('span', {class: 'ld-pips', role: 'img', 'aria-label': label, title: label}, REQUIRED.map(([name]) =>
    h('span', {class: 'ld-pip' + (row[name] ? (row.src && row.src[name] === 'ai' ? ' is-ai' : ' is-on') : '')})));
}

function renderRow(row) {
  const contact = h('td', {class: 'c-contact', 'data-label': 'Kontakt'}, h('div', {class: 'ld-line'}, fieldOrMissing(row, 'contact')),
    row.phone ? h('span', {class: 'ld-sub2', text: row.phone}) : null);
  const about = h('td', {class: 'c-about', 'data-label': 'Czym się zajmuje'}, row.about
    ? h('div', {class: 'ld-line'}, h('span', {class: 'ld-about', text: row.about}), row.src && row.src.about === 'ai' ? aiMark() : null)
    : h('span', {class: 'ld-miss', text: 'uzupełnij'}));
  const fields = h('td', {class: 'c-fields', 'data-label': 'Pola'}, pips(row), h('span', {class: 'ld-count', text: (5 - (row.missing || []).length) + '/5'}));
  const stage = h('td', {class: 'c-stage', 'data-label': 'Etap'}, stageBadge(row.stage));
  const tr = h('tr', {class: 'ld-row', tabindex: '0', 'data-id': row.id, 'aria-current': row.id === state.selected ? 'true' : 'false',
    onclick: () => openLead(row.id)}, firmCell(row), contact, emailCell(row), about, fields, stage);
  tr.addEventListener('keydown', (event) => { if (event.key === 'Enter' && event.target === tr) openLead(row.id); });
  return tr;
}

function emptyState() {
  const filtered = state.q || state.segment || state.stage;
  if (filtered) return h('div', {class: 'ld-empty'}, h('strong', {text: 'Nic nie pasuje do filtrów'}), 'Zmień wyszukiwanie albo wyczyść filtry.');
  if (state.tab !== 'all') return h('div', {class: 'ld-empty'}, h('strong', {text: 'Pusto w tym widoku'}), 'Nic tu teraz nie czeka.');
  return h('div', {class: 'ld-empty'}, h('strong', {text: 'Nie ma jeszcze leadów'}),
    'Lead pojawia się, gdy odpowiedź firmy dostanie ocenę pozytywną lub neutralną w zakładce ', h('a', {href: '/odpowiedzi', text: 'Odpowiedzi'}), '.');
}

function renderList() {
  if (!state.rows.length) { setChildren(el('list'), [emptyState()]); return; }
  const head = h('tr', null, h('th', {class: 'c-firm', text: 'Firma'}), h('th', {class: 'c-contact', text: 'Kontakt'}), h('th', {class: 'c-email', text: 'E-mail'}),
    h('th', {class: 'c-about', text: 'Czym się zajmuje'}), h('th', {class: 'c-fields', text: 'Pola'}), h('th', {class: 'c-stage', text: 'Etap'}));
  const nodes = [h('div', {class: 'table-wrap'}, h('table', {class: 'data-table ld-table'}, h('thead', null, head), h('tbody', null, state.rows.map(renderRow))))];
  if (state.pages > 1) {
    nodes.push(h('div', {class: 'ld-pager'},
      h('button', {type: 'button', class: 'btn', text: 'Poprzednia', disabled: state.page <= 1, onclick: () => goPage(state.page - 1)}),
      h('span', {text: 'Strona ' + state.page + ' z ' + state.pages}),
      h('button', {type: 'button', class: 'btn', text: 'Następna', disabled: state.page >= state.pages, onclick: () => goPage(state.page + 1)})));
  }
  setChildren(el('list'), nodes);
}

function goPage(page) {
  state.page = page;
  state.snapshot = '';
  loadList();
  window.scrollTo && window.scrollTo(0, 0);
}

function refilter() {
  state.page = 1;
  state.snapshot = '';
  syncUrl();
  loadList();
}

function switchTab(code) {
  state.tab = code;
  refilter();
}

function markSelected() {
  for (const node of document.querySelectorAll('.ld-row')) node.setAttribute('aria-current', node.getAttribute('data-id') === state.selected ? 'true' : 'false');
}

function moveCursor(delta) {
  const rows = Array.from(document.querySelectorAll('.ld-row'));
  if (!rows.length) return;
  const at = rows.indexOf(document.activeElement);
  const next = Math.max(0, Math.min(rows.length - 1, at < 0 ? (delta > 0 ? 0 : rows.length - 1) : at + delta));
  rows[next].focus();
  if (rows[next].scrollIntoView) rows[next].scrollIntoView({block: 'nearest'});
}

// ---- panel szczegółów ---------------------------------------------------------------------------------------------------

function showDrawer(on) {
  for (const id of ['drawer', 'scrim']) { if (on) el(id).removeAttribute('hidden'); else el(id).setAttribute('hidden', ''); }
  el('leads').classList.toggle('has-drawer', on);
}

async function openLead(id) {
  const version = ++detailVersion;
  state.selected = id;
  markSelected();
  syncUrl();
  showDrawer(true);
  const {ok, data} = await api('/api/leady/lead', {id});
  if (version !== detailVersion) return;
  if (!ok) { setChildren(el('drawer'), [h('div', {class: 'ld-empty', text: data.error || 'Nie udało się wczytać leada.'})]); return; }
  state.lead = data.lead;
  renderDrawer(data.lead);
}

function closeDrawer() {
  detailVersion += 1;
  state.selected = '';
  state.lead = null;
  markSelected();
  syncUrl();
  showDrawer(false);
}

function threadCard(thread) {
  const when = thread.last_in_at;
  return h('article', {class: 'ld-thread'},
    h('div', {class: 'ld-thread-head'}, ratingBadge(thread.rating, thread.rating_by), thread.optout ? h('span', {class: 'badge is-negative', text: 'Prosił o brak kontaktu'}) : null,
      h('span', {class: 'when', text: longDate(when), title: when || ''})),
    thread.last_snippet ? h('p', {text: (thread.last_from ? thread.last_from + ': ' : '') + thread.last_snippet}) : null,
    thread.rating_note ? h('p', {class: 'note', text: 'Notatka do oceny: ' + thread.rating_note}) : null,
    h('div', null, h('a', {class: 'btn plain', href: '/odpowiedzi?w=' + encodeURIComponent(thread.id), text: 'Otwórz wątek'})));
}

function quickActions(lead) {
  const site = safeUrl(lead.website);
  const mail = mailtoHref(lead.email);
  const tel = telHref(lead.phone);
  return h('div', {class: 'ld-quick'},
    site ? h('a', {class: 'btn', href: site, target: '_blank', rel: 'noopener noreferrer', text: 'Otwórz stronę'}) : null,
    mail ? h('a', {class: 'btn', href: mail, text: 'Napisz'}) : null,
    tel ? h('a', {class: 'btn', href: tel, text: 'Zadzwoń'}) : null,
    lead.email ? h('button', {type: 'button', class: 'btn', text: 'Kopiuj e-mail', onclick: () => copyText(lead.email, 'e-mail')}) : null);
}

function drawerBanners(lead) {
  const banners = [];
  if (!lead.active) banners.push(h('div', {class: 'banner warn', text: 'Ten lead jest wycofany: żaden wątek nie ma już oceny pozytywnej ani neutralnej. Dane zostały zachowane.'}));
  if (lead.optout) banners.push(h('div', {class: 'banner error', text: 'Firma prosiła o zaprzestanie kontaktu.'}));
  if (lead.do_not_contact) banners.push(h('div', {class: 'banner warn', text: 'Status firmy w Wysyłce: Nie kontaktować.'}));
  if (lead.active && lead.rating_by === 'ai') banners.push(h('div', {class: 'banner', text: 'Ocena odpowiedzi pochodzi od AI i czeka na przegląd w zakładce Odpowiedzi.'}));
  return banners;
}

function stageControl(lead) {
  return h('div', {class: 'ld-section'}, h('h3', {text: 'Etap'}),
    h('div', {class: 'ld-stages', role: 'group', 'aria-label': 'Etap leada', id: 'stageGroup'}, STAGES.map(([code, label]) =>
      h('button', {type: 'button', class: 'st-' + code, 'data-stage': code, 'aria-pressed': lead.stage === code ? 'true' : 'false', onclick: () => saveField(lead.id, 'stage', code), text: label}))));
}

function fieldBlock(lead, [name, label, tag, width, placeholder]) {
  const input = h(tag, {id: 'f_' + name, 'data-field': name, maxlength: LIMITS[name], placeholder, autocomplete: 'off', spellcheck: tag === 'textarea' ? 'true' : 'false',
    rows: tag === 'textarea' ? (name === 'about' ? '3' : '4') : null, type: tag === 'input' ? (name === 'email' ? 'email' : (name === 'phone' ? 'tel' : 'text')) : null});
  input.value = lead[name] || '';
  input.baseline = input.value;  // zapisana wartość pola (w atrybucie nie trzymamy treści z cudzych stron)
  input.addEventListener('blur', () => saveField(lead.id, name, input.value));
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter' && tag === 'input') input.blur(); });
  if (tag === 'textarea' && typeof autosize === 'function') autosize(input, 220);
  return h('div', {class: 'ld-field' + (width ? ' ' + width : ''), 'data-wrap': name}, h('div', {class: 'ld-label', 'data-marks': name}, h('label', {for: 'f_' + name, text: label})), input);
}

// Znacznik „AI” i przycisk „Zatwierdź” przy etykietach pól: odświeżane bez przebudowy pól, żeby nie zgubić fokusu.
function refreshMarks(lead) {
  const pending = lead.pending || [];
  for (const [name] of FIELDS) {
    const wrap = document.querySelector('[data-wrap="' + name + '"]');
    const marks = document.querySelector('[data-marks="' + name + '"]');
    if (!wrap || !marks) continue;
    wrap.classList.toggle('is-ai', pending.includes(name));
    for (const old of marks.querySelectorAll('.ld-ai,.ld-approve')) old.remove();
    if (pending.includes(name)) {
      marks.appendChild(aiMark());
      marks.appendChild(h('button', {type: 'button', class: 'btn plain ld-approve', text: 'Zatwierdź', title: 'Uznaj wpis AI za sprawdzony',
        onclick: () => approveFields(lead.id, [name])}));
    }
  }
  const stages = document.querySelectorAll('#stageGroup button');
  for (const button of stages) button.setAttribute('aria-pressed', button.getAttribute('data-stage') === lead.stage ? 'true' : 'false');
  const all = document.querySelector('.ld-approve-all');
  if (all) { if (pending.length > 1) { all.removeAttribute('hidden'); all.textContent = 'Zatwierdź wszystkie wpisy AI (' + pending.length + ')'; } else all.setAttribute('hidden', ''); }
  const title = document.querySelector('.ld-dtitle h2');
  if (title) title.textContent = lead.name || lead.id;
}

function renderDrawer(lead) {
  const sub = [lead.segment, lead.country, lead.last_in_at ? 'ostatnia odpowiedź ' + longDate(lead.last_in_at) : ''].filter(Boolean).join(' · ');
  const head = h('div', {class: 'ld-dhead'},
    h('div', {class: 'ld-dtitle'}, h('h2', {text: lead.name || lead.id}),
      h('button', {type: 'button', class: 'ld-close', 'aria-label': 'Zamknij panel', title: 'Zamknij (Esc)', onclick: closeDrawer})),
    sub ? h('div', {class: 'ld-dsub', text: sub}) : null, quickActions(lead));
  const base = lead.base_description ? h('div', {class: 'ld-base'}, h('span', {text: 'Opis z bazy: ' + lead.base_description}),
    h('button', {type: 'button', class: 'btn plain', text: 'Użyj', onclick: () => saveField(lead.id, 'about', lead.base_description.slice(0, LIMITS.about))})) : null;
  const threads = (lead.threads || []).map(threadCard);
  const body = h('div', {class: 'ld-dbody'}, ...drawerBanners(lead), stageControl(lead),
    h('div', {class: 'ld-section'}, h('h3', {text: 'Dane leada'}),
      h('div', {class: 'ld-form'}, FIELDS.map((spec) => fieldBlock(lead, spec))), base,
      h('button', {type: 'button', class: 'btn ld-approve-all', hidden: true, onclick: () => approveFields(lead.id, (state.lead && state.lead.pending) || [])})),
    h('div', {class: 'ld-section'}, h('h3', {text: 'Skąd ten lead'}), h('div', {class: 'ld-threads'}, threads.length ? threads : [h('p', {class: 'ld-hint', text: 'Brak wątków z odpowiedzią.'})])),
    h('div', {class: 'ld-meta', text: lead.updated_at ? 'Ostatnia zmiana: ' + longDate(lead.updated_at) + (lead.updated_by === 'ai' ? ' (AI)' : (lead.updated_by === 'human' ? ' (ręcznie)' : '')) : ''}));
  setChildren(el('drawer'), [head, body]);
  refreshMarks(lead);
}

// ---- zapis ------------------------------------------------------------------------------------------------------------------

function syncInputs(lead) {
  for (const [name] of FIELDS) {
    const input = document.getElementById('f_' + name);
    if (!input || document.activeElement === input) continue;
    input.value = lead[name] || '';
    input.baseline = input.value;
  }
}

// Wynik zapisu z serwera wchodzi do widoku: wiersz listy, wiersz w stanie i (jeśli otwarty) panel.
function applyLead(lead) {
  const fresh = Object.assign({}, state.lead && state.lead.id === lead.id ? state.lead : {}, lead);
  if (state.lead && state.lead.id === lead.id) { state.lead = fresh; syncInputs(fresh); refreshMarks(fresh); }
  const at = state.rows.findIndex((row) => row.id === lead.id);
  if (at >= 0) { state.rows[at] = Object.assign({}, state.rows[at], lead); state.snapshot = ''; renderList(); markSelected(); }
  reloadSoon();
}

async function saveField(id, name, value) {
  const current = state.lead && state.lead.id === id ? state.lead : null;
  if (current && String(current[name] || '') === String(value).trim()) return;
  const {ok, data} = await api('/api/leady/zapisz', {id, changes: {[name]: value}});
  if (!ok) {
    showToast(data.error || 'Nie udało się zapisać.', 'error');
    const input = document.getElementById('f_' + name);
    if (input && current) { input.value = current[name] || ''; }
    return;
  }
  applyLead(data.lead);
  showToast('Zapisano: ' + (LABEL[name] || 'Etap'), 'ok', {action: {label: 'Cofnij', onClick: () => undo(id, data.previous)}});
}

async function approveFields(id, fields) {
  if (!fields.length) return;
  const {ok, data} = await api('/api/leady/zatwierdz', {id, fields});
  if (!ok) { showToast(data.error || 'Nie udało się zatwierdzić.', 'error'); return; }
  applyLead(data.lead);
  showToast('Zatwierdzono ' + data.approved.length + ' ' + plural(data.approved.length, 'pole', 'pola', 'pól') + '.', 'ok', {action: {label: 'Cofnij', onClick: () => undo(id, data.previous)}});
}

async function undo(id, previous) {
  const {ok, data} = await api('/api/leady/cofnij', {id, previous});
  if (!ok) { showToast(data.error || 'Nie udało się cofnąć.', 'error'); return; }
  applyLead(data.lead);
  showToast('Cofnięto.', 'ok');
}

async function exportCsv() {
  const payload = listPayload();
  delete payload.page;
  delete payload.size;
  const result = await downloadFile('/api/leady/eksport', payload, 'leady.csv');
  showToast(result.ok ? 'Zapisano plik: ' + result.name : (result.error || 'Nie udało się przygotować pliku.'), result.ok ? 'ok' : 'error');
}

// ---- podpięcie --------------------------------------------------------------------------------------------------------

for (const [code, label] of STAGES) el('stageSel').appendChild(h('option', {value: code, text: label}));
for (const [code, label] of SORTS) el('sortSel').appendChild(h('option', {value: code, text: label}));

let searchTimer = null;
el('q').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = el('q').value.trim(); refilter(); }, 250);
});
el('stageSel').addEventListener('change', () => { state.stage = el('stageSel').value; refilter(); });
el('segmentSel').addEventListener('change', () => { state.segment = el('segmentSel').value; refilter(); });
el('sortSel').addEventListener('change', () => { state.sort = el('sortSel').value; refilter(); });
el('exportBtn').addEventListener('click', exportCsv);
el('helpBtn').addEventListener('click', () => showShortcutHelp(SHORTCUTS));
el('scrim').addEventListener('click', closeDrawer);

// Esc: w polu z niezapisaną zmianą cofa ją do zapisanej wartości, w pozostałych przypadkach zamyka panel.
function onEscape() {
  const active = document.activeElement;
  const isField = Boolean(active) && typeof active.baseline === 'string';
  if (isField && active.value !== active.baseline) { active.value = active.baseline; return; }
  if (isField && active.blur) active.blur();
  if (state.selected) closeDrawer();
}

bindShortcuts({j: () => moveCursor(1), k: () => moveCursor(-1), '/': () => el('q').focus(), '?': () => showShortcutHelp(SHORTCUTS), Escape: onEscape});

pollWhileVisible(loadList, {interval: POLL_MS});

async function start() {
  restoreFromUrl();
  renderTabs();
  await loadList();
  if (state.selected) openLead(state.selected);
}

start();
