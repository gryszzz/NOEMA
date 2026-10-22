/* NOEMA live panels: SSE push via /api/live/stream, poll fallback /api/live. Read-only, paper only. */
(function(){
  const $=(s)=>document.querySelector(s), esc=(x)=>String(x==null?"":x).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const T=(x)=>x?Date.parse(x):NaN, ago=(ms)=>{if(!isFinite(ms))return"—";const s=Math.max(0,ms/1e3);return s<90?s.toFixed(0)+"s":s<5400?(s/60).toFixed(0)+"m":s<172800?(s/3600).toFixed(1)+"h":(s/86400).toFixed(1)+"d"};
  const n=(x,d=2)=>x==null||x===""||isNaN(+x)?"—":(+x).toFixed(d), cls=(x)=>+x>0?"lvg":+x<0?"lvr":"lvm", sh=(m)=>m?String(m).slice(0,4)+"…"+String(m).slice(-4):"—";
  const P=[["launch","LIVE LAUNCH STREAM"],["candles","FRESH LAUNCH CANDLES · auto-cycle 5s"],["look","WHAT EACH AGENT IS LOOKING AT"],["agents","PAPER AGENT WALL"],
           ["smart","SMART WALLETS"],["treas","TREASURY"],["pmh","POLYMARKET HISTORY"],["near","NEAR-CLOSE CRYPTO"],["xv","CROSS-VENUE GAPS · 1¢ fee+buffer gate"]];
  const SEC={launch:"launch",candles:"candles",look:"looking",agents:"agents",smart:"smart",treas:"treasury",pmh:"pm_history",near:"near_close",xv:"xv"};
  function mount(){
    if($("#lv"))return;const w=document.createElement("section");w.id="lv";
    w.innerHTML=P.map(([k,t])=>`<div class="lvp" id="lv-${k}"><h3>${t}<span class="lvfr" id="lv-${k}-f"><i class="lvdot"></i><span>—</span></span></h3><div id="lv-${k}-b">${k==="candles"?'<div class="lvhl" id="lv-cinfo"></div><canvas id="lv-cv"></canvas>':'<span class="lvm">waiting for push…</span>'}</div></div>`).join("")
      +`<div class="lvm" style="grid-column:1/-1" id="lv-tx"></div>`;
    (document.querySelector(".app")||document.body).insertAdjacentElement("afterend",w);
    document.documentElement.style.overflowY="auto";document.body.style.overflowY="auto";document.body.style.height="auto";
  }
  let D=null, mode="—", lastPush=0, ci=0, gaps=[];
  function fresh(){ if(!D)return; const now=Date.now();
    for(const [k] of P){const s=D[SEC[k]]||{},f=$(`#lv-${k}-f`);if(!f)continue;const a=now-T(s.as_of),c=(s.cadence_s||30)*1e3;
      f.className="lvfr"+(!isFinite(a)||a>6*c?" lvred":a>2*c?" lvamber":"");f.lastChild.textContent=(isFinite(a)?ago(a)+" old":"no data")+` · every ${s.cadence_s||"?"}s`;}
    $("#lv-tx").textContent=`transport: ${mode} · last update ${ago(now-lastPush)} ago · server build ${D.build_ms}ms · paper only — no live orders`+(gaps.length?` · push interval ~${(gaps.reduce((a,b)=>a+b,0)/gaps.length/1e3).toFixed(1)}s`:"");}
  function tbl(h,rows){return`<table><tr>${h.map(x=>`<th>${x}</th>`).join("")}</tr>${rows.join("")}</table>`}
  function render(){ const L=D.launch||{},now=Date.now();
    $("#lv-launch-b").innerHTML=`<div class="lvhl">${Object.entries(L.health||{}).map(([k,v])=>`<span class="lvchip ${v.status==="ok"?"lvg":v.status==="stale"?"lvy":"lvr"}" title="${esc(v.status)}">${esc(k)} ${v.per_min!=null?n(v.per_min,0)+"/m":""} ${ago(now-T(v.last_at))}${v.status!=="ok"?" · "+esc(v.status):""}</span>`).join("")}</div>
      <div class="lvsc" style="max-height:150px">${tbl(["new coin","mcap◎","trades","age"],(L.creates||[]).map(c=>`<tr><td>${esc(c.sym||sh(c.mint))} <span class="lvm">${esc((c.name||"").slice(0,18))}</span></td><td>${n(c.mcap,1)}</td><td class="lvm">${c.trades_recent??""}</td><td>${ago(now-T(c.at))}</td></tr>`))}</div>
      <div class="lvsc" style="max-height:150px">${tbl(["trade","side","◎","mcap◎","age"],(L.ticker||[]).map(t=>`<tr><td>${esc(t.sym||sh(t.mint))}</td><td class="${t.side==="buy"?"lvg":"lvr"}">${esc(t.side)}</td><td>${n(t.sol,3)}</td><td>${n(t.mcap,1)}</td><td>${ago(now-T(t.at))}</td></tr>`))}</div>`;
    const E=(D.looking||{}).events||[];
    $("#lv-look-b").innerHTML=`<div class="lvsc" style="max-height:340px">${tbl(["agent","looking at","features","decision","why","age"],E.map(e=>{const ft=typeof e.features==="object"&&e.features?Object.entries(e.features).map(([k,v])=>k+"="+(typeof v==="number"?+v.toFixed(3):v)).join(" "):e.features||"";return`<tr><td class="lvy">${esc(e.agent)}</td><td title="${esc(e.ref||e.subject)}">${esc(e.subject)}</td><td class="lvm" title="${esc(ft)}">${esc(ft)}</td><td class="${/enter|buy|open/.test(e.decision)?"lvg":/exit|close/.test(e.decision)?"lvy":"lvm"}">${esc(e.decision)}</td><td title="${esc(e.reason)}">${esc(e.reason)}</td><td>${ago(now-T(e.at))}</td></tr>`}))}</div>`;
    const A=D.agents||{};
    $("#lv-agents-b").innerHTML=`<div class="lvm">${esc(A.note||"")}</div><div class="lvsc" style="max-height:320px">${tbl(["agent","kind","n","PnL / score","CI95","status"],(A.agents||[]).map(a=>`<tr><td title="${esc(a.name)}">${esc(a.name)}</td><td class="lvm">${esc(a.kind)}</td><td>${a.n??"—"}</td><td class="${cls(a.pnl)}">${n(a.pnl,3)}</td><td class="lvm">${a.ci?`[${n(a.ci[0],2)}, ${n(a.ci[1],2)}]`:"—"}</td><td class="${/live|active|ok|run/.test(a.status)?"lvg":"lvm"}">${esc(a.status)}</td></tr>`))}</div>`;
    const S=D.smart||{};
    $("#lv-smart-b").innerHTML=`<div class="lvm">${S.n_vetted??0} vetted of ${S.candidates??0} candidates · hard-rejected: ${esc(Object.entries(S.rejections||{}).filter(([k])=>k!=="vetted").map(([k,v])=>k+" "+v).join(", "))}</div><div class="lvsc" style="max-height:150px">${tbl(["vetted wallet","closed","win","PnL◎","CI lo"],(S.vetted||[]).map(w=>`<tr><td>${sh(w.wallet)}</td><td>${w.n_closed}</td><td>${n(w.win_rate,2)}</td><td class="${cls(w.pnl_sol)}">${n(w.pnl_sol,2)}</td><td>${n(w.ci_lo,2)}</td></tr>`))}</div>
      <div class="lvsc" style="max-height:140px">${tbl(["move","coin","◎","age"],(S.moves||[]).map(m=>`<tr><td class="${m.side==="buy"?"lvg":"lvr"}">${sh(m.wallet)}${m.vetted?" ★":""} ${esc(m.side)}</td><td>${sh(m.mint)}</td><td>${n(m.sol,2)}</td><td>${ago(now-T(m.at))}</td></tr>`))}</div>`;
    const R=D.treasury||{};
    $("#lv-treas-b").innerHTML=`<div class="lvkv">${(R.venues||[]).map(v=>{const ag=v.last_ok_at?(now-T(v.last_ok_at))/1e3:NaN,f=v.status==="excluded"?"lvm":v.status!=="ok"||!(ag<=360)?"lvred":ag>150?"lvamber":"";return`<span>${esc(v.venue)}</span><span class="${f}" title="${esc(v.error||"")}"><b>${v.usd==null?"—":"$"+n(v.usd,2)}</b> <span class="lvm">·</span> <i class="lvdot" style="width:6px;height:6px"></i> ${v.status==="ok"?"read "+ago(ag*1e3)+" ago"+(v.latency_ms!=null?" ("+v.latency_ms+"ms)":""):esc(v.status)+(v.last_ok_at?" · last good "+ago(ag*1e3)+" ago":" · never read")}</span>`}).join("")}<span class="lvm">total</span><b>$${n(R.total_usd,2)}</b><span class="lvm">net deposits</span><span>$${n(R.net_deposits_usd,2)}${R.bonus_usd?` <span class="lvm">(yours $${n(R.owner_deposits_usd,2)} + promo bonus $${n(R.bonus_usd,2)}, not profit)</span>`:""}</span><span class="lvm">PnL vs deposits</span><span class="${cls(R.total_usd-R.net_deposits_usd)}">$${n(R.total_usd-R.net_deposits_usd,2)}</span><span class="lvm">status</span><span>${esc(R.status||"")}</span></div>`;
    const H=D.pm_history||{};
    $("#lv-pmh-b").innerHTML=`${(()=>{const u=H.polymarket_us||{},k=H.kalshi_train||{};return`<div class="lvkv"><span class="lvm">PM-US markets</span><b>${(u.markets||0).toLocaleString()}</b><span class="lvm">quotes</span><span>${(u.quotes||0).toLocaleString()}</span><span class="lvm">settled</span><span>${u.settled||0}</span><span class="lvm">Kalshi train rows</span><span>${(k.rows||0).toLocaleString()}</span></div>`+tbl(["category","PM-US markets","Kalshi rows"],[...new Set([...Object.keys(u.by_category||{}),...Object.keys(k.by_category||{})])].map(c=>`<tr><td>${esc(c)}</td><td>${(u.by_category||{})[c]??"—"}</td><td>${(k.by_category||{})[c]??"—"}</td></tr>`))})()}`;
    const N=D.near_close||{};
    $("#lv-near-b").innerHTML=`<div class="lvsc" style="max-height:220px">${`<div class="lvm">${N.report?esc(N.report.mode)+" · "+(N.report.rows||0).toLocaleString()+" rows / "+(N.report.events||0).toLocaleString()+" events · report "+ago(now-T(N.report_at))+" ago":""}</div>`+tbl(["ticker","lead","bid","ask","mid","vol10"],(N.rows||[]).map(r=>`<tr><td title="${esc(r.ticker)}">${esc(r.ticker)}</td><td>${r.lead_s}s</td><td>${n(r.bid,2)}</td><td>${n(r.ask,2)}</td><td>${n(r.mid,3)}</td><td>${n(r.vol10,0)}</td></tr>`))}</div>`;
    const X=D.xv||{};
    $("#lv-xv-b").innerHTML=`<div class="lvsc" style="max-height:220px">${tbl(["PM ↔ Kalshi","gap","cost","edge","verdict","age"],(X.rows||[]).map(r=>`<tr><td title="${esc(r.pm+" ↔ "+r.kalshi)}">${esc(r.pm)} <span class="lvm">↔ ${esc(r.kalshi)}</span></td><td>${n(r.raw_gap,3)}</td><td>${n(r.cost,3)}</td><td class="${+r.edge>=0.01?"lvg":"lvm"}">${n(r.edge,3)}</td><td>${esc(r.verdict)} · <span class="${/clears/.test(r.gate)?"lvg":"lvm"}">${esc(r.gate||"")}</span></td><td>${ago(now-T(r.at))}</td></tr>`))}</div>`;
    drawCandle(); fresh();
  }
  function drawCandle(){ const C=(D&&D.candles&&D.candles.charts)||[],cv=$("#lv-cv"),info=$("#lv-cinfo"); if(!cv)return;
    if(!C.length){info.innerHTML='<span class="lvm">no fresh launch with ≥12 captured trades yet</span>';return}
    const c=C[ci%C.length], r=cv.getBoundingClientRect(); cv.width=Math.max(300,r.width)*devicePixelRatio; cv.height=Math.max(200,r.height)*devicePixelRatio;
    info.innerHTML=`<b class="lvy">${esc(c.sym||sh(c.mint))}</b><span class="lvm">${esc(c.name||"")}</span><span class="lvchip">found ${ago(Date.now()-T(c.found_at))} ago</span><span class="lvchip">${c.n_trades} trades · tf ${c.tf_s}s</span><span class="lvm">${ci%C.length+1}/${C.length}</span>`;
    if(window.TEVCandles)TEVCandles.draw(cv,c.rows,{interval:c.tf_s,label:"mcap ◎ · our captured trades",markers:[{t:Math.floor(T(c.found_at)/1e3),kind:"find"}],volLabel:"◎ vol"});
  }
  function onData(d){const t=Date.now();if(lastPush)gaps=gaps.concat([t-lastPush]).slice(-10);lastPush=t;D=d;try{render()}catch(e){console.error(e)}}
  let poll=null; function startPoll(){if(poll)return;mode="poll /api/live 5s";const f=()=>fetch("/api/live",{cache:"no-store"}).then(r=>r.json()).then(onData).catch(()=>{});f();poll=setInterval(f,5000)}
  function startSSE(){ if(!window.EventSource)return startPoll(); const es=new EventSource("/api/live/stream"); let ok=false;
    es.onmessage=(e)=>{ok=true;mode="SSE push";if(poll){clearInterval(poll);poll=null}onData(JSON.parse(e.data))};
    es.onerror=()=>{if(!ok||es.readyState===2){startPoll()}};
    setTimeout(()=>{if(!ok)startPoll()},8000);}
  function init(){mount();startSSE();setInterval(fresh,1000);setInterval(()=>{ci++;drawCandle()},5000);addEventListener("resize",drawCandle)}
  document.readyState==="loading"?document.addEventListener("DOMContentLoaded",init):init();
})();
