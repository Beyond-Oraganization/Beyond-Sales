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

function showToast(text, tone) {
  const message = String(text == null ? '' : text);
  if (!message) return null;
  const item = document.createElement('div');
  item.className = 'toast' + (tone ? ' ' + tone : '');
  item.setAttribute('role', tone === 'error' ? 'alert' : 'status');
  item.textContent = message;
  const close = () => { item.classList.add('is-leaving'); setTimeout(() => item.remove(), 200); };
  item.addEventListener('click', close);
  toastHost().appendChild(item);
  setTimeout(close, tone === 'error' ? 10000 : 5000);
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
