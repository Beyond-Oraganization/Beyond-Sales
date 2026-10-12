'use strict';
// Zakładka „Automat”: limity, stan planu, prognoza, dziennik i Start z potwierdzeniem. Cały tekst z serwera (też z bazy:
// nazwy firm w dzienniku) przechodzi przez escapeHtml; pola formularza ustawiamy przez .value, nie przez HTML.
const el = (id) => document.getElementById(id);
const DAY_LABELS = ['pn', 'wt', 'śr', 'cz', 'pt', 'sb', 'nd'];
const REASONS = {
  not_armed: 'plan nieuzbrojony', paused: 'wstrzymany', outside_window: 'poza oknem godzin', not_send_day: 'dzień bez wysyłki',
  cap_reached: 'limit dnia wyczerpany', horizon_over: 'koniec planu', company_gap: 'czeka na odstęp dla firmy',
};
const FIELDS = ['daily_cap', 'window_start', 'window_end', 'segment', 'horizon_days', 'max_consecutive_errors', 'company_gap_days'];
const BAND_UNITS = {s: 1, min: 60};  // sekundy w ustawieniach; w formularzu przedział ma własną jednostkę
let BAND_ROWS = [];  // {row, low, high, unit, percent, remove, factor} dla każdego wiersza edytora przedziałów
let STATE = null;
let PREVIEW = null;
let pollTimer = 0;
let previewTimer = 0;

function fmtTime(iso) {
  return iso ? String(iso).slice(5, 16).replace('T', ' ') : '—';
}

const toast = (text, tone) => showToast(text, tone);

// Segmenty to dowolne etykiety z bazy (APP.segmentOrder); zapisany w ustawieniach segment, którego już nie ma, też zostaje na liście.
function fillSegmentOptions(current) {
  const select = el('f_segment');
  const known = typeof APP === 'undefined' ? [] : (APP.segmentOrder || []);
  const names = ['both', ...known];
  if (current && !names.includes(current)) names.push(current);
  select.textContent = '';
  for (const name of names) {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = name === 'both' ? 'wszystkie' : name;
    select.appendChild(option);
  }
}

// „45 s”, „2 min”, „1 min 30 s” (jak delay_bands.format_duration po stronie serwera).
function fmtDuration(seconds) {
  const whole = Math.round(Number(seconds));
  if (!(whole >= 0)) return '—';
  if (whole < 60) return whole + ' s';
  const rest = whole % 60;
  return Math.floor(whole / 60) + ' min' + (rest ? ' ' + rest + ' s' : '');
}

const round3 = (number) => Math.round(number * 1000) / 1000;
const numberOrNull = (text) => { const number = parseFloat(text); return Number.isFinite(number) ? number : null; };

// Przedziały odstępu z ustawień; starszy zapis (sam zakres min–max) to jeden przedział wybierany zawsze.
function bandsOf(settings) {
  if (settings.delay_bands && settings.delay_bands.length) return settings.delay_bands;
  return [{min: settings.delay_min, max: settings.delay_max, percent: 100}];
}

function canonicalBands(bands) {
  return JSON.stringify(bands.map((band) => [band.min, band.max, band.percent]));
}

function bandControl(tag, label) {
  const control = document.createElement(tag);
  control.setAttribute('aria-label', label);
  if (tag === 'input') { control.type = 'number'; control.min = '0'; control.step = 'any'; }
  return control;
}

function bandField(caption, control) {
  const label = document.createElement('label');
  const text = document.createElement('span');
  text.textContent = caption;
  label.appendChild(text);
  label.appendChild(control);
  return label;
}

function bandUnitSelect() {
  const select = bandControl('select', 'Jednostka czasu');
  for (const unit of Object.keys(BAND_UNITS)) {
    const option = document.createElement('option');
    option.value = unit;
    option.textContent = unit;
    select.appendChild(option);
  }
  return select;
}

function shown(seconds, factor) {
  return seconds === null || seconds === undefined ? '' : String(round3(seconds / factor));
}

function addBandRow(band) {
  const unit = [band.min, band.max].every((seconds) => seconds % 60 === 0) ? 'min' : 's';
  const record = {row: document.createElement('div'), low: bandControl('input', 'Odstęp od'), high: bandControl('input', 'Odstęp do'),
    unit: bandUnitSelect(), percent: bandControl('input', 'Prawdopodobieństwo (%)'), remove: document.createElement('button'),
    factor: BAND_UNITS[unit]};
  record.row.className = 'band-row';
  record.low.value = shown(band.min, record.factor);
  record.high.value = shown(band.max, record.factor);
  record.unit.value = unit;
  record.percent.value = band.percent === null || band.percent === undefined ? '' : String(band.percent);
  record.remove.type = 'button';
  record.remove.className = 'btn plain danger';
  record.remove.textContent = 'Usuń';
  record.remove.setAttribute('aria-label', 'Usuń przedział');
  record.row.appendChild(bandField('Od', record.low));
  record.row.appendChild(bandField('Do', record.high));
  record.row.appendChild(bandField('Jednostka', record.unit));
  record.row.appendChild(bandField('Prawdopodobieństwo (%)', record.percent));
  record.row.appendChild(record.remove);
  for (const input of [record.low, record.high, record.percent]) input.addEventListener('input', renderBandsSum);
  record.unit.addEventListener('change', () => changeBandUnit(record));
  record.remove.addEventListener('click', () => removeBandRow(record));
  BAND_ROWS.push(record);
  el('bandsList').appendChild(record.row);
}

// Zmiana jednostki przelicza wpisane liczby, tak żeby przedział został ten sam (1 min → 60 s).
function changeBandUnit(record) {
  const factor = BAND_UNITS[record.unit.value] || 1;
  for (const input of [record.low, record.high]) {
    const seconds = numberOrNull(input.value);
    if (seconds !== null) input.value = shown(seconds * record.factor, factor);
  }
  record.factor = factor;
  renderBandsSum();
}

function removeBandRow(record) {
  BAND_ROWS = BAND_ROWS.filter((other) => other !== record);
  record.row.remove();
  renderBandsSum();
}

// Przedziały z formularza w sekundach i procentach; puste lub błędne pole to null (serwer odpowie komunikatem).
function readBands() {
  return BAND_ROWS.map((record) => {
    const factor = BAND_UNITS[record.unit.value] || 1;
    const low = numberOrNull(record.low.value);
    const high = numberOrNull(record.high.value);
    return {min: low === null ? null : round3(low * factor), max: high === null ? null : round3(high * factor),
      percent: numberOrNull(record.percent.value)};
  });
}

function bandsTotal(bands) {
  return Math.round(bands.reduce((sum, band) => sum + (band.percent || 0), 0) * 100) / 100;
}

function renderBandsSum() {
  const bands = readBands();
  const total = bandsTotal(bands);
  const exact = Math.abs(total - 100) <= 0.01;
  el('bandsSum').textContent = 'Suma: ' + total + '%' + (exact ? '' : ' — musi wynosić 100%');
  el('bandsSum').classList.toggle('is-off', !exact);
  el('evenBandsBtn').hidden = exact || !bands.length || bands.some((band) => !(band.percent > 0));
  for (const record of BAND_ROWS) record.remove.disabled = BAND_ROWS.length < 2;
}

// Skaluje prawdopodobieństwa proporcjonalnie do 100%; ostatni przedział dostaje resztę po zaokrągleniach.
function evenBands() {
  const bands = readBands();
  const scale = 100 / bandsTotal(bands);
  let used = 0;
  BAND_ROWS.forEach((record, index) => {
    const last = index === BAND_ROWS.length - 1;
    const percent = last ? Math.round((100 - used) * 100) / 100 : Math.round(bands[index].percent * scale * 100) / 100;
    used += percent;
    record.percent.value = String(percent);
  });
  renderBandsSum();
}

function addBand() {
  const rest = Math.round((100 - bandsTotal(readBands())) * 100) / 100;
  addBandRow({min: 60, max: 120, percent: rest > 0 ? rest : null});
  renderBandsSum();
}

// Odświeżanie stanu co kilka sekund nie może przebudowywać wierszy, które już pokazują te same przedziały (zgubiłoby fokus).
function fillBands(bands) {
  if (BAND_ROWS.length && canonicalBands(readBands()) === canonicalBands(bands)) return;
  el('bandsList').textContent = '';
  BAND_ROWS = [];
  for (const band of bands) addBandRow(band);
  renderBandsSum();
}

function fillForm(settings) {
  fillSegmentOptions(settings.segment);
  for (const key of FIELDS) el('f_' + key).value = settings[key];
  fillBands(bandsOf(settings));
  el('f_countries').value = (settings.countries || []).join(',');
  for (let day = 0; day < 7; day++) el('d' + day).checked = (settings.weekdays || []).includes(day);
}

function readForm() {
  const raw = {};
  for (const key of FIELDS) raw[key] = el('f_' + key).value;
  raw.delay_bands = readBands();
  raw.countries = el('f_countries').value.split(',').map((code) => code.trim()).filter(Boolean)
    .map((code) => (code.startsWith('_') ? code : code.toUpperCase()));
  raw.weekdays = [];
  for (let day = 0; day < 7; day++) if (el('d' + day).checked) raw.weekdays.push(day);
  return raw;
}

function normalized(settings) {
  const out = {};
  for (const key of FIELDS) out[key] = String(settings[key]);
  out.delay_bands = canonicalBands(bandsOf(settings));
  out.countries = (settings.countries || []).map(String).join(',');
  out.weekdays = (settings.weekdays || []).map(Number).join(',');
  return JSON.stringify(out);
}

function isDirty() {
  const form = readForm();
  form.countries = form.countries || [];
  return !STATE || normalized(form) !== normalized(STATE.settings);
}

// Skrzynka wyłączona po błędach nie wysyła, więc „limit dnia” dotyczy już tylko pozostałych: mówimy to wprost.
function offNote(stats) {
  const off = (stats.wylaczone || []).map((n) => 'nr ' + n);
  return off.length ? ' · wyłączona po błędach: skrzynka ' + off.join(', ') : '';
}

function statusText(state) {
  const stats = (state.daemon && state.daemon.stats) || {};
  if (!state.armed) return 'plan nieuzbrojony';
  if (state.paused) return 'WSTRZYMANY: ' + (state.pause_reason || '');
  if (stats.stan === 'send') return 'wysyła' + offNote(stats);
  const capped = stats.powod === 'cap_reached' && (stats.wylaczone || []).length;
  const reason = capped ? 'limit dnia wyczerpany na sprawnych skrzynkach' : REASONS[stats.powod] || stats.powod || '';
  return reason + (stats.do ? ' (następna wysyłka ' + fmtTime(stats.do) + ')' : '') + offNote(stats);
}

function card(label, value) {
  return '<div class="status-card">' + escapeHtml(label) + '<b>' + escapeHtml(value) + '</b></div>';
}

// Przy kilku skrzynkach pod licznikiem pokazujemy, ile wysłała każda (limit dzienny obowiązuje je osobno).
function mailboxBreakdown(s) {
  const boxes = s.mailboxes || [];
  if (boxes.length < 2) return '';
  return ' (' + boxes.map((b) => b.address + ': ' + b.sent_today).join(', ') + ')';  // card() ucieka tekst, więc bez znaczników
}

function renderStatus() {
  const s = STATE;
  const daemon = s.daemon;
  const dot = '<span class="dot ' + (daemon.running ? 'live' : 'bad') + '"></span>';
  const age = daemon.heartbeat_age == null ? '' : ' (puls sprzed ' + Math.round(daemon.heartbeat_age) + ' s)';
  el('statusBox').innerHTML = '<div class="status-grid">'
    + '<div class="status-card">Demon<b>' + dot + (daemon.running ? 'działa' : 'nie działa') + escapeHtml(age) + '</b></div>'
    + card('Plan', s.armed ? 'uzbrojony do ' + fmtTime(s.armed_until) : 'nieuzbrojony')
    + card('Dziś wysłano', s.sent_today + ' z ' + (s.capacity || s.settings.daily_cap) + mailboxBreakdown(s))
    + card('Teraz', statusText(s)) + '</div>';
  el('startBtn').disabled = s.armed && !s.paused;
  el('pauseBtn').disabled = !s.armed || s.paused;
  el('resumeBtn').disabled = !s.paused;
  el('stopBtn').disabled = !s.armed;
  el('daemonInstallBtn').disabled = s.launchd.loaded;
  el('daemonRestartBtn').disabled = !s.launchd.loaded;
  el('daemonRemoveBtn').disabled = !s.launchd.installed && !s.launchd.loaded;
  el('savedInfo').textContent = s.stamp ? 'Ostatnia zmiana limitów: ' + fmtTime(s.stamp) : 'Limity domyślne — jeszcze nie zapisane.';
}

function renderBanners() {
  const s = STATE;
  const notes = [];
  if (s.armed && !s.daemon.running) notes.push('warn|Plan jest uzbrojony, ale demon nie działa — nic się nie wyśle. Kliknij „Zainstaluj i uruchom” poniżej.');
  else if (!s.launchd.loaded && !s.daemon.running) notes.push('warn|Demon nie jest zainstalowany (nie wystartuje po włączeniu komputera). Kliknij „Zainstaluj i uruchom” poniżej.');
  if (s.daemon.running && bandsOf(s.settings).length > 1 && !(s.daemon.stats || {}).delay_bands) {
    notes.push('warn|Demon działa na starszej wersji i nie zna przedziałów odstępu: losuje czas z całego zakresu ' + fmtDuration(s.settings.delay_min)
      + ' – ' + fmtDuration(s.settings.delay_max) + '. Kliknij „Uruchom ponownie” poniżej.');
  }
  if (s.paused) notes.push('warn|Wysyłka wstrzymana: ' + (s.pause_reason || ''));
  if (s.power && s.power.on_battery) notes.push('warn|Mac jest na baterii' + (s.power.percent == null ? '' : ' (' + s.power.percent + '%)') + ' — zamknięcie klapy uśpi wysyłkę. Podłącz zasilacz i zostaw klapę otwartą.');
  if (!s.channels.includes('ntfy')) notes.push('warn|Brak powiadomień na telefon (NTFY_TOPIC w zakładce Konfiguracja) — alerty zobaczysz tylko na Macu.');
  if (!s.settings_ok) notes.push('warn|Zapisane limity były nieczytelne — używam domyślnych. Zapisz je ponownie.');
  el('banners').innerHTML = notes.map((note) => {
    const [kind, text] = note.split('|');
    return '<div class="banner ' + kind + '">' + escapeHtml(text) + '</div>';
  }).join('');
}

function renderEvents() {
  const rows = (STATE.events || []).map((event) => '<tr><td class="ts">' + escapeHtml(fmtTime(event.ts)) + '</td>'
    + '<td class="lvl-' + escapeHtml(event.poziom) + '">' + escapeHtml(event.tresc) + '</td></tr>').join('');
  el('eventsBox').innerHTML = rows ? '<table class="events-table"><tbody>' + rows + '</tbody></table>' : 'Brak zdarzeń.';
}

// „ (w K firmach)” tylko gdy któraś firma ma więcej niż jeden adres; liczby z serwera przechodzą przez escapeHtml.
function queueScope(p) {
  return p.companies && p.companies !== p.queue ? ' (w <b>' + escapeHtml(p.companies) + '</b> firmach)' : '';
}

function queueNotes(p) {
  const notes = [];
  if (p.deferred_today) notes.push('Dziś czeka na odstęp dla firmy: <b>' + escapeHtml(p.deferred_today) + '</b> maili.');
  if (p.held) notes.push('Wstrzymane przez status innego adresu firmy (odpowiedź, „Nie kontaktować”, klient): <b>' + escapeHtml(p.held) + '</b> maili — nie wejdą do kolejki.');
  return notes.map((note) => '<p class="muted small">' + note + '</p>').join('');
}

// Długie przedziały mogą dać w oknie mniej maili niż dzienny limit; prognoza dni tego nie uwzględnia, więc mówimy to wprost.
function pacingNote(p) {
  if (p.delay_mean == null) return '';
  const capacity = STATE ? STATE.capacity : null;
  const short = capacity != null && p.window_mails < capacity;
  return '<p class="muted small">Średni odstęp między mailami: <b>' + escapeHtml(fmtDuration(p.delay_mean)) + '</b>, więc w oknie godzin zmieści się ok. <b>'
    + escapeHtml(p.window_mails) + '</b> maili dziennie.'
    + (short ? ' <span class="lvl-warn">To mniej niż dzienny limit (' + escapeHtml(capacity) + ') — limit nie zostanie wyczerpany.</span>' : '') + '</p>';
}

function renderForecast() {
  const p = PREVIEW;
  if (!p) return;
  const chips = (p.countries || []).map(([code, n]) => '<span class="chip">' + escapeHtml(code === '_none' ? 'bez kraju' : code) + ': ' + escapeHtml(n) + '</span>').join('');
  const plan = (p.plan || []).map(([day, count]) => '<tr><td>' + escapeHtml(day) + '</td><td>' + escapeHtml(count) + ' maili</td></tr>').join('');
  el('forecastBox').innerHTML = '<p>W kolejce: <b>' + escapeHtml(p.queue) + '</b> maili „Do wysłania”' + queueScope(p) + ', w tym <b>' + escapeHtml(p.foreign)
    + '</b> do firm spoza Polski' + (p.foreign ? ' (maile są dziś generowane po polsku)' : '') + ', ' + escapeHtml(p.no_country) + ' bez kraju.</p>'
    + queueNotes(p)
    + pacingNote(p)
    + '<div class="chips">' + chips + '</div>'
    + (p.last_day ? '<p>Przy tym tempie koniec około <b>' + escapeHtml(p.last_day) + '</b> (' + escapeHtml(p.send_days) + ' dni wysyłki).'
      + (p.limited_by_gap ? ' <span class="lvl-warn">Odstęp dla firmy (' + escapeHtml(p.gap_days) + ' dn.) wydłuża plan.</span>' : '')
      + (p.fits_in_horizon ? '' : ' <span class="lvl-warn">Nie zmieści się w horyzoncie planu (do ' + escapeHtml(String(p.until).slice(0, 10)) + ').</span>') + '</p>'
      + '<table class="plan-table"><tbody>' + plan + '</tbody></table>' : '<p>Kolejka pusta.</p>');
}

async function loadStatus() {
  const {ok, data} = await apiGet('/api/automat/status');
  if (!ok) return;
  const first = STATE === null;
  STATE = data;
  if (first || !isDirty()) fillForm(data.settings);
  renderStatus(); renderBanners(); renderEvents();
}

async function loadPreview() {
  const {ok, data} = await apiGet('/api/automat/preview');
  if (ok) { PREVIEW = data; renderForecast(); }
}

async function saveLimits() {
  const {ok, data} = await api('/api/automat/limits', readForm());
  toast(ok ? (data.disarmed ? 'Zapisano. Plan był uzbrojony — rozbrojono go, potwierdź Start od nowa.' : 'Zapisano limity.') : (data.error || 'Nie udało się zapisać.'), ok ? 'ok' : 'error');
  if (ok) { STATE = null; await loadStatus(); await loadPreview(); }
}

// „45 s – 2 min” dla jednego zakresu, „1 min – 2 min (50%), 5 min – 10 min (40%), …” dla kilku przedziałów.
function bandsText(settings) {
  const bands = bandsOf(settings);
  return bands.map((band) => fmtDuration(band.min) + ' – ' + fmtDuration(band.max) + (bands.length > 1 ? ' (' + band.percent + '%)' : '')).join(', ');
}

function confirmSummary() {
  const s = STATE.settings;
  const days = (s.weekdays || []).map((d) => DAY_LABELS[d]).join(', ');
  const lines = [
    'dzienny limit: ' + s.daily_cap + ((STATE.mailboxes || []).length > 1
      ? ' na każdą z ' + STATE.mailboxes.length + ' skrzynek (razem ' + STATE.capacity + ')' : ''), 'dni: ' + days, 'okno: ' + s.window_start + '–' + s.window_end,
    'odstęp między mailami: ' + bandsText(s), 'segment: ' + s.segment,
    'kraje: ' + ((s.countries || []).join(', ') || 'wszystkie'), 'horyzont planu: ' + s.horizon_days + ' dni',
    'odstęp dla firmy: ' + (Number(s.company_gap_days) > 0 ? s.company_gap_days + ' dn. między mailami do jednej firmy' : 'brak'),
    'limity ostatnio zmienione: ' + (STATE.stamp ? fmtTime(STATE.stamp) : 'NIGDY (domyślne)'),
  ];
  const pacing = PREVIEW && PREVIEW.delay_mean != null ? ['średni odstęp: ' + fmtDuration(PREVIEW.delay_mean)
    + ', w oknie zmieści się ok. ' + PREVIEW.window_mails + ' maili dziennie'] : [];
  const queue = PREVIEW ? ['w kolejce: ' + PREVIEW.queue + ' maili' + (PREVIEW.companies && PREVIEW.companies !== PREVIEW.queue ? ' (' + PREVIEW.companies + ' firm)' : '')
    + ', w tym ' + PREVIEW.foreign + ' do firm spoza Polski'] : [];
  return '<ul class="sum-list">' + lines.concat(pacing, queue).map((line) => '<li>' + escapeHtml(line) + '</li>').join('') + '</ul>';
}

async function openConfirm() {
  if (isDirty()) { toast('Najpierw zapisz limity — potwierdzasz zapisane wartości.', 'error'); return; }
  await loadPreview();
  el('confirmBody').innerHTML = confirmSummary();
  el('ackBox').checked = false;
  el('confirmOk').disabled = true;
  el('confirmDlg').showModal();
}

async function confirmStart() {
  const {ok, data} = await api('/api/automat/start', {potwierdzam: el('ackBox').checked, stamp: STATE.stamp});
  el('confirmDlg').close();
  toast(ok ? 'Plan uzbrojony.' : (data.error || 'Nie udało się uzbroić planu.'), ok ? 'ok' : 'error');
  STATE = null;
  await loadStatus();
}

async function simple(path, okText) {
  const {ok, data} = await api(path, {});
  toast(ok ? okText : (data.error || 'Operacja nie powiodła się.'), ok ? 'ok' : 'error');
  STATE = null;
  await loadStatus();
}

async function daemonAction(action, okText) {
  const {ok, data} = await api('/api/automat/daemon', {action});
  toast(ok ? okText : (data.error || 'Operacja na demonie nie powiodła się.'), ok ? 'ok' : 'error');
  STATE = null;
  await loadStatus();
  setTimeout(loadStatus, 4000);  // demon potrzebuje chwili, żeby zapisać pierwszy puls
}

async function doctor() {
  const {ok, data} = await apiGet('/api/automat/doctor');
  if (!ok) { el('doctorBox').textContent = data.error || 'Nie udało się sprawdzić.'; return; }
  const items = data.checks.map((check) => '<li><span class="lvl ' + (check.level === 'OK' ? 'ok' : 'warn') + '">'
    + escapeHtml(check.level) + '</span>' + escapeHtml(check.text) + '</li>').join('');
  el('doctorBox').innerHTML = '<ul class="doctor-list">' + items + '</ul>'
    + '<p>Wybudzanie Maca przed oknem (wymaga hasła administratora, wykonaj w Terminalu): <code class="cmd">'
    + escapeHtml(data.pmset) + '</code></p>';
}

async function testNotify() {
  const {ok, data} = await api('/api/automat/test-notify', {});
  if (!ok) { toast(data.error || 'Nie udało się wysłać próby.', 'error'); return; }
  const parts = data.sent.map((name) => 'OK ' + name)
    .concat(Object.entries(data.failed).map(([name, why]) => 'BŁĄD ' + name + ': ' + why))
    .concat(data.skipped.map((name) => 'pominięto ' + name + ' (nieskonfigurowany)'));
  el('doctorBox').innerHTML = '<ul class="doctor-list">' + parts.map((text) => '<li>' + escapeHtml(text) + '</li>').join('') + '</ul>';
}

function poll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => { await loadStatus(); poll(); }, 3000);
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => { await loadPreview(); }, 30000);
}

el('saveBtn').addEventListener('click', saveLimits);
el('addBandBtn').addEventListener('click', addBand);
el('evenBandsBtn').addEventListener('click', evenBands);
el('startBtn').addEventListener('click', openConfirm);
el('pauseBtn').addEventListener('click', () => simple('/api/automat/pause', 'Wstrzymano.'));
el('resumeBtn').addEventListener('click', () => simple('/api/automat/resume', 'Wznowiono.'));
el('stopBtn').addEventListener('click', async () => {
  const message = 'Nic więcej nie zostanie wysłane, dopóki nie uzbroisz planu ponownie.';
  if (await showSheet({title: 'Zatrzymać plan?', message, confirmLabel: 'Zatrzymaj plan', tone: 'danger'})) simple('/api/automat/stop', 'Plan zatrzymany.');
});
el('daemonInstallBtn').addEventListener('click', () => daemonAction('install', 'Demon zainstalowany i uruchomiony.'));
el('daemonRestartBtn').addEventListener('click', () => daemonAction('restart', 'Demon uruchomiony ponownie.'));
el('daemonRemoveBtn').addEventListener('click', async () => {
  const message = 'Demon przestanie wstawać po zalogowaniu i nic nie wyśle, dopóki go nie zainstalujesz ponownie.';
  if (await showSheet({title: 'Usunąć demona?', message, confirmLabel: 'Usuń demona', tone: 'danger'})) daemonAction('remove', 'Demon usunięty.');
});
el('doctorBtn').addEventListener('click', doctor);
el('notifyBtn').addEventListener('click', testNotify);
el('ackBox').addEventListener('change', () => { el('confirmOk').disabled = !el('ackBox').checked; });
el('confirmOk').addEventListener('click', confirmStart);
el('confirmCancel').addEventListener('click', () => el('confirmDlg').close());
Promise.all([loadStatus(), loadPreview()]).then(poll);
