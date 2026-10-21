/* TEV CRAWLER hero bay. Makes the crawler the centre of the room.
 * Every row, blip and counter comes from /api/room-v7 (crawl + funnel), which room-v7.js
 * already polls into window.TEV_V7.d. Nothing is invented: when there are no new fetches
 * the bay says it is idle, what it is waiting for, and when the next sweep is due. */
(()=>{"use strict";
const byId=id=>document.getElementById(id),q=s=>document.querySelector(s);
const E=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const KINDS={news:["NEWS","#27e7ff"],x:["X","#b06bff"],culture:["CULTURE","#ffd23f"],pumpfun:["PUMP.FUN","#ff8a3d"],
 prediction:["MARKETS","#39ff9c"],pool:["DEX POOL","#ff4fd8"],rpc:["DATA API","#5f8aa3"]};
const H={seen:new Set(),blips:[],booted:false,lastCycle:null,raf:0};
const CH=window.TEV_CRAWLER=H;
const ts=t=>{const d=new Date(t);return isNaN(d)?null:d.getTime()};
const ago=ms=>{const s=Math.max(0,Math.round(ms/1e3));return s<60?s+"s":s<3600?Math.floor(s/60)+"m "+(s%60)+"s":Math.floor(s/3600)+"h "+Math.floor(s%3600/60)+"m"};
/* pure helpers (tested in tests/crawler-hero.test.mjs) */
H.kindOf=h=>{const s=(String(h.source||"")+" "+String(h.url||"")).toLowerCase();
 if(h.kind==="prediction"&&/kalshi/.test(s))return"kalshi";if(h.kind==="prediction"&&/polymarket/.test(s))return"polymarket";
 if(h.kind==="pool"&&/dexscreener/.test(s))return"dexscreener";if(h.kind==="pool"&&/gecko/.test(s))return"geckoterminal";return h.kind};
H.state=(d,now)=>{const c=(d&&d.crawl)||{},f=(d&&d.funnel)||{},hits=c.hits||[];
 const last=hits.length?ts(hits[0].at):null,lastSweep=ts(f.ts),nx=c.next||{};
 const due=Object.entries(nx).map(([k,v])=>[k,ts(v)]).filter(x=>x[1]).sort((a,b)=>a[1]-b[1]);
 const active=last!=null&&now-last<90e3;
 const upcoming=due.find(x=>x[1]>now)||null,overdue=due.filter(x=>x[1]<=now).map(x=>x[0]);
 return {active,last,lastSweep,upcoming,overdue,hits:hits.length}};
H.idleText=(st,now)=>{if(st.active)return null;const parts=[];
 if(st.last)parts.push(`last real fetch ${ago(now-st.last)} ago`);else parts.push("no crawler fetch recorded yet");
 if(st.overdue.length)parts.push(`waiting on ${st.overdue.join(" + ")} (due now)`);
 if(st.upcoming)parts.push(`next ${st.upcoming[0]} sweep in ${ago(st.upcoming[1]-now)}`);
 return "IDLE · "+parts.join(" · ")};
function scaffold(){const top=q(".center-top");if(!top||byId("ch"))return!!byId("ch");
 const h2=top.querySelector("h2");if(h2)h2.innerHTML=`<span class="dot on" id="ch-dot"></span>CRAWLER<span class="tag" id="bay-tag"></span>`;
 const old=top.querySelector(".bay");if(old){old.classList.add("ch-retired");const bt=old.querySelector("#bay-tag");if(bt)bt.remove()}
 const html=`<div id="ch"><div class="ch-hud"><div class="ch-title"><b>CRAWLER</b><span id="ch-state" class="idle">connecting…</span></div><div class="ch-ctr" id="ch-ctr"></div></div>
 <div class="ch-main"><div class="ch-radar"><canvas id="ch-rd"></canvas><div class="ch-legend" id="ch-leg"></div></div><div class="ch-stage" id="ch-stage"></div>
 <div class="ch-feed"><div class="ch-fh"><b>LIVE CRAWL FEED</b><span id="ch-fh"></span></div><div class="ch-rows" id="ch-rows"></div></div></div>
 <div class="ch-srcs" id="ch-srcs"></div></div>`;
 (h2||top.firstChild).insertAdjacentHTML(h2?"afterend":"beforebegin",html);return true}
function wires(){const app=q(".app");if(!app)return;let w=byId("ch-wires");if(!w){app.insertAdjacentHTML("beforeend",'<div id="ch-wires"></div>');w=byId("ch-wires")}
 /* one page everywhere: news, X wire, strategies and health always live in #ch-wires (no tabs) */
 for(const s of [".wire",".xw"]){const el=q(s);if(el&&el.parentElement!==w)w.appendChild(el)}
 const sp=byId("strat-pnl");if(sp&&sp.parentElement!==w)w.appendChild(sp);
 const hp=byId("health")&&byId("health").closest(".pnl");if(hp&&hp.parentElement!==w)w.appendChild(hp)}
function adoptScan(){const s=byId("v7-scan"),st=byId("ch-stage");if(s&&st&&s.parentElement!==st)st.appendChild(s)}
function counters(d,now){const c=(d.crawl||{}).counters||{},f=d.funnel||{},lt=f.latest||{},st=H.state(d,now);
 const cell=(k,v,t)=>`<div title="${E(t)}"><span>${k}</span><b>${E(v??"—")}</b></div>`;
 byId("ch-ctr").innerHTML=cell("SWEEP",f.cycle_id!=null?"#"+f.cycle_id:"—","latest trench sweep id")+cell("POOLS NOW",lt.crawled,"pools crawled in the latest sweep")
  +cell("PUMP.FUN NEW",f.pumpfun_new,"new pump.fun launches seen in the latest sweep")+cell("SWEEPS 24H",c.sweeps_24h,"trench sweeps in the last 24h")
  +cell("POOLS 24H",c.pools_24h!=null?Number(c.pools_24h).toLocaleString():null,"pools crawled in the last 24h")+cell("FETCHES 1H",c.fetches_1h,"recorded crawler GETs in the last hour")
  +cell("SOURCES",c.sources,"distinct sources with a recorded fetch")
  +cell("NEXT SWEEP",st.upcoming?ago(st.upcoming[1]-now):(st.overdue.length?"due now":"—"),st.upcoming?`next: ${st.upcoming[0]}`:"from news schedule / sweep interval / X cadence");
 const el=byId("ch-state"),idle=H.idleText(st,now);
 const txt=idle||`SWEEPING · ${st.hits} recent fetches · last ${ago(now-st.last)} ago`;
 if(el.textContent!==txt)el.textContent=txt;el.className=idle?"idle":"live";byId("ch-dot").className="dot "+(idle?"off":"on")}
function feed(d,now){const hits=((d.crawl||{}).hits)||[],rows=byId("ch-rows");if(!rows)return;
 const fresh=[];for(const h of hits.slice().reverse()){const k=h.at+"|"+h.url+"|"+h.status;if(H.seen.has(k))continue;H.seen.add(k);fresh.push(h)}
 for(const h of fresh){const kd=H.kindOf(h),[lab,col]=KINDS[h.kind]||[String(h.kind).toUpperCase(),"#5f8aa3"];
  const ok=/^(200|304|ok|healthy)$/i.test(String(h.status||""))||h.kind==="pool";
  rows.insertAdjacentHTML("afterbegin",`<div class="ch-r${H.booted?" new":""}" data-k="${E(kd)}" data-at="${E(h.at)}"><time></time><i style="--c:${col}">${E(lab)}</i><span class="src">${E(h.source)}</span><span class="u" title="${E(h.url)}">GET ${E(h.url)}</span><em class="${ok?"ok":"bad"}">${E(h.status??"")}${h.items!=null?` · ${E(h.items)} items`:""}${h.detail?` · ${E(h.detail)}`:""}</em></div>`);
  H.blips.push({kind:h.kind,at:ts(h.at),born:now,seed:Math.random()})}
 while(rows.children.length>80)rows.lastElementChild.remove();
 rows.querySelectorAll(".ch-r").forEach(r=>{const t=ts(r.dataset.at);r.firstElementChild.textContent=t?ago(now-t):""});
 if(H.blips.length>200)H.blips.splice(0,H.blips.length-200);
 byId("ch-fh").textContent=fresh.length&&H.booted?`+${fresh.length} new`:`${hits.length} recorded fetches · newest first`}
/* source light for a fetch: venue/provider name wins over the kind (DexScreener boosts are "culture" fetches) */
H.lightOf=h=>{const s=(String(h.source||"")+" "+String(h.url||"")).toLowerCase();if(h.kind==="x")return"x";
 if(h.kind==="pumpfun"||/pump\.?fun/.test(s))return"pumpfun";if(/dexscreener/.test(s))return"dexscreener";if(/geckoterminal/.test(s))return"geckoterminal";return H.kindOf(h)};
H.latestBy=c=>{const by=Object.assign({},(c&&c.latest)||{});for(const h of (c&&c.hits)||[]){const k=H.lightOf(h);if(!by[k]||ts(h.at)>ts(by[k].at))by[k]=h}return by};
function sources(d,now){const by=H.latestBy(d.crawl||{});
 const order=["news","x","culture","dexscreener","geckoterminal","pumpfun","kalshi","polymarket","rpc"];
 const lab={news:"NEWS",x:"X",culture:"CULTURE",dexscreener:"DEXSCREENER",geckoterminal:"GECKOTERMINAL",pumpfun:"PUMP.FUN",kalshi:"KALSHI",polymarket:"POLYMARKET",rpc:"DATA APIs"};
 byId("ch-srcs").innerHTML=order.map(k=>{const h=by[k],t=h&&ts(h.at),fresh=t&&now-t<10*60e3;
  return `<div class="${h?(fresh?"on":"stale"):"none"}" title="${E(h?h.url:"no recorded fetch")}"><i></i><b>${lab[k]}</b><span>${h?ago(now-t)+" ago":"no fetch yet"}</span></div>`}).join("")}
function legend(){byId("ch-leg").innerHTML=Object.values(KINDS).map(([l,c])=>`<span style="--c:${c}">${l}</span>`).join("")}
/* radar: one blip per real fetch, angle by source kind, distance by age (centre = now) */
const ANG={news:0,x:1,culture:2,pumpfun:3,prediction:4,pool:5,rpc:6};
function radar(){H.raf=0;const cv=byId("ch-rd");if(!cv)return;const W=cv.clientWidth,Hh=cv.clientHeight,dpr=window.devicePixelRatio||1;if(!W||!Hh){H.raf=requestAnimationFrame(radar);return}
 if(cv.width!==Math.round(W*dpr)||cv.height!==Math.round(Hh*dpr)){cv.width=Math.round(W*dpr);cv.height=Math.round(Hh*dpr)}
 const x=cv.getContext("2d");x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,W,Hh);const cx=W/2,cy=Hh/2,R=Math.min(W,Hh)/2-6,now=Date.now();
 x.strokeStyle="rgba(39,231,255,.25)";x.lineWidth=1;for(let i=1;i<=4;i++){x.beginPath();x.arc(cx,cy,R*i/4,0,7);x.stroke()}
 x.beginPath();x.moveTo(cx-R,cy);x.lineTo(cx+R,cy);x.moveTo(cx,cy-R);x.lineTo(cx,cy+R);x.stroke();
 const sw=(now/2600)%1*Math.PI*2,g=x.createConicGradient?x.createConicGradient(sw-0.9,cx,cy):null;
 if(g){g.addColorStop(0,"rgba(57,255,156,0)");g.addColorStop(.14,"rgba(57,255,156,.35)");g.addColorStop(.15,"rgba(57,255,156,0)");x.fillStyle=g;x.beginPath();x.arc(cx,cy,R,0,7);x.fill()}
 x.strokeStyle="rgba(57,255,156,.9)";x.beginPath();x.moveTo(cx,cy);x.lineTo(cx+Math.cos(sw)*R,cy+Math.sin(sw)*R);x.stroke();
 const span=30*60e3;for(const b of H.blips){if(!b.at)continue;const age=now-b.at;if(age>span)continue;const a=(ANG[b.kind]??6)/7*Math.PI*2+b.seed*0.8,r=R*Math.min(1,age/span);
  const px=cx+Math.cos(a)*r,py=cy+Math.sin(a)*r,col=(KINDS[b.kind]||["","#5f8aa3"])[1];
  let d=((sw-Math.atan2(py-cy,px-cx))%(Math.PI*2)+Math.PI*2)%(Math.PI*2);const lit=Math.max(.3,1-d/(Math.PI*2));
  const pop=Math.max(0,1-(now-b.born)/1500);x.globalAlpha=lit;x.fillStyle=col;x.beginPath();x.arc(px,py,2.2+pop*6,0,7);x.fill();x.globalAlpha=1}
 x.fillStyle="rgba(95,138,163,.9)";x.font="9px ui-monospace,monospace";x.fillText("now",cx+3,cy-3);x.fillText("30m",cx+R-22,cy-3);
 H.raf=requestAnimationFrame(radar)}
function tick(){const d=(window.TEV_V7||{}).d;if(!scaffold())return;adoptScan();wires();if(!d)return;const now=Date.now();
 if(!H.legend){legend();H.legend=1}counters(d,now);feed(d,now);sources(d,now);H.booted=true;if(!H.raf)H.raf=requestAnimationFrame(radar)}
function boot(){tick();setInterval(()=>{try{tick()}catch(e){console.error(e)}},1000)}
if(typeof document!=="undefined"&&document.addEventListener){if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",()=>setTimeout(boot,50));else setTimeout(boot,50)}
})();
