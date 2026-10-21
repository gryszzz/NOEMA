import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const ctx = { console, Math, Date, setInterval: () => 0, setTimeout: () => 0 };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(readFileSync(new URL('../noema/static/crawler-hero.js', import.meta.url), 'utf8'), ctx);
const H = ctx.TEV_CRAWLER;
const now = Date.parse('2026-10-21T20:10:00Z');

test('idle state names what it waits for and the next sweep', () => {
  const d = { crawl: { hits: [{ at: '2026-10-21T20:00:00Z', kind: 'news' }],
    next: { news: '2026-10-21T20:09:00Z', trench: '2026-10-21T20:12:00Z' } }, funnel: {} };
  const st = H.state(d, now);
  assert.equal(st.active, false);
  assert.equal(H.idleText(st, now), 'IDLE · last real fetch 10m 0s ago · waiting on news (due now) · next trench sweep in 2m 0s');
});

test('a fetch in the last 90s means the crawler is sweeping', () => {
  const st = H.state({ crawl: { hits: [{ at: '2026-10-21T20:09:30Z', kind: 'x' }], next: {} } }, now);
  assert.equal(st.active, true);
  assert.equal(H.idleText(st, now), null);
});

test('no data is shown as no data, never as activity', () => {
  const st = H.state({}, now);
  assert.equal(st.active, false);
  assert.match(H.idleText(st, now), /no crawler fetch recorded yet/);
});

test('sources map to venue lanes', () => {
  assert.equal(H.kindOf({ kind: 'prediction', source: 'kalshi:production:markets' }), 'kalshi');
  assert.equal(H.kindOf({ kind: 'prediction', url: 'https://gamma-api.polymarket.com/markets' }), 'polymarket');
  assert.equal(H.kindOf({ kind: 'pool', source: 'geckoterminal · BONK' }), 'geckoterminal');
  assert.equal(H.kindOf({ kind: 'news', source: 'bbc' }), 'news');
});
