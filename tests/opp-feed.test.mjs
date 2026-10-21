import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
globalThis.window = {};
new Function(readFileSync(new URL("../noema/static/opp-feed.js", import.meta.url), "utf8"))();
const O = window.TEV_OPP;
test("edge and move formatting are honest", () => {
  assert.equal(O.fmtEdge({ edge: null }), "unmeasured");
  assert.equal(O.fmtEdge({ edge: 0.034 }), "+3.4%");
  assert.equal(O.fmtMove({ move_since_found: null }), "—");
  assert.equal(O.fmtMove({ move_since_found: 0.02, move_unit: "pts" }), "+2.0¢");
  assert.equal(O.fmtMove({ move_since_found: -0.1, move_unit: "pct" }), "-10.0%");
});
test("strand health from real fetch status and age", () => {
  const now = Date.parse("2026-10-21T20:00:00Z");
  assert.equal(O.health(null, now), "none");
  assert.equal(O.health({ at: "2026-10-21T19:59:00Z", status: "200" }, now), "ok");
  assert.equal(O.health({ at: "2026-10-21T18:00:00Z", status: "ok" }, now), "stale");
  assert.equal(O.health({ at: "2026-10-21T19:59:00Z", status: "degraded: timeout" }, now), "bad");
});
test("spider only crawls when a source's newest fetch time advances", () => {
  const prev = { news: Date.parse("2026-10-21T19:00:00Z"), x: Date.parse("2026-10-21T19:00:00Z") };
  const latest = { news: { at: "2026-10-21T19:05:00Z" }, x: { at: "2026-10-21T19:00:00Z" }, kalshi: { at: "2026-10-21T19:05:00Z" } };
  assert.deepEqual(O.newFetches(prev, latest), ["news"]); // kalshi has no baseline yet: not a new event
});
