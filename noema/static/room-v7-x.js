/* TEV room: X WIRE v3 (.xw). Reads /api/x-signal (read-only, real data only).
 * Order: freshness line -> ACTIONABLE (not-priced-in / bursts with stance, market, price + move since the post)
 * -> smart money (ledger score) -> narratives with collapsed, de-duplicated one-line posts. Shill-shield flags shown.
 * All X text is untrusted: rendered with textContent only. X is evidence, never a trade trigger. */
(function(){"use strict";
const SEEN=new Set();let first=true;const PULL_MIN=30;
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=String(x);return e}
function mins(s){return Math.max(0,(Date.now()-Date.parse(s))/6e4)}
function ago(s){if(!s)return"—";const m=mins(s);return m<60?Math.round(m)+"m":m<1440?Math.round(m/60)+"h":Math.round(m/1440)+"d"}
function kfmt(n){n=+n||0;return n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?(n/1e3).toFixed(1)+"k":String(Math.round(n))}
function title(t){return String(t||"").split(" — ")[0]}
function px(v,venue){if(v==null||!isFinite(v))return"";return venue==="token"?(v>=1000?"$"+Math.round(v).toLocaleString():"$"+(+v).toPrecision(4)):Math.round(v*100)+"¢"}
function chip(st,tok){const c=st>0.15?"up":st<-0.15?"dn":"nu";return el("span","chip "+c,st>0.15?(tok?"BULL":"YES↑"):st<-0.15?(tok?"BEAR":"YES↓"):"NEUTRAL")}
const T={};
T.norm=t=>String(t||"").replace(/^RT @\w+:\s*/i,"").replace(/https?:\/\/\S+/g,"").replace(/[^\p{L}\p{N}$#@ ]/gu," ").replace(/\s+/g," ").trim().toLowerCase().slice(0,100);
T.dedupe=ps=>{const m=new Map();for(const p of ps){const k=T.norm(p.text);const h=m.get(k);if(h){h.dups+=1;h.authors.add(p.author)}else m.set(k,{p,dups:0,authors:new Set([p.author])})}return[...m.values()]};
T.velocity=p=>{const e=p.engagement||{},n=(+e.likes||0)+2*(+e.reposts||0)+(+e.replies||0)+2*(+e.quotes||0);const h=Math.max(.25,mins(p.created_at)/60);return n?n/h:(p.velocity!=null?+p.velocity:null)};
T.groupOf=(p,N)=>{const tx=String(p.text||"").toLowerCase();for(const n of N){const toks=(Array.isArray(n.tokens)?n.tokens:[]).map(x=>String(x).toLowerCase()),words=String(n.label||"").toLowerCase().split(" / ").filter(w=>w.length>2);
 if(toks.some(t=>t&&tx.includes(t))||words.filter(w=>tx.includes(w)).length>=2)return n.label}const s=(p.stances||[])[0];return s?title(s.title):"other"};
T.fresh=(asOf,now)=>{if(!asOf)return"X pull: no run recorded yet";const m=Math.round(((now||Date.now())-Date.parse(asOf))/6e4);const nx=PULL_MIN-(m%PULL_MIN);return`X pull ${m}m ago · next ~${nx}m`};
function build(xw){xw.classList.add("xe7-on");const r=el("div");r.id="xe7";
 r.append(el("div","fr"),el("div","al"),el("div","lb"),el("div","st"));xw.insertBefore(r,document.getElementById("xw"));return r}
function fresh(box,d){box.textContent="";box.append(el("b",null,T.fresh(d.as_of)),el("span",null,` · ${(d.posts||[]).length} posts · ${(d.narratives||[]).length} narratives · evidence only`))}
function alerts(box,d){box.textContent="";box.append(el("h5",null,"ACTIONABLE"));
 const np=(d.alerts||[]).filter(a=>a.status==="not_priced_in").slice(0,3),hot=(d.alerts||[]).filter(a=>a.status!=="not_priced_in"&&a.z>=4).slice(0,2);
 for(const a of np.concat(hot)){const x=el("div","row a"+(a.status==="not_priced_in"?" np":""));const tok=a.venue==="token";
  x.append(el("i","tag",a.status==="not_priced_in"?"NOT PRICED IN":"BURST"),chip(a.direction,tok),el("b",null,a.entity),
   el("span",null,`z${(+a.z).toFixed(1)} · ${ago(a.at)}`),el("span","mk","→ "+title(a.title)+" "+px(a.price_now,a.venue)),
   el("span",a.price_move>0?"up":a.price_move<0?"dn":"",a.price_move!=null?`${a.price_move>0?"+":""}${(a.price_move*100).toFixed(2)}% since`:""));box.append(x)}
 for(const w of (d.shill_warnings||[]).filter(w=>w.score>=0.5).slice(0,2)){const x=el("div","row sh");
  x.append(el("i","tag sh","SHILL SHIELD"),el("b",null,w.kind==="contract"?String(w.entity).slice(0,12)+"…":w.entity),el("span",null,`${(+w.score).toFixed(2)} · ${(w.reasons||[]).slice(0,2).map(s=>String(s).replace(/_/g," ")).join(", ")}`));box.append(x)}
 if(box.children.length===1)box.append(el("div","none","No X burst ahead of its market · no active shill campaigns"))}
function board(lb,d){lb.textContent="";const L=(d.leaderboard||[]).slice(0,6);lb.append(el("h5",null,"SMART MONEY"));
 if(!L.length){lb.append(el("span","none","scoring calls (1h / 24h)…"));return}
 for(const a of L)lb.append(el("span","sm"+(a.status==="promoted"?" p":""),`${a.status==="promoted"?"★":""}@${a.author} ${a.hits}/${a.n} ${(+a.shrunk).toFixed(2)}`))}
function line(g,p,dups){const r=el("div","row ln"+((p.pump_risk||0)>=0.5?" pump":""));
 if(!first&&!SEEN.has(p.id))r.classList.add("new"),r.append(el("i","nb","NEW"));SEEN.add(p.id);
 const a=el("a",null,"@"+p.author);a.href=p.url;a.target="_blank";a.rel="noopener noreferrer";r.append(a);
 const sm=p.smart_money||{};if(sm.status==="promoted")r.append(el("i","tag smp","★"+(+sm.shrunk).toFixed(2)));
 r.append(el("span","dim",ago(p.created_at)));const s=(p.stances||[])[0];
 if(s){r.append(chip(s.stance,s.venue==="token"),el("span","mk",title(s.title)))}
 const v=T.velocity(p);if(v!=null)r.append(el("span","dim",`${kfmt(v)}/h`));if(dups)r.append(el("span","dim",`+${dups} dup`));
 for(const f of (p.flags||[]).filter(f=>/pump|shill|coordinated|swarm/.test(f)).slice(0,1))r.append(el("i","tag sh",f.replace(/_/g," ")));
 const tx=el("span","tx",p.text);r.title=p.text;r.append(tx);return r}
function stream(box,d){box.textContent="";const ps=d.posts||[];if(!ps.length){box.append(el("div","none","Waiting for X drops…"));return}
 const N=d.narratives||[],G=new Map();for(const it of T.dedupe(ps)){const g=T.groupOf(it.p,N);if(!G.has(g))G.set(g,[]);G.get(g).push(it)}
 const meta=Object.fromEntries(N.map(n=>[n.label,n]));
 for(const [g,items] of [...G.entries()].sort((a,b)=>b[1].length-a[1].length)){const n=meta[g];const h=el("div","gh");
  h.append(el("b",null,g),el("span","dim",n?` ${n.phase} · ${(+n.rate_now||0).toFixed(0)}/h · ${n.authors} authors`:` ${items.length} posts`));box.append(h);
  for(const it of items.slice(0,4))box.append(line(g,it.p,it.dups))}first=false}
let root=null;
async function tick(){const xw=document.querySelector(".xw");if(!xw)return;if(!root||!xw.contains(root))root=build(xw);
 try{const d=await (await fetch("/api/x-signal",{credentials:"same-origin"})).json();
  root._asof=d.as_of;fresh(root.querySelector(".fr"),d);alerts(root.querySelector(".al"),d);board(root.querySelector(".lb"),d);stream(root.querySelector(".st"),d)}catch(e){/* keep last frame */}}
function boot(){tick();setInterval(tick,20000);setInterval(()=>{const f=root&&root.querySelector(".fr b");if(f&&root._asof)f.textContent=T.fresh(root._asof)},30000)}
if(typeof document!=="undefined"&&document.addEventListener)(document.readyState==="loading"?document.addEventListener("DOMContentLoaded",boot):boot());
if(typeof window!=="undefined")window.TEV_V7_X={tick,T};
})();
