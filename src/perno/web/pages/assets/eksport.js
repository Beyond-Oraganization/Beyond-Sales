const F = PernoFilters, S = PernoSelection, K = PernoKit;
const el = (id) => document.getElementById(id);
const summary_ = el('summary'), download = el('download'), exportMsg = el('exportMsg');

const ctx = F.createContext({
  statusMeta: APP.statusMeta, sendable: APP.sendable, countries: APP.countries, segmentOrder: APP.segmentOrder,
});
const selection = S.create();
const PREFS_KEY = 'export.prefs';

let META = {columns: [], column_groups: [], column_presets: []};
let columns = new Set();   // kolumny, które trafią do pliku
let scope = 'filtered';    // 'filtered' = wszystkie pasujące do filtrów, 'selected' = tylko zaznaczone
let lastSelectionSize = 0;
let busy = false;

const view = K.createListView({
  key: 'eksport', ctx, selection, defaultSize: 50, rowClickToggles: true,
  filtersEl: el('filterBar'), tableEl: el('listTable'), tbodyEl: el('listBody'),
  pagerEl: el('pager'), selectEl: el('selectBar'), emptyEl: el('emptyState'),
  cells: (row) => `
    <td class="company-cell">${escapeHtml(row.company)}${K.websiteLink(row.website)}</td>
    <td>${escapeHtml(row.segment)}</td>
    <td class="country-cell">${K.countryCell(row, ctx)}</td>
    <td>${row.email ? escapeHtml(row.email) : '<span class="muted">—</span>'}</td>
    <td>${K.badge(row.status, ctx)}</td>
    <td class="dim">${escapeHtml(K.shortDate(row.generated_at))}</td>
    <td class="dim">${escapeHtml(K.shortDate(row.sent_at))}</td>`,
  onChange: onViewChange,
});

function format(){ return document.querySelector('input[name=format]:checked').value; }
function delimiter(){ return document.querySelector('input[name=delimiter]:checked').value; }

function setScope(next){
  scope = next;
  document.querySelector(`input[name=scope][value=${next}]`).checked = true;
}

// Pierwsze zaznaczenie przełącza zakres na „zaznaczone”, wyczyszczenie wraca do „pasujących”; radio można zmienić ręcznie.
function onViewChange(info){
  const size = selection.size;
  if (info.reason === 'selection' && lastSelectionSize === 0 && size > 0) setScope('selected');
  else if (size === 0 && scope === 'selected') setScope('filtered');
  lastSelectionSize = size;
  updateSummary();
}

// Co wysłać serwerowi: zaznaczone jako id (układa je jak listę na ekranie) albo cały widok jako stan filtrów i sortowania —
// przy 50 tys. firm nie przesyłamy listy id, serwer sam wybiera wiersze.
function exportRows(){
  return scope === 'selected' ? {ids: selection.toArray(), sort: view.getState().sort} : view.exportScope();
}

function exportCount(){
  return scope === 'selected' ? selection.size : view.total();
}

function showMessage(text, kind){
  exportMsg.textContent = text || '';
  exportMsg.className = 'export-msg' + (kind ? ' ' + kind : '');
}

function updateSummary(){
  const matching = view.total(), selected = selection.size, summary = view.summary();
  const outside = summary && !view.summaryPending() && !view.onlySelected() ? Math.max(0, selected - summary.in_view) : 0;
  el('scopeFilteredN').textContent = `(${matching})`;
  el('scopeSelectedN').textContent = `(${selected})`;
  el('scopeSelected').disabled = selected === 0;
  if (selected === 0 && scope === 'selected') setScope('filtered');

  const n = exportCount();
  let text = `Do pliku trafi ${n} ${K.plural(n, 'wiersz', 'wiersze', 'wierszy')}`;
  if (scope === 'selected') text += outside ? ` (zaznaczone; ${outside} spoza bieżącego filtra)` : ' (zaznaczone)';
  else text += ' (wszystkie pasujące do filtrów)';
  summary_.textContent = `${text}, kolumn: ${columns.size}.`;
  if (n === 0) summary_.textContent = 'Nie ma czego eksportować — zmień filtry albo zaznacz wiersze.';
  else if (columns.size === 0) summary_.textContent = 'Wybierz przynajmniej jedną kolumnę.';
  download.disabled = busy || n === 0 || columns.size === 0;
}

// ---- kolumny ------------------------------------------------------------------------------------

function sameSet(a, b){ return a.size === b.size && [...a].every((x) => b.has(x)); }

function renderColumns(){
  el('columnsCount').textContent = `(${columns.size} z ${META.columns.length})`;
  el('columnPresets').innerHTML = META.column_presets.map((p) =>
    `<button type="button" class="kit-btn${sameSet(columns, new Set(p.columns)) ? ' is-active' : ''}" data-preset="${escapeHtml(p.key)}">${escapeHtml(p.label)}</button>`).join('');
  el('columnGroups').innerHTML = META.column_groups.map((group) => {
    const own = META.columns.filter((c) => c.group === group);
    return `<div class="column-group"><div class="column-group-head"><b>${escapeHtml(group)}</b>` +
      `<button type="button" data-group="${escapeHtml(group)}" data-on="1">wszystkie</button>` +
      `<button type="button" data-group="${escapeHtml(group)}" data-on="0">żadne</button></div>` +
      own.map((c) => `<label class="choice" title="Pole w bazie: ${escapeHtml(c.name)}"><input type="checkbox" data-col="${escapeHtml(c.name)}"${columns.has(c.name) ? ' checked' : ''}> ` +
        `${escapeHtml(c.label)}</label>`).join('') + '</div>';
  }).join('');
}

function setColumns(names){
  columns = new Set(names);
  savePrefs();
  renderColumns();
  updateSummary();
}

function savePrefs(){
  K.writeStore(PREFS_KEY, {columns: [...columns], format: format(), delimiter: delimiter()});
}

function loadPrefs(){
  const prefs = K.readStore(PREFS_KEY, null) || {};
  const known = new Set(META.columns.map((c) => c.name));
  const saved = Array.isArray(prefs.columns) ? prefs.columns.filter((c) => known.has(c)) : [];
  const standard = META.column_presets.find((p) => p.key === 'standard');
  columns = new Set(saved.length ? saved : (standard ? standard.columns : [...known]));
  ['xlsx', 'csv'].includes(prefs.format) && (document.querySelector(`input[name=format][value=${prefs.format}]`).checked = true);
  ['semicolon', 'comma'].includes(prefs.delimiter) && (document.querySelector(`input[name=delimiter][value=${prefs.delimiter}]`).checked = true);
  el('delimiterBox').hidden = format() !== 'csv';
}

el('columnPresets').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-preset]');
  if (b) setColumns(META.column_presets.find((p) => p.key === b.dataset.preset).columns);
});
el('columnGroups').addEventListener('change', (e) => {
  const name = e.target.dataset.col;
  if (!name) return;
  const next = new Set(columns);
  if (e.target.checked) next.add(name); else next.delete(name);
  setColumns(next);
});
el('columnGroups').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-group]');
  if (!b) return;
  const next = new Set(columns);
  META.columns.filter((c) => c.group === b.dataset.group).forEach((c) => { if (b.dataset.on === '1') next.add(c.name); else next.delete(c.name); });
  setColumns(next);
});

// ---- dane ---------------------------------------------------------------------------------------

function stampSnapshot(){
  el('snapshot').textContent = `dane z ${new Date().toLocaleTimeString('pl-PL')}`;
}

async function loadRows(){
  await view.refresh();
  stampSnapshot();
  return !view.error();
}

el('reload').addEventListener('click', async () => {
  showMessage('');
  if (await loadRows()) showMessage('Lista odświeżona.', 'ok');
});

// ---- pobieranie ---------------------------------------------------------------------------------

document.querySelectorAll('input[name=scope]').forEach((i) => i.addEventListener('change', () => { scope = i.value; updateSummary(); }));
document.querySelectorAll('input[name=format]').forEach((i) => i.addEventListener('change', () => {
  el('delimiterBox').hidden = format() !== 'csv';
  savePrefs();
}));
document.querySelectorAll('input[name=delimiter]').forEach((i) => i.addEventListener('change', savePrefs));

download.addEventListener('click', async () => {
  const n = exportCount();
  if (!n || !columns.size) return;
  busy = true;
  showMessage('');
  download.textContent = 'Przygotowuję plik…';
  updateSummary();
  let result;
  try {
    result = await downloadExport({format: format(), delimiter: delimiter(), ...exportRows(), columns: [...columns]});
  } catch (err) {
    result = {ok: false, error: 'Nie udało się połączyć z serwerem.'};
  }
  busy = false;
  download.textContent = 'Pobierz plik';
  if (result.ok){
    const missing = n - result.rows;
    showMessage(`Pobrano ${result.name} — ${result.rows} ${K.plural(result.rows, 'wiersz', 'wiersze', 'wierszy')}` +
      (missing > 0 ? `; ${missing} ${K.plural(missing, 'firmy', 'firm', 'firm')} z listy nie ma już w bazie.` : '.'), 'ok');
  } else {
    showMessage(result.error, 'error');
  }
  updateSummary();
});

async function init(){
  const {ok, data} = await apiGet('/api/export/meta');
  if (!ok){ summary_.textContent = data.error || 'Nie udało się wczytać danych.'; return; }
  META = data;
  loadPrefs();
  renderColumns();
  await view.start();
  stampSnapshot();
  updateSummary();
}

init();
