function escapeHtml(s){
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function sleep(ms){ return new Promise(resolve => setTimeout(resolve, ms)); }

async function readJson(res){
  try { return await res.json(); } catch (err) { return {}; }
}

// Każde POST musi iść z Content-Type: application/json — serwer odrzuca resztę (ochrona przed CSRF).
async function api(path, payload){
  const res = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload || {}),
  });
  return {ok: res.ok, status: res.status, data: await readJson(res)};
}

async function apiGet(path){
  const res = await fetch(path);
  return {ok: res.ok, status: res.status, data: await readJson(res)};
}

// Kopiuje tekst do schowka: navigator.clipboard, a gdy przeglądarka odmówi albo go nie ma, zaznaczone pole tekstowe + execCommand.
// Zwraca true, gdy się udało; komunikat („Skopiowano…”) pokazuje wywołujący przez showToast.
async function copyToClipboard(text){
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (err) { /* brak uprawnień albo kontekstu bezpiecznego: próbujemy starą drogą */ }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.setAttribute('aria-hidden', 'true');
  document.body.appendChild(area);
  area.select();
  let done = false;
  try { done = Boolean(document.execCommand('copy')); } catch (err) { done = false; }
  area.remove();
  return done;
}

// Kopiuje tekst i od razu mówi, jak poszło (zielony komunikat albo błąd).
async function copyWithToast(text, message){
  if (await copyToClipboard(text)) showToast(message || 'Skopiowano.', 'ok');
  else showToast('Przeglądarka nie pozwoliła skopiować.', 'error');
}

// Przyciski z data-copy="tekst" (opcjonalnie data-copied="komunikat") kopiują ten tekst po kliknięciu; jedno nasłuchiwanie na całą stronę.
function bindCopyButtons(){
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-copy]');
    if (button) copyWithToast(button.getAttribute('data-copy'), button.getAttribute('data-copied'));
  });
}

// Eksport wskazanych wierszy: POST z listą id → plik zapisywany przez przeglądarkę (nazwa z Content-Disposition).
// Zwraca {ok, name, rows, size} albo {ok: false, error}.
async function downloadExport(payload){
  const res = await fetch('/api/export', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok){
    const data = await readJson(res);
    return {ok: false, status: res.status, error: data.error || 'Nie udało się przygotować pliku.'};
  }
  const blob = await res.blob();
  const match = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '');
  const name = match ? match[1] : 'Maile.' + (payload && payload.format || 'csv');
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  return {ok: true, name, rows: Number(res.headers.get('X-Export-Rows') || 0), size: blob.size};
}

// Plik z dowolnego punktu POST (np. raport): zapisuje go przeglądarka, nazwa z Content-Disposition. Zwraca {ok, name, size} albo {ok: false, error}.
async function downloadFile(url, payload, fallbackName){
  const res = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload || {})});
  if (!res.ok){
    const data = await readJson(res);
    return {ok: false, status: res.status, error: data.error || 'Nie udało się przygotować pliku.'};
  }
  const blob = await res.blob();
  const match = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '');
  const name = match ? match[1] : (fallbackName || 'plik');
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  return {ok: true, name, size: blob.size};
}

// ---- komunikaty i potwierdzenia (jedne dla wszystkich stron) ------------------------------------------------------
// showToast(tekst, ton): chwilowe okienko u góry (ton 'ok' | 'error'; błąd zostaje dłużej).
// showSheet({title, message, confirmLabel, cancelLabel, tone, focusCancel}) → Promise<boolean>: zamiennik alert()/confirm().
//   cancelLabel: null daje samo „OK” (jak alert), tone: 'danger' maluje przycisk na czerwono i ustawia fokus na „Anuluj”.
// Treść wstawiamy wyłącznie jako tekst (textContent): komunikaty zawierają nazwy firm ze skanowanych stron.

function toastHost() {
  let host = document.getElementById('toastHost');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toastHost';
    host.className = 'toast-host';
    document.body.appendChild(host);
  }
  return host;
}

// Komunikat u góry. `options.action` ({label, onClick}) dodaje przycisk (np. „Cofnij”), a `options.timeout` (ms) zmienia czas pokazania.
function showToast(text, tone, options) {
  const message = String(text == null ? '' : text);
  if (!message) return null;
  const o = options || {};
  const item = document.createElement('div');
  item.className = 'toast' + (tone ? ' ' + tone : '');
  item.setAttribute('role', tone === 'error' ? 'alert' : 'status');
  const close = () => { item.classList.add('is-leaving'); setTimeout(() => item.remove(), 200); };
  if (o.action && typeof o.action.onClick === 'function') {
    const label = document.createElement('span');
    label.textContent = message;
    item.appendChild(label);
    const button = document.createElement('button');
    button.className = 'toast-action';
    button.setAttribute('type', 'button');
    button.textContent = String(o.action.label || 'Cofnij');
    button.addEventListener('click', (event) => { event.stopPropagation(); close(); o.action.onClick(); });
    item.appendChild(button);
  } else {
    item.textContent = message;
  }
  item.addEventListener('click', close);
  toastHost().appendChild(item);
  setTimeout(close, o.timeout || (tone === 'error' ? 10000 : (o.action ? 8000 : 5000)));
  return item;
}

function sheetNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function showSheet(options) {
  const o = options || {};
  return new Promise((resolve) => {
    const dialog = sheetNode('dialog', 'sheet');
    const form = sheetNode('form', 'sheet-form');
    form.setAttribute('method', 'dialog');
    if (o.title) {
      const header = sheetNode('header');
      header.appendChild(sheetNode('h2', '', String(o.title)));
      form.appendChild(header);
    }
    const body = sheetNode('div', 'sheet-body');
    String(o.message == null ? '' : o.message).split('\n\n').forEach((part) => body.appendChild(sheetNode('p', '', part)));
    form.appendChild(body);
    const footer = sheetNode('footer');
    let cancel = null;
    if (o.cancelLabel !== null) {
      cancel = sheetNode('button', 'btn', String(o.cancelLabel || 'Anuluj'));
      cancel.setAttribute('type', 'submit');
      cancel.setAttribute('value', 'cancel');
      footer.appendChild(cancel);
    }
    const okButton = sheetNode('button', 'btn primary' + (o.tone === 'danger' ? ' danger' : ''), String(o.confirmLabel || 'OK'));
    okButton.setAttribute('type', 'submit');
    okButton.setAttribute('value', 'ok');
    footer.appendChild(okButton);
    form.appendChild(footer);
    dialog.appendChild(form);
    dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close('cancel'); });
    dialog.addEventListener('close', () => { const accepted = dialog.returnValue === 'ok'; dialog.remove(); resolve(accepted); });
    document.body.appendChild(dialog);
    dialog.showModal();
    ((o.tone === 'danger' || o.focusCancel) && cancel ? cancel : okButton).focus();
  });
}


// Skróty klawiszy strony: `map` to {'j': fn, 'ctrl+Enter': fn, '?': fn}. W polach tekstowych działają tylko klawisze z `options.whileTyping`
// (domyślnie Esc i Ctrl/Cmd+Enter), a przy otwartym oknie dialogowym żadne. Zwraca funkcję zdejmującą nasłuch.
function bindShortcuts(map, options) {
  const o = options || {};
  const allowedWhileTyping = new Set(o.whileTyping || ['Escape', 'ctrl+Enter', 'meta+Enter']);
  const handler = (event) => {
    if (event.defaultPrevented || event.altKey) return;
    const target = event.target || {};
    const typing = Boolean(target.isContentEditable) || /^(INPUT|TEXTAREA|SELECT)$/.test(String(target.tagName || ''));
    const key = (event.ctrlKey ? 'ctrl+' : '') + (event.metaKey ? 'meta+' : '') + event.key;
    if (typing && !allowedWhileTyping.has(key)) return;
    if (document.querySelector && document.querySelector('dialog[open]')) return;
    const action = Object.prototype.hasOwnProperty.call(map, key) ? map[key] : null;
    if (typeof action !== 'function') return;
    event.preventDefault();
    action(event);
  };
  document.addEventListener('keydown', handler);
  return () => document.removeEventListener('keydown', handler);
}

// Ściąga skrótów w arkuszu: `rows` to [[klawisze, opis], …].
function showShortcutHelp(rows) {
  return showSheet({title: 'Skróty klawiszy', message: rows.map((row) => row[0] + '   ' + row[1]).join('\n'),
                    confirmLabel: 'Zamknij', cancelLabel: null});
}

// Odpytuje `fn` co `options.interval` ms (domyślnie 60 s), ale tylko gdy karta jest widoczna; po powrocie do karty odpytuje od razu.
// Zwraca funkcję zatrzymującą. Błąd w `fn` nie przerywa odpytywania.
function pollWhileVisible(fn, options) {
  const interval = (options && options.interval) || 60000;
  let timer = null;
  let stopped = false;
  const schedule = () => { clearTimeout(timer); if (!stopped) timer = setTimeout(tick, interval); };
  const tick = async () => {
    if (!document.hidden) { try { await fn(); } catch (error) { /* następny przebieg spróbuje ponownie */ } }
    schedule();
  };
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !stopped) { clearTimeout(timer); tick(); } });
  schedule();
  return () => { stopped = true; clearTimeout(timer); };
}

// Pole tekstowe rośnie z treścią (do `max` px).
function autosize(textarea, max) {
  const limit = max || 320;
  const fit = () => { textarea.style.height = 'auto'; textarea.style.height = Math.min(textarea.scrollHeight, limit) + 'px'; };
  textarea.addEventListener('input', fit);
  fit();
  return fit;
}

// ---- budowanie DOM bez innerHTML (strony Odpowiedzi i Leady: treść pochodzi od osób trzecich) ----------------------------

// Węzeł DOM bez innerHTML: `props.text` to tekst, `class`/`aria-*`/`data-*` to atrybuty, `on*` to nasłuchy, reszta dzieci (tekst albo węzły).
function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = String(value);
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

function setChildren(node, children) {
  node.replaceChildren(...children.filter(Boolean));
}

const MONTHS = ['sty', 'lut', 'mar', 'kwi', 'maj', 'cze', 'lip', 'sie', 'wrz', 'paź', 'lis', 'gru'];
const pad = (n) => String(n).padStart(2, '0');

// „12:04” dla dzisiejszych, „5 paź” dla tegorocznych, „5 paź 2025” dla starszych; pełny czas w title.
function shortDate(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return pad(date.getHours()) + ':' + pad(date.getMinutes());
  return date.getDate() + ' ' + MONTHS[date.getMonth()] + (date.getFullYear() === now.getFullYear() ? '' : ' ' + date.getFullYear());
}

function longDate(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.getDate() + ' ' + MONTHS[date.getMonth()] + ' ' + date.getFullYear() + ', ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
}

// Tylko http(s): adres strony z bazy może pochodzić ze skanu cudzych stron.
function safeUrl(raw) {
  try {
    const url = new URL(String(raw || '').includes('://') ? String(raw) : 'https://' + String(raw || ''));
    return (url.protocol === 'http:' || url.protocol === 'https:') && url.hostname.includes('.') ? url.href : '';
  } catch (error) { return ''; }
}

// Na telefonie menu przewija się w poziomie: aktywna zakładka ma być widoczna.
(() => {
  if (typeof document.querySelector !== 'function') return;
  const current = document.querySelector('.topnav-link[aria-current="page"]');
  if (current && typeof current.scrollIntoView === 'function') current.scrollIntoView({inline: 'center', block: 'nearest'});
})();
