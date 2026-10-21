/* TEV STRATEGIES panel: structural-edge strategy book (paper only).
 *   <script src="/static/strategies-panel.js" defer></script>   (auto-mounts into the room)
 *   or TEVStrategies.mount(el) on any page.
 * Reads /api/strategies (read-only). Shows each strategy's paper P&L, n, EV, backtest verdict,
 * allocator weight and status. Nothing in this panel can trade. */
(function () {
  'use strict';
  var CSS = '.st{font:12px/1.35 ui-monospace,Menlo,monospace;color:var(--t,#bfe9ff)}' +
    '.st table{width:100%;border-collapse:collapse}.st th{font-size:10px;letter-spacing:.12em;color:var(--d,#5f8aa3);' +
    'text-align:right;font-weight:400;padding:2px 4px}.st th:first-child,.st td:first-child{text-align:left}' +
    '.st td{padding:4px;border-top:1px dashed rgba(95,138,163,.18);text-align:right;white-space:nowrap}' +
    '.st .up{color:var(--g,#39ff9c)}.st .dn{color:var(--r,#ff3b6b)}.st .dim{color:var(--d,#5f8aa3)}' +
    '.st .chip{font-size:10px;padding:1px 6px;border-radius:9px;border:1px solid currentColor}' +
    '.st .active{color:var(--g,#39ff9c)}.st .exploring{color:var(--c,#27e7ff)}.st .benched{color:var(--r,#ff3b6b)}' +
    '.st .w{display:inline-block;height:4px;border-radius:2px;background:var(--c,#27e7ff);box-shadow:0 0 6px var(--c,#27e7ff);vertical-align:middle}' +
    '.st .feed{margin-top:8px;max-height:150px;overflow:auto}.st .feed div{padding:2px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '.st .new{animation:stin .8s ease-out}@keyframes stin{from{background:rgba(39,231,255,.25)}to{background:transparent}}';
  function el(t, c, x) { var e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = String(x); return e; }
  function num(x, d) { return x == null ? '—' : Number(x).toFixed(d == null ? 3 : d); }
  function money(x) { if (x == null) return '—'; var v = Number(x); return (v >= 0 ? '+$' : '−$') + Math.abs(v).toFixed(2); }
  var seen = {};
  function render(root, d) {
    root.textContent = '';
    var tb = el('table'), hr = el('tr');
    ['strategy', 'status', 'weight', 'paper P&L', 'n', 'EV/trade', 'open', 'backtest'].forEach(function (h) { hr.appendChild(el('th', null, h)); });
    tb.appendChild(hr);
    (d.strategies || []).forEach(function (s) {
      var tr = el('tr');
      var name = el('td'); name.appendChild(el('span', null, s.label)); tr.appendChild(name);
      var st = el('td'); var chip = el('span', 'chip ' + s.status, s.status); chip.title = s.reason || ''; st.appendChild(chip); tr.appendChild(st);
      var w = el('td'); var bar = el('span', 'w'); bar.style.width = Math.round((s.weight || 0) * 60) + 'px'; w.appendChild(bar);
      w.appendChild(el('span', 'dim', ' ' + Math.round((s.weight || 0) * 100) + '%')); tr.appendChild(w);
      tr.appendChild(el('td', (s.pnl_usd || 0) > 0 ? 'up' : (s.pnl_usd || 0) < 0 ? 'dn' : 'dim', money(s.pnl_usd)));
      tr.appendChild(el('td', null, s.n || 0));
      tr.appendChild(el('td', (s.ev || 0) > 0 ? 'up' : (s.ev || 0) < 0 ? 'dn' : 'dim', num(s.ev)));
      tr.appendChild(el('td', null, (s.open || 0) + (s.shadow_24h ? ' · ' + s.shadow_24h + ' shadow' : '')));
      var b = s.backtest || {}, bt = el('td', 'dim', b.verdict ? b.verdict.replace(/_/g, ' ') + (b.n ? ' (n ' + b.n + ', EV ' + num(b.ev, 4) + ')' : '') : 'forward only');
      tr.appendChild(bt); tb.appendChild(tr);
    });
    root.appendChild(tb);
    var feed = el('div', 'feed');
    (d.recent || []).forEach(function (r) {
      var k = r.at + r.title, row = el('div', seen[k] ? null : 'new');
      seen[k] = 1;
      row.appendChild(el('span', 'dim', (r.at || '').slice(11, 16) + ' '));
      row.appendChild(el('span', 'exploring', r.strategy + ' '));
      row.appendChild(el('span', null, (r.title || '') + ' '));
      row.appendChild(el('span', r.status === 'shadow' ? 'dim' : 'up', r.status === 'shadow' ? '(shadow)' : '$' + num(r.size_usd, 0)));
      feed.appendChild(row);
    });
    if (!(d.recent || []).length) feed.appendChild(el('div', 'dim', 'No strategy signals yet — every strategy waits for a priced, costed edge.'));
    root.appendChild(feed);
  }
  function mount(root) {
    if (!document.getElementById('st-css')) { var s = el('style'); s.id = 'st-css'; s.textContent = CSS; document.head.appendChild(s); }
    root.classList.add('st');
    function tick() {
      fetch('/api/strategies', {credentials: 'same-origin'}).then(function (r) { return r.json(); })
        .then(function (d) { render(root, d); }).catch(function () {});
    }
    tick(); return setInterval(tick, 30000);
  }
  function auto() {
    var app = document.querySelector('.app');
    if (!app || document.getElementById('strat')) return;
    var sec = el('section', 'pnl'); sec.id = 'strat-pnl';
    sec.appendChild(el('h2', null, 'STRATEGIES · PAPER'));
    var body = el('div'); body.id = 'strat'; sec.appendChild(body);
    var after = document.querySelector('.culture-row');
    if (after && after.parentNode) after.parentNode.insertBefore(sec, after.nextSibling); else app.appendChild(sec);
    mount(body);
  }
  window.TEVStrategies = {mount: mount};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', auto); else auto();
})();
