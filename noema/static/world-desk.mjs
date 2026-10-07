// NOEMA Autonomous Desk — presentation-only role binding over canonical habitat entities.
//
// The desk schema is inspired by small, single-responsibility agent teams. A visible seat never
// creates a specialist, capability, permission, execution route, or economic claim. Seats remain
// explicitly vacant when no canonical entity can be defensibly bound.

export const DESK_STAGES = Object.freeze([
  Object.freeze({ id: 'chief', label: 'CHIEF', purpose: 'coordinate only', terms: [] }),
  Object.freeze({ id: 'scout', label: 'SCOUT', purpose: 'discover candidates', terms: ['scout', 'scan', 'discovery', 'trench', 'web3', 'onchain', 'on_chain'] }),
  Object.freeze({ id: 'context', label: 'MAP / CONTEXT', purpose: 'world + source context', terms: ['meridian', 'map', 'intel', 'world', 'context', 'news', 'geopolit'] }),
  Object.freeze({ id: 'vet', label: 'VET', purpose: 'reject weak evidence', terms: ['critic', 'vet', 'validation', 'validate', 'evidence', 'review', 'quality'] }),
  Object.freeze({ id: 'odds', label: 'ODDS', purpose: 'forecast + valuation', terms: ['quant', 'forecast', 'prediction', 'odds', 'probability', 'market'] }),
  Object.freeze({ id: 'size', label: 'SIZE', purpose: 'bounded capital sizing', terms: ['size', 'sizing', 'allocation', 'budget', 'capital'] }),
  Object.freeze({ id: 'execution', label: 'EXECUTION', purpose: 'deterministic gateway only', terms: [] }),
  Object.freeze({ id: 'risk', label: 'RISK / EXIT', purpose: 'veto + close policy', terms: ['risk', 'exit', 'drawdown', 'stop', 'safety'] }),
]);

const ACTIVE_MISSION_STATES = new Set(['claimed', 'running', 'waiting']);

function normalized(value) {
  return String(value ?? '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

function nodeCorpus(node) {
  return normalized([
    node?.label,
    node?.record?.family,
    node?.record?.state,
    node?.metadata,
    ...(node?.capabilities ?? []),
  ].filter(Boolean).join(' '));
}

function scoreFor(node, stage) {
  if (!node || node.type !== 'agent' || node.id === 'agent:NOEMA') return 0;
  const corpus = nodeCorpus(node);
  let score = 0;
  for (const term of stage.terms) {
    const needle = normalized(term);
    if (needle && corpus.includes(needle)) score += needle.includes(' ') ? 5 : 3;
  }
  if (score > 0 && node.stateKind === 'healthy') score += 1;
  return score;
}

function seatState(node, activeIds) {
  if (!node) return 'vacant';
  if (activeIds.has(node.id)) return 'active';
  if (['degraded', 'offline', 'disabled', 'stale'].includes(node.stateKind)) return 'blocked';
  if (node.stateKind === 'healthy') return 'ready';
  return 'observed';
}

export function activeMissionIds(nodes = []) {
  return new Set(nodes
    .filter(node => node.type === 'mission'
      && ACTIVE_MISSION_STATES.has(normalized(node.rawStatus ?? node.status).replaceAll(' ', '_')))
    .map(node => node.missionId ?? node.record?.mission_id)
    .filter(Boolean));
}

export function activeDeskEntityIds(nodes = [], edges = []) {
  const missions = activeMissionIds(nodes);
  const ids = new Set(['agent:NOEMA']);
  for (const node of nodes) {
    const missionId = node.missionId ?? node.record?.mission_id;
    if (missionId && missions.has(missionId)) ids.add(node.id);
  }
  for (const edge of edges) {
    if (edge.missionId && missions.has(edge.missionId)) {
      ids.add(edge.from);
      ids.add(edge.to);
    }
  }
  return ids;
}

export function bindAutonomousDesk(nodes = [], edges = []) {
  const byId = new Map(nodes.map(node => [node.id, node]));
  const activeIds = activeDeskEntityIds(nodes, edges);
  const agents = nodes.filter(node => node.type === 'agent' && node.id !== 'agent:NOEMA');
  const used = new Set();
  const seats = [];

  for (const stage of DESK_STAGES) {
    let node = null;
    let basis = 'No canonical entity matched this presentation role';

    if (stage.id === 'chief') {
      node = byId.get('agent:NOEMA') ?? null;
      basis = node ? 'Persistent NOEMA identity' : basis;
    } else if (stage.id === 'execution') {
      node = byId.get('tool:execution-gateway') ?? null;
      basis = node ? 'Deterministic execution gateway status projection' : basis;
    } else {
      const ranked = agents
        .filter(agent => !used.has(agent.id))
        .map(agent => ({ agent, score: scoreFor(agent, stage) }))
        .filter(item => item.score > 0)
        .sort((a, b) => b.score - a.score || String(a.agent.id).localeCompare(String(b.agent.id)));
      node = ranked[0]?.agent ?? null;
      if (node) {
        used.add(node.id);
        basis = 'Presentation binding from persisted specialist name/family/capability metadata';
      }
    }

    seats.push({
      stageId: stage.id,
      label: stage.label,
      purpose: stage.purpose,
      nodeId: node?.id ?? null,
      nodeLabel: node?.label ?? 'VACANT / UNBOUND',
      state: seatState(node, activeIds),
      status: node?.status ?? 'No observed specialist/tool bound',
      basis,
      authority: stage.id === 'execution'
        ? 'Gateway visibility does not grant execution authority'
        : 'Seat binding does not grant permissions or execution authority',
    });
  }
  return seats;
}

export function deriveShiftPackets(nodes = [], edges = []) {
  const byId = new Map(nodes.map(node => [node.id, node]));
  return nodes
    .filter(node => node.type === 'mission'
      && ACTIVE_MISSION_STATES.has(normalized(node.rawStatus ?? node.status).replaceAll(' ', '_')))
    .map(mission => {
      const missionId = mission.missionId ?? mission.record?.mission_id;
      const route = [];
      const assigned = edges
        .filter(edge => edge.to === mission.id && edge.type === 'assigned specialist')
        .map(edge => byId.get(edge.from))
        .filter(Boolean);
      for (const agent of assigned) route.push({ nodeId: agent.id, label: agent.label, relation: 'lead' });
      const handoffs = edges
        .filter(edge => edge.missionId === missionId && String(edge.type ?? '').startsWith('handoff · '))
        .sort((a, b) => Number(a.at ?? 0) - Number(b.at ?? 0));
      for (const edge of handoffs) {
        const agent = byId.get(edge.to);
        if (!agent || route.some(item => item.nodeId === agent.id)) continue;
        route.push({ nodeId: agent.id, label: agent.label, relation: edge.type });
      }
      return {
        missionId,
        missionNodeId: mission.id,
        objective: mission.record?.objective ?? mission.label,
        status: mission.rawStatus ?? mission.status ?? 'unknown',
        route,
      };
    })
    .sort((a, b) => String(a.missionId).localeCompare(String(b.missionId)));
}

export function deriveShiftTape(timeline = [], limit = 10) {
  const max = Math.max(1, Math.min(20, Number(limit) || 10));
  return [...timeline]
    .filter(event => event?.at)
    .sort((a, b) => Number(b.at) - Number(a.at))
    .slice(0, max)
    .map(event => ({
      id: event.id,
      at: event.at,
      time: event.time ?? null,
      actor: event.actor ?? 'NOEMA',
      title: event.title ?? 'recorded event',
      status: event.status ?? 'recorded',
      missionId: event.missionId ?? null,
      kind: event.kind ?? 'EVENT',
    }));
}

const asRows = (snapshot, section) => snapshot?.sections?.[section]?.rows ?? [];
const parseTime = value => {
  const raw = String(value ?? '');
  const normalizedTime = raw && !/[zZ]|[+-]\d\d:\d\d$/.test(raw)
    ? `${raw.replace(' ', 'T')}Z` : raw;
  const stamp = Date.parse(normalizedTime);
  return Number.isFinite(stamp) ? stamp : null;
};

function parseResult(value) {
  if (value && typeof value === 'object' && !Array.isArray(value)) return value;
  if (typeof value !== 'string') return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

export function deriveRuleRack(snapshot = {}, sources = {}) {
  if (sources.historicalReplay) {
    return ['Research runtime', 'Heavy workload slots', 'Model budget', 'Research evidence', 'Prediction execution', 'Treasury authority']
      .map(name => ({ name, value: 'Unavailable · historical replay', source: 'Current state is outside the selected event cutoff' }));
  }
  const resources = snapshot.resources ?? {};
  const gateway = sources.gateway;
  const control = sources.wallets?.control_plane;
  const qualification = sources.qualification;
  const bill = sources.bill;
  const billFreshness = sources.billFreshness ?? 'unknown';
  const freshness = sources.freshness ?? {};
  const observed = (value, fallback = 'Unknown') => value == null || value === '' ? fallback : String(value);
  const controlValue = value => typeof value === 'boolean' ? String(value) : 'UNKNOWN';
  const controlState = (value, state) => state === 'stale' ? `STALE · ${value}` : value;
  const operationsState = value => controlState(value, freshness.operations);
  const qualificationFreshness = qualification?.market_data?.freshness;
  const qualificationUnavailable = qualification?.status === 'unavailable';
  const qualificationRequestFreshness = sources.qualificationRequestFreshness ?? 'unknown';
  const budgetValue = observed(bill?.model_budget_usd, 'Unavailable');
  return [
    { name: 'Research runtime', value: operationsState(observed(snapshot.runtime?.state)), source: freshness.operations === 'stale'
      ? 'Latest /api/operations request failed; retained worker state is the last successful snapshot'
      : 'Persisted worker heartbeat / cycle state' },
    { name: 'Heavy workload slots', value: operationsState(resources.limits
      ? `${Object.entries(resources.limits).map(([key, value]) => `${key.replaceAll('_', ' ')} ${value}`).join(' · ')} · ${observed(resources.state)}`
      : 'Unavailable'), source: freshness.operations === 'stale'
        ? 'Latest /api/operations request failed; retained admission limits are the last successful snapshot'
        : `Deterministic resource admission · memory floor ${observed(resources.minimum_available_memory_percent, 'Unknown')}%` },
    { name: 'Model budget', value: billFreshness === 'stale' && budgetValue !== 'Unavailable'
      ? `STALE · ${budgetValue}` : budgetValue, source: billFreshness === 'stale'
      ? 'Latest /api/bill request failed; retained budget is the last successful snapshot'
      : billFreshness === 'unavailable'
        ? 'Latest /api/bill request failed; model spend limit unknown'
        : bill?.status === 'estimate_missing'
          ? 'No persisted budget; model spend limit unknown'
          : `Persisted operator budget · ${bill?.basis ?? 'coverage unknown'}` },
    { name: 'Research evidence', value: qualificationUnavailable ? 'Unavailable'
      : qualificationRequestFreshness === 'stale' || qualificationFreshness === 'stale'
      ? `STALE · ${observed(qualification?.stage, 'Unavailable')}` : observed(qualification?.stage, 'Unavailable'),
      source: qualificationUnavailable ? 'Market-data qualification unavailable; no stage inferred'
        : qualificationRequestFreshness === 'stale' ? 'Latest qualification request failed; retained evidence is the last successful snapshot'
          : qualificationFreshness === 'stale' ? 'Latest qualifying market evidence is stale; stage is based on old observations'
        : qualification?.explanation ?? 'Qualification evidence unavailable; no readiness inferred' },
    { name: 'Prediction execution', value: controlState(`enabled=${controlValue(gateway?.enabled)} · halt=${controlValue(gateway?.master_halt)} · live=${controlValue(gateway?.prediction_execution_enabled)}`, freshness.gateway),
      source: freshness.gateway === 'stale' ? 'Latest gateway request failed; retained policy is the last successful snapshot'
        : gateway?.status ?? 'Gateway policy unavailable; execution state unknown' },
    { name: 'Treasury authority', value: controlState(`live=${controlValue(control?.live_execution_enabled)} · mission authority=${controlValue(control?.mission_authority_present)} · halted=${controlValue(control?.halted)}`, freshness.wallets),
      source: freshness.wallets === 'stale' ? 'Latest wallet request failed; retained controls are the last successful snapshot'
        : control?.status ?? 'Wallet control-plane state unavailable' },
  ];
}

export function deriveKillBoard(snapshot = {}, limit = 12, cutoff = Infinity) {
  const items = [];
  const beforeCutoff = value => {
    if (!Number.isFinite(cutoff)) return true;
    const time = parseTime(value);
    return time != null && time <= cutoff;
  };
  for (const row of asRows(snapshot, 'decisions')) {
    if (!beforeCutoff(row.created_at)) continue;
    const decision = String(row.decision ?? '').toUpperCase();
    if (!['PASS', 'NO_ACTION', 'REJECT', 'REJECTED', 'BLOCKED'].includes(decision)) continue;
    items.push({ id: `decision:${row.id}`, at: row.created_at, kind: 'FORECAST DECISION',
      title: `${row.market_id ?? 'Market unknown'} · ${decision}`,
      reason: row.reason ?? 'No rejection reason persisted', marketId: row.market_id ?? null,
      decisionId: row.id, missionId: null });
  }
  for (const row of asRows(snapshot, 'missions')) {
    const status = String(row.status ?? '').toLowerCase();
    if (!['failed', 'rejected', 'terminated', 'quarantined', 'cancelled', 'canceled'].includes(status)) continue;
    const at = row.completed_at ?? row.updated_at ?? row.created_at;
    if (!beforeCutoff(at)) continue;
    items.push({ id: `mission:${row.mission_id}`, at,
      kind: 'MISSION TERMINATION', title: row.objective ?? row.mission_id,
      reason: row.result?.reason ?? row.result?.failure_reason ?? row.status,
      missionId: row.mission_id, decisionId: null });
  }
  for (const row of asRows(snapshot, 'research_runs')) {
    if (!beforeCutoff(row.completed_at ?? row.created_at)) continue;
    const review = parseResult(row.result).critic_review;
    if (!review || review.result_accepted !== false) continue;
    items.push({ id: `critic:${row.id}`, at: row.completed_at ?? row.created_at,
      kind: 'EVIDENCE CRITIC', title: `${row.specialist ?? 'Specialist unknown'} · ${row.kind ?? 'research'}`,
      reason: review.reason ?? (Array.isArray(review.issues) ? review.issues.join(' · ') : null)
        ?? review.conclusion ?? review.verdict ?? 'Evidence critic rejected the result',
      missionId: row.mission_id ?? null, decisionId: null });
  }
  return items.sort((a, b) => (parseTime(b.at) ?? 0) - (parseTime(a.at) ?? 0)
    || a.id.localeCompare(b.id)).slice(0, Math.max(1, Math.min(50, Number(limit) || 12)));
}

export function hasKillBoardCoverage(snapshot = {}) {
  return ['decisions', 'missions', 'research_runs'].every(name =>
    ['recorded', 'empty'].includes(snapshot.sections?.[name]?.status));
}

export function deriveShiftReport(snapshot = {}, hours = 24, freshness = {}, cutoff = Infinity) {
  if (Number.isFinite(cutoff)) {
    const sections = snapshot.sections ?? {};
    const beforeCutoff = value => {
      const time = parseTime(value);
      return time != null && time <= cutoff;
    };
    const missionEvents = asRows(snapshot, 'mission_events').filter(event => beforeCutoff(event.created_at));
    const missions = asRows(snapshot, 'missions').filter(mission => beforeCutoff(mission.created_at)).map(mission => {
      if (beforeCutoff(mission.updated_at ?? mission.created_at)) return mission;
      const lastEvent = missionEvents.filter(event => event.mission_id === mission.mission_id && event.status)
        .sort((a, b) => (parseTime(a.created_at) ?? 0) - (parseTime(b.created_at) ?? 0)).at(-1);
      return { ...mission, status: lastEvent?.status ?? 'unknown', updated_at: lastEvent?.created_at ?? mission.created_at };
    });
    const filteredSections = Object.fromEntries(Object.entries(sections).map(([name, section]) => {
      const timeField = name === 'missions' ? 'created_at'
        : ['research_runs', 'sessions'].includes(name) ? null : 'created_at';
      const rows = name === 'missions' ? missions : (section.rows ?? []).filter(row => {
        if (timeField === null) return beforeCutoff(row.completed_at ?? row.created_at);
        return beforeCutoff(row[timeField]);
      });
      return [name, { ...section, rows }];
    }));
    snapshot = { ...snapshot, as_of: new Date(cutoff).toISOString(), sections: filteredSections };
  }
  const now = parseTime(snapshot.as_of) ?? Date.now();
  const windowMs = Math.max(1, Math.min(168, Number(hours) || 24)) * 60 * 60 * 1000;
  const since = now - windowMs;
  const within = (section, timeField = 'created_at') => asRows(snapshot, section)
    .filter(row => { const time = parseTime(row[timeField]); return time != null && time >= since && time <= now; });
  const missions = within('missions', 'updated_at');
  const completedWithin = section => asRows(snapshot, section).filter(row => {
    const time = parseTime(row.completed_at ?? row.created_at);
    return time != null && time >= since && time <= now;
  });
  const runs = completedWithin('research_runs');
  const sessions = completedWithin('sessions');
  const forecasts = within('decisions');
  const events = [...within('activity'), ...within('mission_events')];
  const sectionRecorded = name => ['recorded', 'empty'].includes(snapshot.sections?.[name]?.status);
  const missionAvailable = sectionRecorded('missions');
  const runsAvailable = sectionRecorded('research_runs');
  const decisionsAvailable = sectionRecorded('decisions');
  const eventsAvailable = sectionRecorded('activity') && sectionRecorded('mission_events');
  const counts = { missions: missionAvailable ? missions.length : null,
    investigations: runsAvailable ? runs.length : null, forecasts: decisionsAvailable ? forecasts.length : null,
    completed: missionAvailable ? missions.filter(row => ['completed', 'passed'].includes(String(row.status).toLowerCase())).length : null,
    failures: missionAvailable ? missions.filter(row => ['failed', 'rejected', 'terminated', 'quarantined', 'cancelled', 'canceled'].includes(String(row.status).toLowerCase())).length : null,
    events: eventsAvailable ? events.length : null };
  const runCostValues = runs.map(row => row.compute_cost_usd == null ? null : Number(row.compute_cost_usd))
    .filter(value => value != null && Number.isFinite(value) && value >= 0);
  const missionsById = new Map(asRows(snapshot, 'missions').map(row => [String(row.mission_id), row]));
  const sessionIdsByRun = new Map();
  for (const run of runs) {
    const linkedSessionIds = new Set();
    if (run.session_id != null) linkedSessionIds.add(String(run.session_id));
    for (const mission of missionsById.values()) {
      const runLinkedById = mission.run_id != null && String(mission.run_id) === String(run.id);
      const runLinkedByMission = run.mission_id != null && mission.mission_id != null
        && String(mission.mission_id) === String(run.mission_id);
      if ((runLinkedById || runLinkedByMission) && mission.session_id != null)
        linkedSessionIds.add(String(mission.session_id));
    }
    sessionIdsByRun.set(String(run.id), linkedSessionIds);
  }
  const sessionCostRows = [];
  const seenSessionCosts = new Set();
  for (const row of sessions) {
    if (row.session_id != null && seenSessionCosts.has(String(row.session_id))) continue;
    if (row.session_id != null) seenSessionCosts.add(String(row.session_id));
    sessionCostRows.push(row);
  }
  const validCost = value => value != null && Number.isFinite(Number(value)) && Number(value) >= 0;
  const sessionModelCostValues = sessionCostRows.filter(row => validCost(row.estimated_model_cost_usd))
    .map(row => Number(row.estimated_model_cost_usd));
  const sessionComputeCostRows = sessionCostRows.filter(row => {
    const linkedRuns = runs.filter(run => sessionIdsByRun.get(String(run.id))?.has(String(row.session_id)));
    return !linkedRuns.some(run => validCost(run.compute_cost_usd));
  });
  const sessionComputeCostValues = sessionComputeCostRows.filter(row => validCost(row.compute_cost_usd))
    .map(row => Number(row.compute_cost_usd));
  const linkedSessionComputeIds = new Set(sessionComputeCostRows.filter(row => validCost(row.compute_cost_usd))
    .map(row => String(row.session_id)));
  const runCostUnknown = runs.some(run => !validCost(run.compute_cost_usd)
    && ![...(sessionIdsByRun.get(String(run.id)) ?? [])].some(id => linkedSessionComputeIds.has(id)));
  const sessionCostUnknown = sessionCostRows.some(row => !validCost(row.estimated_model_cost_usd)
    || (!validCost(row.compute_cost_usd)
      && !runs.some(run => validCost(run.compute_cost_usd)
        && sessionIdsByRun.get(String(run.id))?.has(String(row.session_id)))));
  const costValues = [...runCostValues, ...sessionModelCostValues, ...sessionComputeCostValues];
  const cost = costValues.length ? costValues.reduce((sum, value) => sum + value, 0) : null;
  const contributors = [...new Set(runs.map(row => row.specialist).filter(Boolean))];
  const blocked = missionAvailable ? asRows(snapshot, 'missions')
    .filter(row => ['queued', 'waiting', 'blocked'].includes(String(row.status).toLowerCase())) : null;
  const requiredSections = ['missions', 'research_runs', 'sessions', 'decisions', 'activity', 'mission_events'];
  const missing = requiredSections.filter(section => !sectionRecorded(section));
  const capped = requiredSections.some(section => snapshot.sections?.[section]?.has_more === true);
  return {
    window_hours: Math.round(windowMs / 3600000),
    as_of: snapshot.as_of ?? null,
    coverage: missing.length ? `unavailable · not recorded: ${missing.join(', ')}`
      : ['stale', 'unavailable'].includes(freshness.operations) ? `partial · operations snapshot ${freshness.operations}`
        : capped ? 'partial · section cap reached' : 'loaded records only',
    counts,
    compute_cost_usd: cost,
    compute_cost_records: costValues.length,
    compute_cost_unknown: runCostUnknown || !sectionRecorded('sessions') || sessionCostUnknown,
    contributors,
    blockers: blocked?.map(row => ({ mission_id: row.mission_id, objective: row.objective ?? row.mission_id, status: row.status })) ?? null,
    economic_contribution: 'Unknown · realized net value is not inferred from activity or paper results',
  };
}
