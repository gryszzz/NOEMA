import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
globalThis.window = {};
new Function(readFileSync(new URL("../noema/static/room-roam.js", import.meta.url), "utf8"))();
const R = window.TEV_ROAM;
test("trips only for sources whose newest fetch advanced", () => {
  const prev = { news: Date.parse("2026-10-21T19:00:00Z"), x: Date.parse("2026-10-21T19:00:00Z") };
  const latest = { news: { at: "2026-10-21T19:05:00Z", source: "fed" }, x: { at: "2026-10-21T19:00:00Z" }, kalshi: { at: "2026-10-21T19:05:00Z" } };
  assert.deepEqual(R.newEvents(prev, latest).map(e => e.kind), ["news"]);
});
test("new opportunities only after a baseline", () => {
  assert.deepEqual(R.newOpps(null, [{ id: "a" }]), []);
  assert.equal(R.newOpps(new Set(["a"]), [{ id: "a" }, { id: "b", title: "B" }]).length, 1);
});
test("landing clamps into the viewport and flags off-screen panels", () => {
  assert.equal(R.land({ left: 100, top: 100, width: 300, height: 200 }, 1512, 945).off, null);
  const l = R.land({ left: 100, top: 2000, width: 300, height: 200 }, 1512, 945);
  assert.equal(l.off, "down"); assert.ok(l.y <= 945);
});
test("step moves at most speed*dt and arrives", () => {
  const s = R.step({ x: 0, y: 0 }, { x: 100, y: 0 }, 0.1, 400);
  assert.equal(s.x, 40); assert.equal(s.done, false);
  assert.equal(R.step({ x: 99, y: 0 }, { x: 100, y: 0 }, 0.1, 400).done, true);
});

test("tetrapod gait: diagonal groups alternate, stance feet never lift", () => {
  assert.equal(R.group(0, -1), R.group(1, 1)); assert.notEqual(R.group(0, -1), R.group(0, 1));
  for (let g = 0; g < 1; g += 0.05) for (let j = 0; j < 4; j++) {
    const a = R.legPhase(g, j, -1), b = R.legPhase(g, j, 1);
    assert.notEqual(a.swing, b.swing); if (!a.swing) assert.equal(a.lift, 0);
  }
});
test("leg IK keeps segment lengths and knees bulge outward", () => {
  for (const side of [-1, 1]) for (let j = 0; j < 4; j++) {
    const P = R.legPose({ gait: 0.3, stride: 1 }, j, side, 0);
    assert.ok(Math.abs(Math.hypot(P.knee.x - P.hip.x, P.knee.y - P.hip.y) - R.L1[j]) < 0.6);
    assert.ok(P.knee.x * side > P.hip.x * side);
  }
});
test("legs at rest with no stride stay planted (no gait offset)", () => {
  const a = R.legPose({ gait: 0.1, stride: 0 }, 1, 1, 0), b = R.legPose({ gait: 0.7, stride: 0 }, 1, 1, 0);
  assert.ok(Math.abs(a.foot.y - b.foot.y) < 1e-9);
});

test('crawl-live maps hosts to sources and builds an honest caption', () => {
  const src = readFileSync(new URL('../noema/static/room-roam.js', import.meta.url), 'utf8');
  const ctx = { window: {} }; ctx.globalThis = ctx;
  (new Function('window', 'document', src))(ctx.window, undefined);
  const R = ctx.window.TEV_ROAM;
  assert.equal(R.kindOf({ host: 'api.geckoterminal.com' }), 'geckoterminal');
  assert.equal(R.kindOf({ host: 'feeds.reuters.com', crawler: 'news' }), 'news');
  assert.equal(R.kindOf({ host: 'api.elections.kalshi.com' }), 'kalshi');
  const now = Date.parse('2026-10-22T01:00:10Z');
  const s = R.liveState({ entries: [
    { id: 1, host: 'a', started_at: '2026-10-22T01:00:08Z', status: 'in_flight' },
    { id: 2, host: 'b', started_at: '2026-10-22T01:00:05Z', duration_ms: 3000, status: '200', items: 12 },
    { id: 3, host: 'c', started_at: '2026-10-22T01:00:06Z', duration_ms: 2000, status: '429' },
    { id: 4, host: 'd', started_at: '2026-10-22T00:50:00Z', status: 'in_flight' }] }, now);
  assert.equal(s.inflight.length, 1); // stale in-flight rows are not shown as live
  assert.equal(s.caption, '1 fetch in flight · 12 items/min');
  assert.equal(s.done.find(d => d.id === 3).rl, true);
  assert.equal(R.liveState({ entries: [] }, now).caption, 'idle · legs resting');
});

test('crawl-live retry backoff is gentle and capped at 10s', () => {
  const src = readFileSync(new URL('../noema/static/room-roam.js', import.meta.url), 'utf8');
  const ctx = { window: {} };
  (new Function('window', 'document', src))(ctx.window, undefined);
  const R = ctx.window.TEV_ROAM;
  assert.equal(R.backoff(0), 0);
  assert.equal(R.backoff(1), 2000);
  assert.equal(R.backoff(2), 3000);
  assert.equal(R.backoff(3), 4500);
  assert.equal(R.backoff(20), 10000);
  assert.doesNotMatch(src, /Math\.random\(\)>\.05/);
});
