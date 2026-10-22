/* TEV ROAMING CRAWLERS — the spider leaves the crawler bay and walks across the whole room.
 * Every trip is caused by a real event only:
 *   - a crawler source's newest fetch time advances (/api/room-v7 crawl.latest, same rule as the bay)
 *   - a new opportunity id appears in /api/opportunities
 * The spider walks to the panel that shows that data and drops a silk tag there ("NEWS fetched 4s ago").
 * When several events arrive together, small crawler sprites carry the extras in parallel.
 * No event = no trip: idle crawlers rest in the bay. Overlay is pointer-events:none, never blocks clicks.
 * prefers-reduced-motion: no walking; a static tag appears on the target instead. */
(()=>{"use strict";
const W_=typeof window!=="undefined"?window:{};
const R={main:null,minis:[],tags:[],q:[],seenOpp:null,last:{},raf:0,reduced:false,lastT:0};
W_.TEV_ROAM=R;
/* source -> panels that display it (first visible one wins) */
R.TARGETS={news:["wire"],x:["xw","xe7"],culture:["culture"],dexscreener:["lanes","alpha"],geckoterminal:["lanes","alpha"],
 pumpfun:["lanes","alpha"],kalshi:["pm"],polymarket:["pm"],rpc:["health","treas"],opp:["opp","chain"],home:["ch","chain"]};
R.LAB={news:"NEWS",x:"X",culture:"CULTURE",dexscreener:"DEX",geckoterminal:"GECKO",pumpfun:"PUMP.FUN",kalshi:"KALSHI",polymarket:"POLYMARKET",rpc:"RPC/APIs",opp:"NEW FIND"};
R.SOURCES=["news","x","culture","dexscreener","geckoterminal","pumpfun","kalshi","polymarket","rpc"];
const ts=t=>{const d=new Date(t);return isNaN(d)?null:d.getTime()};
/* ---------- pure helpers (tests/room-roam.test.mjs) ---------- */
R.newEvents=(prev,latest,now)=>{const out=[];for(const k of R.SOURCES){const h=latest&&latest[k];const t=h&&ts(h.at);if(t&&prev[k]!=null&&t>prev[k])out.push({kind:k,at:t,detail:String(h.source||"")})}return out};
R.newOpps=(seen,items)=>{if(!seen)return[];return(items||[]).filter(it=>it&&it.id&&!seen.has(it.id)).map(it=>({kind:"opp",at:Date.now(),detail:String(it.title||"").slice(0,40)}))};
/* landing point on a rect, clamped inside the viewport so off-screen panels still get a visible arrow */
R.land=(rect,vw,vh)=>{const x=rect.left+Math.min(70,rect.width/2),y=rect.top+12;const cx=Math.max(16,Math.min(vw-16,x)),cy=Math.max(16,Math.min(vh-16,y));
 return{x:cx,y:cy,off:cy!==y?(y<cy?"up":"down"):cx!==x?(x<cx?"left":"right"):null}};
R.step=(p,t,dt,speed)=>{const dx=t.x-p.x,dy=t.y-p.y,d=Math.hypot(dx,dy),m=speed*dt;if(d<=m||d<.5)return{x:t.x,y:t.y,done:true,moved:d};return{x:p.x+dx/d*m,y:p.y+dy/d*m,done:false,moved:m}};
/* ---------- DOM ---------- */
const doc=typeof document!=="undefined"?document:null;
function el(kind){for(const id of R.TARGETS[kind]||[]){const e=doc.getElementById(id);if(e&&e.getClientRects().length){const r=e.getBoundingClientRect();if(r.width>4&&r.height>4)return{e,r}}}return null}
function canvas(){let c=doc.getElementById("tev-roam");if(!c){c=doc.createElement("canvas");c.id="tev-roam";c.setAttribute("aria-hidden","true");
 c.style.cssText="position:fixed;inset:0;width:100vw;height:100vh;pointer-events:none;z-index:9000";doc.body.appendChild(c)}return c}
function home(){const h=el("home");if(!h)return{x:innerWidth/2,y:innerHeight/2};return{x:h.r.left+h.r.width/2,y:h.r.top+Math.min(h.r.height/2,120)}}
function crawler(size){const p=home();return{x:p.x,y:p.y,size,legs:0,ev:null,tgt:null,arrive:null,anchor:{x:p.x,y:p.y},dir:0,moving:false}}
function poll(){const v=(W_.TEV_V7||{}).d,H=W_.TEV_CRAWLER;const L=v?(H&&H.latestBy?H.latestBy(v.crawl||{}):((v.crawl||{}).latest||{})):null;const now=Date.now();
 if(L){const ev=R.newEvents(R.last,L,now);for(const k of R.SOURCES){const t=L[k]&&ts(L[k].at);if(t)R.last[k]=t}for(const e of ev)if(!R.q.some(x=>x.kind===e.kind))R.q.push(e)}
 const od=(W_.TEV_OPP||{}).d;if(od&&od.items){const ids=new Set(od.items.map(i=>i.id));const fresh=R.newOpps(R.seenOpp,od.items);R.seenOpp=ids;
  if(fresh.length&&!R.q.some(x=>x.kind==="opp"))R.q.push({...fresh[0],detail:fresh.length>1?`${fresh.length} new finds`:fresh[0].detail})}
 if(R.q.length>12)R.q.splice(0,R.q.length-12)}
function dispatch(c,ev){const t=el(ev.kind);if(!t)return false;c.ev=ev;c.tgt=t;c.arrive=null;return true}
function addTag(c,now){const r=c.tgt.r;R.tags.push({x:c.x,y:c.y,t:now,kind:c.ev.kind,text:`${R.LAB[c.ev.kind]||c.ev.kind} ${c.ev.kind==="opp"?"":"fetched "}${Math.max(0,Math.round((now-c.ev.at)/1e3))}s ago${c.ev.detail?" · "+c.ev.detail.slice(0,32):""}`,w:r.width});
 if(R.tags.length>8)R.tags.shift()}
function drawSpider(x,c,size,moving){const s=size;x.save();x.translate(c.x,c.y);x.rotate(moving?c.dir+Math.PI/2:0);x.scale(s,s);x.strokeStyle="#e8f6ff";x.lineWidth=1.4/s*Math.max(.8,s);
 for(let side=-1;side<=1;side+=2)for(let j=0;j<4;j++){const base=-.9+j*.6,wig=R.reduced?0:Math.sin(c.legs+j*1.7+(side>0?Math.PI:0))*.25,a=base+wig;
  x.beginPath();x.moveTo(0,(j-1.5)*2);x.lineTo(side*10*Math.cos(a*.6),-8*Math.sin(a)+(j-1.5)*3);x.lineTo(side*19*Math.cos(a*.5),-14*Math.sin(a)+5+(j-1.5)*4);x.stroke()}
 x.fillStyle="#0b1d2a";x.strokeStyle="#27e7ff";x.lineWidth=1.2;x.beginPath();x.ellipse(0,5,5.5,7,0,0,7);x.fill();x.stroke();x.beginPath();x.arc(0,-4,3.6,0,7);x.fill();x.stroke();
 x.fillStyle="#ff3b6b";x.beginPath();x.arc(-1.4,-5,.9,0,7);x.arc(1.4,-5,.9,0,7);x.fill();x.restore()}
function tick(t){R.raf=0;const cv=canvas(),vw=innerWidth,vh=innerHeight,dpr=devicePixelRatio||1;if(cv.width!==Math.round(vw*dpr)||cv.height!==Math.round(vh*dpr)){cv.width=Math.round(vw*dpr);cv.height=Math.round(vh*dpr)}
 const x=cv.getContext("2d");x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,vw,vh);const now=Date.now(),dt=Math.min(.05,R.lastT?(t-R.lastT)/1e3:.016);R.lastT=t;
 const sc=Math.max(1.4,Math.min(2.4,vw/1100));if(!R.main)R.main=crawler(sc);R.main.size=sc;
 const all=[R.main,...R.minis];
 /* hand out queued real events: main spider first, extras to mini crawlers (max 5) */
 while(R.q.length){let c=all.find(k=>!k.ev);if(!c){if(R.minis.length>=5)break;c=crawler(sc*.55);c.x=R.main.x;c.y=R.main.y;c.anchor={x:c.x,y:c.y};R.minis.push(c);all.push(c)}
  if(!dispatch(c,R.q.shift())&&c!==R.main){R.minis.splice(R.minis.indexOf(c),1);all.splice(all.indexOf(c),1)}}
 /* tags (silk webs left where data landed) */
 R.tags=R.tags.filter(g=>now-g.t<9000);x.font=(vw>2000?"12":"10.5")+"px ui-monospace,monospace";
 for(const g of R.tags){const f=1-(now-g.t)/9000;x.globalAlpha=Math.max(0,f);x.strokeStyle="#27e7ff";x.lineWidth=.7;
  for(let i=0;i<6;i++){const a=i/6*Math.PI*2;x.beginPath();x.moveTo(g.x,g.y);x.lineTo(g.x+Math.cos(a)*16,g.y+Math.sin(a)*16);x.stroke()}
  for(const rr of[6,11,16]){x.beginPath();x.arc(g.x,g.y,rr,0,7);x.stroke()}
  const tw=x.measureText(g.text).width+10,lx=Math.max(4,Math.min(vw-tw-4,g.x+20)),ly=Math.max(4,Math.min(vh-18,g.y-8));
  x.fillStyle="rgba(4,14,22,.88)";x.fillRect(lx,ly,tw,16);x.strokeRect(lx,ly,tw,16);x.fillStyle="#39ff9c";x.fillText(g.text,lx+5,ly+11.5)}
 x.globalAlpha=1;
 const speed=Math.max(380,Math.hypot(vw,vh)*.42);
 for(const c of all){if(c.ev&&c.tgt){c.tgt.r=c.tgt.e.getBoundingClientRect();const L=R.land(c.tgt.r,vw,vh);
   if(R.reduced){c.x=L.x;c.y=L.y}const s=R.step(c,L,dt,c===R.main?speed:speed*1.25);c.moving=!s.done;c.dir=Math.atan2(L.y-c.y,L.x-c.x);c.x=s.x;c.y=s.y;c.legs+=s.moved*.08;
   if(s.done){if(!c.arrive){c.arrive=now;addTag(c,now)}if(now-c.arrive>1600){c.anchor={x:c.x,y:c.y};c.ev=null;c.tgt=null;c.arrive=null}}
   if(L.off){x.fillStyle="#ffd23f";x.font="bold 11px ui-monospace,monospace";x.fillText({up:"▲",down:"▼",left:"◀",right:"▶"}[L.off]+" "+(R.LAB[c.ev?.kind]||""),Math.min(vw-90,L.x+16),L.y+4)}}
  else if(c===R.main){c.moving=false}
  /* dragline from where it last stood */
  x.strokeStyle="rgba(39,231,255,.45)";x.lineWidth=.8;x.beginPath();x.moveTo(c.anchor.x,c.anchor.y);x.lineTo(c.x,c.y);x.stroke();
  drawSpider(x,c,c.size,c.moving)}
 /* idle minis retire back into the main spider */
 R.minis=R.minis.filter(m=>m.ev||(Math.hypot(m.x-R.main.x,m.y-R.main.y)>4&&(Object.assign(m,R.step(m,R.main,dt,speed)),!m.done)));
 R.raf=requestAnimationFrame(tick)}
function boot(){if(!doc||!doc.body)return;const mq=W_.matchMedia&&W_.matchMedia("(prefers-reduced-motion: reduce)");R.reduced=!!(mq&&mq.matches);mq&&mq.addEventListener&&mq.addEventListener("change",e=>{R.reduced=e.matches});
 setInterval(()=>{try{poll()}catch(e){console.error(e)}},1000);if(!R.raf)R.raf=requestAnimationFrame(tick);
 doc.addEventListener("visibilitychange",()=>{if(!doc.hidden&&!R.raf){R.lastT=0;R.raf=requestAnimationFrame(tick)}})}
if(doc&&doc.addEventListener){if(doc.readyState==="loading")doc.addEventListener("DOMContentLoaded",()=>setTimeout(boot,200));else setTimeout(boot,200)}
})();
