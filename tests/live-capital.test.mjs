import test from 'node:test';
import assert from 'node:assert/strict';
import { capitalNumber, capitalAccounts, agentBalanceState } from '../noema/static/live-capital.mjs';
const now = Date.parse('2026-09-29T12:00:00Z'), at = new Date(now).toISOString();

test('unknown values remain unknown; true observed zero is preserved', () => {
  for (const value of [null, undefined, '', ' ', false, true, 'NaN', 'Infinity', {}, []]) assert.equal(capitalNumber(value), null);
  assert.equal(capitalNumber('0'), 0);
  assert.equal(capitalNumber('1.25'), 1.25);
});

test('Agent Balance never promotes a known subtotal to reconciled NAV', () => {
  assert.equal(agentBalanceState({ amount_usd: '0', status: 'CACHED', stale_sources: 0 }), 'UNRECONCILED');
  assert.equal(agentBalanceState({ amount_usd: '4.25', status: 'CACHED', stale_sources: 1 }), 'STALE');
  assert.equal(agentBalanceState({ amount_usd: null, status: 'UNKNOWN', stale_sources: 0 }), 'UNAVAILABLE');
  assert.equal(agentBalanceState({ amount_usd: '4.25', status: 'CACHED' }, true), 'STALE');
});

test('isolates native units, account identities, and stale sources', () => {
  const venues = { as_of: at, venues: [{ venue: 'Kalshi', account: { status: 'authenticated_read_only', cash_balance_usd: '0' } }] };
  const wallets = { observed_at: at, networks: [{ chain: 'polygon', address: 'wallet-a', native_balance: '2.4', readable: true }] };
  const accounts = capitalAccounts(venues, wallets, {}, now);
  assert.equal(accounts[0].amount, 0); assert.equal(accounts[0].status, 'Observed');
  assert.equal(accounts[1].amount, null);
  assert.equal(accounts.find(a => a.unit === 'POL').amount, 2.4);
  assert.ok(accounts.find(a => a.unit === 'POL').id.includes('wallet-a'));
  assert.equal(capitalAccounts(venues, wallets, { venues: true }, now)[0].status, 'Stale');
  assert.equal(capitalAccounts(venues, wallets, {}, now + 121000)[0].status, 'Stale');
  assert.equal(capitalAccounts(venues, wallets, {}, now - 10000)[0].status, 'Stale');
  assert.equal(capitalAccounts(null, null, {}, now).filter(a => a.amount !== null).length, 0);
});
