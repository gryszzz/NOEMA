import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
const src = readFileSync(new URL('../noema/static/room-v7-x.js', import.meta.url), 'utf8');
const ctx = { window: {} };
vm.runInNewContext(src, ctx);
const T = ctx.window.TEV_V7_X.T;
test('duplicates and retweets collapse into one row', () => {
  const g = T.dedupe([{ id: 1, author: 'a', text: 'BTC breaking out https://t.co/x' }, { id: 2, author: 'b', text: 'RT @a: BTC breaking out https://t.co/y' }, { id: 3, author: 'c', text: 'other' }]);
  assert.equal(g.length, 2);
  assert.equal(g[0].dups, 1);
});
test('freshness line counts to the next 30-minute pull', () => {
  const now = Date.parse('2026-10-22T01:04:00Z');
  assert.equal(T.fresh('2026-10-22T01:00:00Z', now), 'X pull 4m ago · next ~26m');
  assert.match(T.fresh(null), /no run/);
});
test('no radar and text is never innerHTML', () => {
  assert.doesNotMatch(src, /innerHTML|RADAR|radar\(/);
});
test('posts group under a matching narrative', () => {
  const N = [{ label: 'vault / eth / ethereum', tokens: [] }];
  assert.equal(T.groupOf({ text: 'New ETH vault on Ethereum', stances: [] }, N), 'vault / eth / ethereum');
  assert.equal(T.groupOf({ text: 'hi', stances: [{ title: '$SOL' }] }, N), '$SOL');
});
test('legacy X effect line charts stay hidden in the one-page layout', () => {
  const css = readFileSync(new URL('../noema/static/room-v7-x.css', import.meta.url), 'utf8');
  assert.match(css, /body\.v7-one #ch-wires \.xw\.xe7-on \.xfx\{display:none!important\}/);
});
