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
  $('prediction-venues-asof').textContent = payload?.as_of ? `READ ONLY · ${dateLabel(payload.as_of)}` : 'VENUE STATE UNAVAILABLE';
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
      root.append(compare);
    }
  } else {
    root.append(element('p', match?.reason ?? 'No validated cross-venue comparison is available.', 'prediction-match-state'));
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
  $('metric-revenue').textContent = 'Unknown';
  $('metric-net').textContent = money(measurement?.net_economic_profit_usd);
  const attempts = Number(measurement?.operating_estimates?.model_attempt_count ?? 0);
  $('metric-model-cost').textContent = attempts > 0 ? money(measurement.operating_estimates.model_reserved_usd) : 'Unknown';
  const settled = Number(measurement?.paper?.settled_markets_this_month ?? 0);
  $('metric-paper').textContent = settled > 0 ? money(measurement.paper.net_after_execution_costs_usd) : 'Unknown';
  $('economics-time').textContent = measurement?.as_of ? `Accounting snapshot · ${dateLabel(measurement.as_of)}` : 'Economic measurement unavailable';

  const rows = snapshot?.economic_state?.balances;
  $('treasury-reserve').textContent = rows ? money(rows.reserve_usd) : 'Unknown';
  $('treasury-strategy').textContent = rows ? money(rows.strategy_capital_usd) : 'Unknown';
  $('treasury-research').textContent = rows ? money(rows.research_budget_usd) : 'Unknown';
  $('treasury-infrastructure').textContent = rows ? money(rows.infrastructure_budget_usd) : 'Unknown';

  const walletConnection = snapshot?.runtime?.connections?.evm_wallet;
  const wallet = walletConnection && typeof walletConnection === 'object' ? walletConnection.status : walletConnection;
  const cashCount = recordedEntries;
  const walletConfigured = wallet && !['unconfigured', 'disabled', 'unavailable'].includes(wallet);
  $('wallet-state').textContent = walletConfigured || cashCount ? 'PARTIAL RECORDS' : 'NO BALANCE SERIES';
  $('account-empty-title').textContent = walletConfigured || cashCount ? 'No reconciled balance curve' : 'No verified series';
  $('account-empty-detail').textContent = cashCount
    ? `${cashCount} operator-reported cash entries exist, but no reconciled account balance history is available.`
    : `No cash entries are recorded; wallet status is ${wallet ?? 'unknown'}.`;
  $('account-note').textContent = 'Wallet value, transfers and reconciled cash are not yet available as a time series.';
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
  $('wallets-asof').textContent = networks.length ? 'READ-ONLY CHAIN STATE' : 'WALLET STATE UNAVAILABLE';
  if (!networks.length) host.append(element('p', 'No isolated signing credential was found.', 'quiet'));
  for (const network of networks) {
    const card = element('article', undefined, 'wallet-network');
    const head = element('div', undefined, 'wallet-network-head');
    head.append(element('span', `${network.chain ?? 'unknown'} · ${network.network ?? 'unknown'}`));
    head.append(element('span', network.status === 'unavailable' ? 'RPC OFFLINE' : 'RPC ONLINE'));
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
    const signerState = network.signing_enabled && !network.master_halt ? 'LIVE EXECUTION ENABLED' : 'SIGNING PAUSED';
    card.append(element('div', signerState, 'wallet-network-detail'));
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
    const share = element('div', undefined, 'attention-share');
    if (Number.isFinite(pct) && pct >= 0 && pct <= 1) {
      share.append(element('span', `${(pct * 100).toFixed(0)}% research attention${Number.isFinite(Number(allocation.attention_delta)) ? ` · ${(Number(allocation.attention_delta) * 100).toFixed(1)}pp` : ''}`));
      const meter = document.createElement('meter'); meter.min = 0; meter.max = 1; meter.value = pct; meter.setAttribute('aria-label', `${specialist.name} research attention`); share.append(meter);
    } else share.append(element('span', 'Research allocation unknown'));
    copy.append(share);
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
  noemaWorld.update(snapshot);
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
  const [operations, economics, wallets, trench, stripe, knowledge] = await Promise.allSettled([
    get('/api/operations'), get('/api/economic-measurement'), get('/api/wallet-status'), get('/api/trench'), get('/api/stripe-economy'), get('/api/knowledge'),
  ]);
  const errors = [];
  if (operations.status === 'fulfilled') {
    snapshot = operations.value;
    if (selected) {
      const fresh = section(selected.view).rows.find((row) => keyOf(selected.view, row) === keyOf(selected.view, selected.row));
      if (fresh) selected = { ...selected, row: fresh };
    }
    renderWorld();
    renderLiveChange();
    if ($('deep-records').open) { renderArchive(); renderDetail(); }
  } else errors.push('Live operating state unavailable; showing the last successful snapshot.');
  if (economics.status === 'fulfilled') economicsSnapshot = economics.value;
  else errors.push('Economic measurement unavailable; showing the last successful snapshot.');
  renderEconomy(economicsSnapshot);
  if (wallets.status === 'fulfilled') renderWalletState(wallets.value);
  else $('wallets-asof').textContent = 'WALLET STATE UNAVAILABLE';
  if (trench.status === 'fulfilled') trenchSnapshot = trench.value;
  renderWeb3Evidence(trenchSnapshot);
  if (stripe.status === 'fulfilled') stripeEconomySnapshot = stripe.value;
  renderStripeEconomy(stripeEconomySnapshot);
  if (knowledge.status === 'fulfilled') knowledgeSnapshot = knowledge.value;
  renderKnowledge(knowledgeSnapshot);
  if (Date.now() - predictionVenuesAt >= 60000) {
    predictionVenuesAt = Date.now();
    try { predictionVenuesSnapshot = await get('/api/prediction-venues'); predictionVenuesAt = Date.now(); }
    catch { predictionVenuesSnapshot = null; }
  }
  renderPredictionVenues(predictionVenuesSnapshot);
  if (Date.now() - providerHealthAt >= 300000) {
    providerHealthAt = Date.now();
    try { providerHealth = await get('/api/provider-health'); providerHealthAt = Date.now(); }
    catch { providerHealth = null; }
  }
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
  $('sync-state').textContent = 'LIVE · COMMIT STREAM';
  $('sync-state').dataset.state = 'live';
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
