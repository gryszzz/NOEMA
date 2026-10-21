/* TEV OPPORTUNITY FEED + CRAWLER SPIDER.
 * Feed: polls /api/opportunities (deduped, first-seen, expiry, price-since-found history).
 * Spider: one strand per real crawler source from /api/room-v7 crawl.latest; the spider walks to
 * a strand only when that source's newest recorded fetch time changes. Strand colour = health.
 * Nothing is seeded: empty sources say so, and the clock shows the age of the last real update. */
(()=>{"use strict";
const byId=id=>document.getElementById(id);
const E=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ts=t=>{const d=new Date(t);return isNaN(d)?null:d.getTime()};
const ago=ms=>{if(ms==null||isNaN(ms))return"—";const s=Math.max(0,Math.round(ms/1e3));return s<60?s+"s":s<3600?Math.floor(s/60)+"m":Math.floor(s/3600)+"h "+Math.floor(s%3600/60)+"m"};
const O={d:null,fetchedAt:null,err:null,spider:{x:.5,y:.5,tx:.5,ty:.5,target:null,q:[],last:{},pulses:[],legs:0},raf:0};
window.TEV_OPP=O;
/* ---------- pure helpers (tests/opp-feed.test.mjs) ---------- */
O.fmtEdge=it=>it.edge==null?"unmeasured":(it.edge>=0?"+":"")+(it.edge*100).toFixed(1)+"%";
O.fmtMove=it=>{const m=it.move_since_found;if(m==null)return"—";return it.move_unit==="pts"?(m>=0?"+":"")+(m*100).toFixed(1)+"¢":(m>=0?"+":"")+(m*100).toFixed(1)+"%"};
O.SRC={pm_arb:"ARB",news_latency:"NEWS·LAT",x_burst:"X BURST",alpha:"ALPHA",culture:"CULTURE",trench:"POOL",strategy:"STRATEGY"};
O.STRANDS=["news","x","culture","dexscreener","geckoterminal","pumpfun","kalshi","polymarket","rpc"];
O.LAB={news:"NEWS",x:"X",culture:"CULTURE",dexscreener:"DEX",geckoterminal:"GECKO",pumpfun:"PUMP.FUN",kalshi:"KALSHI",polymarket:"POLY",rpc:"RPC/APIs"};
O.health=(h,now)=>{if(!h)return"none";const t=ts(h.at),s=String(h.status??"").toLowerCase();
 const ok=s===""||s==="ok"||s==="healthy"||/^(2\d\d|304)$/.test(s);if(!ok)return"bad";return t&&now-t<15*60e3?"ok":"stale"};
O.newFetches=(prev,latest)=>{const out=[];for(const k of O.STRANDS){const h=latest[k];if(!h)continue;const t=ts(h.at);if(t&&prev[k]!=null&&t>prev[k])out.push(k)}return out};
const COL={ok:"#39ff9c",stale:"#ffd23f",bad:"#ff3b6b",none:"#2a3d4b"};

/* ---------- feed ---------- */
function mount(){const ch=byId("chain");if(!ch||byId("opp"))return!!byId("opp");
 ch.insertAdjacentHTML("afterbegin",`<div id="opp"><div class="opp-h"><b>OPPORTUNITY FEED</b><span id="opp-ct"></span><span id="opp-clk" class="dim"></span></div>
 <div class="opp-hon" id="opp-hon"></div><div class="opp-src" id="opp-src"></div><div class="opp-t"><table id="opp-tb"></table></div>
 <div class="opp-hist"><div class="opp-hh"><b>FOUND EARLIER · WHAT HAPPENED NEXT</b><span id="opp-ms" class="dim"></span></div><div id="opp-hl"></div></div></div>`);return true}
function render(){const d=O.d;if(!mount())return;const now=Date.now();
 byId("opp-clk").textContent=O.err?`feed error: ${O.err}`:d?`last real update ${ago(now-ts(d.newest_source_row_at))} ago · scanned ${ago(now-O.fetchedAt)} ago`:"connecting to /api/opportunities…";
 if(!d)return;const c=d.counts||{};
 byId("opp-ct").innerHTML=`<b>${c.live??0}</b> live · <b class="ok">${c.new??0}</b> new · ${c.history??0} expired (24h)`;
 byId("opp-hon").textContent=d.honesty||"";
 const S=d.sources||{};byId("opp-src").innerHTML=Object.entries(S).map(([k,s])=>`<span class="${s.status==="ok"?"on":"empty"}" title="${E(s.label+" · "+s.note)}">${E(O.SRC[k]||k)} <b>${s.n}</b>${s.status==="ok"?"":` <i>${E((s.note.split(" · ")[1])||"empty")}</i>`}</span>`).join("");
 const rows=(d.items||[]).slice(0,40).map(it=>`<tr class="${it.is_new?"nw":""}" title="${E(it.reason)}"><td><span class="lb ${E(it.label)}">${E(it.label)}</span>${it.is_new?'<span class="nwb">NEW</span>':""}</td><td class="dim">${E(O.SRC[it.source]||it.source)}</td><td class="tt">${it.url?`<a href="${E(it.url)}" target="_blank" rel="noopener">${E(it.title)}</a>`:E(it.title)}</td><td class="${it.edge>0?"ok":"dim"}">${O.fmtEdge(it)}</td><td class="why">${E(it.reason)}</td><td>${ago(it.age_s*1e3)}</td><td class="${(it.move_since_found||0)>0?"ok":(it.move_since_found||0)<0?"bad":"dim"}">${O.fmtMove(it)}</td></tr>`).join("");
 const h=`<tr><th></th><th>source</th><th>find</th><th>edge after fees</th><th>why</th><th>found</th><th>since found</th></tr>`+(rows||'<tr><td colspan=7 class="dim">every source is empty right now (see chips above) — nothing is shown rather than replaying old finds</td></tr>');
 const tb=byId("opp-tb");if(tb.dataset.h!==h){tb.dataset.h=h;tb.innerHTML=h}
 const m=d.measured||{};byId("opp-ms").textContent=m.n?`${m.n} finds ≥5 min old: ${m.up} up · ${m.down} down · ${m.note}`:`tracking since ${c.tracking_since?new Date(c.tracking_since).toLocaleTimeString([],{hour12:false}):"—"} · no find is 5 min old yet`;
 const hl=(d.history||[]).slice(0,12).map(it=>`<span title="${E(it.reason)}"><b>${E((it.title||"").slice(0,34))}</b> ${E(O.SRC[it.source]||"")} · lived ${ago((ts(it.expired_at)-ts(it.first_seen)))} · <em class="${(it.move_since_found||0)>0?"ok":(it.move_since_found||0)<0?"bad":"dim"}">${O.fmtMove(it)}</em></span>`).join("");
 const hh=hl||'<span class="dim">nothing has expired yet — finds move here when their source stops reporting them</span>';const he=byId("opp-hl");if(he.dataset.h!==hh){he.dataset.h=hh;he.innerHTML=hh}}
let busy=false;
async function poll(){if(busy||document.hidden)return;busy=true;try{const r=await fetch("/api/opportunities",{cache:"no-store"});if(r.ok){O.d=await r.json();O.fetchedAt=Date.now();O.err=null}else O.err="HTTP "+r.status}catch(e){O.err="offline"}finally{busy=false;render()}}

/* ---------- spider ---------- */
function latest(){const v=(window.TEV_V7||{}).d,H=window.TEV_CRAWLER;if(!v)return null;return H&&H.latestBy?H.latestBy(v.crawl||{}):((v.crawl||{}).latest||{})}
function spiderMount(){const rd=byId("ch-rd");if(!rd)return null;if(!byId("ch-spider")){rd.insertAdjacentHTML("afterend",'<canvas id="ch-spider"></canvas><div id="ch-spider-cap" class="dim"></div>');rd.style.display="none";const lg=byId("ch-leg");if(lg)lg.style.display="none"}return byId("ch-spider")}
function spiderTick(){const L=latest();if(!L)return;const sp=O.spider,now=Date.now();
 const fresh=O.newFetches(sp.last,L);for(const k of O.STRANDS){const t=L[k]&&ts(L[k].at);if(t)sp.last[k]=t}
 for(const k of fresh)if(!sp.q.includes(k))sp.q.push(k);
 const cap=byId("ch-spider-cap");if(cap){const tgt=sp.target&&L[sp.target];cap.textContent=tgt?`→ ${O.LAB[sp.target]} · ${String(tgt.source||"").slice(0,40)} · ${ago(now-ts(tgt.at))} ago`:`idle · newest fetch ${(()=>{const m=Math.max(...Object.values(sp.last));return isFinite(m)?ago(now-m)+" ago":"none yet"})()}`}}
function spiderDraw(){O.raf=0;const cv=spiderMount();if(!cv)return;const W=cv.clientWidth,Hh=cv.clientHeight,dpr=window.devicePixelRatio||1;
 if(!W||!Hh){O.raf=requestAnimationFrame(spiderDraw);return}
 if(cv.width!==Math.round(W*dpr)||cv.height!==Math.round(Hh*dpr)){cv.width=Math.round(W*dpr);cv.height=Math.round(Hh*dpr)}
 const x=cv.getContext("2d");x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,W,Hh);const cx=W/2,cy=Hh/2,R=Math.max(28,Math.min(W-40,Hh-40)/2),now=Date.now(),L=latest()||{},sp=O.spider,n=O.STRANDS.length;
 const ang=i=>-Math.PI/2+i/n*Math.PI*2,pt=(i,r)=>[cx+Math.cos(ang(i))*r,cy+Math.sin(ang(i))*r];
 /* web rings */
 x.lineWidth=.7;x.strokeStyle="rgba(150,200,230,.18)";for(let k=1;k<=5;k++){x.beginPath();for(let i=0;i<=n;i++){const[a,b]=pt(i%n,R*k/5);i?x.lineTo(a,b):x.moveTo(a,b)}x.stroke()}
 /* strands = real sources */
 x.font=(W<200?"8":"9")+"px ui-monospace,monospace";O.STRANDS.forEach((k,i)=>{const hs=O.health(L[k],now),[ex,ey]=pt(i,R);x.strokeStyle=COL[hs];x.lineWidth=sp.target===k?2.4:1.3;x.globalAlpha=hs==="none"?.5:1;
  x.beginPath();x.moveTo(cx,cy);x.lineTo(ex,ey);x.stroke();x.globalAlpha=1;x.fillStyle=COL[hs];x.beginPath();x.arc(ex,ey,3,0,7);x.fill();
  const lab=O.LAB[k],t=L[k]&&ts(L[k].at),sub=t?ago(now-t):"none",c=Math.cos(ang(i)),sn=Math.sin(ang(i));
  const tw=Math.max(x.measureText(lab).width,x.measureText(sub).width),clampX=v=>Math.max(2,Math.min(W-tw-2,v));
  const lx=clampX(c>.2?ex+5:c<-.2?ex-5-tw:ex-tw/2),ly=Math.max(9,Math.min(Hh-14,ey+sn*9+3));
  x.textAlign="left";x.fillStyle=hs==="none"?"#5f8aa3":"#cfe6f2";x.fillText(lab,lx,ly);x.fillStyle="#5f8aa3";x.fillText(sub,lx,ly+9)});
 x.textAlign="left";
 /* pulses travelling outward along a strand when that source really fetched */
 sp.pulses=sp.pulses.filter(p=>now-p.t<1800);for(const p of sp.pulses){const i=O.STRANDS.indexOf(p.k),f=(now-p.t)/1800,[a,b]=pt(i,R*f);x.fillStyle="rgba(57,255,156,"+(1-f)+")";x.beginPath();x.arc(a,b,3+4*(1-f),0,7);x.fill()}
 /* spider motion: walk to the queued strand tip, pause, return to the hub */
 if(!sp.target&&sp.q.length){sp.target=sp.q.shift();const i=O.STRANDS.indexOf(sp.target);const[a,b]=pt(i,R*.82);sp.tx=a/W;sp.ty=b/Hh;sp.arrive=null}
 if(sp.target){const d=Math.hypot(sp.tx*W-sp.x*W,sp.ty*Hh-sp.y*Hh);if(d<2){if(!sp.arrive){sp.arrive=now;sp.pulses.push({k:sp.target,t:now})}
   if(now-sp.arrive>1400){sp.target=null;sp.tx=.5;sp.ty=.5}}}
 const k=.06;const ox=sp.x,oy=sp.y;sp.x+=(sp.tx-sp.x)*k;sp.y+=(sp.ty-sp.y)*k;const moving=Math.hypot((sp.x-ox)*W,(sp.y-oy)*Hh)>.3;if(moving)sp.legs+=.35;
 const X=sp.x*W,Y=sp.y*Hh,dir=Math.atan2(sp.ty*Hh-Y,sp.tx*W-X);
 x.strokeStyle="rgba(39,231,255,.55)";x.lineWidth=.8;x.beginPath();x.moveTo(cx,cy);x.lineTo(X,Y);x.stroke(); /* silk thread */
 x.save();x.translate(X,Y);x.rotate(moving?dir+Math.PI/2:0);x.strokeStyle="#e8f6ff";x.lineWidth=1.4;
 for(let s=-1;s<=1;s+=2)for(let j=0;j<4;j++){const base=(-.9+j*.6),wig=Math.sin(sp.legs+j*1.7+(s>0?Math.PI:0))*.25;const a=base+wig;
  const k1=[s*10*Math.cos(a*.6),-8*Math.sin(a)],k2=[s*19*Math.cos(a*.5),-14*Math.sin(a)+5];x.beginPath();x.moveTo(0,(j-1.5)*2);x.lineTo(k1[0],k1[1]+(j-1.5)*3);x.lineTo(k2[0],k2[1]+(j-1.5)*4);x.stroke()}
 x.fillStyle="#0b1d2a";x.strokeStyle="#27e7ff";x.lineWidth=1.2;x.beginPath();x.ellipse(0,5,5.5,7,0,0,7);x.fill();x.stroke();x.beginPath();x.arc(0,-4,3.6,0,7);x.fill();x.stroke();
 x.fillStyle="#ff3b6b";x.beginPath();x.arc(-1.4,-5,.9,0,7);x.arc(1.4,-5,.9,0,7);x.fill();x.restore();
 O.raf=requestAnimationFrame(spiderDraw)}

function boot(){poll();setInterval(poll,4000);setInterval(()=>{try{render();spiderTick();if(!O.raf&&spiderMount())O.raf=requestAnimationFrame(spiderDraw)}catch(e){console.error(e)}},1000)}
if(typeof document!=="undefined"&&document.addEventListener){if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",()=>setTimeout(boot,80));else setTimeout(boot,80)}
})();
