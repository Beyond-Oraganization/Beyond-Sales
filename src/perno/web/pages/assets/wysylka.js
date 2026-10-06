const F = PernoFilters, S = PernoSelection, K = PernoKit;
const STATUS_LABELS = APP.statusMeta;
const MANUAL = APP.manualStatuses;
const COUNTRIES = APP.countries;  // [[kod, nazwa po polsku, flaga]], Polska pierwsza
const el = (id) => document.getElementById(id);

const ctx = F.createContext({
  statusMeta: APP.statusMeta, sendable: APP.sendable, countries: APP.countries, segmentOrder: APP.segmentOrder,
});
const selection = S.create();
let STATUS = {smtp_configured: false, smtp_user: '', sent_today: 0};
let expandedId = null;       // wiersz z rozwiniętymi szczegółami
const detailCache = {};      // id → pełny wiersz (z treścią maila) dla rozwiniętych wierszy
let loadingDetail = null;
let bulk = null;
let sending = false;

const sendBtn = el('sendBtn');
const logEl = el('log');

function logLine(text){
  logEl.style.display = 'block';
  logEl.textContent += text + "\n";
  logEl.scrollTop = logEl.scrollHeight;
}

function isLocked(row){ return row.status === 'wysylanie'; }

async function loadStatus(){
  const {ok, data} = await apiGet('/api/status');
  const banner = el('configBanner');
  if (!ok){
    banner.innerHTML = `<div class="banner warn">${escapeHtml(data.error || 'Nie udało się odczytać stanu aplikacji.')}</div>`;
    return;
  }
  STATUS = data;
  el('sentToday').textContent = `Dziś wysłano: ${data.sent_today}`;
  if (!data.smtp_configured){
    banner.innerHTML = `<div class="banner warn">Brak hasła SMTP w config.env — uzupełnij je, zanim spróbujesz wysłać. Panel działa (podgląd/edycja), ale wysyłka zwróci błąd.</div>`;
  } else {
    banner.innerHTML = `<div class="banner ok">Wysyłka skonfigurowana z konta: ${escapeHtml(data.smtp_user)}</div>`;
  }
}

// ---- lista ----------------------------------------------------------------------------------

function statusSelect(row){
  const info = STATUS_LABELS[row.status] || [row.status, '#6b7280'];
  const values = MANUAL.includes(row.status) ? MANUAL : [...MANUAL, row.status];
  const options = values.map(value =>
    `<option value="${escapeHtml(value)}" ${value === row.status ? 'selected' : ''}>${escapeHtml((STATUS_LABELS[value] || [value])[0])}</option>`
  ).join('');
  return `<select data-id="${escapeHtml(row.id)}" data-field="status" class="badge-select" style="--c:${escapeHtml(info[1])}">${options}</select>`;
}

function cells(row){
  const lock = isLocked(row) ? 'disabled' : '';
  return `
    <td data-col="segment">${escapeHtml(row.segment)}</td>
    <td data-col="company" class="company-cell">${escapeHtml(row.company)}${K.websiteLink(row.website)}</td>
    <td data-col="country" class="country-cell">${K.countryCell(row, ctx)}</td>
    <td data-col="email"><input type="text" data-field="email" value="${escapeHtml(row.email)}" placeholder="wpisz e-mail…" ${lock}></td>
    <td data-col="subject"><input type="text" data-field="subject" value="${escapeHtml(row.subject)}" ${lock}></td>
    <td data-col="status">${statusSelect(row)}</td>
    <td data-col="generated" class="muted">${escapeHtml(K.shortDate(row.generated_at))}</td>
    <td data-col="sent" class="muted">${escapeHtml(K.shortDate(row.sent_at))}</td>`;
}

function decorate(tr, row){
  tr.querySelector('.company-cell').addEventListener('click', () => {
    expandedId = expandedId === row.id ? null : row.id;
    view.redraw();
  });
  const siteLink = tr.querySelector('.site-link');
  if (siteLink) siteLink.addEventListener('click', e => e.stopPropagation());
  tr.querySelectorAll('input[data-field]').forEach(inp => {
    inp.addEventListener('click', e => e.stopPropagation());
    inp.addEventListener('blur', () => saveField(row.id, inp.dataset.field, inp.value));
  });
  const statusEl = tr.querySelector('select[data-field="status"]');
  statusEl.addEventListener('click', e => e.stopPropagation());
  statusEl.addEventListener('change', () => saveField(row.id, 'status', statusEl.value));
}

const view = K.createListView({
  key: 'wysylka', ctx, selection, defaultSize: 100,
  filtersEl: el('filterBar'), tableEl: el('listTable'), tbodyEl: el('listBody'),
  pagerEl: el('pager'), selectEl: el('selectBar'), emptyEl: el('emptyState'),
  canSelect: (row) => !isLocked(row),
  lockedStatuses: ['wysylanie'],   // „zaznacz wszystkie pasujące” pomija wiersze, które właśnie wysyła program
  rowClass: (row) => isLocked(row) ? 'is-locked' : '',
  cells, decorate,
  detail: (row) => renderDetail(row),
  selectItems: [{
    label: 'Zaznacz gotowe do wysłania',
    run: async ({ids}) => { selection.add((await ids({ready: true})).ids); },
  }],
  onChange: () => { if (bulk) bulk.refresh(); },
  onError: (message) => { if (bulk) bulk.toast(message, 'error'); else alert(message); },
});

// ---- zapis pól i rozwinięty wiersz ----------------------------------------------------------

function rowById(id){ return view.pageRows().find(r => r.id === id); }

function replaceRow(row){
  const rows = view.pageRows();
  const idx = rows.findIndex(r => r.id === row.id);
  row.has_body = !!row.body_html;
  if (idx !== -1) rows[idx] = row;
  if (detailCache[row.id]) detailCache[row.id] = row;
  if (isLocked(row)) selection.remove([row.id]);
}

async function saveField(id, field, value){
  const bodyField = field === 'body_html' || field === 'body_text';
  const baseline = bodyField ? (detailCache[id] || {}) : (rowById(id) || {});
  if ((baseline[field] || '') === value) return;
  const {ok, data} = await api('/api/update-row', {id, [field]: value});
  if (data.row) replaceRow(data.row);
  if (!ok) alert(data.error || 'Nie udało się zapisać zmiany.');
  view.redraw();    // od razu z tego, co zwrócił serwer…
  view.refresh();   // …a liczniki i przynależność do filtra serwer policzy od nowa (wiersz może już nie pasować do widoku)
}

function countrySelect(row, lock){
  const options = ['<option value="">— nieznany —</option>'].concat(COUNTRIES.map(c =>
    `<option value="${escapeHtml(c[0])}" ${c[0] === row.country ? 'selected' : ''}>${c[2]} ${escapeHtml(c[1])}</option>`));
  return `<select data-id="${escapeHtml(row.id)}" data-field="country" ${lock}>${options.join('')}</select>`;
}

async function loadDetail(id){
  if (loadingDetail === id) return;
  loadingDetail = id;
  const {ok, data} = await apiGet('/api/maile/row?id=' + encodeURIComponent(id));
  loadingDetail = null;
  if (!ok){
    alert(data.error || 'Nie udało się wczytać szczegółów wiersza.');
    expandedId = null;
  } else {
    detailCache[id] = data.row;
  }
  view.redraw();
}

function renderDetail(row){
  if (expandedId !== row.id) return null;
  const tr = document.createElement('tr');
  tr.className = 'detail-row';
  const td = document.createElement('td');
  td.colSpan = 9;
  tr.appendChild(td);
  const full = detailCache[row.id];
  if (!full){
    td.innerHTML = '<span class="muted">Wczytuję treść maila…</span>';
    loadDetail(row.id);
    return tr;
  }
  const lock = isLocked(full) ? 'disabled' : '';
  td.innerHTML = `
    <div class="profile-grid">
      <label>Kraj ${countrySelect(full, lock)}</label>
      <label>Miasto <input type="text" data-id="${escapeHtml(full.id)}" data-field="city" value="${escapeHtml(full.city)}" ${lock}></label>
      <label class="wide">Adres <input type="text" data-id="${escapeHtml(full.id)}" data-field="address" value="${escapeHtml(full.address)}" placeholder="ulica, kod pocztowy…" ${lock}></label>
      <label class="wide">Opis działalności <textarea data-id="${escapeHtml(full.id)}" data-field="description" rows="2" ${lock}>${escapeHtml(full.description)}</textarea></label>
    </div>
    <div class="detail-grid">
      <div>
        <h4>Treść HTML (edytowalna)</h4>
        <textarea data-id="${escapeHtml(full.id)}" data-field="body_html" ${lock}>${escapeHtml(full.body_html)}</textarea>
      </div>
      <div>
        <h4>Treść plain text (edytowalna)</h4>
        <textarea data-id="${escapeHtml(full.id)}" data-field="body_text" ${lock}>${escapeHtml(full.body_text)}</textarea>
      </div>
    </div>
    ${full.error ? `<div class="error-box"><strong>Błąd:</strong> ${escapeHtml(full.error)}</div>` : ''}
  `;
  td.querySelectorAll('textarea').forEach(ta => {
    ta.addEventListener('blur', () => saveField(ta.dataset.id, ta.dataset.field, ta.value));
  });
  td.querySelectorAll('.profile-grid input[data-field]').forEach(inp => {
    inp.addEventListener('blur', () => saveField(inp.dataset.id, inp.dataset.field, inp.value));
  });
  td.querySelector('.profile-grid select[data-field="country"]').addEventListener('change', (e) => {
    saveField(e.target.dataset.id, 'country', e.target.value);
  });
  return tr;
}

// ---- wczytywanie i odświeżanie --------------------------------------------------------------

async function loadRows(){
  Object.keys(detailCache).forEach(key => delete detailCache[key]);  // po przeładowaniu szczegóły mogą być nieaktualne
  await view.refresh();
  return true;
}

el('reloadBtn').addEventListener('click', async () => {
  if (await loadRows()) { await loadStatus(); if (bulk) bulk.toast('Lista przeładowana.'); }
});

el('refreshBtn').addEventListener('click', async () => {
  const {ok, data} = await api('/api/regenerate', {rows: false});
  if (!ok){ alert(data.error || 'Nie udało się odświeżyć danych.'); return; }
  await loadRows();
  const parts = Object.entries(data).filter(([key]) => key !== 'rows')
    .map(([segment, s]) => `${segment}: ${s.created} nowych, ${s.regenerated} przeliczonych`);
  logLine(`Odświeżono — ${parts.join('; ')}.`);
});

// ---- widoczność kolumn ----------------------------------------------------------------------

const COLUMN_CHOICES = [
  ['segment', 'Segment'], ['country', 'Kraj'], ['email', 'E-mail'], ['subject', 'Temat'],
  ['status', 'Status'], ['generated', 'Wygenerowano'], ['sent', 'Wysłano'],
];

function applyHiddenColumns(hidden){
  COLUMN_CHOICES.forEach(([key]) => el('listTable').classList.toggle('hide-' + key, hidden.includes(key)));
}

function setupColumns(){
  const pop = el('columnsPop'), panel = pop.querySelector('.kit-popover');
  let hidden = (K.readStore('columns.wysylka', []) || []).filter(k => COLUMN_CHOICES.some(c => c[0] === k));
  const render = () => {
    panel.innerHTML = '<label class="kit-option"><input type="checkbox" checked disabled> Firma <span class="n">zawsze</span></label>' +
      COLUMN_CHOICES.map(([key, label]) =>
        `<label class="kit-option"><input type="checkbox" data-col="${key}" ${hidden.includes(key) ? '' : 'checked'}> ${escapeHtml(label)}</label>`).join('');
  };
  K.popover(pop.querySelector('button'), panel);
  panel.addEventListener('change', (e) => {
    const key = e.target.dataset.col;
    if (!key) return;
    hidden = e.target.checked ? hidden.filter(k => k !== key) : hidden.concat([key]);
    K.writeStore('columns.wysylka', hidden);
    applyHiddenColumns(hidden);
  });
  render();
  applyHiddenColumns(hidden);
}

// ---- wysyłka --------------------------------------------------------------------------------

function describeOutcome(status, data){
  if (status === 429) return `  LIMIT: ${data.error}`;
  if (data.outcome === 'sent') return '  OK';
  if (data.outcome === 'sent_unrecorded') return `  ${data.message}`;
  if (data.outcome === 'error') return `  BŁĄD: ${data.message || (data.row && data.row.error) || ''}`;
  return `  POMINIĘTO: ${data.error || data.message || 'nic nie wysłano'}`;
}

// Gotowe do wysyłki: zaznaczone, w kolejności widoku, ze statusem „Do wysłania”/„Błąd” oraz e-mailem i treścią — to
// sprawdza serwer, bo zaznaczone firmy nie muszą być na bieżącej stronie.
async function readyIds(){
  const {ok, data} = await api('/api/mailing/selection', {ids: selection.toArray(), ready_ids: true, state: {sort: view.getState().sort}});
  if (!ok){ alert(data.error || 'Nie udało się sprawdzić zaznaczonych firm.'); return null; }
  return data.ready_ids;
}

async function foreignWarning(ids){
  const {ok, data} = await api('/api/mailing/selection', {ids});
  if (!ok) return '';
  const counts = data.foreign || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (!total) return '';
  const breakdown = Object.entries(counts).sort((a, b) => b[1] - a[1])
    .map(([code, n]) => `${ctx.countryNames[code] || code} ${n}`).join(', ');
  return `UWAGA: ${total} z tych maili jedzie do firm spoza Polski (${breakdown}), a szablony są po polsku.`;
}

sendBtn.addEventListener('click', async () => {
  const dailyCap = parseInt(el('dailyCap').value, 10) || 50;
  const delayMin = parseFloat(el('delayMin').value) || 0;
  const delayMax = Math.max(delayMin, parseFloat(el('delayMax').value) || delayMin);

  await loadStatus();
  const sentToday = STATUS.sent_today || 0;
  const remainingCap = Math.max(0, dailyCap - sentToday);

  const selectedCount = selection.size;
  let ids = await readyIds();
  if (ids === null) return;
  const notReady = selectedCount - ids.length;
  if (!ids.length){ alert('Żadna z zaznaczonych firm nie jest gotowa do wysyłki (potrzebny status „Do wysłania” lub „Błąd”, e-mail i treść).'); return; }
  if (ids.length > remainingCap){
    if (!confirm(`Gotowych jest ${ids.length}, ale dzienny limit pozwala jeszcze na ${remainingCap} (dziś wysłano już ${sentToday}). Wysłać pierwsze ${remainingCap}?`)) return;
    ids = ids.slice(0, remainingCap);
  }
  if (!ids.length){ alert('Nic do wysłania (limit dzienny wyczerpany).'); return; }
  if (!STATUS.smtp_configured){ alert('Brak hasła SMTP w config.env — uzupełnij je, zanim spróbujesz wysłać.'); return; }
  const lines = [`Wysyłam ${ids.length} maili z konta ${STATUS.smtp_user || '?'}.`];
  if (notReady > 0) lines.push(`Pominę ${notReady} zaznaczonych firm, które nie są gotowe do wysyłki (status, e-mail albo treść).`);
  const foreign = await foreignWarning(ids);
  if (foreign) lines.push(foreign);
  lines.push('Kontynuować?');
  if (!confirm(lines.join('\n\n'))) return;

  sending = true;
  sendBtn.disabled = true;
  logEl.textContent = '';
  for (let i = 0; i < ids.length; i++){
    const id = ids[i];
    const row = rowById(id);
    logLine(`Wysyłam ${id}${row ? ` (${row.company})` : ''}…`);
    let stop = false;
    try {
      const {status, data} = await api('/api/send-one', {id, daily_cap: dailyCap});
      if (data.row) replaceRow(data.row);
      logLine(describeOutcome(status, data));
      stop = status === 429;
    } catch (err) {
      logLine(`  BŁĄD sieci: ${err}`);
    }
    selection.remove([id]);
    view.redraw();
    if (stop) break;
    if (i < ids.length - 1) await sleep((delayMin + Math.random() * (delayMax - delayMin)) * 1000);
  }
  logLine('Gotowe.');
  sending = false;
  view.selectionChanged();
  await Promise.all([loadStatus(), view.refresh()]);
  if (bulk) bulk.refresh();
});

// ---- start ----------------------------------------------------------------------------------

bulk = createBulkUI({
  view, selection, ctx, reload: loadRows,
  els: {
    count: el('actionCount'), statusBtn: el('statusBtn'), statusMenu: el('statusMenu'), dataBtn: el('dataBtn'),
    copyBtn: el('copyBtn'), exportBtn: el('exportBtn'), exportMenu: el('exportMenu'), undoBtn: el('undoBtn'),
    sendBtn: sendBtn, toast: el('toast'),
    dialog: el('bulkDialog'), form: el('bulkForm'), title: el('bulkTitle'), body: el('bulkBody'),
    cancel: el('bulkCancel'), apply: el('bulkApply'),
  },
});
setupColumns();
loadStatus();
view.start().then(() => bulk.loadUndo());
