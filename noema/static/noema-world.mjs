import { HABITAT_ZONES, HABITAT_FAR_LAYOUT, habitatZoneKey, habitatSlotPosition, habitatDeckCorners } from './world-habitat.mjs';
import { deriveInhabitantActivity, handoffTraversal, inhabitantGlyph } from './world-inhabitants.mjs';
import { workstationDescriptor } from './world-workstations.mjs';
import { deriveMissionOccupancies, deriveActiveMissionLineage, recordedDeliverable } from './world-occupancy.mjs';
import {
  bindAutonomousDesk, deriveShiftPackets, deriveShiftTape, deriveRuleRack,
  deriveKillBoard, deriveShiftReport, hasKillBoardCoverage,
} from './world-desk.mjs';

// Lightweight navigable spatial view. All vertices and edges are projected from
// the same bounded /api/operations snapshot rendered by the workstation.
const esc = (value) => String(value ?? 'Unknown');
const palette = { core: '#d7f8ff', mission: '#58f0ce', agent: '#70baff', provider: '#61e3ef', wallet: '#ffca73', tool: '#b2c8ff', experiment: '#cf9dff', lesson: '#b48cff', session: '#91a6bb', evidence: '#ffad76', deliverable: '#ffd38d', market: '#5ce4d0', forecast: '#d1a6ff' };
const statePalette = { healthy: '#58f0ce', degraded: '#ffc367', offline: '#ff637c', disabled: '#ffae62', stale: '#ff9c73', unknown: '#7588a4', observed: '#73baff' };

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
  if (value.includes('stale') || value.includes('delayed')) return 'stale';
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
    const liveMarket = (capabilities.radar ?? []).find(row => row.market_id === decision.market_id
      && String(row.venue).toLowerCase().replaceAll(' ', '_') === String(decision.venue).toLowerCase().replaceAll(' ', '_'));
    const marketFresh = liveMarket?.freshness_seconds != null && Number(liveMarket.freshness_seconds) <= 300;
    put({ id: marketKey, type: 'market', label: `${decision.venue} · ${decision.market_id.slice(-12)}`,
      status: liveMarket ? `${liveMarket.freshness_seconds == null ? 'quote age unknown' : marketFresh ? 'market data observed' : 'market data stale'} · reference ${liveMarket.market_probability ?? 'unknown'}` : 'no current market quote linked',
      created: at, record: { venue: decision.venue, market_id: decision.market_id, created_at: decision.created_at,
        reference_probability: liveMarket?.market_probability ?? null, yes_bid: liveMarket?.yes_bid ?? null,
        yes_ask: liveMarket?.yes_ask ?? null, freshness_seconds: liveMarket?.freshness_seconds ?? null,
        quote_observed_at: liveMarket?.observed_at ?? null } });
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
        venue.account?.cash_balance_usd == null ? 'cash balance unavailable' : `cash $${venue.account.cash_balance_usd}`,
        venue.account?.capital?.reported_exposure_usd == null ? 'position exposure unavailable' : `reported exposure $${venue.account.capital.reported_exposure_usd}`,
        venue.account?.fills == null ? null : `${venue.account.fills} account fill records`,
        venue.account?.open_orders == null ? null : `${venue.account.open_orders} open orders`,
        freshness === 'stale' ? 'stale venue snapshot' : null].filter(Boolean).join(' · ');
      put({ id, type: 'provider', label: `${venue.venue ?? 'Prediction venue'} venue`,
        status: freshness === 'stale' ? 'degraded · stale venue status'
          : caps.connected === true ? 'read-only' : state,
        stateKind: state, rawStatus: `${marketStatus} / ${accountStatus}`, created: 0,
        capabilities: can,
        limits: ['Live execution disabled', 'Read status does not establish executable liquidity or strategy edge'],
        metadata, source: 'Official prediction venue status projection',
        observedAt: capabilities.venues?.as_of ?? capabilities.freshness?.venuesAt ?? null,
        record: { venue: venue.venue, market_status: marketStatus, account_status: accountStatus,
          cash_balance_usd: venue.account?.cash_balance_usd, observed_at: venue.account?.observed_at,
          exposure_usd: venue.account?.capital?.reported_exposure_usd } });
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
      const state = sourceFreshness === 'stale' ? 'degraded' : connected === false ? 'offline' : connected === true ? 'healthy' : 'unknown';
      const status = sourceFreshness === 'stale' ? 'degraded · stale wallet observation'
        : connected === false ? 'offline' : connected === true ? 'connected · read-only account state' : 'unknown';
      const capabilitiesList = [connected === true ? 'Public chain state observed' : 'Public chain state not confirmed'];
      if (network.readable === true) capabilitiesList.push('Read-only balance and token observations');
      const limits = ['Signing is disabled in this status projection'];
      if (network.mission_authority_present !== true) limits.push('No mission authority recorded');
      if (network.coordinator_wired !== true) limits.push('Wallet coordinator is not wired');
      put({ id: `wallet:${key}`, type: 'wallet', label: `${network.chain ?? 'Wallet'} · ${network.network ?? 'network unknown'}`,
        status, stateKind: state, rawStatus: connected === true ? 'connected' : connected === false ? 'disconnected' : 'unknown',
        created: 0, capabilities: capabilitiesList, limits,
        metadata: [sourceFreshness === 'stale' ? 'stale wallet snapshot' : sourceFreshness === 'unavailable' ? 'wallet check unavailable' : null,
          (network.sol ?? network.native_balance ?? network.btc) != null ? `Native balance ${network.sol ?? network.native_balance ?? network.btc} ${String(network.chain ?? '').toUpperCase()}` : 'Native balance unavailable',
          network.native_value_usd != null ? `Native assets $${Number(network.native_value_usd).toFixed(2)}` : 'USD valuation unavailable',
          'balance projection uses a cache up to 60 seconds',
          network.chain_id ? `chain ${network.chain_id}` : null,
          network.signer_configured === true ? 'signer configured' : 'signer not configured',
          execution ? 'live execution flag enabled; per-action policy still applies' : 'live execution disabled',
          network.halted === true ? 'halted' : null].filter(Boolean).join(' · ') || 'Network metadata unavailable',
        source: 'Live wallet status and deterministic policy projection',
        retrievedAt: capabilities.freshness?.walletsAt ?? null,
        record: { chain: network.chain, connected, execution_enabled: execution,
          native_value_usd: network.native_value_usd, observed_at: network.observed_at,
          native_balance: network.sol ?? network.native_balance ?? network.btc } });
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
    const deliverable = recordedDeliverable({ id, type: 'mission', missionId: mission.mission_id, status: missionStatus, record: safeMission });
    if (deliverable) {
      put({ id: deliverable.id, type: 'deliverable', label: deliverable.label, status: deliverable.deliveryTested ? 'delivery tested' : 'produced · delivery test not recorded',
        created: stamp(deliverable.createdAt), missionId: mission.mission_id,
        capabilities: ['Recorded mission output exists'], limits: ['No file path, download, publication, or economic value is implied by this visualization'],
        metadata: deliverable.deliveryTested ? 'Mission result records deliverable_produced and delivery_tested=true' : 'Mission result records deliverable_produced',
        source: deliverable.source, record: deliverable });
      edges.push({ from: id, to: deliverable.id, type: 'recorded deliverable', at: stamp(deliverable.createdAt), missionId: mission.mission_id });
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
  const canonicalEdges = edges.filter((e) => byId.has(e.from) && byId.has(e.to) && (!e.at || e.at <= cutoff));
  const activityAt = replaying ? cutoff : Date.now();
  for (const node of nodes) {
    if (['core', 'agent'].includes(node.type)) node.activity = deriveInhabitantActivity(node, timeline, canonicalEdges, activityAt);
  }
  return { nodes, edges: canonicalEdges, timeline };
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

export function createNoemaWorld(onSelect = () => {}, onSelectEdge = () => {}) {
  const canvas = document.getElementById('world-map-canvas');
  if (!canvas) return { update() {} };
  const ctx = canvas.getContext('2d', { alpha: false });
  if (!ctx) return { update() {} };
  const controls = {
    range: document.getElementById('world-time-range'),
    list: document.getElementById('world-entity-list'),
    camera: { zoom: 1, panX: 0, panY: 0, orbitX: -.12, orbitY: .16 },
  };
  let source = null, nodes = [], edges = [], timeline = [], selectedId = 'agent:NOEMA', focusNodeId = 'agent:NOEMA', selectedEdge = null, replayIndex = null, dpr = 1;
  let navigation = [['agent:NOEMA']], navigationIndex = 0, viewMode = 'all';
  const hiddenTypes = new Set(), hiddenStatuses = new Set(), pinnedIds = new Set();
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const announceSelection = (node) => onSelect({ ...node, selectedAt: new Date().toISOString() });
  const typeNames = { core: 'Core', agent: 'Agent', mission: 'Mission', market: 'Market', forecast: 'Forecast', provider: 'Provider', wallet: 'Wallet', tool: 'Tool', evidence: 'Evidence', deliverable: 'Deliverable', session: 'Session', experiment: 'Experiment', lesson: 'Lesson' };
  const statusNames = ['active', 'degraded', 'observed', 'healthy', 'stale', 'unknown', 'disabled'];
  const currentRoot = () => navigation[navigationIndex]?.at(-1) ?? 'agent:NOEMA';
  const nodeById = (id) => nodes.find((node) => node.id === id);
  const labelFor = (id) => nodeById(id)?.label ?? id;
  const edgeId = (edge) => edge.id ?? `${edge.from}|${edge.to}|${edge.type}|${edge.at ?? 0}|${edge.missionId ?? ''}`;
  const relativeTime = (value) => { const n = Date.parse(value ?? ''); if (!Number.isFinite(n)) return 'Time unavailable'; const seconds = Math.max(0, Math.floor((Date.now() - n) / 1000)); return seconds < 60 ? `${seconds}s ago` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : seconds < 86400 ? `${Math.floor(seconds / 3600)}h ago` : `${Math.floor(seconds / 86400)}d ago`; };
  function visibleByPreset(node) {
    const status = String(node.status ?? '').toLowerCase();
    const active = ['running', 'claimed', 'waiting', 'active', 'in_progress'].some((word) => status.includes(word));
    switch (viewMode) {
      case 'active': return active;
      case 'agents': return node.type === 'core' || node.type === 'agent';
      case 'money': return node.type === 'wallet' || node.id.startsWith('provider:venue:');
      case 'markets': return ['market', 'forecast'].includes(node.type);
      case 'research': return ['mission', 'evidence', 'session', 'experiment', 'lesson', 'tool'].includes(node.type);
      case 'degraded': return node.stateKind === 'degraded' || node.stateKind === 'offline';
      case 'mission': return node.type === 'mission' && active;
      default: return true;
    }
  }
  function statusForFilter(node, wanted) {
    const status = String(node.status ?? '').toLowerCase();
    if (wanted === 'active') return ['running', 'claimed', 'waiting', 'active', 'in_progress'].some((word) => status.includes(word));
    if (wanted === 'degraded') return ['degraded', 'offline'].includes(node.stateKind);
    if (wanted === 'stale') return status.includes('stale') || status.includes('delayed');
    return node.stateKind === wanted;
  }
  function localIds() {
    if (navigationIndex === 0) return null;
    const root = currentRoot();
    const connected = edges.filter((edge) => edge.from === root || edge.to === root);
    return new Set([root, ...connected.map((edge) => edge.from === root ? edge.to : edge.from)]);
  }
  function visibleNodes() {
    const allowed = localIds();
    return nodes.filter((node) => (!allowed || allowed.has(node.id))
      && !hiddenTypes.has(node.type) && visibleByPreset(node)
      && !statusNames.every((status) => hiddenStatuses.has(status))
      && (hiddenStatuses.size === 0 || statusNames.some((status) => !hiddenStatuses.has(status) && statusForFilter(node, status))));
  }
  function visibleEdges(visible = visibleNodes()) {
    const ids = new Set(visible.map((node) => node.id));
    return edges.filter((edge) => ids.has(edge.from) && ids.has(edge.to));
  }
  function updateNavigationControls() {
    const back = document.getElementById('world-nav-back'), forward = document.getElementById('world-nav-forward');
    back.disabled = navigationIndex <= 0; forward.disabled = navigationIndex >= navigation.length - 1;
    const crumbs = document.getElementById('world-breadcrumbs'); crumbs.replaceChildren();
    const path = navigation[navigationIndex] ?? ['agent:NOEMA'];
    let priorCluster = null;
    for (const [index, id] of path.entries()) {
      if (index) { const slash = document.createElement('span'); slash.textContent = '/'; slash.setAttribute('aria-hidden', 'true'); crumbs.append(slash); }
      const node = nodeById(id), cluster = node ? clusterKey(node) : null;
      if (index && cluster && cluster !== priorCluster) {
        const section = document.createElement('span'); section.className = 'world-breadcrumb-category';
        section.textContent = HABITAT_ZONES[cluster]?.name ?? cluster;
        crumbs.append(section);
        const slash = document.createElement('span'); slash.textContent = '/'; slash.setAttribute('aria-hidden', 'true'); crumbs.append(slash);
      }
      const button = document.createElement('button'); button.type = 'button'; button.textContent = labelFor(id);
      button.setAttribute('aria-current', String(index === path.length - 1));
      button.onclick = () => { const next = path.slice(0, index + 1); pushNavigation(next); setInspector(nodeById(id) ?? nodeById('agent:NOEMA')); announceSelection(nodeById(id) ?? nodeById('agent:NOEMA')); };
      crumbs.append(button);
      priorCluster = cluster;
    }
  }
  function pushNavigation(path) {
    navigation = navigation.slice(0, navigationIndex + 1);
    const previous = navigation.at(-1) ?? [];
    if (previous.join('\u0000') !== path.join('\u0000')) navigation.push(path);
    navigationIndex = navigation.length - 1;
    selectedEdge = null;
    updateNavigationControls(); renderEntityList(); draw();
  }
  function setPreset(name) {
    viewMode = name;
    for (const button of document.querySelectorAll('[data-topology-view]')) button.setAttribute('aria-pressed', String(button.dataset.topologyView === name));
    renderEntityList(); draw();
  }
  function togglePin(node) {
    if (pinnedIds.has(node.id)) pinnedIds.delete(node.id);
    else { if (pinnedIds.size >= 4) pinnedIds.delete(pinnedIds.values().next().value); pinnedIds.add(node.id); }
    renderCompare();
  }
  function renderCompare() {
    const host = document.getElementById('world-pinned-entities'), table = document.getElementById('world-compare-table');
    const pinCount = document.getElementById('world-pin-count');
    if (!host || !table || !pinCount) return;
    const pinned = [...pinnedIds].map(nodeById).filter(Boolean);
    pinCount.textContent = String(pinned.length); host.replaceChildren(); table.replaceChildren();
    for (const node of pinned) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = `× ${node.label}`;
      button.setAttribute('aria-label', `Unpin ${node.label}`); button.onclick = () => togglePin(node); host.append(button);
    }
    if (!pinned.length) { const empty = document.createElement('p'); empty.textContent = 'Shift-click up to four entities to compare their state, evidence and relationships.'; table.append(empty); return; }
    const grid = document.createElement('div'); grid.className = 'world-compare-grid';
    for (const node of pinned) {
      const card = document.createElement('article');
      const connected = edges.filter((edge) => edge.from === node.id || edge.to === node.id).length;
      for (const [term, value] of [['TYPE', typeNames[node.type] ?? node.type], ['STATE', node.status ?? 'Unknown'], ['LAST ACTIVITY', node.observedAt ?? node.record?.updated_at ?? node.record?.created_at ?? 'Unknown'], ['LINKS', String(connected)], ['EVIDENCE', node.metadata ?? node.source ?? 'No summary recorded']]) {
        const line = document.createElement('p'); const label = document.createElement('span'); label.textContent = term; const detail = document.createElement('strong'); detail.textContent = term === 'LAST ACTIVITY' ? relativeTime(value) : value; line.append(label, detail); card.append(line);
      }
      grid.append(card);
    }
    table.append(grid);
  }
  function renderEntityList() {
    document.querySelectorAll('#world-type-filters input').forEach((input) => { input.checked = !hiddenTypes.has(input.dataset.filterValue); });
    document.querySelectorAll('#world-status-filters input').forEach((input) => { input.checked = !hiddenStatuses.has(input.dataset.filterValue); });
    controls.list.replaceChildren();
    const shown = visibleNodes();
    for (const node of shown) {
      const button = document.createElement('button'); button.type = 'button'; button.dataset.nodeId = node.id;
      button.setAttribute('aria-pressed', String(node.id === selectedId));
      button.textContent = `${(typeNames[node.type] ?? node.type).toUpperCase()} · ${node.label} · ${node.status ?? 'unknown'}`;
      button.onclick = (event) => { if (event.shiftKey) togglePin(node); selectNode(node); };
      button.ondblclick = () => drillNode(node);
      button.oncontextmenu = (event) => { event.preventDefault(); togglePin(node); };
      controls.list.append(button);
    }
    if (!shown.length) { const empty = document.createElement('p'); empty.className = 'topology-empty'; empty.textContent = 'No canonical entities match this view and filter combination.'; controls.list.append(empty); }
    renderCompare();
  }
  function initializeExplorerControls() {
    const typeHost = document.getElementById('world-type-filters'), statusHost = document.getElementById('world-status-filters');
    const allTypes = Object.keys(typeNames);
    const appendCheck = (host, value, label, hiddenSet) => {
      const wrapper = document.createElement('label'); wrapper.className = 'topology-filter-option';
      const input = document.createElement('input'); input.type = 'checkbox'; input.checked = !hiddenSet.has(value);
      input.dataset.filterValue = value;
      const text = document.createElement('span'); text.textContent = label;
      input.onchange = () => { if (input.checked) hiddenSet.delete(value); else hiddenSet.add(value); renderEntityList(); draw(); };
      wrapper.append(input, text); host.append(wrapper);
    };
    typeHost.replaceChildren(); statusHost.replaceChildren();
    for (const type of allTypes) appendCheck(typeHost, type, typeNames[type], hiddenTypes);
    for (const status of statusNames) appendCheck(statusHost, status, status.replaceAll('_', ' ').toUpperCase(), hiddenStatuses);
    document.querySelectorAll('[data-topology-view]').forEach((button) => button.addEventListener('click', () => setPreset(button.dataset.topologyView)));
    document.getElementById('world-search').addEventListener('input', renderSearchResults);
    document.getElementById('world-search').addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { event.currentTarget.value = ''; document.getElementById('world-search-results').hidden = true; }
      if (event.key === 'Enter') document.getElementById('world-search-results').querySelector('button')?.click();
    });
    document.getElementById('world-nav-back').onclick = () => { if (navigationIndex > 0) { navigationIndex--; focusNodeId = currentRoot(); selectedEdge = null; selectedId = currentRoot(); selectNode(nodeById(currentRoot())); updateNavigationControls(); draw(); } };
    document.getElementById('world-nav-forward').onclick = () => { if (navigationIndex < navigation.length - 1) { navigationIndex++; focusNodeId = currentRoot(); selectedEdge = null; selectedId = currentRoot(); selectNode(nodeById(currentRoot())); updateNavigationControls(); draw(); } };
    document.getElementById('world-pinned-entities').replaceChildren();
    renderCompare(); updateNavigationControls();
  }
  function showCluster(key) {
    const clusterTypes = { providers: ['provider'], agents: ['core', 'agent'], missions: ['mission'], markets: ['market', 'forecast'], research: ['evidence', 'session', 'experiment', 'lesson'], resources: ['wallet', 'tool'] };
    const allowed = new Set(clusterTypes[key] ?? []);
    hiddenTypes.clear();
    for (const type of Object.keys(typeNames)) if (!allowed.has(type)) hiddenTypes.add(type);
    setPreset('all'); controls.camera.zoom = .9; renderEntityList(); draw();
  }
  function pointToSegmentDistance(point, start, end) {
    const dx = end.x - start.x, dy = end.y - start.y;
    const length = dx * dx + dy * dy;
    const t = length ? Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / length)) : 0;
    return Math.hypot(point.x - (start.x + t * dx), point.y - (start.y + t * dy));
  }
  function hitTest(x, y) {
    const nodeHit = (canvas._hitNodes ?? []).map((node) => ({ node, distance: Math.hypot(node.screen.x - x, node.screen.y - y) }))
      .filter(({ node, distance }) => distance < Math.max(14, (node.screen.radius ?? 7) + 5))
      .sort((a, b) => a.distance - b.distance)[0];
    if (nodeHit) return { kind: 'node', value: nodeHit.node };
    const edgeHits = (canvas._hitEdges ?? []).map(({ edge, points }) => ({ edge,
      distance: Math.min(...points.slice(1).map((point, index) => pointToSegmentDistance({ x, y }, points[index], point))) }))
      .filter(({ distance }) => distance < 8).sort((a, b) => a.distance - b.distance);
    if (edgeHits.length) return { kind: 'edge', value: edgeHits[0].edge };
    const cluster = (canvas._hitClusters ?? []).find((item) => x >= item.x && x <= item.x + item.w && y >= item.y && y <= item.y + item.h);
    return cluster ? { kind: 'cluster', value: cluster } : null;
  }
  function pushEdgeContext(edge) {
    const from = nodeById(edge.from), to = nodeById(edge.to);
    const relation = { ...edge, id: edgeId(edge), fromLabel: from?.label ?? edge.from, toLabel: to?.label ?? edge.to };
    onSelectEdge(relation);
  }
  function selectNode(node, { notify = true } = {}) {
    if (!node) return;
    selectedEdge = null; selectedId = node.id; focusNodeId = node.id;
    document.querySelector('[data-inspector-tab="overview"]')?.click();
    setInspector(node); renderEntityList();
    if (notify) announceSelection(node);
  }
  function drillNode(node) {
    if (!node) return;
    selectedEdge = null; selectedId = node.id; focusNodeId = node.id;
    const path = navigation[navigationIndex] ?? ['agent:NOEMA'];
    const next = path.at(-1) === node.id ? path : [...path, node.id];
    pushNavigation(next);
    controls.camera.zoom = Math.max(controls.camera.zoom, 1.5); draw();
    if (node.screen) { controls.camera.panX += canvas.clientWidth / 2 - node.screen.x; controls.camera.panY += canvas.clientHeight / 2 - node.screen.y; }
    setInspector(node); announceSelection(node);
  }
  function inspectEdge(edge, { notify = true } = {}) {
    if (!edge) return;
    selectedEdge = edgeId(edge); focusNodeId = edge.from;
    const from = nodeById(edge.from), to = nodeById(edge.to);
    document.getElementById('world-inspector-kind').textContent = 'RELATIONSHIP';
    document.getElementById('world-inspector-title').textContent = `${from?.label ?? edge.from} → ${to?.label ?? edge.to}`;
    document.getElementById('world-inspector-detail').textContent = edge.type;
    document.getElementById('world-inspector-drill').hidden = true;
    const facts = document.getElementById('world-inspector-facts'); facts.replaceChildren();
    const addFact = (term, detail) => { const dt = document.createElement('dt'); dt.textContent = term; const dd = document.createElement('dd'); dd.textContent = detail; facts.append(dt, dd); };
    addFact('DIRECTION', `${from?.type ?? 'entity'} → ${to?.type ?? 'entity'}`);
    addFact('RELATIONSHIP', edge.type);
    if (edge.missionId) addFact('MISSION', edge.missionId);
    addFact('RECORDED', edge.at ? new Date(edge.at).toLocaleString() : 'Current observed configuration');
    const relationHost = document.getElementById('world-inspector-relationships'); relationHost.replaceChildren();
    relationHost.append(makeRelationshipButton(edge, 'Selected relationship'));
    addEndpointActions(relationHost, edge);
    renderActivity(edge);
    document.getElementById('world-inspector-raw').textContent = JSON.stringify(edge, null, 2);
    selectedId = null; renderEntityList(); draw();
    document.querySelector('[data-inspector-tab="relationships"]')?.click();
    if (notify) pushEdgeContext(edge);
  }
  function makeRelationshipButton(edge, direction) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'topology-relationship';
    const label = document.createElement('strong'); label.textContent = `${labelFor(edge.from)} → ${labelFor(edge.to)}`;
    const detail = document.createElement('span'); detail.textContent = `${direction} · ${edge.type}`;
    button.append(label, detail); button.setAttribute('aria-pressed', String(selectedEdge === edgeId(edge)));
    button.onclick = () => inspectEdge(edge); return button;
  }
  function addEndpointActions(host, edge) {
    const row = document.createElement('div'); row.className = 'topology-edge-endpoints';
    for (const id of [edge.from, edge.to]) {
      const node = nodeById(id); if (!node) continue;
      const button = document.createElement('button'); button.type = 'button'; button.textContent = `Explore ${node.label}`;
      button.onclick = () => { drillNode(node); };
      row.append(button);
    }
    host.append(row);
  }
  function renderNodeRelationships(node) {
    const host = document.getElementById('world-inspector-relationships'); host.replaceChildren();
    const linked = edges.filter((edge) => edge.from === node.id || edge.to === node.id);
    const incoming = linked.filter((edge) => edge.to === node.id), outgoing = linked.filter((edge) => edge.from === node.id);
    for (const [title, group] of [['Incoming', incoming], ['Outgoing', outgoing]]) {
      if (!group.length) continue;
      const heading = document.createElement('h3'); heading.textContent = `${title} · ${group.length}`; host.append(heading);
      for (const edge of group) host.append(makeRelationshipButton(edge, `${title} · ${edge.type}`));
    }
    if (!linked.length) { const empty = document.createElement('p'); empty.textContent = 'No direct relationships are recorded for this entity.'; host.append(empty); }
  }
  function renderActivity(entity) {
    const host = document.getElementById('world-inspector-activity'); if (!host) return;
    host.replaceChildren();
    const missionId = entity.missionId ?? entity.record?.mission_id;
    const name = entity.type === 'agent' ? entity.label : null;
    const marketId = entity.record?.market_id;
    let matches = timeline.filter((event) => (missionId && event.missionId === missionId)
      || (name && event.actor === name) || (marketId && event.source?.market_id === marketId));
    if (entity.type === 'core') matches = timeline;
    const recent = matches.slice(-20).reverse();
    if (!recent.length) { const empty = document.createElement('p'); empty.textContent = 'No persisted activity is linked to this selection.'; host.append(empty); return; }
    for (const event of recent) {
      const item = document.createElement('article'); item.className = 'topology-activity-item';
      const title = document.createElement('strong'); title.textContent = event.title;
      const meta = document.createElement('span'); meta.textContent = `${event.kind} · ${event.actor ?? 'actor unknown'} · ${event.time ?? 'time unavailable'}`;
      const detail = document.createElement('p'); detail.textContent = event.detail ?? event.status ?? 'No event detail recorded.';
      item.append(title, meta, detail); host.append(item);
    }
  }
  function renderSearchResults() {
    const input = document.getElementById('world-search'), host = document.getElementById('world-search-results');
    const query = String(input.value ?? '').trim().toLowerCase(); host.replaceChildren();
    if (!query) { host.hidden = true; return; }
    const terms = query.split(/\s+/).filter(Boolean);
    const matches = nodes.filter((node) => {
      const haystack = `${node.label} ${node.id} ${node.type} ${node.status ?? ''} ${node.record?.chain ?? ''} ${node.record?.venue ?? ''}`.toLowerCase();
      return terms.every((term) => haystack.includes(term));
    }).slice(0, 12);
    if (!matches.length) { const empty = document.createElement('p'); empty.textContent = 'No matching entity in the current canonical graph.'; host.append(empty); }
    for (const node of matches) {
      const button = document.createElement('button'); button.type = 'button'; button.role = 'option';
      button.append(Object.assign(document.createElement('strong'), { textContent: node.label }), Object.assign(document.createElement('span'), { textContent: `${typeNames[node.type] ?? node.type} · ${node.status ?? 'unknown'}` }));
      button.onclick = () => {
        hiddenTypes.clear(); hiddenStatuses.clear(); setPreset('all');
        pushNavigation(node.id === 'agent:NOEMA' ? ['agent:NOEMA'] : ['agent:NOEMA', node.id]);
        selectNode(node); controls.camera.zoom = Math.max(controls.camera.zoom, 1.5); draw();
        const point = node.screen;
        if (point) { controls.camera.panX += canvas.clientWidth / 2 - point.x; controls.camera.panY += canvas.clientHeight / 2 - point.y; }
        input.value = ''; host.hidden = true; draw();
      };
      host.append(button);
    }
    host.hidden = false;
  }
  function setInspector(node) {
    if (!node) return;
    selectedId = node.id; selectedEdge = null;
    focusNodeId = node.id;
    document.getElementById('world-inspector-kind').textContent = (typeNames[node.type] ?? node.type).toUpperCase();
    document.getElementById('world-inspector-title').textContent = node.label;
    document.getElementById('world-inspector-drill').hidden = !edges.some((edge) => edge.from === node.id || edge.to === node.id);
    const mission = node.record?.mission_id ?? node.missionId;
    const time = node.observedAt ?? node.retrievedAt ?? node.record?.updated_at ?? node.record?.created_at;
    const timeAt = ['market', 'forecast'].includes(node.type) ? forecastStamp(time) : stamp(time);
    document.getElementById('world-inspector-detail').textContent = [mission ? `Mission · ${mission}` : null,
      timeAt ? `Last activity · ${relativeTime(time)}` : time ? 'Recorded · time unavailable' : null,
      node.record?.evidence_hash ? `Evidence hash · ${node.record.evidence_hash}` : null,
    ].filter(Boolean).join(' · ') || 'Inspect the live status and evidence for this node.';
    const facts = document.getElementById('world-inspector-facts'); facts.replaceChildren();
    const addFact = (term, detail) => {
      if (detail === null || detail === undefined || detail === '') return;
      const dt = document.createElement('dt'); dt.textContent = term;
      const dd = document.createElement('dd'); dd.textContent = String(detail);
      facts.append(dt, dd);
    };
    addFact('STATE', `${node.status ?? 'unknown'}${node.rawStatus ? ` · raw: ${node.rawStatus}` : ''}`);
    if (node.activity) {
      addFact('INHABITANT ACTIVITY', `${node.activity.label} · ${node.activity.detail}`);
      addFact('ACTIVITY EVIDENCE', node.activity.evidenceAt ? `${node.activity.evidenceAt} · ${relativeTime(node.activity.evidenceAt)}` : 'No current activity timestamp recorded');
    }
    if (node.occupancy) addFact('MISSION OCCUPANCY', `${node.occupancy.role} · ${node.occupancy.missionId} · ${node.occupancy.evidence}`);
    const station = workstationDescriptor(node);
    if (station) addFact('HABITAT STATION', `${station.glyph} ${station.label} · presentation derived from canonical entity type`);
    addFact('LAST ACTIVITY', time ? `${time} · ${relativeTime(time)}` : 'No timestamp recorded');
    addFact('CURRENT MISSION', mission);
    addFact('DESCRIPTION', node.metadata);
    if (node.capabilities?.length) addFact('CAPABILITIES', node.capabilities.join(' · '));
    if (node.limits?.length) addFact('LIMITS', node.limits.join(' · '));
    addFact('ECONOMIC IMPACT', node.record?.native_value_usd != null ? `$${node.record.native_value_usd}` : node.record?.cash_balance_usd != null ? `$${node.record.cash_balance_usd}` : null);
    addFact('DATA SOURCE', node.source);
    if (node.observedAt) addFact('FRESHNESS', relativeTime(node.observedAt));
    renderNodeRelationships(node); renderActivity(node);
    document.getElementById('world-inspector-raw').textContent = JSON.stringify(node.record ?? node, null, 2);
    controls.list.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.nodeId === node.id)));
    draw();
  }
  function renderAutonomousDesk() {
    const seatsHost = document.getElementById('autonomous-desk-seats');
    const routeHost = document.getElementById('autonomous-desk-route');
    const tapeHost = document.getElementById('autonomous-desk-tape');
    const rulesHost = document.getElementById('autonomous-rule-rack');
    const killsHost = document.getElementById('autonomous-kill-board');
    const reportHost = document.getElementById('autonomous-shift-report');
    const stateHost = document.getElementById('autonomous-desk-state');
    if (!seatsHost || !routeHost || !tapeHost || !stateHost) return;

    const seats = bindAutonomousDesk(nodes, edges);
    const packets = deriveShiftPackets(nodes, edges);
    const tape = deriveShiftTape(timeline, 10);
    const activeSeats = seats.filter((seat) => seat.state === 'active').length;
    const vacantSeats = seats.filter((seat) => seat.state === 'vacant').length;
    stateHost.textContent = `${activeSeats} active · ${vacantSeats} vacant · ${packets.length} active work packet${packets.length === 1 ? '' : 's'}`;

    seatsHost.replaceChildren();
    for (const seat of seats) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'desk-seat';
      button.dataset.state = seat.state;
      button.disabled = !seat.nodeId;
      button.title = `${seat.basis}. ${seat.authority}`;
      const stage = document.createElement('div'); stage.className = 'desk-seat-stage';
      const label = document.createElement('span'); label.textContent = seat.label;
      const state = document.createElement('span'); state.className = 'desk-seat-state'; state.textContent = seat.state.toUpperCase();
      stage.append(label, state);
      const name = document.createElement('strong'); name.textContent = seat.nodeLabel;
      const purpose = document.createElement('p'); purpose.textContent = seat.purpose;
      const status = document.createElement('small'); status.textContent = seat.status;
      button.append(stage, name, purpose, status);
      if (seat.nodeId) {
        button.onclick = () => {
          const node = nodeById(seat.nodeId);
          if (!node) return;
          hiddenTypes.clear(); hiddenStatuses.clear(); setPreset('all');
          selectNode(node);
        };
      }
      seatsHost.append(button);
    }

    routeHost.replaceChildren();
    if (!packets.length) {
      const empty = document.createElement('p'); empty.className = 'autonomous-empty';
      empty.textContent = 'No active persisted mission assignment is currently moving through the desk.';
      routeHost.append(empty);
    } else {
      for (const packet of packets.slice(0, 5)) {
        const article = document.createElement('article'); article.className = 'desk-packet';
        const head = document.createElement('div'); head.className = 'desk-packet-head';
        const mission = document.createElement('strong'); mission.textContent = packet.missionId;
        const status = document.createElement('span'); status.textContent = packet.status;
        head.append(mission, status);
        const objective = document.createElement('p'); objective.textContent = packet.objective ?? 'Objective unavailable';
        const chain = document.createElement('div'); chain.className = 'desk-route-chain';
        if (!packet.route.length) {
          const empty = document.createElement('span'); empty.className = 'desk-route-empty';
          empty.textContent = 'No persisted specialist route';
          chain.append(empty);
        } else {
          packet.route.forEach((step, index) => {
            if (index) {
              const arrow = document.createElement('span'); arrow.className = 'desk-route-arrow'; arrow.textContent = '→';
              chain.append(arrow);
            }
            const button = document.createElement('button'); button.type = 'button';
            button.textContent = `${step.label} · ${step.relation}`;
            button.onclick = () => {
              const node = nodeById(step.nodeId);
              if (node) selectNode(node);
            };
            chain.append(button);
          });
        }
        article.append(head, objective, chain);
        article.onclick = (event) => {
          if (event.target.closest('button')) return;
          const node = nodeById(packet.missionNodeId);
          if (node) selectNode(node);
        };
        routeHost.append(article);
      }
    }

    tapeHost.replaceChildren();
    if (!tape.length) {
      const empty = document.createElement('p'); empty.className = 'autonomous-empty';
      empty.textContent = 'No persisted shift events loaded.';
      tapeHost.append(empty);
    } else {
      for (const event of tape) {
        const row = document.createElement(event.missionId ? 'button' : 'div');
        if (event.missionId) row.type = 'button';
        row.className = 'desk-tape-row';
        const time = document.createElement('time');
        time.textContent = new Date(event.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const actor = document.createElement('strong'); actor.textContent = event.actor;
        const title = document.createElement('span'); title.textContent = `${event.title} · ${event.status}`;
        row.append(time, actor, title);
        if (event.missionId) {
          row.onclick = () => {
            const node = nodes.find((candidate) => candidate.missionId === event.missionId && candidate.type === 'mission');
            if (node) selectNode(node);
          };
        }
        tapeHost.append(row);
      }
    }

    if (rulesHost) {
      rulesHost.replaceChildren();
      for (const rule of deriveRuleRack(source, capabilitySources)) {
        const row = document.createElement('article'); row.className = 'desk-rule-row';
        const name = document.createElement('strong'); name.textContent = rule.name;
        const value = document.createElement('span'); value.textContent = rule.value;
        const basis = document.createElement('small'); basis.textContent = rule.source;
        row.append(name, value, basis); rulesHost.append(row);
      }
    }
    if (killsHost) {
      killsHost.replaceChildren();
      const kills = deriveKillBoard(source);
      if (!kills.length) {
        const empty = document.createElement('p'); empty.className = 'autonomous-empty';
        empty.textContent = source?.database_present && hasKillBoardCoverage(source)
          ? 'No persisted PASS, critic rejection, or terminated mission is present in the loaded records.'
          : 'Decision history unavailable; no failures are inferred.';
        killsHost.append(empty);
      }
      for (const item of kills) {
        const targetNode = item.decisionId != null ? nodeById(`forecast:${item.decisionId}`)
          : item.missionId ? nodeById(`mission:${item.missionId}`) : null;
        const button = targetNode ? document.createElement('button') : document.createElement('article');
        if (targetNode) button.type = 'button';
        button.className = 'desk-kill-row';
        const heading = document.createElement('strong'); heading.textContent = `${item.kind} · ${item.title}`;
        const reason = document.createElement('span'); reason.textContent = item.reason;
        const meta = document.createElement('small'); meta.textContent = `${item.at ?? 'time unknown'} · ${item.id}`;
        button.append(heading, reason, meta);
        if (targetNode) button.onclick = () => {
          selectNode(targetNode); document.getElementById('workstation-topology')?.scrollIntoView({ block: 'nearest' });
        };
        killsHost.append(button);
      }
    }
    if (reportHost) {
      reportHost.replaceChildren();
      const report = deriveShiftReport(source, 24, capabilitySources.freshness);
      const stats = [
        ['MISSIONS', report.counts.missions], ['INVESTIGATIONS', report.counts.investigations],
        ['FORECASTS', report.counts.forecasts], ['COMPLETED', report.counts.completed],
        ['FAILURES', report.counts.failures], ['RECORDED EVENTS', report.counts.events],
      ];
      const grid = document.createElement('div'); grid.className = 'desk-report-stats';
      for (const [label, value] of stats) {
        const cell = document.createElement('div'); const title = document.createElement('small'); title.textContent = label;
        const number = document.createElement('strong'); number.textContent = value == null ? 'Unavailable' : String(value);
        cell.append(title, number); grid.append(cell);
      }
      reportHost.append(grid);
      const summary = document.createElement('p'); summary.textContent = `Window ${report.window_hours}h · ${report.coverage} · model/compute cost ${report.compute_cost_usd == null ? 'Unknown' : `$${report.compute_cost_usd.toFixed(4)}`} (${report.compute_cost_records} explicit run records${report.compute_cost_unknown ? ', some unknown' : ''}).`;
      reportHost.append(summary);
      const people = document.createElement('p'); people.textContent = `Recorded contributors: ${report.contributors.join(' · ') || 'Unknown'}; this counts linked investigations, not independent-agent performance.`;
      reportHost.append(people);
      const blockers = document.createElement('p'); blockers.textContent = `Outstanding blockers: ${report.blockers == null ? 'Unavailable · mission records not recorded' : report.blockers.map(item => `${item.mission_id} · ${item.status} · ${item.objective}`).join(' | ') || 'None in loaded mission rows'}.`;
      reportHost.append(blockers);
      const economics = document.createElement('p'); economics.className = 'desk-report-economics'; economics.textContent = report.economic_contribution;
      reportHost.append(economics);
    }
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    canvas.width = Math.round(rect.width * dpr); canvas.height = Math.round(rect.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }
  const layoutSlots = new Map();
  let hoveredId = null, hoveredEdge = null, livingTimer = null;
  const clusters = HABITAT_ZONES;
  const farClusterLayout = HABITAT_FAR_LAYOUT;
  const clusterKey = habitatZoneKey;
  function draw() {
    const rect = canvas.getBoundingClientRect(); if (!rect.width || !rect.height) return;
    const width = rect.width, height = rect.height;
    const core = nodes.find((node) => node.type === 'core');
    const groups = new Map(Object.keys(clusters).map((key) => [key, []]));
    const ids = new Set(nodes.map((node) => node.id));
    for (const id of layoutSlots.keys()) if (!ids.has(id)) layoutSlots.delete(id);
    const layoutGroups = new Map(Object.keys(clusters).map((key) => [key, []]));
    for (const node of nodes) if (node !== core) layoutGroups.get(clusterKey(node)).push(node);
    const visible = visibleNodes();
    for (const node of visible) if (node !== core) groups.get(clusterKey(node)).push(node);
    const level = controls.camera.zoom < .76 ? 'far' : controls.camera.zoom > 1.45 ? 'close' : 'medium';
    const zoomVisible = level === 'close' ? visible : level === 'medium'
      ? visible.filter((node) => !['forecast', 'evidence', 'deliverable', 'session', 'experiment', 'lesson', 'tool'].includes(node.type))
      : visible.filter((node) => node.type === 'core');
    const renderEdges = level === 'far' ? [] : visibleEdges(zoomVisible);
    const currentRootId = navigationIndex > 0 ? currentRoot() : null;
    const activeEdges = renderEdges.filter((edge) => edge.from === focusNodeId || edge.to === focusNodeId
      || (currentRootId && (edge.from === currentRootId || edge.to === currentRootId)));
    const neighborhood = new Set([focusNodeId, ...activeEdges.flatMap((edge) => [edge.from, edge.to])]);
    const overview = navigationIndex === 0 && focusNodeId === 'agent:NOEMA';
    for (const [key, group] of layoutGroups) {
      const center = clusters[key];
      const used = new Set(group.filter((node) => layoutSlots.has(node.id)).map((node) => layoutSlots.get(node.id)));
      for (const node of group) {
        if (!layoutSlots.has(node.id)) {
          let slot = 0; while (used.has(slot)) slot++;
          layoutSlots.set(node.id, slot); used.add(slot);
        }
        const slot = layoutSlots.get(node.id);
        const point = habitatSlotPosition(key, slot);
        node.x = point.x;
        node.y = point.y;
        node.z = point.z;
      }
    }
    if (core) { core.x = 0; core.y = 0; core.z = 0; }
    const occupancies = deriveMissionOccupancies(nodes, edges);
    for (const occupancy of occupancies) {
      const agent = nodes.find((node) => node.id === occupancy.agentId);
      const mission = nodes.find((node) => node.id === occupancy.missionNodeId);
      if (!agent || !mission) continue;
      agent.occupancy = occupancy;
      agent.x = mission.x + occupancy.offset.x;
      agent.y = mission.y + occupancy.offset.y;
      agent.z = mission.z + occupancy.offset.z;
    }
    const activeLineage = deriveActiveMissionLineage(nodes, edges);
    const activeZones = new Set([...activeLineage].map((id) => nodes.find((node) => node.id === id)).filter(Boolean).map(clusterKey));
    const positions = new Map(nodes.map((node) => [node.id, project(node, width, height, controls.camera)]));
    const sorted = [...zoomVisible].sort((a, b) => positions.get(a.id).depth - positions.get(b.id).depth);
    for (const node of sorted) node.screen = positions.get(node.id);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.globalAlpha = 1;
    ctx.fillStyle = '#0b1420'; ctx.fillRect(0, 0, width, height);
    const atmosphere = ctx.createRadialGradient(width * .5, height * .45, 0, width * .5, height * .5, width * .65);
    atmosphere.addColorStop(0, '#24466344'); atmosphere.addColorStop(1, '#0b142000');
    ctx.fillStyle = atmosphere; ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = '#abc2da18';
    for (let x = 20; x < width; x += 26) for (let y = 18; y < height; y += 26) { ctx.beginPath(); ctx.arc(x, y, .6, 0, Math.PI * 2); ctx.fill(); }
    const occupied = [];
    const hitClusters = [];
    for (const [key, group] of groups) {
      if (!group.length) continue;
      const c = clusters[key], farPosition = farClusterLayout[key];
      const center = level === 'far'
        ? { x: width * farPosition[0] + controls.camera.panX, y: height * farPosition[1] + controls.camera.panY, scale: 1 }
        : project(c, width, height, controls.camera);
      const radius = Math.max(52, 29 * Math.sqrt(group.length) + 32) * center.scale;
      if (level === 'far') {
        const compactName = c.shortName ?? c.name;
        const title = `${compactName} · ${group.length}${activeZones.has(key) ? ' · ACTIVE' : ''}`;
        ctx.font = '500 12px ui-monospace, monospace';
        const pillWidth = Math.min(width * .44, Math.max(118, ctx.measureText(title).width + 24));
        const pillHeight = 38, x = center.x - pillWidth / 2, y = center.y - pillHeight / 2;
        ctx.beginPath(); ctx.roundRect(x, y, pillWidth, pillHeight, 8);
        ctx.fillStyle = activeZones.has(key) ? `${c.color}1f` : '#122235ed'; ctx.fill(); ctx.strokeStyle = activeZones.has(key) ? c.color : `${c.color}a0`; ctx.lineWidth = activeZones.has(key) ? 2 : 1.5; ctx.stroke();
        ctx.fillStyle = c.color; ctx.textAlign = 'center'; ctx.fillText(title, center.x, center.y + 4); ctx.textAlign = 'left';
        hitClusters.push({ key, x, y, w: pillWidth, h: pillHeight });
        continue;
      }
      const deck = habitatDeckCorners(key, group.length).map((point) => project(point, width, height, controls.camera));
      ctx.beginPath(); ctx.moveTo(deck[0].x, deck[0].y);
      for (const point of deck.slice(1)) ctx.lineTo(point.x, point.y);
      ctx.closePath();
      const zoneActive = activeZones.has(key);
      ctx.fillStyle = zoneActive ? `${c.color}18` : `${c.color}0b`; ctx.fill();
      ctx.strokeStyle = zoneActive ? `${c.color}9a` : `${c.color}38`; ctx.lineWidth = zoneActive ? 2 : 1.2; ctx.setLineDash([4, 5]); ctx.stroke(); ctx.setLineDash([]);
      const backLeft = deck[0], backRight = deck[1];
      ctx.beginPath(); ctx.moveTo(backLeft.x, backLeft.y); ctx.lineTo(backLeft.x, backLeft.y - 8);
      ctx.lineTo(backRight.x, backRight.y - 8); ctx.lineTo(backRight.x, backRight.y);
      ctx.strokeStyle = `${c.color}25`; ctx.stroke();
      if (width > 500) {
        const label = `${c.name}  ${group.length}${activeZones.has(key) ? '  · ACTIVE MISSION' : ''}`;
        const purpose = c.purpose ?? '';
        ctx.font = '600 10px ui-monospace, monospace'; ctx.fillStyle = c.color;
        const w = Math.max(ctx.measureText(label).width, ctx.measureText(purpose).width), x = center.x - w / 2;
        const y = Math.min(...deck.map((point) => point.y)) - 25;
        if (x >= 8 && x + w < width - 8 && y > 64 && y < height - 28) {
          ctx.fillText(label, x, y);
          ctx.font = '9px ui-monospace, monospace'; ctx.fillStyle = '#89a7bd';
          ctx.fillText(purpose, x, y + 13);
          occupied.push({ x: x - 4, y: y - 12, w: w + 8, h: 30 });
        }
      }
    }
    const edgeSegments = [];
    for (const edge of renderEdges) {
      const from = positions.get(edge.from), to = positions.get(edge.to); if (!from || !to) continue;
      const active = activeEdges.includes(edge), curve = Math.min(35, Math.abs(to.x - from.x) * .1);
      const control1 = { x: from.x + (to.x - from.x) * .4, y: from.y - curve };
      const control2 = { x: from.x + (to.x - from.x) * .6, y: to.y - curve };
      ctx.beginPath(); ctx.moveTo(from.x, from.y);
      ctx.bezierCurveTo(control1.x, control1.y, control2.x, control2.y, to.x, to.y);
      const selected = selectedEdge === edgeId(edge), edgeHovered = hoveredEdge === edgeId(edge);
      ctx.strokeStyle = selected ? '#f4d58c' : active ? (edge.type.startsWith('handoff') ? '#c5a3f0d0' : '#76e0c2cc') : '#7389a354';
      ctx.lineWidth = selected ? 3.2 : active || edgeHovered ? 2 : 1.05; ctx.stroke();
      const points = [];
      for (let index = 0; index <= 20; index++) {
        const t = index / 20, mt = 1 - t;
        points.push({ x: mt ** 3 * from.x + 3 * mt ** 2 * t * control1.x + 3 * mt * t ** 2 * control2.x + t ** 3 * to.x,
          y: mt ** 3 * from.y + 3 * mt ** 2 * t * control1.y + 3 * mt * t ** 2 * control2.y + t ** 3 * to.y });
      }
      edgeSegments.push({ edge, points });
      if ((selected || edgeHovered || active) && width > 560 && level === 'close') {
        const middle = points[Math.floor(points.length / 2)];
        ctx.font = '10px ui-monospace, monospace'; ctx.fillStyle = selected ? '#f4d58c' : '#b9d5e8';
        const shortType = String(edge.type).slice(0, 34);
        ctx.fillText(shortType, middle.x + 4, middle.y - 5);
      }
    }
    let livingMotion = false;
    if (replayIndex === null) {
      const now = Date.now();
      for (const segment of edgeSegments) {
        const traversal = handoffTraversal(segment.edge, now);
        if (!traversal) continue;
        livingMotion = true;
        const scaled = traversal.progress * (segment.points.length - 1);
        const index = Math.min(segment.points.length - 2, Math.floor(scaled));
        const local = scaled - index;
        const a = segment.points[index], b = segment.points[index + 1];
        const x = a.x + (b.x - a.x) * local, y = a.y + (b.y - a.y) * local;
        ctx.save();
        ctx.beginPath(); ctx.arc(x, y, 8, 0, Math.PI * 2);
        ctx.fillStyle = '#c5a3f01f'; ctx.fill();
        ctx.translate(x, y); ctx.rotate(Math.PI / 4);
        ctx.fillStyle = '#e3caff'; ctx.fillRect(-3.5, -3.5, 7, 7);
        ctx.restore();
      }
    }
    for (const node of sorted) {
      const p = node.screen; if (p.x < -30 || p.y < -30 || p.x > width + 30 || p.y > height + 30) continue;
      const isCore = node.type === 'core', selected = node.id === selectedId || node.id === hoveredId;
      const color = palette[node.type] ?? '#94b3cc';
      const radius = isCore ? 20 : Math.max(5, Math.min(9, p.scale * 9));
      node.screen.radius = radius;
      ctx.save(); ctx.globalAlpha = overview || selected || neighborhood.has(node.id) ? 1 : .3;
      if (isCore || selected) {
        ctx.beginPath(); ctx.arc(p.x, p.y, radius + 10, 0, Math.PI * 2);
        ctx.strokeStyle = `${color}38`; ctx.lineWidth = 1; ctx.stroke();
        ctx.beginPath(); ctx.arc(p.x, p.y, radius + 5, 0, Math.PI * 2);
        ctx.strokeStyle = `${color}a0`; ctx.stroke();
      }
      const isInhabitant = node.type === 'agent';
      if (isInhabitant) {
        const body = Math.max(6, radius * .9);
        node.screen.radius = radius + 4;
        ctx.beginPath(); ctx.ellipse(p.x, p.y + body * .78, body * .8, body * .28, 0, 0, Math.PI * 2);
        ctx.fillStyle = '#00000042'; ctx.fill();
        ctx.beginPath(); ctx.arc(p.x, p.y - body * .52, body * .42, 0, Math.PI * 2);
        ctx.fillStyle = `${color}42`; ctx.fill();
        ctx.strokeStyle = statePalette[node.stateKind] ?? '#8296af'; ctx.lineWidth = selected ? 2 : 1.2; ctx.stroke();
        ctx.beginPath(); ctx.roundRect(p.x - body * .5, p.y - body * .05, body, body * 1.12, body * .24);
        ctx.fillStyle = `${color}28`; ctx.fill();
        ctx.strokeStyle = statePalette[node.stateKind] ?? '#8296af'; ctx.stroke();
        const glyph = node.occupancy ? 'M' : inhabitantGlyph(node.activity?.mode);
        ctx.beginPath(); ctx.arc(p.x + body * .72, p.y - body * .7, 5.5, 0, Math.PI * 2);
        ctx.fillStyle = '#0d1928'; ctx.fill(); ctx.strokeStyle = color; ctx.stroke();
        ctx.fillStyle = color; ctx.font = '600 8px ui-monospace, monospace'; ctx.textAlign = 'center';
        ctx.fillText(glyph, p.x + body * .72, p.y - body * .7 + 2.6); ctx.textAlign = 'left';
      } else {
        const station = workstationDescriptor(node);
        if (station) {
          const size = Math.max(7, radius * 1.05);
          node.screen.radius = radius + 4;
          ctx.strokeStyle = statePalette[node.stateKind] ?? '#8296af';
          ctx.fillStyle = `${color}22`;
          ctx.lineWidth = selected ? 2 : 1.2;
          if (station.kind === 'market' || station.kind === 'cognition' || station.kind === 'session') {
            ctx.beginPath(); ctx.roundRect(p.x - size, p.y - size * .72, size * 2, size * 1.3, 2.5); ctx.fill(); ctx.stroke();
            ctx.beginPath(); ctx.moveTo(p.x - size * .68, p.y + size * .8); ctx.lineTo(p.x + size * .68, p.y + size * .8); ctx.stroke();
            ctx.beginPath(); ctx.moveTo(p.x, p.y + size * .58); ctx.lineTo(p.x, p.y + size * .8); ctx.stroke();
          } else if (station.kind === 'vault') {
            ctx.beginPath(); ctx.roundRect(p.x - size * .82, p.y - size * .82, size * 1.64, size * 1.64, 3); ctx.fill(); ctx.stroke();
            ctx.beginPath(); ctx.arc(p.x, p.y, size * .34, 0, Math.PI * 2); ctx.stroke();
            ctx.beginPath(); ctx.moveTo(p.x, p.y - size * .34); ctx.lineTo(p.x, p.y + size * .34); ctx.stroke();
          } else if (station.kind === 'gateway') {
            ctx.beginPath();
            for (let i = 0; i < 6; i++) {
              const angle = Math.PI / 3 * i - Math.PI / 6;
              const x = p.x + Math.cos(angle) * size, y = p.y + Math.sin(angle) * size;
              if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            }
            ctx.closePath(); ctx.fill(); ctx.stroke();
          } else if (station.kind === 'command' || station.kind === 'experiment') {
            ctx.beginPath(); ctx.moveTo(p.x, p.y - size); ctx.lineTo(p.x + size, p.y);
            ctx.lineTo(p.x, p.y + size); ctx.lineTo(p.x - size, p.y); ctx.closePath(); ctx.fill(); ctx.stroke();
          } else {
            ctx.beginPath(); ctx.roundRect(p.x - size * .82, p.y - size * .64, size * 1.64, size * 1.28, 2); ctx.fill(); ctx.stroke();
          }
          ctx.fillStyle = color; ctx.font = '600 8px ui-monospace, monospace'; ctx.textAlign = 'center';
          ctx.fillText(station.glyph, p.x, p.y + 2.8); ctx.textAlign = 'left';
        } else {
          ctx.beginPath(); ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
          ctx.fillStyle = isCore ? '#182e3c' : `${color}25`; ctx.fill();
          ctx.strokeStyle = statePalette[node.stateKind] ?? '#8296af'; ctx.lineWidth = selected ? 2 : 1.3; ctx.stroke();
          if (isCore) {
            ctx.fillStyle = '#ecfff9'; ctx.font = '500 19px sans-serif'; ctx.textAlign = 'center'; ctx.fillText('N', p.x, p.y + 7); ctx.textAlign = 'left';
          } else {
            ctx.beginPath(); ctx.arc(p.x, p.y, Math.max(2, radius * .38), 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill();
          }
        }
      }
      ctx.restore();
    }
    // Labels are screen-sized, collision-aware, and never shrunk to fit the graph.
    const candidates = [...zoomVisible].sort((a, b) => {
      const priority = (n) => n.id === hoveredId ? 0 : n.id === selectedId ? 1 : n.type === 'core' ? 2 : n.type === 'mission' ? 3 : 4;
      return priority(a) - priority(b);
    });
    let labels = 0;
    const maxLabels = width < 500 ? 3 : 10;
    for (const node of candidates) {
      if (labels >= maxLabels) break;
      if (level !== 'close' && !overview && !neighborhood.has(node.id) && node.id !== hoveredId) continue;
      const p = node.screen, active = node.id === selectedId || node.id === hoveredId;
      ctx.font = `${active ? '500' : '400'} 12px sans-serif`;
      let title = node.label, maxWidth = Math.min(190, width * .38);
      while (ctx.measureText(title).width > maxWidth && title.length > 4) title = `${title.replace(/…$/, '').slice(0, -1)}…`;
      const w = ctx.measureText(title).width + 18, h = 28, r = p.radius ?? 7;
      const placements = [
        { x: p.x + r + 10, y: p.y - h / 2, w, h },
        { x: p.x - w / 2, y: p.y + r + 11, w, h },
        { x: p.x - r - w - 10, y: p.y - h / 2, w, h },
        { x: p.x - w / 2, y: p.y - r - h - 11, w, h },
      ];
      const overlaps = (a, b) => a.x < b.x + b.w + 4 && a.x + a.w + 4 > b.x && a.y < b.y + b.h + 4 && a.y + a.h + 4 > b.y;
      const box = placements.find((b) => b.x >= 8 && b.y >= 66 && b.x + b.w <= width - 8 && b.y + b.h <= height - 12
        && !occupied.some((old) => overlaps(b, old))
        && !zoomVisible.some((other) => other.id !== node.id && overlaps(b, { x: other.screen.x - 8, y: other.screen.y - 8, w: 16, h: 16 })));
      if (!box) continue;
      ctx.beginPath(); ctx.roundRect(box.x, box.y, box.w, box.h, 4);
      ctx.fillStyle = active ? '#20374cf5' : '#101e2cf5'; ctx.fill();
      ctx.strokeStyle = active ? '#93c5ed' : '#304a61'; ctx.lineWidth = 1; ctx.stroke();
      ctx.fillStyle = active ? '#f0f7ff' : '#bcd0e0'; ctx.fillText(title, box.x + 9, box.y + 18);
      occupied.push(box); labels++;
    }
    canvas._hitNodes = sorted;
    canvas._hitEdges = edgeSegments;
    canvas._hitClusters = hitClusters;
    const freshness = capabilitySources.freshness ?? {};
    const state = document.getElementById('world-map-state');
    const inhabitantCount = visible.filter((node) => ['core', 'agent'].includes(node.type)).length;
    const stationCount = visible.filter((node) => workstationDescriptor(node)).length;
    state.textContent = level === 'far'
      ? `${hitClusters.length} habitat zones · ${inhabitantCount} inhabitants · ${stationCount} stations · ${visible.length} entities · ${replayIndex === null ? 'current view' : 'historical replay'}`
      : `${inhabitantCount} inhabitants · ${stationCount} stations · ${zoomVisible.length}/${nodes.length} entities · ${renderEdges.length} routes · ${level} detail · ${replayIndex === null ? 'current view' : 'historical replay'}`;
    state.title = `Records ${freshness.operations ?? 'unknown'} · providers ${freshness.providers ?? 'unknown'} · venues ${freshness.venues ?? 'unknown'} · wallets ${freshness.wallets ?? 'unknown'}`;
    if (livingMotion && !reducedMotion.matches && livingTimer === null) {
      livingTimer = setTimeout(() => {
        livingTimer = null;
        requestAnimationFrame(draw);
      }, 80);
    }
  }
  function setTime(index) {
    replayIndex = index;
    const cutoff = index === null || !timeline.length ? Infinity : timeline[index]?.at ?? Infinity;
    const model = buildModel(source, cutoff, capabilitySources); nodes = model.nodes;
    edges = model.edges.map((edge) => ({ ...edge, id: edgeId(edge) }));
    document.getElementById('world-time-value').textContent = index === null ? 'Latest known state' : new Date(cutoff).toLocaleString();
    document.getElementById('world-time-count').textContent = `${timeline.length} loaded persisted events · ${index === null ? 'following live state' : `as of event ${index + 1}/${timeline.length}`}`;
    const eventList = document.getElementById('world-event-list'); eventList.replaceChildren();
    for (const [eventIndex, event] of timeline.slice(-12).entries()) {
      const absoluteIndex = timeline.length - Math.min(12, timeline.length) + eventIndex;
      const button = document.createElement('button'); button.type = 'button'; button.dataset.eventIndex = String(absoluteIndex);
      button.setAttribute('aria-pressed', String(index === absoluteIndex));
      const time = new Date(event.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      button.textContent = `${time} · ${event.actor} · ${event.title}`;
      button.dataset.missionId = event.missionId ?? '';
      button.dataset.marketId = event.source?.market_id ?? event.source?.payload?.market_id ?? '';
      button.onclick = () => {
        controls.range.value = String(absoluteIndex); setTime(absoluteIndex);
        const missionNode = nodes.find((node) => node.missionId === event.missionId);
        if (missionNode) selectedId = missionNode.id;
        const eventNode = { id: event.id, type: event.kind, label: event.title, status: event.status,
          missionId: event.missionId, record: { created_at: event.time, detail: event.detail,
            mission_id: event.missionId, actor: event.actor, status: event.status } };
        setInspector(eventNode); announceSelection(eventNode);
      };
      eventList.append(button);
    }
    renderEntityList();
    renderAutonomousDesk();
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
    const selectedLink = edges.find((edge) => edgeId(edge) === selectedEdge);
    const found = nodes.find((node) => node.id === selectedId);
    if (selectedLink) inspectEdge(selectedLink, { notify: false });
    else {
      const edgeWasRemoved = Boolean(selectedEdge); selectedEdge = null;
      if (edgeWasRemoved) document.querySelector('[data-inspector-tab="overview"]')?.click();
      const fallback = found ?? nodeById('agent:NOEMA') ?? nodes[0];
      setInspector(fallback);
      if (edgeWasRemoved && fallback) announceSelection(fallback);
    }
    updateNavigationControls(); draw();
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
    if (!drag) {
      const rect = canvas.getBoundingClientRect(), x = event.clientX - rect.left, y = event.clientY - rect.top;
      const hit = hitTest(x, y);
      const tooltip = document.getElementById('topology-tooltip');
      if (tooltip) {
        tooltip.hidden = !hit;
        if (hit) {
          tooltip.textContent = hit.kind === 'node'
            ? `${hit.value.label} · ${hit.value.status ?? 'unknown'} · ${edges.filter((edge) => edge.from === hit.value.id || edge.to === hit.value.id).length} direct relationships`
            : hit.kind === 'edge' ? `${labelFor(hit.value.from)} → ${labelFor(hit.value.to)} · ${hit.value.type}${hit.value.missionId ? ` · mission ${hit.value.missionId}` : ''}`
              : `${clusters[hit.value.key].name} · ${groupsCount(hit.value.key)} canonical entities · click to expand`;
          tooltip.style.left = `${Math.max(8, Math.min(x + 16, rect.width - 300))}px`;
          tooltip.style.top = `${Math.max(8, Math.min(y + 18, rect.height - 80))}px`;
        }
      }
      const nextNode = hit?.kind === 'node' ? hit.value.id : null;
      const nextEdge = hit?.kind === 'edge' ? edgeId(hit.value) : null;
      if (hoveredId !== nextNode || hoveredEdge !== nextEdge) { hoveredId = nextNode; hoveredEdge = nextEdge; draw(); }
      return;
    }
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
      const hit = hitTest(x, y);
      if (hit?.kind === 'node') {
        if (event.shiftKey) togglePin(hit.value);
        selectNode(hit.value);
      } else if (hit?.kind === 'edge') inspectEdge(hit.value);
      else if (hit?.kind === 'cluster') showCluster(hit.value.key);
      else {
        const root = nodeById('agent:NOEMA');
        navigation = [['agent:NOEMA']]; navigationIndex = 0; selectedEdge = null; focusNodeId = 'agent:NOEMA';
        selectNode(root); updateNavigationControls();
      }
      }
    }
    drag = null;
    if (activeTouches.size === 1) {
      const [pointerId, point] = activeTouches.entries().next().value;
      drag = { pointerId, x: point.x, y: point.y, startX: point.x, startY: point.y, button: 0, pan: false };
    }
  });
  canvas.addEventListener('pointerleave', () => {
    const tooltip = document.getElementById('topology-tooltip');
    if (tooltip) tooltip.hidden = true;
    if (hoveredId || hoveredEdge) { hoveredId = null; hoveredEdge = null; draw(); }
  });
  canvas.addEventListener('pointercancel', (event) => {
    activeTouches.delete(event.pointerId);
    pinchDistance = null;
    drag = null;
  });
  canvas.addEventListener('contextmenu', (event) => event.preventDefault());
  canvas.addEventListener('wheel', (event) => { event.preventDefault(); controls.camera.zoom = Math.max(.55, Math.min(3.2, controls.camera.zoom * (event.deltaY > 0 ? .92 : 1.08))); draw(); }, { passive: false });
  canvas.addEventListener('dblclick', (event) => {
    event.preventDefault(); const rect = canvas.getBoundingClientRect();
    const hit = hitTest(event.clientX - rect.left, event.clientY - rect.top);
    if (hit?.kind === 'node') drillNode(hit.value);
  });
  controls.range.addEventListener('input', () => setTime(Number(controls.range.value)));
  document.getElementById('world-time-live').onclick = () => { replayIndex = null; update(source); };
  document.getElementById('world-reset').onclick = () => { Object.assign(controls.camera, { zoom: 1, panX: 0, panY: 0, orbitX: -.12, orbitY: .16 }); draw(); };
  document.getElementById('world-inspector-drill').onclick = () => { const node = nodeById(selectedId); if (node) drillNode(node); };
  initializeExplorerControls();
  new ResizeObserver(resize).observe(canvas);
  window.addEventListener('resize', resize);
  if (reducedMotion.matches) canvas.dataset.motion = 'reduced';
  function selectEntity(selector, notify = false) {
    const node = nodes.find((candidate) => candidate.id === selector?.id
      || (selector?.market_id && candidate.record?.market_id === selector.market_id
        && (!selector.venue || candidate.record?.venue === selector.venue))
      || (selector?.mission_id && (candidate.missionId === selector.mission_id
        || candidate.record?.mission_id === selector.mission_id))
      || (selector?.trial_id && candidate.record?.trial_id === selector.trial_id)
      || (selector?.name && candidate.label === selector.name));
    if (!node) return false;
    hiddenTypes.clear(); hiddenStatuses.clear(); setPreset('all');
    selectNode(node, { notify });
    return true;
  }
  function groupsCount(key) { return visibleNodes().filter((node) => node.type !== 'core' && clusterKey(node) === key).length; }
  return {
    update,
    selectEntity,
    drillEntity: (selector) => { const found = nodes.find((node) => node.id === selector?.id); if (!found) return false; drillNode(found); return true; },
    inspectRelationship: (selector) => { const found = edges.find((edge) => edgeId(edge) === selector?.id); if (!found) return false; inspectEdge(found); return true; },
  };
}
