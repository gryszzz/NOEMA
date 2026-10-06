// Runtime-backed inhabitant semantics for the NOEMA Economic Habitat.
//
// No activity is synthesized here. Labels and movement are derived only from canonical
// node state, persisted timeline events, and persisted specialist handoff edges.

const ACTIVE_WORDS = ['running', 'claimed', 'active_research', 'in_progress'];
const BAD_WORDS = ['degraded', 'failed', 'error', 'offline', 'quarantined', 'invalid'];

function asStamp(value) {
  if (Number.isFinite(value)) return Number(value);
  const parsed = Date.parse(value ?? '');
  return Number.isFinite(parsed) ? parsed : 0;
}

export function deriveInhabitantActivity(node, timeline = [], edges = [], now = Date.now()) {
  if (!node || !['core', 'agent'].includes(node.type)) return null;

  const status = String(node.rawStatus ?? node.status ?? 'unknown').toLowerCase();
  const linkedHandoffs = edges
    .filter(edge => String(edge.type ?? '').startsWith('handoff · ')
      && (edge.from === node.id || edge.to === node.id))
    .filter(edge => asStamp(edge.at) > 0 && asStamp(edge.at) <= now)
    .sort((a, b) => asStamp(b.at) - asStamp(a.at));
  const latestHandoff = linkedHandoffs[0];
  const handoffAge = latestHandoff ? now - asStamp(latestHandoff.at) : Infinity;

  if (latestHandoff && handoffAge <= 90_000) {
    const receiving = latestHandoff.to === node.id;
    return {
      mode: receiving ? 'receiving' : 'handoff',
      label: receiving ? 'Receiving persisted handoff' : 'Persisted handoff in progress',
      detail: latestHandoff.missionId ? `Mission ${latestHandoff.missionId}` : latestHandoff.type,
      evidenceAt: latestHandoff.at,
    };
  }

  if (BAD_WORDS.some(word => status.includes(word))) {
    return {
      mode: 'degraded',
      label: 'Degraded / unavailable',
      detail: node.status ?? 'Recorded degraded state',
      evidenceAt: node.observedAt ?? node.record?.updated_at ?? node.record?.created_at ?? null,
    };
  }

  if (ACTIVE_WORDS.some(word => status.includes(word))) {
    return {
      mode: 'working',
      label: 'Recorded active work',
      detail: node.status ?? 'Active state recorded',
      evidenceAt: node.observedAt ?? node.record?.updated_at ?? node.record?.created_at ?? null,
    };
  }

  const actor = node.type === 'core' ? 'NOEMA' : node.label;
  const latestEvent = [...timeline].reverse().find(event => event.actor === actor && asStamp(event.at) <= now);
  if (latestEvent) {
    return {
      mode: 'observed',
      label: 'No active work recorded',
      detail: `Latest persisted event · ${latestEvent.title}`,
      evidenceAt: latestEvent.time ?? latestEvent.at,
    };
  }

  return {
    mode: 'unknown',
    label: 'Activity unknown',
    detail: 'No persisted activity proves a current task.',
    evidenceAt: null,
  };
}

export function handoffTraversal(edge, now = Date.now(), durationMs = 90_000) {
  if (!edge || !String(edge.type ?? '').startsWith('handoff · ')) return null;
  const at = asStamp(edge.at);
  const duration = Math.max(1, Number(durationMs) || 90_000);
  if (!at || at > now) return null;
  const age = now - at;
  if (age > duration) return null;
  return {
    progress: Math.max(0, Math.min(1, age / duration)),
    ageMs: age,
    durationMs: duration,
    missionId: edge.missionId ?? null,
  };
}

export function inhabitantGlyph(mode) {
  return ({
    working: '●',
    receiving: '⇢',
    handoff: '⇥',
    degraded: '!',
    observed: '·',
    unknown: '?',
  })[mode] ?? '·';
}
