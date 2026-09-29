// Lightweight navigable spatial view. All vertices and edges are projected from
// the same bounded /api/operations snapshot rendered by the workstation.
const esc = (value) => String(value ?? 'Unknown');
const palette = { core: '#d7f8ff', mission: '#58f0ce', agent: '#70baff', provider: '#61e3ef', wallet: '#ffca73', tool: '#b2c8ff', experiment: '#cf9dff', lesson: '#b48cff', session: '#91a6bb', evidence: '#ffad76', market: '#5ce4d0', forecast: '#d1a6ff' };
const statePalette = { healthy: '#58f0ce', degraded: '#ffc367', offline: '#ff637c', disabled: '#ffae62', unknown: '#7588a4', observed: '#73baff' };

function stamp(value) {
  const n = Date.parse(value ?? '');
  return Number.isFinite(n) ? n : 0;
}

// forecast_ledger.created_at uses SQLite CURRENT_TIMESTAMP, which is UTC but lacks a suffix.
function forecastStamp(value) {
  if (typeof value !== 'string') return 0;
  return stamp(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value.replace(' ', 'T')}Z`);
}

function stateForStatus(status) {
  const value = String(status ?? 'unknown').toLowerCase();
  if (value.includes('offline') || value === 'stopped') return 'offline';
  if (value.includes('degraded') || value.includes('failed') || value.includes('invalid')
      || value.includes('quarantined') || value.includes('unavailable')) return 'degraded';
  if (value.includes('disabled')) return 'disabled';
  if (value.includes('running') || value.includes('claimed') || value.includes('healthy')
      || value.includes('connected') || value === 'active') return 'healthy';
  if (value === 'unknown' || value === 'stale' || value === '') return 'unknown';
  return 'observed';
}

function buildModel(snapshot, cutoff = Infinity, capabilities = {}) {
  const sections = snapshot?.sections ?? {};
  const rows = (key) => sections[key]?.rows ?? [];
  const replaying = Number.isFinite(cutoff);
  const missions = rows('missions').filter((m) => stamp(m.created_at) <= cutoff && (replaying || m.status !== 'superseded'));
  const events = rows('mission_events').filter((e) => stamp(e.created_at) <= cutoff);
  const handoffs = rows('handoffs').filter((h) => stamp(h.created_at) <= cutoff);
  const runs = rows('research_runs').filter((r) => stamp(r.created_at) <= cutoff);
  const sessions = rows('sessions').filter((s) => stamp(s.created_at) <= cutoff);
  const decisions = rows('decisions').slice(0, 12).filter((d) => {
    const at = forecastStamp(d.created_at);
    return at > 0 && at <= cutoff;
  });
  const runtimeState = replaying ? 'historical participant' : snapshot?.runtime?.state ?? 'unknown';
  const operationsStale = !replaying && capabilities.freshness?.operations === 'stale';
  const nodes = [{ id: 'agent:NOEMA', type: 'core', label: 'NOEMA',
    status: operationsStale ? `degraded · stale runtime snapshot (${runtimeState})` : runtimeState,
    stateKind: replaying ? 'observed' : operationsStale ? 'degraded' : runtimeState === 'running' ? 'healthy' : runtimeState === 'stale' ? 'degraded' : runtimeState === 'stopped' ? 'offline' : 'unknown',
    rawStatus: runtimeState, metadata: 'Persistent primary agent identity',
    capabilities: replaying ? ['Identity in historical replay'] : ['Cognitive planning and bounded research when runtime is enabled'],
    limits: ['Cannot grant itself permissions or override deterministic risk policy'],
    source: replaying ? 'Persisted runtime history' : operationsStale ? 'Stale operations snapshot; last recorded heartbeat' : 'Persisted runtime heartbeat',
    observedAt: replaying ? null : snapshot?.runtime?.last_heartbeat_at, created: 0 }];
  const edges = [];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const put = (node) => {
    if (!node.id || byId.has(node.id)) return;
    node.stateKind ??= stateForStatus(node.status);
    byId.set(node.id, node); nodes.push(node);
  };
  const specialistNames = new Set();
  if (replaying) {
    for (const event of events) if (event.actor && event.actor !== 'NOEMA') specialistNames.add(event.actor);
  } else {
    for (const mission of missions) if (mission.specialist) specialistNames.add(mission.specialist);
  }
  for (const handoff of handoffs) {
    if (handoff.from_specialist) specialistNames.add(handoff.from_specialist);
    if (handoff.to_specialist) specialistNames.add(handoff.to_specialist);
  }
  for (const decision of decisions) {
    const marketId = `${decision.venue}:${decision.market_id}`;
    const marketKey = `market:${marketId}`;
    const forecastKey = `forecast:${decision.id}`;
    const at = forecastStamp(decision.created_at);
    put({ id: marketKey, type: 'market', label: `${decision.venue} · ${decision.market_id.slice(-12)}`,
      status: 'forecast ledger reference', created: at, record: { venue: decision.venue, market_id: decision.market_id, created_at: decision.created_at } });
    put({ id: forecastKey, type: 'forecast', label: `${decision.decision?.toUpperCase() ?? 'RECORDED'} · ${decision.market_id.slice(-12)}`,
      status: decision.decision ?? 'recorded', created: at, record: decision });
    edges.push({ from: marketKey, to: forecastKey, type: 'venue + market id', at });
    edges.push({ from: 'agent:NOEMA', to: forecastKey, type: 'immutable forecast record', at });
  }
  if (!replaying) {
    const providerHealth = capabilities.providers ?? {};
    const providerAliases = {
      cloudflare: ['cloudflare', 'cloudflare_workers_ai'],
      openai: ['openai'],
      groq: ['groq'], foundry: ['foundry'],
      docker_model_runner: ['docker_model_runner', 'local', 'local_model_runner'],
      chronos: ['chronos'], finbert: ['finbert'],
    };
    const configuredRoute = String(providerHealth.configured_provider ?? '').toLowerCase();
    for (const [key, data] of Object.entries(providerHealth)) {
      if (!data || typeof data !== 'object' || !('status' in data)) continue;
      const raw = String(data.status ?? 'unknown').toLowerCase();
      const isConfiguredRoute = (providerAliases[key] ?? [key]).includes(configuredRoute);
      const sourceFreshness = capabilities.freshness?.providers ?? 'unknown';
      const configurationOnly = ['ready', 'configured', 'not_configured'].includes(raw);
      const state = sourceFreshness === 'stale' ? 'degraded' : sourceFreshness !== 'current' ? 'unknown'
        : configurationOnly ? 'unknown' : raw === 'healthy' ? 'healthy'
        : raw === 'offline' ? 'offline'
          : ['unknown', ''].includes(raw) ? 'unknown' : 'degraded';
      const label = ({ cloudflare: 'Cloudflare Workers AI', openai: 'OpenAI',
        docker_model_runner: 'Local model runner', chronos: 'Chronos service',
        finbert: 'FinBERT service', foundry: 'Azure AI Foundry' })[key] ?? key.replaceAll('_', ' ');
      const metadata = [sourceFreshness === 'stale' ? 'stale health snapshot' : sourceFreshness === 'unavailable' ? 'health check unavailable' : null,
        data.model ? `model ${data.model}` : null,
        data.selected_model ? `selected ${data.selected_model}` : null,
        data.model_available === true ? 'configured model available' : null,
        isConfiguredRoute && providerHealth.configured_provider_ready === true ? 'route configuration ready · no inference request made' : null,
        data.selected_model_resource_eligible === false ? 'model resource limited' : null,
        raw.replaceAll('_', ' ')].filter(Boolean).join(' · ');
      const providerCapabilities = isConfiguredRoute && providerHealth.configured_provider_ready === true
        ? ['Configured cognition inference route · connectivity not verified']
        : key === 'docker_model_runner' && data.selected_model_capabilities?.length
          ? data.selected_model_capabilities.map((capability) => `Selected model metadata · ${capability}`)
          : ['Provider health observation'];
      const providerLimits = [];
      if (!isConfiguredRoute) providerLimits.push('Not the configured cognition route');
      if (raw === 'credential_present_unwired') providerLimits.push('Credential present; provider route is not wired');
      if (data.selected_model_resource_reason) providerLimits.push(String(data.selected_model_resource_reason));
      providerLimits.push('Configuration or health does not prove forecast quality or grant wallet / venue authority');
      const displayStatus = raw === 'ready' ? 'configured · reachability unverified'
        : raw === 'configured' ? 'configured · health not probed'
          : raw === 'not_configured' ? 'not configured' : state;
      put({ id: `provider:${key}`, type: 'provider', label, status: displayStatus,
        stateKind: state, rawStatus: raw, created: 0, capabilities: providerCapabilities,
        limits: providerLimits,
        metadata: metadata || 'Provider state observed', source: 'Live provider health projection',
        observedAt: capabilities.freshness?.providersAt ?? null, record: { provider: key, status: raw } });
    }
    const route = configuredRoute;
    if (route) {
      const providerKey = Object.entries(providerAliases).find(([, aliases]) => aliases.includes(route))?.[0]
        ?? (byId.has(`provider:${route}`) ? route : null);
      if (providerKey && byId.has(`provider:${providerKey}`)) {
        edges.push({ from: 'agent:NOEMA', to: `provider:${providerKey}`,
          type: 'configured cognition route', at: 0 });
      }
    }
    for (const specialist of rows('specialists')) {
      const raw = String(specialist.state ?? 'unknown').toLowerCase();
      const state = operationsStale ? 'degraded' : raw === 'quarantined' ? 'degraded' : ['shadow', 'paper'].includes(raw) ? 'observed'
        : raw === 'active_research' ? 'healthy' : 'unknown';
      put({ id: `agent:${specialist.name}`, type: 'agent', label: specialist.name,
        status: operationsStale ? `degraded · stale registry snapshot (${raw.replaceAll('_', ' ')})` : raw.replaceAll('_', ' '),
        stateKind: state, rawStatus: raw, created: 0,
        capabilities: [`Research family · ${specialist.family ?? 'unknown'}`,
          `Research state · ${raw.replaceAll('_', ' ')}`],
        limits: raw === 'quarantined' ? ['Quarantined; receives no research attention']
          : ['Specialist status does not imply live execution authority'],
        source: 'Persisted specialist registry', observedAt: capabilities.freshness?.operationsAt ?? snapshot.as_of,
        record: specialist });
      edges.push({ from: 'agent:NOEMA', to: `agent:${specialist.name}`,
        type: 'registered specialist capability', at: 0 });
    }
    for (const venue of capabilities.venues?.venues ?? []) {
      const caps = venue.capabilities ?? {};
      const marketStatus = String(venue.market_data?.status ?? 'unknown').toLowerCase();
      const accountStatus = String(venue.account?.status ?? 'unknown').toLowerCase();
      const freshness = capabilities.freshness?.venues ?? 'unknown';
      const state = freshness === 'stale' ? 'degraded' : caps.connected === true ? 'healthy'
        : marketStatus === 'degraded' ? 'degraded' : 'unknown';
      const idPart = String(venue.venue ?? 'unknown').toLowerCase().replace(/[^a-z0-9-]/g, '-');
      const id = `provider:venue:${idPart}`;
      const can = [];
      if (caps.connected === true) can.push('Read official market data');
      if (caps.readable === true) can.push('Read-only market or account observations');
      if (venue.market_data?.order_book_readable === true) can.push('Read observed order-book levels');
      if (!can.length) can.push('Venue status only; no current data access confirmed');
      const metadata = [venue.environment, `market data ${marketStatus}`, `account ${accountStatus}`,
        freshness === 'stale' ? 'stale venue snapshot' : null].filter(Boolean).join(' · ');
      put({ id, type: 'provider', label: `${venue.venue ?? 'Prediction venue'} venue`,
        status: freshness === 'stale' ? 'degraded · stale venue status'
          : caps.connected === true ? 'read-only' : state,
        stateKind: state, rawStatus: `${marketStatus} / ${accountStatus}`, created: 0,
        capabilities: can,
        limits: ['Live execution disabled', 'Read status does not establish executable liquidity or strategy edge'],
        metadata, source: 'Official prediction venue status projection',
        observedAt: capabilities.venues?.as_of ?? capabilities.freshness?.venuesAt ?? null,
        record: { venue: venue.venue, market_status: marketStatus, account_status: accountStatus } });
      if (caps.connected === true || caps.authenticated === true) {
        edges.push({ from: 'agent:NOEMA', to: id,
          type: caps.authenticated === true ? 'read-only venue connection' : 'public market-data connection', at: 0 });
      }
    }
    for (const network of capabilities.wallets?.networks ?? []) {
      const key = `${network.chain ?? 'unknown'}-${network.network ?? 'unknown'}`.toLowerCase().replace(/[^a-z0-9-]/g, '-');
      const connected = network.connected;
      const execution = network.live_execution_enabled === true;
      const sourceFreshness = capabilities.freshness?.wallets ?? 'unknown';
      const state = sourceFreshness === 'stale' ? 'degraded' : connected === false ? 'offline' : connected === true ? (execution ? 'healthy' : 'disabled') : 'unknown';
      const status = sourceFreshness === 'stale' ? 'degraded · stale wallet observation'
        : connected === false ? 'offline' : connected === true ? (execution ? 'read-only' : 'execution disabled') : 'unknown';
      const capabilitiesList = [connected === true ? 'Public chain state observed' : 'Public chain state not confirmed'];
      if (network.readable === true) capabilitiesList.push('Read-only balance and token observations');
      const limits = ['Signing is disabled in this status projection'];
      if (network.mission_authority_present !== true) limits.push('No mission authority recorded');
      if (network.coordinator_wired !== true) limits.push('Wallet coordinator is not wired');
      put({ id: `wallet:${key}`, type: 'wallet', label: `${network.chain ?? 'Wallet'} · ${network.network ?? 'network unknown'}`,
        status, stateKind: state, rawStatus: connected === true ? 'connected' : connected === false ? 'disconnected' : 'unknown',
        created: 0, capabilities: capabilitiesList, limits,
        metadata: [sourceFreshness === 'stale' ? 'stale wallet snapshot' : sourceFreshness === 'unavailable' ? 'wallet check unavailable' : null,
          'balance projection uses a cache up to 60 seconds',
          network.chain_id ? `chain ${network.chain_id}` : null,
          network.signer_configured === true ? 'signer configured' : 'signer not configured',
          network.halted === true ? 'halted' : null].filter(Boolean).join(' · ') || 'Network metadata unavailable',
        source: 'Live wallet status and deterministic policy projection',
        retrievedAt: capabilities.freshness?.walletsAt ?? null,
        record: { chain: key, connected, execution_enabled: execution } });
      edges.push({ from: 'agent:NOEMA', to: `wallet:${key}`,
        type: 'wallet network status observed', at: 0 });
    }
    if (capabilities.gateway) {
      const gatewayStatus = String(capabilities.gateway.status ?? 'unknown');
      const gatewayStale = capabilities.freshness?.gateway === 'stale';
      put({ id: 'tool:execution-gateway', type: 'tool', label: 'Execution gateway',
        status: gatewayStale ? 'degraded · stale gateway status' : capabilities.gateway.live_execution_enabled === true ? 'policy gated' : 'execution disabled',
        stateKind: gatewayStale ? 'degraded' : capabilities.gateway.live_execution_enabled === true ? 'healthy' : 'disabled',
        rawStatus: gatewayStatus, created: 0,
        capabilities: ['Deterministic proposal and policy review'],
        limits: ['Does not grant authority; requests must pass current deterministic policy'],
        metadata: `${gatewayStale ? 'stale snapshot · ' : ''}${gatewayStatus}`, source: 'Live execution gateway status projection',
        observedAt: capabilities.freshness?.gatewayAt ?? null, record: { status: gatewayStatus } });
      edges.push({ from: 'agent:NOEMA', to: 'tool:execution-gateway', type: 'policy-gated action route', at: 0 });
    }
  }
  for (const name of specialistNames) {
    const assignment = replaying ? null : missions.find((mission) => mission.specialist === name && ['claimed', 'running', 'waiting'].includes(mission.status));
    const status = replaying ? 'historical participant' : assignment?.status ?? 'idle · no open mission';
    put({ id: `agent:${name}`, type: 'agent', label: name, status, stateKind: replaying ? 'observed' : 'unknown', created: Infinity,
      capabilities: ['Recorded mission participation'], limits: ['No additional permissions are implied by past participation'],
      source: 'Persisted mission or handoff record' });
  }
  for (const mission of missions) {
    const id = `mission:${mission.mission_id}`;
    const lineage = events.filter((event) => event.mission_id === mission.mission_id)
      .sort((a, b) => stamp(a.created_at) - stamp(b.created_at) || Number(a.id) - Number(b.id));
    const knownTransition = lineage.at(-1);
    const missionStatus = replaying ? knownTransition?.status ?? 'discovered' : mission.status;
    const missionAgent = replaying ? lineage.find((event) => event.event_type === 'claimed')?.actor ?? null : mission.specialist;
    const safeMission = replaying ? { ...mission, status: missionStatus, specialist: missionAgent,
      updated_at: mission.created_at, result: null, measurement: null, completed_at: null,
      lesson_id: rows('lessons').some((lesson) => lesson.id === mission.lesson_id && stamp(lesson.created_at) <= cutoff) ? mission.lesson_id : null } : mission;
    put({ id, type: 'mission', label: mission.objective ?? mission.mission_id, status: missionStatus,
      created: stamp(mission.created_at), missionId: mission.mission_id, record: safeMission });
    if (missionAgent) edges.push({ from: `agent:${missionAgent}`, to: id, type: 'assigned specialist', at: stamp(mission.created_at), missionId: mission.mission_id });
    if (lineage.some((event) => event.actor === 'NOEMA' && event.event_type === 'opportunity_discovered')) {
      edges.push({ from: 'agent:NOEMA', to: id, type: 'opportunity discovered', at: stamp(mission.created_at), missionId: mission.mission_id });
    }
    if (mission.evidence_hash) {
      const evidenceId = `evidence:${mission.evidence_hash}`;
      put({ id: evidenceId, type: 'evidence', label: `Frozen evidence · ${mission.evidence_hash.slice(0, 12)}`,
        status: 'hash linked', created: stamp(mission.created_at), missionId: mission.mission_id,
        record: { mission_id: mission.mission_id, evidence_hash: mission.evidence_hash, created_at: mission.created_at } });
      edges.push({ from: id, to: evidenceId, type: 'frozen evidence hash', at: stamp(mission.created_at), missionId: mission.mission_id });
    }
    if (mission.session_id && sessions.some((s) => s.session_id === mission.session_id)) {
      const session = sessions.find((s) => s.session_id === mission.session_id);
      const sid = `session:${session.session_id}`;
      const sessionStatus = replaying && session.completed_at && stamp(session.completed_at) > cutoff ? 'running' : session.status;
      const safeSession = replaying ? { ...session, status: sessionStatus, completed_at: sessionStatus === 'running' ? null : session.completed_at, result: null } : session;
      put({ id: sid, type: 'session', label: `Session ${session.session_id.slice(0, 8)}`, status: sessionStatus,
        created: stamp(session.created_at), missionId: mission.mission_id, record: safeSession });
      edges.push({ from: id, to: sid, type: 'exact session id', at: stamp(mission.created_at), missionId: mission.mission_id });
    }
    if (mission.trial_id && runs.some((r) => r.trial_id === mission.trial_id)) {
      const run = runs.find((r) => r.trial_id === mission.trial_id);
      const rid = `experiment:${mission.trial_id}`;
      const runStatus = replaying && run.completed_at && stamp(run.completed_at) > cutoff ? 'running' : run.status;
      const safeRun = replaying ? { ...run, status: runStatus, completed_at: runStatus === 'running' ? null : run.completed_at, result: null } : run;
      put({ id: rid, type: 'experiment', label: `${run.kind} · ${mission.trial_id.slice(0, 8)}`, status: runStatus,
        created: stamp(run.created_at), missionId: mission.mission_id, record: safeRun });
      edges.push({ from: id, to: rid, type: 'exact trial id', at: stamp(run.created_at), missionId: mission.mission_id });
    }
    if (mission.lesson_id) {
      const lesson = rows('lessons').find((item) => item.id === mission.lesson_id);
      if (lesson && stamp(lesson.created_at) <= cutoff) {
        const lid = `lesson:${lesson.id}`;
        put({ id: lid, type: 'lesson', label: `Lesson ${lesson.id}`, status: 'persisted', created: stamp(lesson.created_at), missionId: mission.mission_id, record: lesson });
        edges.push({ from: id, to: lid, type: 'lesson recorded', at: stamp(lesson.created_at), missionId: mission.mission_id });
      }
    }
  }
  for (const handoff of handoffs) {
    const mission = missions.find((m) => m.mission_id === handoff.mission_id);
    if (!mission) continue;
    const handoffStatus = replaying && stamp(handoff.updated_at) > cutoff ? 'requested' : handoff.status;
    edges.push({ from: `agent:${handoff.from_specialist}`, to: `agent:${handoff.to_specialist}`,
      type: `handoff · ${handoffStatus}`, at: stamp(handoff.created_at), missionId: handoff.mission_id });
  }
  const timeline = [
    ...events.map((event) => ({ id: `mission-event:${event.id}`, at: stamp(event.created_at), time: event.created_at,
      title: event.event_type.replaceAll('_', ' '), detail: event.detail, status: event.status,
      actor: event.actor, missionId: event.mission_id, kind: 'MISSION EVENT', source: event })),
    ...rows('activity').map((event) => ({ id: `runtime-event:${event.id}`, at: stamp(event.created_at), time: event.created_at,
      title: `${event.stage} · ${event.status}`, detail: event.detail, status: event.status,
      actor: event.tool ?? 'NOEMA', missionId: event.mission_id ?? null, kind: 'RUNTIME EVENT', source: event })),
    ...decisions.map((decision) => ({ id: `forecast-event:${decision.id}`, at: forecastStamp(decision.created_at), time: decision.created_at,
      title: `forecast · ${decision.venue} · ${decision.market_id}`, detail: `${decision.decision ?? 'recorded'} · ${decision.model_version ?? 'model unknown'} · p=${decision.probability_yes ?? 'unknown'}`,
      status: decision.decision ?? 'recorded', actor: decision.model_version ?? 'NOEMA', missionId: null, kind: 'FORECAST LEDGER', source: decision })),
  ].filter((e) => e.at && e.at <= cutoff).sort((a, b) => a.at - b.at || a.id.localeCompare(b.id));
  const toolEvents = rows('activity').filter((event) => event.tool && stamp(event.created_at) <= cutoff);
  const latestTools = new Map();
  for (const event of toolEvents) {
    const key = String(event.tool).slice(0, 120);
    if (!latestTools.has(key)) latestTools.set(key, event);
  }
  for (const [name, event] of [...latestTools].slice(0, 16)) {
    const raw = String(event.status ?? 'unknown').toLowerCase();
    const state = operationsStale ? 'degraded' : ['failed', 'error', 'rejected'].includes(raw) ? 'degraded'
      : ['running', 'started'].includes(raw) ? 'healthy'
        : raw === 'unknown' ? 'unknown' : 'observed';
    const id = `tool:${name}`;
    put({ id, type: 'tool', label: name,
      status: operationsStale ? `degraded · stale runtime event (${raw})` : state === 'healthy' ? 'active' : state === 'observed' ? 'recorded' : state,
      stateKind: state, rawStatus: raw, created: stamp(event.created_at), capabilities: [`Observed at runtime stage · ${String(event.stage ?? 'unknown').slice(0, 80)}`],
      limits: ['A recorded invocation does not establish broader permissions or current availability'],
      metadata: `Latest recorded stage · ${event.stage ?? 'unknown'} · ${raw}`,
      source: 'Persisted runtime event', observedAt: event.created_at, record: event });
    const missionId = event.mission_id ? `mission:${event.mission_id}` : null;
    const related = missionId && byId.has(missionId) ? missionId : 'agent:NOEMA';
    edges.push({ from: related, to: id, type: missionId && related === missionId ? 'mission tool event' : 'recorded tool invocation', at: stamp(event.created_at), missionId: event.mission_id });
  }
  return { nodes, edges: edges.filter((e) => byId.has(e.from) && byId.has(e.to) && (!e.at || e.at <= cutoff)), timeline };
}

function project(node, width, height, camera) {
  const fit = Math.min((width - 56) / 1240, (height - 64) / 590);
  const scale = Math.max(.12, fit) * camera.zoom;
  const yaw = camera.orbitX, pitch = camera.orbitY;
  const x = node.x * Math.cos(yaw) - node.z * Math.sin(yaw);
  const z = node.x * Math.sin(yaw) + node.z * Math.cos(yaw);
  const y = node.y * Math.cos(pitch) - z * Math.sin(pitch);
  const depth = node.y * Math.sin(pitch) + z * Math.cos(pitch);
  const perspective = 1040 / Math.max(640, 1040 - depth * scale * .42);
  return { x: width / 2 + x * scale * perspective + camera.panX,
    y: height / 2 - y * scale * perspective + camera.panY, scale: scale * perspective, depth };
}

export function createNoemaWorld() {
  const canvas = document.getElementById('world-map-canvas');
  if (!canvas) return { update() {} };
  const ctx = canvas.getContext('2d', { alpha: false });
  if (!ctx) return { update() {} };
  const controls = {
    range: document.getElementById('world-time-range'),
    list: document.getElementById('world-entity-list'),
    camera: { zoom: 1, panX: 0, panY: 0, orbitX: -.12, orbitY: .16 },
  };
  let source = null, nodes = [], edges = [], timeline = [], selectedId = 'agent:NOEMA', focusNodeId = 'agent:NOEMA', replayIndex = null, dpr = 1;
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const setInspector = (node) => {
    if (!node) return;
    selectedId = node.id;
    focusNodeId = node.missionId ? `mission:${node.missionId}` : node.id;
    document.getElementById('world-inspector-kind').textContent = node.type.toUpperCase();
    document.getElementById('world-inspector-title').textContent = node.label;
    const mission = node.record?.mission_id ?? node.missionId;
    const time = node.record?.updated_at ?? node.record?.created_at;
    const timeAt = ['market', 'forecast'].includes(node.type) ? forecastStamp(time) : stamp(time);
    document.getElementById('world-inspector-detail').textContent = [mission ? `Mission · ${mission}` : null,
      timeAt ? `Recorded · ${new Date(timeAt).toLocaleString()}` : time ? 'Recorded · time unavailable' : null,
      node.record?.evidence_hash ? `Evidence hash · ${node.record.evidence_hash}` : null,
    ].filter(Boolean).join(' · ') || 'Inspect the live status and evidence for this node.';
    const facts = document.getElementById('world-inspector-facts');
    facts.replaceChildren();
    const addFact = (term, detail) => {
      if (!detail) return;
      const dt = document.createElement('dt'); dt.textContent = term;
      const dd = document.createElement('dd'); dd.textContent = detail;
      facts.append(dt, dd);
    };
    addFact('STATUS', `${node.status ?? 'unknown'}${node.rawStatus ? ` · source: ${node.rawStatus}` : ''}`);
    addFact('TYPE METADATA', node.metadata);
    if (node.capabilities?.length) addFact('CAN', node.capabilities.join(' · '));
    if (node.limits?.length) addFact('CANNOT / LIMITS', node.limits.join(' · '));
    const relationships = edges.filter((edge) => edge.from === node.id || edge.to === node.id)
      .map((edge) => {
        const otherId = edge.from === node.id ? edge.to : edge.from;
        const other = nodes.find((candidate) => candidate.id === otherId);
        return `${edge.type} · ${other?.label ?? otherId}`;
      }).slice(0, 12);
    if (relationships.length) addFact('RELATIONSHIPS', relationships.join(' · '));
    addFact('EVIDENCE SOURCE', node.source);
    if (node.observedAt) addFact('OBSERVED', new Date(node.observedAt).toLocaleString());
    if (node.retrievedAt) addFact('RESPONSE RETRIEVED', new Date(node.retrievedAt).toLocaleString());
    controls.list.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.nodeId === node.id)));
    draw();
  };
  function resize() {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    canvas.width = Math.round(rect.width * dpr); canvas.height = Math.round(rect.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }
  function draw() {
    const rect = canvas.getBoundingClientRect(); if (!rect.width || !rect.height) return;
    const width = rect.width, height = rect.height;
    const core = nodes.find((node) => node.type === 'core');
    const rest = nodes.filter((node) => node !== core);
    for (let i = 0; i < rest.length; i++) {
      const node = rest[i], ring = ({ agent: 140, provider: 205, wallet: 255, mission: 300, tool: 350, market: 390, experiment: 425,
        session: 400, forecast: 445, evidence: 485, lesson: 520 }[node.type] ?? 350);
      const same = rest.slice(0, i).filter((item) => item.type === node.type).length;
      const typeCount = Math.max(1, rest.filter((item) => item.type === node.type).length);
      const angle = same / typeCount * Math.PI * 2 - Math.PI / 2;
      node.x = Math.cos(angle) * ring; node.y = Math.sin(angle) * ring * .48;
      node.z = ({ agent: -105, provider: -145, wallet: -85, mission: 20, tool: 65, market: 100,
        experiment: 145, session: 175, forecast: 205, evidence: 245, lesson: 275 }[node.type] ?? 0);
    }
    if (core) { core.x = 0; core.y = 0; core.z = 0; }
    const positions = new Map(nodes.map((node) => [node.id, project(node, width, height, controls.camera)]));
    const sorted = [...nodes].sort((a, b) => (positions.get(a.id)?.depth ?? 0) - (positions.get(b.id)?.depth ?? 0));
    for (const node of sorted) node.screen = positions.get(node.id);
    const activeEdges = edges.filter((edge) => edge.from === focusNodeId || edge.to === focusNodeId);
    const neighborhood = new Set([focusNodeId, ...activeEdges.map((edge) => edge.from), ...activeEdges.map((edge) => edge.to)]);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = '#080d18'; ctx.fillRect(0, 0, width, height);
    for (const edge of edges) {
      const from = positions.get(edge.from), to = positions.get(edge.to); if (!from || !to) continue;
      ctx.beginPath(); ctx.moveTo(from.x, from.y); ctx.lineTo(to.x, to.y);
      const active = activeEdges.includes(edge);
      const perspective = Math.max(.12, ((from.scale + to.scale) / 2) / Math.max(.12, Math.min(from.scale, to.scale)));
      ctx.strokeStyle = active ? (edge.type.startsWith('handoff') ? '#cf9dffed' : '#58f0cedd') : '#6778942d';
      ctx.lineWidth = active ? 1.4 * perspective : .75; ctx.stroke();
    }
    for (const node of sorted) {
      const p = positions.get(node.id); if (!p || p.x < -30 || p.y < -30 || p.x > width + 30 || p.y > height + 30) continue;
      const color = palette[node.type] ?? '#91a3aa', radius = (node.type === 'core' ? 14 : node.type === 'mission' ? 9 : 7) * Math.min(1.9, p.scale * 1.65);
      const focused = focusNodeId === node.id;
      const neighborhoodNode = neighborhood.has(node.id);
      const earned = node.stateKind === 'healthy';
      ctx.save();
      ctx.globalAlpha = neighborhoodNode ? 1 : .2;
      ctx.shadowColor = statePalette[node.stateKind] ?? '#7588a4';
      ctx.shadowBlur = focused || earned ? (focused ? 16 : 9) : 0;
      ctx.beginPath(); ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
      const sphere = ctx.createRadialGradient(p.x - radius * .34, p.y - radius * .4, radius * .08, p.x, p.y, radius * 1.05);
      sphere.addColorStop(0, '#ffffff'); sphere.addColorStop(.18, color); sphere.addColorStop(1, '#111827');
      ctx.fillStyle = sphere; ctx.fill(); ctx.shadowBlur = 0;
      ctx.beginPath(); ctx.arc(p.x, p.y, radius + (focused ? 4 : 0), 0, Math.PI * 2);
      ctx.strokeStyle = focused ? '#eafaff' : (statePalette[node.stateKind] ?? '#7588a4');
      ctx.lineWidth = focused ? 1.6 : 1; ctx.stroke();
      ctx.restore();
      if (!neighborhood.has(node.id)
          || (width <= 520 && node.id !== selectedId && node.id !== 'agent:NOEMA')) continue;
      ctx.globalAlpha = 1;
      ctx.fillStyle = '#e3edff'; ctx.font = `${node.type === 'core' ? '11px' : '9px'} ui-monospace, monospace`;
      const label = node.label.length > 34 ? `${node.label.slice(0, 32)}…` : node.label; ctx.fillText(label, p.x + radius + 5, p.y + 3);
    }
    canvas._hitNodes = sorted;
    const state = source?.runtime?.state ?? 'unknown';
    const capabilityCount = nodes.filter((node) => ['provider', 'wallet', 'tool'].includes(node.type)).length;
    const agentCount = nodes.filter((node) => node.type === 'agent').length;
    const freshness = capabilitySources.freshness ?? {};
    const sourceState = replayIndex === null
      ? `checks · records ${freshness.operations ?? 'unknown'} · providers ${freshness.providers ?? 'unknown'} · venues ${freshness.venues ?? 'unknown'} · wallets ${freshness.wallets ?? 'unknown'} · gateway ${freshness.gateway ?? 'unknown'}`
      : `historical replay · live-only checks hidden · records ${freshness.operations ?? 'unknown'}`;
    document.getElementById('world-map-state').textContent = `${capabilityCount} capability nodes · ${agentCount} agents · ${edges.length} observed or persisted links · ${sourceState} · NOEMA ${state}`;
  }
  function setTime(index) {
    replayIndex = index;
    const cutoff = index === null || !timeline.length ? Infinity : timeline[index]?.at ?? Infinity;
    const model = buildModel(source, cutoff, capabilitySources); nodes = model.nodes; edges = model.edges;
    document.getElementById('world-time-value').textContent = index === null ? 'Latest known state' : new Date(cutoff).toLocaleString();
    document.getElementById('world-time-count').textContent = `${timeline.length} loaded persisted events · ${index === null ? 'following live state' : `as of event ${index + 1}/${timeline.length}`}`;
    const eventList = document.getElementById('world-event-list'); eventList.replaceChildren();
    for (const [eventIndex, event] of timeline.slice(-12).entries()) {
      const absoluteIndex = timeline.length - Math.min(12, timeline.length) + eventIndex;
      const button = document.createElement('button'); button.type = 'button'; button.dataset.eventIndex = String(absoluteIndex);
      button.setAttribute('aria-pressed', String(index === absoluteIndex));
      const time = new Date(event.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      button.textContent = `${time} · ${event.actor} · ${event.title}`;
      button.onclick = () => {
        controls.range.value = String(absoluteIndex); setTime(absoluteIndex);
        const missionNode = nodes.find((node) => node.missionId === event.missionId);
        if (missionNode) selectedId = missionNode.id;
        setInspector({ id: event.id, type: event.kind, label: event.title, status: event.status,
          missionId: event.missionId, record: { created_at: event.time, detail: event.detail,
            mission_id: event.missionId, actor: event.actor, status: event.status } });
      };
      eventList.append(button);
    }
    controls.list.replaceChildren();
    for (const node of nodes) {
      const button = document.createElement('button'); button.type = 'button'; button.dataset.nodeId = node.id;
      button.setAttribute('aria-pressed', String(node.id === selectedId));
      button.textContent = `${node.type.toUpperCase()} · ${node.label} · ${node.status ?? 'unknown'}`;
      button.onclick = () => setInspector(node); controls.list.append(button);
    }
    const legend = document.getElementById('world-type-legend');
    legend.replaceChildren();
    const typeHeading = document.createElement('strong'); typeHeading.textContent = 'TYPE'; legend.append(typeHeading);
    for (const type of new Set(nodes.map((node) => node.type))) {
      const item = document.createElement('span');
      const swatch = document.createElement('i');
      swatch.style.backgroundColor = palette[type] ?? '#91a3aa';
      swatch.setAttribute('aria-hidden', 'true');
      const label = document.createElement('span'); label.textContent = type.toUpperCase();
      item.append(swatch, label); legend.append(item);
    }
    const statusHeading = document.createElement('strong'); statusHeading.textContent = 'STATUS'; legend.append(statusHeading);
    for (const state of new Set(nodes.map((node) => node.stateKind ?? 'unknown'))) {
      const item = document.createElement('span');
      const swatch = document.createElement('i'); swatch.className = 'status-swatch';
      swatch.style.borderColor = statePalette[state] ?? statePalette.unknown;
      swatch.setAttribute('aria-hidden', 'true');
      const label = document.createElement('span'); label.textContent = state.toUpperCase();
      item.append(swatch, label); legend.append(item);
    }
    const found = nodes.find((node) => node.id === selectedId);
    setInspector(found ?? nodes[0]); draw();
  }
  let capabilitySources = {};
  function update(snapshot, sources = capabilitySources) {
    source = snapshot;
    capabilitySources = sources ?? {};
    const all = buildModel(source, Infinity);
    timeline = all.timeline;
    controls.range.max = String(Math.max(0, timeline.length - 1));
    if (replayIndex === null) {
      controls.range.value = String(Math.max(0, timeline.length - 1)); setTime(null);
    } else {
      replayIndex = Math.min(replayIndex, Math.max(0, timeline.length - 1));
      controls.range.value = String(replayIndex); setTime(replayIndex);
    }
  }
  let drag = null, pinchDistance = null;
  const activeTouches = new Map();
  const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  canvas.addEventListener('pointerdown', (event) => {
    canvas.setPointerCapture(event.pointerId);
    if (event.pointerType === 'touch') {
      activeTouches.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (activeTouches.size === 2) {
        const [first, second] = activeTouches.values();
        pinchDistance = distance(first, second);
        drag = null;
        return;
      }
    }
    drag = { pointerId: event.pointerId, x: event.clientX, y: event.clientY,
      startX: event.clientX, startY: event.clientY, button: event.button, pan: event.shiftKey };
  });
  canvas.addEventListener('pointermove', (event) => {
    if (event.pointerType === 'touch' && activeTouches.has(event.pointerId)) {
      activeTouches.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (activeTouches.size >= 2) {
        const [first, second] = activeTouches.values();
        const currentDistance = distance(first, second);
        if (pinchDistance && currentDistance > 0) controls.camera.zoom = Math.max(.55,
          Math.min(1.9, controls.camera.zoom * currentDistance / pinchDistance));
        pinchDistance = currentDistance;
        draw();
        return;
      }
    }
    if (!drag) return;
    if (drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y; drag.x = event.clientX; drag.y = event.clientY;
    if (drag.pan) { controls.camera.panX += dx; controls.camera.panY += dy; }
    else {
      controls.camera.orbitX += dx * .004;
      controls.camera.orbitY = Math.max(-.85, Math.min(.85, controls.camera.orbitY + dy * .004));
    }
    draw();
  });
  canvas.addEventListener('pointerup', (event) => {
    const completedDrag = drag?.pointerId === event.pointerId ? drag : null;
    if (event.pointerType === 'touch') activeTouches.delete(event.pointerId);
    pinchDistance = null;
    if (completedDrag) {
      const moved = Math.abs(event.clientX - completedDrag.startX) + Math.abs(event.clientY - completedDrag.startY);
      if (moved < 4 && event.button === 0) {
      const rect = canvas.getBoundingClientRect(), x = event.clientX - rect.left, y = event.clientY - rect.top;
      const hit = (canvas._hitNodes ?? []).map((node) => ({ node, p: node.screen }))
        .filter(({ p }) => p && Math.hypot(p.x - x, p.y - y) < 22).sort((a, b) => Math.hypot(a.p.x - x, a.p.y - y) - Math.hypot(b.p.x - x, b.p.y - y))[0];
      if (hit) setInspector(hit.node);
      }
    }
    drag = null;
    if (activeTouches.size === 1) {
      const [pointerId, point] = activeTouches.entries().next().value;
      drag = { pointerId, x: point.x, y: point.y, startX: point.x, startY: point.y, button: 0, pan: false };
    }
  });
  canvas.addEventListener('pointercancel', (event) => {
    activeTouches.delete(event.pointerId);
    pinchDistance = null;
    drag = null;
  });
  canvas.addEventListener('contextmenu', (event) => event.preventDefault());
  canvas.addEventListener('wheel', (event) => { event.preventDefault(); controls.camera.zoom = Math.max(.55, Math.min(1.9, controls.camera.zoom * (event.deltaY > 0 ? .92 : 1.08))); draw(); }, { passive: false });
  controls.range.addEventListener('input', () => setTime(Number(controls.range.value)));
  document.getElementById('world-time-live').onclick = () => { replayIndex = null; update(source); };
  document.getElementById('world-reset').onclick = () => { Object.assign(controls.camera, { zoom: 1, panX: 0, panY: 0, orbitX: -.12, orbitY: .16 }); draw(); };
  new ResizeObserver(resize).observe(canvas);
  window.addEventListener('resize', resize);
  if (reducedMotion.matches) canvas.dataset.motion = 'reduced';
  return { update };
}
