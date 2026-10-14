'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Strona główna i podstrony z dokumentacją są renderowane na serwerze; skrypt tylko
// kopiuje (polecenia, odwołanie do pliku, bloki kodu) i steruje spisem treści, więc sprawdzamy schowek, komunikaty i ścieżkę awaryjną.
const test = require('node:test');
const assert = require('node:assert/strict');
const {createApp} = require('./support/mini_dom.js');

function boot(files, options) {
  const o = options || {};
  const app = createApp({ids: o.ids || {}, respond: async () => ({})});
  const fallback = [];
  app.eval(`navigator = {clipboard: ${o.clipboard === false ? 'undefined' : '{writeText: async (text) => { if (globalThis.__deny) throw new Error("odmowa"); globalThis.__copied.push(text); }}'}};
    globalThis.__copied = []; globalThis.__deny = false;`);
  app.document.execCommand = (name) => { fallback.push(name); return o.execResult !== false; };
  app.load(files);
  return {app, fallback, written: () => Array.from(app.eval('globalThis.__copied'))};  // tablica z kontekstu vm ma inny prototyp
}

const node = (app, tag, attrs, text, parent) => {
  const element = app.document.createElement(tag);
  Object.entries(attrs || {}).forEach(([name, value]) => element.setAttribute(name, value));
  if (text) element.textContent = text;
  (parent || app.document.body).appendChild(element);
  return element;
};
const toasts = (app) => app.all('.toast').map((n) => n.textContent);

test('strona główna: kliknięcie polecenia kopiuje jego tekst i pokazuje komunikat z przycisku', async () => {
  const {app, written} = boot(['common.js', 'home.js']);
  const button = node(app, 'button', {class: 'hm-cmd-copy', 'data-copy': 'WYSZUKAJ [ilość] firm typu [typ]', 'data-copied': 'Skopiowano polecenie.'});
  const inner = node(app, 'code', {}, 'WYSZUKAJ', button);
  inner.click();  // klik w element wewnątrz przycisku (closest znajduje przycisk)
  await app.settle();
  assert.deepEqual(written(), ['WYSZUKAJ [ilość] firm typu [typ]']);
  assert.deepEqual(toasts(app), ['Skopiowano polecenie.']);
});

test('przycisk bez własnego komunikatu pokazuje „Skopiowano.”', async () => {
  const {app} = boot(['common.js', 'home.js']);
  node(app, 'button', {'data-copy': '@Wysylka/AI_INSTRUCTIONS.md'}).click();
  await app.settle();
  assert.deepEqual(toasts(app), ['Skopiowano.']);
});

test('klik poza przyciskami kopiowania niczego nie kopiuje', async () => {
  const {app, written} = boot(['common.js', 'home.js']);
  node(app, 'a', {href: '/instrukcje'}).click();
  await app.settle();
  assert.deepEqual(written(), []);
  assert.deepEqual(toasts(app), []);
});

test('gdy schowek odmawia, tekst trafia do pola tekstowego i execCommand, a pole znika', async () => {
  const {app, fallback} = boot(['common.js', 'home.js']);
  app.eval('globalThis.__deny = true;');
  node(app, 'button', {'data-copy': 'POKAŻ STAN'}).click();
  await app.settle();
  assert.deepEqual(fallback, ['copy']);
  assert.equal(app.all('textarea').length, 0);
  assert.deepEqual(toasts(app), ['Skopiowano.']);
});

test('gdy nie działa nic, użytkownik dostaje błąd zamiast cichej porażki', async () => {
  const {app} = boot(['common.js', 'home.js'], {clipboard: false, execResult: false});
  node(app, 'button', {'data-copy': 'POKAŻ STAN'}).click();
  await app.settle();
  assert.deepEqual(toasts(app), ['Przeglądarka nie pozwoliła skopiować.']);
  assert.ok(app.first('.toast').classList.contains('error'));
});

test('dokumentacja: skrypt odsłania ukryte przyciski przy blokach kodu', () => {
  const {app} = boot(['common.js']);
  const wrap = node(app, 'div', {class: 'doc-code'});
  const button = node(app, 'button', {'data-copy-code': '', hidden: '', class: 'btn doc-copy'}, 'Kopiuj', wrap);
  assert.equal(button.hasAttribute('hidden'), true);
  app.load(['docs.js']);
  assert.equal(button.hasAttribute('hidden'), false);
});

test('dokumentacja: „Kopiuj” przy bloku kodu kopiuje kod bez końcowego znaku nowej linii', async () => {
  const {app, written} = boot(['common.js', 'docs.js']);
  const wrap = node(app, 'div', {class: 'doc-code'});
  const pre = node(app, 'pre', {}, '', wrap);
  node(app, 'code', {class: 'language-bash'}, 'uv sync\nuv run pytest\n', pre);
  node(app, 'button', {'data-copy-code': '', class: 'btn doc-copy'}, 'Kopiuj', wrap).click();
  await app.settle();
  assert.deepEqual(written(), ['uv sync\nuv run pytest']);
  assert.deepEqual(toasts(app), ['Skopiowano blok kodu.']);
});

test('dokumentacja: przycisk z nazwą pliku kopiuje odwołanie dla Claude Code', async () => {
  const {app, written} = boot(['common.js', 'docs.js']);
  node(app, 'button', {class: 'doc-file', 'data-copy': '@Wysylka/README.md', 'data-copied': 'Skopiowano odwołanie do pliku.'}).click();
  await app.settle();
  assert.deepEqual(written(), ['@Wysylka/README.md']);
  assert.deepEqual(toasts(app), ['Skopiowano odwołanie do pliku.']);
});

test('dokumentacja: spis treści jest rozwinięty na szerokim ekranie, zwinięty na wąskim i reaguje na zmianę szerokości', () => {
  const {app} = boot(['common.js'], {ids: {docToc: {tag: 'details', attrs: {open: ''}}}});
  app.eval('globalThis.__wide = false; matchMedia = () => ({get matches() { return globalThis.__wide; }, addEventListener(type, fn) { globalThis.__onChange = fn; }});');
  app.load(['docs.js']);
  assert.equal(app.el('docToc').open, false);
  app.eval('globalThis.__wide = true; globalThis.__onChange();');
  assert.equal(app.el('docToc').open, true);
});

test('dokumentacja: bez matchMedia spis zostaje taki, jak go wyrenderował serwer', () => {
  const {app} = boot(['common.js'], {ids: {docToc: {tag: 'details', attrs: {open: ''}}}});
  app.load(['docs.js']);
  assert.ok(app.el('docToc').hasAttribute('open'));
});
