// NOEMA Economic Habitat spatial contract.
//
// This module is intentionally pure: it contains no DOM/canvas access and no runtime
// authority. It maps canonical operating entities into stable functional zones so the
// visual world remains a projection of persisted/live state rather than a simulation.

export const HABITAT_ZONES = Object.freeze({
  providers: Object.freeze({
    x: -390, y: 120, z: -55,
    name: 'COGNITION ARRAY',
    shortName: 'COGNITION',
    purpose: 'MODEL + DATA ROUTES',
    color: '#72cfe2',
  }),
  agents: Object.freeze({
    x: -40, y: 205, z: -10,
    name: 'AGENT COLONY',
    shortName: 'AGENTS',
    purpose: 'SPECIALISTS + HANDOFFS',
    color: '#94c5ff',
  }),
  missions: Object.freeze({
    x: 335, y: 155, z: 15,
    name: 'MISSION CONTROL',
    shortName: 'MISSIONS',
    purpose: 'BOUNDED WORK',
    color: '#76e0c2',
  }),
  markets: Object.freeze({
    x: 390, y: -160, z: 30,
    name: 'MARKET DECK',
    shortName: 'MARKETS',
    purpose: 'VENUES + FORECASTS',
    color: '#c5a3f0',
  }),
  research: Object.freeze({
    x: -25, y: -190, z: 35,
    name: 'EVIDENCE VAULT',
    shortName: 'EVIDENCE',
    purpose: 'RUNS + LESSONS + PROOF',
    color: '#f1bd84',
  }),
  resources: Object.freeze({
    x: -395, y: -160, z: -25,
    name: 'TREASURY & GATEWAY',
    shortName: 'TREASURY',
    purpose: 'WALLETS + POLICY TOOLS',
    color: '#9cbadb',
  }),
});

export const HABITAT_FAR_LAYOUT = Object.freeze({
  providers: Object.freeze([.28, .20]),
  agents: Object.freeze([.72, .20]),
  missions: Object.freeze([.28, .50]),
  markets: Object.freeze([.72, .50]),
  research: Object.freeze([.28, .80]),
  resources: Object.freeze([.72, .80]),
});

export const HABITAT_ZONE_TYPES = Object.freeze({
  providers: Object.freeze(['provider']),
  agents: Object.freeze(['core', 'agent']),
  missions: Object.freeze(['mission']),
  markets: Object.freeze(['market', 'forecast']),
  research: Object.freeze(['evidence', 'session', 'experiment', 'lesson']),
  resources: Object.freeze(['wallet', 'tool']),
});

export function habitatZoneKey(node) {
  return ({
    provider: 'providers',
    agent: 'agents',
    mission: 'missions',
    market: 'markets',
    forecast: 'markets',
    wallet: 'resources',
    tool: 'resources',
  })[node?.type] ?? 'research';
}

export function habitatSlotPosition(zoneKey, slot) {
  const zone = HABITAT_ZONES[zoneKey] ?? HABITAT_ZONES.research;
  const safeSlot = Math.max(0, Number.isFinite(Number(slot)) ? Number(slot) : 0);
  const angle = safeSlot * 2.399963;
  const radius = 29 * Math.sqrt(safeSlot);
  return {
    x: zone.x + Math.cos(angle) * radius,
    y: zone.y + Math.sin(angle) * radius * .8,
    z: zone.z + (safeSlot % 3) * 9,
  };
}

export function habitatDeckCorners(zoneKey, entityCount = 1) {
  const zone = HABITAT_ZONES[zoneKey] ?? HABITAT_ZONES.research;
  const count = Math.max(1, Number(entityCount) || 1);
  const halfWidth = Math.max(78, 34 * Math.sqrt(count) + 48);
  const halfDepth = Math.max(50, halfWidth * .62);
  const z = zone.z - 8;
  return [
    { x: zone.x - halfWidth, y: zone.y - halfDepth, z },
    { x: zone.x + halfWidth, y: zone.y - halfDepth, z },
    { x: zone.x + halfWidth, y: zone.y + halfDepth, z },
    { x: zone.x - halfWidth, y: zone.y + halfDepth, z },
  ];
}
