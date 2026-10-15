(function(){
__BOOTSTRAP__
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
var s=__mcpState(), cid=__CONTAINER_ID__, sel=__ITEM_SELECTOR__, field=__KEY_FIELD__, box=null, doc=document.scrollingElement||document.documentElement;
function scrollable(el){
  try{var st=getComputedStyle(el);return /(auto|scroll|overlay)/.test(st.overflowY)&&el.scrollHeight>el.clientHeight+20;}catch(e){return false;}
}
if(cid){box=__mcpRecoverElement(cid,s);if(!box)return __mcpB64({ok:false,error:'stale_element',reason_code:'ELEMENT_DETACHED',observe_again:true});
  var up=box;while(up&&up!==document.body&&!scrollable(up))up=up.parentElement;if(up&&up!==document.body)box=up;else if(!scrollable(box))box=doc;}
var candidates=[];
function rowsOf(el){
  // A list container holds many children of one shape (often under a single sizing wrapper).
  var parent=el;while(parent.children.length===1)parent=parent.children[0];
  var shapes={},best=0;Array.from(parent.children).forEach(function(c){var k=c.tagName+'.'+String(c.className||'');shapes[k]=(shapes[k]||0)+1;if(shapes[k]>best)best=shapes[k];});
  return best;
}
if(!cid){
  document.querySelectorAll('*').forEach(function(el){if(!scrollable(el))return;var r=el.getBoundingClientRect();
    var a=Math.max(0,Math.min(r.right,innerWidth)-Math.max(r.left,0))*Math.max(0,Math.min(r.bottom,innerHeight)-Math.max(r.top,0));
    var rows=rowsOf(el);
    // A list scrolled out of view is still the list; other containers count only when visible.
    if(a<=0&&rows<3)return;candidates.push({el:el,area:a,rows:rows});});
  // Prefer the container with the most repeated rows; area breaks ties and decides when nothing repeats.
  candidates.sort(function(x,y){var rx=x.rows>=3?x.rows:0,ry=y.rows>=3?y.rows:0;return (ry-rx)||(y.area-x.area);});
  var pick=candidates[0];
  box=(pick&&(pick.rows>=3||pick.area>innerWidth*innerHeight*0.15))?pick.el:doc;
}
var root=(box===doc)?document.body:box, items=[];
if(sel){try{items=Array.from(root.querySelectorAll(sel));}catch(e){return __mcpB64({ok:false,error:'invalid_item_selector'});}}
else{
  // Only guess the rows when the caller did not name them; a named selector that matches nothing returns nothing.
  items=Array.from(root.querySelectorAll('[role=listitem],[role=row],[role=article],[role=option],li,tr,article'));
  if(!items.length){var parent=root;while(parent.children.length===1)parent=parent.children[0];items=Array.from(parent.children);}
}
function clean(v){return String(v==null?'':v).replace(/\s+/g,' ').trim();}
var out=[];
for(var i=0;i<items.length&&out.length<400;i++){var el=items[i],text=clean(el.innerText||el.textContent);if(!text)continue;
  var link=el.matches('a[href]')?el:el.querySelector('a[href]'),href=link?String(link.href||''):'';
  var key=field==='href'?href:text.toLowerCase();if(!key)continue;out.push({key:key.slice(0,300),text:text.slice(0,200),href:href.slice(0,500)});}
var top=(box===doc)?scrollY:box.scrollTop, height=(box===doc)?doc.scrollHeight:box.scrollHeight, view=(box===doc)?innerHeight:box.clientHeight;
var atEnd=top+view>=height-2;
if(!atEnd){if(box===doc)scrollBy(0,Math.round(view*0.85));else box.scrollTop=top+Math.round(view*0.85);}
// Lazy loaders (infinite scroll) fetch more on a scroll event near the end, even when nothing can move.
try{(box===doc?window:box).dispatchEvent(new Event('scroll'));}catch(e){}
var info={tag:(box===doc)?'document':box.tagName.toLowerCase(),element_id:(box===doc)?null:__mcpId(box,s),scroll_top:Math.round(top),scroll_height:Math.round(height),viewport:Math.round(view)};
var others=candidates.filter(function(c){return c.el!==box;}).slice(0,4).map(function(c){return {element_id:__mcpId(c.el,s),tag:c.el.tagName.toLowerCase(),rows:c.rows,text:clean(c.el.innerText||'').slice(0,60)};});
return __mcpB64({ok:true,items:out,at_end:atEnd,container:info,other_containers:others});
})()