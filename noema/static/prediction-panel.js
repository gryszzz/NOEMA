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
// Picks only: market, side, price, fair, stake, resolves (+ now and P&L once marks exist). No post titles,
// summaries or notes are displayed (they stay stored server-side). Pairs and parlays list their legs.
function money(v){return v==null||!isFinite(v)?"—":"$"+(v>=1000?Math.round(v/100)/10+"k":Math.round(v*100)/100)}
function pct(v){if(v==null||!isFinite(v))return"—";const x=Math.round(v*1000)/10;return(x>0?"+":"")+x+"%"}
function marked(p){return p.status==="settled"||p.current!=null}
function safeUrl(u){return typeof u==="string"&&/^https:\/\/([a-z0-9-]+\.)*(kalshi\.com|polymarket\.com)(\/|$)/i.test(u)?u:null}
function mkCell(text,url){const td=el("td","mk");const u=safeUrl(url);
 if(u){const a=el("a",null,text);a.href=u;a.target="_blank";a.rel="noopener noreferrer";td.append(a)}else td.textContent=String(text);return td}
function venueTag(v){return el("span",v==="kalshi"?"vk":"vp",v==="kalshi"?"K":"PM")}
function px(v){return v==null||!isFinite(v)?"—":v>1?"$"+(Math.round(v*1000)/1000):c(v)}
function pickRow(p,showNow){const kind=p.kind||"single";const tr=el("tr","k-"+kind);const planned=p.status==="planned";
 const sd=kind==="parlay"?(p.legs||[]).length+"-leg":kind==="pair"?"pair":p.side;
 const st=p.posted_stake_usd!=null?p.posted_stake_usd:p.stake_usd;
 tr.append(mkCell(p.market,p.url),el("td",kind==="pair"||kind==="parlay"?"sd-PAR":"sd-"+(p.side==="YES"?"YES":"NO"),sd),
  el("td",null,px(p.price)),el("td",null,px(p.fair)),el("td",null,money(st)),el("td","rs",p.resolves?p.resolves.slice(5):"—"));
 if(showNow){const done=p.status==="settled";
  const now=el("td",done?(p.outcome==="won"?"won":"lost"):null,done?(p.outcome==="won"?"WON":"LOST"):px(p.current));
  const pl=planned?el("td","flat","planned"):el("td",p.pnl==null?"flat":p.pnl>0?"won":p.pnl<0?"lost":"flat",p.pnl==null?"—":pp(p.pnl));
  tr.append(now,pl)}
 const rows=[tr];
 if(kind==="parlay"||kind==="pair")for(const g of p.legs||[]){const lr=el("tr","leg");const m=mkCell("",g.url);
  const a=m.querySelector("a")||m;a.textContent="";a.append(venueTag(g.venue),document.createTextNode(" "+g.market));
  lr.append(m,el("td","sd-"+(g.side==="YES"?"YES":"NO"),g.side),el("td",null,c(g.price)),el("td"),el("td"),el("td"));
  if(showNow)lr.append(el("td",g.result?(g.result==="won"?"won":"lost"):null,g.result?(g.result==="won"?"WON":"LOST"):c(g.current)),el("td"));
  rows.push(lr)}
 if(kind==="pair"&&p.pair){const m=p.pair;const lr=el("tr","leg pm");
  const td=el("td","mk "+(m.locked?"won":"flat"),`cost ${px(m.combined_cost)} · ${m.locked?"locked ":""}${pct(m.profit_per_usd)}/$ · ${pct(m.annualized)} ann · ${m.rules_match?"rules match":"unverified"}`);
  td.colSpan=showNow?8:6;lr.append(td);rows.push(lr)}
 return rows}
function table(picks,showNow){const tb=el("table");const cg=el("colgroup");
 for(const w of ["","40px","40px","36px","44px","40px"].concat(showNow?["40px","46px"]:[])){const col=el("col");if(w)col.style.width=w;cg.append(col)}tb.append(cg);
 const hr=el("tr");for(const x of ["market","side","price","fair","stake","resolves"].concat(showNow?["now","P&L"]:[]))hr.append(el("th",null,x));tb.append(hr);
 for(const pk of picks)for(const r of pickRow(pk,showNow))tb.append(r);return tb}
function render(d){mount();const body=document.getElementById("pb-body"),tag=document.getElementById("pb-tag");body.textContent="";
 let all=(d&&d.picks)||null;if(!all){all=[];for(const p of (d&&d.posts)||[])for(const pk of p.picks||[])all.push(pk)}
 if(!all.length){body.append(el("div","empty","No Prediction Bot picks yet."));tag.textContent="paper picks · never trades";return}
 const s=d.stats||{};tag.textContent=`paper · ${s.open||0} open${s.planned?" · "+s.planned+" planned":""}${s.settled?" · hit "+Math.round(s.hit_rate*100)+"%":""}`;
 const showNow=all.some(marked);
 const groups=[["Singles",x=>(x.kind||"single")==="single"],["Pairs",x=>x.kind==="pair"],["Parlays",x=>x.kind==="parlay"],["Scaled",x=>x.kind==="scaled"]];
 for(const [name,f] of groups){const picks=all.filter(f);if(!picks.length)continue;const box=el("div","pb-grp");
  box.append(el("div","pb-gh",name),table(picks,showNow));body.append(box)}}
async function poll(){if(busy||document.hidden)return;busy=true;try{const r=await fetch("/api/prediction-bot",{cache:"no-store"});if(r.ok)render(await r.json())}catch(e){}finally{busy=false}}
window.TEV_PB={render,pp,c};
function start(){mount();poll();setInterval(poll,POLL_MS)}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",()=>setTimeout(start,400));else setTimeout(start,400);
})();
