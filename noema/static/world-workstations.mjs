// Visual workstation classification for NOEMA's Economic Habitat.
//
// Classification is presentation-only. It never implies that a capability is healthy,
// reachable, authorized, funded, or executing.

export const WORKSTATION_KINDS = Object.freeze({
  command: Object.freeze({ label: 'Mission command table', glyph: '◇' }),
  cognition: Object.freeze({ label: 'Cognition / data terminal', glyph: '▣' }),
  market: Object.freeze({ label: 'Market console', glyph: '▤' }),
  forecast: Object.freeze({ label: 'Forecast beacon', glyph: '◈' }),
  vault: Object.freeze({ label: 'Treasury vault', glyph: '▧' }),
  gateway: Object.freeze({ label: 'Execution gateway console', glyph: '⬡' }),
  tool: Object.freeze({ label: 'Runtime tool bench', glyph: '□' }),
  archive: Object.freeze({ label: 'Evidence archive', glyph: '▥' }),
  session: Object.freeze({ label: 'Session terminal', glyph: '▱' }),
  experiment: Object.freeze({ label: 'Research bench', glyph: '◆' }),
});

export function workstationKind(node) {
  if (!node || ['core', 'agent'].includes(node.type)) return null;
  if (node.type === 'mission') return 'command';
  if (node.type === 'market') return 'market';
  if (node.type === 'forecast') return 'forecast';
  if (node.type === 'wallet') return 'vault';
  if (node.type === 'provider') {
    return String(node.id ?? '').startsWith('provider:venue:') ? 'market' : 'cognition';
  }
  if (node.type === 'tool') {
    return node.id === 'tool:execution-gateway' ? 'gateway' : 'tool';
  }
  if (node.type === 'session') return 'session';
  if (node.type === 'experiment') return 'experiment';
  if (['evidence', 'lesson', 'deliverable'].includes(node.type)) return 'archive';
  return 'tool';
}

export function workstationDescriptor(node) {
  const kind = workstationKind(node);
  return kind ? { kind, ...WORKSTATION_KINDS[kind] } : null;
}
