const el = (id) => document.getElementById(id);
const PAGE_SIZE = 50;
const SETTING_KEYS = ['concurrency', 'delay', 'depth', 'max_pages', 'max_mb', 'max_minutes'];
const BUDGET_KEYS = ['depth', 'max_pages', 'max_mb', 'max_minutes'];  // ustawiane przez profil

let META = {statuses: [], defaults: {}, limits: {}, contact: {}, reasons: {}, retryable: [], default_statuses: [], profiles: [], profile: ''};
let ROWS = [];
let TOTAL = 0;
let offset = 0;
let selected = new Set();
let expandedId = null;
let detailPage = 0;
let RUN = {aktywny: false, run: null, log: []};
let pollTimer = null;
let searchTimer = null;
let busy = false;

const tbody = el('tbody');
const statusFilter = el('statusFilter');

function safeUrl(url){ return /^https?:\/\//i.test(String(url || '')) ? String(url) : ''; }
function statusInfo(code){
  const found = META.statuses.find(s => s.code === code);
  return found ? [found.label, found.color] : [code, '#6b7280'];
}
function reasonText(code){ return code ? (META.reasons[code] || code) : ''; }
function fmtDate(value){ return value ? String(value).replace('T', ' ').slice(0, 16) : '—'; }
function fmtMb(bytes){ return bytes ? (bytes / 1048576).toFixed(1) : '0'; }
function linkTo(url, label){
  const safe = safeUrl(url);
  const text = escapeHtml(label == null ? url : label);
  return safe ? `<a href="${escapeHtml(safe)}" target="_blank" rel="noopener noreferrer">${text}</a>` : text;
}

function showBanner(kind, text){
  el('banners').insertAdjacentHTML('beforeend', `<div class="banner ${kind}">${escapeHtml(text)}</div>`);
}

// Komunikaty jednorazowe (wynik akcji) to wspólny toast; odświeżenie stanu ich nie kasuje.
function notify(kind, text){
  showToast(text, kind === 'warn' ? 'error' : kind);
}

function renderBanners(){
  el('banners').innerHTML = '';
  if (!META.contact.ok) showBanner('warn', 'Brak SKRAPER_CONTACT w config.env — ustaw adres e-mail lub URL, który bot poda w nagłówku User-Agent. Bez tego Start jest zablokowany.');
  else if (META.contact.source === 'smtp_user') showBanner('warn', `SKRAPER_CONTACT nie jest ustawiony — jako kontakt bota posłuży ${META.contact.value} (SMTP_USER).`);
  if (META.settings_error) showBanner('warn', META.settings_error);
  if (!META.bs4_ok) showBanner('warn', 'Brak pakietu beautifulsoup4 — uruchom: uv sync. Bez niego skraper nie wystartuje.');
  if (RUN.nieodpowiada) showBanner('warn', `Przebieg nie odpowiada od ${Math.round(RUN.heartbeat_age)} s. Jeśli proces został zamknięty, kliknij Stop — firmy wrócą do kolejki.`);
}

function renderChips(){
  const active = statusFilter.value;
  el('statusChips').innerHTML = META.statuses.map(s => {
    return `<span class="chip ${s.code === active ? 'active' : ''}" data-status="${escapeHtml(s.code)}" style="--c:${escapeHtml(s.color)}">${escapeHtml(s.label)}: ${s.count}</span>`;
  }).join('');
  statusFilter.innerHTML = '<option value="">Wszystkie statusy</option>' +
    META.statuses.map(s => `<option value="${escapeHtml(s.code)}">${escapeHtml(s.label)} (${s.count})</option>`).join('');
  statusFilter.value = active;
}

function readSettings(){
  const out = {};
  SETTING_KEYS.forEach(key => {
    const value = el('s_' + key).value;
    if (value !== '') out[key] = Number(value);
  });
  const profile = el('s_profile').value;
  if (profile) out.profile = profile;
  return out;
}

function profileInfo(code){ return (META.profiles || []).find(p => p.code === code); }

function renderProfiles(){
  const select = el('s_profile');
  select.innerHTML = (META.profiles || []).map(p => `<option value="${escapeHtml(p.code)}">${escapeHtml(p.label)}</option>`).join('');
  select.value = META.profile || '';
}

// Profil ustawia budżet na firmę (strony, głębokość, MB, minuty); liczby można potem poprawić ręcznie.
function applyProfile(){
  const info = profileInfo(el('s_profile').value);
  if (info) BUDGET_KEYS.forEach(key => { if (info.values[key] != null) el('s_' + key).value = info.values[key]; });
  onSettingsInput();
}

function renderProfileNote(){
  const info = profileInfo(el('s_profile').value);
  const differs = info && BUDGET_KEYS.some(key => Number(el('s_' + key).value) !== info.values[key]);
  el('profileNote').textContent = differs ? 'Liczby różnią się od wybranego profilu (ustawione ręcznie albo w config.env).' : '';
}

function onSettingsInput(){ renderProfileNote(); renderEstimate(); }

function renderEstimate(){
  const s = readSettings();
  const delay = Math.max(META.floor || 1, s.delay || 1);
  const cycle = delay * 1.25 + 0.7;
  const firms = selected.size || ((META.statuses.filter(x => META.default_statuses.includes(x.code))).reduce((n, x) => n + x.count, 0));
  const parallel = Math.max(1, Math.min(s.concurrency || 1, firms || 1));
  const waves = Math.ceil((firms || 0) / parallel);
  const typical = Math.min(Math.min(60, s.max_pages || 60) * cycle, (s.max_minutes || 30) * 60);
  const worst = (s.max_minutes || 30) * 60;
  el('estimate').textContent = firms
    ? `Szacunek dla ${firms} firm: typowo ≈ ${Math.max(1, Math.round(waves * typical / 60))} min, najwyżej ≈ ${Math.round(waves * worst / 60)} min (≈ ${(1 / cycle).toFixed(2)} żądań/s na domenę).`
    : 'Brak firm do przetworzenia.';
}

function renderRun(){
  const box = el('runBox');
  const run = RUN.run;
  if (!run){ box.innerHTML = ''; return; }
  const stats = run.statystyki || {};
  const live = RUN.aktywny;
  const results = Object.entries(stats.firmy_wynik || {}).map(([k, v]) => `${escapeHtml(statusInfo(k)[0])}: ${v}`).join(', ');
  box.innerHTML = `
    <div class="run-card">
      <div><span class="dot ${live ? 'live' : ''}"></span><b>Przebieg #${run.id}</b> — ${escapeHtml(run.status)}${run.stop_requested && live ? ' (zatrzymywanie…)' : ''}
        <span class="muted">${live ? 'od ' + escapeHtml(fmtDate(run.start_at)) : 'zakończony ' + escapeHtml(fmtDate(run.end_at))}</span></div>
      <div class="run-stats">
        <span>strony: <b>${stats.strony || 0}</b></span>
        <span>zapytania: <b>${stats.zapytania || 0}</b></span>
        <span>kontakty (zapisy): <b>${stats.kontakty || 0}</b></span>
        <span>aktywne domeny: <b>${stats.aktywne || 0}</b></span>
        <span>strony/min: <b>${stats.strony_na_min || 0}</b></span>
        ${results ? `<span>firmy: ${results}</span>` : ''}
      </div>
      ${RUN.log && RUN.log.length ? `<pre id="logTail">${escapeHtml(RUN.log.join('\n'))}</pre>` : ''}
    </div>`;
  const pre = el('logTail');
  if (pre) pre.scrollTop = pre.scrollHeight;
  el('startBtn').disabled = live || busy;
  el('stopBtn').disabled = !live;
}

function actions(row){
  const buttons = [];
  if (META.retryable.includes(row.skan_status)) buttons.push(`<button data-act="retry" data-id="${row.id}">Ponów</button>`);
  if (row.domena_docelowa && row.skan_status !== 'skanowanie') buttons.push(`<button data-act="target" data-id="${row.id}" title="Przekieruje na ${escapeHtml(row.domena_docelowa)}">Użyj ${escapeHtml(row.domena_docelowa)}</button>`);
  if (row.skan_status !== 'skanowanie' && row.skan_status !== 'nieskanowana') buttons.push(`<button class="danger" data-act="wipe" data-id="${row.id}">Usuń dane</button>`);
  return buttons.join('');
}

function renderRows(){
  el('emptyState').hidden = ROWS.length > 0;
  const from = TOTAL ? offset + 1 : 0;
  el('count').textContent = `${from}–${offset + ROWS.length} z ${TOTAL}`;
  el('prevBtn').disabled = offset === 0;
  el('nextBtn').disabled = offset + ROWS.length >= TOTAL;
  tbody.innerHTML = '';
  ROWS.forEach(row => {
    const info = statusInfo(row.skan_status);
    const tr = document.createElement('tr');
    tr.className = 'data-row';
    tr.innerHTML = `
      <td><input type="checkbox" class="sel" data-id="${row.id}" ${selected.has(row.id) ? 'checked' : ''}></td>
      <td class="company-cell" data-id="${row.id}"><span class="domain">${escapeHtml(row.domena)}</span>${row.website ? `<a class="site-link" href="${escapeHtml(safeUrl(row.website))}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(row.website)}" aria-label="Otwórz stronę firmy"></a>` : ''}
        ${row.nazwa ? `<div class="name">${escapeHtml(row.nazwa)}</div>` : ''}</td>
      <td><span class="badge" style="--c:${escapeHtml(info[1])}">${escapeHtml(info[0])}</span>
        ${row.skan_przyczyna ? `<span class="reason" title="${escapeHtml(row.ostatni_blad || '')}">${escapeHtml(reasonText(row.skan_przyczyna))}</span>` : ''}</td>
      <td class="num">${row.stron_pobranych}</td><td class="num">${fmtMb(row.bajty_pobranych)}</td>
      <td class="num">${row.glebokosc_max == null ? '—' : row.glebokosc_max}</td>
      <td class="num">${row.emaile}</td><td class="num">${row.telefony}</td>
      <td>${escapeHtml(fmtDate(row.skan_end_at))}</td>
      <td class="row-actions">${actions(row)}</td>`;
    tbody.appendChild(tr);
    if (row.id === expandedId) tbody.appendChild(detailPlaceholder(row.id));
  });
  updateSelection();
}

function detailPlaceholder(id){
  const tr = document.createElement('tr');
  tr.className = 'detail-row';
  tr.dataset.detail = String(id);
  tr.innerHTML = '<td colspan="10"><span class="muted">Wczytywanie…</span></td>';
  loadDetail(id);
  return tr;
}

function contactItems(contacts, type){
  const list = contacts.filter(c => c.typ === type);
  if (!list.length) return '<span class="muted">brak</span>';
  return '<ul class="contact-list">' + list.map(c => {
    const tags = [];
    if (c.rola === 'prawny') tags.push('<span class="tag legal">prawny</span>');
    else if (c.rola) tags.push(`<span class="tag">${escapeHtml(c.rola)}</span>`);
    if (type === 'email') tags.push(c.w_domenie ? '<span class="tag in">w domenie firmy</span>' : '<span class="tag">obca domena</span>');
    tags.push(`<span class="tag" title="liczba stron, na których znaleziono">${c.liczba_stron}×</span>`);
    const value = (type === 'social' || type === 'formularz') ? linkTo(c.wartosc) : escapeHtml(c.wartosc);
    return `<li><span>${value}</span>${tags.join('')}<span class="muted" title="${escapeHtml(c.zrodlo_typ || '')}">${linkTo(c.zrodlo_url, 'źródło')}</span></li>`;
  }).join('') + '</ul>';
}

async function loadDetail(id, page){
  if (page !== undefined) detailPage = page;
  const {ok, data} = await apiGet(`/api/skaper/firma?id=${id}&page=${detailPage}`);
  const cell = document.querySelector(`tr[data-detail="${id}"] td`);
  if (!cell) return;
  if (!ok){ cell.innerHTML = `<span class="error-box">${escapeHtml(data.error || 'Nie udało się wczytać szczegółów.')}</span>`; return; }
  const f = data.firma;
  const pages = data.strony.map(p => `<tr><td>${linkTo(p.url)}</td><td class="st">${escapeHtml(p.status)}${p.http_status ? ' · ' + p.http_status : ''}</td>
      <td>${escapeHtml(p.tytul || '')}${p.blad ? ` <span class="muted">${escapeHtml(p.blad)}</span>` : ''}</td></tr>`).join('');
  const pageCount = Math.max(1, Math.ceil(data.strony_razem / 100));
  cell.innerHTML = `
    <div class="detail-grid">
      <div>
        <h4>Kontakty</h4>
        <div class="kv"><span class="k">E-maile</span> ${contactItems(data.kontakty, 'email')}</div>
        <div class="kv"><span class="k">Telefony</span> ${contactItems(data.kontakty, 'telefon')}</div>
        <div class="kv"><span class="k">Profile w social media</span> ${contactItems(data.kontakty, 'social')}</div>
        <div class="kv"><span class="k">Formularze kontaktowe</span> ${contactItems(data.kontakty, 'formularz')}</div>
      </div>
      <div>
        <h4>Skan</h4>
        <div class="kv">
          <div><span class="k">Strona:</span> ${linkTo(f.website)}</div>
          <div><span class="k">Powód zatrzymania:</span> ${escapeHtml(reasonText(f.skan_przyczyna) || '—')}</div>
          ${f.ostatni_blad ? `<div><span class="k">Komunikat:</span> ${escapeHtml(f.ostatni_blad)}</div>` : ''}
          ${f.domena_docelowa ? `<div><span class="k">Domena docelowa (przekierowanie):</span> ${escapeHtml(f.domena_docelowa)}</div>` : ''}
          ${f.nastepna_proba_po ? `<div><span class="k">Serwer prosi o powrót po:</span> ${escapeHtml(fmtDate(f.nastepna_proba_po))}</div>` : ''}
          <div><span class="k">robots.txt:</span> ${escapeHtml(f.robots_status || '—')}${f.crawl_delay ? ` · Crawl-delay ${escapeHtml(f.crawl_delay)} s` : ''}</div>
        </div>
        <h4 class="spaced">robots.txt</h4>
        <pre class="robots-pre">${escapeHtml(f.robots_txt || '(brak)')}${f['robots_obcięty'] ? '\n… (obcięto)' : ''}</pre>
      </div>
    </div>
    <h4 class="spaced">Strony (${data.strony_razem})</h4>
    <table class="pages-table"><thead><tr><th>Adres</th><th>Status</th><th>Tytuł / uwagi</th></tr></thead><tbody>${pages || '<tr><td colspan="3" class="muted">Brak pobranych stron.</td></tr>'}</tbody></table>
    ${pageCount > 1 ? `<div class="pager"><button data-act="dprev" data-id="${id}" ${detailPage === 0 ? 'disabled' : ''}>‹</button> strona ${detailPage + 1} z ${pageCount}
      <button data-act="dnext" data-id="${id}" ${detailPage + 1 >= pageCount ? 'disabled' : ''}>›</button></div>` : ''}`;
}

function updateSelection(){
  const count = selected.size;
  el('selInfo').textContent = count ? `Zaznaczone firmy: ${count} (Start przetworzy tylko je)` : 'Nic nie zaznaczono — Start przetworzy wszystkie nieskanowane, w kolejce i z błędem.';
  el('startBtn').textContent = count ? `Start dla zaznaczonych (${count})` : 'Start';
  el('headerCheckbox').checked = ROWS.length > 0 && ROWS.every(r => selected.has(r.id));
  renderEstimate();
}

async function loadMeta(){
  const {ok, data} = await apiGet('/api/skaper/meta');
  if (!ok){ showBanner('warn', data.error || 'Nie udało się wczytać danych skrapera.'); return false; }
  META = data;
  renderChips();
  renderBanners();
  return true;
}

async function loadRows(){
  const params = new URLSearchParams({offset: String(offset), limit: String(PAGE_SIZE)});
  if (statusFilter.value) params.set('status', statusFilter.value);
  const q = el('search').value.trim();
  if (q) params.set('q', q);
  const {ok, data} = await apiGet('/api/skaper/firmy?' + params);
  if (!ok){ notify('warn', data.error || 'Nie udało się wczytać firm.'); return; }
  if (data.rows.length === 0 && offset > 0){ offset = Math.max(0, offset - PAGE_SIZE); return loadRows(); }
  ROWS = data.rows;
  TOTAL = data.total;
  renderRows();
}

async function loadStatus(){
  const {ok, data} = await apiGet('/api/skaper/status');
  if (!ok) return false;
  const wasActive = RUN.aktywny;
  RUN = data;
  renderRun();
  renderBanners();
  return wasActive || data.aktywny;
}

async function refreshAll(){
  await loadMeta();
  await Promise.all([loadRows(), loadStatus()]);
  if (!META.contact.ok || !META.bs4_ok) el('startBtn').disabled = true;
}

function schedulePoll(){
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    const needRows = await loadStatus();
    if (needRows){ await loadMeta(); await loadRows(); }
    schedulePoll();
  }, RUN.aktywny ? 3000 : 15000);
}

async function act(promise, okMessage){
  busy = true;
  try {
    const {ok, data} = await promise;
    if (!ok) notify('warn', data.error || 'Operacja nie powiodła się.');
    else if (okMessage) notify('ok', okMessage(data));
    return {ok, data};
  } finally { busy = false; }
}

async function start(){
  if (busy) return;
  const payload = {ustawienia: readSettings()};
  if (selected.size) payload.firma_ids = [...selected];
  const {ok} = await act(api('/api/skaper/start', payload));
  await refreshAll();
  if (ok) schedulePoll();
}

document.addEventListener('click', async (event) => {
  const chip = event.target.closest('.chip[data-status]');
  if (chip){
    statusFilter.value = statusFilter.value === chip.dataset.status ? '' : chip.dataset.status;
    offset = 0; renderChips(); await loadRows(); return;
  }
  const button = event.target.closest('button[data-act]');
  if (button){
    const id = Number(button.dataset.id);
    const kind = button.dataset.act;
    if (kind === 'retry'){ await act(api('/api/skaper/ponow', {firma_ids: [id]})); await refreshAll(); }
    else if (kind === 'target'){
      if (await showSheet({title: 'Przyjąć domenę docelową?', message: 'Dotychczasowe dane skanu firmy zostaną usunięte, a firma trafi do kolejki.', confirmLabel: 'Przyjmij domenę', focusCancel: true})){
        await act(api('/api/skaper/uzyj-domeny', {firma_id: id})); await refreshAll();
      }
    } else if (kind === 'wipe'){
      if (await showSheet({title: 'Usunąć dane firmy?', message: 'Wszystkie pobrane strony i kontakty tej firmy zostaną usunięte. Tej operacji nie można cofnąć.', confirmLabel: 'Usuń dane', tone: 'danger'})){
        await act(api('/api/skaper/usun-dane', {firma_id: id})); expandedId = null; await refreshAll();
      }
    } else if (kind === 'dprev'){ loadDetail(id, Math.max(0, detailPage - 1)); }
    else if (kind === 'dnext'){ loadDetail(id, detailPage + 1); }
    return;
  }
  const cell = event.target.closest('.company-cell');
  if (cell && !event.target.closest('a')){
    const id = Number(cell.dataset.id);
    expandedId = expandedId === id ? null : id;
    detailPage = 0;
    renderRows();
  }
});

document.addEventListener('change', (event) => {
  const box = event.target.closest('input.sel');
  if (box){
    const id = Number(box.dataset.id);
    if (box.checked) selected.add(id); else selected.delete(id);
    updateSelection();
  }
});

el('headerCheckbox').addEventListener('change', (event) => {
  ROWS.forEach(r => { if (event.target.checked) selected.add(r.id); else selected.delete(r.id); });
  renderRows();
});
el('selectVisibleBtn').addEventListener('click', () => { ROWS.forEach(r => selected.add(r.id)); renderRows(); });
el('clearSelectionBtn').addEventListener('click', () => { selected.clear(); renderRows(); });
el('startBtn').addEventListener('click', start);
el('stopBtn').addEventListener('click', async () => {
  const {data} = await act(api('/api/skaper/stop', {}));
  if (data.odzyskano && data.odzyskano.length) notify('ok', 'Nieaktywny przebieg został odzyskany — firmy wróciły do kolejki.');
  await refreshAll();
});
el('seedBtn').addEventListener('click', async () => {
  const {ok, data} = await act(api('/api/skaper/seed', {}));
  if (ok) notify('ok', `Firm w bazie: ${data.razem} (nowych: ${data.dodano}, pominiętych wierszy maile: ${data.pominieto}).`);
  await refreshAll();
});
el('addBtn').addEventListener('click', async () => {
  const url = el('addUrl').value.trim();
  if (!url) return;
  const {ok, data} = await act(api('/api/skaper/dodaj', {url, nazwa: el('addName').value.trim() || undefined}));
  if (ok){
    notify('ok', `${data.nowa ? 'Dodano' : 'Już istnieje'}: ${data.domena}.`);
    el('addUrl').value = ''; el('addName').value = '';
    await refreshAll();
  }
});
el('addUrl').addEventListener('keydown', (e) => { if (e.key === 'Enter') el('addBtn').click(); });
statusFilter.addEventListener('change', () => { offset = 0; renderChips(); loadRows(); });
el('search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { offset = 0; loadRows(); }, 250); });
el('prevBtn').addEventListener('click', () => { offset = Math.max(0, offset - PAGE_SIZE); loadRows(); });
el('nextBtn').addEventListener('click', () => { offset += PAGE_SIZE; loadRows(); });
SETTING_KEYS.forEach(key => el('s_' + key).addEventListener('input', onSettingsInput));
el('s_profile').addEventListener('change', applyProfile);

(async function init(){
  if (await loadMeta()){
    renderProfiles();
    SETTING_KEYS.forEach(key => { if (META.defaults[key] != null) el('s_' + key).value = META.defaults[key]; });
    const delay = el('s_delay');
    delay.min = String(META.floor || 1);
    await Promise.all([loadRows(), loadStatus()]);
    if (!META.contact.ok || !META.bs4_ok) el('startBtn').disabled = true;
    onSettingsInput();
  }
  schedulePoll();
})();
