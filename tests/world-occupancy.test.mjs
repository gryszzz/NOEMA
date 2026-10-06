import test from 'node:test';
import assert from 'node:assert/strict';
import {
  deriveMissionOccupancies,
  deriveActiveMissionLineage,
  recordedDeliverable,
} from '../noema/static/world-occupancy.mjs';

test('active missions place only canonically assigned or handoff agents', () => {
  const nodes = [
    { id: 'mission:m1', type: 'mission', missionId: 'm1', status: 'running' },
    { id: 'agent:quant', type: 'agent' },
    { id: 'agent:critic', type: 'agent' },
    { id: 'agent:idle', type: 'agent' },
  ];
  const edges = [
    { from: 'agent:quant', to: 'mission:m1', type: 'assigned specialist', missionId: 'm1' },
    { from: 'agent:quant', to: 'agent:critic', type: 'handoff · running', missionId: 'm1' },
  ];
  const occupied = deriveMissionOccupancies(nodes, edges);
  assert.deepEqual(occupied.map(item => item.agentId).sort(), ['agent:critic', 'agent:quant']);
  assert.ok(occupied.every(item => item.missionNodeId === 'mission:m1'));
  assert.ok(occupied.every(item => Number.isFinite(item.offset.x) && Number.isFinite(item.offset.y)));
});

test('completed or inactive missions do not create current occupancy', () => {
  const nodes = [
    { id: 'mission:m1', type: 'mission', missionId: 'm1', status: 'completed' },
    { id: 'agent:quant', type: 'agent' },
  ];
  const edges = [{ from: 'agent:quant', to: 'mission:m1', type: 'assigned specialist', missionId: 'm1' }];
  assert.deepEqual(deriveMissionOccupancies(nodes, edges), []);
});

test('active mission lineage lights only persisted mission relationships', () => {
  const nodes = [
    { id: 'mission:m1', type: 'mission', missionId: 'm1', status: 'claimed' },
    { id: 'agent:a', type: 'agent' },
    { id: 'experiment:t1', type: 'experiment' },
    { id: 'wallet:w1', type: 'wallet' },
  ];
  const edges = [
    { from: 'agent:a', to: 'mission:m1', type: 'assigned specialist', missionId: 'm1' },
    { from: 'mission:m1', to: 'experiment:t1', type: 'exact trial id', missionId: 'm1' },
    { from: 'agent:a', to: 'wallet:w1', type: 'wallet network status observed' },
  ];
  const lineage = deriveActiveMissionLineage(nodes, edges);
  assert.equal(lineage.has('mission:m1'), true);
  assert.equal(lineage.has('agent:a'), true);
  assert.equal(lineage.has('experiment:t1'), true);
  assert.equal(lineage.has('wallet:w1'), false);
});

test('deliverable objects require an explicit completed mission result flag', () => {
  const base = {
    id: 'mission:m1', type: 'mission', missionId: 'm1', status: 'completed',
    record: { mission_id: 'm1', completed_at: '2026-10-06T05:00:00Z' },
  };
  assert.equal(recordedDeliverable(base), null);
  assert.equal(recordedDeliverable({ ...base, record: { ...base.record, result: { deliverable_produced: false } } }), null);
  const output = recordedDeliverable({
    ...base,
    record: { ...base.record, result: { deliverable_produced: 'market memo', delivery_tested: true } },
  });
  assert.equal(output.id, 'deliverable:m1');
  assert.equal(output.label, 'market memo');
  assert.equal(output.deliveryTested, true);
  assert.equal(output.source, 'Persisted mission result_json');
});
