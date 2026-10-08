'use strict';
// Lekka atrapa DOM z prawdziwym drzewem, zdarzeniami (z bąblowaniem) i prostymi selektorami: wystarcza do testowania stron, które budują
// widok przez createElement / textContent. innerHTML jest ZABRONIONE (rzuca błąd), więc test wychwyci każde wstrzyknięcie HTML-a.
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const ASSETS = path.join(__dirname, '../../../src/perno/web/pages/assets');
const read = (name) => fs.readFileSync(path.join(ASSETS, name), 'utf8');

class TextNode {
  constructor(text) { this.tag = '#text'; this.text = String(text); this.parent = null; }
  get textContent() { return this.text; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
}

function parseSimple(selector) {
  // `.a.b`, `tag`, `tag.a`, `[x="y"]`, `.a[x="y"]`, `#id`
  const parts = { tag: '', classes: [], attrs: [], id: '' };
  const re = /^([a-z0-9-]+)|\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/gi;
  let m;
  while ((m = re.exec(selector))) {
    if (m[1]) parts.tag = m[1].toLowerCase();
    else if (m[2]) parts.classes.push(m[2]);
    else if (m[3]) parts.id = m[3];
    else parts.attrs.push([m[4], m[5]]);
  }
  return parts;
}

class El {
  constructor(tag, document) {
    this.tag = String(tag).toLowerCase(); this.document = document; this.children = []; this.attrs = {}; this.listeners = {};
    this.parent = null; this.style = {props: {}, setProperty(name, value) { this.props[name] = value; }};
    this.value = ''; this.checked = false; this.disabled = false; this.dataset = {}; this.opened = false; this.scrollTop = 0;
  }
  get className() { return this.attrs.class || ''; }
  set className(value) { this.attrs.class = String(value); }
  get classList() {
    const self = this;
    const tokens = () => self.className.split(/\s+/).filter(Boolean);
    return {
      add: (name) => { if (!tokens().includes(name)) self.className = [...tokens(), name].join(' '); },
      remove: (name) => { self.className = tokens().filter((t) => t !== name).join(' '); },
      contains: (name) => tokens().includes(name),
      toggle(name, force) {
        const has = tokens().includes(name);
        const want = force === undefined ? !has : Boolean(force);
        if (want && !has) this.add(name); else if (!want && has) this.remove(name);
        return want;
      },
    };
  }
  get id() { return this.attrs.id || ''; }
  set innerHTML(_value) { throw new Error('innerHTML jest zabronione: treść ma trafiać do DOM jako tekst'); }
  get textContent() { return this.children.map((c) => c.textContent).join(''); }
  set textContent(value) { this.children.forEach((c) => { c.parent = null; }); this.children = []; this.appendChild(new TextNode(value)); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  removeAttribute(name) { delete this.attrs[name]; }
  appendChild(child) { if (child.parent) child.remove(); child.parent = this; this.children.push(child); return child; }
  replaceChildren(...nodes) { this.children.forEach((c) => { c.parent = null; }); this.children = []; nodes.forEach((n) => this.appendChild(n)); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  dispatch(type, extra) {
    const event = Object.assign({type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }}, extra || {});
    for (let node = this; node && !event.stopped; node = node.parent || (node.tag === 'html' ? null : null)) (node.listeners[type] || []).forEach((fn) => fn(event));
    if (!event.stopped) this.document.dispatchOnDocument(type, event);
    return event;
  }
  click() { return this.dispatch('click'); }
  focus() { this.document.activeElement = this; }
  scrollIntoView() {}
  showModal() { this.opened = true; this.attrs.open = ''; }
  close(value) { if (value !== undefined) this.returnValue = value; delete this.attrs.open; this.dispatch('close'); }
  get tagName() { return this.tag.toUpperCase(); }
  compound(part) {
    const p = parseSimple(part);
    return (!p.tag || p.tag === this.tag) && (!p.id || p.id === this.id) && p.classes.every((c) => this.classList.contains(c))
      && p.attrs.every(([name, value]) => (value === undefined ? this.hasAttribute(name) : this.getAttribute(name) === value));
  }
  // Selektory złożone: lista po przecinku i relacja potomka (spacja), np. `.rp-rate button`.
  matches(selector) {
    return selector.split(',').some((chain) => {
      const steps = chain.trim().split(/\s+/);
      if (!this.compound(steps[steps.length - 1])) return false;
      let index = steps.length - 2;
      for (let ancestor = this.parent; index >= 0 && ancestor; ancestor = ancestor.parent) {
        if (ancestor.compound && ancestor.compound(steps[index])) index -= 1;
      }
      return index < 0;
    });
  }
  closest(selector) { for (let n = this; n; n = n.parent) if (n.matches && n.matches(selector)) return n; return null; }
  descendants() { const out = []; const walk = (n) => n.children.forEach((c) => { if (c.tag !== '#text') { out.push(c); walk(c); } }); walk(this); return out; }
  querySelectorAll(selector) { return this.descendants().filter((n) => n.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

// Strona z gotowymi elementami o podanych id (to, co normalnie daje HTML strony) i atrapą `fetch`.
function createApp({ids, respond, search = '', hidden = false}) {
  const document = {hidden, title: '', activeElement: null, listeners: {}, byId: {}};
  document.documentElement = new El('html', document);
  document.body = new El('body', document);
  document.documentElement.appendChild(document.body);
  document.createElement = (tag) => new El(tag, document);
  document.createTextNode = (text) => new TextNode(text);
  document.getElementById = (id) => document.byId[id] || document.body.querySelector('#' + id);
  document.querySelector = (selector) => document.body.querySelector(selector);
  document.querySelectorAll = (selector) => document.body.querySelectorAll(selector);
  document.addEventListener = (type, fn) => { (document.listeners[type] = document.listeners[type] || []).push(fn); };
  document.removeEventListener = (type, fn) => { document.listeners[type] = (document.listeners[type] || []).filter((f) => f !== fn); };
  document.dispatchOnDocument = (type, event) => (document.listeners[type] || []).slice().forEach((fn) => fn(event));
  document.fire = (type, extra) => { const event = Object.assign({type, target: document.body, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }}, extra || {}); document.dispatchOnDocument(type, event); return event; };
  for (const [id, spec] of Object.entries(ids)) {
    const node = new El(spec.tag || 'div', document);
    node.attrs.id = id;
    if (spec.attrs) Object.assign(node.attrs, spec.attrs);
    document.byId[id] = node;
    document.body.appendChild(node);
  }
  const calls = [];
  const timers = [];
  const windowListeners = {};
  const location = {search, pathname: '/odpowiedzi'};
  const history = {pushed: [], replaced: [], pushState(state, title, url) { this.pushed.push(url); }, replaceState(state, title, url) { this.replaced.push(url); }};
  const sandbox = {
    document, console, URLSearchParams, JSON,
    URL: class extends URL { static createObjectURL() { return 'blob:fake'; } static revokeObjectURL() {} }, Math, Number, String, Set, Map, Object, Array, Promise, Date, Boolean, Error, encodeURIComponent, isNaN,
    location, history, localStorage: {getItem: () => null, setItem() {}},
    setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; }, clearTimeout(id) { if (timers[id - 1]) timers[id - 1] = {fn() {}, ms: 0}; },
    fetch: async (url, options) => {
      const body = options && options.body ? JSON.parse(options.body) : null;
      calls.push({url: String(url), body, method: (options && options.method) || 'GET'});
      const result = await respond(String(url), body);
      const status = result && result.__status ? result.__status : 200;
      const payload = result && result.__status ? result.body : result;
      return {ok: status < 400, status, json: async () => payload, text: async () => (typeof payload === 'string' ? payload : JSON.stringify(payload)),
        blob: async () => ({size: JSON.stringify(payload).length}), headers: {get: (name) => (/disposition/i.test(name) ? 'attachment; filename="raport_odpowiedzi_20261008.csv"' : 'application/json')}};
    },
    APP: {statusMeta: {odpowiedzial: ['Odpowiedział', '#0e7490'], zainteresowany: ['Zainteresowany', '#be185d'], nie_kontaktowac: ['Nie kontaktować', '#7f1d1d']},
      segmentOrder: ['eventy', 'driverzy'], countries: [['PL', 'Polska', 'PL'], ['DE', 'Niemcy', 'DE']]},
  };
  sandbox.window = Object.assign(sandbox, {addEventListener: (type, fn) => { (windowListeners[type] = windowListeners[type] || []).push(fn); }});
  const context = vm.createContext(sandbox);
  const api = {
    document, context, calls, timers, history, location, windowListeners,
    load(files) { for (const file of files) vm.runInContext(read(file), context, {filename: file}); },
    eval: (code) => vm.runInContext(code, context),
    async settle() { for (let i = 0; i < 40; i++) await new Promise((resolve) => setImmediate(resolve)); },
    async flushTimers() { const pending = timers.splice(0); for (const t of pending) await t.fn(); await api.settle(); },
    el: (id) => document.getElementById(id),
    all: (selector) => document.body.querySelectorAll(selector),
    first: (selector) => document.body.querySelector(selector),
    texts: () => document.body.textContent,
    key(key, extra) { return document.fire('keydown', Object.assign({key, ctrlKey: false, metaKey: false, altKey: false, target: document.body}, extra || {})); },
    callsTo: (url) => calls.filter((c) => c.url === url),
  };
  return api;
}

module.exports = {createApp, El, TextNode, read};
