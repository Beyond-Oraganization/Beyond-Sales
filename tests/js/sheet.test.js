'use strict';
// Uruchamiane przez `node --test` (z tests/test_js.py). Arkusz (showSheet) i toast (showToast) zastępują alert()/confirm():
// w komunikatach lądują nazwy firm ze skanowanych stron, więc treść ma trafiać do DOM wyłącznie jako tekst, a wynik
// (potwierdzenie, anulowanie, Esc, kliknięcie w tło) ma dochodzić do wywołującego jako Promise<boolean>.
const test = require('node:test');
const assert = require('node:assert/strict');
const {read, EVIL, vm} = require('./support/dom.js');

// Mała atrapa DOM z prawdziwym drzewem i zdarzeniami; każde użycie innerHTML kończy test błędem.
class Node {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.className = '';
    this.textContent = ''; this.returnValue = ''; this.parent = null; this.focused = false; this.classes = new Set();
    this.classList = {add: (name) => this.classes.add(name)};
  }
  set innerHTML(_value) { throw new Error('innerHTML jest zabronione w arkuszu i toaście'); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type, event) { (this.listeners[type] || []).forEach((fn) => fn(event || {target: this})); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.removed = true; }
  focus() { this.focused = true; }
  showModal() { this.opened = true; }
  close(value) { this.returnValue = value === undefined ? this.returnValue : value; this.fire('close'); }
  find(predicate) {
    if (predicate(this)) return this;
    for (const child of this.children) { const hit = child.find(predicate); if (hit) return hit; }
    return null;
  }
  all(predicate, acc = []) { if (predicate(this)) acc.push(this); this.children.forEach((c) => c.all(predicate, acc)); return acc; }
}

function load() {
  const body = new Node('body');
  const timers = [];
  const document = {
    body, createElement: (tag) => new Node(tag),
    getElementById: (id) => body.find((n) => n.id === id),
  };
  const context = vm.createContext({
    document, Promise, String, JSON, console,
    setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; }, clearTimeout() {},
  });
  vm.runInContext(read('common.js'), context);
  return {context, body, timers};
}

const buttons = (root) => root.all((n) => n.tag === 'button');
const dialogOf = (body) => body.find((n) => n.tag === 'dialog');

test('treść arkusza trafia do DOM tylko jako tekst, akapity po pustej linii', async () => {
  const {context, body} = load();
  const pending = vm.runInContext('showSheet', context)({title: EVIL.script, message: EVIL.img + '\n\n' + EVIL.attr});
  const dialog = dialogOf(body);
  assert.ok(dialog.opened, 'okno otwarte przez showModal');
  const title = dialog.find((n) => n.tag === 'h2');
  const paragraphs = dialog.all((n) => n.tag === 'p').map((p) => p.textContent);
  assert.equal(title.textContent, EVIL.script);
  assert.deepEqual(paragraphs, [EVIL.img, EVIL.attr]);
  dialog.close('cancel');
  assert.equal(await pending, false);
});

test('potwierdzenie daje true, anulowanie, Esc i kliknięcie w tło dają false, a okno znika z DOM', async () => {
  const {context, body} = load();
  const show = vm.runInContext('showSheet', context);

  let pending = show({message: 'x', confirmLabel: 'Wyślij 3'});
  let dialog = dialogOf(body);
  const [cancel, ok] = buttons(dialog);
  assert.deepEqual([cancel.textContent, ok.textContent], ['Anuluj', 'Wyślij 3']);
  assert.deepEqual([cancel.attributes.value, ok.attributes.value], ['cancel', 'ok']);
  dialog.close(ok.attributes.value);
  assert.equal(await pending, true);
  assert.ok(dialog.removed);

  pending = show({message: 'x'});
  dialog = dialogOf(body);
  dialog.close('cancel');
  assert.equal(await pending, false);

  pending = show({message: 'x'});
  dialog = dialogOf(body);
  dialog.returnValue = '';           // Esc zamyka okno bez wartości
  dialog.fire('close');
  assert.equal(await pending, false);

  pending = show({message: 'x'});
  dialog = dialogOf(body);
  dialog.fire('click', {target: dialog});   // kliknięcie w przysłonę zamyka okno jak „Anuluj”
  assert.equal(dialog.returnValue, 'cancel');
  assert.equal(await pending, false);

  pending = show({message: 'x'});
  dialog = dialogOf(body);
  dialog.fire('click', {target: buttons(dialog)[1]});   // kliknięcie w treść okna nie zamyka go
  assert.ok(!dialog.removed);
  dialog.close('cancel');
  await pending;
});

test('samo „OK” (alert), fokus na „Anuluj” przy tone=danger i focusCancel', async () => {
  const {context, body} = load();
  const show = vm.runInContext('showSheet', context);

  let pending = show({message: 'Brak hasła', cancelLabel: null});
  let dialog = dialogOf(body);
  assert.equal(buttons(dialog).length, 1);
  assert.ok(buttons(dialog)[0].focused);
  dialog.close('ok');
  assert.equal(await pending, true);

  pending = show({message: 'x', tone: 'danger', confirmLabel: 'Usuń'});
  dialog = dialogOf(body);
  const [cancel, ok] = buttons(dialog);
  assert.ok(cancel.focused && !ok.focused, 'przy usuwaniu domyślny fokus to „Anuluj”');
  assert.ok(ok.className.includes('danger'));
  dialog.close('cancel');
  await pending;

  pending = show({message: 'x', focusCancel: true});
  dialog = dialogOf(body);
  assert.ok(buttons(dialog)[0].focused);
  dialog.close('cancel');
  await pending;
});

test('toast: tekst bez znaczników, rola zależy od tonu, puste komunikaty są pomijane, kliknięcie zamyka', () => {
  const {context, body, timers} = load();
  const toast = vm.runInContext('showToast', context);

  assert.equal(toast(''), null);
  assert.equal(toast(null), null);

  const item = toast(EVIL.script, 'error');
  assert.equal(item.textContent, EVIL.script);
  assert.equal(item.attributes.role, 'alert');
  assert.ok(item.className.includes('error'));
  assert.equal(timers[timers.length - 1].ms, 10000, 'błąd zostaje dłużej');

  const calm = toast('Zapisano.', 'ok');
  assert.equal(calm.attributes.role, 'status');
  assert.equal(timers[timers.length - 1].ms, 5000);

  const host = body.find((n) => n.id === 'toastHost');
  assert.equal(host.children.length, 2, 'oba toasty w jednym kontenerze');
  item.fire('click');
  assert.ok(item.classes.has('is-leaving'));
  timers.filter((t) => t.ms === 200).forEach((t) => t.fn());
  assert.equal(host.children.length, 1);
});
