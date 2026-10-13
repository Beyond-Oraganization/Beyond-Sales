'use strict';
// Zakładka „Odpowiedzi”: lista rozmów | wątek, ocena, status, pobieranie poczty. Treść wiadomości pochodzi od osób trzecich:
// do DOM trafia WYŁĄCZNIE przez textContent / węzły tekstowe (funkcja h), nigdy przez innerHTML ani atrybuty z danych.
const el = (id) => document.getElementById(id);
const TABS = [['todo', 'Do odpowiedzi'], ['all', 'Wszystkie'], ['positive', 'Pozytywne'], ['neutral', 'Neutralne'], ['negative', 'Negatywne'],
  ['unrated', 'Nieocenione'], ['machine', 'Zwroty i automaty'], ['unassigned', 'Nieprzypisane']];
const RATINGS = [['positive', 'Pozytywna', '1'], ['neutral', 'Neutralna', '2'], ['negative', 'Negatywna', '3']];
const RATING_LABEL = Object.fromEntries(RATINGS.map(([code, label]) => [code, label]));
const KIND_LABEL = {auto: 'Autoodpowiedź', bounce: 'Zwrot poczty'};
const FETCH_POLL_MS = 2000;
const SEND_DELAY_MS = 6000;  // „Cofnij” po kliknięciu Wyślij: wysyłka rusza dopiero po tym czasie
const DRAFT_SAVE_MS = 800;
const SHORTCUTS = [['j / k', 'następna / poprzednia rozmowa'], ['Enter', 'otwórz zaznaczoną rozmowę'], ['u', 'wróć do listy'],
  ['1  2  3', 'ocena: pozytywna, neutralna, negatywna'], ['0', 'usuń ocenę'], ['e', 'zamknij / otwórz wątek ponownie'],
  ['r', 'odpowiedz (przejdź do edytora)'], ['Ctrl/⌘ + Enter', 'wyślij odpowiedź'], ['Esc', 'wyjdź z edytora'],
  ['/', 'szukaj'], ['g', 'pobierz pocztę teraz'], ['?', 'ta ściąga']];

const state = {
  tab: 'todo', q: '', mailboxes: [], segments: [], countries: [], from: '', to: '', page: 1,
  rows: [], total: 0, pages: 1, counts: {}, selected: '', detail: null, overview: null, known: null, advance: true,
  view: 'mail', repFrom: '', repTo: '', report: null,
};
let listVersion = 0;
let detailVersion = 0;
let lastAutoFetch = 0;
const composer = {open: {}, timer: null, pending: null, text: null, status: null};  // open: {id wątku: true}

// ---- pomocnicze ---------------------------------------------------------------------------------------------------

function pluralNew(n) {
  if (n === 1) return '1 nowa odpowiedź';
  const last = n % 10;
  return n + (last >= 2 && last <= 4 && (n % 100 < 10 || n % 100 >= 20) ? ' nowe odpowiedzi' : ' nowych odpowiedzi');
}

function statusBadge(code) {
  const meta = (APP.statusMeta || {})[code];
  const badge = h('span', {class: 'badge', text: meta ? meta[0] : String(code || '')});
  if (meta) badge.style.setProperty('--c', meta[1]);
  return badge;
}

function ratingBadge(rating, by) {
  if (!rating) return null;
  return h('span', {class: 'is-' + rating + (by === 'ai' ? ' is-ai' : '')}, h('span', {class: 'badge', text: RATING_LABEL[rating] || rating}));
}

// ---- adres w pasku (stan widoku) ------------------------------------------------------------------------------------

function syncUrl(push) {
  const query = new URLSearchParams();
  if (state.tab !== 'todo') query.set('tab', state.tab);
  if (state.q) query.set('q', state.q);
  if (state.mailboxes.length) query.set('mb', state.mailboxes.join(','));
  if (state.segments.length) query.set('seg', state.segments.join(','));
  if (state.countries.length) query.set('cty', state.countries.join(','));
  if (state.from) query.set('from', state.from);
  if (state.to) query.set('to', state.to);
  if (state.selected) query.set('w', state.selected);
  if (state.view === 'report') {
    query.set('widok', 'raport');
    if (state.repFrom) query.set('rfrom', state.repFrom);
    if (state.repTo) query.set('rto', state.repTo);
  }
  const url = location.pathname + (query.toString() ? '?' + query.toString() : '');
  try {
    if (push && history.pushState) history.pushState({w: state.selected}, '', url);
    else if (history.replaceState) history.replaceState({w: state.selected}, '', url);
  } catch (error) { /* adres jest tylko ułatwieniem */ }
}

function restoreFromUrl() {
  const query = new URLSearchParams(location.search);
  const list = (name) => (query.get(name) || '').split(',').filter(Boolean);
  const tab = query.get('tab');
  state.tab = TABS.some(([code]) => code === tab) ? tab : 'todo';
  state.q = query.get('q') || '';
  state.mailboxes = list('mb');
  state.segments = list('seg');
  state.countries = list('cty');
  state.from = query.get('from') || '';
  state.to = query.get('to') || '';
  state.selected = query.get('w') || '';
  state.view = query.get('widok') === 'raport' ? 'report' : 'mail';
  state.repFrom = query.get('rfrom') || '';
  state.repTo = query.get('rto') || '';
  el('q').value = state.q;
  el('fromDay').value = state.from;
  el('toDay').value = state.to;
}

// ---- lista ------------------------------------------------------------------------------------------------------------

function listPayload() {
  return {tab: state.tab, q: state.q, mailboxes: state.mailboxes, segments: state.segments, countries: state.countries,
    from: state.from, to: state.to, page: state.page, size: 50};
}

async function loadList() {
  const version = ++listVersion;
  const {ok, data} = await api('/api/odpowiedzi/lista', listPayload());
  if (version !== listVersion) return;  // nowsze żądanie już trwa albo wróciło
  if (!ok) { showToast(data.error || 'Nie udało się wczytać listy.', 'error'); return; }
  Object.assign(state, {rows: data.rows, total: data.total, pages: data.pages, counts: data.counts});
  renderTabs();
  renderRows();
}

function renderTabs() {
  setChildren(el('tabs'), TABS.map(([code, label]) => {
    const count = state.counts[code];
    return h('button', {type: 'button', class: 'kit-chip', role: 'tab', 'aria-pressed': state.tab === code ? 'true' : 'false',
      onclick: () => switchTab(code)}, label, count ? h('span', {class: 'n', text: String(count)}) : null);
  }));
}

function rowKey(row) { return String(row.id); }

function renderRow(row) {
  const unassigned = Boolean(row.unassigned);
  const company = unassigned ? (row.from_name || row.from_addr || 'Nieznany nadawca') : (row.company || row.id);
  const snippet = unassigned ? (row.snippet || row.subject || '') : ((row.last_direction === 'out' ? 'Ty: ' : (row.last_from ? row.last_from + ': ' : '')) + (row.last_snippet || ''));
  const meta = [];
  if (!unassigned) {
    if (row.rating) meta.push(ratingBadge(row.rating, row.rating_by));
    if (row.optout) meta.push(h('span', {class: 'badge is-negative', text: 'Wypisanie'}));
    if (row.weak) meta.push(h('span', {class: 'badge', text: 'Sprawdź przypisanie'}));
    if (row.other_mailbox) meta.push(h('span', {class: 'badge', text: 'Inna skrzynka'}));
    if ((state.overview && state.overview.mailboxes && state.overview.mailboxes.configured > 1) && row.mailbox) meta.push(h('span', {class: 'badge', text: row.mailbox}));
  }
  const active = rowKey(row) === state.selected;
  return h('button', {type: 'button', class: 'rp-row' + (row.unread ? ' is-unread' : ''), 'aria-current': active ? 'true' : 'false',
    'data-id': rowKey(row), onclick: () => openRow(row)},
  h('span', {class: 'rp-dot', 'aria-hidden': 'true'}),
  h('span', {class: 'rp-main'},
    h('span', {class: 'rp-line'}, h('span', {class: 'rp-company', text: company}),
      h('span', {class: 'rp-date', text: shortDate(unassigned ? row.date_at : row.last_activity_at), title: longDate(unassigned ? row.date_at : row.last_activity_at)})),
    h('span', {class: 'rp-snippet', text: snippet}),
    meta.length ? h('span', {class: 'rp-meta'}, meta) : null));
}

function renderRows() {
  if (!state.rows.length) {
    const message = state.q || state.mailboxes.length || state.segments.length || state.countries.length || state.from || state.to
      ? 'Nic nie pasuje do filtrów.' : (state.tab === 'todo' ? 'Nic nie czeka na odpowiedź.' : 'Brak rozmów w tym widoku.');
    setChildren(el('list'), [h('div', {class: 'rp-empty', text: message})]);
    return;
  }
  const nodes = state.rows.map(renderRow);
  if (state.pages > 1) {
    nodes.push(h('div', {class: 'rp-pager'},
      h('button', {type: 'button', class: 'btn', text: 'Poprzednia', disabled: state.page <= 1, onclick: () => goPage(state.page - 1)}),
      h('span', {text: 'Strona ' + state.page + ' z ' + state.pages}),
      h('button', {type: 'button', class: 'btn', text: 'Następna', disabled: state.page >= state.pages, onclick: () => goPage(state.page + 1)})));
  }
  setChildren(el('list'), nodes);
}

function goPage(page) {
  state.page = page;
  loadList();
  el('list').scrollTop = 0;
}

function switchTab(code) {
  state.tab = code;
  state.page = 1;
  syncUrl(false);
  loadList();
}

function markSelected() {
  for (const node of el('list').querySelectorAll ? el('list').querySelectorAll('.rp-row') : []) {
    node.setAttribute('aria-current', node.getAttribute('data-id') === state.selected ? 'true' : 'false');
  }
}

// ---- wątek --------------------------------------------------------------------------------------------------------------

function openRow(row) {
  if (row.unassigned) openUnassigned(row.id);
  else openThread(row.id, true);
}

function showDetailView(on) {
  el('split').setAttribute('data-view', on ? 'detail' : 'list');
}

async function openThread(id, push) {
  if (state.detail && state.detail.thread && String(state.detail.thread.id) === String(id) && !composer.pending) await flushDraft();
  else if (state.detail && composer.text) await flushDraft();
  const version = ++detailVersion;
  state.selected = String(id);
  markSelected();
  showDetailView(true);
  syncUrl(Boolean(push));
  const {ok, data} = await api('/api/odpowiedzi/watek', {id: String(id)});
  if (version !== detailVersion) return;
  if (!ok) { setChildren(el('detail'), [h('div', {class: 'rp-empty', text: data.error || 'Nie udało się wczytać rozmowy.'})]); return; }
  state.detail = data;
  renderThread(data);
  if (data.thread.unread) markRead(data.thread.id);
}

async function openUnassigned(id) {
  const version = ++detailVersion;
  state.selected = String(id);
  markSelected();
  showDetailView(true);
  syncUrl(true);
  const {ok, data} = await api('/api/odpowiedzi/wiadomosc', {id: Number(id)});
  if (version !== detailVersion) return;
  if (!ok) { setChildren(el('detail'), [h('div', {class: 'rp-empty', text: data.error || 'Nie udało się wczytać wiadomości.'})]); return; }
  state.detail = null;
  renderUnassigned(data.message);
}

async function markRead(id) {
  await api('/api/odpowiedzi/akcja', {id, action: 'read'});
  const row = state.rows.find((r) => r.id === id);
  if (row) { row.unread = false; renderRows(); }
  refreshOverview();
}

function messageCard(message, expanded) {
  const ours = message.direction === 'out';
  const who = ours ? 'Ty' : (message.from_name || message.from_addr || 'Nieznany nadawca');
  const label = KIND_LABEL[message.kind];
  const head = h('button', {type: 'button', class: 'rp-msg-head', 'aria-expanded': expanded ? 'true' : 'false'},
    h('span', {class: 'rp-from', text: who}),
    ours && message.state && message.state !== 'sent' && message.state !== 'received' ? h('span', {class: 'badge', text: message.state === 'draft' ? 'Szkic' : message.state}) : null,
    label ? h('span', {class: 'badge', text: label + (message.bounce_type === 'hard' ? ' (trwały)' : (message.bounce_type === 'soft' ? ' (czasowy)' : ''))}) : null,
    message.optout_hint ? h('span', {class: 'badge is-negative', text: 'Prośba o wypisanie?'}) : null,
    h('span', {class: 'rp-when', text: longDate(message.date), title: message.date || ''}),
    h('span', {class: 'rp-msg-preview', text: (message.new || '').replace(/\s+/g, ' ').slice(0, 140)}));
  const body = h('div', {class: 'rp-msg-body'}, h('pre', {class: 'rp-text', text: message.new || '(pusta wiadomość)'}));
  if (message.quoted) {
    const quote = h('pre', {class: 'rp-quote', text: message.quoted, hidden: true});
    const lines = message.quoted.split('\n').length;
    const toggle = h('button', {type: 'button', class: 'btn plain rp-quote-toggle', text: '··· cytat (' + lines + ' ' + (lines === 1 ? 'linia' : 'linii') + ')',
      'aria-expanded': 'false'});
    toggle.addEventListener('click', () => {
      const open = quote.hasAttribute('hidden');
      if (open) quote.removeAttribute('hidden'); else quote.setAttribute('hidden', '');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    body.appendChild(toggle);
    body.appendChild(quote);
  }
  if (message.attachments && message.attachments.length) {
    body.appendChild(h('div', {class: 'rp-files'}, message.attachments.map((file) => h('span', {class: 'rp-file', text: (file.name || 'plik') + (file.size ? ' · ' + Math.max(1, Math.round(file.size / 1024)) + ' KB' : '')}))));
  }
  const card = h('article', {class: 'rp-msg' + (ours ? ' is-ours' : '') + (expanded ? '' : ' is-collapsed')}, head, body);
  head.addEventListener('click', () => {
    const collapsed = card.classList.contains('is-collapsed');
    card.classList.toggle('is-collapsed', !collapsed);
    head.setAttribute('aria-expanded', collapsed ? 'true' : 'false');
  });
  return card;
}

function originCard(origin) {
  const head = h('button', {type: 'button', class: 'rp-msg-head', 'aria-expanded': 'false'},
    h('span', {class: 'rp-from', text: 'Nasz mail'}), h('span', {class: 'rp-when', text: longDate(origin.sent_at)}),
    h('span', {class: 'rp-msg-preview', text: origin.subject || ''}));
  const card = h('article', {class: 'rp-msg is-ours is-collapsed'}, head,
    h('div', {class: 'rp-msg-body'}, h('pre', {class: 'rp-text', text: (origin.subject ? origin.subject + '\n\n' : '') + (origin.text || '')})));
  head.addEventListener('click', () => {
    const collapsed = card.classList.contains('is-collapsed');
    card.classList.toggle('is-collapsed', !collapsed);
    head.setAttribute('aria-expanded', collapsed ? 'true' : 'false');
  });
  return card;
}

function ratingControl(thread) {
  return h('div', {class: 'rp-rate', role: 'group', 'aria-label': 'Ocena odpowiedzi'}, RATINGS.map(([code, label, key]) => {
    const button = h('button', {type: 'button', class: 'is-' + code, 'aria-pressed': thread.rating === code ? 'true' : 'false', title: 'Skrót: ' + key,
      onclick: () => rate(thread.rating === code ? null : code)}, label);
    return button;
  }));
}

function menuItem(text, action) {
  return h('button', {type: 'button', text, onclick: () => { closeMenus(); action(); }});
}

function closeMenus() {
  for (const node of document.querySelectorAll ? document.querySelectorAll('.rp-menu') : []) node.setAttribute('hidden', '');
}

function threadMenu(thread) {
  const menu = h('div', {class: 'rp-menu', hidden: true, role: 'menu'},
    menuItem(thread.closed ? 'Otwórz wątek ponownie' : 'Zamknij wątek', () => stateAction(thread.closed ? 'reopen' : 'close')),
    menuItem('Oznacz jako nieprzeczytany', () => stateAction('unread')),
    menuItem(thread.optout ? 'Zdejmij znacznik „prośba o wypisanie”' : 'Oznacz jako prośbę o wypisanie', () => stateAction(thread.optout ? 'no_optout' : 'optout')),
    h('a', {href: '/?q=' + encodeURIComponent(thread.company || thread.id), text: 'Pokaż firmę w Wysyłce'}));
  const button = h('button', {type: 'button', class: 'btn rp-more', 'aria-label': 'Więcej działań', 'aria-expanded': 'false'});
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    const open = menu.hasAttribute('hidden');
    closeMenus();
    if (open) menu.removeAttribute('hidden');
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  return h('div', {class: 'rp-menu-wrap'}, button, menu);
}

function renderThread(view) {
  const thread = view.thread;
  const url = safeUrl(view.origin && view.origin.website);
  const sub = [view.origin && view.origin.email, thread.mailbox, thread.country].filter(Boolean).join(' · ');
  const aiNote = thread.rating_by === 'ai' && thread.rating
    ? h('span', {class: 'rp-ai'}, h('span', {text: 'Oceniła AI' + (thread.rating_note ? ': ' + thread.rating_note : '')}),
      h('button', {type: 'button', class: 'btn plain', text: 'Zatwierdź', onclick: () => confirmAiRating(thread)})) : null;
  const header = h('header', {class: 'rp-thead'},
    h('div', {class: 'rp-title'},
      h('button', {type: 'button', class: 'btn plain split-back', text: '‹ Lista', onclick: () => backToList()}),
      h('h2', {text: thread.company || thread.id}),
      url ? h('a', {class: 'site-link', href: url, target: '_blank', rel: 'noopener noreferrer', 'aria-label': 'Strona firmy'}) : null,
      statusBadge(thread.mail_status), thread.closed ? h('span', {class: 'badge', text: 'Zamknięty'}) : null),
    h('div', {class: 'rp-sub', text: sub}),
    h('div', {class: 'rp-actions'}, ratingControl(thread), aiNote, threadMenu(thread)));
  const banners = [];
  if (thread.optout) {
    banners.push(h('div', {class: 'banner warn'}, h('span', {text: 'Ta firma mogła poprosić o zaprzestanie kontaktu. '}),
      thread.mail_status !== 'nie_kontaktowac' ? h('button', {type: 'button', class: 'btn', text: 'Ustaw: Nie kontaktować', onclick: () => setStatus('nie_kontaktowac')}) : null));
  } else if (view.suggestion) {
    const label = (view.statuses.find((s) => s.code === view.suggestion) || {}).label || view.suggestion;
    banners.push(h('div', {class: 'banner'}, h('span', {text: 'Podpowiedź po ocenie: ustawić status „' + label + '”? '}),
      h('button', {type: 'button', class: 'btn', text: 'Ustaw', onclick: () => setStatus(view.suggestion)})));
  }
  if (thread.weak) banners.push(h('div', {class: 'banner warn', text: 'Przypisano po samej domenie nadawcy: sprawdź, czy to na pewno ta firma.'}));
  const messages = view.messages.map((message, index) => messageCard(message, index === view.messages.length - 1));
  const typing = Boolean(composer.text) && document.activeElement === composer.text;
  composer.text = null;
  composer.quote = null;
  composer.status = null;
  setChildren(el('detail'), [h('article', {class: 'rp-thread'}, header, banners, h('div', {class: 'rp-msgs'}, view.origin ? originCard(view.origin) : null, messages), composerNode(view))]);
  if (typing && composer.text) composer.text.focus();
}


// ---- edytor odpowiedzi -------------------------------------------------------------------------------------------------

function draftText() { return composer.text ? composer.text.value : ''; }

function setDraftStatus(text) { if (composer.status) composer.status.textContent = text; }

// Zapisuje szkic od razu (wołane przed przeładowaniem wątku i przed wysyłką, żeby nic nie przepadło).
async function flushDraft() {
  clearTimeout(composer.timer);
  composer.timer = null;
  const thread = state.detail && state.detail.thread;
  if (!thread || !composer.text) return true;
  const body = draftText();
  const had = Boolean(state.detail.compose && state.detail.compose.draft);
  if (!body.trim()) {
    if (had) { await api('/api/odpowiedzi/szkic-usun', {id: thread.id}); state.detail.compose.draft = null; }
    setDraftStatus('');
    return true;
  }
  if (had && state.detail.compose.draft.body === body) return true;
  setDraftStatus('Zapisywanie…');
  const {ok, data} = await api('/api/odpowiedzi/szkic', {id: thread.id, body});
  if (!ok) { setDraftStatus('Nie zapisano szkicu'); showToast(data.error || 'Nie udało się zapisać szkicu.', 'error'); return false; }
  state.detail.compose.draft = Object.assign({}, state.detail.compose.draft || {}, {id: data.id, body, author: 'human', state: 'draft', error: ''});
  setDraftStatus('Szkic zapisany');
  return true;
}

function scheduleDraftSave() {
  clearTimeout(composer.timer);
  setDraftStatus('Zmieniono…');
  composer.timer = setTimeout(flushDraft, DRAFT_SAVE_MS);
}

function openComposer(focus) {
  const thread = state.detail && state.detail.thread;
  if (!thread || !state.detail.compose || !state.detail.compose.can_reply) return;
  if (!composer.open[thread.id]) { composer.open[thread.id] = true; renderThread(state.detail); }
  if (focus && composer.text) { composer.text.focus(); if (composer.text.scrollIntoView) composer.text.scrollIntoView({block: 'center'}); }
}

async function discardDraft() {
  const thread = state.detail && state.detail.thread;
  if (!thread) return;
  clearTimeout(composer.timer);
  await api('/api/odpowiedzi/szkic-usun', {id: thread.id});
  state.detail.compose.draft = null;
  composer.open[thread.id] = false;
  renderThread(state.detail);
}

function cancelPendingSend() {
  if (!composer.pending) return;
  clearTimeout(composer.pending.timer);
  composer.pending = null;
  setDraftStatus('Wysyłanie anulowane. Szkic został.');
  for (const node of document.querySelectorAll('.rp-composer button, .rp-composer textarea')) node.disabled = false;
}

async function requestSend(closeAfter) {
  const thread = state.detail && state.detail.thread;
  const compose = state.detail && state.detail.compose;
  if (!thread || !compose || !compose.can_reply || !compose.mailbox_ok || composer.pending) return;
  if (!draftText().trim()) { showToast('Najpierw napisz odpowiedź.'); return; }
  if (!(await flushDraft())) return;
  let acks = [];
  if (compose.needs_ack && compose.needs_ack.length) {
    const confirmed = await showSheet({title: 'Ta firma prosiła o brak kontaktu', tone: 'danger', focusCancel: true, confirmLabel: 'Wyślij mimo to',
      message: 'W tym wątku jest prośba o zaprzestanie kontaktu (albo status „Nie kontaktować”). Wysłać odpowiedź mimo to?'});
    if (!confirmed) return;
    acks = compose.needs_ack.slice();
  }
  const quote = !composer.quote || composer.quote.checked;
  for (const node of document.querySelectorAll('.rp-composer button, .rp-composer textarea')) node.disabled = true;
  setDraftStatus('Wysyłanie za ' + Math.round(SEND_DELAY_MS / 1000) + ' s…');
  composer.pending = {timer: setTimeout(() => doSend(thread.id, acks, quote, closeAfter), SEND_DELAY_MS)};
  showToast('Odpowiedź zostanie wysłana za ' + Math.round(SEND_DELAY_MS / 1000) + ' s.', 'ok', {action: {label: 'Cofnij', onClick: cancelPendingSend}, timeout: SEND_DELAY_MS});
}

async function doSend(id, acks, quote, closeAfter) {
  composer.pending = null;
  const {ok, status, data} = await api('/api/odpowiedzi/wyslij', {id, ack: acks, quote, close: closeAfter});
  if (status === 409 && data.needs_ack) {
    const again = await showSheet({title: 'Potwierdź wysyłkę', message: 'Serwer prosi o potwierdzenie: ' + data.error, confirmLabel: 'Wyślij mimo to', tone: 'danger', focusCancel: true});
    if (again) { await doSend(id, data.needs_ack, quote, closeAfter); return; }
  } else if (!ok) {
    showToast(data.error || 'Nie udało się wysłać odpowiedzi.', 'error');
  } else {
    showToast('Wysłano.', 'ok');
    if (data.warning) showToast(data.warning, 'error');
    composer.open[id] = false;
  }
  if (state.selected === id) { await reloadAfterChange(id, ok && closeAfter); }
}

function composerNode(view) {
  const compose = view.compose;
  if (!compose) return null;
  if (!compose.can_reply) return compose.reason ? h('div', {class: 'rp-hint', text: compose.reason}) : null;
  if (!compose.mailbox_ok) return h('div', {class: 'banner warn', text: compose.mailbox_problem});
  const thread = view.thread;
  const draft = compose.draft;
  if (!composer.open[thread.id] && !draft) {
    return h('div', {class: 'rp-composer is-closed'}, h('button', {type: 'button', class: 'btn', text: 'Odpowiedz', onclick: () => openComposer(true)}));
  }
  const text = h('textarea', {rows: 5, class: 'rp-draft', placeholder: 'Napisz odpowiedź…', 'aria-label': 'Treść odpowiedzi'});
  text.value = draft ? draft.body : '';
  text.addEventListener('input', scheduleDraftSave);
  autosize(text, 360);
  const quote = h('input', {type: 'checkbox', id: 'quoteChk'});
  quote.checked = true;
  const status = h('span', {class: 'rp-hint', role: 'status', text: draft && draft.state === 'draft' ? 'Szkic zapisany' : ''});
  composer.text = text;
  composer.quote = quote;
  composer.status = status;
  const notes = [];
  if (draft && draft.author === 'ai') notes.push(h('div', {class: 'banner', text: 'Szkic przygotował Claude Code. Przeczytaj go i popraw przed wysłaniem: nic nie wyjdzie, dopóki sam nie klikniesz „Wyślij”.'}));
  if (draft && draft.state === 'failed') notes.push(h('div', {class: 'banner error', text: 'Ostatnia próba wysłania nie udała się: ' + (draft.error || 'nieznany błąd') + '. Nic nie wyszło, szkic został.'}));
  if (compose.needs_ack && compose.needs_ack.length) notes.push(h('div', {class: 'banner warn', text: 'Uwaga: ta firma prosiła o brak kontaktu. Wysyłka wymaga potwierdzenia.'}));
  return h('section', {class: 'rp-composer', 'aria-label': 'Odpowiedź'},
    h('div', {class: 'rp-compose-head'}, h('span', {text: 'Od: ' + compose.mailbox}), h('span', {text: 'Do: ' + compose.to}), h('span', {text: compose.subject})),
    notes, text,
    h('div', {class: 'rp-compose-foot'},
      h('button', {type: 'button', class: 'btn primary', text: 'Wyślij', title: 'Ctrl/⌘ + Enter', onclick: () => requestSend(false)}),
      h('button', {type: 'button', class: 'btn', text: 'Wyślij i zamknij', onclick: () => requestSend(true)}),
      h('button', {type: 'button', class: 'btn plain', text: 'Odrzuć szkic', onclick: () => discardDraft()}),
      h('label', {class: 'rp-quote-opt'}, quote, ' Dołącz cytat'), status));
}

function renderUnassigned(message) {
  const results = h('div', {class: 'rp-assign-results'});
  const input = h('input', {type: 'search', placeholder: 'Szukaj maila kampanii: firma, adres, temat…', autocomplete: 'off', 'aria-label': 'Szukaj maila kampanii'});
  let timer = null;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const {ok, data} = await api('/api/odpowiedzi/kandydaci', {q: input.value});
      if (!ok) return;
      setChildren(results, data.mails.length ? data.mails.map((mail) => h('button', {type: 'button', onclick: () => assign(message.id, mail.id)},
        h('strong', {text: mail.company || mail.id}), h('span', {class: 'rp-hint', text: [mail.email, mail.subject, shortDate(mail.sent_at)].filter(Boolean).join(' · ')})))
        : [h('div', {class: 'rp-hint', text: input.value.trim() ? 'Nic nie znaleziono.' : ''})]);
    }, 250);
  });
  setChildren(el('detail'), [h('article', {class: 'rp-thread'},
    h('header', {class: 'rp-thead'}, h('div', {class: 'rp-title'},
      h('button', {type: 'button', class: 'btn plain split-back', text: '‹ Lista', onclick: () => backToList()}), h('h2', {text: 'Nieprzypisana wiadomość'}))),
    h('div', {class: 'banner warn', text: 'Ta wiadomość ma temat naszej kampanii, ale nie wiadomo, od której firmy jest. Przypisz ją do maila, na który odpowiada.'}),
    h('div', {class: 'rp-msgs'}, messageCard(message, true)),
    h('div', {class: 'rp-assign'}, h('strong', {text: 'Przypisz do maila kampanii'}), input, results))]);
}

async function assign(messageId, mailId) {
  const {ok, data} = await api('/api/odpowiedzi/przypisz', {message_id: Number(messageId), mail_id: mailId});
  if (!ok) { showToast(data.error || 'Nie udało się przypisać.', 'error'); return; }
  showToast('Przypisano do firmy.', 'ok');
  state.selected = '';
  await loadList();
  openThread(mailId, false);
}

function backToList() {
  state.selected = '';
  state.detail = null;
  markSelected();
  showDetailView(false);
  setChildren(el('detail'), []);
  syncUrl(false);
}

// ---- działania na wątku (z „Cofnij”) --------------------------------------------------------------------------------

function applyRowPatch(id, patch) {
  const row = state.rows.find((r) => r.id === id);
  if (row) Object.assign(row, patch);
}

async function rate(rating, options) {
  const thread = state.detail && state.detail.thread;
  if (!thread) return;
  const body = Object.assign({id: thread.id, rating}, options || {});
  const {ok, data} = await api('/api/odpowiedzi/ocena', body);
  if (!ok) { showToast(data.error || 'Nie udało się zapisać oceny.', 'error'); return; }
  const previous = data.previous;
  const undoable = !(options && options.restore_by);
  showToast(rating ? 'Ocena: ' + RATING_LABEL[rating] : 'Usunięto ocenę.', 'ok', undoable ? {action: {label: 'Cofnij', onClick: () => undoRating(thread.id, previous)}} : undefined);
  await reloadAfterChange(thread.id);
}

async function undoRating(id, previous) {
  const body = {id, rating: previous.rating, note: previous.note || ''};
  if (previous.by) body.restore_by = previous.by;
  else body.restore_by = 'human';
  const {ok, data} = await api('/api/odpowiedzi/ocena', body);
  if (!ok) { showToast(data.error || 'Nie udało się cofnąć.', 'error'); return; }
  await reloadAfterChange(id);
}

async function confirmAiRating(thread) {
  await rate(thread.rating, {note: thread.rating_note || ''});
}

async function stateAction(action) {
  const thread = state.detail && state.detail.thread;
  if (!thread) return;
  const {ok, data} = await api('/api/odpowiedzi/akcja', {id: thread.id, action});
  if (!ok) { showToast(data.error || 'Nie udało się zmienić stanu.', 'error'); return; }
  const opposite = {close: 'reopen', reopen: 'close', unread: 'read', read: 'unread', optout: 'no_optout', no_optout: 'optout'}[action];
  const messages = {close: 'Wątek zamknięty.', reopen: 'Wątek otwarty ponownie.', unread: 'Oznaczono jako nieprzeczytany.', optout: 'Oznaczono prośbę o wypisanie.', no_optout: 'Zdjęto znacznik wypisania.'};
  showToast(messages[action] || 'Zapisano.', 'ok', opposite ? {action: {label: 'Cofnij', onClick: async () => {
    await api('/api/odpowiedzi/akcja', {id: thread.id, action: opposite});
    await reloadAfterChange(thread.id);
  }}} : undefined);
  await reloadAfterChange(thread.id, action === 'close');
}

async function setStatus(code) {
  const thread = state.detail && state.detail.thread;
  if (!thread) return;
  const {ok, data} = await api('/api/odpowiedzi/status', {id: thread.id, status: code});
  if (!ok) { showToast(data.error || 'Nie udało się ustawić statusu.', 'error'); return; }
  const label = ((APP.statusMeta || {})[code] || [code])[0];
  showToast('Status maila: ' + label, 'ok', data.previous && data.previous !== code ? {action: {label: 'Cofnij', onClick: async () => {
    await api('/api/odpowiedzi/status', {id: thread.id, status: data.previous});
    await reloadAfterChange(thread.id);
  }}} : undefined);
  await reloadAfterChange(thread.id);
}

// Po zmianie odświeżamy listę i otwarty wątek; po zamknięciu wątku (opcja `advance`) przechodzimy do następnej rozmowy.
async function reloadAfterChange(id, advance) {
  const index = state.rows.findIndex((r) => r.id === id);
  await loadList();
  const stillThere = state.rows.some((r) => r.id === id);
  if (advance && state.advance && !stillThere && state.rows.length) {
    openRow(state.rows[Math.min(index < 0 ? 0 : index, state.rows.length - 1)]);
  } else if (state.selected === id) {
    openThread(id, false);
  }
  refreshOverview();
}

function moveSelection(step) {
  if (!state.rows.length) return;
  const index = state.rows.findIndex((r) => rowKey(r) === state.selected);
  const next = state.rows[Math.max(0, Math.min(state.rows.length - 1, index < 0 ? (step > 0 ? 0 : state.rows.length - 1) : index + step))];
  openRow(next);
  const node = el('list').querySelector ? el('list').querySelector('[data-id="' + String(rowKey(next)).replace(/"/g, '') + '"]') : null;
  if (node && node.scrollIntoView) node.scrollIntoView({block: 'nearest'});
}

// ---- stan skrzynek, pobieranie ---------------------------------------------------------------------------------------

function updateBadge(unread) {
  document.title = (unread ? '(' + unread + ') ' : '') + 'Perno — Odpowiedzi';
  const link = document.querySelector('.topnav-link[aria-current="page"]');
  if (!link || !link.querySelector) return;
  let badge = link.querySelector('.topnav-badge');
  if (!unread) { if (badge) badge.remove(); return; }
  if (!badge) { badge = h('span', {class: 'topnav-badge', role: 'status'}); link.appendChild(badge); }
  badge.textContent = String(unread);
  badge.setAttribute('aria-label', unread + ' nowych');
}

const PROBLEM_TEXT = {auth: 'błąd logowania', network: 'brak połączenia', protocol: 'błąd serwera poczty', config: 'niepełna konfiguracja', no_imap: 'brak ustawienia IMAP'};

function renderOverview(data) {
  const box = data.mailboxes;
  const problems = box.problems || [];
  const status = el('mailStatus');
  if (!box.configured) { status.setAttribute('hidden', ''); return; }
  status.removeAttribute('hidden');
  status.className = 'rp-status' + (problems.length ? ' is-bad' : '');
  const summary = h('span', {text: 'Odbiór: ' + box.receiving + ' z ' + box.configured + ' skrzynek' + (problems.length ? ' · ' + problems.length + ' z problemem' : ' ok')});
  const parts = [summary];
  if (problems.length) {
    const list = h('ul', {class: 'rp-problems', hidden: true}, problems.slice(0, 30).map((p) =>
      h('li', {}, h('strong', {text: p.mailbox}), ' ', h('span', {text: PROBLEM_TEXT[p.kind] || p.kind}), p.error ? h('span', {class: 'rp-hint', text: ' — ' + p.error}) : null)));
    const toggle = h('button', {type: 'button', class: 'btn plain', text: 'pokaż', 'aria-expanded': 'false'});
    toggle.addEventListener('click', () => {
      const open = list.hasAttribute('hidden');
      if (open) list.removeAttribute('hidden'); else list.setAttribute('hidden', '');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.textContent = open ? 'ukryj' : 'pokaż';
    });
    parts.push(toggle, list);
  }
  setChildren(status, parts);
  const fetch = data.fetch;
  el('fetchInfo').textContent = fetch.running ? 'Pobieram…' : (fetch.finished_at ? 'ostatnio ' + shortDate(fetch.finished_at) : 'jeszcze nie pobierano');
  el('fetchBtn').disabled = Boolean(fetch.running);
}

function buildFilterOptions(data) {
  if (state.known) return;
  state.known = {mailboxes: data.mailboxes.names || []};
  renderMailboxList('');
  const segments = el('segmentSel');
  for (const name of (APP.segmentOrder || [])) segments.appendChild(h('option', {value: name, text: name}));
  const countries = el('countrySel');
  for (const [code, name] of (APP.countries || [])) countries.appendChild(h('option', {value: code, text: name}));
  segments.value = state.segments[0] || '';
  countries.value = state.countries[0] || '';
}

function renderMailboxList(find) {
  const needle = find.trim().toLowerCase();
  const names = (state.known ? state.known.mailboxes : []).filter((name) => !needle || name.toLowerCase().includes(needle));
  setChildren(el('mailboxList'), names.length ? names.slice(0, 200).map((name) => {
    const box = h('input', {type: 'checkbox', value: name});
    box.checked = state.mailboxes.includes(name);
    box.addEventListener('change', () => {
      state.mailboxes = box.checked ? [...state.mailboxes, name] : state.mailboxes.filter((n) => n !== name);
      applyFilters();
    });
    return h('label', {}, box, h('span', {text: name}));
  }) : [h('div', {class: 'rp-hint', text: 'Brak skrzynek.'})]);
  el('filterBtn').textContent = 'Filtry' + (filterCount() ? ' · ' + filterCount() : '');
}

function filterCount() {
  return state.mailboxes.length + state.segments.length + state.countries.length + (state.from ? 1 : 0) + (state.to ? 1 : 0);
}

function applyFilters() {
  state.page = 1;
  renderMailboxList(el('mailboxFind').value);
  syncUrl(false);
  loadList();
}

async function refreshOverview() {
  const {ok, data} = await apiGet('/api/odpowiedzi/stan');
  if (!ok) return;
  const before = state.overview;
  state.overview = data;
  buildFilterOptions(data);
  renderOverview(data);
  updateBadge(data.unread);
  if (!before || before.mailboxes.configured !== data.mailboxes.configured) renderRows();  // etykieta skrzynki zależy od ich liczby
  if (before && data.unread > before.unread) {
    showToast(pluralNew(data.unread - before.unread), 'ok');
    loadList();
  }
  maybeAutoFetch(data);
}

function maybeAutoFetch(data) {
  const minutes = data.auto_minutes;
  if (!minutes || !data.mailboxes.receiving || data.fetch.running) return;
  const finished = data.fetch.finished_at ? new Date(data.fetch.finished_at).getTime() : 0;
  const now = Date.now();
  if (now - finished >= minutes * 60000 && now - lastAutoFetch >= minutes * 60000) { lastAutoFetch = now; startFetch(true); }
}

async function startFetch(quiet) {
  const {ok, data} = await api('/api/odpowiedzi/pobierz', {});
  if (!ok) { if (!quiet) showToast(data.error || 'Nie udało się pobrać poczty.', 'error'); return; }
  if (!data.started) { if (!quiet) showToast(data.reason || 'Pobieranie już trwa.'); return; }
  el('fetchInfo').textContent = 'Pobieram…';
  el('fetchBtn').disabled = true;
  waitForFetch(0, quiet);
}

async function waitForFetch(attempt, quiet) {
  await sleep(FETCH_POLL_MS);
  await refreshOverview();
  if (state.overview && state.overview.fetch.running && attempt < 90) { waitForFetch(attempt + 1, quiet); return; }
  await loadList();
  if (!quiet) {
    const summary = state.overview ? state.overview.fetch.summary || {} : {};
    showToast(summary.skipped ? 'Pobieranie pominięto: ' + summary.skipped + '.' : 'Pobrano: ' + (summary.stored || 0) + ' nowych wiadomości.', summary.failed ? 'error' : 'ok');
  }
}


// ---- raport -----------------------------------------------------------------------------------------------------------

const GROUP_TITLES = {segment: 'Segment', country: 'Kraj', mailbox: 'Skrzynka', day: 'Dzień wysyłki'};
const GROUP_PREVIEW = 15;  // przy wielu skrzynkach i dniach pokazujemy najpierw początek tabeli

function pct(value) {
  return value === null || value === undefined ? '—' : (value * 100).toFixed(1).replace('.', ',') + '%';
}

function reportPayload() {
  return {from: state.repFrom, to: state.repTo, segments: state.segments, countries: state.countries, mailboxes: state.mailboxes};
}

function setView(view) {
  state.view = view;
  el('replies').classList.toggle('is-report', view === 'report');
  el('viewMail').setAttribute('aria-pressed', view === 'mail' ? 'true' : 'false');
  el('viewReport').setAttribute('aria-pressed', view === 'report' ? 'true' : 'false');
  if (view === 'report') { el('split').setAttribute('hidden', ''); el('report').removeAttribute('hidden'); }
  else { el('report').setAttribute('hidden', ''); el('split').removeAttribute('hidden'); }
  syncUrl(false);
  if (view === 'report') loadReport();
}

async function loadReport() {
  const {ok, data} = await api('/api/odpowiedzi/raport', reportPayload());
  if (!ok) { showToast(data.error || 'Nie udało się policzyć raportu.', 'error'); return; }
  state.report = data;
  renderReport(data);
}

function kpi(label, value, note) {
  return h('div', {class: 'rp-kpi'}, h('span', {class: 'label', text: label}), h('span', {class: 'value', text: String(value)}), note ? h('span', {class: 'note', text: note}) : null);
}

function barRow(label, value, max, count, tone) {
  const fill = h('span', {class: 'rp-bar-fill' + (tone ? ' ' + tone : '')});
  fill.style.setProperty('--w', (max > 0 ? Math.max(value > 0 ? 2 : 0, Math.round((value / max) * 100)) : 0) + '%');
  return h('div', {class: 'rp-bar-row'}, h('span', {text: label}), h('span', {class: 'rp-bar'}, fill), h('span', {class: 'num', text: count === undefined ? String(value) : count}));
}

function groupLabel(group, key) {
  if (group === 'country') { const hit = (APP.countries || []).find((c) => c[0] === key); return hit ? hit[1] + ' (' + key + ')' : key; }
  return key;
}

function groupTable(group, rows) {
  const head = h('tr', {}, ['', 'Wysłano', 'Dostarczono', 'Odpowiedzi', 'Odsetek', '+', '=', '–'].map((text, index) => h('th', {class: index ? 'num' : '', text})));
  const body = (visible) => visible.map((row) => h('tr', {},
    h('td', {}, groupLabel(group, row.key), row.small_sample ? h('span', {class: 'rp-small', text: ' mała próba'}) : null),
    h('td', {class: 'num', text: String(row.sent)}), h('td', {class: 'num', text: String(row.delivered)}), h('td', {class: 'num', text: String(row.replied)}),
    h('td', {class: 'num', text: pct(row.reply_rate)}), h('td', {class: 'num', text: String(row.positive)}), h('td', {class: 'num', text: String(row.neutral)}),
    h('td', {class: 'num', text: String(row.negative)})));
  const table = h('table', {class: 'data-table'}, h('thead', {}, head), h('tbody', {}, body(rows.slice(0, GROUP_PREVIEW))));
  const wrap = h('div', {class: 'table-wrap'}, table);
  const card = h('div', {class: 'card'}, h('h2', {text: GROUP_TITLES[group]}), wrap);
  if (rows.length > GROUP_PREVIEW) {
    const more = h('button', {type: 'button', class: 'btn plain', text: 'Pokaż wszystkie (' + rows.length + ')'});
    more.addEventListener('click', () => { table.replaceChildren(h('thead', {}, head), h('tbody', {}, body(rows))); more.remove(); });
    card.appendChild(more);
  }
  return card;
}

function renderReport(data) {
  const t = data.total;
  const bar = h('div', {class: 'rp-report-bar'},
    h('label', {}, h('span', {text: 'Wysłano od'}), h('input', {type: 'date', id: 'repFrom', value: state.repFrom})),
    h('label', {}, h('span', {text: 'do'}), h('input', {type: 'date', id: 'repTo', value: state.repTo})),
    h('button', {type: 'button', class: 'btn', text: 'Przelicz', onclick: () => { state.repFrom = el('repFrom').value; state.repTo = el('repTo').value; syncUrl(false); loadReport(); }}),
    h('button', {type: 'button', class: 'btn', text: 'Pobierz CSV', onclick: () => downloadReport('csv')}),
    h('button', {type: 'button', class: 'btn', text: 'Kopiuj podsumowanie', onclick: copySummary}),
    h('span', {class: 'rp-hint', text: 'Okres: ' + data.period}));
  const warnings = data.warnings.map((text) => h('div', {class: 'banner warn', text}));
  const latency = data.latency;
  const tiles = h('div', {class: 'rp-kpis'},
    kpi('Wysłano', t.sent, data.replied_companies ? data.replied_companies + ' firm odpowiedziało' : ''), kpi('Dostarczono', t.delivered, t.bounced + ' twardych zwrotów'),
    kpi('Odpowiedzi', t.replied, t.awaiting + ' czeka na nas'), kpi('Odsetek odpowiedzi', pct(t.reply_rate), t.small_sample ? 'mała próba (< 20)' : ''),
    kpi('Pozytywne', t.positive, pct(t.positive_rate) + ' ocenionych'), kpi('Mediana czasu', latency.median_hours === null ? '—' : (latency.median_hours < 48 ? latency.median_hours + ' h' : (latency.median_hours / 24).toFixed(1).replace('.', ',') + ' dni'), 'n = ' + latency.n));
  const funnel = h('div', {class: 'card'}, h('h2', {text: 'Lejek'}), h('div', {class: 'rp-bars'},
    barRow('Wysłano', t.sent, t.sent), barRow('Dostarczono', t.delivered, t.sent), barRow('Odpowiedzi', t.replied, t.sent), barRow('Pozytywne', t.positive, t.sent, undefined, 'is-positive')));
  const rated = t.positive + t.neutral + t.negative + t.unrated;
  const stack = h('div', {class: 'rp-stack', role: 'img', 'aria-label': 'Rozkład ocen'}, [['positive', t.positive], ['neutral', t.neutral], ['negative', t.negative]].map(([tone, count]) => {
    const part = h('span', {class: 'is-' + tone});
    part.style.setProperty('--w', (rated ? (count / rated) * 100 : 0) + '%');
    return part;
  }));
  const legend = h('div', {class: 'rp-legend'}, [['positive', 'Pozytywne', t.positive], ['neutral', 'Neutralne', t.neutral], ['negative', 'Negatywne', t.negative]].map(([tone, label, count]) =>
    h('span', {class: 'is-' + tone}, h('span', {class: 'badge', text: label}), String(count))), h('span', {text: 'Nieocenione: ' + t.unrated}), h('span', {text: 'Prośby o wypisanie: ' + t.optout}));
  const ratings = h('div', {class: 'card'}, h('h2', {text: 'Oceny odpowiedzi'}), stack, legend);
  const buckets = Object.entries(latency.buckets);
  const maxBucket = Math.max(0, ...buckets.map(([, n]) => n));
  const timing = h('div', {class: 'card'}, h('h2', {text: 'Czas do pierwszej odpowiedzi'}), latency.n
    ? h('div', {class: 'rp-bars'}, buckets.map(([label, n]) => barRow(label, n, maxBucket))) : h('div', {class: 'rp-hint', text: 'Brak odpowiedzi do zmierzenia.'}));
  const tables = Object.keys(GROUP_TITLES).filter((group) => (data.groups[group] || []).length).map((group) => groupTable(group, data.groups[group]));
  setChildren(el('report'), [h('div', {class: 'rp-report-inner'}, bar, warnings, tiles, h('div', {class: 'rp-kpis'}, funnel, ratings, timing), tables)]);
}

async function downloadReport(format) {
  const result = await downloadFile('/api/odpowiedzi/raport-plik', Object.assign({format}, reportPayload()), 'raport_odpowiedzi.' + format);
  showToast(result.ok ? 'Zapisano plik: ' + result.name : (result.error || 'Nie udało się pobrać pliku.'), result.ok ? 'ok' : 'error');
}

async function copySummary() {
  const res = await fetch('/api/odpowiedzi/raport-plik', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(Object.assign({format: 'md'}, reportPayload()))});
  if (!res.ok) { showToast('Nie udało się przygotować podsumowania.', 'error'); return; }
  const text = await res.text();
  try { await navigator.clipboard.writeText(text); showToast('Skopiowano podsumowanie.', 'ok'); }
  catch (error) { showToast('Przeglądarka nie pozwoliła skopiować; użyj „Pobierz CSV”.', 'error'); }
}

// ---- podpięcie --------------------------------------------------------------------------------------------------------

function togglePopover(button, pop, open) {
  const show = open === undefined ? pop.hasAttribute('hidden') : open;
  if (show) pop.removeAttribute('hidden'); else pop.setAttribute('hidden', '');
  button.setAttribute('aria-expanded', show ? 'true' : 'false');
}

el('filterBtn').addEventListener('click', (event) => { event.stopPropagation(); togglePopover(el('filterBtn'), el('filterPop')); });
el('filterPop').addEventListener('click', (event) => event.stopPropagation());
document.addEventListener('click', () => { togglePopover(el('filterBtn'), el('filterPop'), false); closeMenus(); });
el('mailboxFind').addEventListener('input', () => renderMailboxList(el('mailboxFind').value));
el('segmentSel').addEventListener('change', () => { state.segments = el('segmentSel').value ? [el('segmentSel').value] : []; applyFilters(); });
el('countrySel').addEventListener('change', () => { state.countries = el('countrySel').value ? [el('countrySel').value] : []; applyFilters(); });
el('fromDay').addEventListener('change', () => { state.from = el('fromDay').value; applyFilters(); });
el('toDay').addEventListener('change', () => { state.to = el('toDay').value; applyFilters(); });
el('clearFilters').addEventListener('click', () => {
  Object.assign(state, {mailboxes: [], segments: [], countries: [], from: '', to: ''});
  for (const id of ['segmentSel', 'countrySel', 'fromDay', 'toDay']) el(id).value = '';
  applyFilters();
});

let searchTimer = null;
el('q').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = el('q').value.trim(); state.page = 1; syncUrl(false); loadList(); }, 250);
});
el('fetchBtn').addEventListener('click', () => startFetch(false));
el('helpBtn').addEventListener('click', () => showShortcutHelp(SHORTCUTS));
el('viewMail').addEventListener('click', () => setView('mail'));
el('viewReport').addEventListener('click', () => setView('report'));

window.addEventListener('popstate', () => {
  restoreFromUrl();
  if ((state.view === 'report') !== (el('split').hasAttribute('hidden'))) setView(state.view);
  if (state.selected) { if (/^\d+$/.test(state.selected)) openUnassigned(state.selected); else openThread(state.selected, false); }
  else backToList();
  loadList();
});

const inMail = (fn) => (event) => { if (state.view === 'mail') fn(event); };
bindShortcuts({
  j: inMail(() => moveSelection(1)), k: inMail(() => moveSelection(-1)),
  Enter: inMail(() => { if (!state.selected) moveSelection(1); }),
  u: inMail(() => backToList()),
  1: inMail(() => rate('positive')), 2: inMail(() => rate('neutral')), 3: inMail(() => rate('negative')), 0: inMail(() => rate(null)),
  e: inMail(() => { if (state.detail) stateAction(state.detail.thread.closed ? 'reopen' : 'close'); }),
  r: inMail(() => openComposer(true)),
  'ctrl+Enter': () => { if (composer.text && document.activeElement === composer.text) requestSend(false); },
  'meta+Enter': () => { if (composer.text && document.activeElement === composer.text) requestSend(false); },
  Escape: () => { if (composer.text && document.activeElement === composer.text && composer.text.blur) composer.text.blur(); closeMenus(); },
  '/': inMail(() => el('q').focus()), g: inMail(() => startFetch(false)), '?': () => showShortcutHelp(SHORTCUTS),
});

// Wysyłka czeka sześć sekund na „Cofnij”: zamknięcie karty w tym czasie nic nie wysyła (szkic zostaje na serwerze).
window.addEventListener('beforeunload', (event) => { if (composer.pending) { event.preventDefault(); event.returnValue = ''; } });

pollWhileVisible(refreshOverview, {interval: 60000});

async function start() {
  restoreFromUrl();
  renderTabs();
  if (state.view === 'report') setView('report');
  await Promise.all([loadList(), refreshOverview()]);
  if (state.selected) { if (/^\d+$/.test(state.selected)) openUnassigned(state.selected); else openThread(state.selected, false); }
}

start();
