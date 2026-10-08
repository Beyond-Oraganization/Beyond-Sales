'use strict';
// Zakładka „Konfiguracja”: formularz ustawień z config.env. Sekrety są tylko do zapisu (serwer nie odsyła ich wartości);
// wysyłamy wyłącznie pola, które człowiek zmienił. Cały tekst z serwera przechodzi przez escapeHtml.
const el = (id) => document.getElementById(id);
let CONFIG = null;
let INITIAL = {};  // klucz -> wartość pokazana w polu (do wykrycia zmian)
let REMOVED = new Set();  // numery zapisanych skrzynek, które człowiek usunął i jeszcze nie zapisał

const toast = (text, tone) => showToast(text, tone);

function fieldValue(field) {
  if (field.kind === 'bool') return (field.value || field.default || 'true');
  if (field.kind === 'choice') return (field.value || field.default || (field.choices[0] || ''));
  return field.value || '';
}

function inputHtml(field) {
  const id = 'c_' + escapeHtml(field.key);
  if (field.kind === 'secret') {
    return '<input type="password" id="' + id + '" autocomplete="new-password" placeholder="'
      + (field.set ? 'ustawione — zostaw puste, żeby nie zmieniać' : 'nie ustawione') + '">'
      + (field.set ? '<label class="inline"><input type="checkbox" id="x_' + escapeHtml(field.key) + '"> usuń to ustawienie</label>' : '');
  }
  if (field.kind === 'bool' || field.kind === 'choice') {
    const options = field.kind === 'bool' ? [['true', 'tak'], ['false', 'nie']] : field.choices.map((c) => [c, c]);
    const current = fieldValue(field);
    return '<select id="' + id + '">' + options.map(([value, label]) =>
      '<option value="' + escapeHtml(value) + '"' + (value === current ? ' selected' : '') + '>' + escapeHtml(label) + '</option>').join('') + '</select>';
  }
  return '<input type="text" id="' + id + '" value="' + escapeHtml(field.value || '') + '" placeholder="'
    + escapeHtml(field.default || '') + '" autocomplete="off">';
}

function fieldHtml(field) {
  const tag = field.kind === 'secret'
    ? '<span class="tag ' + (field.set ? '' : 'off') + '">' + (field.set ? 'ustawione' : 'puste') + '</span>' : '';
  const restart = field.restart ? ' · po zmianie uruchom ponownie demona' : '';
  return '<div class="cfg-field"><span class="name">' + escapeHtml(field.label) + ' ' + tag + '</span>'
    + inputHtml(field) + '<span class="help">' + escapeHtml(field.help) + escapeHtml(restart) + '</span></div>';
}

function isMailbox(group) {
  return Boolean(group.mailbox) && group.mailbox > 1;
}

const MAILBOX_LABELS = {off: 'Wyłączona', error: 'Nie działa', ok: 'Działa', unknown: 'Nie sprawdzona'};

// „2026-10-08T13:00:00” → „13:00” (dziś) albo „09.10 13:00”.
function shortStamp(stamp) {
  if (!stamp) return '';
  const [day, time] = String(stamp).split('T');
  if (!time) return String(stamp);
  const now = new Date();
  const today = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');
  return (day === today ? '' : day.slice(8, 10) + '.' + day.slice(5, 7) + ' ') + time.slice(0, 5);
}

function mailboxNotes(box) {
  const notes = [];
  if (box.state === 'off') notes.push('Automat nie wysyła z niej' + (box.until ? ' do ' + shortStamp(box.until) : '') + ', potem spróbuje ponownie.');
  if (box.test) notes.push('Test o ' + shortStamp(box.test.at) + (box.test.ok ? ': połączenie i logowanie działają.' : ' nie przeszedł.'));
  if (box.sent_today) notes.push('Dziś wysłała: ' + box.sent_today + '.');
  return notes;
}

// Pasek stanu skrzynki w jej karcie: etykieta (działa / wyłączona …), powód błędu i przycisk testu.
function mailboxBarHtml(slot) {
  const box = ((CONFIG && CONFIG.mailboxes) || []).find((b) => b.slot === slot);
  const button = '<button type="button" class="mbox-test" data-slot="' + slot + '"' + (box ? '' : ' disabled') + '>Sprawdź skrzynkę</button>';
  if (!box) return '<div class="mbox-bar"><span class="mbox-tag unknown">Niezapisana</span><span class="muted small">Zapisz ustawienia, żeby ją sprawdzić.</span>' + button + '</div>';
  const error = box.error ? '<div class="mbox-error">' + escapeHtml(box.error) + '</div>' : '';
  return '<div class="mbox-bar"><span class="mbox-tag ' + escapeHtml(box.state) + '">' + escapeHtml(MAILBOX_LABELS[box.state] || box.state)
    + '</span><span class="muted small">' + escapeHtml([box.address, ...mailboxNotes(box)].join(' · ')) + '</span>' + button + '</div>' + error;
}

function refreshMailboxBars() {
  if (!CONFIG) return;
  for (const group of CONFIG.groups) {
    const bar = group.mailbox && document.getElementById('mbox_' + group.mailbox);
    if (bar) bar.innerHTML = mailboxBarHtml(group.mailbox);
    if (bar) refreshSummary(group);
  }
}

function smtpPrefix(slot) {
  return slot === 1 ? 'SMTP' : 'SMTP' + slot;
}

function groupHtml(group, collapsed) {
  const mailbox = Boolean(group.mailbox);
  const remove = isMailbox(group) ? '<button type="button" class="cfg-remove" data-slot="' + group.mailbox + '">Usuń skrzynkę</button>' : '';
  const title = mailbox
    ? '<button type="button" class="cfg-toggle" data-key="' + escapeHtml(group.key) + '" aria-expanded="' + (collapsed ? 'false' : 'true') + '">'
      + '<span class="cfg-title">' + escapeHtml(group.title) + '</span><span class="cfg-summary muted"></span></button>'
    : '<h2>' + escapeHtml(group.title) + '</h2>';
  return '<section class="card' + (collapsed ? ' is-collapsed' : '') + '" id="card_' + escapeHtml(group.key) + '"><div class="card-head">'
    + title + remove + '</div><p class="muted group-note">'
    + escapeHtml(group.description) + '</p>'
    + (mailbox ? '<div class="mbox" id="mbox_' + group.mailbox + '">' + mailboxBarHtml(group.mailbox) + '</div>' : '')
    + '<div class="cfg-grid">' + group.fields.map(fieldHtml).join('') + '</div></section>';
}

// Jedna linia podsumowania zwiniętej karty: adres skrzynki, stan (z demona i testów) i to, czy odbiera pocztę (IMAP). Tekst ustawiamy przez textContent.
function refreshSummary(group) {
  const card = el('card_' + group.key);
  if (!card || !group.mailbox) return;
  const slot = group.mailbox;
  const value = (name) => { const input = el('c_' + name); return input ? input.value : ''; };
  const user = value(smtpPrefix(slot) + '_USER');
  const imap = value((slot === 1 ? 'IMAP' : 'IMAP' + slot) + '_HOST') ? ' · odbiór IMAP' : ' · bez odbioru';
  const box = ((CONFIG && CONFIG.mailboxes) || []).find((b) => b.slot === slot);
  const state = box && box.state !== 'unknown' ? ' · ' + (MAILBOX_LABELS[box.state] || box.state).toLowerCase() : '';
  card.querySelector('.cfg-summary').textContent = user ? user + state + imap : 'nowa skrzynka';
}

function setCollapsed(card, collapsed) {
  card.classList.toggle('is-collapsed', collapsed);
  const toggle = card.querySelector('.cfg-toggle');
  if (toggle) toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
}

// Przy wielu skrzynkach lista byłaby bardzo długa: skrzynki dodatkowe są zwinięte, a wyszukiwarka zostawia pasujące.
const COLLAPSE_FROM = 4;

function filterMailboxes() {
  const query = el('mailboxSearch').value.trim().toLowerCase();
  for (const group of CONFIG.groups.filter((g) => g.mailbox)) {
    const card = el('card_' + group.key);
    if (!card) continue;
    const text = (group.title + ' ' + card.querySelector('.cfg-summary').textContent).toLowerCase();
    card.classList.toggle('is-filtered', Boolean(query) && !text.includes(query));
  }
}

function updateSearchVisibility() {
  el('mailboxSearchWrap').hidden = CONFIG.groups.filter(isMailbox).length + 1 < COLLAPSE_FROM;
}

// Najmniejszy wolny numer skrzynki od 2: serwer wypisuje tylko skrzynki zapisane w pliku, więc numer liczymy też z kart dodanych w tej sesji.
function nextSlot() {
  const used = new Set(CONFIG.groups.filter(isMailbox).map((group) => group.mailbox));
  let slot = 2;
  while (used.has(slot)) slot += 1;
  return slot;
}

async function addMailbox(prefill) {
  const slot = nextSlot();
  const {ok, data} = await apiGet('/api/config/skrzynka?slot=' + slot);
  if (!ok) { toast(data.error || 'Nie udało się dodać skrzynki.', 'error'); return false; }
  const lastMailbox = CONFIG.groups.filter(isMailbox).pop();
  const anchor = el('card_' + (lastMailbox ? lastMailbox.key : 'mail'));
  CONFIG.groups.splice(CONFIG.groups.indexOf(lastMailbox || CONFIG.groups[0]) + 1, 0, data);
  for (const field of data.fields) INITIAL[field.key] = field.kind === 'secret' ? '' : fieldValue(field);
  anchor.insertAdjacentHTML('afterend', groupHtml(data, false));
  // Najczęściej kolejne skrzynki są u tego samego dostawcy: podpowiadamy serwer, port i SSL (SMTP i IMAP) z głównej.
  const prefix = (name) => name + (slot === 1 ? '' : slot);
  for (const [family, suffix] of [['SMTP', 'HOST'], ['SMTP', 'PORT'], ['SMTP', 'USE_SSL'], ['IMAP', 'HOST'], ['IMAP', 'PORT'], ['IMAP', 'USE_SSL']]) {
    const source = el('c_' + family + '_' + suffix);
    const target = el('c_' + prefix(family) + '_' + suffix);
    if (source && target) target.value = source.value;
  }
  const given = prefill || {};
  if (given.user) el('c_SMTP' + slot + '_USER').value = given.user;
  if (given.password) el('c_SMTP' + slot + '_PASSWORD').value = given.password;
  refreshSummary(data);
  updateSearchVisibility();
  const user = el('c_SMTP' + slot + '_USER');
  if (!prefill && user) { user.scrollIntoView({block: 'center'}); user.focus(); }
  return true;
}

// „Dodaj wiele”: każda linia to `adres;hasło`. Karty powstają jak przy pojedynczym dodawaniu, a zapis zostaje decyzją człowieka.
const BULK_LIMIT = 200;

function parseMailboxLines(text) {
  const entries = [];
  const problems = [];
  String(text).split('\n').forEach((raw, index) => {
    const line = raw.trim();
    if (!line) return;
    const split = /^([^;\t]+)[;\t](.*)$/.exec(line);  // adres to wszystko przed pierwszym separatorem, reszta to hasło (może mieć średniki)
    const user = split ? split[1].trim() : '';
    const password = split ? split[2].trim() : '';
    if (!user.includes('@') || /\s/.test(user) || !password) {
      problems.push('linia ' + (index + 1) + ': oczekiwano „adres;hasło”');
    } else {
      entries.push({user, password});
    }
  });
  if (entries.length > BULK_LIMIT) problems.push('najwyżej ' + BULK_LIMIT + ' skrzynek naraz');
  return {entries, problems};
}

async function addManyMailboxes() {
  const {entries, problems} = parseMailboxLines(el('bulkMailboxText').value);
  if (problems.length) { el('bulkMailboxProblems').textContent = problems.slice(0, 5).join('\n'); return false; }
  if (!entries.length) { el('bulkMailboxProblems').textContent = 'Wklej przynajmniej jedną linię.'; return false; }
  let added = 0;
  for (const entry of entries) {
    if (!(await addMailbox(entry))) break;
    added += 1;
  }
  if (added) toast('Dodano ' + added + ' skrzynek do formularza. Sprawdź i kliknij „Zapisz zmiany”.', 'ok');
  return added === entries.length;
}

function removeMailbox(slot) {
  const group = CONFIG.groups.find((g) => g.mailbox === Number(slot));
  if (!group) return;
  const saved = group.fields.some((field) => field.set || (field.kind !== 'secret' && field.value));
  if (saved) {
    REMOVED.add(group.mailbox);
    for (const field of group.fields) {
      el('c_' + field.key).value = field.kind === 'bool' || field.kind === 'choice' ? INITIAL[field.key] : '';
    }
    el('card_' + group.key).hidden = true;
  } else {  // karta dodana w tej sesji i jeszcze niezapisana: znika bez śladu
    CONFIG.groups = CONFIG.groups.filter((g) => g !== group);
    el('card_' + group.key).remove();
  }
  updateSearchVisibility();
}

// Jeden nasłuch na kontenerze: karty są przerysowywane (innerHTML), więc przyciski „Usuń skrzynkę” nie mają własnych.
el('groups').addEventListener('click', (event) => {
  const target = event.target;
  if (target && target.className === 'cfg-remove') { removeMailbox(target.dataset.slot); return; }
  const toggle = target && target.closest ? target.closest('.cfg-toggle') : null;
  if (toggle) setCollapsed(toggle.closest('.card'), toggle.getAttribute('aria-expanded') === 'true');
});

el('groups').addEventListener('change', (event) => {
  const card = event.target && event.target.closest ? event.target.closest('.card') : null;
  const group = card ? CONFIG.groups.find((g) => 'card_' + g.key === card.id) : null;
  if (group) refreshSummary(group);
});

function render() {
  INITIAL = {};
  REMOVED = new Set();
  const many = CONFIG.groups.filter(isMailbox).length + 1 >= COLLAPSE_FROM;
  el('groups').innerHTML = CONFIG.groups.map((group) => {
    for (const field of group.fields) INITIAL[field.key] = field.kind === 'secret' ? '' : fieldValue(field);
    return groupHtml(group, many && isMailbox(group));
  }).join('');
  CONFIG.groups.filter((group) => group.mailbox).forEach(refreshSummary);
  updateSearchVisibility();
  filterMailboxes();
  el('banners').innerHTML = CONFIG.file_exists ? '' : '<div class="banner warn">Plik config.env nie istnieje — zapis go utworzy.</div>';
}

function collect() {
  const values = {};
  const clear = [];
  for (const group of CONFIG.groups) {
    const removed = REMOVED.has(group.mailbox);
    for (const field of group.fields) {
      const current = el('c_' + field.key).value;
      if (field.kind === 'secret') {
        if (current) values[field.key] = current;
        const box = el('x_' + field.key);
        if ((box && box.checked) || (removed && field.set)) clear.push(field.key);
      } else if (removed) {
        if (field.value) values[field.key] = '';  // pusta wartość usuwa klucz z pliku
      } else if (current !== INITIAL[field.key]) {
        values[field.key] = current;
      }
    }
  }
  return {values, clear};
}

function restartBanner(keys) {
  el('banners').innerHTML = '<div class="banner warn">Zmieniono ustawienia powiadomień (' + escapeHtml(keys.join(', '))
    + '). Zadziałają po ponownym uruchomieniu demona automatu. '
    + '<button id="restartBtn">Uruchom ponownie demona</button></div>';
  el('restartBtn').addEventListener('click', restartDaemon);
}

async function restartDaemon() {
  const {ok, data} = await api('/api/automat/daemon', {action: 'restart'});
  toast(ok ? 'Demon uruchomiony ponownie.' : (data.error || 'Nie udało się uruchomić demona.'), ok ? 'ok' : 'error');
  if (ok) el('banners').innerHTML = '';
}

async function load() {
  const {ok, data} = await apiGet('/api/config');
  if (!ok) { toast(data.error || 'Nie udało się wczytać konfiguracji.', 'error'); return; }
  CONFIG = data;
  render();
}

async function save() {
  const {values, clear} = collect();
  if (!Object.keys(values).length && !clear.length) { toast('Brak zmian do zapisania.'); return; }
  const {ok, data} = await api('/api/config', {values, clear});
  if (!ok) { toast(data.error || 'Nie udało się zapisać.', 'error'); return; }
  CONFIG = data;
  render();
  toast(data.changed.length ? 'Zapisano: ' + data.changed.length + ' zmian(a).' : 'Wartości były już takie same.', 'ok');
  if (data.restart_demon.length) restartBanner(data.restart_demon);
}

async function runTest(slot) {
  const {values, clear} = collect();
  if (Object.keys(values).length || clear.length) { toast('Najpierw zapisz zmiany — sprawdzam zapisane ustawienia.'); return; }
  const target = slot ? el('mbox_' + slot) : null;
  if (target) target.innerHTML = '<div class="mbox-bar"><span class="mbox-tag unknown">Łączę…</span></div>';
  el('smtpResult').textContent = slot ? '' : 'Łączę…';
  const {ok, data} = await api('/api/config/test-smtp', slot ? {slot} : {});
  if (!ok) { el('smtpResult').textContent = data.error || 'Nie udało się sprawdzić.'; refreshMailboxBars(); return; }
  CONFIG.mailboxes = data.mailboxes || CONFIG.mailboxes;
  refreshMailboxBars();
  const results = data.results || [];
  el('smtpResult').textContent = slot ? '' : results.length > 1
    ? results.map((r) => (r.ok ? 'OK: ' : 'BŁĄD: ') + r.message).join('\n')
    : (data.ok ? 'OK: ' : 'BŁĄD: ') + data.message;
  if (slot) toast(data.ok ? 'Skrzynka działa.' : 'Skrzynka nie działa: ' + data.message, data.ok ? 'ok' : 'error');
}

// Stan z demona (wyłączenia po błędach) odświeżamy co jakiś czas, żeby nie trzeba było przeładowywać strony.
async function pollMailboxes() {
  if (document.hidden || !CONFIG) return;
  const {ok, data} = await apiGet('/api/config/mailboxes');
  if (ok) { CONFIG.mailboxes = data.mailboxes; refreshMailboxBars(); }
}

async function testImap() {
  const {values, clear} = collect();
  if (Object.keys(values).length || clear.length) { toast('Najpierw zapisz zmiany — sprawdzam zapisane ustawienia.'); return; }
  el('imapResult').textContent = 'Łączę…';
  const {ok, data} = await api('/api/config/test-imap', {});
  if (!ok) { el('imapResult').textContent = data.error || 'Nie udało się sprawdzić.'; return; }
  const results = data.results || [];
  el('imapResult').textContent = results.length
    ? results.map((r) => (r.ok ? 'OK: ' : 'BŁĄD: ') + r.address + ': ' + r.message).join('\n')
    : (data.ok ? 'OK: ' : 'BŁĄD: ') + data.message;
}

el('saveBtn').addEventListener('click', save);
el('reloadBtn').addEventListener('click', render);
el('smtpBtn').addEventListener('click', () => runTest(null));
el('imapBtn').addEventListener('click', testImap);
el('addMailboxBtn').addEventListener('click', () => addMailbox());
el('mailboxSearch').addEventListener('input', filterMailboxes);
el('bulkMailboxBtn').addEventListener('click', () => {
  el('bulkMailboxProblems').textContent = '';
  el('bulkMailboxDialog').showModal();
});
el('bulkMailboxForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (await addManyMailboxes()) {
    el('bulkMailboxText').value = '';
    el('bulkMailboxDialog').close();
  }
});
el('bulkMailboxCancel').addEventListener('click', () => el('bulkMailboxDialog').close());
el('groups').addEventListener('click', (event) => {
  const target = event.target && event.target.closest ? event.target.closest('.mbox-test') : null;
  if (target) runTest(Number(target.dataset.slot));
});
setInterval(pollMailboxes, 15000);
load();
