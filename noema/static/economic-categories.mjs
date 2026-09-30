import { capitalNumber, format, timeLabel, tokenAmount } from './live-capital.mjs';
import { eventTime } from './workstation-feed.mjs';
const node = (tag, text, className) => { const el = document.createElement(tag); if (text != null) el.textContent = text; if (className) el.className = className; return el; };
const words = value => String(value ?? 'Unknown').replaceAll('_', ' ');
const dollars = value => format(capitalNumber(value));
const normalizeVenue = value => String(value ?? '').toLowerCase().replaceAll(' ', '_');
const displayStatus = value => value === 'CACHED' ? 'CURRENT' : value;

export function createEconomicCategories({ onSelect, clearSelection }) {
  const $ = id => document.getElementById(id);
  let state = {}, context = { type: 'core' }, active = 'prediction';
  const venueSections = new Map();
  function show(name, focus = false) {
    if (!$(`category-${name}`)) return;
    active = name;
    for (const tab of document.querySelectorAll('[data-category]')) {
      const selected = tab.dataset.category === name;
      tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
      $(`category-${tab.dataset.category}`).hidden = !selected;
      if (focus && selected) tab.focus();
    }
    freshness();
  }
  for (const tab of document.querySelectorAll('[data-category]')) {
    tab.onclick = () => show(tab.dataset.category);
    tab.onkeydown = event => {
      const tabs = [...document.querySelectorAll('[data-category]')], index = tabs.indexOf(tab);
      const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null;
      if (next !== null) { event.preventDefault(); show(tabs[next].dataset.category, true); }
    };
  }
  const revealHash = () => {
    const target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    const panel = target?.closest('.category-panel');
    if (panel) { show(panel.id.replace('category-', '')); target.scrollIntoView({ block: 'start' }); }
  };
  window.addEventListener('hashchange', revealHash);
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href^="#"]');
    const target = link && document.getElementById(link.getAttribute('href').slice(1));
    const panel = target?.closest('.category-panel');
    if (panel) show(panel.id.replace('category-', ''));
  });
  $('category-clear').onclick = clearSelection;
  function freshness() {
    const source = active === 'web3' ? 'wallets' : ['prediction', 'markets'].includes(active) ? 'venues' : 'operations';
    const at = active === 'revenue' ? state.stripe?.observed_at : state.freshness?.[`${source}At`];
    const raw = active === 'revenue' ? (state.stripe?.status === 'connected' ? 'current' : 'unavailable') : state.freshness?.[source] ?? 'unknown';
    const age = eventTime(at) === null ? null : (Date.now() - eventTime(at)) / 1000;
    const label = raw === 'stale' || (age !== null && age > (source === 'operations' ? 45 : 120)) ? 'STALE' : raw === 'unavailable' ? 'DISCONNECTED' : age === null ? 'UNKNOWN' : source === 'operations' ? 'LIVE' : 'DELAYED · 30s refresh';
    $('category-freshness').textContent = `${label} · last read ${timeLabel(eventTime(at))}`;
    $('category-context-title').textContent = context.type === 'core' ? 'All NOEMA activity' : `Selected: ${context.label}`;
  }
  function stats(items) {
    const grid = node('dl', null, 'category-stats');
    for (const [label, value] of items) { const cell = node('div'); cell.append(node('dt', label), node('dd', value)); grid.append(cell); }
    return grid;
  }
  function card(title, description, items) {
    const el = node('article', null, 'category-card'); el.append(node('h3', title), node('p', description), stats(items)); return el;
  }
  function table(title, headings, rows, empty) {
    const section = node('section', null, 'category-table-section'); section.append(node('h3', title));
    if (!rows.length) { section.append(node('p', empty, 'category-empty')); return section; }
    const scroll = node('div', null, 'category-table-scroll'), table = node('table'), head = node('tr');
    for (const heading of headings) { const th = node('th', heading); th.scope = 'col'; head.append(th); }
    const thead = node('thead'); thead.append(head); table.append(thead);
    const body = node('tbody');
    for (const cells of rows) { const row = node('tr'); for (const value of cells) row.append(node('td', value)); body.append(row); }
    table.append(body); scroll.append(table); section.append(scroll); return section;
  }
  function venueFills(rows, empty) {
    const section = node('section', null, 'venue-section venue-fills');
    section.append(node('h3', 'Recent fills'));
    if (!rows.length) { section.append(node('p', empty, 'category-empty')); return section; }
    const tape = node('div', null, 'venue-fill-tape');
    for (const row of rows) {
      const item = node('article', null, 'venue-fill');
      item.title = row.ticker ?? '';
      const top = node('div', null, 'venue-fill-top');
      const rawPrice = row.price_usd ?? (row.outcome_side === 'no' || row.side === 'no' ? row.no_price_dollars : row.yes_price_dollars);
      const price = capitalNumber(rawPrice);
      const priceLabel = price === null ? 'price unavailable' : `${(price * 100).toFixed(2)}¢`;
      top.append(node('time', timeLabel(eventTime(row.created_time))), node('span', row.side ?? row.outcome_side ?? 'TRADE', 'venue-fill-side'));
      const contract = node('strong', row.title ?? row.ticker ?? 'Contract unavailable', 'venue-fill-contract');
      const details = node('div', null, 'venue-fill-details');
      details.append(node('span', priceLabel), node('span', `Size ${row.count_fp ?? 'Unavailable'}`), node('span', `Fee ${row.fee_cost == null ? 'unavailable' : dollars(row.fee_cost)}`), node('span', 'FILLED', 'venue-status-pill'));
      item.append(top, contract, details); tape.append(item);
    }
    section.append(tape); return section;
  }
  function venueActivity(fills, orders, empty) {
    const section = node('section', null, 'venue-section venue-fills');
    section.append(node('h3', 'Recent account activity'));
    const events = [
      ...fills.map(row => ({ row, kind: 'fill', at: eventTime(row.created_time) })),
      ...orders.map(row => ({ row, kind: 'order', at: eventTime(row.created_time) })),
    ].sort((left, right) => (right.at ?? 0) - (left.at ?? 0));
    if (!events.length) { section.append(node('p', empty, 'category-empty')); return section; }
    const tape = node('div', null, 'venue-fill-tape');
    for (const { row, kind } of events) {
      const item = node('article', null, 'venue-fill'); item.title = row.ticker ?? '';
      const top = node('div', null, 'venue-fill-top');
      top.append(node('time', timeLabel(eventTime(row.created_time))), node('span', kind === 'fill' ? (row.side ?? 'Fill') : (row.status ?? 'Order'), 'venue-fill-side'));
      const details = node('div', null, 'venue-fill-details');
      if (kind === 'fill') details.append(node('span', `Size ${row.count_fp ?? 'Unknown'}`), node('span', `Fee ${dollars(row.fee_cost)}`), node('span', 'FILLED', 'venue-status-pill'));
      else details.append(node('span', `Filled ${row.fill_count_fp ?? 'Unknown'}`), node('span', `Remaining ${row.remaining_count_fp ?? 'Unknown'}`), node('span', String(row.status ?? 'PENDING').toUpperCase(), 'venue-status-pill'));
      item.append(top, node('strong', row.title ?? row.ticker ?? 'Contract unavailable', 'venue-fill-contract'), details); tape.append(item);
    }
    section.append(tape); return section;
  }
  function render() {
    const scrolls = [...document.querySelectorAll('.category-table-scroll')].map(el => [el.scrollTop, el.scrollLeft]);
    const focusedText = document.activeElement?.classList.contains('category-select') ? document.activeElement.textContent : null;
    freshness();
    const revenue = $('revenue-live-details'); revenue.replaceChildren();
    const stripe = state.canonical?.contributions?.find(row => row.id === 'revenue:stripe');
    revenue.append(card('Stripe available balance', `${stripe?.status ?? 'UNKNOWN'} · ${timeLabel(eventTime(stripe?.observed_at))}`, [
      ['Available USD (observed)', dollars(stripe?.amount_usd)],
      ['Included in known subtotal', stripe?.included ? dollars(stripe.amount_usd) : 'Excluded · source unavailable or stale'],
      ['Captured payments (gross)', state.stripe?.successful_usd_minor == null ? 'Unknown' : dollars(Number(state.stripe.successful_usd_minor) / 100)],
      ['Pending funds', 'Excluded from available-balance subtotal'], ['Historical gross payments', 'Excluded from available-balance subtotal'],
      ['Fees / refunds / payouts', words(state.stripe?.fees_refunds_payouts ?? 'not reconciled by connected tools')],
    ]));
    $('prediction-category-asof').textContent = state.venues ? `${state.venues.venues?.length ?? 0} venue observations · last read ${timeLabel(eventTime(state.venues.as_of))}` : 'No venue observation';
    const pnlRoot = $('account-pnl-details'); pnlRoot.replaceChildren();
    const accountPnl = state.economics?.canonical_ledger ?? {};
    pnlRoot.append(card('Live P&L reconciliation detail', 'NET P&L remains UNRECONCILED until live fills, open positions, settlements, fees, funding, and costs reconcile across all required sources.', [
      ['NET live P&L', 'UNRECONCILED'],
      ['Realized live P&L', accountPnl.financial_events_complete === true && accountPnl.verified_realized_trading_pnl_usd != null ? dollars(accountPnl.verified_realized_trading_pnl_usd) : 'UNAVAILABLE · complete live event coverage not established'],
      ['Unrealized live P&L', 'UNAVAILABLE · open positions and marks are not reconciled'],
      ['Partial reconciled event subtotal', accountPnl.reconciled_trading_pnl_subtotal_usd == null ? 'UNAVAILABLE' : `${dollars(accountPnl.reconciled_trading_pnl_subtotal_usd)} · partial subtotal, not NET P&L`],
      ['Pending settlements', 'UNAVAILABLE · settlement coverage incomplete'],
      ['Live execution authority', state.gateway?.prediction_execution_enabled === true ? 'Enabled by current policy · action checks still apply' : 'Fail closed · not armed'],
    ]));
    const venueRoot = $('prediction-live-details'); venueRoot.replaceChildren();
    const venues = (state.venues?.venues ?? []).filter(venue => !context.venue || normalizeVenue(venue.venue) === normalizeVenue(context.venue));
    if (!venues.length) venueRoot.append(node('p', 'LIVE DATA UNAVAILABLE · no venue observations for this selection.', 'category-empty'));
    for (const venue of venues) {
      const a = venue.account ?? {}, capital = a.capital ?? {};
      const positions = (capital.positions ?? []).filter(row => !context.market_id || row.ticker === context.market_id);
      const fills = (a.recent_fills ?? []).filter(row => !context.market_id || row.ticker === context.market_id);
      const orders = (a.recent_orders ?? []).filter(row => !context.market_id || row.ticker === context.market_id);
      const scoped = Boolean(context.market_id || context.asset || context.strategy_id || context.chain);
      const coverage = a.history_coverage ?? {};
      const positionsComplete = coverage.positions?.complete === true;
      const fillsComplete = (coverage.fills?.complete ?? coverage.activities?.complete) === true;
      const ordersComplete = coverage.orders?.complete === true;
      const observedAt = eventTime(a.observed_at ?? state.venues.as_of);
      const age = observedAt === null ? null : Math.max(0, Math.floor((Date.now() - observedAt) / 1000));
      const synced = age === null ? 'sync time unavailable' : `synced ${age < 60 ? `${age}s` : `${Math.floor(age / 60)}m`} ago`;
      const accountStale = age === null || age > 120;
      const status = words(a.status).toUpperCase();
      const detail = node('article', null, 'venue-terminal');
      detail.dataset.venue = normalizeVenue(venue.venue);
      const liveMetrics = capital.metrics ?? a.account_metrics ?? {};
      const metricKeys = ['balance', 'realized_pnl', 'unrealized_pnl', 'exposure', 'volume', 'fees'];
      const availableMetrics = metricKeys.filter(key => liveMetrics[key]?.value != null).length;
      const header = node('header', null, 'venue-terminal-header');
      const identity = node('div', null, 'venue-terminal-identity');
      const authenticated = a.status === 'authenticated_read_only';
      identity.append(node('h3', venue.venue), node('span', authenticated ? `${accountStale ? 'STALE · ' : ''}AUTHENTICATED · READ ONLY` : status, `venue-status-pill${authenticated && !accountStale ? ' is-live' : ''}`));
      identity.append(node('span', `${availableMetrics}/6 METRICS AVAILABLE`, 'venue-status-pill venue-reconciliation-pill'));
      const portfolioValue = a.account_metrics?.account_value?.value;
      const portfolioLabel = portfolioValue == null ? '' : ` · portfolio value ${dollars(portfolioValue)}`;
      header.append(identity, node('p', `${capital.scope ?? 'Reported account rows'}${portfolioLabel} · ${synced}${capital.more_positions ? ' · more positions available' : ''}`, 'venue-terminal-meta'));
      detail.append(header);

      const metricRail = node('dl', null, 'venue-metric-rail');
      const metrics = [
        ['Balance', liveMetrics.balance],
        ['Realized P&L', liveMetrics.realized_pnl],
        ['Unrealized P&L', liveMetrics.unrealized_pnl],
        ['Exposure', liveMetrics.exposure],
        ['Volume', liveMetrics.volume],
        ['Fees', liveMetrics.fees],
      ];
      for (const [label, metric] of metrics) {
        const cell = node('div', null, 'venue-metric');
        cell.append(node('dt', label));
        const value = metric?.value;
        const unavailable = value == null;
        const dd = node('dd', unavailable ? '—' : dollars(value), unavailable ? 'is-unavailable' : '');
        const provenance = accountStale && !unavailable ? 'STALE' : metric?.provenance;
        if (metric?.provenance) dd.title = `${provenance} · ${metric.provenance}${metric.reason ? ` · ${metric.reason}` : ''}`;
        if (label === 'Realized P&L' && !accountStale && !unavailable && ['VENUE_REPORTED', 'DERIVED_FROM_LIVE_RECORDS'].includes(metric.provenance)) {
          const numeric = capitalNumber(value);
          dd.dataset.pnl = numeric > 0 ? 'positive' : numeric < 0 ? 'negative' : 'flat';
        }
        cell.append(dd);
        if (unavailable) {
          const reason = metric?.reason ?? a.account_read?.reason ?? (a.status === 'authenticated_read_only'
            ? 'Authenticated source did not provide this metric.'
            : `Account source unavailable (${status.toLowerCase()}).`);
          cell.append(node('small', reason, 'venue-metric-reason'));
        } else if (metric?.provenance) {
          cell.append(node('small', `${accountStale && !unavailable ? 'STALE · ' : ''}${metric.provenance.replaceAll('_', ' ')}`, 'venue-metric-provenance'));
        }
        metricRail.append(cell);
      }
      detail.append(metricRail);

      const selectedSection = venueSections.get(detail.dataset.venue) ?? 'positions';
      const tabs = node('nav', null, 'venue-section-tabs');
      tabs.setAttribute('aria-label', `${venue.venue} account sections`);
      const positionEmpty = positions.length ? '' : !positionsComplete
        ? `Position source incomplete (${coverage.positions?.reason ?? 'authenticated position scan unavailable'}).`
        : context.market_id ? 'No positions for the selected market.' : 'No open positions';
      const positionsSection = table('Positions', ['Contract', 'Outcome / position', 'Realized P&L', 'Market value / exposure', 'Fees'], positions.map(row => [`${row.title ?? row.ticker ?? 'Contract unavailable'}${row.outcome ? ` · ${row.outcome}` : ''}`, row.position_fp ?? '—', row.realized_pnl_dollars == null ? '—' : dollars(row.realized_pnl_dollars), row.market_exposure_dollars == null && row.market_value_usd == null ? '—' : dollars(row.market_exposure_dollars ?? row.market_value_usd), row.fees_paid_dollars == null ? '—' : dollars(row.fees_paid_dollars)]), positionEmpty);
      if (!positions.length && positionsComplete) positionsSection.querySelector('.category-empty')?.after(node('small', `Live account synchronized ${synced.replace('synced ', '')}`, 'venue-empty-meta'));
      const fillsEmpty = fills.length ? '' : fillsComplete ? 'No fills in the complete live account activity history.' : 'Fill history incomplete; account-wide activity totals are withheld.';
      const ordersEmpty = orders.length ? '' : ordersComplete ? 'No open orders.' : 'Open-order source unavailable.';
      const sections = [
        ['positions', 'Positions', positionsSection],
        ['fills', 'Fills', venueFills(fills, fillsEmpty)],
        ['orders', 'Orders', table('Orders', ['Contract', 'Status', 'Filled', 'Remaining'], orders.map(row => [row.ticker ?? 'Contract unavailable', row.status ?? 'Status unavailable', row.fill_count_fp ?? 'Unavailable', row.remaining_count_fp ?? 'Unavailable']), ordersEmpty)],
        ['activity', 'Activity', venueActivity(fills, orders, fillsComplete && ordersComplete
          ? 'No activity in the complete live account records.'
          : 'Activity history is incomplete or unavailable.')],
      ];
      const content = node('div', null, 'venue-section-content');
      for (const [key, label, section] of sections) {
        const button = node('button', label.toUpperCase(), 'venue-section-tab');
        button.type = 'button'; button.setAttribute('aria-pressed', String(key === selectedSection));
        button.onclick = () => {
          venueSections.set(detail.dataset.venue, key);
          for (const tab of tabs.querySelectorAll('button')) tab.setAttribute('aria-pressed', String(tab.dataset.section === key));
          for (const panel of content.children) panel.hidden = panel.dataset.section !== key;
        };
        button.dataset.section = key;
        tabs.append(button);
        section.classList.add('venue-section'); section.dataset.section = key; section.hidden = key !== selectedSection;
        if (section.classList.contains('category-table-section')) section.classList.add('venue-table-section');
        content.append(section);
      }
      detail.append(tabs, content);
      venueRoot.append(detail);
    }
    const walletRoot = $('web3-live-details');
    const expandedWallets = new Set([...walletRoot.querySelectorAll('.web3-chain-card[data-account-id]')]
      .filter(element => element.querySelector('details')?.open).map(element => element.dataset.accountId));
    walletRoot.replaceChildren();
    const accounts = (state.accounts ?? []).filter(a => a.kind === 'wallet' && (!context.chain || a.account.chain === context.chain) && (!context.asset || a.unit === context.asset));
    for (const item of accounts) {
      const wallet = item.account;
      const contribution = state.canonical?.contributions?.find(row => row.id === item.id);
      const rows = (state.history?.points ?? []).flatMap(point => {
        const source = (point.contributions ?? []).find(row => row.id === item.id);
        const at = eventTime(point.at), value = source?.included ? capitalNumber(source.amount_usd) : null;
        return at === null ? [] : [{ at, value }];
      }).filter(point => point.at >= Date.now() - 86400000 && point.value !== null);
      const change = rows.length > 1 ? rows.at(-1).value - rows[0].value : null;
      const sourceStatus = displayStatus(contribution?.status ?? item.status.toUpperCase());
      const summary = card(`${item.label} · ${item.unit}`, `${item.amount === null ? 'Balance unavailable' : format(item.amount, item.unit)} · ${contribution?.included ? `${dollars(contribution.amount_usd)} included` : sourceStatus === 'UNPRICED' ? 'FUNDED · USD unpriced' : `${sourceStatus} · not included`}`, []);
      summary.classList.add('web3-chain-card');
      summary.dataset.accountId = item.id;
      summary.querySelector('.category-stats')?.remove();
      if (change !== null) {
        const trend = node('span', `24h · ${change > 0 ? '+' : ''}${dollars(change)} balance change`, 'web3-chain-trend');
        trend.dataset.sign = change > 0 ? 'positive' : change < 0 ? 'negative' : 'neutral';
        summary.append(trend);
      }
      const observedTokens = Array.isArray(wallet.tokens) ? wallet.tokens : [];
      if (observedTokens.length) {
        const visibleTokens = node('div', null, 'web3-token-preview');
        for (const token of observedTokens) {
          const symbol = token.symbol ?? token.asset ?? token.mint ?? 'Unknown asset';
          const amount = tokenAmount(token);
          const usd = capitalNumber(token.value_usd ?? token.amount_usd ?? token.usd_value);
          visibleTokens.append(node('span', `${symbol} ${amount}${usd === null ? ' · unpriced' : ` · ${dollars(usd)}`}`));
        }
        summary.append(visibleTokens);
      }
      const details = node('details', null, 'web3-chain-diagnostics');
      details.open = expandedWallets.has(item.id);
      details.append(node('summary', 'Diagnostics & token balances'));
      const diagnostics = stats([
        ['Source status', sourceStatus], ['Last observed', timeLabel(item.at)], ['Address', wallet.address ?? 'Unavailable'],
        ['Native USD mark', item.usd === null ? 'Unavailable' : dollars(item.usd)], ['Price source', wallet.native_valuation?.source ?? 'Unavailable'],
        ['24h source history', change === null ? 'Unknown · persisted history collecting' : `${change > 0 ? '+' : ''}${dollars(change)} · not P&L`],
        ['Realized P&L', 'Unknown · no attributed NOEMA wallet fills'], ['Unrealized P&L', 'Unknown · position attribution unavailable'],
        ['Executed volume', 'Unknown · no wallet execution ledger'], ['Trading exposure', 'Unknown · no attributed open positions'],
        ['Live execution authority', wallet.live_execution_enabled === true ? 'Enabled flag · action policy still applies' : 'Disabled'],
      ]);
      details.append(diagnostics);
      const tokens = Array.isArray(wallet.tokens) ? wallet.tokens : [];
      const tokenRows = node('div', null, 'web3-token-balances');
      tokenRows.append(node('h4', `Observed token balances · ${tokens.length}`));
      if (!tokens.length) tokenRows.append(node('p', 'No token account rows returned by this source.', 'category-empty'));
      for (const token of tokens) {
        const symbol = token.symbol ?? token.asset ?? token.mint ?? 'Unknown asset';
        const amount = tokenAmount(token);
        const usd = capitalNumber(token.value_usd ?? token.amount_usd ?? token.usd_value);
        tokenRows.append(node('div', `${symbol} · ${amount} · ${usd === null ? 'USD unpriced' : dollars(usd)}`));
      }
      details.append(tokenRows); summary.append(details); walletRoot.append(summary);
    }
    const strategies = state.snapshot?.sections?.experiments?.rows ?? [];
    const strategyRoot = $('strategy-live-details'); strategyRoot.replaceChildren();
    const filtered = strategies.filter(row => !context.strategy_id || row.trial_id === context.strategy_id).filter(row => !context.market_id || row.market_id === context.market_id);
    if (!filtered.length) strategyRoot.append(node('p', 'No strategy with an exact persisted link to this selection. LIVE PERFORMANCE UNAVAILABLE.', 'category-empty'));
    for (const strategy of filtered.slice(0, 24)) {
      const contribution = state.economics?.canonical_ledger?.contribution_by_strategy?.find(row => row.key === strategy.trial_id);
      const realized = dollars(contribution?.reconciled_event_contribution_subtotal_usd);
      const decisions = (state.snapshot?.sections?.decisions?.rows ?? []).filter(row => row.strategy_id === strategy.trial_id || row.trial_id === strategy.trial_id);
      const detail = card(strategy.trial_id, `${words(strategy.status)} · ${strategy.hypothesis ?? 'Hypothesis unavailable'}`, [
        ['Live capital allocated', 'Unknown'], ['Reconciled contribution subtotal', realized], ['Live realized P&L', 'Unknown'], ['Live unrealized P&L', 'Unknown'], ['Live volume', 'Unknown'], ['Linked decisions in loaded window', String(decisions.length)], ['Live win / loss', 'Unknown'], ['Expected value', 'Unknown'], ['Live drawdown', 'Unknown'],
      ]);
      const select = node('button', 'Inspect strategy', 'category-select'); select.type = 'button';
      select.onclick = () => onSelect({ type: 'experiment', id: `experiment:${strategy.trial_id}`, strategy_id: strategy.trial_id, trial_id: strategy.trial_id, label: strategy.trial_id, status: strategy.status, source: 'Persisted strategy experiment', record: strategy });
      detail.append(select); strategyRoot.append(detail);
    }
    const assets = $('asset-live-details'); assets.replaceChildren();
    for (const symbol of ['BTC', 'ETH', 'POL', 'SOL']) {
      const holdings = (state.accounts ?? []).filter(account => account.unit === symbol), quoted = holdings.find(account => account.account.native_valuation?.price_usd != null);
      const mark = quoted?.account.native_valuation;
      const assetCard = card(symbol, `${mark?.source ?? 'LIVE PRICE UNAVAILABLE'} · ${timeLabel(eventTime(mark?.observed_at))}`, [
        ['Reference price', dollars(mark?.price_usd)], ['Observed balance', holdings.length && holdings.every(account => account.amount !== null) ? format(holdings.reduce((sum, account) => sum + account.amount, 0), symbol) : 'Unknown'],
        ['Realized P&L', 'Unknown'], ['Unrealized P&L', 'Unknown'], ['Executed volume', 'Unknown'], ['Exposure', 'Unknown'],
      ]);
      const button = node('button', `Select ${symbol}`, 'category-select'); button.type = 'button'; button.onclick = () => onSelect({ type: 'asset', id: `asset:${symbol}`, asset: symbol, label: symbol, status: mark ? 'observed' : 'unknown', source: mark?.source ?? 'Price unavailable', record: { asset: symbol, ...mark } });
      assetCard.append(button); assets.append(assetCard);
    }
    [...document.querySelectorAll('.category-table-scroll')].forEach((el, index) => {
      if (scrolls[index]) { el.scrollTop = scrolls[index][0]; el.scrollLeft = scrolls[index][1]; }
    });
    if (focusedText) [...document.querySelectorAll('.category-select')].find(el => el.textContent === focusedText)?.focus({ preventScroll: true });
  }
  show(active); revealHash();
  setInterval(() => { if (!document.hidden) freshness(); }, 15000);
  return { update(value) { state = value; render(); }, select(value) { context = value; render(); }, show };
}
