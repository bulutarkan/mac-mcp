(function(){
__BOOTSTRAP__
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
function __mcpOwnText(el){
  var out=[];
  try{Array.from(el.childNodes||[]).forEach(function(n){if(n.nodeType===3){var t=String(n.textContent||'').replace(/\s+/g,' ').trim();if(t)out.push(t);}});}catch(e){}
  return out.join(' ').trim().slice(0,240);
}
function __mcpContentCandidate(el,actionable){
  if(actionable) return true;
  var tag=(el.tagName||'').toLowerCase();
  if(/^h[1-6]$/.test(tag)) return true;
  if(tag==='img' && (el.getAttribute('alt')||'').trim()) return true;
  var own=__mcpOwnText(el);
  if(own.length>=2) return true;
  var cls=String(el.className||'').toLowerCase(), id=String(el.id||'').toLowerCase();
  var cardish=/(^|[-_ ])(card|item|listing|result|row|advert|product|property)([-_ ]|$)/.test(cls+' '+id);
  if((tag==='article'||tag==='tr'||tag==='li'||cardish)){
    var txt=__mcpText(el);
    if(txt.length>=2 && txt.length<=700) return true;
  }
  return false;
}
var s=__mcpState();
__mcpSettleHidden();
__mcpStartMutationWatch(s,5000);
__mcpVisual('Inspecting',null,'',1800);
Object.keys(s.elements).forEach(function(k){var e=s.elements[k];if(!e||!e.isConnected){delete s.elements[k];delete s.targetFp[k];}});
var scope=__SCOPE__;
var all=__mcpQueryAll('*');
var elements=[],picked=[];
/* Interactive and visible scopes rank what the user is working in ahead of page order: a floating
   composer, drawer or popover is usually appended last, so a DOM-order cut drops it behind hundreds
   of links further down the page. Order: the layer holding focus, other fixed/sticky layers (the
   one later in the page first, as overlays are appended after the header), the viewport, then the
   rest; within each group fields and text buttons come before icon-only buttons, and nameless
   controls (empty wrappers) go after every named one. Content scopes keep reading order. The scan
   is bounded in time and size. */
var ranked=(scope==='interactive'||scope==='visible'),eligible=0,scanComplete=true,scanStart=Date.now();
var layerMemo=new Map(),layerOrder=new Map();
function __mcpLayer(el){var chain=[],cur=el,res=null,guard=0;
  while(cur&&cur.nodeType===1&&guard++<60){if(layerMemo.has(cur)){res=layerMemo.get(cur);break;}chain.push(cur);
    var pos='';try{pos=__mcpStyle(cur).position;}catch(e){}if(pos==='fixed'||pos==='sticky'){res=cur;break;}cur=__mcpParent(cur);}
  for(var c=0;c<chain.length;c++)layerMemo.set(chain[c],res);return res;}
var focusEl=document.activeElement,focusLayer=(ranked&&focusEl&&focusEl!==document.body&&focusEl!==document.documentElement)?__mcpLayer(focusEl):null;
/* The open modal is found once per scan, and what lies outside it is skipped before the costly
   visibility check: finding it per element re-read the whole page for every element. */
var topModal=__mcpTopBlockingModal();
for(var i=0;i<all.length && (ranked || picked.length<__MAX_ELEMENTS__);i++){
  var el=all[i];
  if(ranked&&(picked.length>=1500||Date.now()-scanStart>400)){scanComplete=false;break;}
  if(topModal&&el!==topModal&&!__mcpComposedContains(topModal,el))continue;
  if(!__mcpSemanticVisible(el)) continue;
  var actionable=__mcpActionable(el);
  if(scope==='interactive' && !actionable) continue;
  if(scope==='visible' && !actionable){
    var txt=__mcpText(el);
    if(!txt || txt.length<2) continue;
  }
  if((scope==='content'||scope==='leaf') && !__mcpContentCandidate(el,actionable)) continue;
  var rank=0;
  if(ranked){
    var layer=__mcpLayer(el),rect=el.getBoundingClientRect();
    var inView=rect.width>0&&rect.height>0&&rect.bottom>0&&rect.right>0&&rect.top<innerHeight&&rect.left<innerWidth;
    rank=(focusLayer&&layer===focusLayer)?0:(layer?1:(inView?2:3));
    if(layer&&!layerOrder.has(layer))layerOrder.set(layer,i);
    var label=String(el.getAttribute('aria-label')||el.getAttribute('title')||__mcpLabelledBy(el)||__mcpPlaceholder(el)||'');
    if(!/^(input|textarea|select)$/i.test(el.tagName||'')&&!el.isContentEditable&&!String(el.textContent||'').replace(/[\s\u200b\u00a0]+/g,''))rank+=label?0.25:4;
  }
  picked.push({el:el,actionable:actionable,rank:rank,order:i,layer:Math.floor(rank)%4===1?layerOrder.get(layer):0});
}
eligible=picked.length;
if(ranked)picked.sort(function(a,b){return a.rank-b.rank||b.layer-a.layer||a.order-b.order;});
picked=picked.slice(0,__MAX_ELEMENTS__);
for(var pi=0;pi<picked.length;pi++){
  var el=picked[pi].el,actionable=picked[pi].actionable;
  var desc=__mcpDescribe(el,s);
  __mcpRememberTarget(el,desc.element_id,s);
  if((scope==='content'||scope==='leaf') && !actionable){
    var own=__mcpOwnText(el);
    if(own) desc.text=own;
    if(!desc.text && (el.getAttribute('alt')||'')) desc.text=String(el.getAttribute('alt')).slice(0,240);
  }
  elements.push(desc);
}
var obs='bobs_'+s.pageToken+'_'+Date.now().toString(36);
s.observations[obs]=s.mutationRevision;
s.observationMeta[obs]={scope:scope,max_elements:__MAX_ELEMENTS__,at:Date.now(),view:{sx:scrollX,sy:scrollY,w:innerWidth,h:innerHeight},
  ids:__mcpVisibleControls(s).map(function(x){return x.id;}),url:location.href,values:__mcpValuesFp()};
s.lastSeen={ids:s.observationMeta[obs].ids,url:location.href,title:document.title};
// Canvas apps draw their controls: say so, and where, instead of returning an empty-looking page.
var canvasHint=(function(){var vw=innerWidth*innerHeight,best=null,area=0;
  Array.from(document.querySelectorAll('canvas')).forEach(function(c){if(!__mcpVisible(c))return;var r=c.getBoundingClientRect();
    var a=Math.max(0,Math.min(r.right,innerWidth)-Math.max(r.left,0))*Math.max(0,Math.min(r.bottom,innerHeight)-Math.max(r.top,0));if(a>area){area=a;best=c;}});
  if(!best||area<vw*0.25)return undefined;var r=best.getBoundingClientRect();
  return {canvas_heavy:true,element_id:__mcpId(best,s),rect:{x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)},coverage:Math.round(area/vw*100)/100,
    hint:'Controls are drawn in a canvas: observe with visual=viewport, then browser_act click/hover with x,y (viewport CSS px) and this observation_id; keys with input_mode=trusted.'};})();
var metrics={screenX:screenX,screenY:screenY,outerWidth:outerWidth,outerHeight:outerHeight,innerWidth:innerWidth,innerHeight:innerHeight,devicePixelRatio:devicePixelRatio};
// Child frames: same-origin ones are already walked above; cross-origin ones are named so they are not silently missing.
var frameList=(function(){var out=[],found=[],scanned=0;
  // Frames can sit inside open shadow roots (web components) and below the fold; find both, bounded.
  (function collect(root,depth){
    try{Array.from(root.querySelectorAll('iframe,frame')).forEach(function(f){if(found.length<30)found.push(f);});}catch(e){}
    if(depth>=4||found.length>=30)return;
    var all=[];try{all=root.querySelectorAll('*');}catch(e){}
    for(var i=0;i<all.length&&scanned<4000;i++){scanned++;if(all[i].shadowRoot)collect(all[i].shadowRoot,depth+1);}
  })(document,0);
  found.forEach(function(f){
  var shown=false;try{var st=getComputedStyle(f),fr=f.getBoundingClientRect();shown=st.display!=='none'&&st.visibility!=='hidden'&&fr.width>0&&fr.height>0;}catch(e){}
  if(out.length>=15||!shown)return;var r=f.getBoundingClientRect(),src=String(f.src||''),same=false;
  try{same=!!(f.contentDocument&&f.contentDocument.documentElement);}catch(e){same=false;}
  var origin='';try{origin=src?new URL(src,location.href).origin:'';}catch(e){}
  out.push({element_id:__mcpId(f,s),origin:origin,src:src.slice(0,160),title:String(f.title||f.name||'').slice(0,80),
    rect:{x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)},same_origin:same,
    hint:same?undefined:'Cross-origin frame: observe/act inside it with frame=<part of its URL> (Chrome; Safari through Accessibility).'});});
  return out.length?out:undefined;})();
return __mcpB64({
  ok:true, observation_id:obs, dom_revision:s.mutationRevision,
  url:location.href,title:document.title,scroll:{x:scrollX,y:scrollY},
  viewport:{w:innerWidth,h:innerHeight},window_metrics:metrics,
  scope:scope,modal_scope:(function(){var m=topModal;return m?{active:true,element_id:__mcpId(m,s),role:__mcpRole(m),text:__mcpText(m).slice(0,120)}:{active:false};})(),element_count:elements.length,elements:elements,
  ordering:ranked?'foreground_first':'document',eligible_count:ranked?eligible:undefined,omitted_count:ranked&&eligible>elements.length?eligible-elements.length:undefined,
  scan_complete:ranked&&!scanComplete?false:undefined,
  closed_shadow_hosts:__mcpClosedShadowHosts(all),
  canvas:canvasHint,frames:frameList
});
})()