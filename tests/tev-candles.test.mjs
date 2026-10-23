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

test('numeric intervals (seconds) are labelled, not shown as bare numbers', () => {
  assert.equal(C.tfLabel(1), '1s');
  assert.equal(C.tfLabel(10), '10s');
  assert.equal(C.tfLabel(300), '5m');
  assert.equal(C.tfLabel(3600), '1h');
  assert.equal(C.tfLabel('15m'), '15m');
  assert.equal(C.tfLabel(null), '?');
});

function fakeCanvas(W, H) {
  const rects = [];
  const ctx = new Proxy({ rects }, {
    get(t, k) {
      if (k === 'rects') return rects;
      if (k === 'fillRect') return (x, y, w, h) => rects.push([x, y, w, h]);
      if (k === 'measureText') return () => ({ width: 10 });
      if (k in t) return t[k];
      return () => {};
    },
    set(t, k, v) { t[k] = v; return true; },
  });
  return { clientWidth: W, clientHeight: H, width: 0, height: 0, style: {}, getContext: () => ctx, addEventListener() {}, ctx };
}

test('slot mode packs few candles at a fixed width, right-aligned (not stretched)', () => {
  const rows = Array.from({ length: 6 }, (_, i) => [i, 1 + i, 2 + i, 0.5 + i, 1.5 + i, 0]);
  const cv = fakeCanvas(1000, 300);
  const st = C.draw(cv, rows, { interval: 1, slot: 10, key: 'k' });
  assert.equal(st.geo.w, 10);
  assert.equal(st.geo.n, 6);
  assert.equal(st.geo.x0 + st.geo.n * st.geo.w, st.geo.cw - 6, 'candles sit at the right edge next to the price tag');
  // without slot the old behaviour stretches across the width
  const st2 = C.draw(fakeCanvas(1000, 300), rows, { interval: 1, key: 'k2' });
  assert.equal(st2.geo.x0, 0);
  assert.ok(st2.geo.w > 100);
});

test('slot mode shrinks then keeps only the newest candles that fit', () => {
  const rows = Array.from({ length: 500 }, (_, i) => [i, 1, 2, 0.5, 1.5, 0]);
  const st = C.draw(fakeCanvas(300, 200), rows, { interval: 1, slot: 10, key: 'k3' });
  assert.equal(st.geo.w, 3);
  assert.ok(st.geo.n < 500 && st.geo.n >= 80);
  assert.equal(st.geo.c[st.geo.n - 1][0], 499);
});

test('fresh launch panels use packed slot candles', () => {
  const lp = readFileSync(new URL('../noema/static/live-panels.js', import.meta.url), 'utf8');
  const v7 = readFileSync(new URL('../noema/static/room-v7.js', import.meta.url), 'utf8');
  assert.match(lp, /slot:\d+/);
  assert.equal((v7.match(/slot:9/g) || []).length, 2);
});
