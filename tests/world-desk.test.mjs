import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DESK_STAGES,
  bindAutonomousDesk,
  deriveShiftPackets,
  deriveShiftTape,
} from '../noema/static/world-desk.mjs';

test('autonomous desk binds only observable specialists/tools and leaves missing seats vacant', () => {
  const nodes = [
    { id: 'agent:NOEMA', type: 'core', label: 'NOEMA', stateKind: 'healthy', status: 'running' },
    { id: 'agent:trench-1', type: 'agent', label: 'trench-1', stateKind: 'observed', record: { family: 'solana web3 discovery' } },
    { id: 'agent:evidence-critic', type: 'agent', label: 'evidence-critic', stateKind: 'healthy', record: { family: 'validation' } },
    { id: 'agent:quant', type: 'agent', label: 'quant', stateKind: 'observed', record: { family: 'prediction forecast' } },
    { id: 'tool:execution-gateway', type: 'tool', label: 'Execution gateway', stateKind: 'disabled', status: 'execution disabled' },
  ];
  const seats = bindAutonomousDesk(nodes, []);
  assert.equal(seats.find(seat => seat.stageId === 'chief').nodeId, 'agent:NOEMA');
  assert.equal(seats.find(seat => seat.stageId === 'scout').nodeId, 'agent:trench-1');
  assert.equal(seats.find(seat => seat.stageId === 'vet').nodeId, 'agent:evidence-critic');
  assert.equal(seats.find(seat => seat.stageId === 'odds').nodeId, 'agent:quant');
  assert.equal(seats.find(seat => seat.stageId === 'execution').nodeId, 'tool:execution-gateway');
  assert.equal(seats.find(seat => seat.stageId === 'context').state, 'vacant');
  assert.equal(seats.find(seat => seat.stageId === 'risk').nodeLabel, 'VACANT / UNBOUND');
  assert.equal(new Set(seats.map(seat => seat.stageId)).size, DESK_STAGES.length);
});

test('execution desk state follows gateway observation and never creates authority', () => {
  const seats = bindAutonomousDesk([
    { id: 'agent:NOEMA', type: 'core', label: 'NOEMA', stateKind: 'healthy' },
    { id: 'tool:execution-gateway', type: 'tool', label: 'Execution gateway', stateKind: 'disabled', status: 'execution disabled' },
  ], []);
  const execution = seats.find(seat => seat.stageId === 'execution');
  assert.equal(execution.state, 'blocked');
  assert.match(execution.authority, /does not grant execution authority/i);
});

test('shift packet route comes only from persisted assignment and handoff edges', () => {
  const nodes = [
    { id: 'mission:m1', type: 'mission', missionId: 'm1', status: 'running', record: { mission_id: 'm1', objective: 'Review market evidence' } },
    { id: 'agent:quant', type: 'agent', label: 'quant' },
    { id: 'agent:critic', type: 'agent', label: 'critic' },
    { id: 'agent:unrelated', type: 'agent', label: 'unrelated' },
  ];
  const edges = [
    { from: 'agent:quant', to: 'mission:m1', type: 'assigned specialist', missionId: 'm1' },
    { from: 'agent:quant', to: 'agent:critic', type: 'handoff · accepted', missionId: 'm1', at: 2 },
    { from: 'agent:unrelated', to: 'agent:quant', type: 'registered specialist capability', at: 1 },
  ];
  const packets = deriveShiftPackets(nodes, edges);
  assert.equal(packets.length, 1);
  assert.deepEqual(packets[0].route.map(item => item.nodeId), ['agent:quant', 'agent:critic']);
  assert.equal(packets[0].objective, 'Review market evidence');
});

test('shift tape is bounded and chronological newest-first', () => {
  const tape = deriveShiftTape([
    { id: 'a', at: 1, actor: 'A', title: 'one' },
    { id: 'b', at: 3, actor: 'B', title: 'three' },
    { id: 'c', at: 2, actor: 'C', title: 'two' },
  ], 2);
  assert.deepEqual(tape.map(item => item.id), ['b', 'c']);
});
