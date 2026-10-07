import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DESK_STAGES,
  bindAutonomousDesk,
  deriveShiftPackets,
  deriveShiftTape,
  deriveRuleRack,
  deriveKillBoard,
  deriveShiftReport,
  hasKillBoardCoverage,
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

test('Rule Rack shows enforced observations and leaves missing configuration unknown', () => {
  const rules = deriveRuleRack({
    runtime: { state: 'running' },
    resources: { state: 'ready', limits: { experiments: 1 }, minimum_available_memory_percent: 20 },
  }, {
    bill: { model_budget_usd: '0.50', basis: 'operator-reported', status: 'within_owner_limit' },
    gateway: { enabled: false, master_halt: true, prediction_execution_enabled: false },
    wallets: { control_plane: { live_execution_enabled: false, mission_authority_present: false, halted: true } },
    qualification: { stage: 'collecting', explanation: 'Insufficient prospective data' },
  });
  assert.match(rules.find(rule => rule.name === 'Model budget').value, /0\.50/);
  assert.match(rules.find(rule => rule.name === 'Prediction execution').value, /live=false/);
  assert.match(rules.find(rule => rule.name === 'Research evidence').source, /Insufficient/);
  const unavailable = deriveRuleRack({}, {});
  assert.equal(unavailable[2].value, 'Unavailable');
  assert.match(unavailable.find(rule => rule.name === 'Prediction execution').value, /UNKNOWN/);
  assert.match(unavailable.find(rule => rule.name === 'Treasury authority').value, /UNKNOWN/);
  const partial = deriveRuleRack({}, {
    gateway: { enabled: false, master_halt: true },
    wallets: { control_plane: { live_execution_enabled: false } },
  });
  assert.match(partial.find(rule => rule.name === 'Prediction execution').value, /live=UNKNOWN/);
  assert.match(partial.find(rule => rule.name === 'Treasury authority').value, /mission authority=UNKNOWN/);
  assert.match(partial.find(rule => rule.name === 'Treasury authority').value, /halted=UNKNOWN/);
  const staleBill = deriveRuleRack({}, { bill: { model_budget_usd: '0.50', basis: 'operator-reported' }, billFreshness: 'stale' })
    .find(rule => rule.name === 'Model budget');
  assert.equal(staleBill.value, 'STALE · 0.50');
  assert.match(staleBill.source, /request failed/);
  const staleControls = deriveRuleRack({}, {
    gateway: { enabled: false, master_halt: true, prediction_execution_enabled: false },
    wallets: { control_plane: { live_execution_enabled: false, mission_authority_present: false, halted: true } },
    freshness: { gateway: 'stale', wallets: 'stale' },
  });
  assert.match(staleControls.find(rule => rule.name === 'Prediction execution').value, /^STALE ·/);
  assert.match(staleControls.find(rule => rule.name === 'Treasury authority').source, /last successful snapshot/);
  const staleOperations = deriveRuleRack({ runtime: { state: 'running' }, resources: { state: 'ready', limits: { experiments: 1 } } }, {
    qualification: { stage: 'sufficient_for_validation', market_data: { freshness: 'stale' } },
    freshness: { operations: 'stale' },
  });
  assert.match(staleOperations.find(rule => rule.name === 'Research runtime').value, /^STALE ·/);
  assert.match(staleOperations.find(rule => rule.name === 'Heavy workload slots').source, /last successful snapshot/);
  assert.equal(staleOperations.find(rule => rule.name === 'Research evidence').value, 'STALE · sufficient_for_validation');
  assert.match(staleOperations.find(rule => rule.name === 'Research evidence').source, /request failed/);
});

test('Kill Board links only persisted passes, explicit critic rejects, and terminated missions', () => {
  const kills = deriveKillBoard({ sections: {
    decisions: { rows: [
      { id: 1, market_id: 'M1', decision: 'PASS', reason: 'After-cost edge below threshold' },
      { id: 2, market_id: 'M2', decision: 'BUY_YES' },
    ] },
    missions: { rows: [{ mission_id: 'x', status: 'failed', objective: 'Bad thesis', result: { failure_reason: 'ambiguous rules' } }] },
    research_runs: { rows: [
      { id: 3, specialist: 'critic', kind: 'review', result: JSON.stringify({ critic_review: { result_accepted: false, reason: 'source stale' } }) },
      { id: 4, specialist: 'critic', kind: 'review', result: '{malformed' },
    ] },
  } });
  assert.equal(kills.length, 3);
  assert.ok(kills.some(item => item.reason === 'After-cost edge below threshold'));
  assert.ok(kills.some(item => item.reason === 'ambiguous rules'));
  assert.ok(kills.some(item => item.reason === 'source stale'));
  assert.equal(hasKillBoardCoverage({ sections: {
    decisions: { status: 'empty' }, missions: { status: 'recorded' }, research_runs: { status: 'empty' },
  } }), true);
  assert.equal(hasKillBoardCoverage({ sections: {
    decisions: { status: 'empty' }, missions: { status: 'not_recorded' }, research_runs: { status: 'empty' },
  } }), false);
});

test('Shift Report uses persisted records, flags partial coverage, and keeps economics unknown', () => {
  const report = deriveShiftReport({
    as_of: '2026-10-06T12:00:00Z',
    sections: {
      missions: { status: 'recorded', has_more: false, rows: [{ mission_id: 'm1', status: 'completed', updated_at: '2026-10-06T11:00:00Z' }] },
      research_runs: { status: 'recorded', has_more: true, rows: [{ id: 1, specialist: 'kalshi-research', created_at: '2026-10-06T10:00:00Z', compute_cost_usd: null }] },
      decisions: { status: 'recorded', has_more: false, rows: [{ id: 2, created_at: '2026-10-06T09:00:00Z' }] },
      activity: { status: 'recorded', rows: [] }, mission_events: { status: 'recorded', rows: [] },
    },
  });
  assert.equal(report.counts.completed, 1);
  assert.equal(report.counts.investigations, 1);
  assert.equal(report.coverage, 'partial · section cap reached');
  assert.equal(report.compute_cost_usd, null);
  assert.match(report.economic_contribution, /Unknown/);
  const oldBlocker = deriveShiftReport({ as_of: '2026-10-06T12:00:00Z', sections: {
    missions: { status: 'recorded', rows: [{ mission_id: 'old', status: 'blocked', updated_at: '2026-10-01T00:00:00Z' }] },
    research_runs: { status: 'empty', rows: [] }, decisions: { status: 'empty', rows: [] },
    activity: { status: 'empty', rows: [] }, mission_events: { status: 'empty', rows: [] },
  } });
  assert.equal(oldBlocker.counts.missions, 0);
  assert.equal(oldBlocker.blockers[0].mission_id, 'old');
  const absent = deriveShiftReport({ as_of: '2026-10-06T12:00:00Z', sections: {} });
  assert.match(absent.coverage, /unavailable · not recorded/);
  assert.equal(absent.counts.missions, null);
  assert.equal(absent.blockers, null);
  const empty = deriveShiftReport({ as_of: '2026-10-06T12:00:00Z', sections: Object.fromEntries(
    ['missions', 'research_runs', 'decisions', 'activity', 'mission_events'].map(name => [name, { status: 'empty', rows: [] }]),
  ) });
  assert.equal(empty.counts.missions, 0);
  assert.equal(empty.counts.investigations, 0);
  assert.equal(empty.coverage, 'loaded records only');
  const staleSnapshot = deriveShiftReport({ as_of: '2026-10-06T12:00:00Z', sections: {
    missions: { status: 'recorded', rows: [] }, research_runs: { status: 'recorded', rows: [] },
    decisions: { status: 'recorded', rows: [] }, activity: { status: 'recorded', rows: [] },
    mission_events: { status: 'recorded', rows: [] },
  } }, 24, { operations: 'stale' });
  assert.match(staleSnapshot.coverage, /partial · operations snapshot stale/);
});
