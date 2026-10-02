// Zbiór zaznaczonych wierszy — sama logika, bez DOM (globalny PernoSelection na stronie, moduł w testach Node).
// Zaznaczenie to zbiór id, który przeżywa zmianę filtrów i sortowania; „widok” to uporządkowana lista id,
// które użytkownik widzi (po filtrach), a „strona” — jej bieżący wycinek.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.PernoSelection = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function create(initial) {
    var ids = new Set(initial || []);
    var anchor = null;  // ostatnio klikany wiersz — początek zakresu przy Shift+klik

    var api = {
      has: function (id) { return ids.has(id); },
      get size() { return ids.size; },
      toArray: function () { return Array.from(ids); },

      // Zaznacz/odznacz jeden wiersz (bez `on` przełącza) i zapamiętaj go jako początek zakresu.
      toggle: function (id, on) {
        var next = on === undefined ? !ids.has(id) : !!on;
        if (next) ids.add(id); else ids.delete(id);
        anchor = id;
        return next;
      },

      // Shift+klik: wiersze od ostatnio klikanego do `toId` (włącznie, w kolejności widoku) dostają stan `on`.
      // Bez początku zakresu (albo gdy zniknął z widoku) działa jak zwykłe kliknięcie.
      range: function (order, toId, on) {
        var from = anchor === null ? -1 : order.indexOf(anchor);
        var to = order.indexOf(toId);
        if (from === -1 || to === -1) return api.toggle(toId, on);
        var lo = Math.min(from, to), hi = Math.max(from, to);
        for (var i = lo; i <= hi; i++) { if (on) ids.add(order[i]); else ids.delete(order[i]); }
        anchor = toId;
        return !!on;
      },

      add: function (list) { list.forEach(function (id) { ids.add(id); }); },
      remove: function (list) { list.forEach(function (id) { ids.delete(id); }); },
      clear: function () { ids.clear(); anchor = null; },
      replace: function (list) { ids = new Set(list); anchor = null; },
      addFirst: function (order, n) { api.add(order.slice(0, Math.max(0, n))); },

      // Odwróć w obrębie podanych id (np. widoku): zaznaczone → odznaczone i odwrotnie. Reszta bez zmian.
      invert: function (list) {
        list.forEach(function (id) { if (ids.has(id)) ids.delete(id); else ids.add(id); });
      },

      // Po odświeżeniu danych: wyrzuć zaznaczone id, których już nie ma.
      prune: function (existing) {
        var keep = existing instanceof Set ? existing : new Set(existing);
        Array.from(ids).forEach(function (id) { if (!keep.has(id)) ids.delete(id); });
        if (anchor !== null && !keep.has(anchor)) anchor = null;
      },

      // Ile zaznaczonych jest w widoku, a ile poza nim (ukrytych filtrem) — do licznika „Zaznaczono 37 (5 poza widokiem)”.
      counts: function (viewIds) {
        var inView = 0;
        viewIds.forEach(function (id) { if (ids.has(id)) inView++; });
        return {total: ids.size, inView: inView, outside: ids.size - inView};
      },

      // Stan checkboxa w nagłówku dla podanych id (zwykle bieżąca strona): none / some / all.
      headerState: function (list) {
        if (!list.length) return 'none';
        var n = 0;
        list.forEach(function (id) { if (ids.has(id)) n++; });
        return n === 0 ? 'none' : n === list.length ? 'all' : 'some';
      },

      // Zaznaczone w kolejności widoku (do eksportu „zaznaczone”: plik ma wyglądać jak lista); id spoza widoku na końcu.
      ordered: function (order) {
        var seen = new Set(), out = [];
        order.forEach(function (id) { if (ids.has(id) && !seen.has(id)) { seen.add(id); out.push(id); } });
        ids.forEach(function (id) { if (!seen.has(id)) out.push(id); });
        return out;
      }
    };
    return api;
  }

  return {create: create};
});
