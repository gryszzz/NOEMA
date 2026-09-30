import test from 'node:test';
import assert from 'node:assert/strict';
import { recordedFeed, feedDifference, filterFeed, filterFeedCount, feedHistogram, eventTime, canonicalEventFilters } from '../noema/static/workstation-feed.mjs';

const snapshot = (missionEvents = [], decisions = [], activity = []) => ({ sections: {
  mission_events: { rows: missionEvents }, decisions: { rows: decisions }, activity: { rows: activity },
} });
const event = (id, detail = 'Observed evidence', status = 'completed') => ({
  id, detail, status, mission_id: 'm1', event_type: 'research_result', actor: 'critic', created_at: '2026-09-29T12:00:00Z',
});

test('merges recorded sources without colliding IDs and prioritizes agent activity before forecasts', () => {
  const events = recordedFeed(snapshot([event(1)], [{ id: 1, market_id: 'BTC', venue: 'fixture', decision: 'PASS',
    created_at: '2026-09-29 12:00:01', reason: 'Costs exceed edge' }], [{ id: 1, created_at: null, stage: 'unknown' }]));
  assert.deepEqual(events.map((item) => item.key), ['mission:1', 'runtime:1', 'decision:1']);
  assert.equal(events[2].context.venue, 'fixture');
  assert.equal(events.find(item => item.key === 'runtime:1').at, null);
  assert.equal(eventTime('garbled timestamp'), null);
  assert.equal(eventTime('2026-09-29 12:00:01'), Date.parse('2026-09-29T12:00:01Z'));
});

test('the initial snapshot is not counted as newly arrived activity', () => {
  const events = recordedFeed(snapshot([event(1)]));
  assert.deepEqual(feedDifference(null, events), { initial: true, added: 0, updated: 0 });
  assert.deepEqual(feedDifference(events, events), { initial: false, added: 0, updated: 0 });
});

test('counts new and changed records separately and ignores window evictions', () => {
  const before = recordedFeed(snapshot([event(1), event(2)]));
  const after = recordedFeed(snapshot([event(2, 'Rejected evidence', 'failed'), event(3)]));
  assert.deepEqual(feedDifference(before, after), { initial: false, added: 1, updated: 1 });
  assert.equal(filterFeed(after, 'attention')[0].record.id, 2);
  assert.equal(filterFeed(after, 'system', 'REJECTED').length, 1);
  assert.equal(filterFeed(after, 'decisions').length, 0);
});

test('retains hostile text as data and bounds the loaded feed', () => {
  const events = recordedFeed(snapshot(Array.from({ length: 150 }, (_, id) => event(id, '<img src=x onerror=alert(1)>'))));
  assert.equal(events.length, 120);
  assert.equal(events[0].detail, '<img src=x onerror=alert(1)>');
  assert.deepEqual(recordedFeed(null), []);
});

test('histogram counts only actual timestamped records including coincident events', () => {
  const events = recordedFeed(snapshot([event(1), event(2), { ...event(3), created_at: null }]));
  const bins = feedHistogram(events);
  assert.equal(bins.buckets.reduce((sum, count) => sum + count, 0), 2);
  assert.equal(bins.start, bins.end);
  assert.equal(feedHistogram([]), null);
});

test('financial events remain source records and exact selection never guesses attribution', async () => {
  const { contextFeed } = await import('../noema/static/workstation-feed.mjs');
  const data = snapshot();
  data.sections.economic_events = { rows: [{ id: 4, event_type: 'owner_deposit', capital_class: 'owner_capital', amount_usd: '20', reconciliation_state: 'RECONCILED', provider: 'fixture', created_at: '2026-09-29T12:00:00Z' }] };
  const events = recordedFeed(data, { gateway: { recent_requests: [{ proposal_id: 'p1', tier: 'execution', status: 'rejected', venue: 'Kalshi', created_at: '2026-09-29T12:00:01Z' }] }, venues: { venues: [{ venue: 'Kalshi', account: { recent_fills: [{ fill_id: 'f1', ticker: 'BTC-15M', count_fp: '1', created_time: '2026-09-29T12:00:02Z' }] } }] } });
  assert.equal(events.length, 3);
  assert.equal(events.find(event => event.key === 'execution:p1').category, 'error');
  assert.ok(events[2].detail.includes('owner capital'));
  assert.equal(contextFeed(events, { type: 'market', market_id: 'BTC-15M', venue: 'kalshi' }).length, 1);
  assert.equal(contextFeed(events, { type: 'asset', asset: 'BTC' }).length, 0, 'ticker text alone does not establish asset attribution');
  assert.equal(contextFeed(events, { type: 'core' }).length, 3);
});

test('collapses repeated PASS decisions visually and retains represented-record counts', () => {
  const decisions = [1, 2, 3].map(id => ({ id, market_id: 'BTC-15M', venue: 'Kalshi', decision: 'PASS',
    reason: 'No robust edge', model_version: 'm1', created_at: `2026-09-29T12:00:0${id}Z` }));
  const events = recordedFeed(snapshot([], decisions));
  assert.equal(events.length, 1);
  assert.equal(events[0].repeatCount, 3);
  assert.equal(filterFeed(events, 'decision').length, 1);
  assert.equal(filterFeed(events, 'error').length, 0);
  assert.equal(filterFeedCount(events, 'decision'), 3);
  assert.equal(filterFeedCount(events, 'market'), 3);
});

test('canonical event taxonomy supports overlapping filters without duplicating persisted records', () => {
  const types = [
    ['wallet_transaction_confirmed', ['wallet']],
    ['wallet_balance_observed', ['balance', 'wallet']],
    ['venue_balance_observed', ['balance']],
    ['fill_observed', ['fill', 'execution']],
    ['order_observed', ['execution']],
    ['forecast', ['market', 'decision']],
    ['agent_cycle', ['agent', 'system']],
    ['cognition_completed', ['cognition']],
    ['economic_reconciliation', ['economics']],
    ['provider_failure', ['error', 'system']],
  ];
  for (const [event_type, expected] of types) {
    const mapped = canonicalEventFilters({ category: 'system', record: { event_type } });
    assert.ok(expected.every(value => mapped.includes(value)), `${event_type} maps to ${expected.join(', ')}`);
  }
  const events = recordedFeed(snapshot(), { venues: { venues: [{ venue: 'Kalshi', account: {
    recent_fills: [{ fill_id: 'f1', ticker: 'MKT', created_time: '2026-09-29T12:00:00Z' }],
    recent_orders: [{ order_id: 'o1', ticker: 'MKT', status: 'resting', created_time: '2026-09-29T12:00:01Z' }],
  } }] } });
  assert.equal(filterFeed(events, 'fill').length, 1);
  assert.equal(filterFeed(events, 'execution').length, 2);
  assert.equal(events.find(row => row.key.startsWith('fill:')).context.type, 'fill');
  assert.equal(events.find(row => row.key.startsWith('order:')).context.market_id, 'MKT');
});

test('wallet transactions and persisted account valuation observations appear in mapped filters', () => {
  const data = snapshot();
  data.sections.wallet_transactions = { rows: [
    { id: 1, event_type: 'wallet_transaction_confirmed', created_at: '2026-09-29T12:00:00Z', payload: {
      chain: 'base', action: 'evm_native_transfer', transaction_reference: '0xabc', status: 'confirmed' } },
    { id: 2, event_type: 'wallet_transaction_confirmed', created_at: '2026-09-29T12:01:00Z', payload: {
      chain: 'solana', action: 'contract_interaction', transaction_reference: 'sig123', status: 'confirmed' } },
  ] };
  const balanceHistory = { points: [
    { at: '2026-09-29T11:00:00Z', amount_usd: '11.00', scope: 'scope-v1', contributions: [
      { id: 'wallet:base:0x123', label: 'Base', amount_usd: '1.00', observed: true, observed_at: '2026-09-29T11:00:00Z', status: 'CURRENT' },
      { id: 'venue:Kalshi', label: 'Kalshi', amount_usd: '10.00', observed: true, observed_at: '2026-09-29T11:00:00Z', status: 'CURRENT' },
      { id: 'revenue:stripe', label: 'Stripe available', amount_usd: '0', observed: true, observed_at: '2026-09-29T11:00:00Z', status: 'CURRENT' },
    ] },
    { at: '2026-09-29T11:01:00Z', amount_usd: '12.75', scope: 'scope-v1', contributions: [
      { id: 'wallet:base:0x123', label: 'Base', amount_usd: '1.25', observed: true, observed_at: '2026-09-29T11:01:00Z', status: 'CURRENT' },
      { id: 'venue:Kalshi', label: 'Kalshi', amount_usd: '11.00', observed: true, observed_at: '2026-09-29T11:01:00Z', status: 'CURRENT' },
      { id: 'revenue:stripe', label: 'Stripe available', amount_usd: '0.50', observed: true, observed_at: '2026-09-29T11:01:00Z', status: 'CURRENT' },
    ] },
  ] };
  const events = recordedFeed(data, { balanceHistory });
  assert.equal(filterFeedCount(events, 'wallet'), 4, 'two transactions plus two persisted wallet observations');
  assert.equal(filterFeedCount(events, 'balance'), 8, 'Agent Balance plus wallet, venue cash, and Stripe observations');
  assert.equal(filterFeed(events, 'wallet').filter(row => row.context.source === 'Persisted wallet transaction').length, 2);
  assert.ok(filterFeed(events, 'balance').every(row => row.context.source === 'Persisted account balance observation'));
  assert.equal(filterFeed(events, 'balance').filter(row => row.title === 'AGENT BALANCE OBSERVED').length, 2);
  assert.equal(filterFeed(events, 'wallet')[0].context.type, 'wallet');
});

test('mixed feed gives account and wallet records precedence while retaining forecast batches', () => {
  const data = snapshot([event(1)], [{ id: 1, market_id: 'BTC', venue: 'fixture', decision: 'PASS',
    reason: 'No edge', created_at: '2026-09-29T12:03:00Z' }], [{ id: 1, stage: 'agent_cycle', status: 'completed', created_at: '2026-09-29T12:01:00Z' }]);
  data.sections.wallet_transactions = { rows: [{ id: 1, event_type: 'wallet_transaction_confirmed', created_at: '2026-09-29T11:00:00Z', payload: { chain: 'base', status: 'confirmed' } }] };
  const feed = recordedFeed(data);
  assert.deepEqual(feed.map(row => row.category), ['wallet', 'agent', 'system', 'decision']);
  assert.equal(filterFeed(feed, 'agent').length, 1);
  assert.equal(filterFeed(feed, 'system').length, 2);
  assert.equal(filterFeed(feed, 'market').length, 1);
});

test('priority window keeps a useful forecast slice when higher-priority system history is large', () => {
  const data = snapshot(Array.from({ length: 150 }, (_, id) => ({ ...event(id + 1), event_type: 'research_result' })),
    Array.from({ length: 50 }, (_, id) => ({ id, market_id: `MKT-${id}`, venue: 'fixture', decision: 'PASS',
      reason: 'No edge', model_version: 'v1', created_at: `2026-09-29T12:${String(Math.floor(id / 60)).padStart(2, '0')}:${String(id % 60).padStart(2, '0')}Z` })));
  const feed = recordedFeed(data);
  assert.ok(feed.length <= 120);
  assert.ok(filterFeedCount(feed, 'system') > 0);
  assert.equal(filterFeed(feed, 'decision').length, 1, 'repeated forecasts stay compressed into a batch');
  assert.equal(filterFeedCount(feed, 'decision'), 27, 'forecast records occupy their allocated share of the visible window');
  assert.ok(feed.findIndex(row => row.key.startsWith('mission:')) < feed.findIndex(row => row.key.startsWith('decision:')));
});
