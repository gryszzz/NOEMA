const $ = (id) => document.getElementById(id);
const state = { values: {}, filter: 'all', loading: false, updatedAt: null };
const endpoints = {
  operations: '/api/operations', agent: '/api/agent', ecosystem: '/api/ecosystem',
  radar: '/api/radar', cognition: '/api/cognition', providers: '/api/provider-health',
  policy: '/api/wallet-policy', venues: '/api/prediction-venues', trench: '/api/trench',
  knowledge: '/api/knowledge', wallet: '/api/wallet-status', discovery: '/api/research-discovery',
};

function text(id, value) { const el = $(id); if (el) el.textContent = value == null || value === '' ? 'Unknown' : String(value); }
function value(value, fallback = 'Unknown') { return value === null || value === undefined || value === '' ? fallback : String(value); }
function obj(value) { return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; }
function list(value) { return Array.isArray(value) ? value : []; }
function fmtTime(value) {
  if (!value) return 'Unknown';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
function age(value) {
  if (!value) return 'timestamp unavailable';
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  return Number.isFinite(seconds) ? (seconds < 60 ? `${seconds}s ago` : `${Math.floor(seconds / 60)}m ago`) : 'timestamp unavailable';
}
function stateClass(value) {
  const normalized = String(value ?? '').toLowerCase();
  if (/live|healthy|connected|ready|passed|active/.test(normalized)) return 'good';
  if (/unknown|unavailable|failed|degraded|stale|missing|blocked|error|unreconciled/.test(normalized)) return 'bad';
  return '';
}
function isPersistedWorkActive(value) {
  return /^(running|started|in_progress|active)$/i.test(String(value ?? '').trim());
}
function tag(id, status) {
  const element = $(id);
  if (!element) return;
  element.textContent = value(status).toUpperCase().replaceAll('_', ' ');
  element.className = `state-tag ${stateClass(status)}`;
}
async function getJson(url) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(url, { cache: 'no-store', credentials: 'same-origin', signal: controller.signal, headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } finally { clearTimeout(timeout); }
}
async function refresh() {
  if (state.loading) return;
  state.loading = true;
  const entries = Object.entries(endpoints);
  const results = await Promise.allSettled(entries.map(([, url]) => getJson(url)));
  results.forEach((result, index) => {
    const key = entries[index][0];
    if (result.status === 'fulfilled') state.values[key] = result.value;
    else state.values[key] = null;
  });
  try { state.values.capital = await getJson('/api/capital-history?window=24H'); }
  catch { state.values.capital = null; }
  state.updatedAt = new Date();
  render();
  state.loading = false;
}
function render() {
  renderRuntime(); renderTruth(); renderStations(); renderRadar(); renderKalshi();
  renderSpecialists(); renderTrench(); renderJournal(); renderProviders(); renderMissions(); renderCrawler(); renderSpiderGraph();
  text('as-of', `Updated ${state.updatedAt?.toLocaleTimeString() ?? '—'}`);
}
function renderRuntime() {
  const agent = obj(state.values.agent);
  const operations = obj(state.values.operations);
  const runtime = obj(operations.runtime);
  const liveness = value(agent.runtime_state, runtime.liveness || runtime.state || 'UNKNOWN').toUpperCase().replaceAll('_', ' ');
  const health = value(agent.health, runtime.health || 'unknown').toUpperCase();
  const live = liveness === 'LIVE' && agent.alive === true;
  text('runtime-label', live ? `WORKER ${liveness}` : `WORKER ${liveness}`);
  text('worker-state', liveness);
  const heartbeat = agent.last_heartbeat_at || runtime.last_heartbeat_at;
  const cycle = obj(runtime.cycle || agent.last_cycle);
  const cycleId = cycle.cycle_id ?? cycle.id;
  text('worker-detail', `Cycle health ${health} · heartbeat ${age(heartbeat)}`);
  text('cycle-label', cycleId == null ? 'Cycle unknown' : `Cycle ${cycleId}${cycle.duration_seconds ? ` · ${cycle.duration_seconds}s` : ''}`);
  const dot = $('live-dot');
  dot?.classList.toggle('good', live);
  dot?.classList.toggle('bad', !live && liveness !== 'UNKNOWN');
  text('hero-worker', `WORKER · ${liveness}`);
  const stream = $('stream-label');
  if (stream && !stream.dataset.state) stream.textContent = 'Change stream connecting';
}
function renderTruth() {
  const capital = obj(obj(state.values.capital).current);
  const completeness = capital.status === 'CACHED' && Number(capital.stale_sources || 0) === 0 && Number(capital.unpriced_assets || 0) === 0 && Number(capital.unpriced_sources || 0) === 0 && Array.isArray(capital.exclusions) && capital.exclusions.length === 0;
  if (capital.status === 'STALE' || Number(capital.stale_sources || 0) > 0) {
    text('agent-balance', 'STALE');
    text('balance-detail', `${capital.stale_sources ?? 0} stale source(s); known subtotal is not complete NAV`);
  } else if (completeness) {
    text('agent-balance', `$${Number(capital.amount_usd).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
    text('balance-detail', `Live source projection · ${capital.valued_sources}/${capital.expected_sources} sources · ${age(capital.observed_at)}`);
  } else if (capital.amount_usd != null) {
    text('agent-balance', 'UNRECONCILED');
    text('balance-detail', `Known subtotal only · ${capital.exclusions?.length ? capital.exclusions.join(', ') : 'coverage incomplete'}`);
  } else {
    text('agent-balance', 'UNAVAILABLE');
    text('balance-detail', 'No verified current live balance projection');
  }
  // The current endpoint exposes balance observations, not a complete reconciled live P&L ledger.
  text('net-pnl', 'UNRECONCILED');
  text('pnl-detail', 'Complete realized and marked live outcomes are not established by the balance projection');
  const cognition = obj(state.values.cognition);
  const budget = obj(cognition.hosted_cost_gate);
  const providers = obj(cognition.runtime_providers);
  const provider = value(cognition.provider, 'provider unknown');
  const configured = cognition.configured === true;
  const calls = Number(cognition.calls_last_hour || 0);
  const cognitionLabel = configured ? (calls > 0 ? 'CONFIGURED · USED' : 'CONFIGURED · IDLE') : 'UNAVAILABLE';
  text('cognition-state', cognitionLabel);
  const budgetText = budget.status ? `budget ${budget.status}` : budget.monthly_model_budget_configured ? 'monthly budget configured; approval not exposed here' : 'budget approval not verified';
  text('cognition-detail', `${provider} · ${budgetText}`);
  text('research-budget', `Hosted cognition · ${provider} · ${budgetText} · ${calls} calls in last hour`);
  const policy = obj(state.values.policy);
  const liveEnabled = policy.live_execution_enabled === true || policy.control_plane?.live_execution_enabled === true;
  // This desk is observational. It never enables execution or suggests that an intent is authorized.
  const policyKnown = typeof policy.live_execution_enabled === 'boolean' || typeof policy.control_plane?.live_execution_enabled === 'boolean';
  text('execution-state', !policyKnown ? 'UNKNOWN' : liveEnabled ? 'POLICY ENABLED' : 'DISABLED');
  text('execution-detail', !policyKnown ? 'Execution policy state unavailable; this desk cannot authorize activity' : liveEnabled ? 'Policy reports enabled; this desk remains read-only and submits no orders' : 'Execution disabled by current policy');
  text('hero-policy', `AUTHORITY · ${!policyKnown ? 'UNKNOWN' : liveEnabled ? 'POLICY ENABLED' : 'FAIL-CLOSED'}`);
  const guard = $('execution-guard');
  if (guard) guard.textContent = !policyKnown ? 'AUTHORITY UNKNOWN' : liveEnabled ? 'POLICY ENABLED · DESK READ-ONLY' : 'LIVE EXECUTION DISABLED';
  const venueData = obj(state.values.venues);
  const wallet = obj(state.values.wallet);
  const providerCount = Object.keys(obj(state.values.providers)).length;
  text('hero-feeds', `FEEDS · ${venueData.venues ? venueData.venues.length : '—'} VENUES / ${wallet.networks ? wallet.networks.length : '—'} CHAINS · ${providerCount ? 'OBSERVED' : 'UNKNOWN'}`);
}
function renderStations() {
  const hosts = [
    ['SCOUT', list(state.values.radar).length ? `${list(state.values.radar).length} persisted forecast rows in radar` : 'No persisted radar rows returned', '#e66ec4'],
    ['ANALYST', obj(state.values.cognition).configured === true ? `Cognition configured · ${value(state.values.cognition.provider)}` : 'Cognition configuration not confirmed', '#b35bff'],
    ['SKEPTIC', 'Challenge and review records are shown when persisted', '#f36e9f'],
    ['QUANT', `Forecast ledger · ${obj(state.values.operations).sections?.decisions?.rows?.length ?? 0} recent decision records`, '#55cde1'],
    ['RISK', `Live authority ${obj(state.values.policy).live_execution_enabled === true ? 'reported enabled by policy' : 'disabled / not granted'}`, '#ff9d62'],
    ['JOURNAL', `${obj(state.values.operations).sections?.research_runs?.rows?.length ?? 0} recent research runs`, '#80d5a2'],
    ['CRAWLER', obj(state.values.trench).progress?.collector_status || 'Trench collector state unavailable', '#d7c462'],
    ['OWNER GATE', 'Execution permission remains external to research evidence', '#fb7185'],
  ];
  const root = $('stations'); if (!root) return;
  root.replaceChildren(...hosts.map(([name, detail, color]) => {
    const article = document.createElement('article'); article.className = 'station'; article.style.setProperty('--station-color', color);
    const top = document.createElement('div'); top.className = 'station-top';
    const label = document.createElement('strong'); label.className = 'station-name'; label.textContent = name;
    const stateTag = document.createElement('span'); stateTag.className = `state-tag ${stateClass(detail)}`; stateTag.textContent = stateClass(detail) === 'good' ? 'AVAILABLE' : 'OBSERVED';
    const small = document.createElement('small'); small.textContent = detail;
    top.append(label, stateTag); article.append(top, small); return article;
  }));
}
function renderRadar() {
  const rows = list(state.values.radar);
  const filtered = rows.filter((row) => {
    const venue = String(row.venue || '').toLowerCase();
    return state.filter === 'all' || (state.filter === 'kalshi' ? venue.includes('kalshi') : /solana|web3|dex|evm|crypto|trench/.test(venue));
  });
  text('signal-count', `${filtered.length} / ${rows.length} persisted`);
  const root = $('opportunities'); if (!root) return;
  if (!filtered.length) { const p = document.createElement('p'); p.className = 'empty'; p.textContent = rows.length ? 'No recorded opportunities match this venue filter.' : state.values.radar === null ? 'Radar API unavailable; no values inferred.' : 'No persisted forecasts returned.'; root.replaceChildren(p); return; }
  root.replaceChildren(...filtered.slice(0, 30).map((row) => {
    const card = document.createElement('article'); card.className = 'opportunity';
    const head = document.createElement('div'); head.className = 'opportunity-head';
    const title = document.createElement('strong'); title.className = 'opportunity-title'; title.textContent = value(row.title, row.market_id || 'Untitled market');
    const venue = document.createElement('span'); venue.className = 'opportunity-venue'; venue.textContent = value(row.venue, 'venue unknown'); head.append(title, venue);
    const meta = document.createElement('div'); meta.className = 'opportunity-meta';
    const probability = Number.isFinite(Number(row.probability_yes)) ? `Model ${Math.round(Number(row.probability_yes) * 100)}%` : 'Model probability unavailable';
    const market = Number.isFinite(Number(row.market_probability)) ? `Market ${Math.round(Number(row.market_probability) * 100)}%` : 'Market probability unavailable';
    const edge = Number.isFinite(Number(row.robust_edge)) ? `Robust edge estimate ${Number(row.robust_edge).toFixed(3)}` : 'Edge estimate unavailable';
    const freshness = Number.isFinite(Number(row.freshness_seconds)) ? `${Math.round(Number(row.freshness_seconds))}s data age` : 'Freshness unavailable';
    [probability, market, edge, freshness, `${list(row.evidence_ids).length} evidence refs`].forEach((item) => { const span = document.createElement('span'); span.textContent = item; meta.append(span); });
    const reason = document.createElement('p'); reason.className = 'opportunity-reason'; reason.textContent = `${value(row.decision, 'No decision')} · ${value(row.reason, 'No recorded rationale')} · captured ${fmtTime(row.captured_at)}`;
    card.append(head, meta, reason); return card;
  }));
}
function renderKalshi() {
  const venue = list(obj(state.values.venues).venues).find((item) => String(item.venue || '').toLowerCase().includes('kalshi'));
  if (!venue) { tag('kalshi-status', state.values.venues === null ? 'UNAVAILABLE' : 'UNKNOWN'); text('kalshi-market', 'Unknown'); text('kalshi-account', 'Unknown'); text('kalshi-activity', 'Unknown'); text('kalshi-freshness', 'Unknown'); return; }
  const market = obj(venue.market_data); const account = obj(venue.account);
  tag('kalshi-status', account.status || market.status || 'UNKNOWN');
  text('kalshi-market', value(market.status));
  text('kalshi-account', value(account.status));
  const positions = account.positions_count ?? account.position_count;
  const fills = account.fills_count ?? account.fill_count;
  text('kalshi-activity', positions == null && fills == null ? 'Counts unavailable' : `${value(positions, '—')} positions / ${value(fills, '—')} fills`);
  const observed = account.observed_at || market.observed_at || venue.observed_at || obj(state.values.venues).as_of;
  text('kalshi-freshness', observed ? `${age(observed)} · ${fmtTime(observed)}` : 'Unknown');
  const note = account.reason || account.error || market.reason || 'Read-only venue status from the existing authenticated observer; no orders are sent.';
  text('kalshi-note', note);
}
function renderSpecialists() {
  const people = list(obj(state.values.ecosystem).specialists);
  text('specialist-count', `${people.length} registered`);
  const root = $('specialists'); if (!root) return;
  if (!people.length) { const p = document.createElement('p'); p.className = 'empty'; p.textContent = state.values.ecosystem === null ? 'Specialist registry unavailable.' : 'No registered specialists returned.'; root.replaceChildren(p); return; }
  root.replaceChildren(...people.slice(0, 20).map((person) => {
    const row = document.createElement('div'); row.className = 'specialist';
    const name = document.createElement('strong'); name.textContent = value(person.name, 'Unnamed specialist');
    const status = document.createElement('span'); status.textContent = `${value(person.state, 'state unknown')} · ${person.reliability == null ? 'reliability unmeasured' : `reliability ${person.reliability}`}`;
    row.append(name, status); return row;
  }));
}
function renderTrench() {
  const data = obj(state.values.trench); const progress = obj(data.progress); const health = obj(progress.provider_health);
  tag('trench-status', progress.collector_status || 'UNKNOWN');
  text('trench-observations', progress.valid_observations);
  text('trench-pending', progress.labels_pending);
  text('trench-matured', progress.labels_matured);
  text('trench-walkforward', progress.walk_forward_labels);
  const records = obj(health.records); const primary = records['solana_rpc:primary'] || records['solana_rpc'] || health.solana_rpc;
  const fallback = records['solana_rpc:fallback'];
  text('trench-primary', primary?.state || primary?.status || health.solana_rpc || 'Unknown');
  text('trench-fallback', fallback?.state || fallback?.status || (progress.solana_rpc_fallback_configured === true ? 'configured; state unknown' : 'not configured'));
  const note = progress.latest_failure?.error_class || progress.latest_failure?.reason || progress.eligibility_reason || 'Forward labels require their full maturation window; no future information is backfilled.';
  text('trench-note', `${note} · ${progress.threshold ? `qualification ${progress.training_labels ?? '—'}/${progress.threshold.training} training, ${progress.walk_forward_labels ?? '—'}/${progress.threshold.walk_forward} walk-forward` : 'qualification threshold unavailable'}`);
}
function rowsFor(section) { return list(obj(obj(state.values.operations).sections)[section]?.rows); }
function renderJournal() {
  const sections = obj(obj(state.values.operations).sections);
  const trials = rowsFor('experiments'); const runs = rowsFor('research_runs'); const decisions = rowsFor('decisions'); const events = rowsFor('activity');
  const summary = $('journal-summary');
  if (summary) summary.replaceChildren(...[['Trials', trials.length], ['Runs', runs.length], ['Decisions', decisions.length], ['Events', events.length]].map(([name, count]) => { const span = document.createElement('span'); span.textContent = `${name} ${count}`; return span; }));
  const all = [
    ...runs.map((row) => ({ kind: 'Research run', at: row.completed_at || row.created_at, message: `${value(row.status, 'status unknown')} · ${value(row.specialist, 'specialist unknown')} · ${value(row.kind, 'research')}` })),
    ...decisions.map((row) => ({ kind: 'Decision', at: row.created_at, message: `${value(row.venue, 'venue unknown')} · ${value(row.market_id, 'market unknown')}` })),
    ...events.map((row) => ({ kind: value(row.stage, 'Runtime event'), at: row.created_at, message: `${value(row.status, 'status unknown')} · ${value(row.detail, row.tool || 'persisted event')}` })),
    ...trials.map((row) => ({ kind: 'Trial', at: row.created_at, message: `${value(row.status, 'status unknown')} · ${value(row.family, 'family unknown')} · ${value(row.hypothesis, 'hypothesis unavailable')}` })),
  ].sort((a, b) => String(b.at || '').localeCompare(String(a.at || ''))).slice(0, 18);
  const root = $('journal'); if (!root) return;
  if (!all.length) { const li = document.createElement('li'); li.className = 'empty'; li.textContent = state.values.operations === null ? 'Operations projection unavailable.' : 'No recent persisted research records.'; root.replaceChildren(li); return; }
  root.replaceChildren(...all.map((record) => { const li = document.createElement('li'); const time = document.createElement('time'); time.textContent = `${record.kind} · ${fmtTime(record.at)}`; const body = document.createElement('span'); body.textContent = record.message; li.append(time, body); return li; }));
}
function renderProviders() {
  const data = obj(state.values.providers); const rows = [];
  for (const [name, item] of Object.entries(data)) {
    if (!item || typeof item !== 'object') continue;
    const status = item.status || (item.configured_provider_ready === true ? 'ready' : item.configured_provider_ready === false ? 'not ready' : 'observed');
    rows.push([name.replaceAll('_', ' '), status]);
  }
  const root = $('providers'); if (!root) return;
  text('health-count', `${rows.length} records`);
  if (!rows.length) { const p = document.createElement('p'); p.className = 'empty'; p.textContent = 'Provider health unavailable; no assumptions made.'; root.replaceChildren(p); return; }
  root.replaceChildren(...rows.slice(0, 14).map(([name, status]) => { const row = document.createElement('div'); row.className = 'provider'; const label = document.createElement('strong'); label.textContent = name; const tagNode = document.createElement('span'); tagNode.textContent = String(status).toUpperCase().replaceAll('_', ' '); row.append(label, tagNode); return row; }));
}

function renderMissions() {
  const missions = rowsFor('missions');
  const handoffs = rowsFor('handoffs');
  const events = rowsFor('mission_events');
  const runs = rowsFor('research_runs');
  const lessons = rowsFor('lessons');
  const discoverCount = list(obj(state.values.discovery).records).length + list(state.values.radar).length;
  const challengeCount = events.filter((row) => /critic|review/i.test(String(row.event_type || row.status || ''))).length;
  const journey = { discover: discoverCount, assign: missions.length, challenge: challengeCount, measure: runs.length, learn: lessons.length };
  document.querySelectorAll('.flow-step[data-stage]').forEach((step) => {
    const count = journey[step.dataset.stage] || 0;
    step.dataset.state = count > 0 ? 'recorded' : 'awaiting';
    const stateLabel = step.querySelector('small');
    if (stateLabel) stateLabel.textContent = count > 0 ? `${count} recent` : 'awaiting';
  });
  text('mission-count', `${missions.length} recent missions · ${handoffs.length} handoffs`);
  const root = $('mission-board'); if (!root) return;
  if (!missions.length) {
    const p = document.createElement('p'); p.className = 'empty';
    p.textContent = state.values.operations === null ? 'Mission projection unavailable.' : 'No persisted mission assignments in the current projection.';
    root.replaceChildren(p); return;
  }
  root.replaceChildren(...missions.slice(0, 14).map((mission) => {
    const card = document.createElement('article'); card.className = 'mission-card';
    const top = document.createElement('div'); top.className = 'mission-top';
    const title = document.createElement('strong'); title.className = 'mission-title';
    title.textContent = value(mission.objective, mission.mission_id || 'Persisted mission');
    const stateTag = document.createElement('span'); stateTag.className = `state-tag ${stateClass(mission.status)}`;
    stateTag.textContent = value(mission.status, 'UNKNOWN').toUpperCase();
    top.append(title, stateTag);
    const meta = document.createElement('div'); meta.className = 'mission-meta';
    const relatedHandoffs = handoffs.filter((row) => row.mission_id === mission.mission_id);
    const relatedEvents = events.filter((row) => row.mission_id === mission.mission_id);
    meta.textContent = `${value(mission.specialist, 'unassigned')} · ${fmtTime(mission.updated_at || mission.created_at)} · ${relatedHandoffs.length} handoffs · ${relatedEvents.length} events`;
    card.append(top, meta);
    const detailText = document.createElement('p'); detailText.className = 'mission-detail';
    detailText.textContent = `Trial ${value(mission.trial_id, 'unlinked')} · evidence ${value(mission.evidence_hash, 'not recorded')}`;
    card.append(detailText);
    const disclosure = document.createElement('details');
    const summary = document.createElement('summary'); summary.textContent = 'View persisted result and handoffs';
    const pre = document.createElement('pre');
    const data = { mission_id: mission.mission_id, result: mission.result_json, handoffs: relatedHandoffs, events: relatedEvents };
    pre.textContent = JSON.stringify(data, null, 2).slice(0, 12000);
    disclosure.append(summary, pre); card.append(disclosure);
    return card;
  }));
}

function renderCrawler() {
  const data = obj(state.values.discovery);
  const records = list(data.records);
  const issues = records.flatMap((record) => list(record.issues).map((issue) => ({ ...issue, evidence: record })));
  const latest = records[0];
  const latestAge = latest?.retrieved_at ? (Date.now() - new Date(latest.retrieved_at).getTime()) / 1000 : Infinity;
  const recentScan = latestAge >= 0 && latestAge <= 120;
  tag('crawler-status', recentScan ? 'RECENT SCAN' : data.status || 'UNKNOWN');
  document.querySelector('.crawler-panel')?.toggleAttribute('data-recent-scan', recentScan);
  const summary = $('crawler-summary');
  if (summary) {
    if (!records.length) {
      const p = document.createElement('p'); p.className = 'empty';
      p.textContent = state.values.discovery === null ? 'Discovery evidence endpoint unavailable.' : 'No persisted public discovery runs found.';
      summary.replaceChildren(p);
    } else {
      const metrics = [
        ['Persisted scans', records.length],
        ['Latest source', latest.source],
        ['Latest result metadata', `${latest.result_count} / ${latest.total_count ?? 'unknown'} listed`],
        ['Latest evidence', `${latest.integrity_verified ? 'HASH VERIFIED' : 'HASH MISMATCH'} · ${age(latest.retrieved_at)}`],
      ];
      summary.replaceChildren(...metrics.map(([label, content]) => {
        const item = document.createElement('div'); item.className = 'crawler-metric';
        const name = document.createElement('span'); name.textContent = label;
        const valueNode = document.createElement('strong'); valueNode.textContent = value(content);
        item.append(name, valueNode); return item;
      }));
    }
  }
  const root = $('crawler-results'); if (!root) return;
  if (!issues.length) {
    const p = document.createElement('p'); p.className = 'empty';
    p.textContent = records.length ? 'Persisted discovery runs contain no issue rows to display.' : 'Issue leads will appear here only after an actual collector run persists them.';
    root.replaceChildren(p); return;
  }
  root.replaceChildren(...issues.slice(0, 20).map((entry) => {
    const row = document.createElement('article'); row.className = 'crawl-result';
    const main = document.createElement('div');
    const link = document.createElement('a'); link.href = entry.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = entry.title || 'Public GitHub issue';
    const details = document.createElement('small'); details.textContent = `${entry.repository || 'Repository unknown'} · ${entry.state} · ${fmtTime(entry.updated_at)} · evidence ${entry.evidence.integrity_verified ? 'verified' : 'unverified'}`;
    main.append(link, details);
    const labels = document.createElement('span'); labels.className = 'crawl-tags';
    labels.textContent = entry.labels?.length ? entry.labels.join(' · ') : 'PAYMENT NOT VERIFIED';
    row.append(main, labels); return row;
  }));
}

let selectedNetworkNode = 'runtime';
function renderSpiderGraph() {
  const svg = $('research-web'); if (!svg) return;
  const ns = 'http://www.w3.org/2000/svg';
  const center = { x: 450, y: 245 };
  const nodes = [{ id: 'runtime', type: 'core', label: 'NOEMA', name: 'NOEMA runtime',
    detail: `Runtime ${value(obj(state.values.agent).runtime_state, 'unknown')} · cycle health ${value(obj(state.values.agent).health, 'unknown')}`,
    status: value(obj(state.values.agent).runtime_state, 'unknown') }];
  const edges = [];
  const add = (row) => { if (nodes.length < 38) nodes.push(row); };
  const agents = list(obj(state.values.ecosystem).specialists).slice(0, 7);
  agents.forEach((agent, index) => {
    const rawName = String(agent.name || '').toLowerCase();
    const assignedRunning = rowsFor('missions').some((mission) =>
      String(mission.specialist || '').toLowerCase() === rawName && isPersistedWorkActive(mission.status));
    add({ id: `agent:${index}`, type: 'agent', label: String(agent.name || 'specialist').slice(0, 15),
      name: value(agent.name), detail: `${assignedRunning ? 'Persisted mission running' : 'Registered specialist'} · ${value(agent.state)} · reliability ${value(agent.reliability, 'unmeasured')}`,
      status: assignedRunning ? 'running' : value(agent.state), rawName, working: assignedRunning });
  });
  const missions = rowsFor('missions').slice(0, 7);
  missions.forEach((mission, index) => add({ id: `mission:${index}`, type: 'mission', label: `M${index + 1}`,
    name: value(mission.objective, `Mission ${index + 1}`), detail: `${value(mission.status)} · ${value(mission.specialist, 'unassigned')} · trial ${value(mission.trial_id, 'unlinked')}`,
    status: value(mission.status), mission, working: isPersistedWorkActive(mission.status), rawName: String(mission.specialist || '').toLowerCase() }));
  const runs = rowsFor('research_runs').slice(0, 7);
  runs.forEach((run, index) => add({ id: `run:${index}`, type: 'run', label: `R${index + 1}`,
    name: `Research run ${value(run.kind, '')}`.trim(), detail: `${value(run.status)} · ${value(run.specialist, 'unknown specialist')} · trial ${value(run.trial_id, 'unlinked')} · cost ${value(run.compute_cost_usd, 'unavailable')}`,
    status: value(run.status), run, working: isPersistedWorkActive(run.status) }));
  const discovery = list(obj(state.values.discovery).records).slice(0, 4);
  discovery.forEach((record, index) => add({ id: `crawl:${index}`, type: 'crawl', label: `C${index + 1}`,
    name: value(record.source), detail: `${record.result_count} listed metadata rows · ${value(record.query, 'query not persisted')} · hash ${String(record.payload_hash || '').slice(0, 12)} · ${record.integrity_verified ? 'integrity verified' : 'integrity mismatch'}`,
    status: record.integrity_verified ? 'verified' : 'integrity mismatch', record }));
  const issues = discovery.flatMap((record, recordIndex) => list(record.issues).map((issue, issueIndex) => ({ record, recordIndex, issue, issueIndex }))).slice(0, 8);
  issues.forEach(({ record, recordIndex, issue, issueIndex }, index) => add({ id: `issue:${recordIndex}:${issueIndex}`, type: 'issue', label: `I${index + 1}`,
    name: value(issue.title, 'Public issue'), detail: `${value(issue.repository, 'repository unknown')} · payment not verified · evidence ${record.integrity_verified ? 'verified' : 'unverified'}`,
    status: 'lead only', url: issue.url, record }));
  const forecasts = list(state.values.radar).slice(0, 7);
  forecasts.forEach((forecast, index) => add({ id: `market:${index}`, type: 'market', label: `F${index + 1}`,
    name: value(forecast.title, forecast.market_id), detail: `${value(forecast.venue)} · ${value(forecast.decision)} · robust edge estimate ${value(forecast.robust_edge, 'unavailable')} · captured ${fmtTime(forecast.captured_at)}`,
    status: value(forecast.decision), forecast, evidenceIds: list(forecast.evidence_ids) }));
  const agentsByName = new Map(nodes.filter((node) => node.type === 'agent').map((node) => [node.rawName, node.id]));
  const missionsById = new Map(nodes.filter((node) => node.type === 'mission').map((node) => [String(node.mission?.mission_id || ''), node.id]));
  const missionByTrial = new Map(nodes.filter((node) => node.type === 'mission' && node.mission?.trial_id).map((node) => [String(node.mission.trial_id), node.id]));
  const crawlsByEvidence = new Map(nodes.filter((node) => node.type === 'crawl').map((node) => [String(node.record?.evidence_id || ''), node.id]));
  const specialistNodes = nodes.filter((node) => node.type === 'agent');
  specialistNodes.forEach((node) => edges.push(['runtime', node.id, 'registered specialist']));
  nodes.filter((node) => node.type === 'mission').forEach((node) => {
    const assignedAgent = agentsByName.get(node.rawName);
    if (assignedAgent) edges.push([assignedAgent, node.id, 'mission assignment']);
    else if (node.rawName === 'noema') edges.push(['runtime', node.id, 'mission assignment']);
  });
  nodes.filter((node) => node.type === 'run').forEach((node) => {
    const linked = missionsById.get(String(node.run?.mission_id || '')) || missionByTrial.get(String(node.run?.trial_id || ''));
    if (linked) edges.push([linked, node.id, 'persisted run link']);
  });
  discovery.forEach((record, index) => {
    const crawlId = `crawl:${index}`;
    if (nodes.some((node) => node.id === crawlId)) edges.push(['runtime', crawlId, 'persisted crawl evidence']);
  });
  nodes.filter((node) => node.type === 'issue').forEach((node) => {
    const match = String(node.id).match(/^issue:(\d+):/);
    if (match && crawlsByEvidence.has(String(discovery[Number(match[1])]?.evidence_id || ''))) {
      edges.push([crawlsByEvidence.get(String(discovery[Number(match[1])].evidence_id)), node.id, 'returned by source scan']);
    }
  });
  nodes.filter((node) => node.type === 'market').forEach((node) => {
    const matchedEvidence = node.evidenceIds?.map((id) => crawlsByEvidence.get(String(id))).find(Boolean);
    edges.push([matchedEvidence || 'runtime', node.id, matchedEvidence ? 'forecast evidence ID' : 'persisted forecast record']);
  });
  // Mark only records whose persisted state explicitly says work is running.
  const workingIds = new Set(nodes.filter((node) => node.working).map((node) => node.id));
  nodes.filter((node) => node.type === 'mission' && node.working).forEach((mission) => {
    const assignedAgent = agentsByName.get(mission.rawName);
    if (assignedAgent) workingIds.add(assignedAgent);
  });
  const groups = [
    { types: ['agent'], from: 205, to: 335, radius: 190 },
    { types: ['mission'], from: 285, to: 345, radius: 200 },
    { types: ['run'], from: 0, to: 55, radius: 205 },
    { types: ['crawl'], from: 65, to: 110, radius: 200 },
    { types: ['issue'], from: 118, to: 170, radius: 225 },
    { types: ['market'], from: 185, to: 245, radius: 210 },
  ];
  groups.forEach(({ types, from, to, radius }) => {
    const groupNodes = nodes.filter((node) => types.includes(node.type));
    groupNodes.forEach((node, index) => {
      const angle = (groupNodes.length === 1 ? (from + to) / 2 : from + (to - from) * index / (groupNodes.length - 1)) * Math.PI / 180;
      node.x = center.x + Math.cos(angle) * radius;
      node.y = center.y + Math.sin(angle) * radius;
    });
  });
  nodes[0].x = center.x; nodes[0].y = center.y;
  const recentDiscoveryAt = list(obj(state.values.discovery).records)[0]?.retrieved_at;
  const recentDiscovery = Boolean(recentDiscoveryAt) && (Date.now() - new Date(recentDiscoveryAt).getTime()) / 1000 <= 120;
  svg.replaceChildren();
  const defs = document.createElementNS(ns, 'defs');
  const filter = document.createElementNS(ns, 'filter'); filter.id = 'web-glow';
  const blur = document.createElementNS(ns, 'feGaussianBlur'); blur.setAttribute('stdDeviation', '3'); blur.setAttribute('result', 'blur');
  filter.append(blur); defs.append(filter); svg.append(defs);
  const valid = new Map(nodes.map((node) => [node.id, node]));
  edges.forEach(([from, to]) => {
    const a = valid.get(from), b = valid.get(to); if (!a || !b) return;
    const line = document.createElementNS(ns, 'line');
    line.setAttribute('x1', a.x); line.setAttribute('y1', a.y); line.setAttribute('x2', b.x); line.setAttribute('y2', b.y);
    const recentCrawlerPath = recentDiscovery && (a.type === 'crawl' || a.type === 'issue' || b.type === 'crawl' || b.type === 'issue');
    const connectedToRunningWork = workingIds.has(from) || workingIds.has(to);
    line.setAttribute('class', `${recentCrawlerPath || connectedToRunningWork ? 'network-edge active' : 'network-edge'}`); svg.append(line);
  });
  const selected = nodes.find((node) => node.id === selectedNetworkNode) || nodes[0];
  nodes.forEach((node) => {
    const group = document.createElementNS(ns, 'g');
    group.setAttribute('class', `web-node ${node.type}${node.working ? ' working' : ''}${node.id === selected.id ? ' selected' : ''}`);
    group.setAttribute('transform', `translate(${node.x},${node.y})`);
    group.setAttribute('role', 'button'); group.setAttribute('tabindex', '0');
    group.setAttribute('aria-label', `${node.type}: ${node.name}. ${node.detail}`);
    const title = document.createElementNS(ns, 'title'); title.textContent = `${node.name} — ${node.detail}`;
    const circle = document.createElementNS(ns, 'circle'); circle.setAttribute('r', node.type === 'core' ? '11' : '6');
    const label = document.createElementNS(ns, 'text');
    label.setAttribute('x', node.x >= center.x ? '11' : '-11'); label.setAttribute('y', '-9');
    label.setAttribute('text-anchor', node.x >= center.x ? 'start' : 'end'); label.textContent = node.label;
    group.append(title, circle, label);
    const select = () => { selectedNetworkNode = node.id; showNetworkDetail(node); renderSpiderGraph(); };
    group.addEventListener('click', select);
    group.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } });
    svg.append(group);
  });
  showNetworkDetail(selected);
}
function showNetworkDetail(node) {
  const root = $('network-detail'); if (!root || !node) return;
  root.replaceChildren();
  const eyebrow = document.createElement('p'); eyebrow.className = 'eyebrow'; eyebrow.textContent = String(node.type).toUpperCase();
  const heading = document.createElement('strong'); heading.textContent = node.name;
  const kind = document.createElement('p'); kind.className = 'node-kind'; kind.textContent = String(node.status || 'persisted record').toUpperCase();
  const description = document.createElement('p'); description.textContent = node.detail || 'No additional persisted details.';
  root.append(eyebrow, heading, kind, description);
  const identity = node.record?.evidence_id || node.mission?.mission_id || node.run?.id || node.forecast?.market_id;
  if (identity) { const code = document.createElement('code'); code.textContent = String(identity); root.append(code); }
  if (node.url && node.url.startsWith('https://github.com/')) {
    const link = document.createElement('a'); link.href = node.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = 'Open source record ↗'; root.append(link);
  }
}

document.querySelectorAll('[data-market-filter]').forEach((button) => button.addEventListener('click', () => {
  state.filter = button.dataset.marketFilter || 'all';
  document.querySelectorAll('[data-market-filter]').forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
  renderRadar();
}));
$('refresh')?.addEventListener('click', refresh);
let installPrompt = null;
window.addEventListener('beforeinstallprompt', (event) => { event.preventDefault(); installPrompt = event; });
$('install-app')?.addEventListener('click', async () => {
  if (installPrompt) {
    installPrompt.prompt();
    await installPrompt.userChoice;
    installPrompt = null;
    return;
  }
  const dialog = $('install-dialog');
  if (dialog?.showModal) dialog.showModal();
});
const streamStatus = $('stream-label');
try {
  const stream = new EventSource('/api/runtime-stream');
  stream.addEventListener('open', () => { if (streamStatus) { streamStatus.textContent = 'Change stream connected'; streamStatus.dataset.state = 'connected'; } });
  stream.addEventListener('ready', () => { if (streamStatus) { streamStatus.textContent = 'Change stream ready'; streamStatus.dataset.state = 'connected'; } refresh(); });
  stream.addEventListener('change', () => { if (streamStatus) streamStatus.textContent = 'Persisted change · refreshing'; refresh(); });
  stream.addEventListener('unavailable', () => { if (streamStatus) { streamStatus.textContent = 'Change stream unavailable · polling'; streamStatus.dataset.state = 'polling'; } });
  stream.addEventListener('error', () => { if (streamStatus) { streamStatus.textContent = 'Change stream reconnecting · polling'; streamStatus.dataset.state = 'polling'; } });
} catch { if (streamStatus) streamStatus.textContent = 'Change stream unavailable · polling'; }
refresh();
setInterval(refresh, 15000);
