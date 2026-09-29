import { createNoemaWorld } from './noema-world.mjs';

const $ = (id) => document.getElementById(id);
const svgNS = 'http://www.w3.org/2000/svg';
const views = {
  sessions: ['Autonomous sessions', ['session_id', 'status', 'provider', 'model']],
  activity: ['Runtime activity', ['stage', 'status', 'tool', 'detail']],
  wallet_transactions: ['Wallet transactions', ['created_at', 'event_type', 'amount_usd', 'payload']],
  missions: ['Missions', ['mission_id', 'status', 'specialist', 'objective']],
  mission_events: ['Mission history', ['mission_id', 'actor', 'event_type', 'status', 'detail']],
  handoffs: ['Specialist handoffs', ['mission_id', 'from_specialist', 'to_specialist', 'status', 'objective']],
  lessons: ['Learning', ['mission_id', 'trial_id', 'next_priority', 'lesson', 'created_at']],
  research_runs: ['Research runs', ['mission_id', 'trial_id', 'specialist', 'kind', 'status', 'elapsed_seconds']],
  queue: ['Research queue', ['market_id', 'status', 'priority', 'request']],
  specialists: ['Specialists', ['name', 'family', 'state', 'resolved', 'calibration_error']],
  experiments: ['Experiments', ['trial_id', 'family', 'status', 'hypothesis']],
  decisions: ['Forecasts / PASS', ['market_id', 'decision', 'probability_yes', 'model_version']],
  reviews: ['Promotion reviews', ['specialist', 'previous_state', 'next_state', 'created_at']],
  reservations: ['Cost reservations', ['market_id', 'estimated_usd', 'estimated_tokens', 'created_at']],
  outcomes: ['Recorded outcomes', ['market_id', 'outcome_yes', 'resolved_at', 'first_seen_at']],
};
let snapshot, economicsSnapshot, trenchSnapshot, providerHealth, providerHealthAt = 0;
let walletSnapshot, gatewaySnapshot;
const capabilityFreshness = { providers: 'unknown', wallets: 'unknown', gateway: 'unknown',
  venues: 'unknown', operations: 'unknown', providersAt: null, walletsAt: null,
  gatewayAt: null, venuesAt: null, operationsAt: null };
let predictionVenuesSnapshot, predictionVenuesAt = 0;
let stripeEconomySnapshot;
let knowledgeSnapshot;
let activeView = 'sessions', selected, history = [], focusMissionId;
let refreshing = false, refreshQueued = false, latestEventId = null, changeTimer;
let streamRefreshTimer = null, lastStreamRefresh = 0;
const noemaWorld = createNoemaWorld();

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function value(v) { return v === null || v === undefined || v === '' ? 'Unknown' : String(v); }
function label(key) { return key.replaceAll('_', ' '); }
function parseObject(v) {
  if (v && typeof v === 'object' && !Array.isArray(v)) return v;
  try { const parsed = JSON.parse(v ?? '{}'); return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}; }
  catch { return {}; }
}
function money(amount) {
  if (amount === null || amount === undefined || !/^-?\d+(?:\.\d+)?$/.test(String(amount))) return 'Unknown';
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(Number(amount));
}
function moneyPrecise(amount) {
  const raw = String(amount ?? '');
  if (!/^-?\d+(?:\.\d+)?$/.test(raw)) return 'Unknown';
  const negative = raw.startsWith('-');
  const [wholeRaw, fractionRaw = ''] = raw.replace(/^-/, '').split('.');
  const whole = wholeRaw.replace(/^0+(?=\d)/, '').replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const fraction = fractionRaw.replace(/0+$/, '');
  return `${negative ? '-$' : '$'}${whole}${fraction ? `.${fraction}` : ''}`;
}
function dateLabel(iso, options = {}) {
  if (!iso) return 'time unavailable';
  const date = new Date(iso);
  return Number.isNaN(date.valueOf()) ? 'time unavailable' : date.toLocaleString(undefined, options);
}
function shortTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.valueOf()) ? '—' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
function section(name) { return snapshot?.sections?.[name] ?? { status: 'unavailable', rows: [], has_more: false }; }
function rows(name) { return section(name).rows ?? []; }
function keyOf(view, row) {
  return `${view}:${row.mission_id ?? row.handoff_id ?? row.id ?? row.task_id ?? row.trial_id ?? row.session_id ?? row.name ?? `${row.venue}:${row.market_id}`}`;
}
function selectRecord(view, row, remember = true) {
  if (remember && selected) history = [...history.slice(-19), selected];
  selected = { view, row };
  activeView = view;
  $('deep-records').open = true;
  $('search').value = '';
  renderArchive();
  renderDetail();
  $('deep-records').scrollIntoView({ block: 'nearest', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
}
function renderDetail() {
  $('detail').replaceChildren();
  $('links').replaceChildren();
  $('back').hidden = !history.length;
  if (!selected) return;
  $('selection').textContent = views[selected.view]?.[0] ?? 'Selected record';
  for (const [key, val] of Object.entries(selected.row)) {
    const display = val && typeof val === 'object' ? JSON.stringify(val, null, 2) : value(val);
    $('detail').append(element('dt', label(key)), element('dd', display));
  }
  for (const [view, dataset] of Object.entries(snapshot?.sections ?? {})) {
    const matches = dataset.rows.filter((row) => keyOf(view, row) !== keyOf(selected.view, selected.row) && (
      (selected.row.session_id && row.session_id === selected.row.session_id) ||
      (selected.row.mission_id && row.mission_id === selected.row.mission_id) ||
      (selected.row.trial_id && (row.parent_trial_id === selected.row.trial_id || row.trial_id === selected.row.trial_id)) ||
      ((selected.row.name || selected.row.specialist) && (row.name || row.specialist) === (selected.row.name || selected.row.specialist)) ||
      (selected.row.market_id && row.market_id === selected.row.market_id && row.venue && selected.row.venue === row.venue)
    ));
    for (const row of matches.slice(0, 6)) {
      const button = element('button', `${views[view]?.[0] ?? view} · ${row.market_id ?? row.specialist ?? row.name ?? row.trial_id ?? row.session_id ?? row.id}`);
      button.type = 'button';
      button.onclick = () => selectRecord(view, row);
      $('links').append(button);
    }
  }
  if ($('links').childElementCount) $('links').prepend(element('p', 'Related persisted records joined by exact identifiers.'));
}
function renderArchive() {
  $('views').replaceChildren();
  for (const [key, [name]] of Object.entries(views)) {
    const button = element('button', name);
    button.type = 'button';
    button.setAttribute('aria-pressed', String(activeView === key));
    button.onclick = () => { activeView = key; $('search').value = ''; renderArchive(); };
    $('views').append(button);
  }
  const [name, columns] = views[activeView] ?? views.sessions;
  const data = section(activeView);
  $('title').textContent = name;
  const head = document.createElement('tr');
  columns.forEach((key) => head.append(element('th', label(key))));
  $('columns').replaceChildren(head);
  $('rows').replaceChildren();
  const query = $('search').value.trim().toLowerCase();
  const filtered = data.rows.filter((row) => Object.values(row).some((entry) => value(entry).toLowerCase().includes(query)));
  $('record-status').textContent = `${data.status.replaceAll('_', ' ')} · ${filtered.length} loaded records${data.has_more ? ' · recent records only' : ''}`;
  for (const row of filtered) {
    const tr = document.createElement('tr');
    tr.dataset.selected = String(selected && keyOf(activeView, row) === keyOf(selected.view, selected.row));
    columns.forEach((column, index) => {
      const td = document.createElement('td');
      if (index === 0) {
        const button = element('button', value(row[column]));
        button.type = 'button';
        button.onclick = () => selectRecord(activeView, row);
        td.append(button);
      } else td.textContent = value(row[column]);
      tr.append(td);
    });
    $('rows').append(tr);
  }
}

function latestSession() {
  const session = rows('sessions')[0];
  return session ?? null;
}
function runResult(run) { return parseObject(run?.result); }
function missionById(id) { return rows('missions').find((mission) => mission.mission_id === id); }
function currentMission() {
  const missions = rows('missions').filter((mission) => mission.status !== 'superseded');
  if (focusMissionId) {
    const selectedMission = missions.find((mission) => mission.mission_id === focusMissionId);
    if (selectedMission) return selectedMission;
  }
  return missions.find((mission) => ['running', 'claimed', 'waiting'].includes(mission.status)) ?? missions[0] ?? null;
}
function formatCount(n) { return new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(n); }
function svgNode(tag, attributes = {}, text) {
  const node = document.createElementNS(svgNS, tag);
  for (const [key, val] of Object.entries(attributes)) node.setAttribute(key, String(val));
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function runMission(run) {
  if (run.mission_id) return missionById(run.mission_id);
  return rows('missions').find((mission) => mission.trial_id === run.trial_id && mission.run_id === run.id);
}
function renderEvidenceChart() {
  const all = rows('research_runs').filter((run) => run.status === 'completed' && Number.isFinite(runResult(run).observations));
  const runs = all.slice(0, 10).reverse();
  const latest = all[0] ? runResult(all[0]) : null;
  $('evidence-total').textContent = latest ? formatCount(latest.observations) : '—';
  $('evidence-run-count').textContent = all.length ? `${all.length} RUN${all.length === 1 ? '' : 'S'} · EVIDENCE ONLY` : 'NO RECORDED RUNS';
  $('chart-range').textContent = runs.length
    ? `${shortTime(runs[0].created_at)} — ${shortTime(runs[runs.length - 1].created_at)} · local time`
    : 'No experiment history';
  const host = $('evidence-chart');
  host.replaceChildren();
  if (!runs.length) {
    host.append(element('p', 'No bounded experiment has returned a measurable observation count.', 'chart-empty'));
    return;
  }
  const width = 900, height = 198, left = 50, right = 12, top = 15, bottom = 35;
  const plotW = width - left - right, plotH = height - top - bottom;
  const max = Math.max(1, ...runs.map((run) => runResult(run).observations));
  const axisMax = Math.ceil(max / 1000) * 1000 || max;
  const svg = svgNode('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-labelledby': 'evidence-chart-title evidence-chart-description' });
  svg.append(svgNode('title', { id: 'evidence-chart-title' }, 'Observed evidence per completed research run'));
  svg.append(svgNode('desc', { id: 'evidence-chart-description' }, 'Bars are persisted observation counts; they do not represent profit, unique markets, or economic value. Select a run to inspect its mission.'));
  for (let tick = 0; tick <= 4; tick += 1) {
    const y = top + (plotH * tick / 4);
    const valueAtTick = Math.round(axisMax * (4 - tick) / 4);
    svg.append(svgNode('path', { d: `M${left} ${y}H${width - right}`, class: 'chart-grid' }));
    svg.append(svgNode('text', { x: left - 8, y: y + 3, 'text-anchor': 'end', class: 'chart-axis' },
      valueAtTick >= 1000 ? `${(valueAtTick / 1000).toFixed(valueAtTick % 1000 ? 1 : 0)}k` : valueAtTick));
  }
  const slot = plotW / runs.length;
  for (const [index, run] of runs.entries()) {
    const result = runResult(run);
    const observations = result.observations;
    const barW = Math.min(42, slot * .46);
    const x = left + slot * index + (slot - barW) / 2;
    const barH = Math.max(3, plotH * observations / axisMax);
    const y = top + plotH - barH;
    const mission = runMission(run);
    const accepted = result.critic_review?.result_accepted === true;
    const g = svgNode('g', { class: 'chart-run', role: 'button', tabindex: '0', 'aria-label': `${run.kind}, ${formatCount(observations)} observations, ${accepted ? 'critic accepted' : 'critic status not accepted'}, ${dateLabel(run.created_at)}` });
    g.append(svgNode('rect', { x: x - Math.max(2, slot * .1), y: top, width: Math.max(slot - slot * .2, barW), height: plotH + 2, class: 'chart-hit' }));
    g.append(svgNode('rect', { x, y, width: barW, height: barH, rx: 1, class: 'chart-bar', 'data-state': accepted ? 'accepted' : 'unreviewed' }));
    g.append(svgNode('text', { x: x + barW / 2, y: Math.max(top + 9, y - 5), 'text-anchor': 'middle', class: 'chart-value' }, formatCount(observations)));
    g.append(svgNode('text', { x: x + barW / 2, y: height - 18, 'text-anchor': 'middle', class: 'chart-time' }, shortTime(run.created_at)));
    g.addEventListener('click', () => { if (mission) { focusMissionId = mission.mission_id; renderWorld(); } });
    g.addEventListener('keydown', (event) => { if ((event.key === 'Enter' || event.key === ' ') && mission) { event.preventDefault(); focusMissionId = mission.mission_id; renderWorld(); } });
    svg.append(g);
  }
  svg.append(svgNode('path', { d: `M${left} ${top + plotH}H${width - right}`, class: 'chart-grid' }));
  host.append(svg);
}

function laneKey(value) {
  const text = String(value ?? '').toLowerCase().replaceAll('-', '_');
  if (['web3', 'solana', 'onchain', 'on_chain', 'evm', 'crypto'].some((term) => text.includes(term))) return 'web3';
  if (['prediction', 'kalshi', 'polymarket', 'forecast'].some((term) => text.includes(term))) return 'prediction';
  if (['saas', 'micro_saas', 'software_service'].some((term) => text.includes(term))) return 'saas';
  if (text.includes('api')) return 'apis';
  if (text.includes('data_product')) return 'data';
  if (text.includes('subscription')) return 'subscriptions';
  if (text.includes('automation') || text.includes('workflow')) return 'automation';
  if (text.includes('research_product') || text.includes('intelligence_product')) return 'research';
  if (text.includes('agent_service')) return 'agent_services';
  if (text.includes('other') || text.includes('internet_native')) return 'other';
  return null;
}
function renderLanes() {
  const lanes = snapshot?.economic_lanes?.lanes ?? [];
  const activeLanes = lanes.filter((lane) => lane.activity_status !== 'no_recorded_activity');
  const inactive = lanes.filter((lane) => lane.activity_status === 'no_recorded_activity');
  const allocations = snapshot?.attention_allocations?.rows ?? [];
  $('lane-asof').textContent = snapshot?.as_of ? `Evidence snapshot · ${dateLabel(snapshot.as_of)}` : 'Lane evidence unavailable';
  $('lane-rows').replaceChildren();
  if (!activeLanes.length) $('lane-rows').append(element('p', 'No lane-tagged work or capabilities have been recorded.', 'quiet'));
  for (const lane of activeLanes) {
    const laneAllocations = allocations.filter((item) => laneKey(item.family) === lane.key);
    const share = laneAllocations.reduce((sum, item) => sum + Number(item.attention_fraction ?? 0), 0);
    const hasRuns = Number(lane.run_count) > 0;
    const activity = hasRuns ? 'research' : 'capability';
    const card = element('article', undefined, 'lane-card');
    card.dataset.activity = activity;
    const text = document.createElement('div');
    text.append(element('span', hasRuns ? 'RECORDED RESEARCH' : 'REGISTERED CAPABILITY', 'lane-mark'), element('h3', lane.name));
    const qualifier = hasRuns ? `${lane.run_count} persisted run${lane.run_count === 1 ? '' : 's'} · results do not establish revenue` : 'Specialist available; no mission experiment recorded';
    text.append(element('p', qualifier));
    card.append(text, element('span', `${lane.trial_count} trial${lane.trial_count === 1 ? '' : 's'}`, 'lane-count'));
    const metrics = element('div', undefined, 'lane-metrics');
    metrics.append(element('span', `${lane.specialist_count} registered specialist${lane.specialist_count === 1 ? '' : 's'}`));
    if (laneAllocations.length) metrics.append(element('span', `research attention ${Math.round(share * 100)}%`));
    metrics.append(element('span', 'revenue unmeasured'), element('span', 'net economics unmeasured'));
    card.append(metrics);
    if (laneAllocations.length) {
      const focus = element('div', undefined, 'lane-attention');
      focus.setAttribute('role', 'progressbar');
      focus.setAttribute('aria-label', `Recorded research attention for ${lane.name}`);
      focus.setAttribute('aria-valuemin', '0');
      focus.setAttribute('aria-valuemax', '100');
      focus.setAttribute('aria-valuenow', String(Math.round(Math.max(0, Math.min(1, share)) * 100)));
      const fill = element('span');
      fill.style.width = `${Math.max(0, Math.min(100, share * 100))}%`;
      focus.append(fill);
      card.append(focus);
    }
    $('lane-rows').append(card);
  }
  $('other-lanes-label').textContent = `${inactive.length} desks with no recorded activity`;
  $('inactive-lanes').replaceChildren();
  for (const lane of inactive) $('inactive-lanes').append(element('span', lane.name, 'inactive-lane'));
}

function renderWeb3Evidence(data) {
  const host = $('web3-evidence-grid');
  const state = $('web3-runtime-state');
  const note = $('web3-evidence-note');
  host.replaceChildren();
  const progress = data?.progress;
  if (!data?.database_present || !progress) {
    state.textContent = 'EVIDENCE STATE UNAVAILABLE';
    host.append(element('p', 'No persisted Web3 evidence state is available yet.', 'quiet'));
    return;
  }
  const runtime = progress.runtime ?? {};
  state.textContent = `${String(progress.collector_status ?? runtime.status ?? 'unknown').toUpperCase()} · ${runtime.detail ?? 'Runtime detail unavailable'}`;
  state.dataset.state = runtime.status ?? 'unknown';
  const threshold = progress.threshold ?? { training: 50, walk_forward: 20 };
  const remaining = progress.threshold_remaining ?? {};
  const health = progress.provider_health ?? {};
  const invalid = value(progress.invalid_observations);
  const metrics = [
    ['Last successful collection', progress.last_successful_collection ? dateLabel(progress.last_successful_collection) : 'Unknown'],
    ['Valid observations', value(progress.valid_observations ?? progress.observations_collected)],
    ['Launch-distinct observations', progress.launch_distinct_observations],
    ['Pending one-hour outcomes', value(progress.labels_pending)],
    ['Labels matured', progress.labels_matured],
    ['First-label audit checkpoint', maturityCheckpoint(progress.matured_label_checkpoint)],
    ['Training labels', `${progress.training_labels} / ${threshold.training}`],
    ['Walk-forward labels', `${value(progress.walk_forward_labels_available ?? progress.walk_forward_labels)} / ${threshold.walk_forward}`],
    ['Walk-forward tests scored', value(progress.walk_forward_scored)],
    ['Threshold remaining', `${remaining.training ?? '?'} training · ${remaining.walk_forward ?? '?'} walk-forward`],
    ['Invalid / failed / missed', `${invalid} invalid rows · ${value(progress.failed_attempts)} failures · ${value(progress.missed_attempts)} missed horizons`],
    ['Latest collector failure', progress.latest_failure ?? 'None recorded'],
    ['Next expected maturation', progress.next_maturation_at ? dateLabel(progress.next_maturation_at) : (progress.next_maturation_status === 'no_pending_candidates' ? 'Not yet derivable · no pending candidate' : String(progress.next_maturation_status ?? 'unknown').replaceAll('_', ' '))],
    ['Jupiter provider', health.jupiter ?? 'unknown'],
    ['Solana RPC', health.solana_rpc ?? 'unknown'],
    ['Last healthy RPC enrichment', progress.solana_rpc_last_enrichment_at ? dateLabel(progress.solana_rpc_last_enrichment_at) : 'None recorded'],
    ['RPC failover', progress.solana_rpc_fallback_configured ? 'Configured · provider health unverified' : 'Not configured'],
    ['Latest RPC attempt', progress.solana_rpc_latest_attempt ?? 'None recorded'],
    ['Latest field sources', provenanceLabel(progress.latest_field_provenance)],
    ['Latest Solana RPC issue', progress.latest_rpc_failure ?? 'None recorded'],
    ['Web3 mission eligibility', progress.mission_eligible === true ? 'ELIGIBLE' : String(progress.next_eligible_mission_state ?? 'not eligible').replaceAll('_', ' ')],
  ];
  for (const [label, value] of metrics) {
    const metric = element('div', undefined, 'web3-evidence-metric');
    metric.append(element('span', label), element('strong', value === null || value === undefined ? 'Unknown' : value));
    host.append(metric);
  }
  const failures = progress.attempt_statuses ?? {};
  const failed = Number(failures.error ?? 0);
  const missed = Number(failures.missed ?? 0);
  const unavailable = Number(failures.unavailable ?? 0);
  const scored = progress.walk_forward_scored == null ? 'not run' : `${progress.walk_forward_scored}`;
  const nextEvidence = progress.next_maturation_status === 'earliest_scheduled_outcome'
    ? 'Next time is the earliest scheduled one-hour outcome; collection may complete afterward.'
    : 'Next maturation time appears only when a valid pending candidate has a future one-hour target.';
  note.textContent = `Read-only collection · no wallet authority · ${failed} failed normalization/provider attempt${failed === 1 ? '' : 's'} · ${missed} missed horizon${missed === 1 ? '' : 's'} · ${unavailable} unavailable provider result${unavailable === 1 ? '' : 's'} · ${progress.eligibility_reason ?? 'eligibility basis unavailable'} · survival audit ${progress.audit_status ?? 'unavailable'} (${scored} walk-forward tests scored). ${nextEvidence}`;
}

function provenanceLabel(provenance) {
  if (!provenance || provenance.status === 'legacy_source_unattributed') return 'Legacy record · source attribution unavailable';
  if (provenance.status === 'invalid_provenance_record') return 'Invalid provenance record';
  const fields = provenance.fields ?? {};
  const rpc = provenance.solana_rpc ?? {};
  const holder = rpc.status === 'recorded'
    ? `Solana RPC ${rpc.source_alias ?? 'unknown'}`
    : `holder data ${rpc.status ?? 'unknown'}`;
  return `${fields.price_usd ?? 'price source unknown'} · ${fields.liquidity_usd ?? 'liquidity source unknown'} · ${holder}`;
}

function maturityCheckpoint(rows) {
  const first = Array.isArray(rows) ? rows[0] : null;
  if (!first) return 'No verified one-hour labels yet';
  return `#${first.chronological_position} ${first.cohort_assignment} · ${first.label ? 'survived' : 'not survived'} · ${dateLabel(first.actual_maturity_at)}`;
}

function renderPredictionVenues(payload) {
  const root = $('prediction-venue-list');
  if (!root) return;
  const venues = payload?.venues ?? [];
  $('prediction-venues-asof').textContent = payload?.as_of
    ? `${capabilityFreshness.venues === 'stale' ? 'STALE · ' : ''}READ ONLY · ${dateLabel(payload.as_of)}`
    : 'VENUE STATE UNAVAILABLE';
  root.replaceChildren();
  if (!venues.length) {
    root.append(element('p', 'No venue status is available.', 'quiet'));
    return;
  }
  for (const venue of venues) {
    const market = venue.market_data ?? {};
    const account = venue.account ?? {};
    const sample = market.sample_market;
    const row = element('article', undefined, 'prediction-venue-row');
    const head = element('div', undefined, 'prediction-venue-head');
    head.append(element('strong', venue.venue), element('span', String(market.status ?? 'unknown').toUpperCase(), 'venue-health'));
    const state = account.status === 'authenticated_read_only' ? 'AUTHENTICATED · READ ONLY'
      : account.status === 'missing_key_id_and_secret_key' ? 'PUBLIC DATA · API KEY MISSING'
        : String(account.status ?? 'ACCOUNT UNKNOWN').toUpperCase().replaceAll('_', ' ');
    const detail = element('p', `${state} · ${market.open_markets_sampled ?? '—'} open markets sampled · ${venue.latency_ms ?? '—'} ms`);
    const caps = venue.capabilities ?? {};
    const capLabel = (label, value) => `${label} ${value === true ? 'YES' : value === false ? 'NO' : 'UNKNOWN'}`;
    detail.append(element('small', [
      capLabel('CONNECTED', caps.connected), capLabel('AUTHENTICATED', caps.authenticated),
      capLabel('READABLE', caps.readable), capLabel('FUNDED', caps.funded),
      capLabel('SIGNER CONFIGURED', caps.signer_configured),
      capLabel('CREDENTIALS ISOLATED', caps.credentials_isolated),
      capLabel('RESEARCH ENABLED', caps.research_enabled),
      capLabel('PAPER ENABLED', caps.paper_enabled),
      capLabel('MISSION AUTHORITY', caps.mission_authority_present),
      capLabel('LIVE EXECUTION', caps.live_execution_enabled), capLabel('HALTED', caps.halted),
      capLabel('COORDINATOR WIRED', caps.coordinator_wired),
    ].join(' · ')));
    const stats = element('div', undefined, 'prediction-venue-stats');
    stats.append(
      element('span', `Cash ${account.cash_balance_usd == null ? 'unknown' : money(account.cash_balance_usd)}`),
      element('span', `Buying power ${account.buying_power_usd == null ? 'unknown' : money(account.buying_power_usd)}`),
      element('span', `Positions ${account.positions ?? 'unknown'}`),
      element('span', `Open orders ${account.open_orders ?? 'unknown'}`),
      element('span', `Fills ${account.fills ?? 'unknown'}`),
      element('span', `Books ${market.order_book_readable ? `${market.order_book_levels ?? 0} levels` : 'unavailable'}`),
    );
    const quote = sample
      ? `${sample.title} · YES ${sample.yes_bid == null ? 'unknown' : money(sample.yes_bid)} / ${sample.yes_ask == null ? 'unknown' : money(sample.yes_ask)} · spread ${sample.spread == null ? 'unknown' : `${(sample.spread * 100).toFixed(2)}¢`} · depth ${sample.liquidity_usd == null ? 'unknown' : money(sample.liquidity_usd)}`
      : 'No current market quote available.';
    row.append(head, detail, stats, element('p', quote, 'prediction-venue-quote'));
    root.append(row);
  }
  const match = payload?.cross_venue_comparison;
  const maturation = payload?.cross_venue_paper_maturation ?? {};
  const maturationCard = element('article', undefined, 'prediction-overlap-candidate');
  maturationCard.append(
    element('strong', `PAPER MATURATION · ${maturation.status ?? 'state unknown'}`),
    element('span', `Paper fills ${maturation.paper_fill_count ?? 0} · matured ${maturation.matured_total ?? 0} · rule divergences ${maturation.rule_divergences_total ?? maturation.rule_divergences ?? 0} · pending outcomes ${maturation.pending ?? 0} · live trades ${maturation.live_trade_count ?? 0} · capacity ${maturation.capacity === 'unknown' ? 'UNKNOWN' : JSON.stringify(maturation.capacity ?? 'unknown')}`),
    element('span', `Walk-forward ${maturation.walk_forward?.status ?? 'waiting for independent matured events'} · diagnostics invoked ${maturation.walk_forward?.diagnostics_invoked === true ? 'YES' : 'NO'}`),
  );
  root.append(maturationCard);
  const lifecycleHistory = payload?.cross_venue_experiment_history ?? [];
  const renderLifecycle = candidate => {
    const evaluation = candidate.experiment_evaluation ?? {};
    const equivalence = evaluation.contract_equivalence ?? {};
    const freshness = evaluation.freshness ?? {};
    const depth = evaluation.depth ?? {};
    const quotes = evaluation.quotes ?? {};
    const simulation = evaluation.paper_simulation ?? {};
    const rejection = evaluation.rejection_reasons ?? [];
    return [
      element('strong', `${evaluation.verdict ?? 'LIFECYCLE'} · ${candidate.event ?? 'Event'} · ${candidate.team_code ?? 'OUTCOME'}`),
      element('span', `Rules ${equivalence.status ?? 'unverified'} · unresolved ${equivalence.unresolved_clauses?.join(', ') || 'unknown'}`),
      element('span', `Freshness ${freshness.status ?? 'unknown'} · source timestamps ${JSON.stringify(freshness.source_timestamps ?? {})}`),
      element('span', `Fees Kalshi ${quotes.kalshi?.fee_status ?? 'unknown'} · Polymarket US ${quotes.polymarket_us?.fee_status ?? 'unknown'}`),
      element('span', `Depth ${depth.status ?? 'unknown'} · capacity ${depth.capacity === 'unknown' ? 'UNKNOWN' : JSON.stringify(depth.capacity ?? 'unknown')} · simulation ${simulation.status ?? 'not run'}`),
      element('small', rejection.length ? `Why NOEMA said no: ${rejection.join(' · ')}` : (evaluation.contract_equivalence?.reason ?? 'Evidence incomplete.')),
    ];
  };
  if (match?.matches?.length) {
    for (const candidate of match.matches) {
      const compare = element('article', undefined, 'prediction-overlap-candidate');
      compare.append(
        element('strong', `${candidate.event ?? 'Event'} · ${candidate.team_code ?? 'OUTCOME'} · canonical proposition matched`),
        element('span', `Identity ${candidate.canonical_identity?.comparison?.semantic_match ?? 'unverified'} · settlement ${candidate.canonical_identity?.comparison?.settlement_equivalence ?? 'unverified'} · executable comparison unavailable`),
        element('span', `Identity ladder ${Object.entries(candidate.canonical_identity?.comparison?.levels ?? {}).map(([level, state]) => `${level.replaceAll('_', ' ')}: ${state}`).join(' · ') || 'unavailable'}`),
        element('span', `Pair evidence ${match.persistence_status ?? 'not recorded'}`),
        element('span', `Kalshi YES ask ${money(candidate.kalshi?.yes_ask)} · spread ${candidate.kalshi?.spread == null ? 'unknown' : `${(candidate.kalshi.spread * 100).toFixed(2)}¢`}`),
        element('span', `Polymarket US YES ask ${money(candidate.polymarket_us?.yes_ask)} · spread ${candidate.polymarket_us?.spread == null ? 'unknown' : `${(candidate.polymarket_us.spread * 100).toFixed(2)}¢`}`),
        element('span', `Unadjusted ask difference ${candidate.unadjusted_yes_ask_difference == null ? 'unknown' : `${(candidate.unadjusted_yes_ask_difference * 100).toFixed(2)}¢/contract`} · executable edge ${candidate.executable_edge == null ? 'not established' : `${(candidate.executable_edge * 100).toFixed(2)}¢/contract`}`),
        element('small', candidate.settlement_difference ?? 'Venue settlement rules are not reconciled.'),
        element('small', candidate.reason ?? 'Outcome equivalence has not been proven.'),
      );
      if (candidate.experiment_evaluation) compare.append(...renderLifecycle(candidate));
      root.append(compare);
    }
  } else {
    root.append(element('p', match?.reason ?? 'No validated cross-venue comparison is available.', 'prediction-match-state'));
  }
  for (const candidate of lifecycleHistory) {
    const card = element('article', undefined, 'prediction-overlap-candidate');
    card.append(...renderLifecycle(candidate));
    root.append(card);
  }
  const identity = payload?.canonical_market_identity ?? {};
  const coverage = identity.coverage ?? {};
  const coveragePanel = element('article', undefined, 'prediction-overlap-candidate');
  coveragePanel.append(
    element('strong', 'CANONICAL RESOLVER COVERAGE'),
    element('span', `Slow path: ${coverage.slow_path ?? 'unavailable'} · Fast path: ${coverage.fast_path ?? 'unavailable'}`),
    element('span', `Verified live overlap observations: ${coverage.verified_live_overlap_count ?? 0} · Identity review remains separate from quote refresh.`),
    element('span', `Discovery scope: ${coverage.discovery_scope ?? 'not reported'}`),
  );
  const registered = (identity.registered_resolvers ?? []).map(item => `${item.topic_id}/${item.proposition_family}`).join(' · ');
  coveragePanel.append(element('span', `Registered production resolvers: ${registered || 'none'}`));
  for (const family of identity.proposition_families ?? []) {
    coveragePanel.append(element('span', `${family.family} · ${family.resolver_status} · venue overlap ${String(family.production_adapter_status ?? 'unknown').replaceAll('_', ' ')}`));
    if (family.coverage_reason) coveragePanel.append(element('small', family.coverage_reason));
  }
  const latestMatch = match?.matches?.[0];
  if (latestMatch) {
    const observed = Date.parse(latestMatch.observed_at ?? '');
    const age = Number.isFinite(observed) ? `${Math.max(0, Math.round((Date.now() - observed) / 1000))}s old` : 'age unknown';
    coveragePanel.append(
      element('span', `Latest semantic match: ${latestMatch.canonical_identity?.comparison?.semantic_match ?? 'unverified'} · settlement ${latestMatch.settlement_assessment?.status ?? 'unverified'} · economics ${latestMatch.economic_comparability?.status ?? 'unavailable'} · evidence ${age}`),
      element('small', coverage.unresolved_reason ?? 'No unresolved candidate reason is available.'),
    );
  } else if (coverage.unresolved_reason) {
    const reasons = (coverage.rejection_reasons ?? []).join(' · ') || coverage.unresolved_reason;
    coveragePanel.append(element('span', `Unresolved candidates: ${(coverage.unresolved_candidates ?? []).length} · ${reasons}`));
  }
  root.append(coveragePanel);
}

function renderAllocation() {
  const allocation = snapshot?.attention_allocations ?? {};
  const host = $('allocation-chart');
  host.replaceChildren();
  const review = allocation.mission_review;
  const appendMissionReview = () => {
    if (!review) return;
    const note = element('div', undefined, 'allocation-review');
    note.dataset.outcome = review.outcome ?? 'UNKNOWN';
    note.append(element('strong', `${review.outcome ?? 'REVIEW'} · ${review.specialist ?? 'specialist unknown'}`));
    note.append(element('span', review.basis ?? 'Allocation rationale unavailable.'));
    host.append(note);
  };
  if (!allocation.rows?.length) {
    host.append(element('p', 'No specialist attention shares are recorded.', 'quiet'));
    appendMissionReview();
    return;
  }
  host.append(element('p', 'Latest research-attention review · capacity, not financial capital', 'quiet'));
  for (const item of allocation.rows) {
    const row = element('div', undefined, 'allocation-row');
    row.dataset.state = item.state;
    const pct = Number.isFinite(Number(item.attention_fraction)) ? Math.max(0, Math.min(100, Number(item.attention_fraction) * 100)) : 0;
    row.append(element('span', item.specialist));
    const track = element('div', undefined, 'allocation-track');
    const fill = element('span'); fill.style.width = `${pct}%`; track.append(fill);
    const delta = Number.isFinite(Number(item.attention_delta)) ? ` · ${Number(item.attention_delta) > 0 ? '+' : ''}${(Number(item.attention_delta) * 100).toFixed(1)}pp` : '';
    row.append(track, element('strong', `${pct.toFixed(0)}%${delta}`));
    host.append(row);
  }
  if (allocation.idle_fraction != null) host.append(element('p', `${(Number(allocation.idle_fraction) * 100).toFixed(0)}% capacity held idle · no forced redistribution`, 'quiet'));
  appendMissionReview();
}

function renderEconomy(measurement) {
  const recordedEntries = Number(measurement?.cash?.entry_count ?? 0);
  $('metric-cash').textContent = recordedEntries > 0 ? money(measurement.cash.net_cash_usd) : 'Unknown';
  const ledger = measurement?.canonical_ledger ?? {};
  $('metric-revenue').textContent = money(ledger.verified_realized_revenue_usd);
  $('metric-net').textContent = money(measurement?.net_economic_profit_usd);
  const missionProgress = measurement?.mission_progress ?? {};
  $('mission-economics-progress').textContent = `SELF-FUNDING ${String(missionProgress.status ?? 'unknown').toUpperCase()} · verified realized revenue ${money(missionProgress.verified_realized_revenue_usd)} · complete attributable costs ${money(missionProgress.complete_attributable_costs_usd)} · reserves ${money(missionProgress.reserve_balance_usd)} · cost-adjusted value ${money(missionProgress.verified_cost_adjusted_value_usd)}`;
  const attempts = Number(measurement?.operating_estimates?.model_attempt_count ?? 0);
  $('metric-model-cost').textContent = attempts > 0 ? money(measurement.operating_estimates.model_reserved_usd) : 'Unknown';
  const settled = Number(measurement?.paper?.settled_markets_this_month ?? 0);
  $('metric-paper').textContent = settled > 0 ? money(measurement.paper.net_after_execution_costs_usd) : 'Unknown';
  renderPaperPnl(measurement?.paper ?? {}, measurement?.month_utc);
  $('economics-time').textContent = measurement?.as_of ? `Accounting snapshot · ${dateLabel(measurement.as_of)}` : 'Economic measurement unavailable';
  renderCanonicalEconomy(ledger);

  const rows = snapshot?.economic_state?.balances;
  $('treasury-reserve').textContent = rows ? money(rows.reserve_usd) : 'Unknown';
  $('treasury-strategy').textContent = rows ? money(rows.strategy_capital_usd) : 'Unknown';
  $('treasury-research').textContent = rows ? money(rows.research_budget_usd) : 'Unknown';
  $('treasury-infrastructure').textContent = rows ? money(rows.infrastructure_budget_usd) : 'Unknown';

}

function renderPaperPnl(paper, period) {
  const host = $('account-chart');
  const settledCount = Number.isFinite(Number(paper.settled_markets_this_month))
    ? Number(paper.settled_markets_this_month) : 0;
  const points = Array.isArray(paper.pnl_series) ? paper.pnl_series.filter((item) =>
    Number.isFinite(Number(item.cumulative_net_usd)) && Number.isFinite(Number(item.realized_net_usd))
    && Number.isFinite(Date.parse(item.observed_at))) : [];
  const chartPoints = points.length > 180
    ? Array.from({ length: 180 }, (_, index) => points[Math.round(index * (points.length - 1) / 179)])
    : points;
  host.replaceChildren();
  $('wallet-state').textContent = settledCount ? `${formatCount(settledCount)} SETTLED` : 'NO SETTLEMENTS';
  $('account-note').textContent = `Validated paper outcomes · ${paper.model_version ?? 'model unknown'} · ${period ?? 'period unknown'} UTC. Operating costs and capital opportunity cost are excluded.`;
  host.setAttribute('aria-label', points.length
    ? `Cumulative hypothetical paper P and L from ${dateLabel(points[0].observed_at)} through ${dateLabel(points.at(-1).observed_at)}, based on ${formatCount(settledCount)} validated settled positions; showing ${points.length} chart samples`
    : 'No validated settled paper positions are available for a P and L chart');
  if (!points.length) {
    const empty = element('div', undefined, 'account-empty');
    empty.append(element('span', '∅', 'empty-emblem'));
    empty.append(element('strong', 'No settled paper series'));
    empty.append(element('span', 'The chart appears when validated paper positions resolve. This is hypothetical P&L, not revenue.'));
    host.append(empty);
    return;
  }

  const width = 640, height = 210, left = 58, right = 14, top = 22, bottom = 34;
  const values = chartPoints.map((item) => Number(item.cumulative_net_usd));
  let lo = Math.min(0, ...values), hi = Math.max(0, ...values);
  if (lo === hi) { lo -= 0.01; hi += 0.01; }
  const span = Math.max(0.01, hi - lo);
  const xAt = (index) => chartPoints.length === 1 ? (left + width - right) / 2
    : left + (width - left - right) * index / (chartPoints.length - 1);
  const yAt = (value) => top + (hi - value) / span * (height - top - bottom);
  const svg = svgNode('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img',
    'aria-labelledby': 'paper-pnl-title paper-pnl-desc', preserveAspectRatio: 'none' });
  svg.append(svgNode('title', { id: 'paper-pnl-title' }, 'Cumulative settled paper P and L'));
  svg.append(svgNode('desc', { id: 'paper-pnl-desc' }, 'Hypothetical cumulative net from validated paper positions as they were observed to settle. Does not represent live trading or include operating costs.'));
  for (let tick = 0; tick <= 4; tick += 1) {
    const value = lo + span * tick / 4, y = yAt(value);
    if (Math.abs(value) > span * .00001) svg.append(svgNode('path', { d: `M${left} ${y}H${width - right}`, class: 'account-gridline' }));
    svg.append(svgNode('text', { x: left - 8, y: y + 3, 'text-anchor': 'end', class: 'pnl-axis-label' }, money(value.toFixed(2))));
  }
  svg.append(svgNode('path', { d: `M${left} ${yAt(0)}H${width - right}`, class: 'pnl-zero' }));
  svg.append(svgNode('text', { x: left, y: 12, class: 'pnl-axis-caption' }, 'CUMULATIVE PAPER NET · USD'));
  const plotted = chartPoints.map((item, index) => ({
    x: xAt(index), y: yAt(Number(item.cumulative_net_usd)), value: Number(item.cumulative_net_usd),
  }));
  let segment = { sign: plotted[0].value >= 0 ? 'positive' : 'negative', points: [plotted[0]] };
  const drawSegment = () => {
    const d = segment.points.map((point, index) => `${index ? 'L' : 'M'}${point.x} ${point.y}`).join(' ');
    svg.append(svgNode('path', { d, class: 'paper-pnl-line', 'data-sign': segment.sign }));
  };
  for (let index = 1; index < plotted.length; index += 1) {
    const previous = plotted[index - 1], current = plotted[index];
    const sign = current.value >= 0 ? 'positive' : 'negative';
    if (sign !== segment.sign) {
      const ratio = previous.value / (previous.value - current.value);
      const crossing = { x: previous.x + (current.x - previous.x) * ratio, y: yAt(0) };
      segment.points.push(crossing);
      drawSegment();
      segment = { sign, points: [crossing, current] };
    } else segment.points.push(current);
  }
  drawSegment();
  chartPoints.forEach((item, index) => {
    const value = Number(item.cumulative_net_usd), { x, y } = plotted[index];
    const dot = svgNode('circle', { cx: x, cy: y, r: chartPoints.length > 30 ? 2 : 3, class: 'paper-pnl-point', 'data-sign': value >= 0 ? 'positive' : 'negative' });
    dot.append(svgNode('title', {}, `${dateLabel(item.observed_at)} · ${item.venue} · settled net ${money(item.realized_net_usd)} · cumulative paper net ${money(item.cumulative_net_usd)} · ${item.market_id} · ${paper.model_version ?? 'model unknown'}`));
    svg.append(dot);
  });
  const first = element('span', dateLabel(points[0].observed_at, { month: 'short', day: 'numeric' }));
  const last = element('strong', dateLabel(points.at(-1).observed_at, { month: 'short', day: 'numeric' }));
  const range = element('div', undefined, 'pnl-date-range');
  range.append(first, last);
  host.append(svg, range);
}

function renderCanonicalEconomy(ledger) {
  const grid = $('canonical-economy-grid');
  grid.replaceChildren();
  const hasEvents = Number(ledger.event_count ?? 0) > 0;
  const metrics = [
    ['Verified revenue total', ledger.verified_realized_revenue_usd],
    ['Verified trading P&L total', ledger.verified_realized_trading_pnl_usd],
    ['Verified attributable costs total', ledger.verified_attributable_costs_usd],
    ['Known unreconciled USD', hasEvents ? ledger.unreconciled_amount_usd : null],
    ['Owner capital total', ledger.owner_funded_amount_usd],
    ['Reconciled owner capital subtotal', ledger.owner_funded_subtotal_usd],
    ['Operating reserve', ledger.operating_reserve_usd],
    ['Net verified contribution', ledger.net_verified_contribution_usd],
    ['Self-funding ratio', ledger.self_funding_ratio == null ? null : `${(Number(ledger.self_funding_ratio) * 100).toFixed(1)}%`],
    ['Model budget reserved', hasEvents ? ledger.reserved_amount_usd : null],
  ];
  for (const [label, value] of metrics) {
    const item = element('div', undefined, 'canonical-economy-metric');
    item.append(element('span', label), element('strong', typeof value === 'string' && label === 'Self-funding ratio' ? value : money(value)));
    grid.append(item);
  }

  const closure = $('economic-closure');
  closure.replaceChildren();
  const period = ledger.provider_coverage?.[0]?.month_utc ?? 'No period manifest';
  closure.append(element('p', `${period} · ${ledger.period_closure ?? 'OPEN'} · ${ledger.coverage_status ?? 'UNKNOWN'}`));
  const reserveBlockers = Array.isArray(ledger.reserve_blockers) ? ledger.reserve_blockers.join('; ') : 'Reserve evidence unavailable.';
  closure.append(element('p', `Operating reserve · ${ledger.reserve_status ?? 'UNKNOWN'} · ${money(ledger.operating_reserve_usd)} · ${reserveBlockers}`));
  if (Array.isArray(ledger.provider_coverage) && ledger.provider_coverage.length) {
    const list = element('ul', undefined, 'canonical-economy-list');
    for (const provider of ledger.provider_coverage) {
      const missing = provider.state === 'NOT_APPLICABLE' ? [] : (provider.expected_evidence_classes ?? []).filter(
        name => !(provider.actual_evidence_classes ?? []).includes(name),
      );
      const blockers = [...(provider.unresolved_blockers ?? []), ...missing.map(name => `missing ${name}`)];
      const details = blockers.length ? ` · ${blockers.join('; ')}` : '';
      const activity = provider.applicable_activity_detected == null
        ? 'UNKNOWN' : provider.applicable_activity_detected ? 'YES' : 'NO';
      const source = provider.provenance?.source ?? provider.provenance?.endpoint ?? 'provenance unavailable';
      const safeEvidenceKeys = {
        cloudflare: ['cognitive_sessions', 'runtime_events', 'cognition_packets', 'idle_cognition_attempts', 'billing_api'],
        kalshi: ['fills_in_period', 'settlements_in_period', 'reported_fees_usd', 'fill_scan_complete', 'settlement_scan_complete'],
        openai: ['actual_request_count', 'model', 'input_tokens', 'output_tokens', 'configured_price_estimate_usd', 'reservation_count', 'reserved_amount_usd', 'reserved_tokens', 'admin_api_key_present'],
        operator_expenses: ['september_operator_expense_entries', 'owner_no_expenses_attestation_present'],
        polymarket_us: ['activity_records_scanned', 'pages', 'eof_reached', 'activity_types', 'september_trades', 'september_position_resolutions', 'september_account_balance_changes', 'trade_fee_field_present'],
        render: ['provider_invoice_or_statement_present', 'blueprint_monthly_hosting_estimate_usd', 'workspace_subscription_cost', 'no_estimate_promoted_to_expense'],
        stripe: ['payment_intent_count', 'period_successful_usd_payment_intents', 'missing_read_capabilities', 'snapshot_status'],
        wallets: ['current_read_only_balance_status', 'confirmed_september_transactions', 'chains_with_period_history_reconciliation', 'native_fee_dispute'],
      }[provider.provider] ?? [];
      const evidence = Object.fromEntries(safeEvidenceKeys
        .filter(key => Object.hasOwn(provider.latest_evidence ?? {}, key))
        .map(key => [key, provider.latest_evidence[key]]));
      const evidenceText = Object.keys(evidence).length ? ` · evidence ${JSON.stringify(evidence)}` : '';
      list.append(element('li', `${provider.provider} · ${provider.state} · activity ${activity} · events ${value(provider.canonical_event_count)} · reconciled ${moneyPrecise(provider.reconciled_amount_usd)} · estimated ${moneyPrecise(provider.estimated_amount_usd)} · last checked ${value(provider.observed_at)} · ${source}${evidenceText}${details}`));
    }
    closure.append(list);
  } else {
    closure.append(element('p', 'No expected-provider manifest or period attestations exist; silence is not completeness.', 'quiet'));
  }

  renderEconomicInvestigation(ledger.investigation_decision);

  const renderRows = (id, rows, formatter, empty) => {
    const host = $(id);
    host.replaceChildren();
    if (!Array.isArray(rows) || rows.length === 0) {
      host.append(element('p', empty, 'quiet'));
      return;
    }
    const list = element('ul', undefined, 'canonical-economy-list');
    for (const row of rows) list.append(element('li', formatter(row)));
    host.append(list);
  };
  renderRows('economic-lane-contribution', ledger.contribution_by_lane,
    row => `${row.key} · reconciled event subtotal ${money(row.reconciled_event_contribution_subtotal_usd)} · ${row.unreconciled_event_count} unresolved / ${row.event_count} events`,
    'No reconciled contribution events; lane coverage is incomplete.');
  renderRows('economic-cost-drivers', ledger.top_cost_drivers,
    row => `${row.provider} / ${row.lane} · known ${money(row.known_amount_usd)} · ${row.all_events_reconciled ? 'events reconciled' : 'not fully reconciled'}`,
    'Cost sources unknown.');
  renderRows('economic-model-benefit', ledger.model_cost_vs_measured_benefit,
    row => `${row.activity_id} · cost ${money(row.cost_usd)} · benefit ${money(row.measured_benefit_usd)}`,
    'No attributed model cost/benefit pair is measured.');
  renderRows('economic-counterfactuals', ledger.counterfactual_comparisons,
    row => `${row.mission_id} · ${row.scenario_type} · ${row.outcome_basis} · ${money(row.amount_usd)}`,
    'No persisted counterfactual comparisons.');
  renderRows('economic-unknowns', ledger.unknowns,
    value => value,
    'Provider-period coverage is not attested.');
}

function renderEconomicInvestigation(decision) {
  const host = $('economic-investigation');
  host.replaceChildren();
  if (!decision) {
    host.append(element('p', 'No autonomous decision is persisted; blocker state remains unknown.', 'quiet'));
    return;
  }
  const selected = decision.alternatives_considered?.find(
    item => item.candidate_id === decision.selected_candidate,
  );
  const pct = value => Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(0)}%` : 'Unknown';
  const evidenceAge = selected?.evidence_age_seconds == null
    ? 'Unknown' : `${Math.floor(Number(selected.evidence_age_seconds) / 3600)}h since provider snapshot`;
  const fields = [
    ['CURRENT PRIORITY', `${decision.action ?? 'PASS'} · ${decision.selected_candidate ?? 'none'}`],
    ['OBJECTIVE', decision.objective ?? 'No objective persisted.'],
    ['WHY', decision.why ?? 'No rationale persisted.'],
    ['EXPECTED INFORMATION VALUE', decision.expected_information_value == null
      ? 'Unknown' : `${(Number(decision.expected_information_value) * 100).toFixed(0)}% · prioritization estimate`],
    ['POTENTIAL MATERIALITY', selected ? `${pct(selected.estimated_potential_materiality)} relative · ${selected.materiality_basis ?? 'basis unavailable'}` : 'Unknown'],
    ['RESOLUTION PROBABILITY', selected ? `${pct(selected.resolution_probability)} estimated` : 'Unknown'],
    ['EXPECTED COST', decision.expected_cost_usd == null
      ? 'Unknown' : `${moneyPrecise(decision.expected_cost_usd)} API/model/compute estimate`],
    ['EXPECTED RISK / DOWNSIDE', decision.expected_risk?.downside ?? selected?.risk_downside ?? 'Unknown'],
    ['UNCERTAINTY', pct(decision.uncertainty ?? selected?.uncertainty)],
    ['EVIDENCE QUALITY', decision.evidence_quality ?? selected?.evidence_quality ?? 'Unknown'],
    ['AUTHORITY REQUIRED', (decision.authority_required ?? selected?.authority_required ?? []).join(' · ') || 'None recorded'],
    ['PRIORITY CHANGE', decision.priority_change_reason || 'No priority-change reason persisted.'],
    ['ENGINEERING EFFORT', selected ? `${pct(selected.engineering_effort)} relative estimate` : 'Unknown'],
    ['EVIDENCE AGE', evidenceAge],
    ['BLOCKER', decision.blocker ?? 'No unresolved blocker selected.'],
    ['NEXT ELIGIBLE RETRY', decision.next_eligible_retry ?? 'No retry scheduled'],
    ['OWNER INPUT REQUIRED', decision.owner_input_required ?? 'No'],
    ['LAST DECISION RESULT', decision.last_decision_result ?? 'Unknown'],
    ['LIFECYCLE', decision.active_lifecycle_state ?? 'Unknown'],
    ['AUTHORITY STATE', decision.authority_state ?? 'Decision is separate from execution authority'],
    ['DECISION-TIME EVIDENCE', `${decision.decision_state_at_time?.provider_coverage?.length ?? 0} provider snapshots · ${(decision.economic_event_ids ?? []).length} selected-provider event references`],
    ['CHANGE MY MIND IF', (decision.change_my_mind_if ?? []).join(' · ') || 'No condition persisted'],
  ];
  const grid = element('div', undefined, 'economic-investigation-fields');
  for (const [label, value] of fields) {
    const item = element('div', undefined, 'economic-investigation-field');
    item.append(element('span', label), element('p', String(value)));
    grid.append(item);
  }
  host.append(grid);
  const alternatives = Array.isArray(decision.alternatives_considered)
    ? decision.alternatives_considered : [];
  const list = element('ul', undefined, 'canonical-economy-list');
  host.append(element('p', 'Autonomous ranking uses relative planning estimates for materiality, resolution likelihood, information value, urgency, dependency, cost and effort. They are not reconciled accounting values.', 'quiet'));
  for (const item of alternatives) {
    const selectedMark = item.candidate_id === decision.selected_candidate ? ' · SELECTED' : '';
    const info = Number.isFinite(Number(item.expected_information_value))
      ? `${(Number(item.expected_information_value) * 100).toFixed(0)}% info` : 'info unknown';
    const cost = item.expected_api_model_compute_cost_usd == null
      ? 'cost unknown' : `${moneyPrecise(item.expected_api_model_compute_cost_usd)} estimated cost`;
    const metrics = `materiality ${pct(item.estimated_potential_materiality)} relative · resolve ${pct(item.resolution_probability)} · ${info} · urgency ${pct(item.urgency)} · dependency ${pct(item.dependency_value)} · effort ${pct(item.engineering_effort)} relative · autonomous ${item.autonomous_action_permitted ? 'yes' : 'no'} · ${cost}`;
    const selectionReason = item.selection_reason ?? (item.candidate_id === decision.selected_candidate
      ? decision.selected_reason : 'Rejected; no reason persisted');
    list.append(element('li', `${item.candidate_id} · ${item.action} · priority ${Number(item.priority_score).toFixed(3)} · ${metrics}${selectedMark} — ${selectionReason} ${item.candidate_id === decision.selected_candidate ? `Selected rationale: ${item.rationale}` : ''}`));
  }
  const alternativesSection = element('section', undefined, 'economic-investigation-alternatives');
  alternativesSection.append(element('strong', 'ALTERNATIVES CONSIDERED'), list);
  if (selected?.materiality_basis) {
    alternativesSection.append(element('p', `Materiality basis: ${selected.materiality_basis}`, 'quiet'));
  }
  if (selected?.autonomous_action_permitted === false) {
    alternativesSection.append(element('p', 'The selected action requires owner input; no autonomous provider call was made.', 'quiet'));
  }
  host.append(alternativesSection);

  const history = Array.isArray(decision.result_history) ? decision.result_history : [];
  const timeline = element('section', undefined, 'economic-investigation-alternatives');
  timeline.append(element('strong', 'APPENDED LIFECYCLE / MATURATION RECORDS'));
  if (!history.length) {
    timeline.append(element('p', 'No result or maturation record is persisted yet.', 'quiet'));
  } else {
    const entries = element('ol', undefined, 'canonical-economy-list');
    for (const item of [...history].reverse()) {
      const details = [
        item.detail,
        `evidence ${JSON.stringify(item.evidence ?? {})}`,
        `resources ${JSON.stringify(item.actual_resources ?? {})}`,
        `outcome ${JSON.stringify(item.actual_outcome ?? {})}`,
        `measured benefit ${JSON.stringify(item.measured_benefit ?? {})}`,
        `counterfactual ${JSON.stringify(item.counterfactual ?? {})}`,
        `calibration ${JSON.stringify(item.calibration ?? {})}`,
        item.work_ref ? `work ${item.work_ref}` : null,
        item.mission_id ? `mission ${item.mission_id}` : null,
        item.trial_id ? `trial ${item.trial_id}` : null,
        item.research_run_id ? `research run ${item.research_run_id}` : null,
        item.cognition_session_id ? `cognition session ${item.cognition_session_id}` : null,
        item.execution_proposal_id ? `execution proposal ${item.execution_proposal_id}` : null,
      ].filter(Boolean).join(' · ');
      entries.append(element('li', `${item.recorded_at ?? 'time unknown'} · ${item.lifecycle_state ?? item.state} · ${details}`));
    }
    timeline.append(entries);
  }
  host.append(timeline);
  const evidence = decision.decision_state_at_time ?? {};
  const stateNote = element('p', `Decision snapshot: ${decision.decision_version ?? 'legacy'} · ${decision.decided_at ?? 'time unknown'} · period ${decision.month_utc ?? 'unknown'} ${evidence.period_closure ?? ''}. Snapshot values are preserved at decision time; results above are appended observations.`, 'quiet');
  host.append(stateNote);
  const decisionHistory = Array.isArray(decision.decision_history) ? decision.decision_history : [];
  if (decisionHistory.length > 1) {
    const priorSection = element('section', undefined, 'economic-investigation-alternatives');
    priorSection.append(element('strong', 'PRIOR PERSISTED DECISIONS'));
    const priorList = element('ol', undefined, 'canonical-economy-list');
    for (const prior of decisionHistory.slice(1)) {
      const matured = prior.result_history?.map(item => `${item.lifecycle_state}: ${item.detail}`).join(' → ')
        || 'no appended result yet';
      priorList.append(element('li', `${prior.decided_at} · ${prior.action} ${prior.selected_candidate ?? ''} · ${prior.active_lifecycle_state} · ${prior.why} · ${matured}`));
    }
    priorSection.append(priorList);
    host.append(priorSection);
  }
}

function stripeAmount(item) {
  if (item?.amount_minor == null) return 'Unknown';
  return item.currency === 'usd'
    ? money((Number(item.amount_minor) / 100).toFixed(2))
    : `${item.amount_minor} minor units · ${String(item.currency ?? 'unknown').toUpperCase()}`;
}

function renderStripeEconomy(payload) {
  const state = $('stripe-state');
  const balances = $('stripe-balance');
  const payments = $('stripe-payments');
  const history = $('stripe-history');
  balances.replaceChildren();
  payments.replaceChildren();
  history.replaceChildren();
  if (!payload || payload.status === 'not_observed') {
    state.textContent = payload?.status === 'not_observed' ? 'Awaiting protected-profile sync' : 'Stripe state unavailable';
    balances.append(element('p', 'The agent has not persisted a Stripe account observation.', 'quiet'));
    payments.append(element('p', 'No payment history is available yet.', 'quiet'));
    history.append(element('p', 'Balance history will appear after persisted account observations.', 'quiet'));
    $('stripe-payment-count').textContent = 'No persisted Stripe records';
    return;
  }
  state.textContent = `${payload.status === 'connected' ? (payload.livemode ? 'LIVE ACCOUNT' : 'TEST ACCOUNT') : String(payload.status).toUpperCase()} · ${dateLabel(payload.observed_at)}`;
  const addBalance = (labelText, rows) => {
    for (const row of rows ?? []) {
      const card = element('div', undefined, 'stripe-balance');
      card.append(element('span', `${labelText} · ${String(row.currency).toUpperCase()}`));
      card.append(element('strong', stripeAmount(row)));
      balances.append(card);
    }
  };
  addBalance('Available', payload.available);
  addBalance('Pending', payload.pending);
  const gross = payload.successful_usd_minor == null ? null : Number(payload.successful_usd_minor) / 100;
  const grossCard = element('div', undefined, 'stripe-balance');
  grossCard.append(element('span', 'Successful gross · returned batch'));
  grossCard.append(element('strong', gross == null ? 'Unknown' : money(gross.toFixed(2))));
  balances.append(grossCard);
  const statuses = (counts) => Object.entries(counts ?? {}).map(([name, count]) => `${name} ${count}`).join(' · ') || 'none returned';
  const subscriptionCard = element('div', undefined, 'stripe-balance');
  subscriptionCard.append(element('span', 'Subscription statuses · bounded query'));
  subscriptionCard.append(element('strong', statuses(payload.subscription_status_counts)));
  balances.append(subscriptionCard);
  const invoiceCard = element('div', undefined, 'stripe-balance');
  invoiceCard.append(element('span', 'Invoice statuses · bounded query'));
  invoiceCard.append(element('strong', statuses(payload.invoice_status_counts)));
  balances.append(invoiceCard);
  const samples = (payload.balance_history ?? []).filter((item) => Number.isFinite(Number(item.usd_balance_minor)));
  history.append(element('p', 'STRIPE USD BALANCE · AVAILABLE + PENDING · NOT PROFIT', 'stripe-history-label'));
  if (samples.length < 2) {
    history.append(element('p', 'A real balance trend will appear after a second persisted sample.', 'quiet'));
  } else {
    const width = 640, height = 112, pad = 12;
    const values = samples.map((item) => Number(item.usd_balance_minor));
    const lo = Math.min(...values), hi = Math.max(...values);
    const span = Math.max(1, hi - lo);
    const points = values.map((value, index) => ({
      x: pad + (width - 2 * pad) * index / (values.length - 1),
      y: height - pad - ((value - lo) / span) * (height - 2 * pad),
    }));
    const chart = svgNode('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img',
      'aria-label': `Persisted Stripe USD balance observations from ${dateLabel(samples[0].observed_at)} to ${dateLabel(samples.at(-1).observed_at)}` });
    chart.append(svgNode('title', {}, 'Observed Stripe available plus pending USD balance; this is not revenue or profit.'));
    chart.append(svgNode('path', { d: points.map((point, index) => `${index ? 'L' : 'M'} ${point.x} ${point.y}`).join(' '), class: 'stripe-history-line' }));
    points.forEach((point, index) => {
      const dot = svgNode('circle', { cx: point.x, cy: point.y, r: 3, class: 'stripe-history-point' });
      dot.append(svgNode('title', {}, `${dateLabel(samples[index].observed_at)} · ${money((values[index] / 100).toFixed(2))}`));
      chart.append(dot);
    });
    history.append(chart);
    history.append(element('small', `${money((lo / 100).toFixed(2))} – ${money((hi / 100).toFixed(2))} · ${samples.length} persisted samples`, 'stripe-history-range'));
  }
  $('stripe-payment-count').textContent = `${payload.succeeded_count ?? 0} succeeded / ${payload.payment_intent_count ?? 0} returned (cap ${payload.scan_limit ?? 'unknown'})`;
  const recent = payload.payments ?? [];
  if (!recent.length) payments.append(element('p', 'No PaymentIntents were returned by the latest bounded scan.', 'quiet'));
  for (const payment of recent) {
    const row = element('div', undefined, 'stripe-payment-row');
    const statusId = `${payment.status ?? 'unknown'} · ${payment.payment_intent_id ?? 'id unavailable'}`;
    row.append(element('strong', statusId));
    const amount = payment.amount_received_minor == null ? 'Amount unavailable'
      : payment.currency === 'usd' ? money((Number(payment.amount_received_minor) / 100).toFixed(2))
        : `${payment.amount_received_minor} minor units · ${String(payment.currency ?? '').toUpperCase()}`;
    const attribution = payment.mission_id ? `mission ${payment.mission_id}`
      : payment.attribution?.noema_lane ? `lane ${payment.attribution.noema_lane}`
        : payment.attribution?.noema_product_id ? `product ${payment.attribution.noema_product_id}`
          : dateLabel(payment.created_at);
    row.append(element('span', amount));
    row.append(element('small', attribution));
    payments.append(row);
  }
  const unavailable = payload.capabilities?.not_exposed ?? ['fees', 'refunds', 'payouts'];
  $('stripe-note').textContent = `${payload.accounting_note ?? 'Gross payment observations only.'} Not exposed by current MCP tools: ${unavailable.join(', ') || 'none reported'}. Stripe balance is not treated as profit or owner-independent net revenue.`;
}

function renderKnowledge(payload) {
  const target = $('learning-overview');
  target.replaceChildren();
  if (!payload || payload.status === 'unavailable' || payload.status === 'degraded') {
    $('learning-state').textContent = payload?.status === 'degraded' ? 'LEARNING STORE DEGRADED' : 'LEARNING STATE UNAVAILABLE';
    target.append(element('p', 'No source registry or empirical belief records are available yet.', 'quiet'));
    return;
  }
  const beliefs = payload.beliefs ?? [];
  const sources = payload.source_status ?? [];
  const retrievals = payload.recent_retrievals ?? [];
  const refreshFailures = payload.failed_source_count ?? sources.filter((item) => item.status === 'failed').length;
  $('learning-state').textContent = payload.status === 'not_initialized' ? 'NOT INITIALIZED' : 'PERSISTED';
  const stats = element('div', undefined, 'knowledge-stats');
  stats.append(
    element('span', `Sources · ${payload.source_count ?? 0}`),
    element('span', `Fetched · ${payload.fetched_source_count ?? 0}`),
    element('span', `Stale · ${payload.stale_source_count ?? 0}`),
    element('span', `Refresh failures · ${refreshFailures}`),
    element('span', `Not fetched · ${payload.never_fetched_source_count ?? 0}`),
    element('span', `Beliefs · ${payload.belief_count ?? 0}`),
    element('span', `Recent retrievals · ${retrievals.length}`),
  );
  target.append(stats);
  const liveRoles = snapshot?.sections?.specialists?.rows ?? [];
  const assignments = element('div', undefined, 'knowledge-sources');
  const assignmentsData = payload.curriculum_assignments?.length ? payload.curriculum_assignments
    : liveRoles.map((item) => ({ specialist: item.name, role: `${item.state ?? 'registered'} · ${item.family ?? 'area unavailable'}`, curriculum: item.family ?? 'area unavailable' }));
  for (const item of assignmentsData) {
    assignments.append(element('div', `${item.specialist} · ${item.role} · ${item.curriculum.replaceAll('_', ' ')}`, 'knowledge-source'));
  }
  if (assignments.childElementCount) {
    const scopeLabel = payload.curriculum_assignments?.length
      ? 'Active prompt scopes · curricula do not create additional runtime agents'
      : 'Registered specialist areas from the live roster';
    const heading = element('p', scopeLabel, 'quiet');
    target.append(heading, assignments);
  }
  const list = element('div', undefined, 'knowledge-sources');
  for (const source of sources.filter((item) => item.freshness !== 'fresh' || item.status === 'failed').slice(0, 6)) {
    const row = element('div', undefined, 'knowledge-source');
    const failure = source.last_detail_code ? ` · ${source.last_detail_code}` : '';
    const label = element('span', `${source.source_id} · ${source.freshness.replaceAll('_', ' ')} · ${source.status}${failure}`);
    row.append(label);
    if (typeof source.url === 'string' && source.url.startsWith('https://')) {
      const link = element('a', 'source'); link.href = source.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      row.append(link);
    }
    list.append(row);
  }
  if (!list.childElementCount) list.append(element('p', 'All registered sources are within their freshness window.', 'quiet'));
  target.append(list);
  const beliefList = element('div', undefined, 'knowledge-beliefs');
  for (const belief of beliefs.slice(0, 4)) {
    const row = element('article', undefined, 'knowledge-belief');
    const confidence = belief.confidence == null ? 'unmeasured' : Number(belief.confidence).toFixed(2);
    row.append(element('strong', belief.claim));
    row.append(element('span', `${belief.validation_state} · confidence ${confidence} · n=${belief.sample_size}`));
    row.append(element('small', `Support ${belief.supporting_evidence?.length ?? 0} · contradict ${belief.contradicting_evidence?.length ?? 0} · checked ${dateLabel(belief.last_verified)}`));
    const detail = element('details');
    detail.append(element('summary', 'Inspect evidence and revision provenance'));
    const evidence = element('pre', JSON.stringify({
      applicable_regime: belief.applicable_regime,
      first_observed: belief.first_observed,
      expires_at: belief.expires_at,
      supporting_evidence: belief.supporting_evidence,
      contradicting_evidence: belief.contradicting_evidence,
      source_provenance: belief.source_provenance,
    }, null, 2));
    detail.append(evidence);
    row.append(detail);
    beliefList.append(row);
  }
  if (!beliefList.childElementCount) beliefList.append(element('p', 'No empirical beliefs have been recorded. Documentation has not been converted into profitability claims.', 'quiet'));
  target.append(beliefList);
}

function renderWalletState(payload) {
  const host = $('wallet-networks');
  const networks = payload?.networks ?? [];
  host.replaceChildren();
  $('wallets-asof').textContent = payload?.control_plane?.status ?? (networks.length ? 'READ-ONLY CHAIN STATE' : 'WALLET STATE UNAVAILABLE');
  if (!networks.length) host.append(element('p', 'No configured wallet network state is available. Wallet authority and live execution remain disabled.', 'quiet'));
  for (const network of networks) {
    const card = element('article', undefined, 'wallet-network');
    const head = element('div', undefined, 'wallet-network-head');
    head.append(element('span', `${network.chain ?? 'unknown'} · ${network.network ?? 'unknown'}`));
    head.append(element('span', network.connected ? 'CONNECTED' : network.connected === false ? 'DISCONNECTED' : 'CONNECTION UNKNOWN'));
    card.append(head);
    const native = network.sol ?? network.native_balance ?? network.btc;
    const symbol = network.chain === 'bitcoin' ? 'BTC' : network.chain === 'solana' ? 'SOL' : 'ETH';
    card.append(element('strong', native === undefined ? 'Balance unknown' : `${native} ${symbol}`, 'wallet-network-balance'));
    const detail = element('div', undefined, 'wallet-network-detail');
    detail.append(element('span', network.chain_id ? `CHAIN ${network.chain_id}` : 'MAINNET'));
    const assetStatus = network.token_status === 'unavailable'
      ? 'ERC-20 state unavailable'
      : network.token_status === 'indexed'
        ? `${network.tokens?.length ?? 0} indexed ERC-20`
        : network.chain === 'solana'
          ? `${network.tokens?.length ?? 0} SPL token account(s)`
          : 'token list not indexed';
    detail.append(element('span', assetStatus));
    card.append(detail);
    const capabilityValue = (value) => value === true ? 'YES' : value === false ? 'NO' : 'UNKNOWN';
    const capabilityRows = [
      ['AUTHENTICATED', network.authenticated], ['READABLE', network.readable],
      ['FUNDED', network.funded], ['SIGNER CONFIGURED', network.signer_configured],
      ['CREDENTIALS ISOLATED', network.credentials_isolated],
      ['RESEARCH ENABLED', network.research_enabled], ['PAPER ENABLED', network.paper_enabled],
      ['MISSION AUTHORITY PRESENT', network.mission_authority_present],
      ['LIVE EXECUTION ENABLED', network.live_execution_enabled], ['HALTED', network.halted],
      ['COORDINATOR WIRED', network.coordinator_wired],
    ];
    for (const [label, value] of capabilityRows) {
      card.append(element('div', `${label} · ${capabilityValue(value)}`, 'wallet-network-detail'));
    }
    if (network.signer_configured && !network.live_execution_enabled) {
      card.append(element('strong', 'SIGNER CONFIGURED · LIVE EXECUTION DISABLED', 'wallet-network-detail'));
    }
    if (network.funded && !network.mission_authority_present) {
      card.append(element('strong', 'FUNDED · UNAUTHORIZED', 'wallet-network-detail'));
    }
    if (network.address) card.append(element('div', network.address, 'wallet-network-address'));
    host.append(card);
  }
  const activity = $('wallet-activity');
  activity.replaceChildren();
  const events = section('wallet_transactions').rows.slice(0, 5);
  if (!events.length) {
    activity.append(element('p', 'No reconciled NOEMA wallet transactions recorded.', 'quiet'));
    return;
  }
  for (const event of events) {
    const row = element('div', undefined, 'wallet-activity-row');
    const details = typeof event.payload === 'string' ? parseObject(event.payload) : (event.payload ?? {});
    row.append(element('span', dateLabel(event.created_at, { hour: '2-digit', minute: '2-digit' })));
    row.append(element('span', details.chain_id ? `${details.chain ?? 'chain'} · ${details.chain_id}` : details.chain ?? 'chain unknown'));
    row.append(element('strong', `${details.status ?? (event.event_type ?? 'wallet event').replaceAll('_', ' ')} · ${details.action ?? 'action unknown'}`));
    const fee = details.fee_paid_wei ?? details.fee_amount_atomic ?? details.fee_lamports;
    const feeUnit = details.chain === 'bitcoin' ? 'sats' : details.chain === 'solana' ? 'lamports' : 'wei';
    row.append(element('span', fee === undefined || fee === null ? 'Fee unreported' : `Fee ${fee} ${feeUnit}`));
    row.append(element('span', details.transaction_reference ?? 'receipt pending'));
    activity.append(row);
  }
}

function renderExecutionGateway(payload) {
  $('execution-gateway-state').textContent = payload?.status ?? 'GATEWAY STATE UNAVAILABLE';
  const host = $('execution-gateway-activity');
  host.replaceChildren();
  const requests = payload?.recent_requests ?? [];
  if (!requests.length) {
    host.append(element('p', 'No execution proposals or policy decisions recorded.', 'quiet'));
    return;
  }
  for (const request of requests) {
    const row = element('div', undefined, 'gateway-request');
    const reasonObject = typeof request.result_json === 'string' ? parseObject(request.result_json) : {};
    const proposal = typeof request.request_json === 'string' ? parseObject(request.request_json) : {};
    const reasons = Array.isArray(reasonObject.reasons) ? reasonObject.reasons.join(' · ') : '';
    row.append(
      element('span', dateLabel(request.created_at, { hour: '2-digit', minute: '2-digit' })),
      element('span', `${request.route ?? 'route'} · ${request.tier ?? 'tier'}`),
      element('strong', `${request.status ?? 'unknown'} · ${request.venue ?? 'venue unknown'} · ${proposal.instrument ?? ''} · ${proposal.side ?? proposal.action ?? ''}`),
      element('span', request.notional_usd == null ? 'notional unknown' : `risk ${money(Number(request.notional_usd))}`),
      element('span', proposal.expected_edge == null ? 'edge unknown' : `edge ${(Number(proposal.expected_edge) * 100).toFixed(1)}¢/contract`),
      element('span', request.provider_reference ?? request.proposal_id ?? 'reference unavailable'),
      element('span', reasons || 'No policy rejection recorded.'),
    );
    host.append(row);
  }
}

function renderRoster() {
  const specialists = rows('specialists');
  const plans = snapshot?.attention_allocations?.rows ?? [];
  const attention = new Map(plans.map((item) => [item.specialist, item]));
  const runs = rows('research_runs');
  $('roster-count').textContent = `${specialists.length} registered · real assignment state`;
  const host = $('roster-list'); host.replaceChildren();
  if (!specialists.length) host.append(element('p', 'No specialist records are available.', 'quiet'));
  for (const specialist of specialists) {
    const latest = runs.find((run) => run.specialist === specialist.name);
    const current = runs.find((run) => run.specialist === specialist.name && run.status === 'running');
    const allocation = attention.get(specialist.name);
    const card = element('article', undefined, 'specialist');
    card.dataset.status = String(specialist.state ?? 'unknown').toLowerCase();
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    card.setAttribute('aria-label', `${specialist.name}, ${current ? 'working' : 'idle'}, inspect specialist history`);
    const initials = specialist.name.split(/[-\s]+/).map((part) => part[0]).join('').slice(0, 2).toUpperCase();
    card.append(element('span', initials, 'unit-avatar'));
    const copy = element('div', undefined, 'specialist-copy');
    const heading = element('div', undefined, 'specialist-heading');
    heading.append(element('strong', specialist.name), element('span', specialist.state ?? 'unknown'));
    copy.append(heading, element('p', specialist.family ?? 'Family unavailable', 'specialist-family'));
    copy.append(element('p', current ? `Working · ${current.kind}` : 'Idle · no active run', 'specialist-family'));
    copy.append(element('p', latest ? `Latest · ${latest.kind} · ${latest.status} · ${resultSummary(latest)}` : 'No recorded mission contribution', 'specialist-result'));
    const pct = Number(allocation?.attention_fraction);
    if (Number.isFinite(pct) && pct >= 0 && pct <= 1) {
      const share = element('div', undefined, 'attention-share');
      share.append(element('span', `${(pct * 100).toFixed(0)}% research attention${Number.isFinite(Number(allocation.attention_delta)) ? ` · ${(Number(allocation.attention_delta) * 100).toFixed(1)}pp` : ''}`));
      const meter = document.createElement('meter'); meter.min = 0; meter.max = 1; meter.value = pct; meter.setAttribute('aria-label', `${specialist.name} research attention`); share.append(meter);
      copy.append(share);
    }
    const resolved = Number(specialist.resolved);
    const reliable = resolved >= 30 && specialist.reliability != null;
    const facts = element('div', undefined, 'specialist-metrics');
    facts.append(element('span', reliable ? `${resolved} resolved` : `${Number.isFinite(resolved) ? resolved : 'Unknown'} outcomes · early`));
    facts.append(element('span', reliable ? `Reliability ${Number(specialist.reliability).toFixed(2)}` : 'Reliability unmeasured'));
    if (reliable && specialist.after_cost_return != null) facts.append(element('span', `After-cost return ${specialist.after_cost_return}`));
    copy.append(facts);
    card.append(copy);
    card.onclick = () => selectRecord('specialists', specialist);
    card.onkeydown = (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); card.click(); } };
    host.append(card);
  }
}

function resultSummary(run) {
  const result = runResult(run);
  const summary = result.conclusion ?? result.reason;
  if (typeof summary === 'string' && summary.trim()) return summary.trim().slice(0, 150);
  if (Number.isFinite(result.observations)) return `${formatCount(result.observations)} observations`;
  return `${run.status} · details persisted`;
}
function renderMissionList() {
  const missions = rows('missions').filter((mission) => mission.status !== 'superseded');
  const openStatuses = new Set(['discovered', 'queued', 'claimed', 'running', 'waiting']);
  const open = missions.filter((mission) => openStatuses.has(mission.status));
  const completed = missions.filter((mission) => !openStatuses.has(mission.status));
  const visible = [...open, ...completed].slice(0, 8);
  $('mission-count').textContent = `${open.length} open · ${missions.length} persisted`;
  const list = $('mission-list'); list.replaceChildren();
  if (!visible.length) list.append(element('p', 'NOEMA is idle. No current opportunity has earned a mission.', 'quiet'));
  for (const mission of visible) {
    const button = element('button', undefined, 'mission-card');
    button.type = 'button';
    button.dataset.status = mission.status;
    button.dataset.missionId = mission.mission_id;
    button.setAttribute('aria-pressed', String(mission.mission_id === focusMissionId));
    button.append(element('span', `MISSION · ${String(mission.mission_id).slice(0, 14)}`, 'mission-id'));
    button.append(element('span', mission.status, 'mission-state'));
    button.append(element('span', mission.objective ?? 'Objective unavailable', 'mission-objective'));
    const measurement = mission.measurement ?? {};
    const handoffs = rows('handoffs').filter((item) => item.mission_id === mission.mission_id);
    const handoffSummary = handoffs.map((item) => `${item.from_specialist} → ${item.to_specialist}`).join(' · ');
    const review = mission.result?.critic_review?.verdict;
    const result = mission.status === 'passed' ? 'PASS' : review ?? mission.status;
    const metadata = [mission.specialist, `lane ${measurement.economic_lane ?? 'unknown'}`, `result ${result}`,
      handoffSummary || 'no handoff', mission.lesson_id ? `lesson ${mission.lesson_id}` : 'lesson unrecorded',
      `worker ${measurement.elapsed_worker_seconds == null ? 'unknown' : `${Number(measurement.elapsed_worker_seconds).toFixed(2)}s`}`,
      dateLabel(mission.updated_at)].join(' · ');
    button.append(element('span', metadata, 'mission-meta'));
    button.onclick = () => { focusMissionId = mission.mission_id; renderWorld(); };
    list.append(button);
  }
}

function renderLineage(mission) {
  const events = rows('mission_events').filter((event) => event.mission_id === mission?.mission_id)
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  const handoffs = rows('handoffs').filter((item) => item.mission_id === mission?.mission_id);
  const trace = $('event-trace'); trace.replaceChildren();
  $('trace-title').textContent = mission ? `Mission ${String(mission.mission_id).slice(0, 12)} lineage` : 'Mission lineage';
  $('trace-limit').textContent = events.length ? `${events.length} persisted transitions` : 'No transitions recorded';
  const chips = $('lineage-strip'); chips.replaceChildren();
  const hasWork = Boolean(mission?.run_id);
  const participants = [];
  const addParticipant = (name, role) => {
    if (!name) return;
    const participant = participants.find((item) => item.name === name);
    if (participant) { if (!participant.roles.includes(role)) participant.roles.push(role); }
    else participants.push({ name, roles: [role] });
  };
  if (events.some((event) => event.actor === 'NOEMA')) addParticipant('NOEMA', 'coordinator');
  if (hasWork && mission?.specialist) addParticipant(mission.specialist, 'worker');
  for (const handoff of handoffs) {
    addParticipant(handoff.from_specialist, handoff.from_specialist?.includes('critic') ? 'critic' : 'worker');
    addParticipant(handoff.to_specialist, handoff.to_specialist?.includes('critic') ? 'critic' : 'worker');
  }
  if (mission?.lesson_id) addParticipant(`Lesson ${mission.lesson_id}`, 'memory');
  if (!participants.length) chips.append(element('span', 'No worker handoff or contribution persisted', 'lineage-chip'));
  for (const participant of participants) {
    const chip = element('span', undefined, 'lineage-chip');
    chip.dataset.role = participant.roles[0];
    chip.append(element('span', participant.name), element('small', participant.roles.join(' / '), 'lineage-role'));
    chips.append(chip);
  }
  const ordered = events.slice(-20).reverse();
  if (!ordered.length) {
    trace.append(element('li', mission ? 'No mission transitions are recorded.' : 'No runtime events recorded.', 'quiet'));
    return;
  }
  for (const event of ordered) {
    const item = document.createElement('li');
    item.append(element('span', event.detail || event.event_type, 'event-copy'));
    const meta = [event.event_type, event.status, event.actor, event.evidence_id ? `evidence ${String(event.evidence_id).slice(0, 10)}` : null, shortTime(event.created_at)].filter(Boolean).join(' · ');
    item.append(element('span', meta, 'event-meta'));
    trace.append(item);
  }
}

function renderWorld() {
  if (!snapshot) return;
  noemaWorld.update(snapshot, { providers: providerHealth, venues: predictionVenuesSnapshot, wallets: walletSnapshot,
    gateway: gatewaySnapshot, freshness: capabilityFreshness });
  const runtime = snapshot.runtime ?? {};
  const cycle = runtime.cycle ?? {};
  const resources = snapshot.resources ?? {};
  const sessions = rows('sessions');
  const mission = currentMission();
  const run = rows('research_runs').find((item) => item.status === 'running');
  const activity = rows('activity');
  const latestEvent = mission ? rows('mission_events').filter((item) => item.mission_id === mission.mission_id).sort((a,b) => String(b.created_at).localeCompare(String(a.created_at)))[0] : null;
  const hasActiveWork = Boolean(run || rows('missions').some((item) => ['running', 'claimed', 'waiting'].includes(item.status)));
  const partial = runtime.state === 'running' && cycle.health && cycle.health !== 'healthy';
  const stateText = resources.state === 'resource_limited' ? 'RESOURCE LIMITED'
    : runtime.state === 'running' ? partial ? 'LIVE · PARTIAL' : 'LIVE'
      : String(runtime.state ?? 'unknown').toUpperCase();
  $('runtime').textContent = `NOEMA ${stateText} · health ${cycle.health ?? runtime.health ?? 'unknown'}`;
  $('runtime-ribbon').dataset.state = resources.state === 'resource_limited' ? 'partial' : runtime.state;
  $('runtime-indicator').dataset.state = runtime.state ?? 'unknown';
  $('updated').textContent = snapshot.as_of ? dateLabel(snapshot.as_of) : 'Snapshot unavailable';
  $('lead-state').textContent = resources.state === 'resource_limited' ? 'RESOURCE LIMITED'
    : runtime.state === 'running' ? partial ? 'RUNNING / PARTIAL' : 'RUNNING'
      : String(runtime.state ?? 'unknown').toUpperCase();
  $('lead-state').dataset.tone = resources.state === 'resource_limited' || partial ? 'warning' : runtime.state === 'running' ? 'running' : runtime.state === 'stale' ? 'warning' : 'bad';
  $('noema-lead').dataset.status = String(runtime.state ?? 'unknown');
  $('lead-cycle').textContent = cycle.cycle_id ? `CYCLE ${cycle.cycle_id} · ${cycle.active_goal ?? 'no active goal'}` : 'No cycle recorded';
  const session = sessions[0] ?? null;
  $('lead-cognition').textContent = session?.provider
    ? `Last route · ${session.provider} · ${session.model ?? 'model unknown'}` : 'Cognition route unrecorded';
  $('core-heartbeat').textContent = runtime.last_heartbeat_at ? `Heartbeat · ${dateLabel(runtime.last_heartbeat_at)}` : 'Heartbeat unavailable';

  const focus = mission;
  focusMissionId = focus?.mission_id ?? focusMissionId;
  $('mission-status-mark').textContent = focus?.status === 'passed' ? '✓' : focus?.status === 'completed' ? '✓' : focus?.status === 'failed' ? '×' : focus?.status === 'running' ? '↗' : focus ? '·' : '—';
  $('mission-status-mark').dataset.state = focus?.status ?? 'unknown';
  $('focus-title').textContent = focus?.objective ?? (run ? `${run.specialist} · ${run.kind}` : 'No mission recorded');
  $('focus-detail').textContent = focus?.measurement?.information_outcome?.conclusion
    ?? latestEvent?.detail ?? (focus ? `Mission ${focus.mission_id} · ${focus.status}` : 'NOEMA is idle until evidence justifies bounded work.');
  $('focus-stage').textContent = focus ? `${focus.status.toUpperCase()} · ${focus.specialist}` : 'NO ACTIVE MISSION';
  const measurement = focus?.measurement ?? {};
  const workerSeconds = measurement.elapsed_worker_seconds;
  const modelCost = measurement.model_api_cost_usd;
  $('focus-cost').textContent = `Worker ${workerSeconds == null ? 'time unknown' : `${Number(workerSeconds).toFixed(2)}s`} · model cost ${money(modelCost)}`;
  const receipts = measurement.cash_receipts_usd;
  const information = measurement.information_outcome ?? {};
  if (information.revenue_test_status === 'not_tested') {
    $('focus-economics').textContent = `Qualification · ${information.verified_buyer_count ?? 0} verified buyers · ${money(information.mission_cash_receipt_usd)} mission receipts · revenue not tested · costs unknown`;
  } else {
    $('focus-economics').textContent = receipts == null ? 'Mission-linked cash · unknown · full net profit unknown'
      : `Operator-reported receipts ${money(receipts)} · ${measurement.cash_basis} · full profit unknown`;
  }
  $('inspect-mission').disabled = !focus;
  $('inspect-mission').onclick = () => { if (focus) selectRecord('missions', focus); };

  const openclaw = snapshot.openclaw_worker ?? {};
  const memory = resources.available_memory_percent == null ? 'Memory headroom unknown' : `${resources.available_memory_percent}% memory headroom`;
  const slots = Object.entries(resources.slots ?? {}).filter(([, status]) => status !== 'idle').map(([name]) => name);
  const available = resources.active_workload ? `Reserved · ${resources.active_workload}` : slots.length ? `Reserved · ${slots.join(', ')}` : 'No heavyweight worker active';
  $('support-state').textContent = resources.state === 'resource_limited' ? 'RESOURCE LIMITED'
    : resources.state === 'queued' ? 'QUEUED' : hasActiveWork ? 'WORK IN PROGRESS' : 'IDLE';
  $('resource-gate').dataset.state = resources.state === 'resource_limited' ? 'resource_limited' : hasActiveWork ? 'running' : 'idle';
  $('support-detail').textContent = `${memory} · ${available}`;
  $('support-worker').textContent = openclaw.enabled ? `OpenClaw · ${openclaw.state ?? 'unknown'}` : 'OpenClaw · on-demand / disabled';
  $('support-memory').textContent = `Caps · Docker ${resources.limits?.docker_workers ?? 'unknown'} · model ${resources.limits?.large_local_models ?? 'unknown'} · experiments ${resources.limits?.experiments ?? 'unknown'}`;
  const evmConnection = runtime.connections?.evm_wallet;
  const evmState = evmConnection && typeof evmConnection === 'object' ? evmConnection.status : evmConnection;
  $('authority-state').textContent = `EVM wallet · ${evmState ?? 'unknown'} · live activity not inferred`;
  $('resource-state').textContent = resources.state === 'resource_limited'
    ? `RESOURCE LIMITED · ${resources.detail ?? 'capacity unavailable'}`
    : `${resources.state ?? 'unknown'} · ${resources.minimum_available_memory_percent ?? 'unknown'}% minimum memory floor`;

  const activityState = runtime.state === 'running' && partial ? 'partial' : runtime.state ?? 'unknown';
  document.querySelector('.runtime-ribbon').dataset.state = activityState;
  renderEvidenceChart();
  renderLanes();
  renderAllocation();
  renderRoster();
  renderMissionList();
  renderLineage(focus);
  renderEconomy(economicsSnapshot);
}

function renderLiveChange() {
  const events = rows('mission_events');
  const newest = events.reduce((max, event) => Math.max(max, Number(event.id) || 0), 0);
  if (latestEventId !== null && newest > latestEventId) {
    const event = events.find((item) => Number(item.id) === newest);
    if (event) {
      $('live-change').textContent = `${event.actor} · ${event.event_type.replaceAll('_', ' ')} · ${event.status}`;
      $('live-change').dataset.visible = 'true';
      clearTimeout(changeTimer);
      changeTimer = setTimeout(() => { $('live-change').dataset.visible = 'false'; }, 4200);
      const card = document.querySelector(`.mission-card[data-mission-id="${CSS.escape(event.mission_id)}"]`);
      card?.classList.add('state-arrived');
      if (card) setTimeout(() => card.classList.remove('state-arrived'), 1700);
    }
  }
  latestEventId = Math.max(latestEventId ?? 0, newest);
}

async function get(path) {
  const response = await fetch(path, { cache: 'no-store', signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}
async function refresh() {
  if (refreshing) { refreshQueued = true; return; }
  refreshing = true;
  $('refresh').disabled = true;
  const checkProviders = Date.now() - providerHealthAt >= 300000;
  const [operations, economics, wallets, gateway, trench, stripe, knowledge, providers, runtime] = await Promise.allSettled([
    get('/api/operations'), get('/api/economic-measurement'), get('/api/wallet-status'), get('/api/execution-gateway'), get('/api/trench'), get('/api/stripe-economy'), get('/api/knowledge'),
    checkProviders ? get('/api/provider-health') : Promise.resolve(providerHealth),
    get('/api/runtime'),
  ]);
  const errors = [];
  if (operations.status === 'fulfilled') {
    snapshot = operations.value;
    capabilityFreshness.operations = 'current';
    capabilityFreshness.operationsAt = snapshot.as_of ?? new Date().toISOString();
    if (selected) {
      const fresh = section(selected.view).rows.find((row) => keyOf(selected.view, row) === keyOf(selected.view, selected.row));
      if (fresh) selected = { ...selected, row: fresh };
    }
    renderLiveChange();
    if ($('deep-records').open) { renderArchive(); renderDetail(); }
  } else {
    capabilityFreshness.operations = snapshot ? 'stale' : 'unavailable';
    errors.push('Live operating state unavailable; showing the last successful snapshot.');
  }
  if (economics.status === 'fulfilled') economicsSnapshot = economics.value;
  else errors.push('Economic measurement unavailable; showing the last successful snapshot.');
  renderEconomy(economicsSnapshot);
  if (wallets.status === 'fulfilled') {
    walletSnapshot = wallets.value;
    capabilityFreshness.wallets = 'current';
    capabilityFreshness.walletsAt = new Date().toISOString();
    renderWalletState(walletSnapshot);
  } else {
    capabilityFreshness.wallets = walletSnapshot ? 'stale' : 'unavailable';
    $('wallets-asof').textContent = 'WALLET STATE UNAVAILABLE';
  }
  if (gateway.status === 'fulfilled') {
    gatewaySnapshot = gateway.value;
    capabilityFreshness.gateway = 'current';
    capabilityFreshness.gatewayAt = new Date().toISOString();
    renderExecutionGateway(gatewaySnapshot);
  } else {
    capabilityFreshness.gateway = gatewaySnapshot ? 'stale' : 'unavailable';
    $('execution-gateway-state').textContent = 'GATEWAY STATE UNAVAILABLE';
  }
  if (trench.status === 'fulfilled') trenchSnapshot = trench.value;
  renderWeb3Evidence(trenchSnapshot);
  if (stripe.status === 'fulfilled') stripeEconomySnapshot = stripe.value;
  renderStripeEconomy(stripeEconomySnapshot);
  if (knowledge.status === 'fulfilled') knowledgeSnapshot = knowledge.value;
  renderKnowledge(knowledgeSnapshot);
  if (Date.now() - predictionVenuesAt >= 60000) {
    try {
      predictionVenuesSnapshot = await get('/api/prediction-venues');
      predictionVenuesAt = Date.now();
      capabilityFreshness.venues = 'current';
      capabilityFreshness.venuesAt = new Date(predictionVenuesAt).toISOString();
    } catch {
      capabilityFreshness.venues = predictionVenuesSnapshot ? 'stale' : 'unavailable';
      predictionVenuesAt = Date.now();
    }
  }
  renderPredictionVenues(predictionVenuesSnapshot);
  if (checkProviders) {
    if (providers.status === 'fulfilled') {
      providerHealth = providers.value;
      capabilityFreshness.providers = 'current';
      capabilityFreshness.providersAt = new Date().toISOString();
    } else {
      capabilityFreshness.providers = providerHealth ? 'stale' : 'unavailable';
    }
    providerHealthAt = Date.now();
  }
  if (runtime.status === 'fulfilled' && runtime.value.database_present && runtime.value.snapshot_at) {
    const source = runtime.value.source === 'worker-sqlite-replica' ? 'WORKER REPLICA' : 'LOCAL DATABASE';
    const ageSeconds = Number(runtime.value.age_seconds);
    const age = ageSeconds < 60 ? `${ageSeconds}s ago` : `${Math.floor(ageSeconds / 60)}m ago`;
    const workerCommit = runtime.value.worker?.worker_commit;
    const sourceCommit = workerCommit && workerCommit !== 'unknown' ? ` · worker ${workerCommit.slice(0, 7)}` : '';
    const commit = runtime.value.deployment_commit ? ` · console ${runtime.value.deployment_commit.slice(0, 7)}` : '';
    $('sync-state').textContent = `${runtime.value.snapshot_stale ? 'STALE ' : ''}${source} · ${age}${sourceCommit}${commit}`;
    $('sync-state').dataset.state = runtime.value.snapshot_stale ? 'reconnecting' : 'live';
  } else {
    $('sync-state').textContent = 'WORKER SNAPSHOT UNAVAILABLE';
    $('sync-state').dataset.state = 'reconnecting';
  }
  renderWorld();
  $('error').hidden = errors.length === 0;
  $('error').textContent = errors.join(' ');
  $('refresh').disabled = false;
  refreshing = false;
  if (refreshQueued) {
    refreshQueued = false;
    scheduleLiveRefresh();
  }
}

function scheduleLiveRefresh() {
  if (streamRefreshTimer) return;
  const delay = Math.max(0, 3000 - (Date.now() - lastStreamRefresh));
  streamRefreshTimer = setTimeout(() => {
    streamRefreshTimer = null;
    lastStreamRefresh = Date.now();
    refresh();
  }, delay);
}

const runtimeStream = new EventSource('/api/runtime-stream');
runtimeStream.addEventListener('ready', () => {
  if (document.hidden) return;
  refresh();
});
runtimeStream.addEventListener('change', () => {
  if (document.hidden) return;
  scheduleLiveRefresh();
});
runtimeStream.addEventListener('unavailable', () => {
  $('sync-state').textContent = 'STREAM UNAVAILABLE';
  $('sync-state').dataset.state = 'reconnecting';
});
runtimeStream.onerror = () => {
  $('sync-state').textContent = 'RECONNECTING';
  $('sync-state').dataset.state = 'reconnecting';
};
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh();
});

$('search').oninput = renderArchive;
$('refresh').onclick = refresh;
$('back').onclick = () => { selected = history.pop(); if (selected) activeView = selected.view; renderArchive(); renderDetail(); };
renderArchive();
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
