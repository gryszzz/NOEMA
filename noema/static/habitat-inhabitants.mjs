// Pure visual-state helpers for the NOEMA Economic Habitat.
// These helpers classify already-recorded state; they never create runtime activity.

const ACTIVE_WORDS = Object.freeze(['running', 'claimed', 'waiting', 'active', 'in_progress', 'in progress']);

export function habitatActivityState(status, stateKind = 'unknown') {
  const raw = String(status ?? '').toLowerCase();
  if (ACTIVE_WORDS.some((word) => raw.includes(word))) return 'active';
  if (stateKind === 'degraded' || stateKind === 'offline' || raw.includes('quarantined')) return 'degraded';
  if (raw.includes('historical')) return 'historical';
  if (raw.includes('idle')) return 'idle';
  if (stateKind === 'healthy') return 'ready';
  if (stateKind === 'observed') return 'observed';
  return 'unknown';
}

export function isActiveMissionStatus(status) {
  const raw = String(status ?? '').toLowerCase();
  return ACTIVE_WORDS.some((word) => raw.includes(word));
}

export function isAnimatedHandoff(edge, replaying = false) {
  if (replaying || !edge || !String(edge.type ?? '').toLowerCase().startsWith('handoff ·')) return false;
  const state = String(edge.type).split('·').at(-1)?.trim().toLowerCase() ?? '';
  return ACTIVE_WORDS.includes(state);
}

export function habitatWorkstations(zone, entityCount = 1) {
  const count = Math.max(1, Number(entityCount) || 1);
  const total = Math.min(4, Math.max(2, Math.ceil(Math.sqrt(count))));
  const spread = total === 2 ? 48 : 42;
  const rows = [];
  for (let index = 0; index < total; index++) {
    const offset = index - (total - 1) / 2;
    rows.push({
      x: zone.x + offset * spread,
      y: zone.y + (index % 2 ? 24 : -18),
      z: zone.z + 2,
    });
  }
  return rows;
}

export function recordedOutputNodes(nodes) {
  const types = new Set(['evidence', 'experiment', 'lesson', 'forecast']);
  return [...(nodes ?? [])]
    .filter((node) => types.has(node.type))
    .sort((a, b) => Number(b.created ?? 0) - Number(a.created ?? 0))
    .slice(0, 8);
}
