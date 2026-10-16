(function(){
__BOOTSTRAP__
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
var s=__mcpState(), cid=__CONTAINER_ID__, sel=__ITEM_SELECTOR__, field=__KEY_FIELD__, opts=__SCAN_OPTIONS__, box=null, doc=document.scrollingElement||document.documentElement;
function scrollable(el){
  try{var st=getComputedStyle(el);return /(auto|scroll|overlay)/.test(st.overflowY)&&el.scrollHeight>el.clientHeight+20;}catch(e){return false;}
}
function scrollBox(el){var up=el;while(up&&up!==document.body&&!scrollable(up))up=up.parentElement;return up&&up!==document.body?up:doc;}
var now=Date.now(), sessions=s.scanSessions||(s.scanSessions=Object.create(null));
Object.keys(sessions).forEach(function(k){if(now-sessions[k].updated>300000)delete sessions[k];});
var fingerprint=JSON.stringify([sel,field,location.href]),session=null,cursor=opts.cursor||opts.new_cursor;
if(opts.cursor){
  session=sessions[opts.cursor];
  if(!session||session.fingerprint!==fingerprint)return __mcpB64({ok:false,error:'invalid_scan_cursor'});
  box=session.box;
  if(!box||!box.isConnected)return __mcpB64({ok:false,error:'invalid_scan_cursor'});
  var currentTop=(box===doc)?scrollY:box.scrollTop;
  if(Math.abs(currentTop-session.expectedTop)>2||(cid&&scrollBox(__mcpRecoverElement(cid,s))!==box))
    return __mcpB64({ok:false,error:'invalid_scan_cursor',detail:'The scan container or scroll position changed; observe before starting another scan.'});
}else if(cid){
  box=__mcpRecoverElement(cid,s);if(!box)return __mcpB64({ok:false,error:'stale_element',reason_code:'ELEMENT_DETACHED',observe_again:true});
  box=scrollBox(box);
}
if(sel){
  try{document.querySelector(sel);}catch(e){return __mcpB64({ok:false,error:'invalid_item_selector'});}
  if(!box){var first=document.querySelector(sel),up=first&&first.parentElement;
    while(up&&up!==document.body&&!scrollable(up))up=up.parentElement;
    box=up&&up!==document.body?up:doc;
  }
}
var candidates=[];
function rowsOf(el){
  // A list container holds many children of one shape (often under a single sizing wrapper).
  var parent=el;while(parent.children.length===1)parent=parent.children[0];
  var shapes={},best=0;Array.from(parent.children).forEach(function(c){var k=c.tagName+'.'+String(c.className||'');shapes[k]=(shapes[k]||0)+1;if(shapes[k]>best)best=shapes[k];});
  return best;
}
if(!box){
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
function visible(el){
  if(!el||/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|NAV|HEADER|FOOTER)$/.test(el.tagName)||el.closest('[hidden],[aria-hidden="true"],#mac-mcp-visual-companion-root'))return false;
  try{for(var p=el,depth=0;p&&depth++<128;p=__mcpFlatParent(p)||((p.ownerDocument.defaultView||{}).frameElement)){
      if(p.hasAttribute('hidden')||p.getAttribute('aria-hidden')==='true')return false;var st=__mcpStyle(p);if(st.display==='none'||st.visibility==='hidden'||st.visibility==='collapse'||Number(st.opacity||1)===0)return false;}
    if(__mcpStyle(el).display==='contents')return true;var r=el.getBoundingClientRect();return r.width>0&&r.height>0;}catch(e){return false;}
}
function readText(el){
  var walker=el.ownerDocument.createTreeWalker(el,4),node,out=[];
  while((node=walker.nextNode()))if(visible(node.parentElement))out.push(node.nodeValue);
  return clean(out.join(' '));
}
function signature(text){
  var a=2166136261,b=5381;for(var i=0;i<text.length;i++){a=Math.imul(a^text.charCodeAt(i),16777619);b=Math.imul(b,33)^text.charCodeAt(i);}
  return (a>>>0).toString(16)+'_'+(b>>>0).toString(16)+'_'+text.length;
}
if(!session){
  var keys=Object.keys(sessions);if(keys.length>=4){keys.sort(function(a,b){return sessions[a].updated-sessions[b].updated;});delete sessions[keys[0]];}
  session={fingerprint:fingerprint,box:box,seen:Object.create(null),updated:now,total:0,tracked:0,nodeIds:new WeakMap(),nodeSequence:0};
  if(cursor)sessions[cursor]=session;
}
session.updated=now;
var out=[],used=0,chars=0,pending=false,stopped='',eligible=0;
var itemCap=Math.max(1,Math.min(opts.max_items||400,500)),charCap=Math.max(1,opts.max_chars||20000),byteCap=Math.max(256,opts.max_bytes||48000);
if(items.length>10000)return __mcpB64({ok:false,error:'scan_candidate_limit',next_cursor:cursor});
for(var i=0;i<items.length;i++){
  var el=items[i];if(!visible(el))continue;
  var text=readText(el);if(!text)continue;eligible++;
  var link=el.matches('a[href]')?el:el.querySelector('a[href]'),href=link?String(link.href||''):'';
  var identity=el.id||el.getAttribute('data-id')||el.getAttribute('data-key');
  if(!identity){identity=session.nodeIds.get(el);if(!identity){identity='node_'+(++session.nodeSequence);session.nodeIds.set(el,identity);}}
  var rawKey=field==='href'?href:(String(identity)+'|'+text);
  if(!rawKey||(field==='href'&&!href))continue;
  var key=signature(rawKey),offset=session.seen[key]||0;
  if(offset===-1)continue;
  if(out.length>=itemCap){pending=true;stopped='max_items';break;}
  if(session.tracked>=10000&&!Object.prototype.hasOwnProperty.call(session.seen,key)){pending=true;stopped='state_limit';break;}
  var remaining=charCap-chars,chunk=text.slice(offset,offset+remaining);
  if(/[\uD800-\uDBFF]$/.test(chunk))chunk=chunk.slice(0,-1);
  if(!chunk){pending=true;stopped='payload_budget';break;}
  var record={key:key+':'+offset,record_id:key,text:chunk,text_offset:offset,text_complete:offset+chunk.length>=text.length};
  if(href)record.href=href.slice(0,500);
  function bytes(v){return new TextEncoder().encode(JSON.stringify(v)).length;}
  if(bytes(record)+used>byteCap){
    var low=0,high=chunk.length;
    while(low<high){var mid=Math.ceil((low+high)/2);record.text=chunk.slice(0,mid);if(bytes(record)+used<=byteCap)low=mid;else high=mid-1;}
    record.text=chunk.slice(0,low);if(/[\uD800-\uDBFF]$/.test(record.text))record.text=record.text.slice(0,-1);low=record.text.length;record.text_complete=offset+low>=text.length;
    if(!low){pending=true;stopped='payload_budget';break;}
  }
  out.push(record);used+=bytes(record);chars+=record.text.length;
  if(!Object.prototype.hasOwnProperty.call(session.seen,key))session.tracked++;
  if(record.text_complete){session.seen[key]=-1;session.total++;}
  else{session.seen[key]=offset+record.text.length;pending=true;stopped='payload_budget';break;}
}
var top=(box===doc)?scrollY:box.scrollTop, height=(box===doc)?doc.scrollHeight:box.scrollHeight, view=(box===doc)?innerHeight:box.clientHeight;
var atEnd=top+view>=height-2;
if(!pending&&!atEnd){if(box===doc)scrollBy(0,Math.round(view*0.85));else box.scrollTop=top+Math.round(view*0.85);}
// Lazy loaders (infinite scroll) fetch more on a scroll event near the end, even when nothing can move.
if(!pending)try{(box===doc?window:box).dispatchEvent(new Event('scroll'));}catch(e){}
session.expectedTop=(box===doc)?scrollY:box.scrollTop;
var info={tag:(box===doc)?'document':box.tagName.toLowerCase(),element_id:(box===doc)?null:__mcpId(box,s),scroll_top:Math.round(top),scroll_height:Math.round(height),viewport:Math.round(view)};
var others=candidates.filter(function(c){return c.el!==box;}).slice(0,4).map(function(c){return {element_id:__mcpId(c.el,s),tag:c.el.tagName.toLowerCase(),rows:c.rows,text:clean(c.el.innerText||'').slice(0,60)};});
return __mcpB64({ok:true,items:out,at_end:atEnd&&!pending,container:info,other_containers:others,
  next_cursor:cursor||null,total_seen:session.total,eligible_count:eligible,stopped:stopped||null});
})()