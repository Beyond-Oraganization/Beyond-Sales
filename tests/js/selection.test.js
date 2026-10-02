'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const S = require('../../lib/pages/assets/selection.js');

const ORDER = ['a', 'b', 'c', 'd', 'e', 'f'];
const sorted = (sel) => sel.toArray().sort();

test('toggle: przełącza, zwraca nowy stan i obsługuje stan jawny', () => {
  const sel = S.create();
  assert.equal(sel.toggle('a'), true);
  assert.equal(sel.has('a'), true);
  assert.equal(sel.toggle('a'), false);
  assert.equal(sel.has('a'), false);
  assert.equal(sel.toggle('b', true), true);
  assert.equal(sel.toggle('b', true), true);   // jawne „zaznacz” nie odznacza
  assert.equal(sel.size, 1);
});

test('range (Shift+klik): od ostatnio klikniętego do wskazanego, w obie strony', () => {
  const sel = S.create();
  sel.toggle('b');
  sel.range(ORDER, 'e', true);
  assert.deepEqual(sorted(sel), ['b', 'c', 'd', 'e']);
  sel.toggle('f');
  sel.range(ORDER, 'd', true);                 // wstecz: f → d
  assert.deepEqual(sorted(sel), ['b', 'c', 'd', 'e', 'f']);
  sel.range(ORDER, 'b', false);                // odznaczanie zakresu: początek = d (ostatni klik)
  assert.deepEqual(sorted(sel), ['e', 'f']);
});

test('range bez początku albo gdy początek zniknął z widoku działa jak zwykłe kliknięcie', () => {
  const sel = S.create();
  assert.equal(sel.range(ORDER, 'c', true), true);
  assert.deepEqual(sorted(sel), ['c']);
  sel.toggle('x');                             // „x” nie ma w widoku
  sel.range(ORDER, 'e', true);
  assert.deepEqual(sorted(sel), ['c', 'e', 'x']);
});

test('range nie rusza wierszy spoza zakresu ani spoza widoku', () => {
  const sel = S.create(['z']);
  sel.toggle('b');
  sel.range(ORDER, 'd', true);
  assert.deepEqual(sorted(sel), ['b', 'c', 'd', 'z']);
});

test('add, remove, addFirst, replace, clear', () => {
  const sel = S.create(['a']);
  sel.add(['b', 'c']);
  sel.remove(['a', 'zzz']);
  assert.deepEqual(sorted(sel), ['b', 'c']);
  sel.addFirst(ORDER, 2);
  assert.deepEqual(sorted(sel), ['a', 'b', 'c']);
  sel.addFirst(ORDER, 0);
  sel.addFirst(ORDER, -3);
  assert.equal(sel.size, 3);
  sel.addFirst(ORDER, 99);
  assert.equal(sel.size, 6);
  sel.replace(['q']);
  assert.deepEqual(sel.toArray(), ['q']);
  sel.clear();
  assert.equal(sel.size, 0);
});

test('invert działa tylko w obrębie podanych id', () => {
  const sel = S.create(['a', 'c', 'z']);
  sel.invert(['a', 'b', 'c', 'd']);
  assert.deepEqual(sorted(sel), ['b', 'd', 'z']);   // „z” poza widokiem zostaje
});

test('counts: ile zaznaczonych w widoku, a ile poza nim', () => {
  const sel = S.create(['a', 'b', 'x', 'y', 'z']);
  assert.deepEqual(sel.counts(['a', 'b', 'c']), {total: 5, inView: 2, outside: 3});
  assert.deepEqual(sel.counts([]), {total: 5, inView: 0, outside: 5});
  assert.deepEqual(S.create().counts(ORDER), {total: 0, inView: 0, outside: 0});
});

test('headerState: none / some / all dla bieżącej strony', () => {
  const sel = S.create(['a', 'b']);
  assert.equal(sel.headerState(['a', 'b']), 'all');
  assert.equal(sel.headerState(['a', 'c']), 'some');
  assert.equal(sel.headerState(['c', 'd']), 'none');
  assert.equal(sel.headerState([]), 'none');
});

test('prune wyrzuca id, których już nie ma, i zerwany początek zakresu', () => {
  const sel = S.create();
  sel.toggle('a');
  sel.toggle('b');
  sel.prune(['a', 'c']);
  assert.deepEqual(sel.toArray(), ['a']);
  sel.range(ORDER, 'd', true);                      // początek „b” zniknął → zwykłe kliknięcie
  assert.deepEqual(sorted(sel), ['a', 'd']);
  sel.prune(new Set(['d']));
  assert.deepEqual(sel.toArray(), ['d']);
});

test('ordered: kolejność widoku, potem reszta', () => {
  const sel = S.create(['x', 'c', 'a', 'y']);
  assert.deepEqual(sel.ordered(['c', 'b', 'a', 'c']), ['c', 'a', 'x', 'y']);
  assert.deepEqual(S.create().ordered(ORDER), []);
});

test('toArray zwraca kopię — zmiana wyniku nie rusza zaznaczenia', () => {
  const sel = S.create(['a']);
  sel.toArray().push('b');
  assert.equal(sel.size, 1);
});
