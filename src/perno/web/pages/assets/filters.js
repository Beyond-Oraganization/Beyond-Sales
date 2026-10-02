// Filtry, wyszukiwarka, sortowanie i szybkie widoki listy firm — sama logika, bez DOM.
// Ten plik ładuje strona (jako globalny PernoFilters) i Node (testy: tests/js/filters.test.js).
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.PernoFilters = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var NO_COUNTRY = '_none';      // firmy bez kraju
  var FOREIGN = '_foreign';      // firmy z krajem innym niż Polska
  var HOME = 'PL';
  var TRI = ['', 'yes', 'no'];
  var ORIGINS = ['', 'dossier', 'import'];
  var QUERY_FIELDS = ['all', 'company', 'email', 'website', 'place', 'description'];
  var QUERY_FIELD_LABELS = {all: 'wszędzie', company: 'firma', email: 'e-mail', website: 'strona', place: 'kraj i adres', description: 'opis'};
  var ARRAY_KEYS = ['statuses', 'segments', 'countries'];
  var IMPORT_ID = /-n\d+$/;      // id firm z importu (`eventy-n001`); firmy z dossier mają `eventy-051`

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
  // przepuszczamy tylko znane pola o poprawnym typie, reszta wraca do wartości domyślnych.
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

  function hasFilters(state) { return activeParts(state).length > 0; }

  // ---- tekst: składanie liter, zapytanie ---------------------------------------------------

  var EXTRA_FOLDS = {'ł': 'l', 'ø': 'o', 'ß': 'ss', 'æ': 'ae', 'œ': 'oe', 'đ': 'd', 'ð': 'd', 'þ': 'th', 'ı': 'i'};

  // Małe litery, bez znaków diakrytycznych (ą→a, ł→l, ü→u, ß→ss…), pojedyncze spacje.
  function fold(text) {
    var s = String(text == null ? '' : text).toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
    s = s.replace(/[łøßæœđðþı]/g, function (ch) { return EXTRA_FOLDS[ch]; });
    return s.replace(/\s+/g, ' ').trim();
  }

  // „ala ma” kota -pies -"stary kot" → include: [ala ma, kota], exclude: [pies, stary kot].
  // Niedomknięty cudzysłów obejmuje resztę tekstu; sam „-” jest ignorowany.
  function parseQuery(text) {
    var include = [], exclude = [];
    var re = /(-?)"([^"]*)"?|(-?)([^\s"]+)/g, m;
    var source = String(text == null ? '' : text);
    while ((m = re.exec(source)) !== null) {
      var negative = (m[2] !== undefined ? m[1] : m[3]) === '-';
      var term = fold(m[2] !== undefined ? m[2] : m[4]);
      if (!term || term === '-') continue;
      (negative ? exclude : include).push(term);
    }
    return {include: include, exclude: exclude};
  }

  // ---- kontekst i pola wiersza -------------------------------------------------------------

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

  function day(value) { return value ? String(value).slice(0, 10) : ''; }
  function hasEmail(row) { return !!(row.email && String(row.email).trim()); }
  function hasBody(row) { return row.has_body !== undefined ? !!row.has_body : !!row.body_html; }
  function isImport(row) { return IMPORT_ID.test(row.id || ''); }
  function isReady(row, ctx) { return ctx.sendable.indexOf(row.status) !== -1 && hasEmail(row) && hasBody(row); }

  var haystackCache = typeof WeakMap === 'function' ? new WeakMap() : null;

  function haystacks(row, ctx) {
    var cached = haystackCache && haystackCache.get(row);
    if (cached && cached.names === ctx.countryNames) return cached.value;
    var country = row.country || '';
    var place = [country, ctx.countryNames[country] || '', row.city, row.address].join(' ');
    var company = [row.company, row.contact_person].join(' ');
    var value = {
      company: fold(company), email: fold(row.email), website: fold(row.website),
      place: fold(place), description: fold(row.description)
    };
    value.all = fold([row.id, company, row.email, row.website, place, row.description].join(' '));
    if (haystackCache) haystackCache.set(row, {names: ctx.countryNames, value: value});
    return value;
  }

  function tri(mode, present) { return mode === '' || (mode === 'yes') === present; }

  function inRange(value, from, to) {
    if (!from && !to) return true;
    var d = day(value);
    if (!d) return false;  // wiersz bez daty nigdy nie mieści się w zakresie (tak samo jak NULL w SQL)
    return (!from || d >= from) && (!to || d <= to);
  }

  function matchesCountry(row, selected) {
    var code = row.country || '';
    return selected.some(function (item) {
      if (item === NO_COUNTRY) return code === '';
      if (item === FOREIGN) return code !== '' && code !== HOME;
      return code === item;
    });
  }

  function matches(row, state, ctx, parsed) {
    if (state.statuses.length && state.statuses.indexOf(row.status) === -1) return false;
    if (state.segments.length && state.segments.indexOf(row.segment) === -1) return false;
    if (state.countries.length && !matchesCountry(row, state.countries)) return false;
    if (!tri(state.email, hasEmail(row))) return false;
    if (!tri(state.body, hasBody(row))) return false;
    if (!tri(state.city, !!(row.city && String(row.city).trim()))) return false;
    if (!tri(state.address, !!(row.address && String(row.address).trim()))) return false;
    if (!tri(state.sent, !!row.sent_at)) return false;
    if (state.origin && (state.origin === 'import') !== isImport(row)) return false;
    if (!inRange(row.sent_at, state.sentFrom, state.sentTo)) return false;
    if (!inRange(row.generated_at, state.generatedFrom, state.generatedTo)) return false;
    if (parsed.include.length || parsed.exclude.length) {
      var text = haystacks(row, ctx)[state.qFields];
      for (var i = 0; i < parsed.include.length; i++) if (text.indexOf(parsed.include[i]) === -1) return false;
      for (var j = 0; j < parsed.exclude.length; j++) if (text.indexOf(parsed.exclude[j]) !== -1) return false;
    }
    return true;
  }

  // Wiersze pasujące do filtra, w kolejności wejściowej.
  function filterRows(rows, state, ctx) {
    var s = normalizeState(state), parsed = parseQuery(s.q);
    return rows.filter(function (row) { return matches(row, s, ctx, parsed); });
  }

  // ---- sortowanie --------------------------------------------------------------------------

  var collator = typeof Intl === 'object' && Intl.Collator ? new Intl.Collator('pl', {sensitivity: 'base', numeric: true}) : null;
  function compareText(a, b) { return collator ? collator.compare(a, b) : (a < b ? -1 : a > b ? 1 : 0); }

  var SORT_KEYS = {  // klucz → [wartość do porównania, rodzaj]
    company: function (r) { return [r.company || '', 'text']; },
    city: function (r) { return [r.city || '', 'text']; },
    email: function (r) { return [r.email || '', 'text']; },
    segment: function (r, ctx) {
      var i = ctx.segmentOrder.indexOf(r.segment);
      return [i === -1 ? 999 : i, 'number', r.segment];
    },
    status: function (r, ctx) {
      var i = ctx.statusOrder.indexOf(r.status);
      return [i === -1 ? 999 : i, 'number'];
    },
    country: function (r, ctx) { return [r.country ? (ctx.countryNames[r.country] || r.country) : '', 'text']; },
    generated_at: function (r) { return [r.generated_at || '', 'iso']; },
    sent_at: function (r) { return [r.sent_at || '', 'iso']; }
  };

  function sortKeyOf(sort) { return String(sort || '').replace(/^-/, ''); }

  // 'default' zostawia kolejność serwera; 'klucz' rosnąco, '-klucz' malejąco; puste wartości zawsze na końcu.
  function sortRows(rows, sort, ctx) {
    var key = sortKeyOf(sort), pick = SORT_KEYS[key];
    if (!pick) return rows.slice();
    var dir = String(sort).charAt(0) === '-' ? -1 : 1;
    var decorated = rows.map(function (row, index) { return {row: row, index: index, v: pick(row, ctx)}; });
    decorated.sort(function (a, b) {
      var ea = a.v[0] === '', eb = b.v[0] === '';
      if (ea || eb) return ea && eb ? a.index - b.index : (ea ? 1 : -1);
      var c;
      if (a.v[1] === 'text') c = compareText(a.v[0], b.v[0]);
      else if (a.v[1] === 'iso') c = a.v[0] < b.v[0] ? -1 : a.v[0] > b.v[0] ? 1 : 0;
      else c = a.v[0] - b.v[0] || (a.v[2] && b.v[2] ? compareText(a.v[2], b.v[2]) : 0);
      return c ? c * dir : a.index - b.index;
    });
    return decorated.map(function (d) { return d.row; });
  }

  // Kolejne kliknięcia nagłówka: brak → rosnąco → malejąco → domyślnie.
  function nextSort(current, key) {
    if (sortKeyOf(current) !== key) return key;
    return current === key ? '-' + key : 'default';
  }

  // ---- liczniki ----------------------------------------------------------------------------

  var FACET_VALUE = {
    statuses: function (row) { return row.status; },
    segments: function (row) { return row.segment; },
    countries: function (row) { return row.country || NO_COUNTRY; }
  };

  // Ile wierszy ma każdą wartość pola (status/segment/kraj) przy pozostałych filtrach — ale bez filtra tego pola,
  // żeby licznik przy nieaktywnym chipie mówił, ile wierszy doszłoby po jego kliknięciu.
  function facetCounts(rows, state, ctx, facet) {
    var s = normalizeState(state);
    s[facet] = [];
    var counts = {}, foreign = 0;
    filterRows(rows, s, ctx).forEach(function (row) {
      var value = FACET_VALUE[facet](row);
      counts[value] = (counts[value] || 0) + 1;
      if (facet === 'countries' && row.country && row.country !== HOME) foreign++;
    });
    if (facet === 'countries') counts[FOREIGN] = foreign;
    return counts;
  }

  // ---- szybkie widoki ----------------------------------------------------------------------

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

  function presetCounts(rows, ctx) {
    return PRESETS.map(function (p) {
      return {key: p.key, label: p.label, hint: p.hint || '', count: filterRows(rows, presetState(p.key, ctx), ctx).length};
    });
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
    activeParts: activeParts, hasFilters: hasFilters, fold: fold, parseQuery: parseQuery, localDay: localDay,
    shiftDay: shiftDay, createContext: createContext, hasEmail: hasEmail, hasBody: hasBody, isImport: isImport,
    isReady: isReady, filterRows: filterRows, sortRows: sortRows, nextSort: nextSort, sortKeyOf: sortKeyOf,
    facetCounts: facetCounts, presetState: presetState, presetKeyFor: presetKeyFor, presetCounts: presetCounts,
    describe: describe, toQuery: toQuery, fromQuery: fromQuery
  };
});
