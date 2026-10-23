/* TEV candles: tiny canvas OHLC + volume renderer (no CDN, no deps).
 * Real data only: rows are [t(sec), o, h, l, c, v]. Empty -> honest "no candle data" state.
 * TEVCandles.draw(canvas, rows, {interval, label, markers:[{t,kind:"entry"|"exit"|"find"}], volLabel, empty})
 * Re-drawing the same canvas with the same key updates in place (no flicker); a new key fades in. */
(function(){
const UP="#39ff9c",DN="#ff3b6b",CY="#27e7ff",MUTE="#5f8aa3",GRID="rgba(39,231,255,.08)";
const fmt=v=>v==null||!isFinite(v)?"—":Math.abs(v)>=1000?v.toFixed(0):Math.abs(v)>=1?v.toFixed(3):Number(v).toPrecision(4);
const fmtV=v=>!isFinite(v)?"—":v>=1e6?(v/1e6).toFixed(2)+"M":v>=1e3?(v/1e3).toFixed(1)+"k":v.toFixed(v<10?2:0);
const et=t=>new Date(t*1000).toLocaleTimeString("en-US",{timeZone:"America/New_York",hour:"numeric",minute:"2-digit"})+" ET";
function inferInterval(rows){if(!rows||rows.length<2)return null;const d=[];for(let i=1;i<rows.length;i++)d.push(rows[i][0]-rows[i-1][0]);d.sort((a,b)=>a-b);const s=d[d.length>>1];
 return s>=3600?Math.round(s/3600)+"h":Math.max(1,Math.round(s/60))+"m"}
/* numeric intervals are seconds (live launch stream sends tf_s) -> "1s", "5m"; strings pass through */
function tfLabel(v){if(v==null||v==="")return"?";if(typeof v!=="number"||!isFinite(v))return String(v);return v>=3600&&v%3600===0?v/3600+"h":v>=60&&v%60===0?v/60+"m":v+"s"}
function clean(rows){return (rows||[]).filter(r=>Array.isArray(r)&&r.length>=5&&r.slice(1,5).every(x=>isFinite(+x))).map(r=>[+r[0],+r[1],+r[2],+r[3],+r[4],+(r[5]||0)]).sort((a,b)=>a[0]-b[0])}
function paint(cv,st,hover){const W=cv.clientWidth,H=cv.clientHeight,dpr=window.devicePixelRatio||1;if(!W||!H)return;
 if(cv.width!==Math.round(W*dpr)||cv.height!==Math.round(H*dpr)){cv.width=Math.round(W*dpr);cv.height=Math.round(H*dpr)}
 const x=cv.getContext("2d");x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,W,H);x.font="9px ui-monospace,monospace";
 const c=st.rows,o=st.opts;
 x.strokeStyle=GRID;x.lineWidth=1;for(let g=1;g<4;g++){const y=Math.round(H*g/4)+.5;x.beginPath();x.moveTo(0,y);x.lineTo(W,y);x.stroke()}
 if(!c.length){x.fillStyle=MUTE;x.fillText(o.empty||"no candle data",6,H/2);if(o.label){x.fillStyle=CY;x.fillText(o.label,6,11)}return}
 const R=44,top=13,vh=Math.max(8,H*.18),ph=H-top-vh-4,cw=W-R;
 /* opts.slot: fixed px per candle (max). Few candles stay packed and right-aligned (newest at the price tag)
    instead of being stretched across the whole width; many candles shrink to >=3px, then the oldest scroll off. */
 let w,x0,c0=c;if(o.slot){const gap=6,avail=cw-gap;w=Math.max(3,Math.min(o.slot,avail/c.length));const fit=Math.max(1,Math.floor(avail/w));if(c.length>fit)c0=c.slice(-fit);x0=avail-c0.length*w}else{w=cw/c.length;x0=0}
 const cc=c0;let hi=-Infinity,lo=Infinity,vm=0;for(const k of cc){hi=Math.max(hi,k[2]);lo=Math.min(lo,k[3]);vm=Math.max(vm,k[5])}
 if(hi===lo){hi*=1.001;lo*=.999;if(hi===lo){hi+=1e-9;lo-=1e-9}}const pad=(hi-lo)*.06;hi+=pad;lo-=pad;
 const n=cc.length,y=v=>top+(hi-v)/(hi-lo)*ph,bw=o.slot?Math.max(2,Math.round(w*.64)):Math.max(1,Math.min(12,w*.7)),X=i=>x0+i*w+w/2;st.geo={n,w,cw,x0,c:cc};
 for(let i=0;i<n;i++){const[,op,h,l,cl,v]=cc[i],up=cl>=op,cx=o.slot?Math.round(X(i)):X(i),col=up?UP:DN;
  if(vm>0){x.globalAlpha=o.slot?.22:.28;x.fillStyle=col;const hv=v/vm*vh;x.fillRect(Math.round(cx-bw/2),H-hv,bw,hv);x.globalAlpha=1}
  x.strokeStyle=col;x.lineWidth=1;x.beginPath();x.moveTo(Math.round(cx)+.5,y(h));x.lineTo(Math.round(cx)+.5,y(l));x.stroke();
  const y1=y(Math.max(op,cl)),bh=Math.max(1,Math.abs(y(op)-y(cl)));x.fillStyle=col;
  if(o.slot){x.fillRect(Math.round(cx-bw/2),Math.round(y1),bw,Math.max(1,Math.round(bh)))}
  else{x.shadowColor=col;x.shadowBlur=up?4:3;if(up&&bw>3){x.strokeRect(cx-bw/2+.5,y1+.5,bw-1,bh-1)}else x.fillRect(cx-bw/2,y1,bw,bh);x.shadowBlur=0}}
 /* current price line */
 const last=cc[n-1][4],ly=Math.round(y(last))+.5;x.strokeStyle=CY;x.globalAlpha=o.slot?.55:1;x.setLineDash([3,3]);x.beginPath();x.moveTo(o.slot?Math.max(0,x0-6):0,ly);x.lineTo(cw,ly);x.stroke();x.setLineDash([]);x.globalAlpha=1;
 x.fillStyle=CY;x.fillRect(cw+1,ly-6,R-2,12);x.fillStyle="#04131c";x.fillText(fmt(last),cw+3,ly+3);
 /* hi/lo scale labels, skipped when they would collide with the price tag */
 x.fillStyle=MUTE;if(Math.abs(top+4-ly)>12)x.fillText(fmt(hi-pad),cw+3,top+7);if(Math.abs(top+ph-3-ly)>12)x.fillText(fmt(lo+pad),cw+3,top+ph);
 /* markers: our finds / entries / exits */
 for(const m of o.markers||[]){let i=cc.findIndex(k=>k[0]>=m.t);if(i<0)i=n-1;const cx=X(i),col=m.kind==="exit"?DN:m.kind==="entry"?UP:"#ffd23f",yy=m.kind==="exit"?y(cc[i][2])-4:y(cc[i][3])+4;
  x.fillStyle=col;x.beginPath();if(m.kind==="exit"){x.moveTo(cx,yy);x.lineTo(cx-4,yy-6);x.lineTo(cx+4,yy-6)}else{x.moveTo(cx,yy);x.lineTo(cx-4,yy+6);x.lineTo(cx+4,yy+6)}x.fill();
  x.fillText(m.kind==="find"?"F":m.kind==="entry"?"B":"S",cx-3,m.kind==="exit"?yy-8:yy+15)}
 /* header label */
 x.fillStyle=CY;x.fillText(`${o.label?o.label+" · ":""}${tfLabel(st.interval)} candles · ${c.length}`,4,9);
 if(hover!=null&&hover>=0&&hover<n){const k=cc[hover],cx=X(hover);x.strokeStyle="rgba(191,233,255,.5)";x.setLineDash([2,2]);x.beginPath();x.moveTo(Math.round(cx)+.5,top);x.lineTo(Math.round(cx)+.5,H);x.stroke();x.setLineDash([]);
  const t=`${et(k[0])}  O ${fmt(k[1])} H ${fmt(k[2])} L ${fmt(k[3])} C ${fmt(k[4])} ${o.volLabel||"V"} ${fmtV(k[5])}`,tw=x.measureText(t).width+8,tx=Math.min(Math.max(0,cx-tw/2),W-tw);
  x.fillStyle="rgba(4,19,28,.92)";x.fillRect(tx,top,tw,13);x.strokeStyle=CY;x.strokeRect(tx+.5,top+.5,tw-1,12);x.fillStyle="#bfe9ff";x.fillText(t,tx+4,top+10)}}
function draw(cv,rows,opts){opts=opts||{};const st=cv._tev||(cv._tev={});const r=clean(rows);const key=opts.key||opts.label||"";
 const fresh=st.key!==key;st.rows=r;st.opts=opts;st.key=key;st.interval=opts.interval||inferInterval(r);
 if(!st.bound){st.bound=1;cv.style.transition="opacity .35s ease";cv.addEventListener("mousemove",e=>{const g=st.geo;if(!g)return;const b=cv.getBoundingClientRect(),mx=e.clientX-b.left,i=Math.floor((mx-(g.x0||0))/g.w);st.hover=mx<=g.cw&&i>=0&&i<g.n?i:null;paint(cv,st,st.hover)});
  cv.addEventListener("mouseleave",()=>{st.hover=null;paint(cv,st,null)})}
 if(fresh&&st.drawn){cv.style.opacity=".15";setTimeout(()=>{paint(cv,st,st.hover);cv.style.opacity="1"},180)}else paint(cv,st,st.hover);st.drawn=1;return st}
window.TEVCandles={draw,inferInterval,clean,tfLabel,_fmt:fmt};
if(typeof module!=="undefined")module.exports=window.TEVCandles;
})();
