/* OTHER CHAINS chip (PAPER ONLY). Reads /api/other-chains (read-only): per-chain funnel, top skip reasons,
 * measured discovery lag, paper results and the pre-registered go/no-go verdict. Nothing here can trade. */
(function () {
  'use strict';
  var CSS = '#oc-wrap{position:relative;display:inline-flex;align-self:center;margin-left:6px;font:11px ui-monospace,Menlo,monospace}' +
    '#oc-chip{background:transparent;border:1px solid #2a3340;color:#9fb0c0;border-radius:10px;padding:2px 8px;cursor:pointer;font:inherit}' +
    '#oc-pop{position:absolute;right:0;top:calc(100% + 4px);z-index:80;background:#0d1117;border:1px solid #2a3340;border-radius:6px;' +
    'padding:6px 8px;max-height:70vh;overflow:auto;min-width:640px;box-shadow:0 6px 20px #0008;color:#c9d1d9}' +
    '#oc-pop table{border-collapse:collapse;width:100%}#oc-pop td,#oc-pop th{padding:1px 6px;white-space:nowrap;text-align:right}' +
    '#oc-pop td:first-child,#oc-pop th:first-child,#oc-pop td.l{text-align:left}#oc-pop th{color:#7d8590;font-weight:400}' +
    '#oc-pop .m{color:#7d8590}#oc-pop .up{color:#3fb950}#oc-pop .dn{color:#f85149}';
  function el(t, c, x) { var e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = String(x); return e; }
  function pct(v) { return v == null ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'; }
  function lag(s) { return s == null ? '—' : s < 120 ? s + 's' : Math.round(s / 60) + 'm'; }
  var last = null;
  function render() {
    var d = last, chip = document.getElementById('oc-chip'), pop = document.getElementById('oc-pop');
    if (!d || !chip) return;
    var cs = d.chains || [], closed = 0, entries = 0;
    cs.forEach(function (c) { closed += c.closed || 0; entries += c.entries || 0; });
    chip.textContent = 'Other chains · paper' + (cs.length ? ' · ' + entries + ' entries' : ' · off');
    if (pop.hidden) return;
    pop.textContent = '';
    pop.appendChild(el('div', 'm', 'PAPER ONLY · $' + (d.paper_size_usd || '?') + '/trade · costs incl. tax, slippage, DEX fee, gas, bridge · ' +
      'pre-registered ' + (d.preregistered_at || '—').slice(0, 16) + 'Z'));
    var t = el('table'), h = el('tr');
    ['chain', 'money', 'seen', 'skipped (top)', 'followed', 'signals', 'entries', 'closed', 'lag p50', 'mean net', 'verdict'].forEach(function (x) { h.appendChild(el('th', null, x)); });
    t.appendChild(h);
    cs.forEach(function (c) {
      var r = el('tr'), sk = Object.keys(c.skips || {}).slice(0, 2).map(function (k) { return k + ' ' + c.skips[k]; }).join(', ');
      var v = c.verdict || {};
      [c.chain, c.funded ? 'funded' : 'bridge', c.seen, (c.skipped || 0) + (sk ? ' (' + sk + ')' : ''), c.buyable_followed,
        c.signals, c.entries, c.closed, lag(c.lag_median_s), pct(v.mean), v.verdict || '—'].forEach(function (x, i) {
        var td = el('td', i === 1 || i === 3 ? 'l' : null, x);
        if (i === 9 && v.mean != null) td.className = v.mean > 0 ? 'up' : 'dn';
        r.appendChild(td);
      });
      t.appendChild(r);
    });
    pop.appendChild(t);
    if (!cs.length) pop.appendChild(el('div', 'm', 'Scanner not started.'));
    var v2 = d.v2 || {}, c2 = v2.chains || [];
    if (!c2.length) return;
    pop.appendChild(el('div', 'm', 'v2 arm (GeckoTerminal fallback + tax re-check) · pre-registered ' + (v2.preregistered_at || '—').slice(0, 16) + 'Z'));
    var t2 = el('table'), h2 = el('tr');
    ['chain', 'seen', 'skipped (top)', 'followed', 'entries', 'entry lag p50', 'verdict'].forEach(function (x) { h2.appendChild(el('th', null, x)); });
    t2.appendChild(h2);
    c2.forEach(function (c) {
      var r = el('tr'), sk = Object.keys(c.skips || {}).slice(0, 2).map(function (k) { return k + ' ' + c.skips[k]; }).join(', ');
      [c.chain, c.seen, (c.skipped || 0) + (sk ? ' (' + sk + ')' : ''), c.buyable_followed, c.entries,
        lag(c.entry_lag_median_s), (c.verdict || {}).verdict || '—'].forEach(function (x, i) { r.appendChild(el('td', i === 2 ? 'l' : null, x)); });
      t2.appendChild(r);
    });
    pop.appendChild(t2);
  }
  function tick() {
    fetch('/api/other-chains', {credentials: 'same-origin', cache: 'no-store'}).then(function (r) { return r.json(); })
      .then(function (d) { last = d; render(); }).catch(function () {});
  }
  function mount() {
    if (document.getElementById('oc-wrap')) return;
    var h = document.querySelector('.app header') || document.querySelector('header');
    if (!h) return;
    var s = el('style'); s.textContent = CSS; document.head.appendChild(s);
    var w = el('span'); w.id = 'oc-wrap';
    var b = el('button', null, 'Other chains · paper'); b.id = 'oc-chip'; b.type = 'button'; b.title = 'other-chain launches, paper only';
    var p = el('div'); p.id = 'oc-pop'; p.hidden = true;
    w.appendChild(b); w.appendChild(p);
    var hb = document.getElementById('hb-wrap');
    if (hb && hb.parentNode === h) h.insertBefore(w, hb.nextSibling); else h.appendChild(w);
    b.onclick = function (e) { e.stopPropagation(); p.hidden = !p.hidden; render(); };
    document.addEventListener('click', function (e) { if (!p.hidden && !w.contains(e.target)) p.hidden = true; });
    tick(); setInterval(tick, 30000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { setTimeout(mount, 500); });
  else setTimeout(mount, 500);
})();
