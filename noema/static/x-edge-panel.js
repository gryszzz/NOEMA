/* TEV X edge panel: drop-in module for the v6 trading room (or any page).
 *   <div id="x-edge"></div><script src="/static/x-edge-panel.js"></script>
 *   <script>TEVXEdge.mount(document.getElementById('x-edge'))</script>
 * Reads /api/x-signal (read-only). Every string from X is untrusted: rendered with textContent only.
 * X is evidence only: nothing in this panel can trade. */
(function () {
  'use strict';
  var CSS = '' +
    '.xe{--xc:var(--c,#27e7ff);--xg:var(--g,#39ff9c);--xr:var(--r,#ff3b6b);--xy:var(--y,#ffd23f);--xm:var(--m,#b67bff);' +
    '--xt:var(--t,#bfe9ff);--xd:var(--d,#5f8aa3);--xl:var(--ln,#12324a);font:12px/1.35 ui-monospace,Menlo,monospace;color:var(--xt);' +
    'display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}' +
    '.xe-card{border:1px solid var(--xl);border-radius:10px;padding:10px;background:rgba(7,16,25,.75);position:relative;overflow:hidden}' +
    '.xe-card h4{margin:0 0 8px;font-size:11px;letter-spacing:.14em;color:var(--xc);text-transform:uppercase;display:flex;justify-content:space-between}' +
    '.xe-card h4 small{color:var(--xd);letter-spacing:0}' +
    '.xe-row{display:flex;gap:6px;align-items:center;padding:3px 0;border-bottom:1px dashed rgba(95,138,163,.15)}' +
    '.xe-row:last-child{border-bottom:0}.xe-g{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '.xe-bar{height:4px;border-radius:2px;background:var(--xg);box-shadow:0 0 6px var(--xg)}' +
    '.xe-chip{font-size:10px;padding:1px 6px;border-radius:9px;border:1px solid currentColor;white-space:nowrap}' +
    '.xe-up{color:var(--xg)}.xe-dn{color:var(--xr)}.xe-warn{color:var(--xy)}.xe-dim{color:var(--xd)}.xe-m{color:var(--xm)}' +
    '.xe-alert{border-color:var(--xy);animation:xeflash 1.6s ease-in-out infinite}' +
    '@keyframes xeflash{0%,100%{box-shadow:0 0 0 rgba(255,210,63,0)}50%{box-shadow:0 0 14px rgba(255,210,63,.55)}}' +
    '.xe-shill{border-color:var(--xr)}.xe-radar{width:100%;height:190px}' +
    '.xe-blip{animation:xeblip 2.4s ease-in-out infinite}@keyframes xeblip{0%,100%{opacity:.55}50%{opacity:1}}' +
    '.xe-sweep{transform-origin:100px 95px;animation:xesweep 6s linear infinite}@keyframes xesweep{to{transform:rotate(360deg)}}' +
    '.xe-empty{color:var(--xd);font-style:italic}';
  var PHASE = {birth: '#27e7ff', accelerating: '#39ff9c', peak: '#ffd23f', decaying: '#5f8aa3'};

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = String(text);
    return e;
  }
  function card(title, sub, extra) {
    var c = el('div', 'xe-card' + (extra ? ' ' + extra : ''));
    var h = el('h4', null, title);
    h.appendChild(el('small', null, sub || ''));
    c.appendChild(h);
    return c;
  }
  function empty(c, msg) { c.appendChild(el('div', 'xe-empty', msg)); return c; }
  function pct(x) { return x == null ? '—' : (x * 100).toFixed(1) + '%'; }
  function svg(tag, attrs) {
    var e = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  function alerts(d) {
    var list = (d.alerts || []).filter(function (a) { return a.status === 'not_priced_in'; });
    var c = card('Not priced in', list.length + ' live', list.length ? 'xe-alert' : '');
    if (!list.length) return empty(c, 'No X burst is ahead of its market right now.');
    list.slice(0, 6).forEach(function (a) {
      var r = el('div', 'xe-row');
      r.appendChild(el('span', 'xe-chip xe-warn', 'z ' + (a.z || 0).toFixed(1)));
      r.appendChild(el('span', 'xe-g', (a.title || a.entity)));
      r.appendChild(el('span', a.direction > 0 ? 'xe-up' : 'xe-dn', a.direction > 0 ? 'YES ↑' : 'YES ↓'));
      r.appendChild(el('span', 'xe-dim', a.price_now != null ? (a.price_now * 100).toFixed(0) + '¢' : ''));
      c.appendChild(r);
    });
    return c;
  }

  function leaderboard(d) {
    var rows = d.leaderboard || [];
    var c = card('Smart money', 'early + right, shrunk');
    if (!rows.length) return empty(c, 'Scoring authors: calls resolve after 1h / 24h.');
    rows.slice(0, 10).forEach(function (a) {
      var r = el('div', 'xe-row');
      var cls = a.status === 'promoted' ? 'xe-up' : a.status === 'demoted' ? 'xe-dn' : 'xe-dim';
      r.appendChild(el('span', 'xe-chip ' + cls, a.status === 'promoted' ? '★' : a.status === 'demoted' ? '✕' : '·'));
      r.appendChild(el('span', 'xe-g', '@' + a.author));
      r.appendChild(el('span', 'xe-dim', (a.hits || 0) + '/' + (a.n || 0)));
      var w = el('div', 'xe-bar');
      w.style.width = Math.max(2, Math.min(60, Math.abs(a.shrunk || 0) * 120)) + 'px';
      if ((a.shrunk || 0) < 0) w.style.background = 'var(--xr)';
      r.appendChild(w);
      c.appendChild(r);
    });
    return c;
  }

  function radar(d) {
    var ns = d.narratives || [];
    var c = card('Narrative radar', ns.length + ' tracked');
    if (!ns.length) return empty(c, 'Clustering the wire into narratives…');
    var s = svg('svg', {viewBox: '0 0 200 190', 'class': 'xe-radar'});
    [30, 60, 90].forEach(function (r) {
      s.appendChild(svg('circle', {cx: 100, cy: 95, r: r, fill: 'none', stroke: '#12324a'}));
    });
    var sweep = svg('line', {x1: 100, y1: 95, x2: 100, y2: 5, stroke: '#27e7ff', 'stroke-opacity': '.35', 'class': 'xe-sweep'});
    s.appendChild(sweep);
    var ring = {birth: 85, accelerating: 60, peak: 38, decaying: 16};
    ns.slice(0, 10).forEach(function (n, i) {
      var ang = (i / Math.min(10, ns.length)) * Math.PI * 2 - Math.PI / 2;
      var rr = ring[n.phase] || 50;
      var x = 100 + Math.cos(ang) * rr, y = 95 + Math.sin(ang) * rr;
      var g = svg('g', {'class': 'xe-blip'});
      g.appendChild(svg('circle', {cx: x, cy: y, r: Math.min(9, 3 + (n.rate_now || 0)), fill: PHASE[n.phase] || '#bfe9ff', 'fill-opacity': '.8'}));
      var t = svg('text', {x: x + 6, y: y + 3, fill: '#bfe9ff', 'font-size': '7'});
      t.textContent = String(n.label || '').slice(0, 22);
      g.appendChild(t);
      var tip = svg('title', {});
      tip.textContent = (n.label || '') + ' — ' + n.phase + ' · ' + (n.posts || 0) + ' posts' + (n.sector ? ' · ' + n.sector : '');
      g.appendChild(tip);
      s.appendChild(g);
    });
    c.appendChild(s);
    var legend = el('div', 'xe-row');
    Object.keys(PHASE).forEach(function (k) {
      var chip = el('span', 'xe-chip', k);
      chip.style.color = PHASE[k];
      legend.appendChild(chip);
    });
    c.appendChild(legend);
    return c;
  }

  function rotation(d) {
    var rs = (d.rotation || []).filter(function (r) { return r.mentions_24h || r.share_prev_24h; });
    var c = card('Sector rotation', 'share of voice 24h vs prior');
    if (!rs.length) return empty(c, 'Not enough crypto chatter yet.');
    rs.slice(0, 7).forEach(function (r) {
      var row = el('div', 'xe-row');
      row.appendChild(el('span', 'xe-g', r.sector.replace(/_/g, ' ')));
      row.appendChild(el('span', 'xe-dim', pct(r.share_24h)));
      row.appendChild(el('span', r.rotation >= 0 ? 'xe-up' : 'xe-dn', (r.rotation >= 0 ? '+' : '') + (r.rotation * 100).toFixed(1) + 'pp'));
      c.appendChild(row);
    });
    return c;
  }

  function shills(d) {
    var ws = d.shill_warnings || [];
    var c = card('Shill shield', ws.length + ' flagged', ws.length ? 'xe-shill' : '');
    if (!ws.length) return empty(c, 'No coordinated campaigns detected.');
    ws.slice(0, 6).forEach(function (w) {
      var r = el('div', 'xe-row');
      r.appendChild(el('span', 'xe-chip ' + (w.score >= 0.5 ? 'xe-dn' : 'xe-warn'), w.score >= 0.5 ? 'AVOID' : 'CAUTION'));
      var name = w.entity.indexOf(':') > 0 && w.kind === 'contract' ? w.entity.slice(0, 14) + '…' : w.entity;
      r.appendChild(el('span', 'xe-g', name));
      r.appendChild(el('span', 'xe-dim', (w.reasons || []).map(function (x) { return x.replace(/_/g, ' '); }).join(', ')));
      c.appendChild(r);
    });
    return c;
  }

  function contracts(d) {
    var cs = d.contracts || [];
    var c = card('KOL → wallet check', 'early buyers vs hype');
    if (!cs.length) return empty(c, 'No contract addresses in the wire yet.');
    cs.slice(0, 6).forEach(function (x) {
      var r = el('div', 'xe-row');
      var v = x.verdict || 'pending';
      var cls = v === 'early_buyers_dumped_into_hype' ? 'xe-dn' : v === 'early_buyers_holding' ? 'xe-up' : 'xe-dim';
      r.appendChild(el('span', 'xe-chip ' + cls, v === 'early_buyers_dumped_into_hype' ? 'DUMPED' : v === 'early_buyers_holding' ? 'HOLDING' : v.replace(/_/g, ' ')));
      r.appendChild(el('span', 'xe-g', (x.symbol ? '$' + x.symbol + ' ' : '') + x.address.slice(0, 6) + '…' + x.address.slice(-4)));
      r.appendChild(el('span', 'xe-dim', x.authors + ' acct' + (x.sent_to_alpha ? ' · →alpha' : '')));
      c.appendChild(r);
    });
    return c;
  }

  function crowdAndAgents(d) {
    var c = card('Contrarian + ledger', 'accuracy engine decides');
    (d.crowd || []).slice(0, 3).forEach(function (x) {
      var r = el('div', 'xe-row');
      r.appendChild(el('span', 'xe-chip xe-m', x.signal.toUpperCase()));
      r.appendChild(el('span', 'xe-g', x.title || x.target));
      r.appendChild(el('span', 'xe-dim', 'fade · n=' + x.n));
      c.appendChild(r);
    });
    (d.agents || []).forEach(function (a) {
      var r = el('div', 'xe-row');
      r.appendChild(el('span', 'xe-g', a.agent));
      r.appendChild(el('span', 'xe-dim', a.resolved + '/' + a.forecasts + ' resolved'));
      r.appendChild(el('span', a.brier != null && a.brier < 0.25 ? 'xe-up' : 'xe-dim', a.brier != null ? 'Brier ' + a.brier.toFixed(3) : '—'));
      c.appendChild(r);
    });
    c.appendChild(el('div', 'xe-dim', 'X is evidence only — never a sole trade trigger.'));
    return c;
  }

  function render(root, d) {
    root.textContent = '';
    if (!d || d.status !== 'active') {
      root.appendChild(empty(card('X edge', 'idle'), (d && d.note) || 'Waiting for the first X edge run.'));
      return;
    }
    [alerts, leaderboard, radar, shills, rotation, contracts, crowdAndAgents].forEach(function (f) {
      try { root.appendChild(f(d)); } catch (e) { /* one bad panel never blanks the module */ }
    });
  }

  function mount(root, opts) {
    opts = opts || {};
    if (!document.getElementById('xe-css')) {
      var st = document.createElement('style');
      st.id = 'xe-css';
      st.textContent = CSS;
      document.head.appendChild(st);
    }
    root.classList.add('xe');
    var url = opts.endpoint || '/api/x-signal';
    function tick() {
      fetch(url, {credentials: 'same-origin'}).then(function (r) { return r.json(); })
        .then(function (d) { render(root, d); if (opts.onData) opts.onData(d); })
        .catch(function () { /* keep last render */ });
    }
    tick();
    var id = setInterval(tick, opts.intervalMs || 30000);
    return {refresh: tick, stop: function () { clearInterval(id); }, render: function (d) { render(root, d); }};
  }

  window.TEVXEdge = {mount: mount, render: render};
})();
