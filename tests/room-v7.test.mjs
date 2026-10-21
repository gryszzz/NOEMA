import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function load() {
  const noop = () => {};
  const document = { readyState: 'complete', hidden: false, getElementById: () => null, querySelector: () => null,
    querySelectorAll: () => [], addEventListener: noop, body: { insertAdjacentHTML: noop } };
  const ctx = { document, localStorage: {}, setInterval: () => 0, clearInterval: noop, setTimeout: () => 0,
    requestAnimationFrame: () => 0, fetch: async () => ({ ok: false }), console, addEventListener: noop };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(readFileSync(new URL('../noema/static/room-v7.js', import.meta.url), 'utf8'), ctx);
  return ctx.TEV_V7;
}
const V = load();
const states = (it) => V.checksFor(it).map((c) => c.state).join('');

test('filter checks follow the real stage and reject reason', () => {
  assert.equal(states({ stage: 'rejected', reason: 'liquidity below desk floor' }), 'fsssss');
  assert.equal(states({ stage: 'rejected', reason: 'pool younger than minimum age' }), 'pfssss');
  assert.equal(states({ stage: 'rejected', reason: 'mid-spike: price accelerating' }), 'ppfsss');
  assert.equal(states({ stage: 'rejected', reason: 'top-10 holders above concentration limit' }), 'pppfss');
  assert.equal(states({ stage: 'filters', reason: 'passed filters; queued for holder check' }), 'pppsss');
  assert.equal(states({ stage: 'holders' }), 'ppppss');
  assert.equal(states({ stage: 'chief_veto' }), 'pppppf');
  assert.equal(states({ stage: 'entered' }), 'pppppp');
});

test('prediction board hides sports and near-settled markets and ranks by edge', () => {
  const rows = [
    { venue: 'kalshi', market_id: 'A', title: 'Warriors vs Lakers', market_p: 0.5, edge: 0.3 },
    { venue: 'kalshi', market_id: 'B', title: 'Fed cuts in December?', market_p: 0.97, edge: 0.01 },
    { venue: 'kalshi', market_id: 'C', title: 'CPI above 3%?', market_p: 0.4, edge: 0.02 },
    { venue: 'kalshi', market_id: 'D', title: 'Shutdown by Nov 1?', market_p: 0.3, edge: -0.08 },
    { venue: 'kalshi', market_id: 'D', title: 'Shutdown by Nov 1?', market_p: 0.3, edge: -0.08 },
  ];
  assert.deepEqual(V.rankMarkets(rows, false).map((r) => r.market_id), ['D', 'C']);
  assert.equal(V.rankMarkets(rows, true).length, 4);
});

test('rulebook boilerplate target queries are hidden', () => {
  assert.equal(V.isBoilerplateQuery('S&P "Kalshi Rulebook"'), true);
  assert.equal(V.isBoilerplateQuery('"average gas prices" AAA'), false);
});

test('one-page layout engages on MacBook-sized screens only', () => {
  const noop = () => {};
  const ctx = { document: { readyState: 'complete', body: { classList: { toggle: noop } }, addEventListener: noop },
    setTimeout: () => 0, clearTimeout: noop, addEventListener: noop, dispatchEvent: noop, Event: class {},
    innerWidth: 1512, innerHeight: 982 };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(readFileSync(new URL('../noema/static/room-v7-onepage.js', import.meta.url), 'utf8'), ctx);
  const O = ctx.TEV_V7_ONEPAGE;
  assert.equal(O.isLaptop(1512, 982), true);
  assert.equal(O.isLaptop(1440, 900), true);
  assert.equal(O.isLaptop(2560, 1440), false);
});

test('candidate rows say exactly which filter cut a pool or what the judge said', () => {
  const cut = V.verdict({ stage: 'rejected', reason: 'liquidity below desk floor' });
  assert.equal(cut.out, 'CUT'); assert.equal(cut.txt, 'liquidity below desk floor');
  const q = V.verdict({ stage: 'filters', reason: 'passed filters; queued for holder check' });
  assert.equal(q.out, '→ HOLDERS');
  const j = V.verdict({ stage: 'rejected', reason: 'model pass p=0.38', probability: 0.38, confidence: 0.6 });
  assert.equal(j.out, 'JUDGE SKIP'); assert.match(j.txt, /judge p 0.38/);
  const rows = V.candRows([{ symbol: 'A', stage: 'rejected', reason: 'x', liquidity_usd: 9e6 },
    { symbol: 'B', stage: 'rejected', reason: 'model pass p=0.5', probability: 0.5 },
    { symbol: 'C', stage: 'filters', reason: 'passed filters', liquidity_usd: 1 }]);
  assert.equal(rows.map((r) => r.symbol).join(''), 'BCA');
  assert.equal(V.candRows(undefined).length, 0);
});

test('pools without candles chart their real metrics', () => {
  const rows = V.metricRows({ liquidity_usd: 25000, volume_h1_usd: 332, price_change_m5: -1.2, pool_age_hours: 338 });
  assert.equal(rows.map((r) => r[0]).join(','), 'liq,vol1h,5m %,age h');
  assert.equal(V.metricRows({}).length, 0);
});
