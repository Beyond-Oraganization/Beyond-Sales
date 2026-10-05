'use strict';
// Wspólne narzędzia testów skryptów strony w Node: atrapa DOM, która zapamiętuje każdy zapisany HTML, wrogie dane i
// sprawdzanie, że żadne z nich nie stało się znacznikiem ani atrybutem. Skrypty strony działają w piaskownicy `vm` bez zmian.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ASSETS = path.join(__dirname, '../../../src/perno/web/pages/assets');
const read = (name) => fs.readFileSync(path.join(ASSETS, name), 'utf8');

const EVIL = {
  script: '<script>window.PWNED=1</script>',
  img: '<img src=x onerror=window.PWNED=1>',
  attr: '" onmouseover="window.PWNED=1" x="',
  js: 'javascript:window.PWNED=1',
};

// Atrapa DOM: każdy element to Proxy, które zapamiętuje HTML zapisany przez innerHTML / insertAdjacentHTML.
// `respond(url)` podaje odpowiedź JSON na każde zapytanie `fetch`; `extra` dokłada globalne obiekty skryptu.
function createPage(respond, extra) {
  const written = [];
  const elements = new Map();
  const makeElement = () => {
    const base = {
      dataset: {}, style: {}, classList: {add() {}, remove() {}, toggle() {}}, value: '', checked: false,
      disabled: false, textContent: '', className: '', children: [],
    };
    return new Proxy(base, {
      get(target, prop) {
        if (prop === 'insertAdjacentHTML') return (_where, html) => { written.push(String(html)); };
        if (prop in target) return target[prop];
        if (typeof prop === 'symbol') return undefined;
        return () => makeElement();  // appendChild(), remove(), addEventListener(), closest() …
      },
      set(target, prop, value) {
        if (prop === 'innerHTML' || prop === 'outerHTML') written.push(String(value));
        target[prop] = value;
        return true;
      },
    });
  };
  const cached = (key) => { if (!elements.has(key)) elements.set(key, makeElement()); return elements.get(key); };
  const document = {
    getElementById: (id) => cached('#' + id),
    querySelector: (selector) => cached(selector),
    createElement: () => makeElement(),
    createDocumentFragment: () => makeElement(),
    addEventListener() {},
    body: makeElement(),
  };
  const sandbox = {
    document, console, URLSearchParams, JSON, Math, Number, String, Set, Map, Object, Array, Promise,
    setTimeout: () => 0, clearTimeout() {}, confirm: () => false, alert() {},
    location: {search: '', pathname: '/'}, history: {replaceState() {}}, localStorage: {getItem: () => null, setItem() {}},
    fetch: async (url) => ({ok: true, status: 200, json: async () => respond(String(url))}),
    ...(extra || {}),
  };
  sandbox.window = sandbox;
  return {written, context: vm.createContext(sandbox)};
}

// Nazwy atrybutów prawdziwych znaczników (z pominięciem tego, co siedzi w cudzysłowach wartości): zeskapowany tekst
// `" onmouseover="…` wewnątrz title="…" nie jest atrybutem, a gdyby ucieczka zawiodła, byłby nim.
const TAG = /<([a-z][a-z0-9-]*)((?:\s+[^\s"'<>\/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*)\s*\/?>/gi;
const ATTRIBUTE = /([^\s"'<>\/=]+)(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?/g;
function attributeNames(html) {
  const names = [];
  for (const tag of html.matchAll(TAG)) {
    for (const attribute of tag[2].matchAll(ATTRIBUTE)) names.push(attribute[1]);
  }
  return names;
}

async function settle() {
  for (let i = 0; i < 30; i++) await new Promise((resolve) => setImmediate(resolve));
}

module.exports = {read, EVIL, createPage, attributeNames, settle, vm};
