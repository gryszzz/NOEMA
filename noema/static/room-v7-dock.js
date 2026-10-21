/* TEV room v7 laptop dock: on laptop-sized screens (≤1900×1150 CSS px) keep the hero
 * (ticker, scan lane, funnel, agents, CHIEF stream) always visible and move secondary
 * panels into a tabbed dock (keys 1–7). Nodes are moved, not cloned, so the v6/v7
 * renderers keep updating them by id; on a larger screen they are put back. */
(()=>{"use strict";
const q=s=>document.querySelector(s),byId=id=>document.getElementById(id);
const TABS=[["news","NEWS",()=>q(".wire")],["x","X WIRE",()=>q(".xw")],
 ["culture","CULTURE · ALPHA",()=>q(".culture-row")],["pm","PREDICTION",()=>byId("pm")&&byId("pm").closest(".pnl")],
 ["money","TREASURY · SAFETY",()=>q(".right")],["acc","ACCURACY · HEALTH",()=>byId("acc")&&byId("acc").closest(".pnl")],
 ["strat","STRATEGIES",()=>byId("strat-pnl")]];
const D={on:false,tab:+(localStorage.v7tab||0)||0,moved:[]};window.TEV_V7_DOCK=D;
D.isLaptop=(w,h)=>w<=1900||h<=1150;
function build(){if(byId("v7-dock"))return;const app=q(".app");if(!app)return;
 app.insertAdjacentHTML("beforeend",`<section class="pnl" id="v7-dock"><h2 class="v7-tabs">${TABS.map((t,i)=>`<button data-i="${i}"><kbd>${i+1}</kbd>${t[1]}</button>`).join("")}</h2><div class="v7-panes">${TABS.map((t,i)=>`<div class="v7-pane" data-i="${i}"></div>`).join("")}</div></section>`);
 byId("v7-dock").querySelectorAll("button").forEach(b=>b.onclick=()=>show(+b.dataset.i))}
function park(el,pane){if(!el||el.closest("#v7-dock"))return;const ph=document.createComment("v7-dock");el.before(ph);D.moved.push([el,ph]);pane.appendChild(el)}
function dock(){build();const panes=[...document.querySelectorAll(".v7-pane")];
 /* accuracy/health live inside .right; take them out first so tab 5 stays money-only */
 park(TABS[5][2](),panes[5]);const hp=byId("health")&&byId("health").closest(".pnl");park(hp,panes[5]);
 for(const i of [0,1,2,3,4,6])park(TABS[i][2](),panes[i]);
 document.body.classList.add("v7-laptop");D.on=true;show(D.tab)}
function undock(){for(const [el,ph] of D.moved.reverse()){ph.replaceWith(el)}D.moved=[];document.body.classList.remove("v7-laptop");D.on=false}
function show(i){D.tab=Math.max(0,Math.min(TABS.length-1,i));localStorage.v7tab=D.tab;
 document.querySelectorAll("#v7-dock .v7-pane").forEach(p=>p.classList.toggle("on",+p.dataset.i===D.tab));
 document.querySelectorAll("#v7-dock .v7-tabs button").forEach(b=>b.classList.toggle("on",+b.dataset.i===D.tab));
 window.dispatchEvent(new Event("resize"))}
D.show=show;
function apply(){const want=D.isLaptop(innerWidth,innerHeight);if(want&&!D.on)dock();else if(!want&&D.on)undock()}
let rt=0;window.addEventListener("resize",e=>{if(e.isTrusted){clearTimeout(rt);rt=setTimeout(apply,200)}});
document.addEventListener("keydown",e=>{if(!D.on||e.metaKey||e.ctrlKey||e.altKey||/input|textarea/i.test(e.target.tagName))return;const n=+e.key;if(n>=1&&n<=TABS.length){show(n-1);e.preventDefault()}});
function boot(){apply();const ov=byId("v7-keys");if(ov&&!byId("v7-k-tabs"))ov.firstElementChild.insertAdjacentHTML("beforeend",`<p id="v7-k-tabs"><kbd>1–7</kbd>laptop dock: ${TABS.map(t=>t[1].toLowerCase()).join(" · ")}</p>`)}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",()=>setTimeout(boot,0));else setTimeout(boot,0);
})();
