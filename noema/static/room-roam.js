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
/* ---------- articulated legs: alternating tetrapod gait + 2-bone IK + tarsus ---------- */
/* legs j=0..3 front->back, side -1 left / +1 right. Body frame: forward = -y. Units are pre-scale. */
R.HIPS=[-4.6,-3.2,-1.8,-.4];R.REST=[[16,-21],[22,-8],[22,5],[17,18]];R.L1=R.REST.map((f,j)=>Math.hypot(f[0]-3,f[1]-R.HIPS[j])*.54);R.L2=R.L1.map(l=>l*1.02);
R.group=(j,side)=>(j+(side>0?1:0))%2;
/* gait phase -> fore/aft foot offset (+ = forward) and lift 0..1. Stance [0,.5) plants & slides back; swing [.5,1) lifts & reaches forward */
R.legPhase=(gait,j,side)=>{const p=(((gait+R.group(j,side)*.5)%1)+1)%1;if(p<.5)return{off:1-4*p,lift:0,swing:false};const q=(p-.5)*2;return{off:-1+2*(q*q*(3-2*q)),lift:Math.sin(q*Math.PI),swing:true}};
R.ik=(hx,hy,fx,fy,l1,l2,ox,fa)=>{let dx=fx-hx,dy=fy-hy,d=Math.hypot(dx,dy)||1e-6;const m=l1+l2-.01;if(d>m){fx=hx+dx/d*m;fy=hy+dy/d*m;dx=fx-hx;dy=fy-hy;d=m}
 const a=Math.acos(Math.max(-1,Math.min(1,(l1*l1+d*d-l2*l2)/(2*l1*d)))),b=Math.atan2(dy,dx);
 const k1={x:hx+Math.cos(b+a)*l1,y:hy+Math.sin(b+a)*l1},k2={x:hx+Math.cos(b-a)*l1,y:hy+Math.sin(b-a)*l1};
 /* knee bulges outward, and forward for front legs / backward for rear legs, so legs never cross */
 const sc=k=>k.x*ox+(fa||0)*-k.y*1;const k=sc(k1)>sc(k2)?k1:k2;return{knee:k,foot:{x:fx,y:fy}}};
R.legPose=(c,j,side,t)=>{const st=c.stride||0,ph=R.legPhase(c.gait||0,j,side),S=3.4*st,lift=ph.lift*st;
 let tw=0;if(!R.reduced&&st<.2){const n=Math.sin(t*.0021*(1+j*.37)+j*2.1+side)*Math.sin(t*.0013+j*1.3*side);tw=n>.82?(n-.82)*9:0}
 const hx=side*3,hy=R.HIPS[j],fx=side*(R.REST[j][0]-lift*1+tw*.6),fy=R.REST[j][1]-ph.off*S-tw*1.4;
 const r=R.ik(hx,hy,fx,fy,R.L1[j],R.L2[j],side,j<2?1:-1);
 /* tarsus: last ~30% of the tibia, kinked downward/outward so each leg reads as 3 segments */
 const kx=r.knee.x,ky=r.knee.y,ax=kx+(r.foot.x-kx)*.68+side*(1.3+lift),ay=ky+(r.foot.y-ky)*.68+(j<2?-1:1)*.6;
 return{hip:{x:hx,y:hy},knee:r.knee,ankle:{x:ax,y:ay},foot:r.foot,lift}};
let GLOW=null;function glow(){if(GLOW||!doc)return GLOW;const g=doc.createElement("canvas");g.width=g.height=32;const x=g.getContext("2d"),rg=x.createRadialGradient(16,16,0,16,16,16);
 rg.addColorStop(0,"rgba(220,255,255,1)");rg.addColorStop(.18,"rgba(39,231,255,.95)");rg.addColorStop(.5,"rgba(39,231,255,.28)");rg.addColorStop(1,"rgba(39,231,255,0)");x.fillStyle=rg;x.fillRect(0,0,32,32);return GLOW=g}
function node(x,g,p,r,a){x.globalAlpha=a;x.drawImage(g,p.x-r,p.y-r,r*2,r*2)}
function drawSpider(x,c,size,moving,t){const s=size,mini=c!==R.main,g=glow();
 if(c.heading==null)c.heading=0;if(moving){let d=c.dir+Math.PI/2-c.heading;d=Math.atan2(Math.sin(d),Math.cos(d));c.heading+=d*.25}
 const bob=R.reduced?0:Math.sin((c.gait||0)*Math.PI*4)*.55*(c.stride||0),sway=R.reduced?0:Math.sin((c.gait||0)*Math.PI*2)*.35*(c.stride||0);
 x.save();x.translate(c.x,c.y);x.rotate(c.heading);x.scale(s,s);x.lineCap="round";x.lineJoin="round";
 const poses=[];for(let side=-1;side<=1;side+=2)for(let j=0;j<4;j++)poses.push(R.legPose(c,j,side,t));
 x.save();x.translate(sway,bob*.4);
 for(const P of poses){x.strokeStyle=P.lift>.05?"#bff6ff":"#e8f6ff";
  x.lineWidth=(mini?1.3:1.5)/s+.3;x.beginPath();x.moveTo(P.hip.x,P.hip.y);x.lineTo(P.knee.x,P.knee.y);x.stroke();
  x.lineWidth=(mini?1:1.1)/s+.25;x.beginPath();x.moveTo(P.knee.x,P.knee.y);if(!mini)x.lineTo(P.ankle.x,P.ankle.y);x.lineTo(P.foot.x,P.foot.y);x.stroke()}
 if(g){const prev=x.globalCompositeOperation;x.globalCompositeOperation="lighter";
  for(const P of poses){if(!mini){node(x,g,P.knee,2.6,.95);node(x,g,P.ankle,1.9,.8);node(x,g,P.hip,1.6,.6)}node(x,g,P.foot,mini?2.2:2.4+P.lift*1.4,P.lift>.05?1:.85)}
  x.globalCompositeOperation=prev;x.globalAlpha=1}
 x.restore();
 x.translate(sway*.6,bob);const br=1+bob*.03;x.scale(br,br);
 x.fillStyle="#0b1d2a";x.strokeStyle="#27e7ff";x.lineWidth=1.2;x.beginPath();x.ellipse(0,7,5.8,7.6,0,0,7);x.fill();x.stroke();
 x.beginPath();x.ellipse(0,-2.5,3.9,4.4,0,0,7);x.fill();x.stroke();
 if(!mini){x.strokeStyle="rgba(39,231,255,.55)";x.lineWidth=.6;x.beginPath();x.moveTo(0,2);x.lineTo(0,12);x.moveTo(-2.6,6);x.lineTo(2.6,6);x.stroke()}
 if(g){x.globalCompositeOperation="lighter";node(x,g,{x:0,y:7},mini?4:6,.35);x.globalCompositeOperation="source-over";x.globalAlpha=1}
 x.fillStyle="#ff3b6b";x.beginPath();x.arc(-1.4,-4.6,.9,0,7);x.arc(1.4,-4.6,.9,0,7);x.fill();x.restore()}
R.draw=drawSpider;
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
   if(R.reduced){c.x=L.x;c.y=L.y}const s=R.step(c,L,dt,c===R.main?speed:speed*1.25);c.moving=!s.done;c.dir=Math.atan2(L.y-c.y,L.x-c.x);c.x=s.x;c.y=s.y;if(!R.reduced)c.gait=(c.gait||0)+s.moved/(c.size*19);
   if(s.done){if(!c.arrive){c.arrive=now;addTag(c,now)}if(now-c.arrive>1600){c.anchor={x:c.x,y:c.y};c.ev=null;c.tgt=null;c.arrive=null}}
   if(L.off){x.fillStyle="#ffd23f";x.font="bold 11px ui-monospace,monospace";x.fillText({up:"▲",down:"▼",left:"◀",right:"▶"}[L.off]+" "+(R.LAB[c.ev?.kind]||""),Math.min(vw-90,L.x+16),L.y+4)}}
  else if(c===R.main){c.moving=false}
  /* dragline from where it last stood */
  x.strokeStyle="rgba(39,231,255,.45)";x.lineWidth=.8;x.beginPath();x.moveTo(c.anchor.x,c.anchor.y);x.lineTo(c.x,c.y);x.stroke();
  c.stride=R.reduced?0:(c.stride||0)+((c.moving?1:0)-(c.stride||0))*Math.min(1,dt*7);
  drawSpider(x,c,c.size,c.moving,t)}
 /* idle minis retire back into the main spider */
 R.minis=R.minis.filter(m=>m.ev||(Math.hypot(m.x-R.main.x,m.y-R.main.y)>4&&(m.moving=true,m.dir=Math.atan2(R.main.y-m.y,R.main.x-m.x),Object.assign(m,R.step(m,R.main,dt,speed)),m.gait=(m.gait||0)+m.moved/(m.size*19),!m.done)));
 R.raf=requestAnimationFrame(tick)}
function boot(){if(!doc||!doc.body)return;const mq=W_.matchMedia&&W_.matchMedia("(prefers-reduced-motion: reduce)");R.reduced=!!(mq&&mq.matches);mq&&mq.addEventListener&&mq.addEventListener("change",e=>{R.reduced=e.matches});
 setInterval(()=>{try{poll()}catch(e){console.error(e)}},1000);if(!R.raf)R.raf=requestAnimationFrame(tick);
 doc.addEventListener("visibilitychange",()=>{if(!doc.hidden&&!R.raf){R.lastT=0;R.raf=requestAnimationFrame(tick)}})}
if(doc&&doc.addEventListener){if(doc.readyState==="loading")doc.addEventListener("DOMContentLoaded",()=>setTimeout(boot,200));else setTimeout(boot,200)}
})();
