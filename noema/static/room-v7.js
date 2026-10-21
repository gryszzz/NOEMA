/* TEV room v7 overlay. Loaded after trading-room-v6.html's own script.
 * Every number and animation is driven by /api/room-v7 (+ v6 state S). When a feed is
 * empty we show that plainly; nothing here invents data. */
(()=>{"use strict";
const V={d:null,since:0,ev:[],seenEv:new Set(),sound:localStorage.v7sound==="1",flash:localStorage.v7flash!=="0",
  paused:false,pmAll:false,scanI:0,scanCycle:null,timers:[],raf:0,parts:[],lastFunnelId:null,booted:false};
const T=window.TEV_V7=V; /* exposed for tests/debug */
const E=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const N=(v,d=2)=>v==null||isNaN(v)?"—":(+v).toLocaleString(undefined,{maximumFractionDigits:d,minimumFractionDigits:Math.min(d,2)});
const U=v=>v==null||isNaN(v)?"—":"$"+(Math.abs(v)>=1e6?N(v/1e6,2)+"M":Math.abs(v)>=1e3?N(v/1e3,1)+"k":N(v,2));
const HM=t=>{const d=new Date(String(t).replace(" ","T")+(/[zZ]|[+-]\d\d:?\d\d$/.test(t)?"":"Z"));return isNaN(d)?"—":d.toLocaleTimeString([],{hour12:false})};
const byId=id=>document.getElementById(id);
const SS=()=>{try{return S||{}}catch(e){return {}}}; /* v6 state (lexical global) */
const every=(fn,ms)=>{const h=setInterval(()=>{try{fn()}catch(e){console.error(e)}},ms);V.timers.push(h);return h};
const SPORTS=/\b(nfl|nba|mlb|nhl|wnba|ncaa|ufc|mma|epl|premier league|la liga|serie a|bundesliga|champions league|mls|atp|wta|pga|f1|grand prix|esports?|lol|league of legends|dota|cs2|csgo|valorant|chess|fide|warriors|lakers|celtics|yankees|dodgers|vs\.?|match|game \d|set \d|map \d|points?|rebounds|assists|touchdowns?|goals?|wins? the series|player props?)\b/i;
T.isSportsOrSettled=(r)=>{const p=+r.market_p;return SPORTS.test(r.title||r.market_id||"")||(!isNaN(p)&&(p>=0.95||p<=0.05))};

/* ---------- scaffold ---------- */
function scaffold(){const app=document.querySelector(".app"),hdr=app&&app.querySelector("header");if(!hdr||byId("v7-tape"))return;
 hdr.insertAdjacentHTML("afterend",`<div id="v7-tape" title="real values only; hover to pause"><div class="trk" id="v7-trk"></div></div><div id="v7-pulse"><span class="think">TEV IS THINKING ▸</span><span class="hl" id="v7-hl">connecting to /api/room-v7…</span><span class="v7-regime UNKNOWN" id="v7-reg" title="">REGIME —</span><div class="tl" id="v7-tl" title="last real desk events (green approve/entry · red veto/fire · cyan sweep)"></div></div>`);
 const bay=document.querySelector(".bay");if(bay&&!byId("v7-scan"))bay.insertAdjacentHTML("afterbegin",`<div id="v7-scan"><div class="lh"><b>SCAN LANE</b><span id="v7-sc-t">waiting for a real sweep…</span><span style="margin-left:auto" id="v7-sc-n"></span></div><div class="judge" id="v7-judge">TO<br>JUDGE</div><div class="pf"><span id="v7-pf">pump.fun new launches: waiting for sweep</span></div></div>`);
 const right=document.querySelector(".right");if(right&&!byId("v7-eqp"))right.insertAdjacentHTML("afterbegin",`<section class="pnl v7p" id="v7-eqp"><h2>EQUITY vs FLOOR<span class="tag" id="v7-eq-t">real equity feed</span></h2><div class="body"><canvas id="v7-eq"></canvas><div class="v7-kv" id="v7-eq-kv"></div></div></section><section class="pnl v7p" id="v7-stp"><h2>STRIPE · FINANCES<span class="tag">read-only · never funds trades</span></h2><div class="body" id="v7-stripe"><div class="empty">loading…</div></div></section>`);
 const left=document.querySelector(".left");if(left&&!byId("v7-hp"))left.insertAdjacentHTML("beforeend",`<section class="pnl v7p" id="v7-hp" style="flex:0 0 auto;max-height:34%"><h2>HEAT MAP<span class="tag" id="v7-heat-t">scanned pools · 5m move</span></h2><div class="body"><div id="v7-heat"></div></div></section>`);
 document.body.insertAdjacentHTML("beforeend",`<svg id="v7-links"></svg><div id="v7-flash"></div><div id="v7-keys"><div><h3>TEV · SHORTCUTS</h3><p><kbd>?</kbd>this overlay</p><p><kbd>S</kbd>decision sound <b id="v7-k-s"></b></p><p><kbd>F</kbd>decision flash <b id="v7-k-f"></b></p><p><kbd>P</kbd>pause / resume scan lane</p><p><kbd>A</kbd>prediction board: show all / actionable</p><p><kbd>R</kbd>refresh all feeds now</p><p><kbd>Esc</kbd>close</p><p class="dim">Cmd-R reload · Cmd-F full screen (Mac app)</p></div></div>`);
 const ch=byId("chain");if(bay&&ch)bay.after(ch);
 V.booted=true}

/* ---------- ticker ---------- */
function tape(){const d=V.d||{},g=(d.equity||{}).guard,px=d.prices||{},tr=SS().tr||{};
 const openPos=(tr.seats||[]).reduce((a,s)=>a+(+s.open_positions||0),0);const it=[];
 const cls=v=>v>0?"up":v<0?"dn":"";
 it.push(["EQUITY",g?U(g.equity_usd):"—"],["FLOOR",g?U(g.floor_usd):"—"],["PEAK",g?U(g.peak_usd):"—"]);
 it.push(["DAY P&L",g?`<span class="${cls(g.day_pnl_usd)}">${g.day_pnl_usd>=0?"+":""}${N(g.day_pnl_usd,4)}</span>`:"—"]);
 for(const k of ["BTC","SOL","ETH"]){const p=px[k];it.push([k,p?`${U(p.usd)}${p.change_24h_pct!=null?` <span class="${cls(p.change_24h_pct)}">${p.change_24h_pct>0?"+":""}${N(p.change_24h_pct,2)}%</span>`:""}`:"—"])}
 it.push(["PAPER POSITIONS",tr.seats?String(openPos):"—"]);
 it.push(["PAPER DESK P&L",tr.total_pnl_after_costs_usd!=null?`<span class="${cls(tr.total_pnl_after_costs_usd)}">${N(tr.total_pnl_after_costs_usd)}</span>`:"—"]);
 it.push(["ARTICLES/MIN",(d.news||{}).articles_per_min!=null?N(d.news.articles_per_min,2):"—"]);
 const fl=(d.funnel||{}).latest;it.push(["LAST SWEEP",fl?`${fl.crawled} pools · ${fl.entered} entered`:"—"]);
 it.push(["REGIME",E((d.regime||{}).label||"—")],["LIVE FLAGS",(tr.flags||{}).any_live?'<span class="dn">ON</span>':'<span class="up">ALL OFF</span>']);
 const html=it.map(([a,b])=>`<span class="it">${a}<b>${b}</b></span>`).join("");const trk=byId("v7-trk");
 if(trk&&trk.dataset.h!==html){trk.dataset.h=html;trk.innerHTML=html+html}}

/* ---------- headline + pulse timeline + regime ---------- */
function typeTo(el,txt){if(!el||el.dataset.full===txt)return;el.dataset.full=txt;clearInterval(el._t);let i=0;el._t=setInterval(()=>{el.textContent=txt.slice(0,i+=3);if(i>=txt.length)clearInterval(el._t)},16)}
function pulse(){const d=V.d||{};typeTo(byId("v7-hl"),d.headline||"—");
 const r=d.regime||{},re=byId("v7-reg");if(re){re.className="v7-regime "+(r.label||"UNKNOWN");re.textContent="REGIME "+(r.label||"—");re.title=`${r.basis||""}${r.breadth!=null?` · breadth ${N(r.breadth*100,0)}% up · median 5m ${N(r.median_m5_pct)}% · dispersion ${N(r.dispersion)}`:""}`}
 const tl=byId("v7-tl");if(!tl||!V.ev.length)return;const ev=V.ev.slice(0,60),t1=Date.now(),t0=Math.min(...ev.map(e=>+new Date(e.ts)))||t1-3600e3,span=Math.max(60e3,t1-t0);
 const frag=ev.map(e=>`<i class="${E(e.kind)}${e._new?" new":""}" style="left:${((+new Date(e.ts)-t0)/span*98+1).toFixed(2)}%" title="${E(HM(e.ts)+" "+e.text)}"></i>`).join("");
 tl.innerHTML=frag;ev.forEach(e=>e._new=false)}

/* ---------- decisions: sound / flash / agent vote pulse ---------- */
let AC=null;function beep(ok){if(!V.sound)return;try{AC=AC||new (window.AudioContext||window.webkitAudioContext)();const o=AC.createOscillator(),g=AC.createGain();o.frequency.value=ok?880:330;o.type="triangle";g.gain.setValueAtTime(.08,AC.currentTime);g.gain.exponentialRampToValueAtTime(.0001,AC.currentTime+.35);o.connect(g).connect(AC.destination);o.start();o.stop(AC.currentTime+.36)}catch(e){}}
function flash(ok){if(!V.flash)return;const f=byId("v7-flash");if(!f)return;f.className="on"+(ok?"":" v");requestAnimationFrame(()=>requestAnimationFrame(()=>f.className=ok?"":"v"))}
function vote(id){const a=byId("ag-"+id);if(!a)return;a.classList.remove("v7vote");void a.offsetWidth;a.classList.add("v7vote")}
function ingest(events,initial){for(const e of events.slice().reverse()){if(V.seenEv.has(e.id))continue;V.seenEv.add(e.id);e._new=!initial;V.ev.unshift(e);V.since=Math.max(V.since,e.id);
  if(initial)continue;
  if(e.event==="chief_decision"){vote("chief");beep(e.kind==="approve");flash(e.kind==="approve")}
  else if(e.event==="paper_entry"){if(e.desk)vote(e.desk);beep(true);flash(true)}
  else if(e.event==="desk_cycle"){vote("judge")}}
 V.ev.length=Math.min(V.ev.length,120);if(V.seenEv.size>500)V.seenEv=new Set(V.ev.map(e=>e.id))}

/* ---------- agent brains (confidence + Brier) ---------- */
function brains(){const d=V.d||{},acc=(SS().accuracy||{}).agents||(SS().v6||{}).accuracy_leaderboard||[];
 const find=k=>(acc||[]).find(a=>(a.agent_id||a.agent||"").includes(k));const last=(d.chief||{}).last;
 const set=(id,conf,brier,label)=>{const a=byId("ag-"+id);if(!a)return;let b=a.querySelector(".v7b");if(!b){a.insertAdjacentHTML("beforeend",'<div class="v7b"><span data-l></span><div class="cb"><i></i></div><span data-b></span></div>');b=a.querySelector(".v7b")}
  b.querySelector("[data-l]").textContent=label;b.querySelector(".cb i").style.width=conf==null?"0":(conf*100)+"%";b.querySelector("[data-b]").textContent=brier==null?"brier — (unresolved)":"brier "+N(brier,3)};
 const j=find("judge");set("judge",last?last.confidence:null,j?j.brier:null,last?`conf ${N(last.confidence)}`:"conf —");
 const c=find("chief");set("chief",last?last.probability:null,c?c.brier:null,last?`p ${N(last.probability)}`:"p —")}

/* ---------- scan lane (replays the latest real sweep, item by item) ---------- */
const CHECKS=[["liquidity",/liquidity/i],["pool age",/younger|age/i],["5m spike",/spike|accelerat|5m move/i],["top-10 holders",/holder|concentration/i],["TEV judge",/judge|model|p=|probab/i],["CHIEF gate",/chief|veto|gate/i]];
T.checksFor=(it)=>{const st=String(it.stage||""),rs=String(it.reason||"");let upto,fail=-1;
 if(st==="chief_veto"){upto=5;fail=5}else if(/approve|entry|entered|buy/.test(st)){upto=6}else if(st==="holders"){upto=4}else if(st==="filters"){upto=3}
 else{fail=CHECKS.findIndex(c=>c[1].test(rs));if(fail<0)fail=0;upto=fail}
 return CHECKS.map((c,i)=>({name:c[0],state:i<upto?"p":i===fail?"f":"s"}))};
/* provider returned no candles: draw this pool's real metrics instead of an empty box */
T.metricRows=(it)=>[["liq",+it.liquidity_usd,v=>Math.min(1,Math.log10(1+v)/6)],["vol1h",+it.volume_h1_usd,v=>Math.min(1,Math.log10(1+v)/6)],
 ["5m %",+it.price_change_m5,v=>Math.min(1,Math.abs(v)/25)],["age h",+it.pool_age_hours,v=>Math.min(1,v/48)],["trades1h",+it.trades_h1,v=>Math.min(1,Math.log10(1+v)/4)]].filter(r=>isFinite(r[1]));
function metricBars(x,W,H,it){const R=T.metricRows(it);x.font="9px monospace";x.fillStyle="#5f8aa3";x.fillText("no candles from provider · live pool metrics",4,10);
 if(!R.length)return;const bh=Math.max(5,Math.min(12,(H-16)/R.length-3));let k=0;
 const step=()=>{k=Math.min(1,k+.08);R.forEach((r,i)=>{const y=14+i*(bh+3),w=(W-64)*r[2](r[1])*k;x.clearRect(56,y,W-56,bh);x.fillStyle="#5f8aa3";x.fillText(r[0],4,y+bh-1);
  x.fillStyle=r[0]==="5m %"?(r[1]<0?"#ff3b6b":"#39ff9c"):"#27e7ff";x.fillRect(56,y,Math.max(1,w),bh)});if(k<1)requestAnimationFrame(step)};step()}
function miniChart(cv,c){const W=cv.clientWidth,H=cv.clientHeight,dpr=window.devicePixelRatio||1;if(!W||!H)return;cv.width=W*dpr;cv.height=H*dpr;const x=cv.getContext("2d");x.scale(dpr,dpr);
 if(!c||c.length<2){metricBars(x,W,H,cv._it||{});return}
 const hi=Math.max(...c),lo=Math.min(...c),r=(hi-lo)||1,up=c.at(-1)>=c[0];let k=1;x.lineWidth=1.6;x.strokeStyle=up?"#39ff9c":"#ff3b6b";
 const P=i=>[i/(c.length-1)*W,H-3-(c[i]-lo)/r*(H-6)];
 const step=()=>{if(!cv.isConnected)return;for(let s=0;s<3&&k<c.length;s++,k++){x.beginPath();x.moveTo(...P(k-1));x.lineTo(...P(k));x.stroke()}if(k<c.length)requestAnimationFrame(step)};step()}
function scanNext(){if(V.paused)return;const f=(V.d||{}).funnel||{},L=f.scan||[],lane=byId("v7-scan");if(!lane)return;
 if(!L.length){byId("v7-sc-t").textContent="no sweep recorded yet — the lane only shows real scanned pools";return}
 if(V.scanCycle!==f.cycle_id){V.scanCycle=f.cycle_id;V.scanI=0}
 const i=V.scanI++%L.length,it=L[i],ck=T.checksFor(it),fail=ck.some(c=>c.state==="f");
 byId("v7-sc-t").innerHTML=`sweep <b>#${E(f.cycle_id)}</b> · ${HM(f.ts)} · replaying real crawl results`;byId("v7-sc-n").textContent=`${i+1}/${L.length}`;
 lane.querySelectorAll(".v7c").forEach(o=>{o.classList.add(o.dataset.fail==="1"?"drop":"fly");setTimeout(()=>o.remove(),800)});
 const px=it.price_usd==null?"—":(+it.price_usd<1?(+it.price_usd).toPrecision(4):N(it.price_usd));
 lane.insertAdjacentHTML("beforeend",`<div class="v7c" data-fail="${fail?1:0}"><div><div class="sy">${E(it.symbol||"?")}<small>${E(it.source||"")} · ${E((it.mint||"").slice(0,4))}…</small></div><div class="mt"><span>px <b>${px}</b></span><span>liq <b>${U(it.liquidity_usd)}</b></span><span>vol1h <b>${U(it.volume_h1_usd)}</b></span><span>5m <b class="${it.price_change_m5<0?"bad":"ok"}">${N(it.price_change_m5)}%</b></span><span>age <b>${N(it.pool_age_hours,1)}h</b></span>${it.trades_h1!=null?`<span>trades1h <b>${E(it.trades_h1)}</b></span>`:""}${it.probability!=null?`<span>judge p <b>${N(it.probability)}</b></span>`:""}</div></div><canvas></canvas><div class="ck">${ck.map(c=>`<div data-s="${c.state}"><i>·</i>${E(c.name)}</div>`).join("")}</div><div class="stp ${fail?"rej":"pass"}">${fail?"REJECTED":"PASSED"}<small>${E(it.reason||it.stage||"")}</small></div></div>`);
 const card=lane.lastElementChild;requestAnimationFrame(()=>{card.classList.add("in");const cv=card.querySelector("canvas");cv._it=it;miniChart(cv,it.closes)});
 const rows=[...card.querySelectorAll(".ck div")];rows.forEach((r,k)=>setTimeout(()=>{if(!card.isConnected)return;r.classList.add("on",r.dataset.s);r.querySelector("i").textContent=r.dataset.s==="p"?"✔":r.dataset.s==="f"?"✖":"–"},450+k*170));
 setTimeout(()=>{if(!card.isConnected)return;card.querySelector(".stp").classList.add("on");if(!fail){const j=byId("v7-judge");j.classList.remove("hit");void j.offsetWidth;j.classList.add("hit");link("ag-judge",card)}},450+rows.length*170+120)}
function link(fromId,toEl){const a=byId(fromId),svg=byId("v7-links");if(!a||!svg||!toEl)return;const r1=a.getBoundingClientRect(),r2=toEl.getBoundingClientRect();
 const x1=r1.right,y1=r1.top+r1.height/2,x2=r2.left,y2=r2.top+r2.height/2,mx=(x1+x2)/2;
 const p=document.createElementNS("http://www.w3.org/2000/svg","path");p.setAttribute("d",`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);svg.appendChild(p);vote(fromId.slice(3));setTimeout(()=>p.remove(),1600);while(svg.childNodes.length>6)svg.firstChild.remove()}
function pumpfun(){const f=(V.d||{}).funnel||{},el=byId("v7-pf");if(!el)return;const txt=f.pumpfun_new!=null?`PUMP.FUN · ${f.pumpfun_new} new launches seen in sweep #${f.cycle_id} (${HM(f.ts)}) · research feed only, never auto-bought`:"pump.fun: no sweep yet";if(el.textContent!==txt)el.textContent=txt}

/* ---------- funnel (replaces the old box row) ---------- */
const ST=[["crawled","CRAWLED"],["filters","FILTERS"],["holders","HOLDERS"],["judged","JUDGED"],["entered","ENTERED"]];
function sparkline(v){if(v.length<2)return"";const hi=Math.max(...v,1),pts=v.map((x,i)=>`${(i/(v.length-1)*56).toFixed(1)},${(11-x/hi*10).toFixed(1)}`).join(" ");return`<polyline fill="none" stroke="#27e7ff" stroke-width="1" points="${pts}"/>`}
function chainV7(){const el=byId("chain");if(!el)return;const f=(V.d||{}).funnel||{},lt=f.latest,ser=f.series||[];
 if(!el.querySelector(".v7f")){el.className="chain";el.innerHTML=`<div class="v7f"><div class="pipe"><canvas id="v7-pc"></canvas>${ST.map((s,i)=>`<div class="st" data-k="${s[0]}" style="left:${i*20}%;width:20%">${s[1]}<b>—</b><svg viewBox="0 0 56 12"></svg></div>`).join("")}</div><div class="rj" id="v7-rj"></div></div>`}
 for(const [k] of ST){const n=el.querySelector(`.st[data-k="${k}"]`);n.querySelector("b").textContent=lt?lt[k]:"—";n.querySelector("svg").innerHTML=sparkline(ser.map(s=>s[k]));n.title=`${k}: last ${ser.length} sweeps`}
 const R=f.rejects||[],mx=Math.max(1,...R.map(r=>r.count));
 byId("v7-rj").innerHTML=R.length?R.slice(0,5).map(r=>`<div title="${E(r.reason)}"><i style="width:${r.count/mx*100}%"></i><span>${E(r.reason)}</span><b>${r.count}</b></div>`).join(""):'<span class="dim">no rejects in the last sweep</span>';
 if(lt&&V.lastFunnelId!==f.cycle_id){V.lastFunnelId=f.cycle_id;spawn(lt)}
 const iv=(SS().tr||{}).interval_seconds;const bt=byId("bay-tag");if(bt&&f.ts)bt.textContent=`sweep ${HM(f.ts)} · ${N((lt.crawled||0)/(iv||120)*60,1)} pools/min`}
/* one particle per real item that reached each stage in the latest sweep (capped for perf) */
function spawn(lt){const cnt=ST.map(s=>lt[s[0]]||0);for(let i=0;i<4;i++){const n=Math.min(cnt[i+1],40);for(let k=0;k<n;k++)V.parts.push({s:i,t:-k*.06,v:.004+Math.random()*.003,y:Math.random()})}
 const drop=Math.min(cnt[0]-cnt[1],60);for(let k=0;k<drop;k++)V.parts.push({s:0,t:-k*.03,v:.005,y:Math.random(),drop:1});if(V.parts.length>400)V.parts.splice(0,V.parts.length-400);if(!V.raf)V.raf=requestAnimationFrame(draw)}
function draw(){V.raf=0;const cv=byId("v7-pc");if(!cv||document.hidden){if(V.parts.length)V.raf=requestAnimationFrame(draw);return}
 const W=cv.clientWidth,H=cv.clientHeight,dpr=window.devicePixelRatio||1;if(cv.width!==W*dpr){cv.width=W*dpr;cv.height=H*dpr}const x=cv.getContext("2d");x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,W,H);
 const lt=((V.d||{}).funnel||{}).latest||{},c0=Math.max(1,lt.crawled||1);
 for(let i=0;i<5;i++){const w=W/5,hgt=H*(.35+.6*Math.sqrt((lt[ST[i][0]]||0)/c0));x.fillStyle="rgba(39,231,255,.07)";x.strokeStyle="rgba(39,231,255,.35)";x.beginPath();x.rect(i*w+4,(H-hgt)/2,w-8,hgt);x.fill();x.stroke()}
 V.parts=V.parts.filter(p=>{p.t+=p.v*3;if(p.t<0)return true;if(p.t>1)return false;const w=W/5,X=(p.s+.5)*w+p.t*w,Y=p.drop?H/2+p.t*H*.6*(p.y>.5?1:-1):H*(.3+.4*p.y);x.fillStyle=p.drop?"rgba(255,59,107,"+(1-p.t)+")":"rgba(57,255,156,.9)";x.fillRect(X,Y,2,2);return true});
 if(V.parts.length)V.raf=requestAnimationFrame(draw)}

/* ---------- CHIEF dial: never dead ---------- */
const dial0=window.dial;window.dial=function(p){const c=(V.d||{}).chief||{};const use=p||c.last;if(typeof dial0==="function")dial0(use);
 const t=byId("dial-t");if(!t)return;if(!use){t.innerHTML=`<b class="warn">CHIEF WAITING</b><br><span class="dim">${E(c.waiting_on||"waiting on the first sweep")}</span>`;return}
 if(!p&&c.last)t.insertAdjacentHTML("beforeend",`<br><span class="dim">last decision ${E(HM(c.last.ts))} · now: ${E(c.waiting_on||"")}</span>`);else if(c.waiting_on&&!t.querySelector("[data-w]"))t.insertAdjacentHTML("beforeend",`<br><span class="dim" data-w>now: ${E(c.waiting_on)}</span>`)};

/* ---------- prediction markets: actionable first ---------- */
T.rankMarkets=(rows,all)=>{const seen=new Set();return rows.filter(r=>{const k=r.venue+r.market_id;if(seen.has(k))return false;seen.add(k);return all||!T.isSportsOrSettled(r)})
 .sort((a,b)=>(Math.abs(+b.edge||0)-Math.abs(+a.edge||0))||((+b.liquidity_usd||+b.volume_usd||0)-(+a.liquidity_usd||+a.volume_usd||0))||(Math.abs(.5-(+a.market_p||0))-Math.abs(.5-(+b.market_p||0))))};
window.pm=function(){const m=(SS().cc||{}).markets||{},w=m.watched_counts||{},el=byId("pm");if(!el)return;
 const all=(m.independent||[]).concat(Object.values(m.latest_by_venue||{}).flat()),rows=T.rankMarkets(all,V.pmAll),hidden=all.length-rows.length;
 byId("pm-tag").textContent=`Kalshi ${w["kalshi:production"]??"—"} · Polymarket US ${w["polymarket-us"]??"—"} watched`;
 const row=r=>`<tr><td>${(r.venue||"").startsWith("kalshi")?"KAL":"PM"}</td><td title="${E(r.title)}">${E(r.title||r.market_id)}</td><td>${N(r.market_p)}</td><td>${N(r.noema_p)}</td><td class="${r.edge>0?"ok":r.edge<0?"bad":"dim"}">${N(r.edge)}</td><td class="dim">${E(r.decision)}</td></tr>`;
 el.innerHTML=`<div class="v7-pmbar">ranked by |edge|, then liquidity, then closeness to 50/50 · <span>${V.pmAll?"showing all":`hiding ${hidden} sports / near-settled (≥0.95 or ≤0.05)`}</span><button id="v7-pmt">${V.pmAll?"actionable only":"show all"} (A)</button></div>`+(rows.length?`<table><tr><th></th><th>market</th><th>mkt p</th><th>TEV p</th><th>edge</th><th>act</th></tr>${rows.slice(0,18).map(row).join("")}</table>`:'<div class="empty">No actionable markets right now (all watched ones are sports or near-settled). Press A to show all.</div>');
 byId("v7-pmt").onclick=()=>{V.pmAll=!V.pmAll;window.pm()}};

/* ---------- compact source health strip + clean target lines ---------- */
T.isBoilerplateQuery=q=>/rulebook|terms and conditions|contract terms|settlement source/i.test(q||"");
window.wireSources=function(){const ev=(SS().v6||{}).lanes?.find(l=>l.lane==="evidence");const el=byId("wire-src");if(!el)return;el.classList.add("v7led");
 const Sx=(ev&&ev.sources)||[],pr=(SS().v6||{}).providers||[];const ok=x=>{const s=String(x.status||"");return s==="200"||s==="304"};
 const bad=Sx.filter(x=>!ok(x)).length,pb=pr.filter(p=>p.state!=="healthy").length;
 el.innerHTML=`<em>NEWS ${Sx.length-bad}/${Sx.length}</em>`+Sx.map(x=>`<i class="${ok(x)?"":"bad"}" title="${E(x.name)} · HTTP ${E(x.status)} · every ${Math.round((x.interval_s||0)/60)}m · reliability ${E(x.reliability)}"></i>`).join("")+`<em>DATA ${pr.length-pb}/${pr.length}</em>`+pr.map(p=>`<i class="${p.state==="healthy"?"":p.consecutive_failures?"bad":"stale"}" title="${E(p.provider)} · ${E(p.state)} · ${E(p.consecutive_failures)} fails"></i>`).join("");
 const T2=((ev&&ev.targets)||[]).filter(t=>!T.isBoilerplateQuery(t.query)),dropped=((ev&&ev.targets)||[]).length-T2.length;
 const tg=byId("wire-tgt");if(tg)tg.innerHTML=T2.slice(0,3).map(t=>`<div><i>TARGET</i> ${E(t.market||"")} ⟵ <i>${E(t.backend)}</i> “${E(t.query||"")}” · ${t.results??0} results · ${t.linked??0} linked</div>`).join("")+(dropped?`<div class="dim">${dropped} rulebook-boilerplate quer${dropped>1?"ies":"y"} hidden</div>`:"")};

/* ---------- equity vs floor ---------- */
function equity(){const e=(V.d||{}).equity||{},cv=byId("v7-eq");if(!cv)return;const g=e.guard,R=(e.real||[]).map(r=>r.total_usd),P=(e.paper||[]).map(r=>+r.equity_usd).filter(x=>!isNaN(x));
 const W=cv.clientWidth,H=cv.clientHeight,dpr=window.devicePixelRatio||1;cv.width=W*dpr;cv.height=H*dpr;const x=cv.getContext("2d");x.scale(dpr,dpr);x.font="10px monospace";
 const line=(v,col,lo,hi,frac)=>{if(v.length<2)return;x.strokeStyle=col;x.lineWidth=1.6;x.beginPath();const n=Math.max(2,Math.ceil(v.length*frac));for(let i=0;i<n;i++){const X=i/(v.length-1)*W,Y=H-6-(v[i]-lo)/((hi-lo)||1)*(H-14);i?x.lineTo(X,Y):x.moveTo(X,Y)}x.stroke()};
 if(R.length>=2&&g){const lo=Math.min(...R,g.floor_usd)*.98,hi=Math.max(...R,g.peak_usd)*1.02,fy=H-6-(g.floor_usd-lo)/((hi-lo)||1)*(H-14);
  x.strokeStyle="#ffd23f";x.setLineDash([5,4]);x.beginPath();x.moveTo(0,fy);x.lineTo(W,fy);x.stroke();x.setLineDash([]);x.fillStyle="#ffd23f";x.fillText("floor "+U(g.floor_usd),4,fy-3);
  let f=0;const anim=()=>{f=Math.min(1,f+.08);x.clearRect(0,0,W,fy-12);line(R,"#39ff9c",lo,hi,f);if(f<1&&cv.isConnected)requestAnimationFrame(anim)};if(V.eqN!==R.length){V.eqN=R.length;anim()}else line(R,"#39ff9c",lo,hi,1)}
 else{x.fillStyle="#5f8aa3";x.fillText(R.length<2?`${R.length} real equity reading(s) so far — curve starts at 2`:"no guard state yet",4,16);if(P.length>=2){line(P,"rgba(182,123,255,.8)",Math.min(...P),Math.max(...P),1);x.fillStyle="#b67bff";x.fillText("paper desk equity",4,H-4)}}
 byId("v7-eq-t").textContent=`${R.length} real readings · ${P.length} paper snapshots`;
 byId("v7-eq-kv").innerHTML=g?`<span>equity <b>${U(g.equity_usd)}</b></span><span>floor <b class="warn">${U(g.floor_usd)}</b></span><span>cushion <b>${N((1-g.floor_usd/g.equity_usd)*100,1)}%</b></span><span>day <b class="${g.day_pnl_usd<0?"bad":"ok"}">${N(g.day_pnl_usd,4)}</b></span>${P.length?`<span>paper desks <b>${U(P.at(-1))}</b></span>`:""}`:'<span>capital guard has no reading yet</span>'}

/* ---------- Stripe ---------- */
const money=(a)=>a&&a.length?a.map(r=>`${N(r.amount_minor/100)} ${E((r.currency||"").toUpperCase())}`).join(" · "):"0";
function stripe(){const s=(V.d||{}).stripe||{},el=byId("v7-stripe");if(!el)return;let h;
 if(s.status==="connected")h=`<div class="v7-kv"><span>available <b>${money(s.available)}</b></span><span>pending <b>${money(s.pending)}</b></span><span>${s.livemode?"LIVE":"TEST"} mode</span></div><table><tr><th>payment</th><th>amount</th><th>status</th></tr>${(s.payments||[]).slice(0,5).map(p=>`<tr><td>${new Date(p.created*1e3).toLocaleDateString()}</td><td>${N(p.amount_minor/100)} ${E((p.currency||"").toUpperCase())}</td><td class="${p.status==="succeeded"?"ok":"warn"}">${E(p.refunded?"refunded":p.status)}</td></tr>`).join("")||'<tr><td class="dim" colspan=3>no payments yet</td></tr>'}</table><table><tr><th>payout</th><th>amount</th><th>status</th></tr>${(s.payouts||[]).slice(0,3).map(p=>`<tr><td>${new Date(p.arrival_date*1e3).toLocaleDateString()}</td><td>${N(p.amount_minor/100)}</td><td>${E(p.status)}</td></tr>`).join("")||'<tr><td class="dim" colspan=3>no payouts yet</td></tr>'}</table>`;
 else h=`<div class="empty"><b class="warn">${s.status==="error"?"STRIPE ERROR":s.status==="refused"?"KEY REFUSED":"NOT CONNECTED"}</b><br>${E(s.reason||"")}</div>`;
 if(el.dataset.h!==h){el.dataset.h=h;el.innerHTML=h}}

/* ---------- heat map ---------- */
function heat(){const el=byId("v7-heat");if(!el)return;const L=((V.d||{}).funnel||{}).scan||[];
 if(!L.length){el.innerHTML='<div class="empty" style="grid-column:1/-1">no scanned pools yet</div>';return}
 const col=v=>{if(v==null)return"#1a2a36";const a=Math.min(1,Math.abs(v)/6);return v>=0?`rgba(57,255,156,${.15+a*.7})`:`rgba(255,59,107,${.15+a*.7})`};
 const h=L.slice().sort((a,b)=>(+b.liquidity_usd||0)-(+a.liquidity_usd||0)).map(s=>`<div style="background:${col(s.price_change_m5)}" title="${E(s.symbol)} · 5m ${N(s.price_change_m5)}% · liq ${U(s.liquidity_usd)} · ${E(s.reason||s.stage)}"><b>${E(s.symbol)}</b>${N(s.price_change_m5,1)}%</div>`).join("");
 if(el.dataset.h!==h){el.dataset.h=h;el.innerHTML=h}byId("v7-heat-t").textContent=`${L.length} pools · color = 5m move · sorted by liquidity`}

/* ---------- keys ---------- */
function keysUI(){byId("v7-k-s").textContent=V.sound?"ON":"OFF";byId("v7-k-f").textContent=V.flash?"ON":"OFF"}
document.addEventListener("keydown",e=>{if(e.metaKey||e.ctrlKey||e.altKey||/input|textarea/i.test(e.target.tagName))return;const k=e.key.toLowerCase(),ov=byId("v7-keys");
 if(k==="?"||(k==="/"&&e.shiftKey)){ov.classList.toggle("on");keysUI()}else if(k==="escape")ov.classList.remove("on");
 else if(k==="s"){V.sound=!V.sound;localStorage.v7sound=V.sound?"1":"0";keysUI();if(V.sound)beep(true)}
 else if(k==="f"){V.flash=!V.flash;localStorage.v7flash=V.flash?"1":"0";keysUI();flash(true)}
 else if(k==="p")V.paused=!V.paused;else if(k==="a"){V.pmAll=!V.pmAll;window.pm()}else if(k==="r"){poll();if(typeof load==="function")load()}});

/* ---------- loop ---------- */
let busy=false;
async function poll(){if(busy||document.hidden)return;busy=true;try{const r=await fetch(`/api/room-v7?since=${V.since}`,{cache:"no-store"});if(r.ok){const d=await r.json();const first=!V.d;V.d=d;ingest(d.events||[],first);render7()}}catch(e){}finally{busy=false}}
function render7(){for(const f of [tape,pulse,brains,pumpfun,chainV7,equity,stripe,heat,window.dial&&(()=>window.dial(null))])try{f&&f()}catch(e){console.error(e)}}
window.chain=chainV7;
function boot(){scaffold();poll();every(poll,2000);every(scanNext,2600);every(()=>{try{window.wireSources();window.pm()}catch(e){}},5000);setTimeout(scanNext,400);
 window.addEventListener("resize",()=>{V.eqN=-1;equity()})}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",boot);else boot();
})();
