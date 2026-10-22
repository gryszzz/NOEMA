/* TEV TIMING panel: in-house day-trader / timing agents (PAPER ONLY). Reads /api/timing (read-only).
 * Shows agents (status, scored tokens, paper P&L), watched tokens with current state wait/enter/idle,
 * and recently exited tokens. Nothing in this panel can trade. */
(function () {
  'use strict';
  var CSS = '.tm{font:12px/1.35 ui-monospace,Menlo,monospace;color:var(--t,#bfe9ff);display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}' +
    '.tm h3{margin:0 0 4px;font-size:10px;letter-spacing:.14em;color:var(--d,#5f8aa3);font-weight:400}' +
    '.tm .r{display:flex;justify-content:space-between;gap:8px;padding:2px 0;border-top:1px dashed rgba(95,138,163,.18);white-space:nowrap;overflow:hidden}' +
    '.tm .r span:first-child{overflow:hidden;text-overflow:ellipsis}.tm .up{color:var(--g,#39ff9c)}.tm .dn{color:var(--r,#ff3b6b)}.tm .dim{color:var(--d,#5f8aa3)}' +
    '.tm .chip{font-size:10px;padding:0 6px;border-radius:9px;border:1px solid currentColor}.tm .enter{color:var(--g,#39ff9c)}.tm .wait{color:var(--c,#27e7ff)}' +
    '.tm .idle,.tm .shadow{color:var(--d,#5f8aa3)}.tm .earning{color:var(--g,#39ff9c)}.tm .fired{color:var(--r,#ff3b6b)}.tm .sum{grid-column:1/-1;color:var(--d,#5f8aa3)}';
  function el(t, c, x) { var e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = String(x); return e; }
  function money(v) { v = Number(v || 0); return (v >= 0 ? '+$' : '−$') + Math.abs(v).toFixed(2); }
  function short(m) { return m ? m.slice(0, 4) + '…' + m.slice(-4) : '?'; }
  function row(a, b, cls) { var r = el('div', 'r'); r.appendChild(a); var s = el('span', cls); if (b instanceof Node) s.appendChild(b); else s.textContent = b; r.appendChild(s); return r; }
  function render(root, d) {
    root.textContent = '';
    var c = d.counts || {};
    root.appendChild(el('div', 'sum', 'PAPER ONLY · ' + (c.shadow || 0) + ' shadow · ' + (c.earning || 0) + ' earning · ' + (c.fired || 0) +
      ' fired · total paper P&L ' + money(d.paper_pnl_usd) + (d.report ? ' · last walk-forward n=' + d.report.n_tokens : '')));
    var A = el('div'); A.appendChild(el('h3', null, 'TIMING AGENTS'));
    (d.agents || []).slice(0, 8).forEach(function (a) {
      var l = el('span'); l.appendChild(el('span', 'chip ' + a.status, a.status)); l.appendChild(el('span', null, ' ' + a.name));
      A.appendChild(row(l, (a.scored || 0) + ' · ' + money(a.paper_pnl_usd), (a.paper_pnl_usd || 0) > 0 ? 'up' : (a.paper_pnl_usd || 0) < 0 ? 'dn' : 'dim'));
    });
    var W = el('div'); W.appendChild(el('h3', null, 'WATCHING (1m)'));
    (d.watch || []).slice(0, 8).forEach(function (w) {
      var age = Math.round((Date.now() / 1000 - w.t0) / 60);
      W.appendChild(row(el('span', null, short(w.mint) + ' · ' + age + 'm'),
        el('span', 'chip ' + w.state, w.state + (w.agents_entering ? ' ×' + w.agents_entering : ''))));
    });
    if (!(d.watch || []).length) W.appendChild(el('div', 'dim', 'No passed candidates in the last 30 min.'));
    var X = el('div'); X.appendChild(el('h3', null, 'EXITED (avg per agent)'));
    (d.exited || []).forEach(function (x) {
      X.appendChild(row(el('span', null, short(x.mint) + (x.traded ? ' · ' + x.traded + ' traded' : ' · no trade')), money(x.net_usd),
        x.net_usd > 0 ? 'up' : x.net_usd < 0 ? 'dn' : 'dim'));
    });
    if (!(d.exited || []).length) X.appendChild(el('div', 'dim', 'Tokens are scored ~3h after they pass.'));
    root.appendChild(A); root.appendChild(W); root.appendChild(X);
  }
  function mount(root) {
    if (!document.getElementById('tm-css')) { var s = el('style'); s.id = 'tm-css'; s.textContent = CSS; document.head.appendChild(s); }
    root.classList.add('tm');
    function tick() {
      fetch('/api/timing', {credentials: 'same-origin', cache: 'no-store'}).then(function (r) { return r.json(); })
        .then(function (d) { render(root, d); }).catch(function () {});
    }
    tick(); return setInterval(tick, 15000);
  }
  function auto() {
    var app = document.querySelector('.app');
    if (!app || document.getElementById('timing')) return;
    var sec = el('section', 'pnl'); sec.id = 'timing-pnl';
    sec.appendChild(el('h2', null, 'TIMING · DAY TRADER · PAPER'));
    var body = el('div'); body.id = 'timing'; sec.appendChild(body);
    var after = document.getElementById('strat-pnl') || document.querySelector('.culture-row');
    if (after && after.parentNode) after.parentNode.insertBefore(sec, after.nextSibling); else app.appendChild(sec);
    mount(body);
    function adopt() { var w = document.getElementById('ch-wires'); if (w && sec.parentElement !== w) w.appendChild(sec); }
    adopt(); setInterval(adopt, 5000);  /* one-page + big-monitor layouts keep their wires in #ch-wires */
  }
  window.TEVTiming = {mount: mount};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', auto); else auto();
})();
