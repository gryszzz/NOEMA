/* TEV room: X edge upgrade of the X wire panel (.xw). Reads /api/x-signal (read-only).
 * Real post cards (author, credibility badge, smart-money score, stance toward YES per matched market),
 * NOT-PRICED-IN / burst alerts and shill warnings on top, a narrative radar and smart-money board.
 * Works in place, so it follows the .xw node wherever the layout places it.
 * All X text is untrusted: rendered with textContent only. X is evidence, never a trade trigger. */
(function(){"use strict";
const SEEN=new Set();let first=true;
const PH={birth:"#27e7ff",accelerating:"#39ff9c",peak:"#ffd23f",decaying:"#5f8aa3"};
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=String(x);return e}
function sv(t,a){const e=document.createElementNS("http://www.w3.org/2000/svg",t);for(const k in a)e.setAttribute(k,a[k]);return e}
function ago(s){const m=Math.max(0,(Date.now()-Date.parse(s))/6e4);return m<60?Math.round(m)+"m":m<1440?Math.round(m/60)+"h":Math.round(m/1440)+"d"}
function kfmt(n){n=+n||0;return n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?(n/1e3).toFixed(1)+"k":String(n)}
function title(t){return String(t||"").split(" — ")[0]}
function build(xw){
 xw.classList.add("xe7-on");
 const r=el("div");r.id="xe7";
 r.append(el("div","al"));const bd=el("div","bd");bd.append(el("div","st"));
 const sd=el("div","sd");sd.append(el("h5",null,"NARRATIVE RADAR"));
 const s=sv("svg",{viewBox:"0 0 120 120",class:"rd"});sd.append(s);
 const lb=el("div","lb");sd.append(lb);bd.append(sd);r.append(bd);
 xw.insertBefore(r,document.getElementById("xw"));return r}
function alerts(box,d){box.textContent="";
 const np=(d.alerts||[]).filter(a=>a.status==="not_priced_in");
 const hot=(d.alerts||[]).filter(a=>a.status==="priced_in"&&a.z>=4).slice(0,2);
 const sh=(d.shill_warnings||[]).filter(w=>w.score>=0.5).slice(0,4);
 np.slice(0,3).forEach(a=>{const x=el("div","a");const b=el("b");b.append(el("i",null,"NOT PRICED IN"));x.append(b,
  el("span",null,`${a.entity} z${(+a.z).toFixed(1)} → ${title(a.title)} ${a.direction>0?"YES↑":"YES↓"} ${a.price_now!=null?Math.round(a.price_now*100)+"¢":""}`));box.append(x)});
 hot.forEach(a=>{const x=el("div","a");x.style.animation="none";const b=el("b");b.append(el("i",null,"BURST"));
  x.append(b,el("span",null,`${a.entity} z${(+a.z).toFixed(1)} · ${String(a.status).replace(/_/g," ")}`));box.append(x)});
 sh.forEach(w=>{const x=el("div","a sh");const b=el("b");b.append(el("i",null,"AVOID · SHILLED"));
  const n=w.kind==="contract"?w.entity.slice(0,16)+"…":w.entity;
  x.append(b,el("span",null,`${n} · ${(w.reasons||[]).map(s=>s.replace(/_/g," ")).join(", ")}`));box.append(x)});
 if(!box.children.length)box.append(el("div","none","No X burst ahead of its market · no active shill campaigns"))}
function card(p){const c=el("div","pc"+((p.pump_risk||0)>=0.5?" pump":""));
 const hd=el("div","hd");const a=el("a",null,"@"+p.author);a.href=p.url;a.target="_blank";a.rel="noopener noreferrer";
 const cr=+p.credibility||0;hd.append(a,el("span","bg "+(cr>=0.6?"c-hi":cr>=0.25?"c-md":"c-lo"),(p.author_class!=="unknown"?p.author_class+" ":"")+"cred "+cr.toFixed(2)));
 const sm=p.smart_money||{};
 if(sm.status==="promoted")hd.append(el("span","bg sm-p",`★ SMART ${sm.hits}/${sm.n} · ${(+sm.shrunk).toFixed(2)}`));
 else if(sm.status==="demoted")hd.append(el("span","bg sm-d","✕ DEMOTED"));
 else if(sm.n)hd.append(el("span","bg c-lo",`calls ${sm.hits}/${sm.n}`));
 hd.append(el("span",null,kfmt(p.followers)+" · "+ago(p.created_at)));
 c.append(hd,el("div","tx",p.text));
 const sk=el("div","sk");(p.stances||[]).forEach(s=>{const row=el("div");
  row.append(el("span",null,`${title(s.title)} · ${s.stance>0.15?"YES↑":s.stance<-0.15?"YES↓":"neutral"} ${Math.round((s.confidence||0)*100)}%${s.source==="lexicon"?" (lex)":""}`));
  const b=el("div","bar");const i=el("i",s.stance<0?"n":null);const w=Math.min(50,Math.abs(s.stance)*50*(0.5+0.5*(s.confidence||0)));
  i.style.width=w+"%";if(s.stance<0)i.style.left=(50-w)+"%";b.append(i);row.append(b);sk.append(row)});
 if(!(p.stances||[]).length)sk.append(el("div","bg c-lo","no matched market"));
 (p.flags||[]).filter(f=>/pump|shill|coordinated|swarm/.test(f)).slice(0,2).forEach(f=>sk.append(el("div","bg sm-d",f.replace(/_/g," "))));
 c.append(sk);return c}
function stream(box,d){const ps=d.posts||[];box.textContent="";
 if(!ps.length){box.append(el("div","none","Waiting for X drops…"));return}
 ps.forEach(p=>{const c=card(p);if(!SEEN.has(p.id)&&!first)c.classList.add("new");SEEN.add(p.id);box.append(c)});first=false}
function radar(s,d){s.textContent="";[18,36,54].forEach(r=>s.append(sv("circle",{cx:60,cy:60,r,fill:"none",stroke:"#12324a"})));
 s.append(sv("line",{x1:60,y1:60,x2:60,y2:4,stroke:"#b67bff","stroke-opacity":".5",class:"sw"}));
 const ring={birth:52,accelerating:38,peak:24,decaying:10};const ns=(d.narratives||[]).slice(0,9);
 ns.forEach((n,i)=>{const t=i/Math.max(1,ns.length)*6.283-1.57,rr=ring[n.phase]||30,x=60+Math.cos(t)*rr,y=60+Math.sin(t)*rr;
  const g=sv("g",{class:"bl"});g.append(sv("circle",{cx:x,cy:y,r:Math.min(6,2+(+n.rate_now||0)*.6),fill:PH[n.phase]||"#bfe9ff"}));
  const tx=sv("text",{x:x+4,y:y+2,fill:"#bfe9ff","font-size":"5.5"});tx.textContent=String(n.label||"").split(" / ").slice(0,2).join(" ");g.append(tx);
  const tt=sv("title",{});tt.textContent=`${n.label} — ${n.phase}${n.sector?" · "+n.sector:""} · ${n.posts} posts / ${n.authors} authors`;g.append(tt);s.append(g)})}
function board(lb,d){lb.textContent="";lb.append(el("h5",null,"SMART MONEY"));
 const L=(d.leaderboard||[]).slice(0,5);if(!L.length)lb.append(el("div",null,"scoring calls (1h / 24h)…"));
 L.forEach(a=>{const r=el("div");r.append(el("span",a.status==="promoted"?"sm-p":"",(a.status==="promoted"?"★ @":"@")+a.author),el("span",null,`${a.hits}/${a.n} ${(+a.shrunk).toFixed(2)}`));lb.append(r)});
 const rot=(d.rotation||[]).filter(x=>x.rotation>0.02).slice(0,2);
 if(rot.length){lb.append(el("h5",null,"ROTATING IN"));rot.forEach(x=>{const r=el("div");r.append(el("span","c-hi",x.sector.replace(/_/g," ")),el("span",null,"+"+(x.rotation*100).toFixed(1)+"pp"));lb.append(r)})}
 const ag=(d.agents||[]).reduce((s,a)=>s+(a.forecasts||0),0);lb.append(el("div","c-lo",`ledger: ${ag} X forecasts · evidence only`))}
let root=null;
async function tick(){const xw=document.querySelector(".xw");if(!xw)return;if(!root||!xw.contains(root))root=build(xw);
 try{const d=await (await fetch("/api/x-signal",{credentials:"same-origin"})).json();
  alerts(root.querySelector(".al"),d);stream(root.querySelector(".st"),d);radar(root.querySelector(".rd"),d);board(root.querySelector(".lb"),d)}catch(e){/* keep last frame */}}
function boot(){tick();setInterval(tick,20000)}
document.readyState==="loading"?document.addEventListener("DOMContentLoaded",boot):boot();
window.TEV_V7_X={tick};
})();
