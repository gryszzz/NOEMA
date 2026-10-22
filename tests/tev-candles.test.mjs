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

test('scan lane puts pools with real candles first', () => {
  const v7 = readFileSync(new URL('../noema/static/room-v7.js', import.meta.url), 'utf8');
  assert.match(v7, /T\.chartFirst/);
});

test('candle grid never shows the same find twice at once', () => {
  const src = readFileSync(new URL('../noema/static/room-v7.js', import.meta.url), 'utf8');
  const m = src.match(/T\.nextPick=\(pool,cur,taken,shown\)=>\{[^\n]*\n?/);
  assert.ok(m);
  const nextPick = eval('(' + m[0].slice('T.nextPick='.length).replace(/;\s*$/, '') + ')');
  const pool = [{ key: 'a' }, { key: 'b' }, { key: 'c' }];
  assert.equal(nextPick(pool, 0, ['b']), 2);
  assert.equal(nextPick(pool, -1, []), 0);
  assert.equal(nextPick([], 0, []), -1);
  // newest unseen find wins over cycling; seen ones are skipped
  assert.equal(nextPick(pool, 2, [], new Set(['a', 'c'])), 1);
});

test('side charts cycle only live launch-stream finds, not the stale sweep list', () => {
  const v7 = readFileSync(new URL('../noema/static/room-v7.js', import.meta.url), 'utf8');
  const cp = v7.match(/T\.candlePool=[^\n]*\n[^\n]*\n[^\n]*/)[0];
  assert.match(cp, /TEVLive/);
  assert.doesNotMatch(cp, /funnel/);
});
