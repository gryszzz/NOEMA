import test from 'node:test';
import assert from 'node:assert/strict';
import {
  habitatActivityState,
  isActiveMissionStatus,
  isAnimatedHandoff,
  habitatWorkstations,
  recordedOutputNodes,
} from '../noema/static/habitat-inhabitants.mjs';

test('agent activity comes only from recorded status', () => {
  assert.equal(habitatActivityState('running', 'healthy'), 'active');
  assert.equal(habitatActivityState('idle · no open mission', 'unknown'), 'idle');
  assert.equal(habitatActivityState('quarantined', 'degraded'), 'degraded');
  assert.equal(habitatActivityState('historical participant', 'observed'), 'historical');
  assert.equal(isActiveMissionStatus('claimed'), true);
  assert.equal(isActiveMissionStatus('completed'), false);
});

test('handoff animation is limited to current active persisted handoffs', () => {
  assert.equal(isAnimatedHandoff({ type: 'handoff · requested' }, false), true);
  assert.equal(isAnimatedHandoff({ type: 'handoff · running' }, false), true);
  assert.equal(isAnimatedHandoff({ type: 'handoff · completed' }, false), false);
  assert.equal(isAnimatedHandoff({ type: 'assigned specialist' }, false), false);
  assert.equal(isAnimatedHandoff({ type: 'handoff · running' }, true), false);
});

test('zone fixtures and recorded output shelf are deterministic projections', () => {
  const zone = { x: 10, y: 20, z: 5 };
  assert.deepEqual(habitatWorkstations(zone, 9), habitatWorkstations(zone, 9));
  assert.equal(habitatWorkstations(zone, 9).length, 3);
  const outputs = recordedOutputNodes([
    { id: 'mission:1', type: 'mission', created: 9 },
    { id: 'evidence:a', type: 'evidence', created: 2 },
    { id: 'forecast:1', type: 'forecast', created: 4 },
    { id: 'lesson:1', type: 'lesson', created: 3 },
  ]);
  assert.deepEqual(outputs.map((node) => node.id), ['forecast:1', 'lesson:1', 'evidence:a']);
});
