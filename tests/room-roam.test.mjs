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
