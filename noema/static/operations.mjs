import { createNoemaWorld } from './noema-world.mjs';
import { createLiveDesk } from './live-desk.mjs';
import { createLiveCapital } from './live-capital.mjs';
import { createEconomicCategories } from './economic-categories.mjs';

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
let radarSnapshot = null, radarFreshness = 'unknown';
const capabilityFreshness = { providers: 'unknown', wallets: 'unknown', gateway: 'unknown',
  venues: 'unknown', operations: 'unknown', providersAt: null, walletsAt: null,
  gatewayAt: null, venuesAt: null, operationsAt: null };
let predictionVenuesSnapshot;
let marketQualificationSnapshot;
let stripeEconomySnapshot;
let knowledgeSnapshot;
let activeView = 'sessions', selected, history = [], focusMissionId;
let refreshing = false, refreshQueued = false, latestEventId = null, changeTimer;
let streamRefreshTimer = null, lastStreamRefresh = 0;
let workstationContext = { type: 'core', id: 'agent:NOEMA', label: 'NOEMA', status: 'unknown', source: 'Persisted runtime heartbeat' };
let noemaWorld;
let displayGeneration = 0;
let operatingRequest = null, operatingQueued = false, operatingError = null;
let supplementalErrors = [];
let gatewayRequest = null;

function mountWorkstation() {
  const move = (node, target) => { if (node && target) target.append(node); };
  const byId = (id) => $(id);
  const laneSection = $('lanes-title')?.closest('.economy-section');
  const venueSection = $('prediction-venues-title')?.closest('.economy-section');
  const web3Section = $('web3-evidence-title')?.closest('.economy-section');
  const walletSection = $('wallets-title')?.closest('.economy-section');
  const gatewaySection = $('execution-gateway-title')?.closest('.economy-section');
  const stripeSection = $('stripe-title')?.closest('.economy-section');
  const learningSection = $('learning-title')?.closest('.economy-section');
  const map = document.querySelector('.world-map');
  const frame = map?.querySelector('.world-map-frame');
  if (frame) frame.after(byId('world-type-legend-details'));
  move(map, byId('workstation-topology'));
  move(frame?.querySelector('.world-inspector'), byId('workstation-inspector'));
  move(byId('world-entity-list'), byId('workstation-entities'));
  move(map?.querySelector('.world-timebar'), byId('workstation-timebar'));
  move(byId('world-event-list'), byId('workstation-event-list'));
  move(document.querySelector('.mission-stage'), byId('workstation-stage'));
  move(document.querySelector('.mission-board'), byId('workstation-missions'));
  move(document.querySelector('.team-panel'), byId('workstation-agents'));
  move(document.querySelector('.trace-board'), byId('workstation-history'));
  move(document.querySelector('.signal-grid'), byId('workstation-evidence'));
  move(laneSection, byId('workstation-economics'));
  move(document.querySelector('.treasury-panel'), byId('workstation-resources'));
  move(venueSection, byId('workstation-venues'));
  move(web3Section, byId('workstation-web3'));
  move(walletSection, byId('capital-wallet-diagnostics'));
  move(gatewaySection, byId('workstation-execution'));
  move(stripeSection, byId('workstation-revenue'));
  move(learningSection, byId('workstation-learning'));
  move(byId('deep-records'), byId('workstation-deep-records'));
  move(document.querySelector('.workstation-intelligence'), byId('workstation-research-panels'));
  move(byId('workstation-evidence'), byId('workstation-economic-panels'));
  move(byId('workstation-dock'), byId('workstation-support-panels'));
  move(byId('activity-desk'), byId('live-terminal-drawer'));
  move(document.querySelector('.desk-brief'), byId('live-terminal-drawer'));
  move(byId('workstation-venues'), byId('category-prediction'));
  move(byId('workstation-execution'), byId('category-prediction'));
  move(byId('workstation-web3'), byId('category-web3'));
  move(byId('workstation-experiments'), byId('category-strategies'));
  move(document.querySelector('.paper-history'), byId('validation-history'));
  move(document.querySelector('.evidence-panel'), byId('validation-history'));
  move(byId('workstation-learning'), byId('category-research'));
  move(byId('workstation-economics'), byId('category-economics'));
  move(byId('workstation-revenue'), byId('category-revenue'));

}

function contextFromNode(node) {
  const record = node.record ?? {};
  const context = { type: node.type, id: node.id, label: node.label, status: node.status ?? 'unknown',
    source: node.source ?? 'Persisted operating projection', record, node };
  context.market_id = record.market_id;
  context.venue = record.venue;
  context.chain = record.chain;
  context.asset = record.asset;
  context.mission_id = record.mission_id ?? node.missionId;
  context.trial_id = record.trial_id;
  context.specialist = record.specialist ?? record.name ?? (node.type === 'agent' && node.id !== 'agent:NOEMA' ? node.label : undefined);
  return context;
}

function mountInspectorTabs() {
  for (const tab of document.querySelectorAll('[data-inspector-tab]')) tab.addEventListener('click', () => {
    const name = tab.dataset.inspectorTab;
    document.querySelectorAll('[data-inspector-tab]').forEach((item) => item.setAttribute('aria-selected', String(item === tab)));
    document.querySelectorAll('.inspector-tab-content').forEach((panel) => { panel.hidden = panel.id !== `inspector-${name}`; });
  });
  $('clear-shared-selection')?.addEventListener('click', () => selectSharedContext({ type: 'core', id: 'agent:NOEMA', label: 'NOEMA', status: snapshot?.runtime?.state ?? 'unknown', source: 'Persisted runtime heartbeat' }));
}

mountWorkstation();
noemaWorld = createNoemaWorld(
  (node) => selectSharedContext(contextFromNode(node), false),
  (edge) => selectSharedContext({ type: 'relationship', id: edge.id, label: `${edge.fromLabel} → ${edge.toLabel}`,
    status: edge.type, source: 'Canonical topology relationship', mission_id: edge.missionId,
    record: { relationship: edge.type, from: edge.from, to: edge.to, mission_id: edge.missionId,
      created_at: edge.at ? new Date(edge.at).toISOString() : null } }, false),
);
mountInspectorTabs();
const liveDesk = createLiveDesk({
  onSelect: (context) => selectSharedContext(context),
  onPauseChange: (paused) => {
    displayGeneration++;
    liveCapital.render();
    if (!paused) refresh(true);
  },
});

const liveCapital = createLiveCapital({ isPaused: () => liveDesk.paused, onSelect: context => selectSharedContext(context), onWindowChange: () => refreshBalanceHistory() });
const capitalRequests = new Map();
const capitalCheckedAt = new Map();
let balanceHistoryRequest = null, balanceHistoryQueued = false;
const economicCategories = createEconomicCategories({
  onSelect: context => selectSharedContext(context),
  clearSelection: () => $('clear-shared-selection').click(),
});

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
function contextFromRecord(type, record, labelText) {
  return { type, id: `${type}:${record?.id ?? record?.market_id ?? record?.mission_id ?? record?.trial_id ?? record?.name ?? 'unknown'}`,
    label: labelText ?? record?.title ?? record?.objective ?? record?.name ?? record?.market_id ?? 'Selected record',
    status: record?.decision ?? record?.status ?? 'recorded', source: 'Persisted NOEMA record', record,
    market_id: record?.market_id, venue: record?.venue, mission_id: record?.mission_id,
    trial_id: record?.trial_id, specialist: record?.specialist ?? record?.name };
}

function updateWorkstationTelemetry({ state, cognition, mission, authority, budget, alert } = {}) {
  $('telemetry-runtime').textContent = state ?? 'Unknown';
  $('telemetry-cognition').textContent = cognition ?? 'Unknown';
  $('telemetry-mission').textContent = mission ?? 'Unknown';
  $('telemetry-authority').textContent = authority ?? 'Unknown';
  $('telemetry-budget').textContent = budget ?? 'Unknown';
  $('telemetry-alert').hidden = !alert;
  $('telemetry-alert-text').textContent = alert ?? '';
}

function contextFact(term, detail, explanation) {
  const card = element('div', undefined, 'context-fact');
  card.append(element('span', term), element('strong', detail ?? 'Unknown'));
  if (explanation) card.append(element('p', explanation));
  return card;
}

function renderWorkstationContext() {
  const context = workstationContext;
  $('shared-selection-type').textContent = String(context.type ?? 'system').replaceAll('_', ' ').toUpperCase();
  $('shared-selection-title').textContent = context.label ?? 'NOEMA';
  $('shared-selection-summary').textContent = `${context.status ?? 'unknown'} · ${context.source ?? 'source unavailable'}${context.record?.created_at ? ` · ${dateLabel(context.record.created_at)}` : ''}`;
  document.querySelectorAll('[data-node-id]').forEach((button) => {
    const marketNodeId = context.market_id ? `market:${context.venue ? `${context.venue}:` : ''}${context.market_id}` : null;
    button.dataset.contextSelected = String(button.dataset.nodeId === context.id || button.dataset.nodeId === marketNodeId);
  });
  $('sidebar-capability-count').textContent = `${document.querySelectorAll('#workstation-entities [data-node-id]').length} observed`;
  $('shared-selection-overview').replaceChildren(
    contextFact('Selected object', `${context.type ?? 'system'} · ${context.label ?? 'NOEMA'}`),
    contextFact('Live status', context.status ?? 'Unknown', context.source ?? 'No current source is recorded.'),
  );

  const evidence = $('shared-selection-evidence'); evidence.replaceChildren();
  const record = context.record ?? {};
  const detail = record.detail ?? record.reason ?? record.objective ?? record.hypothesis;
  $('selection-reader').hidden = !detail;
  $('selection-reader-text').textContent = detail ?? '';
  const radar = radarSnapshot?.find((row) => context.market_id && row.market_id === context.market_id
    && (!context.venue || row.venue === context.venue));
  const evidenceIds = record.evidence_ids ?? radar?.evidence_ids;
  evidence.append(contextFact('Evidence source', context.source ?? 'Unknown'));
  evidence.append(contextFact('Evidence identifiers', Array.isArray(evidenceIds) && evidenceIds.length ? evidenceIds.join(' · ') : 'Unknown', 'Only persisted or returned identifiers are shown.'));
  evidence.append(contextFact('Evidence hash', record.evidence_hash ?? 'Unknown'));
  evidence.append(contextFact('Observed / recorded', record.observed_at ?? record.captured_at ?? record.created_at ?? 'Unknown'));
  evidence.append(contextFact('Model version', record.model_version ?? radar?.model_version ?? 'Unknown'));

  const economics = $('shared-selection-economics'); economics.replaceChildren();
  const linkedAccounts = liveCapital.accounts.filter(account => context.chain ? account.account.chain === context.chain
    : context.asset ? account.unit === context.asset : context.venue ? account.label.toLowerCase().replaceAll(' ', '_') === context.venue.toLowerCase().replaceAll(' ', '_') : false);
  for (const account of linkedAccounts) {
    const contribution = liveCapital.canonical?.contributions?.find(row => row.id === account.id);
    economics.append(contextFact(`${account.label} · balance`, account.amount == null ? 'Unknown' : `${account.amount} ${account.unit}`, `${account.status} · source ${dateLabel(account.at == null ? null : new Date(account.at).toISOString())}`));
    const contributionState = contribution?.status === 'CACHED' ? 'CURRENT' : contribution?.status;
    economics.append(contextFact('Contribution to known subtotal', contribution?.included ? money(contribution.amount_usd) : 'Excluded', contributionState ?? 'Source not valued'));
  }
  const measurement = record.measurement ?? {};
  if (context.mission_id || record.measurement) {
    economics.append(contextFact('Mission-linked receipts', money(measurement.cash_receipts_usd), 'Reported receipts remain distinct from verified revenue.'));
    economics.append(contextFact('Verified revenue', 'Unknown', 'No exact verified-revenue attribution is established by this selection.'));
    economics.append(contextFact('Net contribution', money(measurement.full_net_economic_profit_usd), 'Full costs must be complete before net contribution is known.'));
    economics.append(contextFact('Live realized P&L', 'Unknown', 'No attributed live position result is present in this mission record.'));
  } else {
    economics.append(contextFact('Verified revenue', 'Unknown'));
    economics.append(contextFact('Verified costs', 'Unknown'));
    economics.append(contextFact('Net contribution', 'Unknown'));
    economics.append(contextFact('Live result', 'LIVE DATA UNAVAILABLE', 'No attributed live execution result is available for this selection.'));
  }
  if (['forecast', 'decision', 'market'].includes(context.type)) {
    economics.append(contextFact('Forecast probability', Number.isFinite(Number(record.probability_yes)) ? `${(Number(record.probability_yes) * 100).toFixed(1)}%` : 'Unknown', 'A probability is not a position or realized result.'));
    const outcome = rows('outcomes').find((item) => item.market_id === context.market_id
      && (!context.venue || item.venue === context.venue));
    economics.append(contextFact('Recorded market outcome', outcome ? String(outcome.outcome_yes) : 'Unknown', 'A market resolution is not by itself a settled paper position or realized trading P&L.'));
    economics.append(contextFact('Market-attributed live P&L', 'Unknown', 'No exact market-level live profit attribution has been reconciled.'));
  }

  const controls = $('shared-selection-controls'); controls.replaceChildren();
  controls.append(contextFact('Execution gateway', $('execution-gateway-state')?.textContent ?? 'Unknown'));
  controls.append(contextFact('Authority', record.capability_grants?.join(' · ') ?? 'No authority inferred from selection'));
  controls.append(contextFact('Limits', context.node?.limits?.join(' · ') ?? 'Live execution and signing remain separately gated.'));
  controls.append(contextFact('Action state', 'Inspection only', 'Selecting an object does not create or authorize an economic action.'));

  const missionId = context.mission_id;
  const marketId = context.market_id;
  for (const button of document.querySelectorAll('#world-event-list button')) {
    button.hidden = Boolean((missionId && button.dataset.missionId !== missionId)
      || (marketId && button.dataset.marketId !== marketId));
  }
  $('timeline-context').textContent = missionId ? `Mission ${missionId}`
    : marketId ? `${context.venue ?? 'Market'} · ${marketId}` : 'Following current state';
  renderWorkstationHistory(context);
  renderWorkstationStreams();
}

function renderWorkstationHistory(context) {
  const events = rows('mission_events');
  let linked = [];
  let basis = null;
  if (context.mission_id) { linked = events.filter((event) => event.mission_id === context.mission_id); basis = `mission ${context.mission_id}`; }
  else if (context.trial_id) { linked = events.filter((event) => event.payload?.trial_id === context.trial_id); basis = `trial ${context.trial_id}`; }
  else if (context.market_id) { linked = events.filter((event) => (event.payload?.market_id ?? event.payload?.source?.market_id) === context.market_id); basis = `market ${context.market_id}`; }
  else if (context.specialist) { linked = events.filter((event) => event.actor === context.specialist); basis = `agent ${context.specialist}`; }
  else if (context.type === 'core') { linked = events; basis = 'all persisted runtime events'; }
  $('trace-title').textContent = context.mission_id ? `Mission ${String(context.mission_id).slice(0, 12)} lineage` : 'Selection history';
  $('trace-limit').textContent = `${linked.length} event${linked.length === 1 ? '' : 's'} · ${basis ?? 'no exact relationship'}`;
  const trace = $('event-trace'); trace.replaceChildren();
  if (!linked.length) { trace.append(element('li', basis ? `No persisted event linked to ${basis}.` : 'No exact history relationship is recorded for this object.', 'quiet')); return; }
  for (const event of linked.slice(-20).reverse()) {
    const item = document.createElement('li');
    item.append(element('span', event.detail || event.event_type, 'event-copy'));
    item.append(element('span', [event.event_type, event.status, event.actor, shortTime(event.created_at)].filter(Boolean).join(' · '), 'event-meta'));
    trace.append(item);
  }
}

function selectSharedContext(context, syncGraph = true) {
  workstationContext = context ?? { type: 'core', id: 'agent:NOEMA', label: 'NOEMA', status: 'unknown' };
  liveDesk.setContext(workstationContext);
  economicCategories.select(workstationContext);
  renderWorkstationContext();
  if (syncGraph) noemaWorld?.selectEntity(workstationContext);
  if (workstationContext.type === 'account') $('capital-balance')?.scrollIntoView({ block: 'nearest' });
  if (workstationContext.chain) document.querySelector(`[data-account-id^="wallet:${CSS.escape(workstationContext.chain)}:"]`)
    ?.scrollIntoView({ block: 'nearest' });
  if (workstationContext.venue) document.querySelector(`[data-account-id="venue:${CSS.escape(workstationContext.venue)}"]`)
    ?.scrollIntoView({ block: 'nearest' });
  if (snapshot) renderEvidenceChart();
  if (economicsSnapshot) renderEconomy(economicsSnapshot);
}

function renderMarketQualification(payload = marketQualificationSnapshot) {
  const host = $('market-data-qualification');
  if (!host) return;
  host.replaceChildren();
  if (!payload) {
    host.append(element('p', 'Market-data qualification unavailable. No execution readiness is inferred.', 'workstation-empty'));
    return;
  }
  if (payload.status === 'unavailable') {
    const corpus = payload.historical_corpus ?? {};
    const count = value => Number.isInteger(value) && value >= 0
      ? new Intl.NumberFormat('en-US').format(value) : 'Unknown';
    host.append(element('p', 'Current market-data qualification unavailable. No execution readiness is inferred.', 'workstation-empty'));
    host.append(element('p', corpus.status === 'recorded'
      ? `Historical corpus · all persisted records: ${count(corpus.forecast_records)} forecasts · ${count(corpus.outcome_records)} outcomes. These totals are not eligible pairs.`
      : 'Historical corpus totals unavailable; no totals inferred.', 'qualification-history-context'));
    return;
  }
  const market = payload.market_data ?? {};
  const validation = payload.validation ?? {};
  const historicalCorpus = payload.historical_corpus ?? {};
  const historicalCount = value => Number.isInteger(value) && value >= 0
    ? new Intl.NumberFormat('en-US').format(value) : 'Unknown';
  const candidateVersion = validation.model_version ?? 'candidate model';
  const candidateCount = historicalCorpus.forecasts_by_model?.[candidateVersion] ?? 0;
  const candidateOutcomeCount = historicalCorpus.forecast_outcome_joins_by_model?.[candidateVersion] ?? 0;
  const baselineCount = historicalCorpus.forecasts_by_model?.['market-baseline-v1'] ?? 0;
  const baselineOutcomeCount = historicalCorpus.forecast_outcome_joins_by_model?.['market-baseline-v1'] ?? 0;
  const stage = payload.stage ?? 'insufficient';
  const label = {
    insufficient: 'INSUFFICIENT', collecting: 'COLLECTING',
    sufficient_for_research: 'SUFFICIENT FOR RESEARCH',
    sufficient_for_validation: 'SUFFICIENT FOR VALIDATION',
    execution_qualified: 'EXECUTION-QUALIFIED',
  }[stage] ?? 'INSUFFICIENT';
  const stages = [
    ['insufficient', 'INSUFFICIENT'], ['collecting', 'COLLECTING'],
    ['sufficient_for_research', 'SUFFICIENT FOR RESEARCH'],
    ['sufficient_for_validation', 'SUFFICIENT FOR VALIDATION'],
    ['execution_qualified', 'EXECUTION-QUALIFIED'],
  ];
  const activeIndex = stages.findIndex(([key]) => key === stage);
  const progression = element('ol', undefined, 'qualification-stages');
  for (const [index, [key, name]] of stages.entries()) {
    const item = element('li', name, index < activeIndex ? 'reached' : index === activeIndex ? 'current' : 'future');
    if (index === activeIndex) item.setAttribute('aria-current', 'step');
    progression.append(item);
  }
  const cards = element('div', undefined, 'qualification-statuses');
  const research = element('button', undefined, 'qualification-status');
  research.type = 'button'; research.setAttribute('aria-expanded', 'true'); research.dataset.kind = 'research';
  research.append(element('span', 'OBSERVED MARKET DATA'), element('strong', label));
  const execution = element('button', undefined, 'qualification-status execution');
  execution.type = 'button'; execution.setAttribute('aria-expanded', 'false'); execution.dataset.kind = 'execution';
  execution.append(element('span', 'EXECUTION EVIDENCE'), element('strong', 'NOT YET QUALIFIED'));
  cards.append(research, execution);

  const detail = element('div', undefined, 'qualification-detail');
  const researchDetail = element('div', undefined, 'qualification-explanation');
  researchDetail.dataset.detail = 'research';
  const recorded = payload.recorded_research_evidence;
  const recordedFull = Boolean(recorded && recorded.run_status === 'completed'
    && recorded.live_eligible === false && recorded.observations > 0
    && recorded.valid_markets === recorded.observations
    && recorded.markets_with_rules === recorded.observations
    && recorded.markets_with_two_sided_quotes === recorded.observations);
  researchDetail.append(element('p', payload.explanation ?? 'Research qualification reflects measured data quality only.'));
  const qualityChecks = element('div', undefined, 'qualification-checks');
  const check = (label, passed, detailText) => {
    const row = element('div', undefined, passed === true ? 'passed' : passed === false ? 'blocked' : 'unknown');
    row.append(element('strong', `${passed === true ? '✓' : passed === false ? '×' : '·'} ${label}`), element('span', detailText));
    qualityChecks.append(row);
  };
  check('Observation validation', recordedFull || (market.observations > 0 && market.valid_markets === market.observations), `${recorded?.valid_markets ?? market.valid_markets ?? 0}/${recorded?.observations ?? market.observations ?? 0} in latest qualification evidence`);
  check('Two-sided quote coverage', recordedFull || (market.observations > 0 && market.markets_with_two_sided_quotes === market.observations), `${recorded?.markets_with_two_sided_quotes ?? market.markets_with_two_sided_quotes ?? 0}/${recorded?.observations ?? market.observations ?? 0} qualified observations`);
  check('Resolution-rule coverage', recordedFull || (market.observations > 0 && market.markets_with_rules === market.observations), `${recorded?.markets_with_rules ?? market.markets_with_rules ?? 0}/${recorded?.observations ?? market.observations ?? 0} qualified observations`);
  const latest = (predictionVenuesSnapshot?.cross_venue_experiment_history ?? []).find(item => item?.experiment_evaluation)?.experiment_evaluation;
  check('Settlement equivalence', latest?.contract_equivalence?.status === 'verified', latest?.contract_equivalence?.status ?? 'not established for a current venue pair');
  const feeRows = Object.values(latest?.quotes ?? {});
  const feesKnown = feeRows.length > 0 && feeRows.every(item => item.fee_status === 'verified');
  check('Fee completeness', feesKnown, feesKnown ? 'verified for evaluated pair' : 'incomplete or unverified');
  check('Executable depth', latest?.depth?.status === 'one_contract_verified', latest?.depth?.status ?? 'partial or unavailable');
  check('Live authority', false, 'disabled · research qualification grants no execution permission');
  researchDetail.append(qualityChecks,
    element('p', `Current rolling pool: ${market.observations ?? 0} markets · ${String(market.current_collection ?? 'unknown').toUpperCase()} · feed freshness ${String(market.freshness ?? 'unknown').toUpperCase()}${market.age_seconds == null ? '' : ` · ${Math.floor(market.age_seconds)}s`}`),
    element('p', historicalCorpus.status === 'recorded'
      ? `Historical corpus · all persisted records: ${historicalCount(historicalCorpus.forecast_records)} forecasts · ${historicalCount(historicalCorpus.outcome_records)} outcomes. These totals are not eligible pairs.`
      : `Historical corpus totals · ${historicalCorpus.status === 'incomplete_schema' ? 'source tables incomplete' : 'unavailable'}; no total inferred.`),
    element('p', historicalCorpus.status === 'recorded'
      ? `Pair inputs · ${historicalCount(candidateCount)} ${candidateVersion} forecasts (${historicalCount(candidateOutcomeCount)} joined to outcomes) · ${historicalCount(baselineCount)} market-baseline forecasts (${historicalCount(baselineOutcomeCount)} joined).`
      : 'Pair inputs unavailable; no model counts inferred.'),
    element('p', `Eligible paired evaluation: ${validation.distinct_resolved_markets ?? 0} markets · ${validation.distinct_resolved_events ?? 0}/${validation.minimum_events ?? 100} independent resolved events · ${String(validation.status ?? 'unavailable').replaceAll('_', ' ')}`),
    element('small', 'Research readiness supports investigation only. It does not qualify a strategy or authorize live execution.'));
  if (recorded) researchDetail.append(element('small', `Recorded quality mission #${recorded.run_id} · ${recorded.created_at ?? 'time unavailable'} · ${recorded.observations ?? 0} observations · live eligible ${recorded.live_eligible === false ? 'NO' : 'UNKNOWN'}.`));

  const executionDetail = element('div', undefined, 'qualification-explanation');
  executionDetail.dataset.detail = 'execution'; executionDetail.hidden = true;
  const recent = predictionVenuesSnapshot?.cross_venue_experiment_history ?? [];
  const evaluated = recent.find(item => item?.experiment_evaluation)?.experiment_evaluation;
  const pairFreshness = evaluated?.freshness?.status ?? 'pair-specific freshness unknown';
  const freshness = `${String(market.freshness ?? 'unknown').toUpperCase()} rolling feed · ${pairFreshness}${market.age_seconds == null ? '' : ` · ${Math.floor(market.age_seconds)}s snapshot age`}`;
  const rules = evaluated?.contract_equivalence?.status ?? 'unverified · no current equivalence evidence';
  const fees = evaluated?.quotes
    ? Object.entries(evaluated.quotes).map(([venue, quote]) => `${venue} ${quote.fee_status ?? (quote.fee_source ? 'source recorded; verification unknown' : 'unknown')}`).join(' · ')
    : 'unknown · no qualifying evaluation';
  const depth = evaluated?.depth?.status ?? 'unknown · no qualifying evaluation';
  const blockers = [
    ['Freshness', freshness], ['Settlement equivalence', rules], ['Fees', fees],
    ['Executable depth', depth], ['Slippage', 'unmeasured'],
    ['Fill probability', 'unmeasured from live order/fill outcomes'],
    ['Capacity', evaluated?.depth?.capacity ?? 'unknown'],
    ['Live authority', 'not granted · execution remains fail-closed'],
  ];
  executionDetail.append(element('p', 'Data-quality qualification does not establish executable economics. Every strategy and venue pair must independently resolve these gates.'),
    ...blockers.map(([name, value]) => {
      const row = element('div', undefined, 'qualification-blocker');
      row.append(element('span', name), element('strong', String(value).replaceAll('_', ' ')));
      return row;
    }),
    element('small', 'A paper-test-eligible comparison is not live execution qualification or owner authority.'));
  detail.append(researchDetail, executionDetail);
  for (const card of [research, execution]) card.onclick = () => {
    const selected = card.dataset.kind;
    research.setAttribute('aria-expanded', String(selected === 'research'));
    execution.setAttribute('aria-expanded', String(selected === 'execution'));
    researchDetail.hidden = selected !== 'research';
    executionDetail.hidden = selected !== 'execution';
  };
  host.append(progression, cards, detail);
}

function renderWorkstationStreams() {
  const context = workstationContext;
  const marketScoped = Boolean(context.market_id);
  const matchesSelection = row => (!context.market_id || row.market_id === context.market_id)
    && (!context.venue || String(row.venue).toLowerCase().replaceAll(' ', '_') === String(context.venue).toLowerCase().replaceAll(' ', '_'))
    && (!context.asset || String(row.asset ?? row.underlying_asset ?? '').toUpperCase() === context.asset)
    && (!context.chain || row.chain === context.chain)
    && (!context.strategy_id || row.strategy_id === context.strategy_id || row.trial_id === context.strategy_id);
  const radarRows = Array.isArray(radarSnapshot) ? radarSnapshot : [];
  const staleRadarRows = radarRows.filter((row) => row.freshness_seconds != null && Number.isFinite(Number(row.freshness_seconds)) && Number(row.freshness_seconds) > 300).length;
  $('radar-pane-state').textContent = `${radarFreshness === 'stale' ? 'STALE · ' : ''}${radarFreshness.toUpperCase()} · ${radarRows.length} records${staleRadarRows ? ` · ${staleRadarRows} stale` : ''}`;
  const radarHost = $('workstation-radar-list'); radarHost.replaceChildren();
  const visibleRadar = radarRows.filter(matchesSelection);
  if (radarFreshness === 'unknown' || radarFreshness === 'unavailable') {
    radarHost.append(element('p', 'Opportunity state unavailable. No opportunity is inferred from an empty or failed radar.', 'workstation-empty'));
  } else if (!visibleRadar.length) {
    radarHost.append(element('p', marketScoped ? 'No exact market match is persisted in the radar.' : 'No recorded market opportunities are available.', 'workstation-empty'));
  }
  for (const row of visibleRadar.slice(0, 12)) {
    const recordStale = row.freshness_seconds != null && Number.isFinite(Number(row.freshness_seconds)) && Number(row.freshness_seconds) > 300;
    const button = element('button', undefined, 'workstation-stream-item'); button.type = 'button';
    button.dataset.marketId = row.market_id; button.dataset.status = recordStale ? 'stale' : row.decision ?? 'unknown';
    button.setAttribute('aria-pressed', String(context.market_id === row.market_id && context.venue === row.venue));
    button.append(element('strong', row.title || row.market_id));
    button.append(element('span', `${row.venue} · ${recordStale ? 'STALE · ' : ''}${row.decision} · p ${Number.isFinite(Number(row.probability_yes)) ? `${(Number(row.probability_yes) * 100).toFixed(1)}%` : 'unknown'}`));
    button.append(element('small', `robust edge ${Number.isFinite(Number(row.robust_edge)) ? `${(Number(row.robust_edge) * 100).toFixed(2)}pp` : 'unknown'} · cost ${Number.isFinite(Number(row.estimated_cost)) ? `${(Number(row.estimated_cost) * 100).toFixed(2)}pp` : 'unknown'} · ${Math.round(Number(row.freshness_seconds))}s old`));
    button.onclick = () => selectSharedContext(contextFromRecord('market', row, row.title || row.market_id));
    radarHost.append(button);
  }

  const decisions = rows('decisions');
  $('decision-pane-state').textContent = `${section('decisions').status.replaceAll('_', ' ')} · ${decisions.length} persisted`;
  const decisionHost = $('workstation-decision-list'); decisionHost.replaceChildren();
  const visibleDecisions = decisions.filter(matchesSelection);
  if (!visibleDecisions.length) decisionHost.append(element('p', marketScoped ? 'No exact forecast record is persisted for this market.' : 'No immutable forecasts are recorded.', 'workstation-empty'));
  for (const row of visibleDecisions.slice(0, 12)) {
    const button = element('button', undefined, 'workstation-stream-item'); button.type = 'button';
    button.dataset.marketId = row.market_id; button.dataset.status = row.decision ?? 'unknown';
    button.setAttribute('aria-pressed', String(context.market_id === row.market_id && context.venue === row.venue));
    button.append(element('strong', `${row.venue} · ${row.market_id}`));
    button.append(element('span', `${row.decision ?? 'UNKNOWN'} · probability ${Number.isFinite(Number(row.probability_yes)) ? `${(Number(row.probability_yes) * 100).toFixed(1)}%` : 'unknown'} · ${row.model_version ?? 'model unknown'}`));
    button.append(element('small', row.reason ?? 'Decision rationale unavailable'));
    button.onclick = () => selectSharedContext(contextFromRecord('decision', row, row.market_id));
    decisionHost.append(button);
  }

  const experimentRows = rows('experiments');
  $('experiment-pane-state').textContent = `${section('experiments').status.replaceAll('_', ' ')} · ${experimentRows.length} persisted`;
  const experimentHost = $('workstation-experiment-list'); experimentHost.className = 'workstation-experiment-list'; experimentHost.replaceChildren();
  let visibleExperiments = experimentRows;
  let linkBasis = null;
  const selectedTrial = context.trial_id ?? (context.mission_id ? rows('missions').find((item) => item.mission_id === context.mission_id)?.trial_id : null);
  if (selectedTrial) { visibleExperiments = experimentRows.filter((item) => item.trial_id === selectedTrial || item.parent_trial_id === selectedTrial); linkBasis = `Trial ${selectedTrial}`; }
  else if (context.market_id) { visibleExperiments = experimentRows.filter((item) => item.market_id === context.market_id); linkBasis = 'exact market identifier'; }
  else if (context.specialist) { visibleExperiments = experimentRows.filter((item) => item.specialist === context.specialist); linkBasis = 'exact specialist identifier'; }
  else if (context.asset || context.chain || context.venue) { visibleExperiments = experimentRows.filter(matchesSelection); linkBasis = 'exact selected asset / venue'; }
  if (!visibleExperiments.length) experimentHost.append(element('p', linkBasis ? `No experiment link persisted by ${linkBasis}.` : 'No research experiments are recorded.', 'workstation-empty'));
  for (const row of visibleExperiments.slice(0, 12)) {
    const button = element('button', undefined, 'workstation-stream-item'); button.type = 'button';
    button.dataset.trialId = row.trial_id; button.dataset.status = row.status ?? 'unknown';
    button.append(element('strong', row.hypothesis ?? row.trial_id));
    button.append(element('span', `${row.family ?? 'family unknown'} · ${row.status ?? 'status unknown'} · ${row.trial_id}`));
    button.onclick = () => selectSharedContext(contextFromRecord('experiment', row, row.hypothesis ?? row.trial_id));
    experimentHost.append(button);
  }
}

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
  selectSharedContext(contextFromRecord(view === 'decisions' ? 'decision' : view === 'experiments' ? 'experiment' : view, row));
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
  const context = workstationContext;
  let scoped = all;
  if (context.mission_id) scoped = all.filter((run) => run.mission_id === context.mission_id);
  else if (context.trial_id) scoped = all.filter((run) => run.trial_id === context.trial_id);
  else if (context.specialist) scoped = all.filter((run) => run.specialist === context.specialist);
  else if (context.market_id) scoped = all.filter((run) => run.market_id === context.market_id
    && (!context.venue || run.venue === context.venue));
  const runs = scoped.slice(0, 10).reverse();
  const latest = scoped[0] ? runResult(scoped[0]) : null;
  $('evidence-total').textContent = latest ? formatCount(latest.observations) : '—';
  $('evidence-run-count').textContent = scoped.length ? `${scoped.length} RUN${scoped.length === 1 ? '' : 'S'} · EVIDENCE ONLY` : context.market_id || context.mission_id || context.trial_id || context.specialist ? 'NO EXACT LINKED RUNS' : 'NO RECORDED RUNS';
  $('chart-range').textContent = runs.length
    ? `${shortTime(runs[0].created_at)} — ${shortTime(runs[runs.length - 1].created_at)} · local time`
    : context.market_id || context.mission_id || context.trial_id || context.specialist ? 'No exact linked experiment history' : 'No experiment history';
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
    const inspectRun = () => { if (mission) { focusMissionId = mission.mission_id; selectSharedContext(contextFromRecord('mission', mission, mission.objective ?? mission.mission_id)); renderWorld(); } };
    g.addEventListener('click', inspectRun);
    g.addEventListener('keydown', (event) => { if ((event.key === 'Enter' || event.key === ' ') && mission) { event.preventDefault(); inspectRun(); } });
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
  const historical = element('details', undefined, 'historical-validation');
  historical.append(element('summary', 'Research / Validation · historical simulation'), maturationCard);
  root.append(historical);
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
  if (lifecycleHistory.length) {
    const counts = new Map();
    for (const candidate of lifecycleHistory) {
      const verdict = candidate.experiment_evaluation?.verdict ?? 'evidence unavailable';
      counts.set(verdict, (counts.get(verdict) ?? 0) + 1);
    }
    const summary = [...counts].map(([verdict, count]) => `${count} ${verdict}`).join(' · ');
    const rawHistory = element('details', undefined, 'historical-validation');
    rawHistory.append(element('summary', `Research evidence summary · ${lifecycleHistory.length} evaluations · ${summary}`));
    const rawRows = element('div', undefined, 'qualification-raw-history');
    for (const candidate of lifecycleHistory) {
      const card = element('article', undefined, 'prediction-overlap-candidate');
      card.append(...renderLifecycle(candidate));
      rawRows.append(card);
    }
    rawHistory.append(rawRows);
    root.append(rawHistory);
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
  const ledger = measurement?.canonical_ledger ?? {};
  const missionProgress = measurement?.mission_progress ?? {};
  $('mission-economics-progress').textContent = `SELF-FUNDING ${String(missionProgress.status ?? 'unknown').toUpperCase()} · verified realized revenue ${money(missionProgress.verified_realized_revenue_usd)} · complete attributable costs ${money(missionProgress.complete_attributable_costs_usd)} · reserves ${money(missionProgress.reserve_balance_usd)}`;
  $('economics-time').textContent = measurement?.as_of ? `Accounting snapshot · ${dateLabel(measurement.as_of)}` : 'Economic measurement unavailable';
  renderCanonicalEconomy(ledger);

  const rows = snapshot?.economic_state?.balances;
  $('treasury-reserve').textContent = rows ? money(rows.reserve_usd) : 'Unknown';
  $('treasury-strategy').textContent = rows ? money(rows.strategy_capital_usd) : 'Unknown';
  $('treasury-research').textContent = rows ? money(rows.research_budget_usd) : 'Unknown';
  $('treasury-infrastructure').textContent = rows ? money(rows.infrastructure_budget_usd) : 'Unknown';

}

function renderCanonicalEconomy(ledger) {
  const grid = $('canonical-economy-grid');
  grid.replaceChildren();
  const hasEvents = Number(ledger.event_count ?? 0) > 0;
  const metrics = [
    ['Verified revenue total', ledger.verified_realized_revenue_usd],
    ['Verified attributable costs total', ledger.verified_attributable_costs_usd],
    ['Known unreconciled USD', hasEvents ? ledger.unreconciled_amount_usd : null],
    ['Owner capital total', ledger.owner_funded_amount_usd],
    ['Reconciled owner capital subtotal', ledger.owner_funded_subtotal_usd],
    ['Operating reserve', ledger.operating_reserve_usd],
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
    const symbol = network.native_symbol ?? (network.chain === 'bitcoin' ? 'BTC' : network.chain === 'solana' ? 'SOL' : 'ETH');
    card.append(element('strong', native === undefined ? 'Balance unknown' : `${native} ${symbol}`, 'wallet-network-balance'));
    const detail = element('div', undefined, 'wallet-network-detail');
    detail.append(element('span', network.chain_id ? `CHAIN ${network.chain_id}` : 'MAINNET'));
    if (network.rpc_health) detail.append(element('span', `RPC ${network.rpc_health.toUpperCase()}`));
    if (network.latest_block_number != null) {
      const age = Number(network.block_age_seconds);
      detail.append(element('span', `BLOCK ${network.latest_block_number} · ${Number.isFinite(age) ? `${Math.round(age)}s` : 'age unknown'}`));
    }
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
    const idleDetail = allocation
      ? allocation.attention_fraction === 0
        ? `Idle · not selected in latest allocation · ${allocation.reason ?? 'zero research attention recorded'}`
        : `Idle · ${allocation.reason ?? 'no active run assigned'}`
      : 'Idle · no specialist allocation reason recorded';
    copy.append(element('p', current ? `Working · ${current.kind}` : idleDetail, 'specialist-family'));
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
    button.onclick = () => {
      focusMissionId = mission.mission_id;
      selectSharedContext(contextFromRecord('mission', mission, mission.objective ?? mission.mission_id));
      noemaWorld?.selectEntity({ mission_id: mission.mission_id });
      renderWorld();
    };
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
  economicCategories.update({ snapshot, venues: predictionVenuesSnapshot, wallets: walletSnapshot,
    economics: economicsSnapshot, accounts: liveCapital.accounts, canonical: liveCapital.canonical, history: liveCapital.history, stripe: stripeEconomySnapshot, gateway: gatewaySnapshot, freshness: capabilityFreshness });
  noemaWorld.update(snapshot, { providers: providerHealth, venues: predictionVenuesSnapshot, wallets: walletSnapshot, radar: radarSnapshot,
    gateway: gatewaySnapshot, freshness: capabilityFreshness });
  const runtime = snapshot.runtime ?? {};
  const cycle = runtime.cycle ?? {};
  const resources = snapshot.resources ?? {};
  const sessions = rows('sessions');
  const mission = currentMission();
  renderActiveNow(snapshot, mission);
  const run = rows('research_runs').find((item) => item.status === 'running');
  const activity = rows('activity');
  const latestEvent = mission ? rows('mission_events').filter((item) => item.mission_id === mission.mission_id).sort((a,b) => String(b.created_at).localeCompare(String(a.created_at)))[0] : null;
  const hasActiveWork = Boolean(run || rows('missions').some((item) => ['running', 'claimed', 'waiting'].includes(item.status)));
  const partial = runtime.state === 'running' && cycle.health && cycle.health !== 'healthy';
  const idle = runtime.state === 'running' && !hasActiveWork;
  const liveness = String(runtime.liveness ?? runtime.state ?? 'unknown').replaceAll('_', ' ').toUpperCase();
  const stateText = resources.state === 'resource_limited' ? 'RESOURCE LIMITED'
    : runtime.state === 'running' ? partial ? 'LIVE · PARTIAL' : idle ? 'LIVE · IDLE' : 'LIVE' : liveness;
  const consoleState = $('console-freshness').textContent || 'UNKNOWN';
  $('runtime').textContent = `NOEMA WORKER ${stateText} · CONSOLE ${consoleState} · cycle health ${cycle.health ?? runtime.health ?? 'unknown'}`;
  $('runtime-ribbon').dataset.state = resources.state === 'resource_limited' || (runtime.state !== 'running' && consoleState === 'CONNECTED') ? 'partial' : runtime.state;
  $('runtime-indicator').dataset.state = runtime.state ?? 'unknown';
  $('updated').textContent = snapshot.as_of ? dateLabel(snapshot.as_of) : 'Snapshot unavailable';
  renderSourceFreshness();
  $('lead-state').textContent = resources.state === 'resource_limited' ? 'RESOURCE LIMITED'
    : runtime.state === 'running' ? partial ? 'LIVE · PARTIAL' : idle ? 'LIVE · IDLE' : 'LIVE' : liveness;
  $('lead-state').dataset.tone = resources.state === 'resource_limited' || partial
    || ['STALE', 'HEARTBEAT STUCK', 'PROCESS MISSING', 'PID MISMATCH', 'WRONG CHECKOUT', 'WRONG DATABASE'].includes(liveness)
    ? 'warning' : runtime.state === 'running' ? 'running' : consoleState === 'CONNECTED' ? 'warning' : 'bad';
  $('noema-lead').dataset.status = String(runtime.state ?? 'unknown');
  $('lead-cycle').textContent = cycle.cycle_id ? `CYCLE ${cycle.cycle_id} · ${cycle.active_goal ?? 'no active goal'}` : 'No cycle recorded';
  const session = sessions[0] ?? null;
  $('lead-cognition').textContent = session?.provider
    ? `Last route · ${session.provider} · ${session.model ?? 'model unknown'}` : 'Cognition route unrecorded';
  const processIdentity = runtime.process_identity ?? {};
  const processLabel = processIdentity.pid
    ? `PID ${processIdentity.pid} · commit ${String(processIdentity.commit ?? 'unknown').slice(0, 12)}${processIdentity.working_tree_dirty ? ' · dirty checkout' : ''}`
    : 'process identity unavailable';
  $('core-heartbeat').textContent = `${liveness} · ${runtime.last_heartbeat_at ? `heartbeat ${dateLabel(runtime.last_heartbeat_at)}` : 'heartbeat unavailable'} · ${processLabel}`;
  $('core-heartbeat').title = processIdentity.pid
    ? `Started ${processIdentity.started_at ?? 'unknown'} · checkout ${processIdentity.checkout_path ?? 'unknown'} · database ${processIdentity.database_path ?? 'unknown'}`
    : 'A persisted heartbeat without a recorded process identity cannot prove runtime liveness.';
  const runtimeDetail = $('runtime-detail');
  runtimeDetail.replaceChildren();
  const hasSeconds = value => value !== null && value !== undefined && Number.isFinite(Number(value));
  const timingRows = [
    ['Cycle', cycle.cycle_id ?? 'Unavailable'],
    ['Cycle duration', hasSeconds(cycle.duration_seconds) ? `${Number(cycle.duration_seconds).toFixed(1)}s` : 'Unavailable'],
    ['Configured cadence', hasSeconds(cycle.cadence_seconds) ? `${Number(cycle.cadence_seconds).toFixed(1)}s` : 'Unavailable'],
  ];
  if (hasSeconds(cycle.duration_seconds) && hasSeconds(cycle.cadence_seconds)) {
    const overrun = Number(cycle.duration_seconds) - Number(cycle.cadence_seconds);
    timingRows.push(['Cadence status', overrun > 0 ? `OVERRUN · ${overrun.toFixed(1)}s` : 'WITHIN CADENCE']);
  }
  const stages = Object.entries(cycle.stage_timings ?? {}).filter(([, seconds]) => hasSeconds(seconds) && Number(seconds) >= 0)
    .sort((a, b) => Number(b[1]) - Number(a[1])).slice(0, 6);
  for (const [stage, seconds] of stages) timingRows.push([`Stage · ${stage.replaceAll('_', ' ')}`, `${Number(seconds).toFixed(2)}s`]);
  for (const [label, value] of timingRows) runtimeDetail.append(element('dt', label), element('dd', String(value)));

  const focus = mission;
  focusMissionId = focus?.mission_id ?? focusMissionId;
  const gatewayStatus = gatewaySnapshot?.status ?? gatewaySnapshot?.gateway_state ?? capabilityFreshness.gateway;
  const budgetStatus = economicsSnapshot?.operating_estimates?.model_budget_status
    ?? (economicsSnapshot?.operating_estimates?.model_reserved_usd == null ? 'Unknown' : `Reserved ${money(economicsSnapshot.operating_estimates.model_reserved_usd)}`);
  updateWorkstationTelemetry({
    state: `Worker ${stateText} · Console ${consoleState} · health ${cycle.health ?? runtime.health ?? 'unknown'}${capabilityFreshness.operations === 'stale' ? ' · snapshot stale' : ''}`,
    cognition: session?.provider ? `${session.provider} · ${session.model ?? 'model unknown'}` : 'Route unrecorded',
    mission: focus ? `${focus.status} · ${focus.specialist ?? 'specialist unknown'}` : 'No mission recorded',
    authority: gatewayStatus ? String(gatewayStatus).replaceAll('_', ' ') : 'Unknown',
    budget: budgetStatus,
    alert: runtime.state === 'stale' ? 'Runtime reports stale state' : null,
  });
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
  const clawState = openclaw.state ?? 'unknown';
  const clawStatus = (clawState === 'idle' || (clawState === 'stopped' && openclaw.enabled && openclaw.reason === 'Gateway starts only for admitted NOEMA work')) ? 'READY · awaiting eligible mission'
    : clawState === 'working' ? `RUNNING · ${openclaw.current_task ?? openclaw.last_mission?.mission_id ?? 'OpenClaw mission'}`
      : clawState === 'queued' ? `QUEUED · ${openclaw.current_task ?? openclaw.last_mission?.mission_id ?? 'OpenClaw mission'}`
        : clawState === 'disabled' ? `DISABLED · ${openclaw.reason ?? 'configuration disabled'}`
          : clawState === 'resource_limited' ? `BLOCKED · ${openclaw.reason ?? 'resource cap'}`
            : `BLOCKED · ${openclaw.reason ?? 'runtime unavailable'}`;
  const workerDetail = [
    openclaw.elapsed_seconds == null ? null : `${openclaw.elapsed_seconds}s`,
    openclaw.resource_usage?.model_cost_usd == null ? null : `$${Number(openclaw.resource_usage.model_cost_usd).toFixed(4)}`,
    openclaw.latest_result?.status ? `result ${openclaw.latest_result.status}` : null,
    openclaw.last_mission?.mission_id ? `last ${openclaw.last_mission.mission_id}` : null,
    openclaw.latest_evidence ? `evidence ${String(openclaw.latest_evidence).slice(0, 10)}` : null,
    openclaw.reason_idle ? `idle: ${openclaw.reason_idle}` : null,
  ].filter(Boolean).join(' · ');
  $('support-worker').textContent = `OpenClaw · ${clawStatus}`;
  $('support-memory').textContent = `${workerDetail || 'No prior OpenClaw mission'} · Caps · Docker ${resources.limits?.docker_workers ?? 'unknown'} · model ${resources.limits?.large_local_models ?? 'unknown'} · experiments ${resources.limits?.experiments ?? 'unknown'}`;
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
  renderWorkstationHistory(workstationContext);
  renderEconomy(economicsSnapshot);
  renderWorkstationContext();
}

function renderActiveNow(data, selectedMission) {
  const runtime = data.runtime ?? {};
  const cycle = runtime.cycle ?? {};
  const missions = rows('missions');
  const activeMission = missions.find(item => ['running', 'claimed', 'waiting'].includes(item.status));
  const activeRun = rows('research_runs').find(item => ['running', 'claimed'].includes(item.status));
  const mission = activeMission ?? activeRun;
  const specialist = mission?.specialist ?? activeMission?.specialist ?? 'None recorded';
  const currentMarket = radarFreshness === 'current'
    ? (radarSnapshot ?? []).find(item => Number(item.freshness_seconds) <= 300)
    : null;
  const cognition = data.primary_cognition ?? {};
  const latestDecision = rows('decisions')[0];
  const cycleResearchReason = /(?:^|; )research_reason=([^;]+)/.exec(String(cycle.note ?? ''))?.[1];
  const ledgerChanges = rows('economic_events').filter(item =>
    (item.capital_class === 'owner_capital' && String(item.reconciliation_state).toUpperCase() === 'RECONCILED')
    || (item.capital_class === 'trading_pnl' && item.value_state === 'realized'
      && String(item.reconciliation_state).toUpperCase() === 'RECONCILED'));
  const latestLedgerChange = ledgerChanges.sort((a, b) =>
    (eventTime(b.occurred_at ?? b.created_at) ?? 0) - (eventTime(a.occurred_at ?? a.created_at) ?? 0))[0];
  const idleReason = data.resources?.state === 'resource_limited'
    ? `Resource limited · ${data.resources.detail ?? 'capacity unavailable'}`
    : activeMission?.status === 'waiting' ? 'Mission waiting · see persisted mission state'
      : activeMission?.status === 'queued' ? 'Mission queued · awaiting an eligible research slot'
        : activeMission ? 'No blocker recorded for active mission'
          : cycleResearchReason
            ? `No eligible research slot · ${cycleResearchReason}`
            : data.openclaw_worker?.reason_idle ?? 'No current opportunity has earned an eligible mission';

  $('active-cycle').textContent = cycle.cycle_id
    ? `${cycle.cycle_id} · ${cycle.active_goal ?? 'goal unavailable'}` : 'No active cycle recorded';
  $('active-mission').textContent = mission
    ? `${mission.status} · ${mission.objective ?? mission.kind ?? mission.mission_id}`
    : selectedMission ? `Idle · last mission ${selectedMission.status}` : 'Idle · no mission recorded';
  $('active-specialist').textContent = mission ? `${specialist} · ${mission.status}` : 'No active specialist';
  $('active-market').textContent = currentMarket
    ? `${currentMarket.title ?? currentMarket.market_id} · ${currentMarket.venue} · ${Math.round(Number(currentMarket.freshness_seconds))}s old`
    : 'No fresh radar market observed';
  $('active-cognition').textContent = cognition.session_id
    ? `${cognition.status === 'running' ? 'RUNNING' : cognition.status === 'failed' ? 'FAILED' : 'IDLE · last route'} · ${cognition.provider ?? 'provider unknown'} · ${cognition.model ?? 'model unknown'}`
    : 'IDLE · no active cognition session recorded';
  $('active-decision').textContent = latestDecision
    ? `${latestDecision.decision ?? 'Unknown'} · ${latestDecision.market_id ?? 'market unknown'} · ${dateLabel(latestDecision.created_at)}`
    : 'No persisted forecast decision';
  $('active-balance-change').textContent = latestLedgerChange
    ? `${latestLedgerChange.event_type} · ${latestLedgerChange.amount_usd ?? 'amount unknown'} USD · ${dateLabel(latestLedgerChange.occurred_at ?? latestLedgerChange.created_at)}`
    : 'No reconciled balance-change event recorded';
  $('active-blocker').textContent = idleReason;
}

function renderSourceFreshness() {
  const age = snapshot?.as_of ? Date.now() - Date.parse(snapshot.as_of) : null;
  const seconds = Number.isFinite(age) && age >= 0 ? Math.floor(age / 1000) : null;
  const workerState = capabilityFreshness.operations === 'stale' || snapshot?.runtime?.state === 'stale' ? 'STALE'
    : capabilityFreshness.operations === 'current' ? String(snapshot?.runtime?.state ?? 'UNKNOWN').toUpperCase() : 'UNKNOWN';
  $('worker-freshness').textContent = workerState;
  $('worker-freshness').dataset.state = workerState === 'STALE' ? 'stale' : workerState === 'RUNNING' ? 'live' : 'unknown';
  $('worker-snapshot-age').textContent = seconds === null ? 'Unknown' : seconds < 60 ? `${seconds}s ago` : `${Math.floor(seconds / 60)}m ago`;
  const feeds = (predictionVenuesSnapshot?.venues ?? []).map(row => row.market_data?.status);
  const degradedFeeds = feeds.filter(status => status !== 'connected').length;
  $('market-freshness').textContent = radarFreshness === 'unknown' || radarFreshness === 'unavailable' ? 'UNKNOWN'
    : !feeds.length ? 'DEGRADED · venue status unavailable' : degradedFeeds ? `DEGRADED · ${degradedFeeds}/${feeds.length} venue feeds` : `LIVE · ${feeds.length} venue feeds`;
  $('market-freshness').dataset.state = radarFreshness !== 'current' || degradedFeeds ? 'stale' : 'live';
  const canonical = liveCapital.canonical;
  const accountFailures = liveCapital.failedSources.filter(source => ['wallets', 'venues', 'history'].includes(source));
  $('account-freshness').textContent = !canonical ? 'UNKNOWN'
    : `${accountFailures.length ? 'STALE · previous observation retained' : canonical.status} · ${canonical.valued_sources}/${canonical.expected_sources} valued${canonical.stale_sources ? ` · ${canonical.stale_sources} stale` : ''}`;
  $('account-freshness').dataset.state = accountFailures.length || canonical?.stale_sources ? 'stale' : canonical?.valued_sources ? 'connected' : 'unknown';
  const stream = $('sync-state').textContent;
  $('console-freshness').textContent = stream.replace('STREAM ', '').replace('POLLING · ', '').replace('DISPLAY ', '').toUpperCase();
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
function renderFetchErrors() {
  const errors = [...(operatingError ? [operatingError] : []), ...supplementalErrors];
  $('error').hidden = errors.length === 0;
  $('error').textContent = errors.join(' ');
  if (errors.length) {
    $('telemetry-alert').hidden = false;
    $('telemetry-alert-text').textContent = errors.join(' ');
  }
}

async function refreshOperatingRecords() {
  if (liveDesk.paused) { liveDesk.markPending(); return; }
  if (operatingRequest) { operatingQueued = true; return operatingRequest; }
  const generation = displayGeneration;
  operatingRequest = (async () => {
    try {
      const data = await get('/api/operations');
      if (liveDesk.paused || generation !== displayGeneration) { liveDesk.markPending(); return; }
      snapshot = data;
      capabilityFreshness.operations = 'current';
      capabilityFreshness.operationsAt = data.as_of ?? new Date().toISOString();
      operatingError = null;
      if (selected) {
        const fresh = section(selected.view).rows.find((row) => keyOf(selected.view, row) === keyOf(selected.view, selected.row));
        if (fresh) selected = { ...selected, row: fresh };
      }
      liveDesk.update(data);
      renderLiveChange();
      renderWorld();
      renderWorkstationStreams();
      if ($('deep-records').open) { renderArchive(); renderDetail(); }
      renderFetchErrors();
    } catch {
      if (liveDesk.paused || generation !== displayGeneration) { liveDesk.markPending(); return; }
      capabilityFreshness.operations = snapshot ? 'stale' : 'unavailable';
      liveDesk.markStale();
      operatingError = 'Live operating state unavailable; showing the last successful snapshot.';
      renderWorld();
      renderFetchErrors();
    } finally {
      operatingRequest = null;
      if (operatingQueued && !liveDesk.paused) { operatingQueued = false; scheduleLiveRefresh(); }
    }
  })();
  return operatingRequest;
}

async function refreshBalanceHistory() {
  if (liveDesk.paused) return;
  if (balanceHistoryRequest) { balanceHistoryQueued = true; return balanceHistoryRequest; }
  const generation = displayGeneration, window = liveCapital.window;
  balanceHistoryRequest = get(`/api/capital-history?window=${window}`).then(payload => {
    if (liveDesk.paused || generation !== displayGeneration || window !== liveCapital.window) return;
    liveDesk.updateSources({ balanceHistory: payload });
    liveCapital.update('history', payload);
    economicCategories.update({ snapshot, venues: predictionVenuesSnapshot, wallets: walletSnapshot, economics: economicsSnapshot, accounts: liveCapital.accounts, canonical: payload.current, history: payload, stripe: stripeEconomySnapshot, gateway: gatewaySnapshot, freshness: capabilityFreshness });
  }).catch(() => { if (!liveDesk.paused && generation === displayGeneration) liveCapital.fail('history'); })
    .finally(() => {
      balanceHistoryRequest = null;
      if (balanceHistoryQueued && !liveDesk.paused) { balanceHistoryQueued = false; refreshBalanceHistory(); }
    });
  return balanceHistoryRequest;
}

function refreshGateway() {
  if (liveDesk.paused) return Promise.resolve();
  if (gatewayRequest) return gatewayRequest;
  const generation = displayGeneration;
  gatewayRequest = get('/api/execution-gateway').then(payload => {
    if (liveDesk.paused || generation !== displayGeneration) { liveDesk.markPending(); return payload; }
    gatewaySnapshot = payload;
    capabilityFreshness.gateway = 'current';
    capabilityFreshness.gatewayAt = new Date().toISOString();
    liveCapital.update('gateway', payload);
    renderExecutionGateway(payload);
    liveDesk.updateSources({ venues: predictionVenuesSnapshot, gateway: gatewaySnapshot });
    return payload;
  }).catch(error => {
    if (!liveDesk.paused && generation === displayGeneration) {
      liveCapital.fail('gateway');
      capabilityFreshness.gateway = gatewaySnapshot ? 'stale' : 'unavailable';
      $('execution-gateway-state').textContent = 'GATEWAY STATE UNAVAILABLE';
    }
    throw error;
  }).finally(() => { gatewayRequest = null; });
  return gatewayRequest;
}

// Capital readers settle independently, so a slow research/provider check cannot
// hold a successful balance or ledger update off-screen.
function refreshCapital(force = false) {
  if (liveDesk.paused) return Promise.resolve();
  const generation = displayGeneration;
  const sources = [
    ['economics', '/api/economic-measurement', 1000],
    ['wallets', '/api/wallet-status', 15000],
    ['venues', '/api/prediction-venues', 15000],
  ];
  return Promise.allSettled(sources.map(([source, path, interval]) => {
    if (capitalRequests.has(source)) return capitalRequests.get(source);
    if (!force && Date.now() - (capitalCheckedAt.get(source) ?? 0) < interval) return Promise.resolve();
    capitalCheckedAt.set(source, Date.now());
    const current = () => !liveDesk.paused && generation === displayGeneration;
    const request = get(path).then((payload) => {
      if (!current()) { liveDesk.markPending(); return; }
      if (source === 'economics') { economicsSnapshot = payload; renderEconomy(payload); }
      if (source === 'wallets') {
        walletSnapshot = payload;
        capabilityFreshness.wallets = 'current';
        capabilityFreshness.walletsAt = payload.observed_at ?? null;
        renderWalletState(payload);
        liveDesk.updateSources({ wallets: payload });
      }
      if (source === 'venues') {
        predictionVenuesSnapshot = payload;
        capabilityFreshness.venues = 'current';
        capabilityFreshness.venuesAt = payload.as_of ?? null;
        renderPredictionVenues(payload);
      }
      liveCapital.update(source, payload);
      if (source === 'wallets' || source === 'venues') refreshBalanceHistory();
      if (source === 'venues') liveDesk.updateSources({ venues: predictionVenuesSnapshot, gateway: gatewaySnapshot });
      renderWorld();
    }).catch(() => {
      if (!current()) return;
      liveCapital.fail(source);
      if (source === 'wallets') {
        capabilityFreshness.wallets = walletSnapshot ? 'stale' : 'unavailable';
        $('wallets-asof').textContent = 'WALLET STATE UNAVAILABLE · LAST OBSERVATION RETAINED';
      }
      if (source === 'venues') {
        capabilityFreshness.venues = predictionVenuesSnapshot ? 'stale' : 'unavailable';
        renderPredictionVenues(predictionVenuesSnapshot);
      }
      renderWorld();
    }).finally(() => {
      capitalRequests.delete(source);
      if (generation !== displayGeneration && !liveDesk.paused) {
        capitalCheckedAt.delete(source);
        refreshCapital();
      }
    });
    capitalRequests.set(source, request);
    return request;
  }));
}

async function refresh(force = false) {

  if (liveDesk.paused) { liveDesk.markPending(); return; }
  // Commit notifications refresh this independently, including while providers are slow.
  const records = refreshOperatingRecords();
  const capital = refreshCapital(force);
  const gatewayState = refreshGateway();
  if (refreshing) { refreshQueued = true; return; }
  const generation = displayGeneration;
  refreshing = true;
  $('refresh').disabled = true;
  try {
  const checkProviders = Date.now() - providerHealthAt >= 300000;
  const [, , gateway, trench, stripe, knowledge, providers, radar, qualification] = await Promise.allSettled([
    records, capital, gatewayState, get('/api/trench'), get('/api/stripe-economy'), get('/api/knowledge'),
    checkProviders ? get('/api/provider-health') : Promise.resolve(providerHealth),
    get('/api/radar'), get('/api/market-data-qualification'),
  ]);
  if (liveDesk.paused || generation !== displayGeneration) { liveDesk.markPending(); return; }
  const errors = [];
  if (gateway.status === 'fulfilled') {
    // refreshGateway already published its state as soon as the local ledger read settled.
  } else {
    errors.push('Execution gateway history unavailable.');
  }
  if (trench.status === 'fulfilled') trenchSnapshot = trench.value;
  renderWeb3Evidence(trenchSnapshot);
  if (stripe.status === 'fulfilled') stripeEconomySnapshot = stripe.value;
  renderStripeEconomy(stripeEconomySnapshot);
  if (knowledge.status === 'fulfilled') knowledgeSnapshot = knowledge.value;
  renderKnowledge(knowledgeSnapshot);
  if (radar.status === 'fulfilled' && Array.isArray(radar.value)) {
    radarSnapshot = radar.value;
    radarFreshness = 'current';
  } else {
    radarFreshness = radarSnapshot ? 'stale' : 'unavailable';
    if (radar.status === 'rejected') errors.push('Opportunity radar unavailable; showing the last successful snapshot.');
  }
  if (qualification.status === 'fulfilled') {
    marketQualificationSnapshot = qualification.value;
  } else if (marketQualificationSnapshot) {
    marketQualificationSnapshot = { ...marketQualificationSnapshot, market_data: {
      ...marketQualificationSnapshot.market_data, freshness: 'stale',
    } };
  } else {
    marketQualificationSnapshot = { status: 'unavailable' };
  }
  renderMarketQualification();
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
  renderWorld();
  renderWorkstationStreams();
  supplementalErrors = errors;
  renderFetchErrors();
  } finally {
    $('refresh').disabled = false;
    refreshing = false;
    if (refreshQueued && !liveDesk.paused) {
      refreshQueued = false;
      refresh();
    }
  }
}

function scheduleLiveRefresh() {
  if (liveDesk.paused) { liveDesk.markPending(); return; }
  if (streamRefreshTimer) return;
  const delay = Math.max(0, 1000 - (Date.now() - lastStreamRefresh));
  streamRefreshTimer = setTimeout(() => {
    streamRefreshTimer = null;
    lastStreamRefresh = Date.now();
    refreshOperatingRecords();
    // The server-side venue and wallet caches bound source polling. Force the
    // projection reads so a committed observation reaches this page immediately.
    refreshCapital(true);
  }, delay);
}

const runtimeStream = new EventSource('/api/runtime-stream');
runtimeStream.addEventListener('ready', () => {
  liveDesk.setTransport('connected');
});
runtimeStream.addEventListener('change', () => {
  if (document.hidden) return;
  scheduleLiveRefresh();
});
runtimeStream.addEventListener('unavailable', () => {
  liveDesk.setTransport('unavailable');
});
runtimeStream.onerror = () => {
  liveDesk.setTransport('reconnecting');
};
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh();
});

$('search').oninput = renderArchive;
$('refresh').onclick = () => refresh(true);
$('back').onclick = () => { selected = history.pop(); if (selected) activeView = selected.view; renderArchive(); renderDetail(); };
renderArchive();
refresh();
refreshBalanceHistory();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
setInterval(() => { if (!document.hidden) renderSourceFreshness(); }, 1000);
