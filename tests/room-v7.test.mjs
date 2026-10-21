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

test('laptop dock engages on MacBook-sized screens only', () => {
  const noop = () => {};
  const ctx = { document: { readyState: 'complete', querySelector: () => null, querySelectorAll: () => [],
    getElementById: () => null, addEventListener: noop }, localStorage: {}, setTimeout: () => 0,
    clearTimeout: noop, addEventListener: noop, innerWidth: 1512, innerHeight: 982 };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(readFileSync(new URL('../noema/static/room-v7-dock.js', import.meta.url), 'utf8'), ctx);
  const D = ctx.TEV_V7_DOCK;
  assert.equal(D.isLaptop(1512, 982), true);
  assert.equal(D.isLaptop(1728, 1117), true);
  assert.equal(D.isLaptop(2560, 1440), false);
});
