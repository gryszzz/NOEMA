import test from 'node:test';
import assert from 'node:assert/strict';
import { deriveInhabitantActivity, handoffTraversal, inhabitantGlyph } from '../noema/static/world-inhabitants.mjs';

test('inhabitant activity only upgrades when canonical evidence supports it', () => {
  const now = Date.parse('2026-10-06T05:00:00Z');
  const idle = deriveInhabitantActivity(
    { id: 'agent:alpha', type: 'agent', label: 'alpha', status: 'idle · no open mission' },
    [],
    [],
    now,
  );
  assert.equal(idle.mode, 'unknown');
  assert.equal(idle.label, 'Activity unknown');

  const working = deriveInhabitantActivity(
    { id: 'agent:alpha', type: 'agent', label: 'alpha', status: 'running' },
    [],
    [],
    now,
  );
  assert.equal(working.mode, 'working');

  const degraded = deriveInhabitantActivity(
    { id: 'agent:alpha', type: 'agent', label: 'alpha', status: 'quarantined' },
    [],
    [],
    now,
  );
  assert.equal(degraded.mode, 'degraded');
});

test('recent persisted handoffs produce one-way, non-looping traversal', () => {
  const start = Date.parse('2026-10-06T05:00:00Z');
  const edge = { from: 'agent:a', to: 'agent:b', type: 'handoff · accepted', at: start, missionId: 'm1' };
  assert.equal(handoffTraversal(edge, start + 45_000)?.progress, .5);
  assert.equal(handoffTraversal(edge, start + 90_001), null);
  assert.equal(handoffTraversal({ ...edge, type: 'registered specialist capability' }, start + 20_000), null);

  const receiving = deriveInhabitantActivity(
    { id: 'agent:b', type: 'agent', label: 'b', status: 'idle' },
    [],
    [edge],
    start + 30_000,
  );
  assert.equal(receiving.mode, 'receiving');
  assert.equal(receiving.detail, 'Mission m1');
});

test('persisted past activity does not masquerade as current work', () => {
  const now = Date.parse('2026-10-06T05:00:00Z');
  const activity = deriveInhabitantActivity(
    { id: 'agent:alpha', type: 'agent', label: 'alpha', status: 'idle · no open mission' },
    [{ actor: 'alpha', title: 'research complete', at: now - 3_600_000, time: '2026-10-06T04:00:00Z' }],
    [],
    now,
  );
  assert.equal(activity.mode, 'observed');
  assert.equal(activity.label, 'No active work recorded');
  assert.equal(inhabitantGlyph('working'), '●');
});
