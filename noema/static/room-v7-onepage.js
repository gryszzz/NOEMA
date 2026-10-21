/* TEV room v7 one-page layout. On laptop-sized screens (≤1900×1150 CSS px) every panel is
 * rendered on ONE scrollable page: the crawler hero fills the first screen, and decisions,
 * prediction markets, news, X wire, strategies, health and culture/alpha follow below it.
 * No tabs and no hidden panels. Bigger screens keep the 4-column wall layout. */
(()=>{"use strict";
const O={on:false};window.TEV_V7_ONEPAGE=O;
O.isLaptop=(w,h)=>w<=1900||h<=1150;
function apply(){const want=O.isLaptop(innerWidth,innerHeight);if(want===O.on)return;O.on=want;
 document.body.classList.toggle("v7-laptop",want);document.body.classList.toggle("v7-one",want);
 window.dispatchEvent(new Event("resize"))}
O.apply=apply;
let rt=0;window.addEventListener("resize",e=>{if(e.isTrusted){clearTimeout(rt);rt=setTimeout(apply,200)}});
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",()=>setTimeout(apply,0));else setTimeout(apply,0);
})();
