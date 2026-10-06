// Wspólny interfejs list firm: pasek filtrów (szybkie widoki, chipy statusów, wyszukiwarka, kraj/segment,
// dodatkowe filtry, zapisane widoki), sortowanie, paginacja i zaznaczanie (menu, Shift+klik, „tylko zaznaczone”).
// Wiersze, sumy i liczniki przychodzą z serwera (PernoListModel); stan filtrów to PernoFilters, a zaznaczenie — zbiór id
// (PernoSelection). Całą logikę testują testy w Node, tu jest tylko DOM.
// Strona podaje sposób rysowania komórek i kontenery; kit zarządza resztą i zgłasza zmiany przez onChange.
(function (root) {
  'use strict';
  const F = root.PernoFilters, M = root.PernoListModel;
  const PREFIX = 'perno.';

  // ---- pamięć przeglądarki (może być zablokowana albo pusta — strona działa i bez niej) ----------

  function readStore(key, fallback) {
    try {
      const raw = root.localStorage.getItem(PREFIX + key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (err) { return fallback; }
  }
  function writeStore(key, value) {
    try { root.localStorage.setItem(PREFIX + key, JSON.stringify(value)); } catch (err) { /* brak pamięci: trudno */ }
  }

  // ---- okienka przy przyciskach ------------------------------------------------------------------

  const popovers = [];
  function closePopovers(except) { popovers.forEach((p) => { if (p !== except) p.close(); }); }
  function popover(button, panel, onOpen) {
    const p = {
      open() { closePopovers(p); panel.hidden = false; button.setAttribute('aria-expanded', 'true'); if (onOpen) onOpen(); },
      close() { panel.hidden = true; button.setAttribute('aria-expanded', 'false'); },
      isOpen() { return !panel.hidden; }
    };
    button.addEventListener('click', (e) => { e.stopPropagation(); if (p.isOpen()) p.close(); else p.open(); });
    panel.addEventListener('click', (e) => e.stopPropagation());
    popovers.push(p);
    return p;
  }
  document.addEventListener('click', () => closePopovers());
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePopovers(); });

  // ---- drobne pomocniki wspólne dla stron ---------------------------------------------------------

  function badge(status, ctx) {
    const info = ctx.statusMeta[status] || [status, '#6b7280'];
    return `<span class="kit-badge" style="--c:${escapeHtml(info[1])}">${escapeHtml(info[0])}</span>`;
  }
  function countryCell(row, ctx) {
    const code = row.country || '';
    if (!code) return '<span class="muted">—</span>';
    const name = ctx.countryNames[code];
    const city = row.city ? `<div class="muted city">${escapeHtml(row.city)}</div>` : '';
    return `<span title="${escapeHtml(name || code)}">${escapeHtml(ctx.countryFlags[code] || '')} ${escapeHtml(code)}</span>${city}`;
  }
  function websiteLink(url) {
    return url ? `<a class="site-link" href="${escapeHtml(url)}" target="_blank" rel="noopener" title="${escapeHtml(url)}" aria-label="Otwórz stronę firmy"></a>` : '';
  }
  // Adres firmy z wieloma adresami: znacznik „2/3” (który z ilu), nazwa przygaszona na kolejnych adresach i odznaka, gdy
  // inny adres firmy wstrzymuje ten (odpowiedź, „Nie kontaktować”, klient). Liczby i status przychodzą z serwera.
  function addressPosition(row) { return parseInt(row.position, 10) || 1; }
  function addressCount(row) { return parseInt(row.mail_count, 10) || 1; }
  function isSibling(row) { return addressPosition(row) > 1; }
  function addressMarker(row) {
    const count = addressCount(row), position = addressPosition(row);
    return count > 1 ? `<span class="kit-addr" title="Adres ${position} z ${count} tej firmy">${position}/${count}</span>` : '';
  }
  function holdBadge(row, ctx) {
    if (!row.hold) return '';
    const label = (ctx.statusMeta[row.hold] || [row.hold])[0];
    return `<span class="kit-hold" title="Inny adres tej firmy ma status „${escapeHtml(label)}” — ten adres jest wstrzymany ` +
      `(nie wyjdzie z Automatu, z terminala ani przyciskiem Wyślij; odblokuje go zmiana tamtego statusu)">wstrzymany: ${escapeHtml(label)}</span>`;
  }
  function shortDate(iso) { return iso ? String(iso).replace('T', ' ').slice(0, 16) : ''; }
  function plural(n, one, few, many) {
    if (n === 1) return one;
    const last = n % 10, tens = n % 100;
    return last >= 2 && last <= 4 && !(tens >= 12 && tens <= 14) ? few : many;
  }

  // ---- widok listy --------------------------------------------------------------------------------
  //
  // opts: key (przestrzeń w pamięci przeglądarki), ctx (PernoFilters.createContext), selection (PernoSelection),
  //       filtersEl, tableEl, tbodyEl, pagerEl, selectEl, emptyEl (kontenery ze strony),
  //       cells(row) → HTML komórek <td>, decorate(tr, row), detail(row) → <tr> pod wierszem lub null,
  //       canSelect(row), rowClass(row), rowClickToggles, defaultSize, lockedStatuses (statusy, których „zaznacz wszystkie”
  //       pomija, np. „wysylanie”), selectItems [{label, run({ids, selection})}] (dodatkowe pozycje menu „Zaznacz”),
  //       onChange({reason: 'data' | 'selection' | 'summary'}), onError(komunikat).

  function createListView(opts) {
    const ctx = opts.ctx, selection = opts.selection;
    const stateKey = 'state.' + opts.key;
    const defaultSize = opts.defaultSize === undefined ? 100 : opts.defaultSize;
    const canSelect = opts.canSelect || (() => true);
    const model = M.create({api: root.api, filters: F, ctx: ctx, selection: selection, defaultSize: defaultSize, urls: opts.urls});
    let bar = null, selectBar = null, searchTimer = null;
    let drawn = '';          // podpis narysowanych wierszy: odświeżenie w tle nie przerysowuje tabeli bez potrzeby
    let deferred = false;    // dane przyszły, gdy ktoś pisał w komórce: przerysujemy, gdy ją opuści

    const pageRows = () => model.rows();
    const selectable = (rows) => rows.filter(canSelect).map((r) => r.id);
    const pageSelectable = () => selectable(pageRows());
    const lockedFilter = () => (opts.lockedStatuses && opts.lockedStatuses.length ? {exclude_statuses: opts.lockedStatuses} : {});
    function tell(message) { if (opts.onError) opts.onError(message); else root.showToast(message, 'error'); }

    // ---- stan w adresie i pamięci
    function parseSize(value) {
      const n = parseInt(value, 10);
      return model.SIZES.indexOf(n) !== -1 ? n : null;
    }
    function restore() {
      const params = new URLSearchParams(root.location.search);
      if (Array.from(params.keys()).length) {
        model.restore({state: F.fromQuery(root.location.search), page: parseInt(params.get('page'), 10) || 1, size: parseSize(params.get('size'))});
        return;
      }
      const saved = readStore(stateKey, null);
      if (saved && typeof saved === 'object') model.restore({state: saved.state, size: saved.size});
    }
    function persist() {
      const page = model.getPage(), size = model.getSize();
      const query = [F.toQuery(model.getState()), page > 1 ? 'page=' + page : '', size !== defaultSize ? 'size=' + size : ''].filter(Boolean).join('&');
      try { root.history.replaceState(null, '', root.location.pathname + (query ? '?' + query : '')); } catch (err) { /* np. file:// */ }
      writeStore(stateKey, {state: model.getState(), size: size});
    }

    // ---- zmiany stanu
    function applyState(next, keepSort) {
      model.setState(keepSort ? Object.assign({}, next, {sort: model.getState().sort}) : next);
    }
    const patch = (part) => model.patch(part);

    // ---- pasek filtrów
    const TRI_OPTIONS = '<option value="">dowolny</option><option value="yes">jest</option><option value="no">brak</option>';
    const SENT_OPTIONS = '<option value="">dowolnie</option><option value="yes">tak</option><option value="no">nie</option>';

    function buildBar() {
      const fieldOptions = F.QUERY_FIELDS.map((f) => `<option value="${f}">${escapeHtml(F.QUERY_FIELD_LABELS[f])}</option>`).join('');
      opts.filtersEl.innerHTML = `
        <div class="kit-filters">
          <div class="kit-presets" data-role="presets" role="group" aria-label="Szybkie widoki"></div>
          <div class="kit-statuses" data-role="statuses" role="group" aria-label="Status"></div>
          <div class="kit-searchrow">
            <input type="search" data-role="q" placeholder="Szukaj: słowa, „fraza”, -wyklucz…" aria-label="Szukaj" autocomplete="off">
            <select data-role="qfield" aria-label="Szukaj w polu" title="W którym polu szukać">${fieldOptions}</select>
            <div class="kit-pop" data-role="segments"><button type="button" class="kit-btn" aria-expanded="false">Segment</button><div class="kit-popover" hidden></div></div>
            <div class="kit-pop" data-role="countries"><button type="button" class="kit-btn" aria-expanded="false">Kraj</button><div class="kit-popover" hidden></div></div>
            <button type="button" class="kit-btn" data-role="more-toggle" aria-expanded="false">Więcej filtrów</button>
            <div class="kit-pop kit-views" data-role="views"><button type="button" class="kit-btn" aria-expanded="false">Widoki</button><div class="kit-popover align-right" hidden></div></div>
            <label class="kit-only" title="Przy sortowaniu po e-mailu, statusie albo dacie adresy jednej firmy stoją razem, a firmy układają się wg swojego najlepszego adresu"><input type="checkbox" data-role="group"> Grupuj wg firmy</label>
            <button type="button" class="kit-btn" data-role="clear" hidden>Wyczyść filtry</button>
          </div>
          <div class="kit-more" data-role="more" hidden>
            <label>E-mail <select data-field="email">${TRI_OPTIONS}</select></label>
            <label>Treść maila <select data-field="body">${TRI_OPTIONS}</select></label>
            <label>Miasto <select data-field="city">${TRI_OPTIONS}</select></label>
            <label>Adres <select data-field="address">${TRI_OPTIONS}</select></label>
            <label>Wysłano <select data-field="sent">${SENT_OPTIONS}</select></label>
            <label>Adresy firmy <select data-field="multi"><option value="">dowolnie</option><option value="yes">więcej niż jeden</option><option value="no">jeden</option></select></label>
            <label>Wstrzymane przez inny adres firmy <select data-field="held"><option value="">dowolnie</option><option value="yes">tak</option><option value="no">nie</option></select></label>
            <label>Pochodzenie <select data-field="origin"><option value="">dowolne</option><option value="dossier">z dossier</option><option value="import">zaimportowane</option></select></label>
            <div class="kit-range">
              <label>Wysłano od <input type="date" data-field="sentFrom"></label>
              <label>do <input type="date" data-field="sentTo"></label>
              <span class="kit-shortcuts" data-range="sent"><button type="button" data-days="1">Dziś</button><button type="button" data-days="7">7 dni</button><button type="button" data-days="30">30 dni</button></span>
            </div>
            <div class="kit-range">
              <label>Wygenerowano od <input type="date" data-field="generatedFrom"></label>
              <label>do <input type="date" data-field="generatedTo"></label>
              <span class="kit-shortcuts" data-range="generated"><button type="button" data-days="1">Dziś</button><button type="button" data-days="7">7 dni</button><button type="button" data-days="30">30 dni</button></span>
            </div>
          </div>
          <div class="kit-active" data-role="active"></div>
        </div>`;
      const q = (role) => opts.filtersEl.querySelector(`[data-role="${role}"]`);
      bar = {
        presets: q('presets'), statuses: q('statuses'), search: q('q'), qfield: q('qfield'), more: q('more'),
        moreToggle: q('more-toggle'), clear: q('clear'), active: q('active'), group: q('group'),
        segmentsPop: q('segments'), countriesPop: q('countries'), viewsPop: q('views')
      };
      ['segmentsPop', 'countriesPop', 'viewsPop'].forEach((name) => {
        bar[name].btn = bar[name].querySelector('button');
        bar[name].panel = bar[name].querySelector('.kit-popover');
      });
      popover(bar.segmentsPop.btn, bar.segmentsPop.panel);
      popover(bar.countriesPop.btn, bar.countriesPop.panel);
      popover(bar.viewsPop.btn, bar.viewsPop.panel, renderViews);

      bar.presets.addEventListener('click', (e) => {
        const b = e.target.closest('button[data-preset]');
        if (b) applyState(F.presetState(b.dataset.preset, ctx), true);
      });
      bar.statuses.addEventListener('click', (e) => {
        const b = e.target.closest('button[data-status]');
        if (b) patch({statuses: toggled(model.getState().statuses, b.dataset.status)});
      });
      bar.search.addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => patch({q: bar.search.value}), 150);
      });
      bar.qfield.addEventListener('change', () => patch({qFields: bar.qfield.value}));
      bar.group.addEventListener('change', () => patch({group: bar.group.checked ? 'yes' : ''}));
      bar.segmentsPop.panel.addEventListener('change', (e) => {
        if (e.target.dataset.value !== undefined) patch({segments: toggled(model.getState().segments, e.target.dataset.value)});
      });
      bar.countriesPop.panel.addEventListener('change', (e) => {
        if (e.target.dataset.value !== undefined) patch({countries: toggled(model.getState().countries, e.target.dataset.value)});
      });
      bar.moreToggle.addEventListener('click', () => {
        bar.more.hidden = !bar.more.hidden;
        bar.moreToggle.setAttribute('aria-expanded', String(!bar.more.hidden));
      });
      bar.more.addEventListener('change', (e) => {
        const field = e.target.dataset.field;
        if (field) patch({[field]: e.target.value});
      });
      bar.more.addEventListener('click', (e) => {
        const b = e.target.closest('button[data-days]');
        if (!b) return;
        const which = b.parentElement.dataset.range, days = parseInt(b.dataset.days, 10);
        const to = F.localDay();
        patch(which === 'sent' ? {sentFrom: F.shiftDay(to, 1 - days), sentTo: to} : {generatedFrom: F.shiftDay(to, 1 - days), generatedTo: to});
      });
      bar.clear.addEventListener('click', () => applyState(F.emptyState(), true));
      bar.active.addEventListener('click', (e) => {
        const b = e.target.closest('button[data-clear]');
        if (b) model.setState(F.clearPart(model.getState(), b.dataset.clear));
      });
      bar.viewsPop.panel.addEventListener('click', (e) => {
        const views = loadViews();
        const apply = e.target.closest('button[data-apply]'), remove = e.target.closest('button[data-remove]');
        if (apply && views[apply.dataset.apply]) { closePopovers(); applyState(views[apply.dataset.apply].state); }
        if (remove) { views.splice(parseInt(remove.dataset.remove, 10), 1); saveViews(views); renderViews(); }
        if (e.target.closest('button[data-save]')) saveCurrentView();
      });
      bar.viewsPop.panel.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.target.matches('input[data-name]')) { e.preventDefault(); saveCurrentView(); }
      });
    }

    function toggled(list, value) {
      return list.indexOf(value) === -1 ? list.concat([value]) : list.filter((v) => v !== value);
    }

    // zapisane widoki użytkownika (wspólne dla wszystkich stron w tej przeglądarce)
    function loadViews() {
      const list = readStore('views', []);
      if (!Array.isArray(list)) return [];
      return list.filter((v) => v && typeof v.name === 'string' && v.name)
        .map((v) => ({name: v.name.slice(0, 60), state: F.normalizeState(v.state)}));
    }
    function saveViews(list) { writeStore('views', list.slice(0, 50)); }
    function saveCurrentView() {
      const input = bar.viewsPop.panel.querySelector('input[data-name]');
      const name = (input.value || '').trim().slice(0, 60);
      if (!name) { input.focus(); return; }
      const views = loadViews().filter((v) => v.name !== name);
      views.push({name: name, state: model.getState()});
      saveViews(views);
      renderViews();
    }
    function renderViews() {
      const views = loadViews();
      bar.viewsPop.panel.innerHTML =
        (views.length ? views.map((v, i) => `<div class="view"><button type="button" class="apply" data-apply="${i}">${escapeHtml(v.name)}</button>` +
          `<button type="button" class="remove" data-remove="${i}" aria-label="Usuń widok ${escapeHtml(v.name)}"></button></div>`).join('')
          : '<div class="empty">Nie masz jeszcze zapisanych widoków.</div>') +
        '<hr><div class="save"><input type="text" data-name placeholder="Nazwa bieżącego widoku" maxlength="60" aria-label="Nazwa widoku">' +
        '<button type="button" data-save>Zapisz</button></div>';
    }

    // Pasek rysujemy z tego, co ostatnio przysłał serwer (liczniki bez filtra własnego pola), a stan filtrów (zaznaczone
    // chipy, pola) — z bieżącego stanu, więc kliknięcie widać od razu, jeszcze przed odpowiedzią.
    function updateBar() {
      const state = model.getState(), facets = model.facets(), only = model.isOnlySelected();
      const totals = facets.totals, here = facets.overview || {statuses: {}, segments: {}, countries: {}};
      // szybkie widoki
      const activePreset = only ? '' : F.presetKeyFor(state, ctx);
      bar.presets.innerHTML = '<span class="kit-label">Widoki</span>' + F.PRESETS.map((p, i) => {
        const n = facets.presets ? facets.presets[i] : null;
        return `<button type="button" class="kit-pill${n === 0 ? ' is-zero' : ''}" data-preset="${p.key}" aria-pressed="${p.key === activePreset}"` +
          `${p.hint ? ` title="${escapeHtml(p.hint)}"` : ''}>${escapeHtml(p.label)}${n === null ? '' : `<span class="n">${n}</span>`}</button>`;
      }).join('');
      // statusy
      const all = totals ? totals.statuses : {};
      const codes = ctx.statusOrder.concat(Object.keys(all).filter((c) => ctx.statusOrder.indexOf(c) === -1));
      bar.statuses.innerHTML = '<span class="kit-label">Status</span>' + codes.filter((c) => all[c] || state.statuses.indexOf(c) !== -1).map((c) => {
        const info = ctx.statusMeta[c] || [c, '#6b7280'], n = here.statuses[c] || 0, on = state.statuses.indexOf(c) !== -1;
        return `<button type="button" class="kit-chip${n === 0 ? ' is-zero' : ''}" data-status="${escapeHtml(c)}" aria-pressed="${on}" style="--c:${escapeHtml(info[1])}">` +
          `<span class="kit-dot"></span>${escapeHtml(info[0])}<span class="n">${n}</span></button>`;
      }).join('');
      // wyszukiwarka
      if (root.document.activeElement !== bar.search && bar.search.value !== state.q) bar.search.value = state.q;
      bar.qfield.value = state.qFields;
      // segmenty
      const segments = Object.keys(totals ? totals.segments : {})
        .sort((a, b) => (ctx.segmentOrder.indexOf(a) + 1 || 99) - (ctx.segmentOrder.indexOf(b) + 1 || 99) || a.localeCompare(b));
      bar.segmentsPop.panel.innerHTML = segments.map((s) => option(s, s, here.segments[s] || 0, state.segments.indexOf(s) !== -1)).join('');
      setPopButton(bar.segmentsPop.btn, 'Segment', state.segments);
      // kraje
      const present = Object.keys(totals ? totals.countries : {}).filter((c) => c !== F.NO_COUNTRY && c !== F.FOREIGN);
      present.sort((a, b) => (a === 'PL' ? -1 : b === 'PL' ? 1 : (ctx.countryNames[a] || a).localeCompare(ctx.countryNames[b] || b, 'pl')));
      const sel = (c) => state.countries.indexOf(c) !== -1;
      bar.countriesPop.panel.innerHTML =
        option(F.FOREIGN, 'Spoza Polski', here.countries[F.FOREIGN] || 0, sel(F.FOREIGN)) +
        option(F.NO_COUNTRY, 'Bez kraju', here.countries[F.NO_COUNTRY] || 0, sel(F.NO_COUNTRY)) + '<hr>' +
        present.map((c) => option(c, `${ctx.countryFlags[c] || ''} ${ctx.countryNames[c] || c}`.trim(), here.countries[c] || 0, sel(c))).join('');
      setPopButton(bar.countriesPop.btn, 'Kraj', state.countries.map((c) => c === F.FOREIGN ? 'spoza PL' : c === F.NO_COUNTRY ? 'bez kraju' : c));
      // dodatkowe filtry
      bar.more.querySelectorAll('[data-field]').forEach((el) => { if (el.value !== state[el.dataset.field]) el.value = state[el.dataset.field]; });
      const extra = ['email', 'body', 'city', 'address', 'sent', 'multi', 'held', 'origin', 'sentFrom', 'sentTo', 'generatedFrom', 'generatedTo'].some((k) => state[k] !== '');
      bar.group.checked = state.group === 'yes';
      bar.moreToggle.classList.toggle('is-active', extra);
      // aktywne filtry i licznik
      const parts = F.describe(state, ctx);
      bar.clear.hidden = !parts.length && !only;
      bar.active.innerHTML = (only
        ? '<span class="muted">Pokazuję tylko zaznaczone — filtry są wstrzymane.</span>'
        : parts.map((d) => `<button type="button" class="kit-tag" data-clear="${d.part}" title="Usuń ten filtr">${escapeHtml(d.text)}</button>`).join('')) +
        `<span class="kit-count">${model.total()} <span class="muted">z ${totals ? totals.all : '…'}</span></span>`;
    }
    function option(value, label, n, checked) {
      return `<label class="kit-option${n === 0 ? ' is-zero' : ''}"><input type="checkbox" data-value="${escapeHtml(value)}"${checked ? ' checked' : ''}> ${escapeHtml(label)}<span class="n">${n}</span></label>`;
    }
    function setPopButton(btn, label, picked) {
      btn.textContent = picked.length ? `${label}: ${picked.length > 2 ? picked.length : picked.join(', ')}` : label;
      btn.classList.toggle('is-active', picked.length > 0);
    }

    // ---- tabela
    function renderTable() {
      const tbody = opts.tbodyEl, frag = root.document.createDocumentFragment(), rows = pageRows();
      tbody.innerHTML = '';
      rows.forEach((row) => {
        const tr = root.document.createElement('tr');
        const picked = selection.has(row.id);
        tr.className = 'kit-row' + (picked ? ' is-selected' : '') + (opts.rowClass ? ' ' + opts.rowClass(row) : '');
        tr.dataset.id = row.id;
        tr.innerHTML = `<td class="kit-check"><input type="checkbox" class="kit-check-row" data-id="${escapeHtml(row.id)}"` +
          `${picked ? ' checked' : ''}${canSelect(row) ? '' : ' disabled'} aria-label="Zaznacz ${escapeHtml(row.company || row.id)}"></td>` + opts.cells(row);
        if (opts.decorate) opts.decorate(tr, row);
        frag.appendChild(tr);
        const extra = opts.detail ? opts.detail(row) : null;
        if (extra) frag.appendChild(extra);
      });
      tbody.appendChild(frag);
      const sort = model.getState().sort;
      opts.tableEl.querySelectorAll('thead th[data-sort]').forEach((cell) => {
        if (cell.dataset.sort === F.sortKeyOf(sort)) cell.setAttribute('aria-sort', sort.charAt(0) === '-' ? 'descending' : 'ascending');
        else cell.removeAttribute('aria-sort');
      });
      if (opts.emptyEl) { opts.emptyEl.textContent = 'Brak wierszy pasujących do filtrów.'; opts.emptyEl.hidden = rows.length > 0; }
      drawn = JSON.stringify(rows);
      syncChecks();
    }

    // znacznik zaznaczenia we wszystkich widocznych wierszach + nagłówek
    function syncChecks() {
      opts.tbodyEl.querySelectorAll('tr.kit-row').forEach((tr) => {
        const on = selection.has(tr.dataset.id);
        tr.classList.toggle('is-selected', on);
        const box = tr.querySelector('.kit-check-row');
        if (box && box.checked !== on) box.checked = on;
      });
      const head = opts.tableEl.querySelector('.kit-check-all');
      if (head) {
        const stateName = selection.headerState(pageSelectable());
        head.checked = stateName === 'all';
        head.indeterminate = stateName === 'some';
      }
    }

    // ---- paginacja
    function renderPager() {
      if (!opts.pagerEl) return;
      const total = model.total(), pages = model.pages(), page = model.getPage(), size = model.getSize();
      const from = total === 0 ? 0 : (page - 1) * size + 1, to = Math.min(total, page * size);
      opts.pagerEl.innerHTML = `<div class="kit-pager"><span>${total ? `Wiersze ${from}–${to} z ${total}` : 'Brak wierszy'}</span>` +
        (pages > 1 ? `<span class="pages"><button type="button" data-page="1" ${page === 1 ? 'disabled' : ''} aria-label="Pierwsza strona">«</button>` +
          `<button type="button" data-page="${page - 1}" ${page === 1 ? 'disabled' : ''} aria-label="Poprzednia strona">‹</button>` +
          `<span>Strona <b>${page}</b> z ${pages}</span>` +
          `<button type="button" data-page="${page + 1}" ${page === pages ? 'disabled' : ''} aria-label="Następna strona">›</button>` +
          `<button type="button" data-page="${pages}" ${page === pages ? 'disabled' : ''} aria-label="Ostatnia strona">»</button></span>` : '') +
        `<label>Na stronie <select data-size>${model.SIZES.map((n) => `<option value="${n}"${n === size ? ' selected' : ''}>${n}</option>`).join('')}</select></label></div>`;
    }

    // ---- pasek zaznaczania
    function buildSelectBar() {
      if (!opts.selectEl) return;
      const extras = (opts.selectItems || []).map((item, i) => `<button type="button" class="item" data-extra="${i}"></button>`).join('');
      opts.selectEl.innerHTML = `
        <div class="kit-selectbar">
          <div class="kit-pop kit-menu"><button type="button" class="kit-btn" aria-expanded="false">Zaznacz</button>
            <div class="kit-popover" hidden>
              <button type="button" class="item" data-act="page"></button>
              <button type="button" class="item" data-act="all"></button>
              ${extras}
              <div class="inline">Pierwsze <input type="number" min="1" value="10" data-first aria-label="Ile pierwszych wierszy"> <button type="button" data-act="first">Zaznacz</button></div>
              <button type="button" class="item" data-act="siblings">Dodaj pozostałe adresy zaznaczonych firm</button>
              <hr>
              <button type="button" class="item" data-act="invert">Odwróć zaznaczenie w widoku</button>
              <button type="button" class="item" data-act="unmatch">Odznacz wiersze z widoku</button>
              <button type="button" class="item" data-act="clear"></button>
            </div></div>
          <span class="kit-selcount" aria-live="polite"></span>
          <label class="kit-only"><input type="checkbox" data-only> Pokaż tylko zaznaczone</label>
        </div>`;
      const q = (sel) => opts.selectEl.querySelector(sel);
      selectBar = {pop: q('.kit-pop'), menu: q('.kit-popover'), count: q('.kit-selcount'), only: q('[data-only]'), first: q('[data-first]')};
      popover(selectBar.pop.querySelector('button'), selectBar.menu);
      selectBar.menu.addEventListener('click', onSelectMenu);
      selectBar.only.addEventListener('change', () => model.setOnlySelected(selectBar.only.checked));
    }

    // Akcje, które potrzebują całego widoku (a nie tylko strony), pytają serwer o id w kolejności widoku.
    async function onSelectMenu(e) {
      const b = e.target.closest('button[data-act],button[data-extra]');
      if (!b) return;
      closePopovers();
      const act = b.dataset.act;
      try {
        if (b.dataset.extra !== undefined) {
          await opts.selectItems[parseInt(b.dataset.extra, 10)].run({ids: (extra) => model.viewIds(Object.assign({}, lockedFilter(), extra)), selection: selection});
        } else if (act === 'page') {
          selection.add(pageSelectable());
        } else if (act === 'all' || act === 'invert') {
          const found = await model.viewIds(lockedFilter());
          if (act === 'all') selection.add(found.ids); else selection.invert(found.ids);
          if (found.truncated) tell(`Objęto tylko pierwsze ${found.ids.length} firm — zawęź filtry, żeby objąć resztę.`);
        } else if (act === 'first') {
          const n = Math.max(1, parseInt(selectBar.first.value, 10) || 1);
          selection.addFirst((await model.viewIds(Object.assign({limit: n}, lockedFilter()))).ids, n);
        } else if (act === 'siblings') {
          const found = await model.siblingIds();
          selection.add(found.ids);
          if (found.truncated) tell(`Objęto tylko pierwsze ${found.ids.length} adresów — zaznacz mniej firm.`);
        } else if (act === 'unmatch') {
          selection.remove((await model.viewIds()).ids);
        } else if (act === 'clear') {
          selection.clear();
        }
      } catch (err) {
        tell(err.message);
      }
      selectionChanged();
    }

    function updateSelectBar() {
      if (!selectBar) return;
      const total = selection.size, summary = model.summary(), only = model.isOnlySelected();
      let text = total ? `Zaznaczono ${total}` : 'Nic nie zaznaczono';
      if (total && !only && summary && !model.summaryPending() && summary.in_view !== undefined && total > summary.in_view) {
        text += ` (${total - summary.in_view} poza widokiem)`;
      }
      selectBar.count.textContent = text;
      const label = (act, value) => { selectBar.menu.querySelector(`[data-act="${act}"]`).textContent = value; };
      label('page', `Zaznacz stronę (${pageSelectable().length})`);
      label('all', `Zaznacz wszystkie pasujące (${model.total()})`);
      label('clear', `Wyczyść zaznaczenie (${total})`);
      (opts.selectItems || []).forEach((item, i) => {
        selectBar.menu.querySelector(`[data-extra="${i}"]`).textContent = typeof item.label === 'function' ? item.label({total: model.total()}) : item.label;
      });
      selectBar.menu.querySelector('[data-act="clear"]').disabled = total === 0;
      selectBar.menu.querySelector('[data-act="siblings"]').disabled = total === 0;
      selectBar.only.checked = only;
      selectBar.only.disabled = total === 0 && !only;
    }

    function selectionChanged() {
      syncChecks();
      updateSelectBar();
      model.selectionChanged();   // podsumowanie od nowa; w widoku „tylko zaznaczone” także lista
      if (opts.onChange) opts.onChange({reason: 'selection'});
    }

    // ---- zdarzenia tabeli i paginacji
    function wireTable() {
      const head = opts.tableEl.querySelector('thead tr');
      head.insertAdjacentHTML('afterbegin', '<th class="kit-check"><input type="checkbox" class="kit-check-all" aria-label="Zaznacz stronę"></th>');
      head.addEventListener('click', (e) => {
        const th = e.target.closest('th[data-sort]');
        if (th) model.setSort(F.nextSort(model.getState().sort, th.dataset.sort));
      });
      head.querySelector('.kit-check-all').addEventListener('click', (e) => {
        const ids = pageSelectable();
        if (selection.headerState(ids) === 'all') selection.remove(ids); else selection.add(ids);
        e.stopPropagation();
        selectionChanged();
      });
      const toggleRow = (id, on, shift) => {
        if (shift) selection.range(pageSelectable(), id, on); else selection.toggle(id, on);
        selectionChanged();
      };
      opts.tbodyEl.addEventListener('click', (e) => {
        const box = e.target.closest('input.kit-check-row');
        if (box) { toggleRow(box.dataset.id, box.checked, e.shiftKey); return; }
        if (!opts.rowClickToggles || e.target.closest('a,button,input,select,textarea,label')) return;
        const tr = e.target.closest('tr.kit-row');
        if (tr && canSelect(pageRows().find((r) => r.id === tr.dataset.id) || {})) toggleRow(tr.dataset.id, !selection.has(tr.dataset.id), e.shiftKey);
      });
      // Odświeżenie w tle (po edycji, wysyłce) nie może zabrać komórki, w której ktoś właśnie pisze.
      opts.tbodyEl.addEventListener('focusout', () => {
        if (!deferred) return;
        setTimeout(() => { if (!editing()) { deferred = false; drawBody(); } }, 0);
      });
      if (opts.pagerEl) {
        opts.pagerEl.addEventListener('click', (e) => {
          const b = e.target.closest('button[data-page]');
          if (b && !b.disabled) model.goTo(parseInt(b.dataset.page, 10));
        });
        opts.pagerEl.addEventListener('change', (e) => {
          if (e.target.matches('select[data-size]')) model.setSize(parseInt(e.target.value, 10));
        });
      }
    }

    function editing() {
      const el = root.document.activeElement;
      return !!el && opts.tbodyEl.contains(el) && el.matches('input,textarea,select');
    }
    function drawBody() { renderTable(); updateSelectBar(); }

    // ---- model → DOM
    function onData() {
      if (bar) updateBar();
      renderPager();
      if (JSON.stringify(pageRows()) !== drawn) {
        if (editing()) deferred = true; else drawBody();
      } else {
        updateSelectBar();
      }
      persist();
      if (opts.onChange) opts.onChange({reason: 'data'});
    }
    function setBusy(on) {
      opts.tableEl.classList.toggle('is-loading', on);
      opts.tableEl.setAttribute('aria-busy', String(on));
    }
    model.subscribe((type) => {
      if (type === 'state') { persist(); if (bar) updateBar(); updateSelectBar(); }
      else if (type === 'loading') setBusy(true);
      else if (type === 'data') { setBusy(false); onData(); }
      else if (type === 'error') {
        setBusy(false);
        if (opts.emptyEl) { opts.emptyEl.textContent = model.error(); opts.emptyEl.hidden = false; }
        tell(model.error());
      } else if (type === 'summary') {
        updateSelectBar();
        if (opts.onChange) opts.onChange({reason: 'summary'});
      }
    });

    // ---- start
    buildBar();
    buildSelectBar();
    wireTable();
    restore();
    if (bar) updateBar();
    updateSelectBar();

    return {
      start() { return model.load(); },
      // Dane zmieniły się poza listą (edycja, zmiana zbiorcza, wysyłka): serwer liczy wszystko od nowa.
      refresh() { return model.refresh(); },
      // Zmieniony wiersz (np. po edycji pola) rysujemy od razu z danych, które już mamy; serwer dopowie resztę.
      redraw() { drawBody(); },
      pageRows: pageRows,
      total: () => model.total(),
      getState: () => model.getState(),
      applyState: applyState,
      selectionChanged: selectionChanged,
      onlySelected: () => model.isOnlySelected(),
      summary: () => model.summary(),
      summaryPending: () => model.summaryPending(),
      error: () => model.error(),
      viewIds: (extra) => model.viewIds(extra),
      siblingIds: () => model.siblingIds(),
      exportScope: () => model.exportScope()
    };
  }

  root.PernoKit = {
    createListView: createListView, badge: badge, countryCell: countryCell, websiteLink: websiteLink,
    addressMarker: addressMarker, holdBadge: holdBadge, isSibling: isSibling,
    shortDate: shortDate, plural: plural, readStore: readStore, writeStore: writeStore, closePopovers: closePopovers,
    popover: popover
  };
})(window);
