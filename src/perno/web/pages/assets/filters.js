// Stan filtrów listy firm, szybkie widoki, opis aktywnych filtrów i adres URL — sama logika, bez DOM.
// Ten plik ładuje strona (jako globalny PernoFilters) i Node (testy: tests/js/filters.test.js).
// Wiersze, wyszukiwanie, sortowanie i liczniki liczy serwer (perno.domain.listing → perno.db.listing_*, patrz
// tests/listing_cases.json); tu zostaje to, co przeglądarka musi wiedzieć sama: jak wygląda stan, jak trafia do adresu URL
// i zapisanych widoków oraz które szybkie widoki istnieją.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.PernoFilters = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var NO_COUNTRY = '_none';      // firmy bez kraju
  var FOREIGN = '_foreign';      // firmy z krajem innym niż Polska
  var TRI = ['', 'yes', 'no'];
  var ORIGINS = ['', 'dossier', 'import'];
  var QUERY_FIELDS = ['all', 'company', 'email', 'website', 'place', 'description'];
  var QUERY_FIELD_LABELS = {all: 'wszędzie', company: 'firma', email: 'e-mail', website: 'strona', place: 'kraj i adres', description: 'opis'};
  var ARRAY_KEYS = ['statuses', 'segments', 'countries'];

  // ---- stan filtra ------------------------------------------------------------------------

  function emptyState() {
    return {
      q: '', qFields: 'all',
      statuses: [], segments: [], countries: [],
      email: '', body: '', city: '', address: '', sent: '', origin: '',
      sentFrom: '', sentTo: '', generatedFrom: '', generatedTo: '',
      sort: 'default'
    };
  }

  function isDay(value) { return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value); }

  function stringList(value) {
    if (!Array.isArray(value)) return [];
    var seen = {}, out = [];
    value.forEach(function (item) {
      if (typeof item === 'string' && item && item.length <= 64 && !seen[item] && out.length < 100) {
        seen[item] = true;
        out.push(item);
      }
    });
    return out;
  }

  // Stan z adresu URL, localStorage albo zapisanego widoku bywa stary, ręcznie zmieniony albo zepsuty —
  // przepuszczamy tylko znane pola o poprawnym typie, reszta wraca do wartości domyślnych. Serwer robi to samo
  // (perno.domain.listing.parse_state), bo dostaje ten stan w żądaniu.
  function normalizeState(input) {
    var s = emptyState();
    if (!input || typeof input !== 'object') return s;
    if (typeof input.q === 'string') s.q = input.q.slice(0, 200);
    if (QUERY_FIELDS.indexOf(input.qFields) !== -1) s.qFields = input.qFields;
    ARRAY_KEYS.forEach(function (key) { s[key] = stringList(input[key]); });
    ['email', 'body', 'city', 'address', 'sent'].forEach(function (key) {
      if (TRI.indexOf(input[key]) !== -1) s[key] = input[key];
    });
    if (ORIGINS.indexOf(input.origin) !== -1) s.origin = input.origin;
    ['sentFrom', 'sentTo', 'generatedFrom', 'generatedTo'].forEach(function (key) {
      if (isDay(input[key])) s[key] = input[key];
    });
    if (typeof input.sort === 'string' && /^-?[a-z_]+$/.test(input.sort)) s.sort = input.sort;
    return s;
  }

  var PARTS = {  // klucz części filtra → pola stanu, które ona obejmuje (do „x” na liście aktywnych filtrów)
    q: ['q', 'qFields'], statuses: ['statuses'], segments: ['segments'], countries: ['countries'],
    email: ['email'], body: ['body'], city: ['city'], address: ['address'], sent: ['sent'], origin: ['origin'],
    sent_range: ['sentFrom', 'sentTo'], generated_range: ['generatedFrom', 'generatedTo']
  };

  function clearPart(state, part) {
    var next = normalizeState(state), blank = emptyState();
    (PARTS[part] || []).forEach(function (key) { next[key] = blank[key]; });
    return next;
  }

  function activeParts(state) {
    var s = normalizeState(state);
    return Object.keys(PARTS).filter(function (part) {
      if (part === 'q') return s.q.trim() !== '';
      return PARTS[part].some(function (key) {
        return Array.isArray(s[key]) ? s[key].length > 0 : s[key] !== '';
      });
    });
  }

  // ---- dni i kontekst ----------------------------------------------------------------------

  function localDay(date) {
    var d = date || new Date();
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function shiftDay(day, delta) {
    var parts = day.split('-').map(Number);
    return localDay(new Date(parts[0], parts[1] - 1, parts[2] + delta));
  }

  // ctx: {statusMeta: {kod: [etykieta, kolor]}, sendable: [kody], countries: [[kod, nazwa, flaga]], today: 'RRRR-MM-DD'}
  function createContext(options) {
    var o = options || {};
    var meta = o.statusMeta || {};
    var names = {}, flags = {};
    (o.countries || []).forEach(function (c) { names[c[0]] = c[1]; flags[c[0]] = c[2] || ''; });
    return {
      statusMeta: meta,
      statusOrder: Object.keys(meta),
      sendable: o.sendable || [],
      segmentOrder: o.segmentOrder || ['eventy', 'driverzy'],
      countryNames: names,
      countryFlags: flags,
      today: o.today || localDay()
    };
  }

  // ---- sortowanie (samo przełączanie; porządkuje serwer) -----------------------------------

  function sortKeyOf(sort) { return String(sort || '').replace(/^-/, ''); }

  // Kolejne kliknięcia nagłówka: brak → rosnąco → malejąco → domyślnie.
  function nextSort(current, key) {
    if (sortKeyOf(current) !== key) return key;
    return current === key ? '-' + key : 'default';
  }

  // ---- szybkie widoki ----------------------------------------------------------------------

  // Liczbę wierszy każdego widoku liczy serwer na życzenie (kolejność tej listy = kolejność liczb w odpowiedzi).
  var PRESETS = [
    {key: 'all', label: 'Wszystkie', state: function () { return {}; }},
    {key: 'ready', label: 'Gotowe do wysłania', hint: 'Status „Do wysłania” lub „Błąd”, jest e-mail i treść',
      state: function (ctx) { return {statuses: ctx.sendable.slice(), email: 'yes', body: 'yes'}; }},
    {key: 'errors', label: 'Błędy', state: function () { return {statuses: ['blad']}; }},
    {key: 'no_email', label: 'Bez e-maila', state: function () { return {email: 'no'}; }},
    {key: 'email_no_body', label: 'Mają e-mail, brak treści', state: function () { return {email: 'yes', body: 'no'}; }},
    {key: 'sent_today', label: 'Wysłane dziś', hint: 'Data wysłania = dzisiaj',
      state: function (ctx) { return {sentFrom: ctx.today, sentTo: ctx.today}; }},
    {key: 'sent_abroad', label: 'Wysłane do firm spoza PL', hint: 'Mail już poszedł, a firma jest spoza Polski',
      state: function () { return {sent: 'yes', countries: [FOREIGN]}; }},
    {key: 'replies', label: 'Odpowiedzi', hint: 'Status „Odpowiedział” lub „Zainteresowany”',
      state: function () { return {statuses: ['odpowiedzial', 'zainteresowany']}; }},
    {key: 'do_not_contact', label: 'Nie kontaktować', state: function () { return {statuses: ['nie_kontaktowac']}; }},
    {key: 'imported', label: 'Zaimportowane', hint: 'Firmy dodane z list CSV (bez dossier)', state: function () { return {origin: 'import'}; }},
    {key: 'no_country', label: 'Bez kraju', state: function () { return {countries: [NO_COUNTRY]}; }},
    {key: 'no_city', label: 'Bez miasta', state: function () { return {city: 'no'}; }}
  ];

  function presetState(key, ctx) {
    var preset = PRESETS.filter(function (p) { return p.key === key; })[0];
    return normalizeState(preset ? preset.state(ctx) : {});
  }

  function sameFilters(a, b) {
    var x = normalizeState(a), y = normalizeState(b);
    return Object.keys(emptyState()).every(function (key) {
      if (key === 'sort' || key === 'qFields') return true;
      return JSON.stringify(Array.isArray(x[key]) ? x[key].slice().sort() : x[key]) ===
        JSON.stringify(Array.isArray(y[key]) ? y[key].slice().sort() : y[key]);
    });
  }

  // Który szybki widok odpowiada bieżącym filtrom (puste = żaden) — do podświetlenia przycisku.
  function presetKeyFor(state, ctx) {
    for (var i = 0; i < PRESETS.length; i++) {
      if (sameFilters(state, presetState(PRESETS[i].key, ctx))) return PRESETS[i].key;
    }
    return '';
  }

  // ---- opis filtrów i adres URL ------------------------------------------------------------

  var TRI_TEXT = {
    email: ['E-mail', 'jest', 'brak'], body: ['Treść maila', 'jest', 'brak'], city: ['Miasto', 'jest', 'brak'],
    address: ['Adres', 'jest', 'brak'], sent: ['Wysłano', 'tak', 'nie']
  };

  function countryLabel(code, ctx) {
    if (code === NO_COUNTRY) return 'bez kraju';
    if (code === FOREIGN) return 'spoza Polski';
    return ctx.countryNames[code] || code;
  }

  function rangeText(label, from, to) {
    if (from && to) return from === to ? label + ': ' + from : label + ': ' + from + ' – ' + to;
    return from ? label + ' od ' + from : label + ' do ' + to;
  }

  // [{part, text}] dla każdej aktywnej części filtra — do linii „Aktywne filtry”.
  function describe(state, ctx) {
    var s = normalizeState(state);
    return activeParts(s).map(function (part) {
      var text;
      if (part === 'q') text = 'Szukaj (' + QUERY_FIELD_LABELS[s.qFields] + '): ' + s.q.trim();
      else if (part === 'statuses') text = 'Status: ' + s.statuses.map(function (c) { return (ctx.statusMeta[c] || [c])[0]; }).join(', ');
      else if (part === 'segments') text = 'Segment: ' + s.segments.join(', ');
      else if (part === 'countries') text = 'Kraj: ' + s.countries.map(function (c) { return countryLabel(c, ctx); }).join(', ');
      else if (part === 'origin') text = 'Pochodzenie: ' + (s.origin === 'import' ? 'zaimportowane' : 'z dossier');
      else if (part === 'sent_range') text = rangeText('Data wysłania', s.sentFrom, s.sentTo);
      else if (part === 'generated_range') text = rangeText('Data wygenerowania', s.generatedFrom, s.generatedTo);
      else text = TRI_TEXT[part][0] + ': ' + TRI_TEXT[part][s[part] === 'yes' ? 1 : 2];
      return {part: part, text: text};
    });
  }

  var QUERY_NAMES = {
    statuses: 'status', segments: 'segment', countries: 'country', email: 'email', body: 'body', city: 'city',
    address: 'address', sent: 'sent', origin: 'origin', sentFrom: 'sent_from', sentTo: 'sent_to',
    generatedFrom: 'gen_from', generatedTo: 'gen_to', q: 'q', qFields: 'qf', sort: 'sort'
  };

  // Stan → ciąg zapytania (`status=wyslano,blad&country=PL`); wartości domyślne są pomijane.
  function toQuery(state) {
    var s = normalizeState(state), blank = emptyState(), pairs = [];
    Object.keys(QUERY_NAMES).forEach(function (key) {
      var value = Array.isArray(s[key]) ? s[key].join(',') : s[key];
      var base = Array.isArray(blank[key]) ? blank[key].join(',') : blank[key];
      if (value !== base) pairs.push(encodeURIComponent(QUERY_NAMES[key]) + '=' + encodeURIComponent(value).replace(/%2C/g, ','));
    });
    return pairs.join('&');
  }

  function fromQuery(query) {
    var raw = {}, params = new URLSearchParams(String(query || '').replace(/^\?/, ''));
    Object.keys(QUERY_NAMES).forEach(function (key) {
      var value = params.get(QUERY_NAMES[key]);
      if (value === null) return;
      raw[key] = ARRAY_KEYS.indexOf(key) !== -1 ? value.split(',').filter(Boolean) : value;
    });
    return normalizeState(raw);
  }

  return {
    NO_COUNTRY: NO_COUNTRY, FOREIGN: FOREIGN, QUERY_FIELDS: QUERY_FIELDS, QUERY_FIELD_LABELS: QUERY_FIELD_LABELS,
    PRESETS: PRESETS, emptyState: emptyState, normalizeState: normalizeState, clearPart: clearPart,
    activeParts: activeParts, localDay: localDay, shiftDay: shiftDay, createContext: createContext,
    nextSort: nextSort, sortKeyOf: sortKeyOf, presetState: presetState, presetKeyFor: presetKeyFor,
    describe: describe, toQuery: toQuery, fromQuery: fromQuery
  };
});
