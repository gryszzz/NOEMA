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
