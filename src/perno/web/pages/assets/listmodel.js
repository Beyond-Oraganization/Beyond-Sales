// Model listy firm: stan widoku (filtry, strona, rozmiar, „tylko zaznaczone”), żądania do serwera, liczniki i podsumowanie
// zaznaczenia — sama logika, bez DOM (globalny PernoListModel na stronie, moduł w testach Node: tests/js/listmodel.test.js).
// Wiersze, sumy i liczniki liczy serwer (/api/mailing/*): przy 50 tys. firm lista w przeglądarce miałaby 27 MB. Tu zostaje
// to, co trzeba wiedzieć po stronie przeglądarki: sklejanie szybko po sobie idących żądań, odrzucanie odpowiedzi, które
// wyprzedziło nowsze żądanie, i pamięć ostatnich liczników (strona i sortowanie ich nie zmieniają, więc ich nie liczymy).
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.PernoListModel = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var SIZES = [50, 100, 200, 500];   // serwer oddaje najwyżej 1000 wierszy na stronę
  var DEFAULT_SIZE = 100;

  // Co serwer ma policzyć oprócz strony wierszy: counts = suma i liczniki przy bieżącym filtrze, totals = liczniki bez
  // filtrów (jakie wartości w ogóle istnieją), presets = liczba wierszy szybkich widoków.
  function noNeeds() { return {counts: false, totals: false, presets: false}; }
  function allNeeds() { return {counts: true, totals: true, presets: true}; }
  function merge(a, b) { return {counts: a.counts || b.counts, totals: a.totals || b.totals, presets: a.presets || b.presets}; }
  function anyNeeds(n) { return n.counts || n.totals || n.presets; }

  function create(o) {
    var F = o.filters, selection = o.selection, ctx = o.ctx;
    var urls = Object.assign({list: '/api/mailing/list', ids: '/api/mailing/ids', selection: '/api/mailing/selection',
                              siblings: '/api/mailing/siblings'}, o.urls || {});
    var timers = o.timers || {set: function (fn, ms) { return setTimeout(fn, ms); }, clear: function (id) { clearTimeout(id); }};
    var coalesceMs = o.coalesceMs === undefined ? 25 : o.coalesceMs;
    var summaryMs = o.summaryMs === undefined ? 250 : o.summaryMs;

    var state = F.emptyState(), page = 1, size = SIZES.indexOf(o.defaultSize) !== -1 ? o.defaultSize : DEFAULT_SIZE;
    var onlySelected = false;
    var data = {rows: [], total: 0, pages: 1};
    var facets = {overview: null, totals: null, presets: null};
    var summary = null, summaryPending = false;
    var loading = false, error = '';
    var listeners = [];
    var owed = allNeeds();     // o co serwer jeszcze nie odpowiedział (pierwsze wczytanie potrzebuje wszystkiego)
    var timer = null, sequence = 0, version = 0;   // version rośnie przy każdej zmianie widoku: starsza odpowiedź go nie nadpisze
    var waiting = [];          // obietnice czekające na najbliższą zakończoną odpowiedź
    var summaryTimer = null, summarySequence = 0;

    // Wołanie serwera (od razu, bez zwłoki), które nigdy nie rzuca: błąd sieci to zwykła odpowiedź z komunikatem.
    function unreachable() { return {ok: false, status: 0, data: {error: 'Nie udało się połączyć z serwerem.'}}; }
    function call(url, payload) {
      try {
        return Promise.resolve(o.api(url, payload)).catch(unreachable);
      } catch (err) {
        return Promise.resolve(unreachable());
      }
    }

    function emit(type) { listeners.slice().forEach(function (fn) { fn(type); }); }

    // Stan przekazywany serwerowi. W widoku „tylko zaznaczone” filtry są wstrzymane (zostaje sortowanie).
    function viewState() { return onlySelected ? {sort: state.sort} : state; }

    function buildRequest(needs) {
      var request = {state: viewState(), page: page, size: size};
      if (onlySelected) request.ids = selection.toArray();
      if (needs.counts) request.counts = true;
      if (needs.totals) request.totals = true;
      if (needs.presets) {
        ctx.today = F.localDay();   // „Wysłane dziś” zależy od dnia
        request.presets = F.PRESETS.map(function (preset) { return F.presetState(preset.key, ctx); });
      }
      return request;
    }

    function apply(d) {
      data = {rows: d.rows || [], total: d.total || 0, pages: d.pages || 1};
      page = d.page || 1;   // serwer przycina stronę, gdy filtr zawęził wyniki
      if (d.size) size = d.size;
      if (d.overview) facets.overview = d.overview;
      if (d.totals) facets.totals = d.totals;
      if (d.presets) facets.presets = d.presets;
      error = '';
    }

    function settle(resolvers) { resolvers.forEach(function (resolve) { resolve(); }); }

    function run() {
      timer = null;
      var needs = owed, resolvers = waiting, mine = ++sequence, sent = version;
      owed = noNeeds();
      waiting = [];
      loading = true;
      emit('loading');
      call(urls.list, buildRequest(needs)).then(function (res) {
        if (mine !== sequence || sent !== version) {   // wyprzedzone albo widok zmienił się w międzyczasie: to, o co prosiliśmy, jeszcze nam się należy
          owed = merge(owed, needs);
          waiting = resolvers.concat(waiting);
          return;
        }
        loading = false;
        if (!res.ok) {
          owed = merge(owed, needs);
          error = (res.data && res.data.error) || 'Nie udało się wczytać listy.';
          emit('error');
        } else {
          apply(res.data);
          emit('data');
          scheduleSummary();
        }
        settle(resolvers);
        if (res.ok && anyNeeds(owed)) schedule(0);
      });
    }

    function schedule(delay) {
      var done = new Promise(function (resolve) { waiting.push(resolve); });
      if (timer === null) timer = timers.set(run, delay === undefined ? coalesceMs : delay);
      return done;
    }

    function changed(needs, resetPage) {
      if (resetPage) page = 1;
      version++;
      owed = merge(owed, needs);
      emit('state');
      return schedule();
    }

    // ---- podsumowanie zaznaczenia (ile gotowych, z zagranicy, poza widokiem) ---------------------

    function fetchSummary() {
      summaryTimer = null;
      var mine = ++summarySequence;
      var body = {ids: selection.toArray()};
      if (!onlySelected) body.state = state;
      return call(urls.selection, body).then(function (res) {
        if (mine !== summarySequence || !res.ok) return;
        summary = res.data;
        if (onlySelected) summary.in_view = summary.count;
        summaryPending = false;
        emit('summary');
      });
    }

    function scheduleSummary() {
      timers.clear(summaryTimer);
      summaryTimer = null;
      summarySequence++;
      if (!selection.size) {
        var stale = !summary || summary.count !== 0 || summaryPending;
        summary = {count: 0, ready: 0, foreign: {}, in_view: 0};
        summaryPending = false;
        if (stale) emit('summary');
        return;
      }
      summaryPending = true;
      summaryTimer = timers.set(fetchSummary, summaryMs);
    }

    var model = {
      SIZES: SIZES,
      subscribe: function (fn) { listeners.push(fn); },
      getState: function () { return state; },
      getPage: function () { return page; },
      getSize: function () { return size; },
      isOnlySelected: function () { return onlySelected; },
      rows: function () { return data.rows; },
      total: function () { return data.total; },
      pages: function () { return data.pages; },
      isLoading: function () { return loading; },
      error: function () { return error; },
      facets: function () { return facets; },
      summary: function () { return summary; },
      summaryPending: function () { return summaryPending; },
      viewState: viewState,

      // Stan z adresu URL albo pamięci przeglądarki, przed pierwszym wczytaniem (niczego nie pobiera).
      restore: function (parts) {
        if (parts.state) state = F.normalizeState(parts.state);
        if (parts.page > 0) page = Math.floor(parts.page);
        if (SIZES.indexOf(parts.size) !== -1) size = parts.size;
      },

      // Zmiany filtrów wracają na pierwszą stronę i potrzebują nowych liczników; strona i sortowanie — nie.
      setState: function (next) { state = F.normalizeState(next); onlySelected = false; return changed({counts: true}, true); },
      patch: function (part) { state = F.normalizeState(Object.assign({}, state, part)); return changed({counts: true}, true); },
      setSort: function (sort) { state = F.normalizeState(Object.assign({}, state, {sort: sort})); return changed(noNeeds(), true); },
      goTo: function (n) { page = Math.max(1, Math.floor(n) || 1); return changed(noNeeds(), false); },
      setSize: function (n) {
        if (SIZES.indexOf(n) === -1) return Promise.resolve();
        size = n;
        return changed(noNeeds(), true);
      },
      setOnlySelected: function (on) { onlySelected = !!on; return changed({counts: true}, true); },

      // Dane zmieniły się poza listą (edycja, zmiana zbiorcza, wysyłka): wczytaj wszystko jeszcze raz.
      refresh: function () { return changed(allNeeds(), false); },
      load: function () { return schedule(0); },

      // Zaznaczenie się zmieniło: podsumowanie liczymy od nowa, a widok „tylko zaznaczone” zależy od samych id.
      selectionChanged: function () {
        scheduleSummary();
        return onlySelected ? changed({counts: true}, false) : Promise.resolve();
      },

      // Id wierszy bieżącego widoku w jego kolejności; `extra`: ready, limit, exclude_statuses (patrz services/listing.py).
      viewIds: function (extra) {
        var body = Object.assign({state: viewState()}, onlySelected ? {ids: selection.toArray()} : {}, extra || {});
        return call(urls.ids, body).then(function (res) {
          if (!res.ok) throw new Error((res.data && res.data.error) || 'Nie udało się pobrać listy firm.');
          return res.data;
        });
      },

      // Wszystkie adresy firm zaznaczonych adresów (w kolejności panelu), także tych spoza bieżącego widoku.
      siblingIds: function () {
        return call(urls.siblings, {ids: selection.toArray()}).then(function (res) {
          if (!res.ok) throw new Error((res.data && res.data.error) || 'Nie udało się pobrać pozostałych adresów firm.');
          return res.data;
        });
      },

      // Co wysłać do eksportu, żeby dostać dokładnie to, co widać: stan widoku albo (w „tylko zaznaczone”) same id.
      exportScope: function () {
        return onlySelected ? {ids: selection.toArray(), sort: state.sort, group: state.group} : {state: state};
      }
    };
    return model;
  }

  return {create: create, SIZES: SIZES};
});
