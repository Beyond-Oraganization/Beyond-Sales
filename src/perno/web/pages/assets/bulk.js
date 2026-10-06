// Zbiorcze akcje panelu: pasek akcji przy zaznaczeniu, okno z podglądem i ostrzeżeniami, kopiowanie e-maili,
// eksport widoku/zaznaczonych i cofanie. Cała logika zmian jest na serwerze (/api/bulk-update, /api/bulk-undo);
// tu tylko pokazujemy podgląd (dry-run), prosimy o potwierdzenie i odświeżamy listę.
function createBulkUI(o) {
  const K = PernoKit;
  const els = o.els, ctx = o.ctx, selection = o.selection, view = o.view;
  const SKIP_TEXT = {
    not_found: 'nie ma ich już w bazie',
    locked: 'mail jest właśnie wysyłany — poczekaj na wynik',
    not_ready: 'brak e-maila lub treści, więc nie mogą mieć statusu „Do wysłania”',
  };
  let undo = null;        // najnowsza operacja, którą da się cofnąć: {id, at, label, count}
  let toastTimer = null;
  let pending = null;     // {changes, result} w otwartym oknie
  let busy = false;

  const selectedIds = () => selection.toArray();
  const statusLabel = (code) => (ctx.statusMeta[code] || [code])[0];
  const firms = (n) => `${n} ${K.plural(n, 'firmę', 'firmy', 'firm')}`;

  // ---- komunikaty ----------------------------------------------------------------------------
  function toast(text, kind) {
    clearTimeout(toastTimer);
    els.toast.textContent = text;
    els.toast.className = 'toast' + (kind ? ' ' + kind : '');
    els.toast.hidden = false;
    toastTimer = setTimeout(() => { els.toast.hidden = true; }, kind === 'error' ? 12000 : 8000);
  }

  // ---- pasek akcji ---------------------------------------------------------------------------
  // Ile zaznaczonych jest gotowych do wysyłki, wie serwer (zaznaczone firmy nie muszą być na bieżącej stronie);
  // do czasu jego odpowiedzi pokazujemy „…” i nie pozwalamy wysłać.
  function readyCount() {
    const summary = view.summary();
    return summary && !view.summaryPending() ? summary.ready : null;
  }
  function refresh() {
    const total = selection.size;
    const ready = readyCount();
    els.count.textContent = total ? `Zaznaczono ${total}` : 'Nic nie zaznaczono';
    [els.statusBtn, els.dataBtn, els.copyBtn].forEach((b) => { b.disabled = total === 0 || busy; });
    els.sendBtn.textContent = `Wyślij gotowe (${ready === null ? '…' : ready})`;
    els.sendBtn.disabled = !ready || busy;
    els.sendBtn.title = total && ready !== null && ready < total ? `${total - ready} zaznaczonych firm nie jest gotowych do wysyłki (status, e-mail albo treść)` : '';
    if (undo) {
      els.undoBtn.hidden = false;
      els.undoBtn.textContent = `Cofnij: ${undo.label} · ${undo.at.slice(11, 16)}`;
      els.undoBtn.title = 'Cofa ostatnią zbiorczą zmianę (pola zmienione później zostają)';
    } else {
      els.undoBtn.hidden = true;
    }
    renderExportMenu(total);
  }

  function renderStatusMenu() {
    els.statusMenu.innerHTML = '<div class="inline muted">Ustaw status zaznaczonym:</div>' + APP.manualStatuses.map((code) =>
      `<button type="button" class="item" data-status="${escapeHtml(code)}"><span class="status-menu-dot" style="--c:${escapeHtml((ctx.statusMeta[code] || [code, '#6b7280'])[1])}"></span>${escapeHtml(statusLabel(code))}</button>`).join('');
  }
  function renderExportMenu(total) {
    const n = view.total();
    els.exportMenu.innerHTML =
      `<button type="button" class="item" data-export="selected:xlsx" ${total ? '' : 'disabled'}>Zaznaczone (${total}) — XLSX</button>` +
      `<button type="button" class="item" data-export="selected:csv" ${total ? '' : 'disabled'}>Zaznaczone (${total}) — CSV</button><hr>` +
      `<button type="button" class="item" data-export="view:xlsx" ${n ? '' : 'disabled'}>Wszystko w widoku (${n}) — XLSX</button>` +
      `<button type="button" class="item" data-export="view:csv" ${n ? '' : 'disabled'}>Wszystko w widoku (${n}) — CSV</button><hr>` +
      '<a class="item more-link" href="/eksport">Więcej opcji: kolumny, filtry</a>';
  }

  // ---- okno zbiorczej zmiany -----------------------------------------------------------------
  function openDialog(title) {
    els.title.textContent = title;
    els.dialog.showModal();
  }
  function closeDialog() {
    if (els.dialog.open) els.dialog.close();
    pending = null;
  }

  function previewHtml(r) {
    const by = Object.entries(r.by_status).map(([code, n]) => `${escapeHtml(statusLabel(code))} ${n}`).join(', ');
    let html = `<p>Zaznaczono <b>${firms(r.requested)}</b>${by ? ` — ${by}` : ''}.</p>` +
      `<div class="stats"><div class="stat"><b>${r.changed}</b>zmieni się</div>` +
      `<div class="stat"><b>${r.unchanged}</b>bez zmian</div><div class="stat"><b>${r.skipped_count}</b>pominięte</div></div>`;
    const reasons = Object.entries(r.skipped_by_reason || {});
    if (reasons.length) {
      html += '<p class="muted">Pominięte: ' + reasons.map(([code, n]) => `${n} — ${escapeHtml(SKIP_TEXT[code] || code)}`).join('; ') + '.</p>';
    }
    if (r.warnings.length) {
      html += '<div class="warning"><b>Uwaga:</b><ul>' + r.warnings.map((w) => `<li>${escapeHtml(w.message)}</li>`).join('') + '</ul></div>' +
        '<label class="ack"><input type="checkbox" id="bulkAck"> Rozumiem i chcę kontynuować</label>';
    }
    const summary = view.summary();
    const outside = summary && !view.onlySelected() && !view.summaryPending() ? Math.max(0, selection.size - summary.in_view) : 0;
    if (outside) html += `<p class="muted">${firms(outside)} z zaznaczenia jest poza bieżącym filtrem — zmiana obejmie także ${outside === 1 ? 'ją' : 'je'}.</p>`;
    if (!r.changed) html += '<p class="muted">Nic się nie zmieni.</p>';
    return html;
  }

  function updateApply() {
    const ack = els.body.querySelector('#bulkAck');
    const r = pending && pending.result;
    els.apply.disabled = busy || !r || r.changed === 0 || (r.needs_ack.length > 0 && !(ack && ack.checked));
  }

  async function startBulk(title, changes, applyLabel) {
    const ids = selectedIds();
    if (!ids.length) return;
    pending = {changes: changes, result: null};
    els.apply.textContent = applyLabel;
    els.apply.disabled = true;
    els.body.innerHTML = '<p class="muted">Sprawdzam, co się zmieni…</p>';
    if (!els.dialog.open) openDialog(title); else els.title.textContent = title;
    const {ok, data} = await api('/api/bulk-update', {ids: ids, changes: changes, dry_run: true});
    if (!pending) return;  // okno zamknięto w międzyczasie
    if (!ok) { els.body.innerHTML = `<p class="error">${escapeHtml(data.error || 'Nie udało się sprawdzić zmiany.')}</p>`; return; }
    pending.result = data;
    els.body.innerHTML = previewHtml(data);
    const ack = els.body.querySelector('#bulkAck');
    if (ack) ack.addEventListener('change', updateApply);
    updateApply();
  }

  async function confirmBulk() {
    if (!pending || !pending.result) return;
    busy = true; els.apply.disabled = true; refresh();
    const {ok, status, data} = await api('/api/bulk-update', {
      ids: selectedIds(), changes: pending.changes, acknowledge: pending.result.needs_ack,
    });
    busy = false;
    if (!ok) {
      const note = status === 409 ? ' Dane zmieniły się od podglądu — sprawdź jeszcze raz.' : '';
      els.body.innerHTML = `<p class="error">${escapeHtml(data.error || 'Nie udało się zapisać zmiany.')}${note}</p>`;
      pending.result = null; refresh(); return;
    }
    closeDialog();
    await o.reload();
    await loadUndo();
    toast(data.changed ? `Zmieniono ${firms(data.changed)}.` + (data.skipped_count ? ` Pominięto ${data.skipped_count}.` : '') : 'Nic się nie zmieniło.');
  }

  function statusDialog(code) {
    startBulk(`Zmień status na „${statusLabel(code)}”`, {status: code}, 'Zmień status');
  }

  function dataDialog() {
    pending = null;
    els.apply.textContent = 'Dalej';
    els.apply.disabled = false;
    const countries = ['<option value="__keep">— bez zmian —</option>', '<option value="__clear">— wyczyść —</option>']
      .concat(APP.countries.map((c) => `<option value="${escapeHtml(c[0])}">${escapeHtml(c[2])} ${escapeHtml(c[1])}</option>`));
    els.body.innerHTML = `<p class="muted">Pola zostawione jako „bez zmian” nie zostaną ruszone. Dotyczy ${firms(selection.size)}.</p>
      <div class="field-grid">
        <label for="bulkCountry">Kraj</label><select id="bulkCountry">${countries.join('')}</select>
        <label for="bulkCity">Miasto</label><div><input type="text" id="bulkCity" maxlength="120" placeholder="bez zmian">
          <label class="muted"><input type="checkbox" id="bulkCityClear"> wyczyść miasto</label></div>
        <label for="bulkAddress">Adres</label><div><input type="text" id="bulkAddress" maxlength="500" placeholder="bez zmian">
          <label class="muted"><input type="checkbox" id="bulkAddressClear"> wyczyść adres</label></div>
      </div>`;
    pending = {form: true};
    openDialog('Dane firmy dla zaznaczonych');
    els.body.querySelector('#bulkCountry').focus();
  }

  function collectDataChanges() {
    const changes = {};
    const country = els.body.querySelector('#bulkCountry').value;
    if (country === '__clear') changes.country = '';
    else if (country !== '__keep') changes.country = country;
    [['City', 'city'], ['Address', 'address']].forEach(([id, field]) => {
      const text = els.body.querySelector('#bulk' + id).value.trim();
      if (els.body.querySelector(`#bulk${id}Clear`).checked) changes[field] = '';
      else if (text) changes[field] = text;
    });
    return changes;
  }

  els.form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (pending && pending.form) {
      const changes = collectDataChanges();
      if (!Object.keys(changes).length) { els.body.querySelector('.error') || els.body.insertAdjacentHTML('beforeend', '<p class="error">Wybierz, co zmienić.</p>'); return; }
      startBulk('Dane firmy dla zaznaczonych', changes, 'Zastosuj');
    } else {
      confirmBulk();
    }
  });
  els.cancel.addEventListener('click', closeDialog);
  els.dialog.addEventListener('close', () => { pending = null; });

  // ---- menu statusów, dane, kopiowanie, eksport ----------------------------------------------
  K.popover(els.statusBtn, els.statusMenu);
  K.popover(els.exportBtn, els.exportMenu);
  renderStatusMenu();
  els.statusMenu.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-status]');
    if (b) { K.closePopovers(); statusDialog(b.dataset.status); }
  });
  els.dataBtn.addEventListener('click', dataDialog);

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (err) {
      const area = document.createElement('textarea');  // awaryjnie, gdy przeglądarka nie daje dostępu do schowka
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      area.remove();
    }
  }
  els.copyBtn.addEventListener('click', async () => {
    const {ok, data} = await api('/api/mailing/emails', {ids: selectedIds()});
    if (!ok) { toast(data.error || 'Nie udało się pobrać adresów e-mail.', 'error'); return; }
    if (!data.emails.length) { toast('Wśród zaznaczonych firm nie ma żadnego adresu e-mail.', 'error'); return; }
    await copyText(data.emails.join(', '));
    toast(`Skopiowano ${data.emails.length} ${K.plural(data.emails.length, 'adres', 'adresy', 'adresów')} e-mail` +
      (data.without_email ? ` (${firms(data.without_email)} bez adresu pominięto).` : '.'));
  });

  els.exportMenu.addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-export]');
    if (!b || b.disabled) return;
    K.closePopovers();
    const [scope, format] = b.dataset.export.split(':');
    // Zaznaczone idą jako id (serwer układa je jak listę na ekranie), cały widok jako stan filtrów i sortowania.
    const rows = scope === 'selected' ? {ids: selectedIds(), sort: view.getState().sort} : view.exportScope();
    toast('Przygotowuję plik…');
    let result;
    try { result = await downloadExport({format: format, delimiter: 'semicolon', ...rows}); }
    catch (err) { result = {ok: false, error: 'Nie udało się połączyć z serwerem.'}; }
    if (result.ok) toast(`Pobrano ${result.name} — ${result.rows} ${K.plural(result.rows, 'wiersz', 'wiersze', 'wierszy')}.`);
    else toast(result.error, 'error');
  });

  // ---- cofanie -------------------------------------------------------------------------------
  async function loadUndo() {
    const {ok, data} = await apiGet('/api/bulk-log');
    undo = ok && data.operations.length ? data.operations[0] : null;
    refresh();
  }
  els.undoBtn.addEventListener('click', async () => {
    if (!undo || busy) return;
    busy = true; refresh();
    const {ok, data} = await api('/api/bulk-undo', {operation: undo.id});
    busy = false;
    if (!ok) { toast(data.error || 'Nie udało się cofnąć zmiany.', 'error'); await loadUndo(); return; }
    await o.reload();
    await loadUndo();
    const kept = data.conflict_count;
    toast(`Cofnięto: ${data.label} — przywrócono ${data.restored} z ${data.total}.` +
      (kept ? ` Pozostawiono bez zmian: ${kept} (zmienione już po tej operacji).` : ''), kept ? 'error' : '');
  });

  return {refresh: refresh, loadUndo: loadUndo, toast: toast};
}
