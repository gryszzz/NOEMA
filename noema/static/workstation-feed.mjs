// Read-only projections of recorded events. No inferred trades, progress, or revenue.
export function eventTime(value) {
  if (typeof value !== 'string' || !value.trim()) return null;
  const utc = /(?:Z|[+-]\d\d:\d\d)$/i.test(value) ? value : `${value.replace(' ', 'T')}Z`;
  const time = Date.parse(utc);
  return Number.isFinite(time) ? time : null;
}

const words = (value) => String(value ?? 'Unknown').replaceAll('_', ' ');
const tone = (status) => /fail|error|reject|degrad|stale|quarantin|unavailable/i.test(status) ? 'attention'
  : /complete|passed|accepted|resolved/i.test(status) ? 'complete' : 'neutral';

const FILTER_PRIORITY = ['balance', 'wallet', 'fill', 'execution', 'agent', 'cognition', 'error', 'system', 'market', 'decision', 'economics', 'pnl'];
export const CANONICAL_EVENT_FILTERS = Object.freeze({
  wallet_transaction_confirmed: ['wallet'], wallet_balance_observed: ['balance', 'wallet'],
  venue_balance_observed: ['balance'], account_balance_change_observed: ['balance'],
  valuation_updated: ['balance'], native_asset_valuation_updated: ['balance'],
  fill_observed: ['fill', 'execution'], order_observed: ['execution'],
  forecast: ['market', 'decision'], agent_cycle: ['agent', 'system'],
  agent_runtime: ['agent', 'system'], cognition_started: ['cognition'], cognition_completed: ['cognition'],
  economic_reconciliation: ['economics'], provider_failure: ['error', 'system'],
});

export function canonicalEventFilters(event) {
  const type = String(event.record?.event_type ?? event.record?.stage ?? '').toLowerCase();
  const key = String(event.key ?? '');
  const mapped = new Set(event.categories ?? [event.category ?? 'system']);
  for (const category of CANONICAL_EVENT_FILTERS[type] ?? []) mapped.add(category);
  const add = (...values) => values.forEach(value => mapped.add(value));
  if (/wallet_balance|balance_observed/.test(type)) add('balance', 'wallet');
  else if (/wallet_transaction|wallet_execution|wallet_validation|transfer|contract_interaction|signer|wallet_state|wallet_fee/.test(type) || key.startsWith('wallet:')) add('wallet');
  if (/account_balance|venue_balance|capital_balance|valuation|balance_change/.test(type) || key.startsWith('balance:')) add('balance');
  if (/fill/.test(type) || key.startsWith('fill:')) add('fill', 'execution');
  if (/order/.test(type) || key.startsWith('execution:')) add('execution');
  if (/forecast|decision/.test(type) || key.startsWith('decision:')) add('market', 'decision');
  if (/agent_cycle|agent_runtime/.test(type)) add('agent', 'system');
  if (/cognition|inference|model_route/.test(type)) add('cognition');
  if (/economic_reconcil|reconciliation/.test(type)) add('economics');
  if (/provider_failure|provider_error|provider_unavailable/.test(type)) add('error', 'system');
  if (event.tone === 'attention' && !mapped.has('error')) add('error');
  return [...mapped];
}

function balanceEvents(history) {
  const points = Array.isArray(history?.points) ? history.points : [];
  const previous = new Map(), bySource = new Map(), overall = [];
  let previousOverall;
  for (const point of points) {
    const amount = point?.amount_usd == null ? null : String(point.amount_usd);
    if (amount !== previousOverall) {
      previousOverall = amount;
      if (point?.at) overall.push({
        key: `balance:agent:${point.at}`, category: 'balance', title: 'AGENT BALANCE OBSERVED',
        categories: ['balance'], detail: `${amount === null ? 'USD value unavailable' : `${amount} USD`} · scope ${point.scope ?? 'unknown'} · persisted account snapshot`,
        actor: 'Live balance subtotal', status: 'observed', at: eventTime(point.at),
        record: { amount_usd: amount, observed_at: point.at, scope: point.scope, source: 'Persisted account balance observation' },
        context: { type: 'account', id: 'account:agent-balance', label: 'Live balance subtotal', status: 'observed',
          source: 'Persisted account balance observation', record: { amount_usd: amount, observed_at: point.at, scope: point.scope } },
      });
    }
    if (!Array.isArray(point?.contributions)) continue;
    for (const contribution of point.contributions) {
      if (contribution?.observed !== true || !contribution.id) continue;
      const amount = contribution.amount_usd == null ? null : String(contribution.amount_usd);
      const before = previous.get(contribution.id);
      previous.set(contribution.id, amount);
      if (before === amount) continue;
      const wallet = String(contribution.id).startsWith('wallet:');
      const label = contribution.label || contribution.id;
      const at = eventTime(contribution.observed_at ?? point.at);
      const row = { ...contribution, observed_at: contribution.observed_at ?? point.at,
        source_scope: point.scope, source: 'Persisted account balance observation' };
      const event = {
        key: `balance:${contribution.id}:${row.observed_at}`, category: 'balance',
        categories: wallet ? ['balance', 'wallet'] : ['balance'],
        title: wallet ? 'WALLET BALANCE / VALUATION UPDATED' : 'ACCOUNT BALANCE / VALUATION UPDATED',
        detail: `${label} · ${amount === null ? 'USD value unavailable' : `${amount} USD`} · ${contribution.status ?? 'observed'} · source ${row.source}`,
        actor: label, status: contribution.status ?? 'observed', at, record: row,
        context: wallet
          ? { type: 'wallet', id: contribution.id, chain: contribution.id.split(':')[1], label,
            status: contribution.status ?? 'observed', source: row.source, record: row }
          : { type: contribution.id.startsWith('venue:') ? 'venue' : 'economics', id: contribution.id,
            venue: contribution.id.startsWith('venue:') ? label : undefined, label,
            status: contribution.status ?? 'observed', source: row.source, record: row },
      };
      const sourceEvents = bySource.get(contribution.id) ?? [];
      sourceEvents.push(event);
      bySource.set(contribution.id, sourceEvents);
    }
  }
  // Keep a small recent window per account so balance history stays visible
  // without crowding transactions, fills, agent activity, and errors out.
  return [...overall.slice(-2), ...[...bySource.values()].flatMap(rows => rows.slice(-2))];
}

export function recordedFeed(snapshot, sources = {}) {
  const rows = (key) => snapshot?.sections?.[key]?.rows ?? [];
  const events = [
    ...rows('mission_events').filter((row) => row.id != null).map((row) => ({
      key: `mission:${row.id}`, category: 'system', title: words(row.event_type),
      detail: row.detail || 'No detail recorded.', actor: row.actor || 'NOEMA',
      status: row.status || 'unknown', at: eventTime(row.created_at), record: row,
      context: { type: 'mission', id: `mission:${row.mission_id}`, mission_id: row.mission_id,
        label: words(row.event_type), status: row.status, source: 'Persisted mission event', record: row },
    })),
    ...rows('handoffs').filter(row => row.handoff_id != null).map(row => ({
      key: `agent:${row.handoff_id}`, category: 'agent', title: 'SPECIALIST ROUTE',
      detail: `${row.from_specialist ?? 'NOEMA'} → ${row.to_specialist ?? 'specialist unknown'} · ${row.objective ?? 'Objective unavailable'}`,
      actor: 'Persisted mission handoff', status: row.status ?? 'unknown',
      at: eventTime(row.updated_at ?? row.created_at), record: row,
      context: { type: 'mission', id: `mission:${row.mission_id}`, mission_id: row.mission_id,
        label: `${row.from_specialist ?? 'NOEMA'} → ${row.to_specialist ?? 'specialist'}`,
        status: row.status, source: 'Persisted specialist handoff', record: row },
    })),
    ...rows('sessions').filter(row => row.session_id != null).map(row => ({
      key: `cognition:${row.session_id}`, category: 'cognition', title: `COGNITION · ${words(row.provider)}`,
      detail: row.objective || 'Cognition objective unavailable.', actor: row.model || row.provider || 'Cognition route',
      status: row.status || 'unknown', at: eventTime(row.completed_at ?? row.created_at), record: row,
      context: { type: 'system', id: `session:${row.session_id}`, label: row.objective ?? 'Cognition session',
        status: row.status, source: 'Persisted cognition session', record: row },
    })),
    ...rows('activity').filter((row) => row.id != null).map((row) => ({
      key: `runtime:${row.id}`, category: /market|quote|price/i.test(row.stage) ? 'market' : /cognition|model|inference/i.test(row.stage) ? 'cognition' : /agent/i.test(row.stage) ? 'agent' : 'system', title: words(row.stage),
      detail: row.detail || 'No detail recorded.', actor: row.tool || 'NOEMA',
      status: row.status || 'unknown', at: eventTime(row.created_at), record: row,
      context: { type: 'runtime', id: `runtime-event:${row.id}`, mission_id: row.mission_id,
        label: words(row.stage), status: row.status, source: 'Persisted runtime event', record: row },
    })),
    ...rows('decisions').filter((row) => row.id != null).map((row) => ({
      key: `decision:${row.id}`, category: 'decision', title: `${words(row.decision).toUpperCase()} · ${row.market_id}`,
      detail: row.reason || 'Decision rationale unavailable.', actor: row.model_version || 'Model unknown',
      status: row.decision || 'unknown', at: eventTime(row.created_at), record: row,
      context: { type: 'decision', id: `forecast:${row.id}`, market_id: row.market_id, venue: row.venue,
        label: row.market_id, status: row.decision, source: 'Immutable forecast ledger', record: row },
    })),
    ...rows('wallet_transactions').filter((row) => row.id != null).map((row) => {
      let payload = {};
      try { payload = typeof row.payload === 'string' ? JSON.parse(row.payload) : row.payload ?? {}; } catch { /* malformed evidence stays unavailable */ }
      const detail = `${words(payload?.chain ?? 'Chain unknown')} · ${words(payload?.action ?? row.event_type)} · ${payload?.transaction_reference ?? 'Receipt pending'}`;
      const chain = payload?.chain ?? payload?.venue ?? ({ '8453': 'base', '1': 'ethereum', '137': 'polygon' }[String(payload?.chain_id)] ?? 'unknown');
      const contribution = (sources.balanceHistory?.points ?? []).at(-1)?.contributions?.find(item =>
        String(item.id ?? '').startsWith(`wallet:${chain}:`));
      const network = (sources.wallets?.networks ?? []).find(item => item.chain === chain);
      const walletKey = network
        ? `${network.chain ?? 'unknown'}-${network.network ?? 'unknown'}`.toLowerCase().replace(/[^a-z0-9-]/g, '-') : null;
      return { key: `wallet:${row.id}`, category: 'wallet', title: words(row.event_type), detail,
        actor: payload?.chain ?? 'Wallet ledger', status: payload?.status ?? 'recorded', at: eventTime(row.created_at), record: row,
        categories: ['wallet'],
        context: { type: 'wallet', id: walletKey ? `wallet:${walletKey}` : contribution?.id ?? `wallet:${chain}`, chain: chain === 'unknown' ? undefined : chain,
          label: words(row.event_type), status: payload?.status ?? 'recorded',
          source: 'Persisted wallet transaction', record: { ...row, detail } } };
    }),
    ...rows('economic_events').filter((row) => row.id != null && !rows('wallet_transactions').some(wallet => wallet.id === row.id)).map((row) => {
      const reconciled = String(row.reconciliation_state).toUpperCase() === 'RECONCILED';
      const balanceEvent = row.capital_class === 'owner_capital' && row.value_state === 'realized' && reconciled;
      const pnlEvent = row.capital_class === 'trading_pnl' && row.value_state === 'realized' && reconciled;
      const eventType = String(row.event_type ?? '').toLowerCase();
      const marketEvent = /market_observation|market_data|price_observation/.test(eventType);
      const categories = marketEvent ? ['market'] : pnlEvent ? ['pnl', 'economics']
        : balanceEvent ? ['balance', 'economics'] : ['economics'];
      return {
      key: `economics:${row.id}`, category: marketEvent ? 'market' : balanceEvent ? 'balance' : pnlEvent ? 'pnl' : 'economics', categories, title: marketEvent
        ? words(row.event_type) : balanceEvent
        ? 'BALANCE CHANGED' : pnlEvent ? 'P&L REALIZED' : words(row.event_type),
      detail: `${words(row.capital_class)} · ${row.amount_usd == null ? 'USD value unavailable' : `${row.amount_usd} USD`} · ${words(row.reconciliation_state)}`,
      actor: row.provider ?? 'Economic ledger', status: row.reconciliation_state ?? 'unknown',
      at: eventTime(row.occurred_at ?? row.created_at), record: row,
      context: { type: 'economics', id: `economics:${row.id}`, mission_id: row.mission_id, strategy_id: row.strategy_id,
        label: words(row.event_type), status: row.reconciliation_state, source: 'Persisted economic ledger', record: row },
    }; }),
    ...(sources.gateway?.recent_requests ?? []).filter(row => row.proposal_id != null).map(row => ({
      key: `execution:${row.proposal_id}`, category: /rejected|failed|unknown/i.test(row.status) ? 'error' : 'execution', title: row.status === 'submitted' || row.status === 'confirmed' ? 'ORDER ACKNOWLEDGED' : `${words(row.tier)} · ${words(row.status)}`,
      detail: row.reason ?? 'Recorded execution gateway request. Inspect the persisted policy result.',
      actor: row.venue ?? 'Execution gateway', status: row.status ?? 'unknown', at: eventTime(row.created_at), record: row,
      context: { type: 'execution', id: `execution:${row.proposal_id}`, mission_id: row.mission_id, label: words(row.tier),
        status: row.status, source: 'Persisted execution gateway', record: row },
    })),
    ...(sources.venues?.venues ?? []).flatMap(venue => (venue.account?.recent_orders ?? []).filter(row => row.order_id != null).map(row => ({
      key: `order:${venue.venue}:${row.order_id}`, category: 'execution', title: `ORDER · ${row.ticker ?? 'Contract unknown'}`,
      detail: `${row.status ?? 'status unknown'} · ${row.side ?? 'side unknown'} · ${row.remaining_count_fp ?? 'remaining quantity unknown'} remaining`,
      actor: venue.venue, status: row.status ?? 'recorded', at: eventTime(row.updated_time ?? row.created_time),
      record: { ...row, event_type: 'order_observed' },
      context: { type: 'market', id: `market:${row.ticker}`, market_id: row.ticker, venue: venue.venue,
        label: row.ticker ?? 'Venue order', status: row.status ?? 'recorded', source: 'Official venue order record', record: row },
    }))),
    ...(sources.venues?.venues ?? []).flatMap(venue => (venue.account?.recent_fills ?? []).filter(row => row.fill_id != null).map(row => ({
      key: `fill:${venue.venue}:${row.fill_id}`, category: 'fill', title: `FILL · ${row.ticker ?? 'Contract unknown'}`,
      detail: `${row.count_fp ?? 'Unknown'} contracts · ${row.side ?? 'side unknown'} · fee ${row.fee_cost ?? 'unknown'} USD`,
      actor: venue.venue, status: 'filled', at: eventTime(row.created_time), record: row,
      context: { type: 'fill', id: `fill:${row.fill_id}`, market_id: row.ticker, venue: venue.venue,
        label: row.ticker, status: 'filled', source: 'Official venue fill record', record: row },
    }))),
    ...rows('reviews').filter(row => row.id != null && String(row.next_state ?? '').toLowerCase() === 'promoted').map(row => ({
      key: `review:${row.id}`, category: 'system', title: 'STRATEGY PROMOTED',
      detail: `${row.specialist ?? 'Strategy unknown'} · ${row.previous_state ?? 'prior state unknown'} → ${row.next_state}`,
      actor: 'Promotion review', status: 'promoted', at: eventTime(row.created_at), record: row,
      context: { type: 'specialist', id: `specialist:${row.specialist}`, label: row.specialist ?? 'Strategy', status: 'promoted', source: 'Persisted promotion review', record: row },
    })),
  ];
  events.push(...balanceEvents(sources.balanceHistory));
  const priority = event => Math.min(...canonicalEventFilters(event).map(category => {
    const index = FILTER_PRIORITY.indexOf(category); return index < 0 ? FILTER_PRIORITY.length : index;
  }));
  const compare = (a, b) => priority(a) - priority(b) || (b.at ?? -Infinity) - (a.at ?? -Infinity) || a.key.localeCompare(b.key);
  const ordered = events.map((event) => ({ ...event, tone: tone(event.status) })).sort(compare);
  const bands = Array.from({ length: 7 }, () => []);
  for (const event of ordered) {
    const categories = canonicalEventFilters(event);
    const band = event.category === 'balance' || categories.includes('balance') ? 0
      : event.category === 'wallet' || categories.includes('wallet') ? 1
        : event.category === 'fill' || event.category === 'execution' ? 2
          : event.category === 'agent' ? 3 : event.category === 'cognition' ? 4
            : event.tone === 'attention' || event.category === 'error' || event.category === 'system' ? 5 : 6;
    bands[band].push(event);
  }
  const quotas = [15, 18, 20, 14, 8, 18, 27];
  const selected = bands.flatMap((band, index) => band.slice(0, quotas[index]));
  const selectedKeys = new Set(selected.map(event => event.key));
  const sorted = [...selected, ...ordered.filter(event => !selectedKeys.has(event.key))]
    .slice(0, 120).sort(compare);
  const display = [], repeated = new Map();
  for (const event of sorted) {
    const decision = event.record?.decision;
    if (event.category === 'decision' && /^(PASS|NO_ACTION)$/i.test(String(decision ?? ''))) {
      const group = JSON.stringify([event.category, event.record.venue, decision, event.record.reason, event.actor]);
      const existing = repeated.get(group);
      if (existing) { existing.repeatCount++; existing.records.push(event.record); continue; }
      event.repeatCount = 1; event.records = [event.record]; repeated.set(group, event);
    }
    display.push(event);
  }
  for (const event of repeated.values()) {
    if (event.repeatCount > 1) {
      const reason = String(event.record.reason ?? 'No independent edge detected').trim();
      const markets = new Set(event.records.map(row => row.market_id).filter(Boolean));
      event.title = `${event.repeatCount} forecasts passed · ${reason}`;
      event.detail = `${markets.size} distinct markets · expandable immutable forecast records`;
    }
  }
  return display;
}

export function contextFeed(events, context) {
  if (!context || context.type === 'core') return events;
  return events.filter(event => {
    const ref = event.context ?? {}, record = event.record ?? {};
    if (context.market_id) return ref.market_id === context.market_id && (!context.venue || String(ref.venue).toLowerCase().replaceAll(' ', '_') === String(context.venue).toLowerCase().replaceAll(' ', '_'));
    if (context.mission_id) return ref.mission_id === context.mission_id || record.mission_id === context.mission_id;
    if (context.chain) return event.actor === context.chain || event.context?.chain === context.chain
      || record.chain === context.chain || record.payload?.chain === context.chain;
    if (context.venue) return String(ref.venue ?? event.actor).toLowerCase().replaceAll(' ', '_') === String(context.venue).toLowerCase().replaceAll(' ', '_');
    if (context.strategy_id) return ref.strategy_id === context.strategy_id || record.strategy_id === context.strategy_id;
    if (context.asset) return String(record.asset ?? record.underlying_asset ?? '').toUpperCase() === context.asset;
    return events;
  });
}

export function feedDifference(previous, next) {
  if (previous === null) return { initial: true, added: 0, updated: 0 };
  const signature = (event) => JSON.stringify([event.title, event.detail, event.status, event.actor, event.at]);
  const known = new Map(previous.map((event) => [event.key, signature(event)]));
  return { initial: false,
    added: next.filter((event) => !known.has(event.key)).length,
    updated: next.filter((event) => known.has(event.key) && known.get(event.key) !== signature(event)).length,
  };
}

export function filterFeed(events, category = 'all', query = '') {
  const needle = query.trim().toLocaleLowerCase();
  const aliases = { decisions: 'decision', wallets: 'wallet', runtime: 'system', economics: 'economics', 'p&l': 'pnl' };
  const selected = aliases[category] ?? category;
  return events.filter((event) => {
    const categories = canonicalEventFilters(event);
    return (category === 'all' || selected === 'error' && (categories.includes('error') || event.tone === 'attention')
    || selected !== 'error' && categories.includes(selected) || category === 'attention' && event.tone === 'attention')
    && (!needle || `${event.title} ${event.detail} ${event.actor} ${event.status}`.toLocaleLowerCase().includes(needle));
  });
}

export function filterFeedCount(events, category = 'all', query = '') {
  return filterFeed(events, category, query).reduce((count, event) => count + (event.repeatCount ?? 1), 0);
}

export function feedHistogram(events, count = 20) {
  const times = events.map((event) => event.at).filter((at) => at !== null);
  if (!times.length) return null;
  const end = Math.max(...times), start = Math.min(...times);
  const span = Math.max(1, end - start);
  const buckets = Array.from({ length: count }, () => 0);
  for (const at of times) buckets[Math.min(count - 1, Math.floor((at - start) / span * count))]++;
  return { start, end, buckets };
}
