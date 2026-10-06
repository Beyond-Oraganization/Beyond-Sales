'use strict';
// Zakładka „Konfiguracja”: formularz ustawień z config.env. Sekrety są tylko do zapisu (serwer nie odsyła ich wartości);
// wysyłamy wyłącznie pola, które człowiek zmienił. Cały tekst z serwera przechodzi przez escapeHtml.
const el = (id) => document.getElementById(id);
let CONFIG = null;
let INITIAL = {};  // klucz -> wartość pokazana w polu (do wykrycia zmian)

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

function render() {
  INITIAL = {};
  el('groups').innerHTML = CONFIG.groups.map((group) => {
    for (const field of group.fields) INITIAL[field.key] = field.kind === 'secret' ? '' : fieldValue(field);
    return '<section class="card"><h2>' + escapeHtml(group.title) + '</h2><p class="muted group-note">'
      + escapeHtml(group.description) + '</p><div class="cfg-grid">' + group.fields.map(fieldHtml).join('') + '</div></section>';
  }).join('');
  el('banners').innerHTML = CONFIG.file_exists ? '' : '<div class="banner warn">Plik config.env nie istnieje — zapis go utworzy.</div>';
}

function collect() {
  const values = {};
  const clear = [];
  for (const group of CONFIG.groups) {
    for (const field of group.fields) {
      const current = el('c_' + field.key).value;
      if (field.kind === 'secret') {
        if (current) values[field.key] = current;
        const box = el('x_' + field.key);
        if (box && box.checked) clear.push(field.key);
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

async function testSmtp() {
  const {values, clear} = collect();
  if (Object.keys(values).length || clear.length) { toast('Najpierw zapisz zmiany — sprawdzam zapisane ustawienia.'); return; }
  el('smtpResult').textContent = 'Łączę…';
  const {ok, data} = await api('/api/config/test-smtp', {});
  el('smtpResult').textContent = ok ? (data.ok ? 'OK: ' : 'BŁĄD: ') + data.message : (data.error || 'Nie udało się sprawdzić.');
}

el('saveBtn').addEventListener('click', save);
el('reloadBtn').addEventListener('click', render);
el('smtpBtn').addEventListener('click', testSmtp);
load();
