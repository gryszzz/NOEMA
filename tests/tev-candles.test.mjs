import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const src = readFileSync(new URL('../noema/static/tev-candles.js', import.meta.url), 'utf8');
const ctx = { window: {} };
vm.runInNewContext(src, ctx);
const C = ctx.window.TEVCandles;

test('interval is inferred from real candle spacing', () => {
  assert.equal(C.inferInterval([[0], [60], [120]]), '1m');
  assert.equal(C.inferInterval([[0], [900], [1800]]), '15m');
  assert.equal(C.inferInterval([[0], [3600], [7200]]), '1h');
  assert.equal(C.inferInterval([[0]]), null);
});

test('clean drops malformed rows and sorts by time', () => {
  const r = C.clean([[2, 1, 2, 0.5, 1.5, 9], [1, 1, 1, 1, 1], ['x'], [3, 'a', 1, 1, 1, 1]]);
  assert.equal(r.length, 2);
  assert.equal(r[0][0], 1);
  assert.equal(r[0][5], 0);
});

test('room v7 renders prices as candles only, slow readable cycle, no toggles', () => {
  const v7 = readFileSync(new URL('../noema/static/room-v7.js', import.meta.url), 'utf8');
  assert.match(v7, /TEVCandles\.draw/);
  assert.match(v7, /every\(scanNext,10000\)/);
  assert.doesNotMatch(v7, /miniChart\(cv,it\.closes\)/);
  const v6 = readFileSync(new URL('../noema/static/trading-room-v6.html', import.meta.url), 'utf8');
  assert.match(v6, /tev-candles\.js/);
  assert.match(v6, /setInterval\(flipNext,10000\)/);
});
