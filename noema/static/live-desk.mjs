import { recordedFeed, feedDifference, filterFeed, filterFeedCount, feedHistogram, eventTime, contextFeed } from './workstation-feed.mjs';

const node = (tag, text, className) => {
  const item = document.createElement(tag);
  if (text != null) item.textContent = text;
  if (className) item.className = className;
  return item;
};
const clock = (at) => at === null ? 'Time unknown' : new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const date = (at) => at === null ? 'Time unknown' : new Date(at).toLocaleString();

export function createLiveDesk({ onSelect, onPauseChange }) {
  const $ = (id) => document.getElementById(id);
  let events = null, filter = 'all', paused = false, pending = false;
  let asOf = null, stale = false, transport = 'connecting', lastSignature = null;
  let recentDelta = null;
  let selectedKey = null;
  let context = null, sourceSnapshot = null, sources = {};
  const host = $('live-feed');

  function status() {
    const age = asOf === null ? null : Math.max(0, Math.floor((Date.now() - asOf) / 1000));
    const ageText = age === null ? 'Source time unknown' : age < 60 ? `Snapshot ${age}s ago` : `Snapshot ${Math.floor(age / 60)}m ago`;
    $('display-status').textContent = paused ? `Display paused${pending ? ' · updates waiting' : ''}`
      : stale ? 'Snapshot unavailable · showing retained records' : events === null ? 'Waiting for operating data' : ageText;
    $('display-status').dataset.state = paused || stale ? 'attention' : 'current';
    $('sync-state').textContent = paused ? 'DISPLAY PAUSED' : transport === 'connected' ? 'STREAM CONNECTED'
      : transport === 'unavailable' ? 'POLLING · STREAM UNAVAILABLE' : 'STREAM RECONNECTING';
    $('sync-state').dataset.state = paused ? 'paused' : transport === 'connected' ? 'live' : 'reconnecting';
    $('console-freshness').textContent = transport === 'connected' ? 'CONNECTED' : transport === 'unavailable' ? 'POLLING · STREAM UNAVAILABLE' : 'RECONNECTING';
    $('console-freshness').dataset.state = transport === 'connected' ? 'connected' : transport === 'unavailable' ? 'stale' : 'unknown';
    $('feed-transport').textContent = paused ? 'Reading snapshot · runtime continues' : 'Event updates · 15s fallback';
  }

  function render() {
    if (events === null) return;
    const scoped = contextFeed(events, context);
    const visible = filterFeed(scoped, filter, $('feed-search').value).slice(0, 120);
    for (const button of document.querySelectorAll('[data-feed-filter]')) {
      const label = button.dataset.feedFilter === 'all' ? 'All' : button.dataset.feedFilter === 'p&l' ? 'P&L'
        : button.dataset.feedFilter[0].toUpperCase() + button.dataset.feedFilter.slice(1);
      let count = button.querySelector('.feed-filter-count');
      if (!count) { count = node('span', null, 'feed-filter-count'); button.append(count); }
      count.textContent = String(filterFeedCount(scoped, button.dataset.feedFilter, $('feed-search').value));
      button.setAttribute('aria-label', `${label} ${count.textContent}`);
    }
    const signature = JSON.stringify(visible.map((event) => [event.key, event.title, event.detail, event.actor, event.status, event.at, event.repeatCount]));
    $('feed-count').textContent = String(visible.length);
    const represented = events.reduce((count, event) => count + (event.repeatCount ?? 1), 0);
    $('feed-window').textContent = `${visible.length} shown · ${represented} persisted records represented · ${context && context.type !== 'core' ? context.label : 'all activity'} · priority, newest within category`;
    if (signature === lastSignature) return;
    lastSignature = signature;
    const scroll = host.scrollTop;
    const anchor = scroll > 0 ? [...host.children].find((item) => item.getBoundingClientRect().bottom > host.getBoundingClientRect().top) : null;
    const anchorKey = anchor?.querySelector('button')?.dataset.eventKey;
    const anchorOffset = anchor ? anchor.getBoundingClientRect().top - host.getBoundingClientRect().top : 0;
    const focusedKey = host.contains(document.activeElement) ? document.activeElement.dataset.eventKey : null;
    const fragment = document.createDocumentFragment();
    for (const event of visible) {
      const item = node('li', null, 'feed-event');
      item.dataset.tone = event.tone;
      const button = node('button'); button.type = 'button'; button.dataset.eventKey = event.key;
      button.setAttribute('aria-pressed', String(event.key === selectedKey));
      const meta = node('span', null, 'feed-event-meta');
      const time = node('time', clock(event.at));
      if (event.at !== null) { time.dateTime = new Date(event.at).toISOString(); time.title = date(event.at); }
      meta.append(node('span', event.category), time);
      button.append(meta, node('strong', event.title), node('span', event.detail, 'feed-event-detail'));
      const foot = node('span', null, 'feed-event-foot');
      foot.append(node('span', event.actor), node('span', event.status.replaceAll('_', ' '), 'event-status'));
      if (event.repeatCount > 1) foot.append(node('span', `${event.repeatCount} matching PASS records · raw evidence below`, 'event-status'));
      button.append(foot);
      button.onclick = () => {
        selectedKey = event.key;
        for (const entry of host.querySelectorAll('button')) entry.setAttribute('aria-pressed', String(entry.dataset.eventKey === selectedKey));
        onSelect(event.context);
        document.querySelector('[data-inspector-tab="overview"]').click();
        $('reader-heading').scrollIntoView({ block: 'nearest', behavior: 'instant' });
      };
      item.append(button);
      if (event.repeatCount > 1 && Array.isArray(event.records)) {
        const raw = node('details', null, 'feed-raw-evidence');
        raw.append(node('summary', `Expand ${event.repeatCount} immutable forecast records`));
        const list = node('ul');
        for (const record of event.records) {
          list.append(node('li', `${record.market_id ?? 'Market unknown'} · ${record.venue ?? 'venue unknown'} · ${record.decision ?? 'decision unknown'} · ${record.reason ?? 'reason unavailable'} · ${date(eventTime(record.created_at))}`));
        }
        raw.append(list); item.append(raw);
      }
      fragment.append(item);
    }
    if (!visible.length) fragment.append(node('li', events.length ? 'No recorded events match this filter.' : 'No events recorded in the loaded snapshot.', 'feed-empty'));
    host.replaceChildren(fragment);
    host.scrollTop = scroll;
    if (anchorKey) {
      const target = [...host.querySelectorAll('button')].find((button) => button.dataset.eventKey === anchorKey)?.parentElement;
      if (target) host.scrollTop += target.getBoundingClientRect().top - host.getBoundingClientRect().top - anchorOffset;
    }
    if (focusedKey) [...host.querySelectorAll('button')].find((button) => button.dataset.eventKey === focusedKey)?.focus({ preventScroll: true });
  }

  function histogram() {
    const chart = $('feed-activity-chart'), data = feedHistogram(events);
    chart.replaceChildren();
    if (!data) { chart.append(node('span', 'No timestamped records', 'feed-chart-empty')); return; }
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 240 55'); svg.setAttribute('aria-hidden', 'true');
    const max = Math.max(...data.buckets, 1);
    data.buckets.forEach((count, index) => {
      const rect = document.createElementNS(svg.namespaceURI, 'rect');
      const height = count / max * 46;
      for (const [key, value] of Object.entries({ x: index * 12, y: 50 - height, width: 7, height, rx: 2 })) rect.setAttribute(key, value);
      svg.append(rect);
    });
    chart.append(svg);
    chart.setAttribute('aria-label', `${events.filter((event) => event.at !== null).length} loaded timestamped records, from ${date(data.start)} to ${date(data.end)}. Bounded history, not total throughput.`);
    const sameDay = new Date(data.start).toDateString() === new Date(data.end).toDateString();
    const shortDate = (at) => new Date(at).toLocaleString([], { month: 'short', day: 'numeric' });
    $('feed-activity-range').textContent = sameDay ? `${clock(data.start)}–${clock(data.end)} · loaded records`
      : `${shortDate(data.start)}–${shortDate(data.end)} · loaded records`;
    $('feed-activity-range').title = `${date(data.start)} to ${date(data.end)}`;
  }

  for (const button of document.querySelectorAll('[data-feed-filter]')) {
    button.onclick = () => {
      filter = button.dataset.feedFilter;
      for (const option of document.querySelectorAll('[data-feed-filter]')) option.setAttribute('aria-pressed', String(option === button));
      render(); host.scrollTop = 0;
    };
  }
  $('feed-search').oninput = () => { render(); host.scrollTop = 0; };
  $('feed-view-toggle').onclick = () => {
    const terminal = $('activity-desk').dataset.view !== 'terminal';
    $('activity-desk').dataset.view = terminal ? 'terminal' : 'cards';
    $('feed-view-toggle').textContent = terminal ? 'Card view' : 'Terminal view';
    $('feed-view-toggle').setAttribute('aria-pressed', String(terminal));
  };
  $('pause-updates').onclick = () => {
    paused = !paused;
    $('pause-updates').setAttribute('aria-pressed', String(paused));
    $('pause-updates').textContent = paused ? '▶ Resume updates' : 'Ⅱ Pause updates';
    document.body.dataset.paused = String(paused);
    onPauseChange(paused); status();
  };
  setInterval(() => { if (!document.hidden) status(); }, 1000);
  return {
    setContext(value) { context = value; render(); },
    updateSources(value) { sources = { ...sources, ...value }; if (sourceSnapshot) this.update(sourceSnapshot); },
    get paused() { return paused; },
    markPending() { pending = true; status(); },
    setTransport(value) { transport = value; status(); },
    markStale() { stale = true; status(); },
    update(snapshot) {
      if (paused) { pending = true; status(); return; }
      sourceSnapshot = snapshot;
      const next = recordedFeed(snapshot, sources), diff = feedDifference(events, next);
      events = next; asOf = eventTime(snapshot.as_of); stale = false; pending = false;
      $('brief-title').textContent = events[0]?.title ?? 'No recorded developments';
      $('brief-detail').textContent = events[0]?.detail ?? 'The current snapshot contains no recorded events.';
      $('brief-time').textContent = events[0] ? `${events[0].actor} · ${date(events[0].at)}` : 'Waiting for recorded work';
      if (diff.initial) {
        recentDelta = null;
        $('brief-delta').textContent = 'Initial snapshot';
        $('brief-delta-note').textContent = `${events.length} recorded events loaded.`;
      } else if (diff.added || diff.updated) {
        recentDelta = {
          text: diff.added ? `+${diff.added} new` : `${diff.updated} updated`,
          note: `${diff.added} new · ${diff.updated} updated in the loaded window.`,
          expiresAt: Date.now() + 5000,
        };
        $('brief-delta').textContent = recentDelta.text;
        $('brief-delta-note').textContent = recentDelta.note;
      } else if (recentDelta && Date.now() < recentDelta.expiresAt) {
        $('brief-delta').textContent = recentDelta.text;
        $('brief-delta-note').textContent = recentDelta.note;
      } else {
        recentDelta = null;
        $('brief-delta').textContent = 'No changes';
        $('brief-delta-note').textContent = '0 new · 0 updated in the loaded window.';
      }
      render(); histogram(); status();
    },
  };
}
