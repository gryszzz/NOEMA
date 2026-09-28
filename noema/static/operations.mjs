const $ = (id) => document.getElementById(id);
const views = {
  queue: ['Research queue', ['market_id', 'status', 'priority', 'request']],
  specialists: ['Specialists', ['name', 'family', 'state', 'resolved', 'calibration_error']],
  experiments: ['Experiments', ['trial_id', 'family', 'status', 'hypothesis']],
  decisions: ['Decisions / PASS', ['market_id', 'decision', 'probability_yes', 'model_version']],
  reviews: ['Promotion reviews', ['specialist', 'previous_state', 'next_state', 'created_at']],
  reservations: ['Cost reservations', ['market_id', 'estimated_usd', 'estimated_tokens', 'created_at']],
  outcomes: ['Recorded outcomes', ['market_id', 'outcome_yes', 'resolved_at', 'first_seen_at']],
};
let snapshot, active = 'queue', selected, history = [], refreshing = false;
const label = (key) => key.replaceAll('_', ' ');
const value = (v) => v === null || v === undefined ? 'Unknown' : String(v);
function element(tag, text) { const el = document.createElement(tag); el.textContent = text; return el; }
function keyOf(view, row) { return view + ':' + (row.task_id ?? row.trial_id ?? row.id ?? row.name ?? `${row.venue}:${row.market_id}`); }
function select(view, row, remember = true) {
  if (remember && selected) history = [...history.slice(-19), selected];
  selected = { view, row }; render(); renderDetail();
}
function renderDetail() {
  $('detail').replaceChildren(); $('links').replaceChildren(); $('back').hidden = !history.length;
  if (!selected) return;
  $('selection').textContent = views[selected.view][0];
  for (const [key, val] of Object.entries(selected.row)) $('detail').append(element('dt', label(key)), element('dd', value(val)));
  // Explicit identity joins only, across the bounded records currently loaded.
  for (const [view, section] of Object.entries(snapshot?.sections ?? {})) {
    const matches = section.rows.filter(row => keyOf(view, row) !== keyOf(selected.view, selected.row) && (
      (selected.row.market_id && row.market_id === selected.row.market_id && (row.venue && selected.row.venue && row.venue === selected.row.venue)) ||
      ((selected.row.name || selected.row.specialist) && (row.name || row.specialist) === (selected.row.name || selected.row.specialist)) ||
      (selected.row.trial_id && row.parent_trial_id === selected.row.trial_id)
    ));
    for (const row of matches.slice(0, 8)) {
      const b = element('button', `${views[view][0]} · ${row.market_id ?? row.specialist ?? row.name ?? row.trial_id}`);
      b.onclick = () => { active = view; $('search').value = ''; select(view, row); }; $('links').append(b);
    }
  }
  if ($('links').childElementCount) $('links').prepend(element('p', 'Matching venue and market, specialist, or recorded parent trial identifiers.'));
}
function render() {
  $('views').replaceChildren();
  for (const [key, [name]] of Object.entries(views)) { const b = element('button', name); b.setAttribute('aria-pressed', String(active === key)); b.onclick = () => { active = key; $('search').value = ''; render(); }; $('views').append(b); }
  const [name, columns] = views[active], section = snapshot?.sections[active]; $('title').textContent = name;
  const head = document.createElement('tr'); columns.forEach(key => head.append(element('th', label(key)))); $('columns').replaceChildren(head); $('rows').replaceChildren();
  const query = $('search').value.trim().toLowerCase();
  const rows = (section?.rows ?? []).filter(row => Object.values(row).some(v => value(v).toLowerCase().includes(query)));
  $('record-status').textContent = !section ? 'No snapshot loaded' : `${section.status.replaceAll('_', ' ')} · ${rows.length} loaded matches${section.has_more ? ' · limited to 50 recent records' : ''}`;
  for (const row of rows) { const tr = document.createElement('tr'); tr.dataset.selected = String(selected && keyOf(active, row) === keyOf(selected.view, selected.row)); columns.forEach((column, i) => { const td = document.createElement('td'); if (i === 0) { const b = element('button', value(row[column])); b.onclick = () => select(active, row); td.append(b); } else td.textContent = value(row[column]); tr.append(td); }); $('rows').append(tr); }
}
async function get(path) { const response = await fetch(path, { cache: 'no-store', signal: AbortSignal.timeout(12000) }); if (!response.ok) throw new Error('Request failed'); return response.json(); }
async function refresh() {
  if (refreshing) return;
  refreshing = true; $('refresh').disabled = true;
  const results = await Promise.allSettled([get('/api/operations'), get('/api/economic-measurement')]);
  const errors = [];
  if (results[0].status === 'fulfilled') {
    snapshot = results[0].value; $('runtime').textContent = `Runtime ${snapshot.runtime.state} · ${snapshot.database_present ? 'database present' : 'database absent'}`;
    $('updated').textContent = `Snapshot ${snapshot.as_of}`;
    $('runtime-detail').replaceChildren();
    for (const [key, val] of Object.entries({heartbeat: snapshot.runtime.last_heartbeat_at, ...snapshot.runtime.cycle, ...snapshot.runtime.connections})) $('runtime-detail').append(element('dt', label(key)), element('dd', value(val)));
    if (selected) { const row = snapshot.sections[selected.view].rows.find(r => keyOf(selected.view, r) === keyOf(selected.view, selected.row)); if (row) selected = { ...selected, row }; }
    render(); renderDetail();
  } else errors.push('Operational refresh failed; any prior snapshot is retained and stale.');
  if (results[1].status === 'fulfilled') {
    const d = results[1].value, present = d.database_present;
    $('cash').textContent = present ? value(d.cash.net_cash_usd) : 'Unknown';
    $('reserved').textContent = present ? value(d.operating_estimates.model_reserved_usd) : 'Unknown';
    $('paper').textContent = present ? value(d.paper.net_after_execution_costs_usd) : 'Unknown';
    $('economic-status').textContent = d.status.replaceAll('_', ' '); $('economics-time').textContent = `Economic snapshot ${d.as_of}`;
  } else errors.push('Economic refresh failed; any prior amounts are stale.');
  $('error').hidden = !errors.length; $('error').textContent = errors.join(' '); $('refresh').disabled = false; refreshing = false;
}
$('search').oninput = render; $('refresh').onclick = refresh;
$('back').onclick = () => { selected = history.pop(); if (selected) active = selected.view; render(); renderDetail(); };
render(); refresh();

setInterval(() => { if (!document.hidden) refresh(); }, 30000);
