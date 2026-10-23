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
const STAKE_RE=/stake \$([0-9,.]+)/i;
function stakeOf(p){if(typeof p.stake_usd==="number"&&isFinite(p.stake_usd))return p.stake_usd;
 const m=STAKE_RE.exec(String(p.note||""));if(!m)return null;const v=parseFloat(m[1].replace(/,/g,""));return isFinite(v)?v:null}
function usd(v){return v==null?"—":"$"+(v>=10?Math.round(v):v.toFixed(2))}
function ts(s){const t=Date.parse(s||"");return isFinite(t)?t:0}
/* Flatten every post's picks and keep only the latest version of each venue + market + side. */
function latestPicks(posts){const best=new Map();
 for(const post of posts||[])for(const pk of post.picks||[]){const at=ts(post.posted_at);
  const key=[pk.venue,String(pk.market_id||pk.market||"").trim().toLowerCase(),pk.side].join("|");
  const cur=best.get(key);if(!cur||at>cur.at||(at===cur.at&&String(post.id)>String(cur.id)))best.set(key,{at,id:post.id,pk})}
 return [...best.values()].sort((a,b)=>String(a.pk.resolves||"9").localeCompare(String(b.pk.resolves||"9"))||b.at-a.at).map(x=>x.pk)}
function marked(p){return p.status==="settled"||p.current!=null}
function pickRow(p,showMarks){const tr=el("tr");
 const vn=el("td","vn");vn.append(el("span",p.venue==="kalshi"?"vk":"vp",p.venue==="kalshi"?"K":"PM"));
 const mk=el("td","mk",p.market);mk.title=p.market;
 const sd=el("td","sd-"+(p.side==="YES"?"YES":"NO"),p.side);
 const pf=el("td",null,c(p.price)+"→"+c(p.fair));
 const st=el("td",null,usd(stakeOf(p)));
 const rs=el("td","dt",p.resolves?String(p.resolves).slice(5,10):"—");if(p.resolves)rs.title=String(p.resolves);
 tr.append(vn,mk,sd,pf,st,rs);
 if(showMarks){const now=el("td",null,p.status==="settled"?(p.outcome==="won"?"WON":"LOST"):c(p.current));
  if(p.status==="settled")now.className=p.outcome==="won"?"won":"lost";
  const pl=el("td",p.pnl==null?"flat":p.pnl>0?"won":p.pnl<0?"lost":"flat",p.pnl==null?"—":pp(p.pnl));tr.append(now,pl)}
 return tr}
function render(d){mount();const body=document.getElementById("pb-body"),tag=document.getElementById("pb-tag");body.textContent="";
 const picks=latestPicks(d&&d.posts);
 if(!picks.length){body.append(el("div","empty","No Prediction Bot picks yet."));tag.textContent="paper picks · never trades";return}
 const open=picks.filter(p=>p.status!=="settled"),settled=picks.filter(p=>p.status==="settled"),won=settled.filter(p=>p.outcome==="won");
 const pnls=picks.filter(p=>p.pnl!=null).map(p=>p.pnl),pnl=pnls.reduce((a,b)=>a+b,0);
 tag.textContent=`paper · ${open.length} open · ingest ${ago(d.last_run_at)}`;
 const sum=el("div","pb-sum");const kv=(k,v,cl)=>{const sp=el("span",null,k+" ");const b=el("b",cl||null,v);sp.append(b);sum.append(sp)};
 kv("hit rate",settled.length?`${Math.round(won.length/settled.length*100)}% (${won.length}/${settled.length})`:`— (0/${picks.length} settled)`);
 kv("open",String(open.length));kv("marked",`${open.filter(marked).length}/${open.length}`);
 if(pnls.length)kv("paper P&L",pp(pnl)+"/ct",pnl>0?"won":pnl<0?"lost":null);
 body.append(sum);
 const showMarks=picks.some(marked);
 const tb=el("table");const cg=el("colgroup");
 const cols=showMarks?["24px","","36px","74px","46px","46px","40px","48px"]:["24px","","36px","74px","46px","46px"];
 for(const w of cols){const col=el("col");if(w)col.style.width=w;cg.append(col)}tb.append(cg);
 const hr=el("tr");for(const x of (showMarks?["","market","side","px→fair","stake","settle","now","P&L"]:["","market","side","px→fair","stake","settle"]))hr.append(el("th",null,x));tb.append(hr);
 for(const pk of picks)tb.append(pickRow(pk,showMarks));body.append(tb)}
async function poll(){if(busy||document.hidden)return;busy=true;try{const r=await fetch("/api/prediction-bot",{cache:"no-store"});if(r.ok)render(await r.json())}catch(e){}finally{busy=false}}
window.TEV_PB={render,pp,c,stakeOf,latestPicks};
function start(){mount();poll();setInterval(poll,POLL_MS)}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",()=>setTimeout(start,400));else setTimeout(start,400);
})();
