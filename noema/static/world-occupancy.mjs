// Mission-backed occupancy and deliverable semantics for the NOEMA Economic Habitat.
//
// These helpers derive presentation state only from canonical nodes/edges. They never invent
// missions, tools, authority, outputs, or agent activity.

const ACTIVE_MISSION_STATES = new Set(['claimed', 'running', 'waiting']);
const ACTIVE_HANDOFF_STATES = new Set(['requested', 'accepted', 'running']);

function statusValue(node) {
  return String(node?.rawStatus ?? node?.status ?? '').trim().toLowerCase().replaceAll(' ', '_');
}

function stableOffset(key, index = 0) {
  let hash = 2166136261;
  for (const char of String(key ?? '')) {
    hash ^= char.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  const angle = ((hash >>> 0) % 360) * Math.PI / 180 + index * 1.7;
  const radius = 24 + (index % 3) * 8;
  return { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius * .62, z: 15 + (index % 2) * 8 };
}

export function deriveMissionOccupancies(nodes = [], edges = []) {
  const byId = new Map(nodes.map(node => [node.id, node]));
  const activeMissions = nodes
    .filter(node => node.type === 'mission' && ACTIVE_MISSION_STATES.has(statusValue(node)))
    .sort((a, b) => String(a.id).localeCompare(String(b.id)));
  const occupancies = [];
  const seen = new Set();

  for (const mission of activeMissions) {
    const missionId = mission.missionId ?? mission.record?.mission_id;
    const assignments = edges.filter(edge => edge.to === mission.id && edge.type === 'assigned specialist');
    for (const edge of assignments) {
      const agent = byId.get(edge.from);
      if (!agent || !['agent', 'core'].includes(agent.type)) continue;
      const key = `${agent.id}|${mission.id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      occupancies.push({
        agentId: agent.id,
        missionNodeId: mission.id,
        missionId,
        evidence: 'assigned specialist',
        role: 'lead',
      });
    }

    const handoffs = edges.filter(edge => edge.missionId === missionId
      && String(edge.type ?? '').startsWith('handoff · '));
    for (const edge of handoffs) {
      const handoffState = String(edge.type).split('·').at(-1)?.trim().toLowerCase();
      if (!ACTIVE_HANDOFF_STATES.has(handoffState)) continue;
      const recipient = byId.get(edge.to);
      if (!recipient || recipient.type !== 'agent') continue;
      const key = `${recipient.id}|${mission.id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      occupancies.push({
        agentId: recipient.id,
        missionNodeId: mission.id,
        missionId,
        evidence: edge.type,
        role: 'handoff recipient',
      });
    }
  }

  return occupancies.map((item, index) => ({ ...item, offset: stableOffset(item.agentId, index) }));
}

export function deriveActiveMissionLineage(nodes = [], edges = []) {
  const activeMissionIds = new Set(nodes
    .filter(node => node.type === 'mission' && ACTIVE_MISSION_STATES.has(statusValue(node)))
    .map(node => node.missionId ?? node.record?.mission_id)
    .filter(Boolean));
  const ids = new Set(nodes
    .filter(node => node.type === 'mission'
      && activeMissionIds.has(node.missionId ?? node.record?.mission_id))
    .map(node => node.id));
  for (const edge of edges) {
    if (!edge.missionId || !activeMissionIds.has(edge.missionId)) continue;
    ids.add(edge.from);
    ids.add(edge.to);
  }
  return ids;
}

export function recordedDeliverable(mission) {
  if (!mission || mission.type !== 'mission') return null;
  const status = statusValue(mission);
  if (!['passed', 'completed'].includes(status)) return null;
  const result = mission.record?.result;
  if (!result || typeof result !== 'object' || Array.isArray(result)) return null;
  const produced = result.deliverable_produced;
  const present = produced === true
    || (typeof produced === 'number' && Number.isFinite(produced) && produced > 0)
    || (typeof produced === 'string'
      && produced.trim().length > 0
      && !['false', 'no', 'none', '0', 'unknown'].includes(produced.trim().toLowerCase()));
  if (!present) return null;
  const missionId = mission.missionId ?? mission.record?.mission_id;
  if (!missionId) return null;
  return {
    id: `deliverable:${missionId}`,
    missionId,
    label: typeof produced === 'string' ? produced.slice(0, 120) : 'Recorded mission deliverable',
    produced,
    deliveryTested: result.delivery_tested === true,
    createdAt: mission.record?.completed_at ?? mission.record?.updated_at ?? mission.record?.created_at ?? null,
    source: 'Persisted mission result_json',
  };
}
