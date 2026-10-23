/* TEV room: PREDICTION BOT panel. Reads /api/prediction-bot (read-only). Prediction Bot drops JSON posts into
 * /workspace/noema-run/prediction-inbox; NOEMA validates them and tracks each pick on PAPER only.
 * All post text is untrusted: rendered with textContent only (no innerHTML of post data). Never trades. */
(function(){"use strict";
const POLL_MS=30000;let busy=false;
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=String(x);return e}
function ago(s){const t=Date.parse(s||"");if(!isFinite(t))return"—";const m=Math.max(0,(Date.now()-t)/6e4);return m<60?Math.round(m)+"m ago":m<2880?Math.round(m/60)+"h ago":Math.round(m/1440)+"d ago"}
function c(v){return v==null||!isFinite(v)?"—":Math.round(v*100)+"¢"}
function pp(v){if(v==null||!isFinite(v))return"—";const x=Math.round(v*1000)/10;return(x>0?"+":"")+x+"¢"}
function mount(){if(document.getElementById("pb-panel"))return document.getElementById("pb-panel");
 const sec=el("section","pnl v7p");sec.id="pb-panel";const h=el("h2",null,"PREDICTION BOT");const tag=el("span","tag","paper picks · never trades");tag.id="pb-tag";h.append(tag);
 const body=el("div","body");body.id="pb-body";body.append(el("div","empty","waiting for /api/prediction-bot…"));sec.append(h,body);
 const anchor=document.getElementById("v7-pmc"),right=document.querySelector(".right");
 if(anchor&&anchor.parentNode)anchor.after(sec);else if(right)right.append(sec);else document.body.append(sec);return sec}
function pickRow(p){const tr=el("tr");
 const vn=el("td","vn");vn.append(el("span",p.venue==="kalshi"?"vk":"vp",p.venue==="kalshi"?"K":"PM"));
 const mk=el("td","mk",p.market);mk.title=[p.market,p.market_id,p.note,p.resolves?"resolves "+p.resolves:"",p.url].filter(Boolean).join(" · ");
 const sd=el("td","sd-"+(p.side==="YES"?"YES":"NO"),p.side);
 const pf=el("td");pf.append(document.createTextNode(c(p.price)+"→"+c(p.fair)+" "),el("span","cf",(p.confidence||"")[0]?.toUpperCase()||""));
 pf.title=`entry ${c(p.price)} · fair ${c(p.fair)} · edge ${pp(p.fair-p.price)} · confidence ${p.confidence||"?"}`;
 const now=el("td",null,p.status==="settled"?(p.outcome==="won"?"WON":"LOST"):c(p.current));
 if(p.status==="settled")now.className=p.outcome==="won"?"won":"lost";
 now.title=p.status==="settled"?"settled on venue resolution":p.current==null?"NOEMA has no fresh quote for this market yet":`${p.current_source||""} · ${ago(p.current_at)}`;
 const pl=el("td",p.pnl==null?"flat":p.pnl>0?"won":p.pnl<0?"lost":"flat",p.pnl==null?"—":pp(p.pnl));pl.title="paper P&L per $1 contract";
 tr.append(vn,mk,sd,pf,now,pl);return tr}
function render(d){mount();const body=document.getElementById("pb-body"),tag=document.getElementById("pb-tag");body.textContent="";
 const s=d&&d.stats;
 if(!d||!(d.posts||[]).length){body.append(el("div","empty",(d&&d.note)||"No Prediction Bot posts yet. Drops land in /workspace/noema-run/prediction-inbox."));tag.textContent="paper picks · never trades";return}
 tag.textContent=`paper · ingest ${ago(d.last_run_at)}`;
 const sum=el("div","pb-sum");const kv=(k,v,cl)=>{const sp=el("span",null,k+" ");const b=el("b",cl||null,v);sp.append(b);sum.append(sp)};
 kv("hit rate",s.settled?`${Math.round(s.hit_rate*100)}% (${s.won}/${s.settled})`:`— (0/${s.picks} settled)`);
 kv("open",String(s.open));kv("marked",`${s.marked_open}/${s.open}`);
 kv("paper P&L",pp(s.paper_pnl_per_contract)+"/ct",s.paper_pnl_per_contract>0?"won":s.paper_pnl_per_contract<0?"lost":null);
 body.append(sum);
 for(const p of d.posts){const box=el("div","pb-post");const h=el("div","pb-h");const t=el("span","pb-t",p.title);t.title=p.title;
  h.append(t,el("span","pb-ago",ago(p.posted_at)));box.append(h);const sm=el("div","pb-s",p.summary);sm.title=p.summary+(p.report_path?"\nreport: "+p.report_path:"");box.append(sm);
  const tb=el("table");const cg=el("colgroup");for(const w of ["24px","","30px","72px","40px","48px"]){const col=el("col");if(w)col.style.width=w;cg.append(col)}tb.append(cg);
  const hr=el("tr");for(const x of ["","market","side","px→fair","now","P&L"])hr.append(el("th",null,x));tb.append(hr);
  for(const pk of p.picks||[])tb.append(pickRow(pk));box.append(tb);body.append(box)}}
async function poll(){if(busy||document.hidden)return;busy=true;try{const r=await fetch("/api/prediction-bot",{cache:"no-store"});if(r.ok)render(await r.json())}catch(e){}finally{busy=false}}
window.TEV_PB={render,pp,c};
function start(){mount();poll();setInterval(poll,POLL_MS)}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",()=>setTimeout(start,400));else setTimeout(start,400);
})();
