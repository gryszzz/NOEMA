import { eventTime } from './workstation-feed.mjs';

export function capitalNumber(value) {
  if ((typeof value !== 'string' && typeof value !== 'number') || String(value).trim() === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}
export function agentBalanceState(current, historyFailed = false) {
  if (historyFailed || Number(current?.stale_sources ?? 0) > 0 || current?.status === 'STALE') return 'STALE';
  if (capitalNumber(current?.amount_usd) === null) return 'UNAVAILABLE';
  // The current projector excludes positions, unpriced tokens, and liabilities.
  return 'UNRECONCILED';
}
const chains = [['solana', 'Solana', 'SOL'], ['ethereum', 'Ethereum', 'ETH'], ['base', 'Base', 'ETH'], ['polygon', 'Polygon', 'POL'], ['bitcoin', 'Bitcoin', 'BTC']];
export const timeLabel = (at) => at === null ? 'Source time unknown' : new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
export const format = (value, unit = 'USD') => value === null ? 'Unknown' : unit === 'USD'
  ? new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(value)
  : `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 8 }).format(value)} ${unit}`;
function exactDecimal(value) {
  const raw = String(value ?? '').trim();
  if (!/^-?\d+(?:\.\d+)?$/.test(raw)) return null;
  const negative = raw.startsWith('-'), [wholeRaw, fractionRaw = ''] = raw.replace(/^-/, '').split('.');
  const whole = wholeRaw.replace(/^0+(?=\d)/, '').replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const fraction = fractionRaw.replace(/0+$/, '');
  return `${negative ? '-' : ''}${whole}${fraction ? `.${fraction}` : ''}`;
}
export function formatWalletBalance(wallet, unit) {
  const raw = wallet?.sol ?? wallet?.native_balance ?? wallet?.btc;
  const amount = exactDecimal(raw);
  return amount === null ? format(null, unit) : `${amount} ${unit}`;
}
export function tokenAmount(token) {
  for (const key of ['ui_amount', 'amount', 'balance', 'quantity']) {
    const value = exactDecimal(token?.[key]);
    if (value !== null) return value;
  }
  try {
    const decimals = Number(token?.decimals), raw = String(token?.raw_amount ?? '');
    if (!Number.isInteger(decimals) || decimals < 0 || decimals > 255 || !/^\d+$/.test(raw)) return 'Unknown';
    const atomic = BigInt(raw), scale = 10n ** BigInt(decimals), whole = atomic / scale;
    const fraction = decimals ? (atomic % scale).toString().padStart(decimals, '0').replace(/0+$/, '') : '';
    return `${whole.toString()}${fraction ? `.${fraction}` : ''}`;
  } catch { return 'Unknown'; }
}
const status = (readable, at, failed, now) => !readable ? 'Unavailable' : failed || at === null || now - at > 120000 || at > now + 5000 ? 'Stale' : 'Observed';
const displayStatus = value => value === 'CACHED' ? 'LIVE' : value;

export function capitalAccounts(venues, wallets, failures = {}, now = Date.now()) {
  const accounts = ['Kalshi', 'Polymarket US'].map((name) => {
    const venue = venues?.venues?.find((item) => item.venue === name), account = venue?.account ?? {};
    const amount = capitalNumber(account.cash_balance_usd), at = eventTime(account.observed_at ?? venues?.as_of);
    const readable = account.status === 'authenticated_read_only' && amount !== null;
    return { id: `venue:${name}`, label: name, unit: 'USD', amount: readable ? amount : null, at,
      status: status(readable, at, failures.venues, now), usd: readable ? amount : null, kind: 'venue', account,
      detail: readable ? 'Account cash · read only' : String(account.status ?? 'Account data unavailable').replaceAll('_', ' ') };
  });
  for (const [chain, name, unit] of chains) {
    const wallet = wallets?.networks?.find((item) => item.chain === chain) ?? {};
    const amount = capitalNumber(wallet.sol ?? wallet.native_balance ?? wallet.btc);
    const at = eventTime(wallet.observed_at ?? wallets?.observed_at);
    const readable = (wallet.readable === true || String(wallet.status ?? '').startsWith('read_only')) && amount !== null;
    accounts.push({ id: `wallet:${chain}:${wallet.address || 'unknown'}`, label: name, unit, amount: readable ? amount : null, at,
      status: status(readable, at, failures.wallets, now), usd: readable ? capitalNumber(wallet.native_value_usd) : null, kind: 'wallet', account: wallet,
      detail: wallet.address ? `${wallet.address.slice(0, 6)}…${wallet.address.slice(-4)}` : 'No wallet observation' });
  }
  return accounts;
}

const node = (tag, text, className) => {
  const item = document.createElement(tag);
  if (text != null) item.textContent = text;
  if (className) item.className = className;
  return item;
};
const svgNode = (tag, attrs, text) => {
  const item = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) item.setAttribute(key, value);
  if (text != null) item.textContent = text;
  return item;
};

export function createLiveCapital({ isPaused, onSelect, onWindowChange }) {
  const $ = (id) => document.getElementById(id), histories = new Map();
  let venues, wallets, economics, balanceHistory, gateway, accounts = [], selected = null, windowName = '24H';
  const failures = { venues: false, wallets: false, economics: false, gateway: false, history: false };
  function select(id) {
    selected = id;
    const account = accounts.find((item) => item.id === id);
    onSelect({ type: account.kind === 'wallet' ? 'wallet' : 'venue', id, label: account.label,
      status: account.status, source: 'Live account observation', record: account.account,
      venue: account.kind === 'venue' ? account.label : undefined,
      chain: account.kind === 'wallet' ? account.account.chain : undefined });
    render();
  }
  for (const button of document.querySelectorAll('[data-capital-window]')) button.onclick = () => {
    windowName = button.dataset.capitalWindow;
    for (const option of document.querySelectorAll('[data-capital-window]')) option.setAttribute('aria-pressed', String(option === button));
    render(); onWindowChange(windowName);
  };

  function chart(account) {
    const duration = { '1H': 3600000, '24H': 86400000, '7D': 604800000, '30D': 2592000000, ALL: Infinity }[windowName];
    const points = (histories.get(account.id) ?? []).filter((point) => point.at >= Date.now() - duration);
    const valid = points.filter((point) => point.value !== null);
    $('capital-chart-title').textContent = `${windowName} · known live cash + native-asset subtotal`;
    const first = valid[0], last = valid.at(-1);
    const change = first && last && valid.length > 1 && valid.every(point => point.scope === last.scope) ? last.value - first.value : null;
    $('capital-chart-change').textContent = change === null ? `${account.status} · ${timeLabel(account.at)}`
      : `${change > 0 ? '+' : ''}${format(change, account.unit)} balance change · ${account.status.toLowerCase()}`;
    $('capital-chart-change').dataset.sign = change === null || change === 0 ? 'neutral' : change > 0 ? 'positive' : 'negative';
    const host = $('capital-chart'); host.replaceChildren();
    $('capital-chart-caption').textContent = `${valid.length} chart samples / ${balanceHistory?.sample_count ?? 0} persisted live observations · ${windowName} · ${balanceHistory?.history_status ?? 'history unavailable'}. Cash and native-asset subtotal only; open positions, unpriced assets, and liabilities are excluded. This is not Agent Balance or P&L.`;
    host.setAttribute('aria-label', `${account.label} balance history: ${valid.length} actual observations in ${account.unit}. ${account.status}.`);
    if (!valid.length) {
      const empty = node('div', null, 'capital-chart-empty');
      empty.append(node('span', '↗', 'capital-empty-mark'), node('strong', 'Waiting for account value observations'), node('span', 'The chart starts with the first successful balance read.'));
      host.append(empty); return;
    }
    const width = 800, height = 150, left = 90, right = 18, top = 12, bottom = 25;
    const values = valid.map((point) => point.value), min = Math.min(...values), max = Math.max(...values);
    const pad = Math.max((max - min) * .18, Math.abs(max) * .002, account.unit === 'USD' ? .01 : .00000001);
    const lo = min - pad, hi = max + pad;
    const start = points[0].at, end = points.at(-1).at;
    const x = (at) => end === start ? width / 2 : left + (at - start) / (end - start) * (width - left - right);
    const y = (value) => top + (hi - value) / (hi - lo) * (height - top - bottom);
    const svg = svgNode('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': `${account.label} observed balance over time` });
    for (let i = 0; i < 3; i++) {
      const value = lo + (hi - lo) * i / 2, at = y(value);
      svg.append(svgNode('path', { d: `M${left} ${at}H${width - right}`, class: 'capital-gridline' }));
      svg.append(svgNode('text', { x: left - 12, y: at + 4, 'text-anchor': 'end', class: 'capital-axis' }, format(value, account.unit)));
    }
    let d = '', connected = false, prev = null;
    for (const point of points) {
      if (point.value === null) { connected = false; continue; }
      // Leave visible gaps when source observations were unavailable for >2 minutes.
      if (prev && (point.at - prev.at > 120000 || point.scope !== prev.scope)) connected = false;
      d += `${connected ? 'L' : 'M'}${x(point.at)} ${y(point.value)} `; connected = true; prev = point;
    }
    svg.append(svgNode('path', { d, class: 'capital-line' }));
    for (const point of valid) {
      const dot = svgNode('circle', { cx: x(point.at), cy: y(point.value), r: point === last ? 4 : 2, class: 'capital-point' });
      dot.append(svgNode('title', {}, `${new Date(point.at).toLocaleString()} · ${format(point.value, account.unit)}`)); svg.append(dot);
    }
    svg.append(svgNode('text', { x: left, y: height - 2, class: 'capital-axis' }, timeLabel(start)));
    if (end !== start) svg.append(svgNode('text', { x: width - right, y: height - 2, 'text-anchor': 'end', class: 'capital-axis' }, timeLabel(end)));
    host.append(svg);
  }

  function cards(kind, id) {
    const host = $(id), focus = host.contains(document.activeElement) ? document.activeElement.closest('[data-account-id]')?.dataset.accountId : null;
    const openDetails = new Set([...host.querySelectorAll('article[data-account-id]')]
      .filter(card => card.querySelector('details')?.open).map(card => card.dataset.accountId));
    host.replaceChildren();
    for (const account of accounts.filter((item) => item.kind === kind)) {
      const article = node('article', null, `capital-account ${kind === 'wallet' ? 'capital-wallet' : ''}`);
      article.dataset.accountId = account.id; article.dataset.state = account.status.toLowerCase();
      article.dataset.selected = String(selected === account.id);
      const card = node('button', null, 'capital-account-main');
      card.type = 'button'; card.dataset.accountId = account.id;
      card.setAttribute('aria-pressed', String(selected === account.id));
      const contribution = balanceHistory?.current?.contributions?.find(row => row.id === account.id);
      const sourceStatus = displayStatus(contribution?.status ?? account.status.toUpperCase());
      const heading = node('span', null, 'capital-account-name');
      heading.append(node('span', account.label), node('small', sourceStatus === 'UNPRICED' && account.amount > 0 ? 'FUNDED · UNPRICED' : sourceStatus));
      card.append(heading, node('strong', kind === 'wallet' ? formatWalletBalance(account.account, account.unit) : format(account.amount, account.unit)));
      if (kind === 'wallet') {
        card.append(node('span', contribution?.included ? `${format(capitalNumber(contribution.amount_usd))} included in known subtotal` : contribution?.status === 'UNPRICED' ? 'FUNDED · USD unpriced' : `${sourceStatus} · value excluded`, 'capital-usd-contribution'));
        const points = histories.get(account.id) ?? [], recent = points.filter(point => point.at >= Date.now() - 86400000 && point.value !== null);
        const delta = recent.length > 1 ? recent.at(-1).value - recent[0].value : null;
        const trace = sparkline(recent, account.label);
        if (trace) card.append(trace);
        const details = node('details', null, 'capital-chain-diagnostics');
        details.open = openDetails.has(account.id);
        details.append(node('summary', 'Source details'));
        const authority = account.account.live_execution_enabled === true
          ? 'Execution setting enabled · per-action authority still applies' : 'Live execution disabled';
        const rows = node('dl', null, 'capital-diagnostic-list');
        for (const [label, value] of [
          ['Source status', sourceStatus], ['Address', account.account.address ?? 'Unavailable'],
          ['Last observed', account.at === null ? 'Source time unknown' : new Date(account.at).toLocaleString()],
          ['24h balance change', delta === null ? 'Unknown · history collecting' : `${delta > 0 ? '+' : ''}${format(delta)} · balance change, not P&L`],
          ['Realized P&L', 'Unknown · no attributed NOEMA wallet fills'],
          ['Unrealized P&L', 'Unknown · position attribution unavailable'],
          ['Trading volume', 'Unknown · no wallet execution ledger'], ['Live authority', authority],
        ]) { const row = node('div', null, 'capital-diagnostic-row'); row.append(node('dt', label), node('dd', value)); rows.append(row); }
        details.append(rows);
        const tokens = Array.isArray(account.account.tokens) ? account.account.tokens : [];
        const tokenPreview = node('div', null, 'capital-token-inline');
        for (const token of tokens) {
          const symbol = token.symbol ?? token.asset ?? token.mint ?? 'Unknown asset';
          const amount = tokenAmount(token);
          const usd = capitalNumber(token.value_usd ?? token.amount_usd ?? token.usd_value);
          tokenPreview.append(node('span', `${symbol} ${amount}${usd === null ? ' · unpriced' : ` · ${format(usd)}`}`));
        }
        article.append(card);
        if (tokens.length) article.append(tokenPreview);
        const tokenList = node('div', null, 'capital-token-list');
        tokenList.append(node('strong', `Token balances · ${tokens.length}`));
        if (!tokens.length) tokenList.append(node('span', 'No token account rows reported by this source.'));
        for (const token of tokens) {
          const symbol = token.symbol ?? token.asset ?? token.mint ?? 'Unknown asset';
          const amount = tokenAmount(token);
          const usd = capitalNumber(token.value_usd ?? token.amount_usd ?? token.usd_value);
          tokenList.append(node('span', `${symbol} · ${amount} · ${usd === null ? 'USD unpriced' : format(usd)}`));
        }
        details.append(tokenList); article.append(details);
      } else {
        card.append(node('span', account.detail, 'capital-account-detail'));
        card.append(node('span', `Buying power · ${format(capitalNumber(account.account.buying_power_usd))}`, 'capital-account-meta'));
        card.append(node('span', `Activity · ${account.account.fills ?? account.account.activity_records ?? 'Unknown'} account fill/activity records`, 'capital-account-meta'));
        card.append(node('span', 'Authority · read-only account observation · NOEMA live execution disabled', 'capital-account-meta'));
        article.append(card);
      }
      card.title = `${account.label} · ${account.status} · ${account.at === null ? 'Source time unknown' : new Date(account.at).toLocaleString()}`;
      card.onclick = () => select(account.id); host.append(article);
    }
    if (focus) [...host.querySelectorAll('button[data-account-id]')].find((card) => card.dataset.accountId === focus)?.focus({ preventScroll: true });
  }

  function sparkline(points, label) {
    const values = points.map(point => point.value).filter(value => value !== null);
    if (values.length < 2) return null;
    const min = Math.min(...values), max = Math.max(...values), span = max - min || Math.max(Math.abs(max) * .001, .00000001);
    let path = '', previousValid = false;
    points.forEach((point, index) => {
      if (point.value === null) { previousValid = false; return; }
      path += `${previousValid ? 'L' : 'M'}${index / Math.max(points.length - 1, 1) * 100} ${18 - (point.value - min) / span * 16} `;
      previousValid = true;
    });
    const svg = svgNode('svg', { viewBox: '0 0 100 20', role: 'img', 'aria-label': `${label} persisted USD balance observations` });
    svg.append(svgNode('path', { d: path, class: 'capital-sparkline-line' }));
    return svg;
  }

  function render() {
    accounts = capitalAccounts(venues, wallets, failures);
    if (balanceHistory) {
      const bySource = new Map();
      for (const point of histories.get('overall') ?? []) for (const row of point.contributions) {
        if (!bySource.has(row.id)) bySource.set(row.id, []);
        bySource.get(row.id).push({ at: point.at, value: row.included ? capitalNumber(row.amount_usd) : null });
      }
      for (const account of accounts) histories.set(account.id, bySource.get(account.id) ?? []);
    }
    const canonical = balanceHistory?.current ?? {};
    const overall = { id: 'overall', label: 'Known live cash + native assets subtotal', unit: 'USD', amount: capitalNumber(canonical.amount_usd),
      at: eventTime(canonical.observed_at), scope: canonical.scope,
      status: agentBalanceState(canonical, failures.history) };
    const coverage = `${canonical.valued_sources ?? 0}/${canonical.expected_sources ?? 0} sources valued`;
    const subtotal = overall.amount === null ? 'No verified current subtotal' : `Known live cash + native assets subtotal ${format(overall.amount)}`;
    const provenance = (canonical.contributions ?? []).filter(row => row.included === true)
      .map(row => row.label ?? row.id).join(', ') || 'no included sources';
    $('capital-balance').textContent = overall.status === 'UNAVAILABLE' ? 'UNAVAILABLE'
      : overall.status === 'STALE' ? 'STALE' : overall.status === 'UNRECONCILED' ? 'UNRECONCILED' : format(overall.amount);
    $('capital-balance').dataset.state = overall.status.toLowerCase();
    $('capital-balance-note').textContent = `${subtotal} · sources: ${provenance} · ${coverage} · excludes open positions, unpriced token assets, and unreconciled liabilities · as of ${overall.at === null ? 'unknown' : new Date(overall.at).toLocaleString()}`;
    $('capital-pnl').textContent = 'UNRECONCILED';
    $('capital-pnl').dataset.state = 'unreconciled';
    $('capital-pnl').dataset.sign = 'neutral';
    $('capital-pnl-note').textContent = `No authoritative live NET P&L: fills, open positions, settlements, fees, funding, and operating costs are not fully reconciled${failures.economics ? ' · economic source read stale' : ''}.`;
    const contributions = canonical.contributions ?? [];
    const subtotalFor = predicate => {
      const rows = contributions.filter(row => predicate(row) && row.included === true && capitalNumber(row.amount_usd) !== null);
      return { amount: rows.length ? rows.reduce((sum, row) => sum + capitalNumber(row.amount_usd), 0) : null, rows };
    };
    const walletsSubtotal = subtotalFor(row => row.id.startsWith('wallet:'));
    const venuesSubtotal = subtotalFor(row => row.id.startsWith('venue:'));
    const cashSubtotal = subtotalFor(row => row.id.startsWith('venue:') || row.id === 'revenue:stripe');
    const showSubtotal = (id, subtotal, missing) => {
      $(id).textContent = format(subtotal.amount);
      $(`${id}-note`).textContent = subtotal.amount === null ? missing : `${subtotal.rows.length} valued source${subtotal.rows.length === 1 ? '' : 's'} · partial where sources are unpriced or stale`;
    };
    showSubtotal('capital-available', cashSubtotal, 'No current venue or Stripe cash observations');
    showSubtotal('capital-onchain', walletsSubtotal, 'No current valued wallet observations');
    showSubtotal('capital-venue-value', venuesSubtotal, 'No current valued venue balances');
    const kalshi = accounts.find((item) => item.label === 'Kalshi'), capital = kalshi.account.capital;
    $('capital-volume').textContent = format(capitalNumber(capital?.observed_volume_usd));
    $('capital-volume-note').textContent = capital?.observed_volume_usd != null ? `Kalshi · ${capital.position_rows} reported positions${capital.more_positions ? ' · partial page' : ''} · ${kalshi.status.toLowerCase()}` : 'No observed execution volume';
    $('capital-volume-note').title = capital?.scope ?? 'Executed volume is unavailable. Research activity is not trading volume.';
    $('capital-wallet-asof').textContent = `${failures.wallets ? 'Retained · ' : ''}${timeLabel(eventTime(wallets?.observed_at))} · 15s source cache`;
    $('capital-exposure').textContent = format(capitalNumber(capital?.reported_exposure_usd));
    $('capital-exposure-note').textContent = capital?.reported_exposure_usd != null ? `Kalshi reported position subtotal · owner attribution unavailable · ${kalshi.status.toLowerCase()}` : 'Live position data unavailable';
    $('capital-net-flows').textContent = 'Unknown';
    const change24 = failures.history ? null : capitalNumber(balanceHistory?.change_24h_usd);
    $('capital-change24').textContent = format(change24);
    $('capital-change24-note').textContent = change24 === null ? '24h comparable history unavailable' : 'Balance change · includes flows and market marks; not P&L';
    cards('venue', 'capital-venue-list'); cards('wallet', 'capital-wallet-list');
    chart(overall);
  }
  render();
  setInterval(() => { if (!document.hidden && !isPaused()) render(); }, 15000);
  return {
    update(source, payload) {
      if (isPaused()) return;
      if (source === 'venues') venues = payload;
      if (source === 'wallets') wallets = payload;
      if (source === 'economics') economics = payload;
      if (source === 'gateway') gateway = payload;
      failures[source] = false;
      if (source === 'history') {
        balanceHistory = payload;
        histories.set('overall', (payload.points ?? []).map(point => ({ at: eventTime(point.at), value: capitalNumber(point.amount_usd), scope: point.scope, contributions: point.contributions ?? [] })).filter(point => point.at !== null));
      }
      render();
    },
    fail(source) { failures[source] = true; render(); },
    render,
    get accounts() { return accounts; },
    get history() { return balanceHistory; },
    get canonical() { return balanceHistory?.current; },
    get failedSources() { return Object.keys(failures).filter(source => failures[source]); },
    get window() { return windowName; },
  };
}
