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
 const right=document.querySelector(".right");if(right&&!byId("v7-eqp"))right.insertAdjacentHTML("afterbegin",`<section class="pnl v7p" id="v7-eqp"><h2>TREASURY · REAL MONEY<span class="tag" id="v7-eq-t">read-only equity feed</span></h2><div class="body"><div id="v7-tr-head"></div><canvas id="v7-eq"></canvas><div class="v7-kv" id="v7-eq-kv"></div><div id="v7-tr-paper"></div></div></section><section class="pnl v7p" id="v7-pmc"><h2>PREDICTION MKT CANDLES<span class="tag" id="v7-pmc-t">our stored quotes → OHLC</span></h2><div class="body"><canvas id="v7-pmc-cv"></canvas><div class="dim" id="v7-pmc-s"></div></div></section><section class="pnl v7p" id="v7-stp"><h2>STRIPE · FINANCES<span class="tag">read-only · never funds trades</span></h2><div class="body" id="v7-stripe"><div class="empty">loading…</div></div></section>`);
 const left=document.querySelector(".left");/* heat map removed: NEWS wire takes its place in the left column (see room-v6 aw-news) */if(false)left.insertAdjacentHTML("beforeend",`<section class="pnl v7p" id="v7-hp" style="flex:0 0 auto;max-height:34%"><h2>HEAT MAP<span class="tag" id="v7-heat-t">scanned pools · 5m move</span></h2><div class="body"><div id="v7-heat"></div></div></section>`);
 document.body.insertAdjacentHTML("beforeend",`<svg id="v7-links"></svg><div id="v7-flash"></div><div id="v7-keys"><div><h3>TEV · SHORTCUTS</h3><p><kbd>?</kbd>this overlay</p><p><kbd>S</kbd>decision sound <b id="v7-k-s"></b></p><p><kbd>F</kbd>decision flash <b id="v7-k-f"></b></p><p><kbd>P</kbd>pause / resume scan lane</p><p><kbd>A</kbd>prediction board: show all / actionable</p><p><kbd>R</kbd>refresh all feeds now</p><p><kbd>Esc</kbd>close</p><p class="dim">Cmd-R reload · Cmd-F full screen (Mac app)</p></div></div>`);
 const ch=byId("chain");if(bay&&ch)bay.after(ch);
 if(bay&&!byId("v7-cg")){(ch||bay).insertAdjacentHTML("afterend",`<div id="v7-cg"></div>`)}
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
/* real OHLC candles (GeckoTerminal) via tev-candles.js; never a price line */
function miniChart(cv,it,lab){if(!window.TEVCandles)return;TEVCandles.draw(cv,it.ohlc||[],{interval:it.interval,label:lab,key:lab,markers:it.markers||[],empty:"no candle data from GeckoTerminal for this pool"})}
/* pools with real candles first so the chart dwell shows candles; the rest still replay after */
T.chartFirst=L=>L.slice(); // server order = newest find first (novelty); never float old cached-candle tokens to the top
function scanNext(){if(V.paused)return;const f=(V.d||{}).funnel||{},L=T.chartFirst(f.scan||[]),lane=byId("v7-scan");if(!lane)return;
 if(!L.length){byId("v7-sc-t").textContent="no sweep recorded yet — the lane only shows real scanned pools";return}
 if(V.scanCycle!==f.cycle_id){V.scanCycle=f.cycle_id;V.scanI=0}
 const i=V.scanI++%L.length,it=L[i],ck=T.checksFor(it),fail=ck.some(c=>c.state==="f");
 byId("v7-sc-t").innerHTML=`sweep <b>#${E(f.cycle_id)}</b> · ${HM(f.ts)} · replaying real crawl results`;byId("v7-sc-n").textContent=`${i+1}/${L.length}`;
 lane.querySelectorAll(".v7c").forEach(o=>{o.classList.add(o.dataset.fail==="1"?"drop":"fly");setTimeout(()=>o.remove(),800)});
 const px=it.price_usd==null?"—":(+it.price_usd<1?(+it.price_usd).toPrecision(4):N(it.price_usd));
 lane.insertAdjacentHTML("beforeend",`<div class="v7c${(it.ohlc||[]).length>1?" has-c":""}" data-fail="${fail?1:0}"><div><div class="sy">${E(it.symbol||"?")}<small>${E(it.source||"")} · ${E((it.mint||"").slice(0,4))}…</small></div><div class="mt"><span>px <b>${px}</b></span><span>liq <b>${U(it.liquidity_usd)}</b></span><span>vol1h <b>${U(it.volume_h1_usd)}</b></span><span>5m <b class="${it.price_change_m5<0?"bad":"ok"}">${N(it.price_change_m5)}%</b></span><span>age <b>${N(it.pool_age_hours,1)}h</b></span>${it.trades_h1!=null?`<span>trades1h <b>${E(it.trades_h1)}</b></span>`:""}${it.probability!=null?`<span>judge p <b>${N(it.probability)}</b></span>`:""}</div></div><canvas></canvas><div class="ck">${ck.map(c=>`<div data-s="${c.state}"><i>·</i>${E(c.name)}</div>`).join("")}</div><div class="stp ${fail?"rej":"pass"}">${fail?"REJECTED":"PASSED"}<small>${E(it.reason||it.stage||"")}</small></div></div>`);
 const card=lane.lastElementChild;requestAnimationFrame(()=>{card.classList.add("in");const cv=card.querySelector("canvas");miniChart(cv,it,`${i+1}/${L.length} ${it.symbol||"?"}`)});
 const rows=[...card.querySelectorAll(".ck div")];rows.forEach((r,k)=>setTimeout(()=>{if(!card.isConnected)return;r.classList.add("on",r.dataset.s);r.querySelector("i").textContent=r.dataset.s==="p"?"✔":r.dataset.s==="f"?"✖":"–"},450+k*170));
 setTimeout(()=>{if(!card.isConnected)return;card.querySelector(".stp").classList.add("on");if(!fail){const j=byId("v7-judge");j.classList.remove("hit");void j.offsetWidth;j.classList.add("hit");link("ag-judge",card)}},450+rows.length*170+120)}
function pmCandles(){const cv=byId("v7-pmc-cv");if(!cv||!window.TEVCandles)return;const P=(V.d||{}).pm_candles||{},M=P.markets||[];
 if(!M.length){TEVCandles.draw(cv,[],{key:"none",empty:"no candle data · "+(P.status||"waiting")});byId("v7-pmc-s").textContent=P.quotes_seen!=null?`${P.quotes_seen} quotes stored across ${P.markets_seen} markets in ${P.lookback_hours}h — need ${P.min_quotes}+ per market`:"";return}
 V.pmI=((V.pmI??-1)+1)%M.length;const m=M[V.pmI],lab=`${V.pmI+1}/${M.length} ${String(m.title||m.market_id).slice(0,28)}`;
 TEVCandles.draw(cv,m.ohlc,{interval:m.interval,label:lab,key:m.venue+m.market_id,markers:m.markers,volLabel:"quotes",empty:"no candle data"});
 byId("v7-pmc-s").textContent=`${m.venue} · ${m.market_id} · yes mid · ${m.quotes} quotes · ${m.interval} auto`}
/* candle grid: 2 charts (laptop) / 4 (big monitor), each cycling different real finds, staggered */
T.candlePool=d=>{const sc=((d||{}).funnel||{}).scan||[],pm=((d||{}).pm_candles||{}).markets||[];
 return sc.filter(it=>(it.ohlc||[]).length>1).map(it=>({key:"p:"+(it.mint||it.symbol),sym:it.symbol||"?",src:"candles: "+(it.candle_source||"GeckoTerminal"),ohlc:it.ohlc,interval:it.interval,markers:it.markers||[]}))
  .concat(pm.map(m=>({key:"m:"+m.venue+m.market_id,sym:String(m.title||m.market_id).slice(0,24),src:m.venue+" quotes",ohlc:m.ohlc,interval:m.interval,markers:m.markers||[],vol:"quotes"})))};
T.nextPick=(pool,cur,taken)=>{if(!pool.length)return -1;for(let s=1;s<=pool.length;s++){const j=((cur<0?-1:cur)+s)%pool.length;if(!taken.includes(pool[j].key)||pool.length<=taken.length)return j}return cur};
function cgN(){return window.innerWidth>=2200&&window.innerHeight>=1200?4:2}
function cgBuild(){const g=byId("v7-cg");if(!g)return null;const cm=document.querySelector(".ch-main");if(cm&&g.parentElement!==cm)cm.insertBefore(g,cm.firstChild);const n=cgN();if(g.childElementCount!==n){g.dataset.n=n;g.innerHTML=Array.from({length:n},(_,k)=>`<div class="cgc"><canvas data-k="${k}"></canvas></div>`).join("");V.cg=Array.from({length:n},(_,k)=>({i:k-1,key:null}))}return g}
function cgTick(k){const g=cgBuild();if(!g||!window.TEVCandles)return;const pool=T.candlePool(V.d),S=V.cg[k],cv=g.querySelectorAll("canvas")[k];if(!cv)return;
 if(!pool.length){TEVCandles.draw(cv,[],{key:"none",empty:"no candle data yet · waiting for GeckoTerminal OHLCV / stored quotes"});return}
 const taken=V.cg.filter((_,j)=>j!==k).map(o=>o.key);S.i=T.nextPick(pool,S.i,taken);const it=pool[S.i];S.key=it.key;
 TEVCandles.draw(cv,it.ohlc,{interval:it.interval,key:it.key,label:`${S.i+1}/${pool.length} ${it.sym} · ${it.src}`,markers:it.markers,volLabel:it.vol||"V"})}
function cgRefresh(){const g=cgBuild();if(!g||!window.TEVCandles)return;const pool=T.candlePool(V.d);(V.cg||[]).forEach((S,k)=>{const it=pool.find(p=>p.key===S.key),cv=g.querySelectorAll("canvas")[k];
 if(it&&cv)TEVCandles.draw(cv,it.ohlc,{interval:it.interval,key:it.key,label:`${pool.indexOf(it)+1}/${pool.length} ${it.sym} · ${it.src}`,markers:it.markers,volLabel:it.vol||"V"})})}
function cgBoot(){const D=10000;[0,1,2,3].forEach(k=>setTimeout(()=>{cgTick(k);every(()=>{if(k<cgN())cgTick(k)},D)},700+k*D/4));every(cgRefresh,30000);window.addEventListener("resize",()=>{cgBuild();cgRefresh()})}
function link(fromId,toEl){const a=byId(fromId),svg=byId("v7-links");if(!a||!svg||!toEl)return;const r1=a.getBoundingClientRect(),r2=toEl.getBoundingClientRect();
 const x1=r1.right,y1=r1.top+r1.height/2,x2=r2.left,y2=r2.top+r2.height/2,mx=(x1+x2)/2;
 const p=document.createElementNS("http://www.w3.org/2000/svg","path");p.setAttribute("d",`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);svg.appendChild(p);vote(fromId.slice(3));setTimeout(()=>p.remove(),1600);while(svg.childNodes.length>6)svg.firstChild.remove()}
function pumpfun(){const f=(V.d||{}).funnel||{},el=byId("v7-pf");if(!el)return;const txt=f.pumpfun_new!=null?`PUMP.FUN · ${f.pumpfun_new} new launches seen in sweep #${f.cycle_id} (${HM(f.ts)}) · research feed only, never auto-bought`:"pump.fun: no sweep yet";if(el.textContent!==txt)el.textContent=txt}

/* ---------- latest candidates (replaces the CRAWLED/FILTERS/... box row) ----------
 * One row per real pool from the latest sweep: what it is, how it trades, and exactly which
 * filter cut it or what the judge said. Reject reasons are the sweep's own counts. */
T.verdict=it=>{const st=String(it.stage||""),r=String(it.reason||""),ent=/enter|entry|approved|opened/i.test(st)||/^(entered|approved)/i.test(r);
 if(it.probability!=null)return{cls:ent?"ok":"warn",txt:`judge p ${N(it.probability)}${it.confidence!=null?" · conf "+N(it.confidence):""}${r&&!/^model pass/i.test(r)?" · "+r:""}`,out:ent?"ENTERED":/^model pass/i.test(r)?"JUDGE SKIP":st==="rejected"?"VETOED":"JUDGED"};
 if(ent)return{cls:"ok",txt:r||st,out:"ENTERED"};
 if(st!=="rejected"&&(/^passed/i.test(r)||/queued/i.test(r)))return{cls:"ok",txt:r,out:/holder/i.test(r)?"→ HOLDERS":"PASSED"};
 return{cls:"bad",txt:r||st||"—",out:"CUT"}};
T.noveltyLine=n=>{if(!n)return"";const a=n.last_new_token_min_ago;return ` · <b class="${a!=null&&a<=10?"ok":"bad"}">last new token ${a==null?"never":a<1?"<1m":Math.round(a)+"m"} ago</b> · ${E(n.new_unique_tokens_1h)} new/1h · ${E(n.aged_out)} replays aged out`};
T.candRows=(scan,n=12)=>{const rank=it=>it.probability!=null?0:T.verdict(it).cls==="ok"?1:2;
 return (scan||[]).slice().sort((a,b)=>String(b.first_seen_at||"").localeCompare(String(a.first_seen_at||""))||rank(a)-rank(b)).slice(0,n)};
const age=h=>h==null||isNaN(h)?"—":h<1?Math.round(h*60)+"m":h<48?N(h,1)+"h":Math.round(h/24)+"d";
function chainV7(){const el=byId("chain");if(!el)return;const f=(V.d||{}).funnel||{},lt=f.latest;
 if(!el.querySelector(".v7cand")){el.className="v7chain";el.innerHTML=`<div class="v7cand"><div class="v7cand-h" id="v7-cand-h"></div><div class="v7cand-t"><table id="v7-cand"></table></div><div class="v7rj" id="v7-rj"></div></div>`}
 byId("v7-cand-h").innerHTML=lt?`<b>LATEST CANDIDATES</b> · sweep #${E(f.cycle_id)} ${HM(f.ts)} · ${lt.crawled} pools scanned → ${lt.filters} passed filters → ${lt.holders} holder-checked → ${lt.judged} judged → <b class="${lt.entered?"ok":"dim"}">${lt.entered} entered</b>`+T.noveltyLine(f.novelty):"<b>LATEST CANDIDATES</b> · no sweep recorded yet";
 const rows=T.candRows(f.scan),sc=(f.scan||[]).length;
 const h=`<tr><th>token</th><th>age</th><th>liq</th><th>vol 1h</th><th>5m</th><th>filter cut / judge</th><th>outcome</th></tr>`+(rows.map(it=>{const v=T.verdict(it);
  return `<tr title="${E(it.mint)} · ${E(it.source)}"><td><b>${E(it.symbol||"?")}</b></td><td>${age(it.pool_age_hours)}</td><td>${U(it.liquidity_usd)}</td><td>${U(it.volume_h1_usd)}</td><td class="${it.price_change_m5<0?"bad":"ok"}">${N(it.price_change_m5,1)}%</td><td class="v7why" title="${E(v.txt)}">${E(v.txt)}</td><td class="${v.cls}">${v.out}</td></tr>`}).join("")||'<tr><td colspan=7 class="dim">no candidates in the latest sweep</td></tr>')
  +(sc>rows.length?`<tr><td colspan=7 class="dim">+${sc-rows.length} more pools in this sweep (newest finds first; tokens replayed too long are aged out)</td></tr>`:"");
 const t=byId("v7-cand");if(t.dataset.h!==h){t.dataset.h=h;t.innerHTML=h}
 const R=f.rejects||[];byId("v7-rj").innerHTML=R.length?`<span class="dim">TOP CUTS</span>`+R.slice(0,6).map(r=>`<span class="v7cut" title="${E(r.reason)}">${E(r.reason)} <b>${r.count}</b></span>`).join(""):'<span class="dim">no rejects in the last sweep</span>';
 const iv=(SS().tr||{}).interval_seconds;const bt=byId("bay-tag");if(bt&&f.ts&&lt)bt.textContent=`sweep ${HM(f.ts)} · ${N((lt.crawled||0)/(iv||120)*60,1)} pools/min`}

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
function equity(){const cls=v=>v>0?"ok":v<0?"bad":"",T=V.tr,cv=byId("v7-eq");if(!cv)return;const head=byId("v7-tr-head"),kv=byId("v7-eq-kv"),pp=byId("v7-tr-paper");
 if(!T||!T.real){head.innerHTML='<div class="dim">waiting for the first real equity reading…</div>';return}
 const r=T.real,g=T.guard||{},S=(T.series||[]).filter(p=>p.usd!=null),sg=v=>v>=0?"+":"−",pc=v=>v==null?"—":sg(v)+N(Math.abs(v),2)+"%",ago=Math.max(0,Math.round((Date.now()-Date.parse(r.last_reading_at))/1000)),agoS=ago<90?ago+"s":Math.round(ago/60)+"m",stale=ago>900;
 head.innerHTML=`<div class="tr-big">$${N(r.total_usd,4)}<span class="tr-sub">total real balance · last reading <b class="${stale?"bad":"ok"}">${agoS} ago</b>${stale?" (stale)":""}</span></div>
 <div class="tr-pnl"><span>today <b class="${cls(g.pnl_today_usd)}">${sg(g.pnl_today_usd)}$${N(Math.abs(g.pnl_today_usd||0),4)} (${pc(g.pnl_today_pct)})</b></span><span>all-time <b class="${cls(r.pnl_all_usd)}">${sg(r.pnl_all_usd)}$${N(Math.abs(r.pnl_all_usd),4)} (${pc(r.pnl_all_pct)})</b></span><span class="dim">deposits $${N(r.deposits_usd,2)} (not profit)</span></div>
 <div class="tr-ven">${r.venues.map(v=>`<span class="tr-v ${v.status}"><i>${v.label}</i><b>$${N(v.usd,4)}</b></span>`).join("")}<span class="tr-v ro"><i>Stripe</i><b>read-only · not counted</b></span></div>`;
 const W=cv.clientWidth,H=cv.clientHeight,dpr=window.devicePixelRatio||1;cv.width=W*dpr;cv.height=H*dpr;const x=cv.getContext("2d");x.scale(dpr,dpr);x.font="10px monospace";
 if(S.length<2){x.fillStyle="#5f8aa3";x.fillText(`${S.length} reading(s) — chart starts at 2`,4,16)}else{
 const t0=Date.parse(S[0].at),t1=Date.parse(S.at(-1).at)||t0+1,vals=S.map(p=>p.usd),lv=[g.floor_usd,g.peak_usd,g.daily_stop_usd].filter(v=>v!=null),lo=Math.min(...vals,...lv)*.995,hi=Math.max(...vals,...lv)*1.005,
  X=t=>(Date.parse(t)-t0)/((t1-t0)||1)*(W-8)+4,Y=v=>H-12-(v-lo)/((hi-lo)||1)*(H-22);
 const hl=(v,col,lab)=>{if(v==null)return;const y=Y(v);x.strokeStyle=col;x.setLineDash([5,4]);x.beginPath();x.moveTo(0,y);x.lineTo(W,y);x.stroke();x.setLineDash([]);x.fillStyle=col;x.fillText(lab+" $"+N(v,4),W-150,y-3)};
 hl(g.peak_usd,"#7fd4ff","peak");hl(g.daily_stop_usd,"#ff9a3c","daily stop");hl(g.floor_usd,"#ffd23f","floor");
 x.strokeStyle="#39ff9c";x.lineWidth=1.8;x.beginPath();S.forEach((p,i)=>i?x.lineTo(X(p.at),Y(p.usd)):x.moveTo(X(p.at),Y(p.usd)));x.stroke();
 const l=S.at(-1);x.fillStyle="#39ff9c";x.beginPath();x.arc(X(l.at),Y(l.usd),3,0,7);x.fill();
 (T.deposits||[]).forEach(d=>{const dx=X(d.at);if(dx<0||dx>W)return;x.strokeStyle="#b67bff";x.beginPath();x.moveTo(dx,0);x.lineTo(dx,H);x.stroke();x.fillStyle="#b67bff";x.fillText("deposit +$"+N(d.usd,2),dx+3,10)});
 x.fillStyle="#5f8aa3";x.fillText(new Date(t0).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"}),4,H-1);}
 byId("v7-eq-t").textContent=`${S.length} real readings · every 5 min`;
 kv.innerHTML=`<span>floor <b class="warn">$${N(g.floor_usd,4)}</b> (${N((g.floor_fraction||0)*100,0)}% of peak)</span><span>daily stop <b style="color:#ff9a3c">$${N(g.daily_stop_usd,4)}</b> (−${N((g.daily_stop_fraction||0)*100,0)}% from $${N(g.day_start_usd,4)})</span><span>cushion <b>${N((1-g.floor_usd/r.total_usd)*100,1)}%</b></span><span>guard <b class="${g.tripped?"bad":"ok"}">${g.tripped?"TRIPPED":"armed"}</b></span><span class="dim">"today" = UTC day (from 8 PM ET)</span>`;
 const P=T.paper||{},D=(P.desks||[]).filter(p=>p.usd!=null),ab=P.alpha_book,dl=D.at(-1);
 pp.innerHTML=`<div class="tr-paper"><b>PAPER · simulated, not real money</b>${dl?`<span>desk book $${N(dl.usd,2)} <b class="${cls(dl.pnl_usd)}">${sg(dl.pnl_usd)}${N(Math.abs(dl.pnl_usd),2)}</b></span>`:""}${ab?`<span>alpha book cash $${N(ab.cash_usd,2)} of $${N(ab.start_usd,0)}</span>`:""}<canvas id="v7-tr-pcv"></canvas></div>`;
 const pc2=byId("v7-tr-pcv");if(pc2&&D.length>1){const w=pc2.clientWidth,h=pc2.clientHeight,c2=pc2.getContext("2d");pc2.width=w*dpr;pc2.height=h*dpr;c2.scale(dpr,dpr);const v=D.map(p=>p.usd),a=Math.min(...v),b=Math.max(...v);c2.strokeStyle="rgba(182,123,255,.9)";c2.lineWidth=1.3;c2.beginPath();v.forEach((y,i)=>{const X=i/(v.length-1)*w,YY=h-2-(y-a)/((b-a)||1)*(h-4);i?c2.lineTo(X,YY):c2.moveTo(X,YY)});c2.stroke()}}
async function pollTreasury(){try{const r=await fetch("/api/treasury",{cache:"no-store"});if(r.ok){V.tr=await r.json();equity()}}catch(e){}}
setTimeout(pollTreasury,300);setInterval(pollTreasury,30000);setInterval(()=>{try{equity()}catch(e){}},15000);

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
function render7(){for(const f of [tape,pulse,pumpfun,chainV7,equity,stripe,heat,window.dial&&(()=>window.dial(null))])try{f&&f()}catch(e){console.error(e)}}
window.chain=chainV7;
function boot(){scaffold();poll();every(poll,2000);every(scanNext,10000);every(pmCandles,10000);cgBoot();setTimeout(pmCandles,900);every(()=>{try{window.wireSources();window.pm()}catch(e){}},5000);setTimeout(scanNext,400);
 window.addEventListener("resize",()=>{V.eqN=-1;equity()})}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",boot);else boot();
})();
