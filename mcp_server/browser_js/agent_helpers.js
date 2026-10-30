
function __mcpInternalHost(el){try{return !!el&&el.id==='mac-mcp-visual-companion-root';}catch(e){return false;}}
function __mcpInternalMutation(rec){
  if(rec.type==='attributes'&&rec.attributeName==='data-mac-mcp-visual-event')return true;
  if(rec.type==='attributes'&&/^data-mac-mcp-(id|page|n)$/.test(rec.attributeName||''))return true;
  if(__mcpInternalHost(rec.target))return true;
  if(rec.type!=='childList')return false;
  var nodes=Array.prototype.concat.call([],Array.prototype.slice.call(rec.addedNodes||[]),Array.prototype.slice.call(rec.removedNodes||[]));
  return nodes.length>0&&nodes.every(function(n){return __mcpInternalHost(n);});
}
function __mcpRoots(){
  var roots=[],seen=new Set();
  function visit(root,depth){
    if(!root||seen.has(root)||depth>10)return;seen.add(root);roots.push(root);
    var nodes=[];try{nodes=Array.from(root.querySelectorAll('*'));}catch(e){return;}
    for(var i=0;i<nodes.length;i++){
      var el=nodes[i];
      try{if(el.shadowRoot&&!__mcpInternalHost(el))visit(el.shadowRoot,depth+1);}catch(e){}
      var tag=String(el.tagName||'').toLowerCase();
      if(tag==='iframe'||tag==='frame'){try{if(el.contentDocument)visit(el.contentDocument,depth+1);}catch(e){}}
    }
  }
  visit(document,0);return roots;
}
function __mcpQueryAll(selector){
  var out=[],seen=new Set(),roots=__mcpRoots();
  for(var r=0;r<roots.length;r++){
    var nodes=[];try{nodes=Array.from(roots[r].querySelectorAll(selector));}catch(e){continue;}
    for(var i=0;i<nodes.length;i++){if(!seen.has(nodes[i])){seen.add(nodes[i]);out.push(nodes[i]);}}
  }
  return out;
}
function __mcpQueryOne(selector){var all=__mcpQueryAll(selector);return all.length?all[0]:null;}
function __mcpOwnerWindow(el){try{return (el.ownerDocument&&el.ownerDocument.defaultView)||window;}catch(e){return window;}}
function __mcpStyle(el){try{return __mcpOwnerWindow(el).getComputedStyle(el);}catch(e){return getComputedStyle(el);}}
function __mcpTopRect(el){
  var r=el.getBoundingClientRect(),left=r.left,top=r.top,w=r.width,h=r.height,win=__mcpOwnerWindow(el),guard=0;
  while(win&&win!==window&&guard++<10){var frame=null;try{frame=win.frameElement;}catch(e){}if(!frame)break;var fr=frame.getBoundingClientRect();left+=fr.left;top+=fr.top;win=__mcpOwnerWindow(frame);}
  return {left:left,top:top,right:left+w,bottom:top+h,width:w,height:h};
}
function __mcpStopMutationWatch(s){
  if(s.observerTimer){try{clearTimeout(s.observerTimer);}catch(e){}s.observerTimer=null;}
  var obs=s.rootObservers||[];for(var i=0;i<obs.length;i++){try{obs[i].disconnect();}catch(e){}}s.rootObservers=[];
}
function __mcpStartMutationWatch(s,ttl){
  __mcpStopMutationWatch(s);var roots=__mcpRoots(),bump=function(records){
    for(var j=0;j<records.length;j++){var rec=records[j];if(__mcpInternalMutation(rec))continue;s.mutationRevision+=1;s.lastMutationAt=Date.now();break;}
  };
  for(var i=0;i<roots.length;i++){try{var ob=new MutationObserver(bump);ob.observe(roots[i],{subtree:true,childList:true,attributes:true,characterData:true});s.rootObservers.push(ob);}catch(e){}}
  s.observerTimer=setTimeout(function(){__mcpStopMutationWatch(s);},Math.max(500,Math.min(Number(ttl||3000),8000)));
}
/* Chrome can run page scripts in two worlds (the companion debugger, or Apple Events when a frame of another
   extension blocks the debugger). The page token, the id counter and every element id live on the DOM,
   which both worlds share, so an id handed out in one world still finds its element in the other. */
function __mcpPageToken(){var de=document.documentElement,t=de&&de.getAttribute('data-mac-mcp-page');if(!t||!/^[a-z0-9]{4,12}$/.test(t)){t=Math.random().toString(36).slice(2,10);try{de.setAttribute('data-mac-mcp-page',t);}catch(e){}}return t;}
function __mcpNextId(s){var de=document.documentElement,n=Math.max(Number(de&&de.getAttribute('data-mac-mcp-n'))||0,s.counter)+1;s.counter=n;try{de.setAttribute('data-mac-mcp-n',String(n));}catch(e){}return n;}
/* Typing or picking a value moves no DOM node, so the mutation revision misses it: a short
   fingerprint of the form controls lets a repeat observe tell that the page did change. */
function __mcpValuesFp(){var h=0,list=document.querySelectorAll('input,textarea,select');for(var i=0;i<list.length&&i<2000;i++){var el=list[i],v=String(el.value==null?'':el.value)+(el.checked?'1':'0');for(var j=0;j<v.length;j++){h=(h*31+v.charCodeAt(j))|0;}h=(h*31+i)|0;}return list.length+':'+h;}
function __mcpState(){
  var s=window.__macMcpBrowserAgent;
  if(!s){var stableAt=Date.now();try{var nav=performance.getEntriesByType&&performance.getEntriesByType('navigation')[0];if(document.readyState==='complete'&&nav&&nav.loadEventEnd>0&&performance.now()-nav.loadEventEnd>=300)stableAt=Date.now()-1000;}catch(e){}s=window.__macMcpBrowserAgent={counter:0,ids:new WeakMap(),elements:Object.create(null),pageToken:__mcpPageToken(),mutationRevision:0,lastMutationAt:stableAt,observations:Object.create(null),observationMeta:Object.create(null),rootObservers:[],observerTimer:null};}
if(!s.observationMeta)s.observationMeta=Object.create(null);
if(!s.targetFp)s.targetFp=Object.create(null);
  return s;
}
function __mcpVisualTarget(el){
  try{if(!el||el.nodeType!==1)return 'Page';var tag=(el.tagName||'').toLowerCase(),role=(el.getAttribute('role')||'').toLowerCase(),type=(el.getAttribute('type')||'').toLowerCase(),aria=(el.getAttribute('aria-label')||'');
    if(tag==='button'||role==='button'||(tag==='input'&&['button','submit','reset'].indexOf(type)>=0))return 'Button';
    if(tag==='a'||role==='link')return 'Link';
    if(tag==='textarea'||el.isContentEditable||role==='textbox'||role==='searchbox'||(tag==='input'&&['checkbox','radio','button','submit','reset'].indexOf(type)<0))return 'Text field';
    if(tag==='select'||role==='combobox'||role==='listbox'||role==='menu'||role==='menuitem')return 'Menu';
    if(type==='checkbox'||role==='checkbox'||role==='switch')return 'Checkbox';if(type==='radio'||role==='radio'||role==='option')return 'Option';if(role==='tab')return 'Tab';
    if(/(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday).*(?:january|february|march|april|may|june|july|august|september|october|november|december)/i.test(aria))return 'Date';return 'Item';
  }catch(e){return 'Item';}}
function __mcpVisual(action,el,effect,ttl,detail){
  try{var payload={seq:Date.now().toString(36)+'_'+Math.random().toString(36).slice(2,7),claim:true,phase:'working',action:String(action||'Working').slice(0,40),target_kind:__mcpVisualTarget(el),ttl_ms:Math.max(500,Math.min(Number(ttl||1800),30000))};
    if(el&&el.nodeType===1){var r=__mcpTopRect(el);if(isFinite(r.left)&&isFinite(r.top)&&isFinite(r.width)&&isFinite(r.height)){payload.x=Math.max(0,Math.min(innerWidth,Math.round(r.left+r.width/2)));payload.y=Math.max(0,Math.min(innerHeight,Math.round(r.top+r.height/2)));}}
    if(effect==='click')payload.effect='click';if(['Up','Down','Into view'].indexOf(String(detail||''))>=0)payload.detail=String(detail);if(typeof window.__mcpVisualWho==='string'&&window.__mcpVisualWho)payload.who=window.__mcpVisualWho.slice(0,80);
    var raw=btoa(unescape(encodeURIComponent(JSON.stringify(payload))));(document.documentElement||document.body).setAttribute('data-mac-mcp-visual-event',raw);try{window.dispatchEvent(new Event('mac-mcp-visual'));}catch(e){}
  }catch(e){}}
function __mcpId(el,s){var id=s.ids.get(el);if(!id){var tagged=el.getAttribute&&el.getAttribute('data-mac-mcp-id');id=tagged&&tagged.indexOf('e_'+s.pageToken+'_')===0?tagged:'e_'+s.pageToken+'_'+__mcpNextId(s);s.ids.set(el,id);if(id!==tagged){try{el.setAttribute('data-mac-mcp-id',id);}catch(e){}}}s.elements[id]=el;return id;}
function __mcpRoute(){var h=String(location.hash||'');return String(location.pathname||'')+String(location.search||'')+((h.indexOf('#/')===0||h.indexOf('#!')===0)?h:'');}
function __mcpTargetFp(el){
  var role=String(__mcpRole(el)||''),tag=String(el.tagName||'').toLowerCase();
  var editable=!!el.isContentEditable||['input','textarea','select'].indexOf(tag)>=0||['textbox','searchbox','combobox'].indexOf(role)>=0;
  var attr=function(n){try{return String(el.getAttribute(n)||'');}catch(e){return '';}};
  var label=attr('aria-label')||attr('name')||attr('placeholder')||attr('title')||(editable?'':__mcpText(el));
  return {route:__mcpRoute(),role:role,tag:tag,name:__mcpNorm(label).replace(/[0-9]+/g,'#').slice(0,120)};
}
function __mcpRememberTarget(el,id,s){if(!el||!id)return;try{s.targetFp[id]=__mcpTargetFp(el);}catch(e){}}
/* A background tab does not advance CSS transitions, so a composer or dialog that opens
   there stays at its first frame (height 2px, controls clipped) until the tab is shown.
   Finish short running UI transitions in a hidden document so its controls are laid out. */
function __mcpSettleHidden(){try{if(document.visibilityState!=='hidden'||!document.getAnimations)return 0;var n=0;
  document.getAnimations().forEach(function(a){try{if(a.playState!=='running')return;var t=a.effect&&a.effect.getComputedTiming&&a.effect.getComputedTiming();
    if(!t||!isFinite(t.endTime)||t.endTime>5000)return;a.finish();n++;}catch(e){}});return n;}catch(e){return 0;}}
/* The visible controls that the "changes" of an act are measured against; observe records the same set,
   so a small max_elements does not make everything it left out look new afterwards. */
function __mcpVisibleControls(s){
  var sel='button,a[href],input,textarea,select,summary,[role=button],[role=link],[role=option],[role=menuitem],[role=menuitemradio],[role=menuitemcheckbox],[role=tab],[role=checkbox],[role=radio],[role=combobox],[role=switch],[contenteditable=true],[draggable=true]';
  var now=[],seen=Object.create(null),nodes=__mcpQueryAll(sel);
  for(var i=0;i<nodes.length&&now.length<500;i++){var el=nodes[i];if(!__mcpVisible(el))continue;var id=__mcpId(el,s);if(seen[id])continue;seen[id]=1;now.push({id:id,el:el});}
  return now;
}
/* Milliseconds left on short UI animations still running in a visible page (a panel opening). */
function __mcpAnimatingMs(){try{if(document.visibilityState==='hidden'||!document.getAnimations)return 0;var left=0;
  document.getAnimations().forEach(function(a){try{if(a.playState!=='running')return;var t=a.effect.getComputedTiming();
    if(!isFinite(t.endTime)||t.endTime>2000)return;left=Math.max(left,t.endTime-(t.localTime||0));}catch(e){}});
  return Math.max(0,Math.min(1000,Math.round(left)));}catch(e){return 0;}}
function __mcpVisible(el){if(!el||el.nodeType!==1)return false;var st=__mcpStyle(el);if(st.display==='none'||st.visibility==='hidden'||parseFloat(st.opacity||'1')===0)return false;var r=__mcpTopRect(el);if(r.width<1||r.height<1)return false;return r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;}
function __mcpActionable(el){
  var tag=(el.tagName||'').toLowerCase(),role=(el.getAttribute('role')||'').toLowerCase();
  if(['a','button','input','textarea','select','summary','details','label'].indexOf(tag)>=0)return true;
  if(['button','link','checkbox','radio','tab','menuitem','menuitemradio','menuitemcheckbox','option','combobox','textbox','searchbox','switch','slider','listbox'].indexOf(role)>=0)return true;
  /* Only the editing host: the paragraphs inside a rich editor are not separate controls. */
  if((el.isContentEditable&&!__mcpEditableChild(el))||el.hasAttribute('onclick')||el.getAttribute('draggable')==='true')return true;var cls=String(el.className||'');if(/collapseTitle|collapse-title|dropdown-toggle|select-trigger|clickable|toggle/i.test(cls))return true;
  try{
    if(__mcpStyle(el).cursor==='pointer'){
      var parent=__mcpParent(el),parentPointer=false;try{parentPointer=!!parent&&__mcpStyle(parent).cursor==='pointer';}catch(_){}
      var pointerTag=String(el.tagName||'').toLowerCase();
      if(!parentPointer&&['path','g','use','circle','rect','polygon','polyline'].indexOf(pointerTag)<0)return true;
    }
  }catch(e){}
  var ti=el.getAttribute('tabindex');return ti!==null&&Number(ti)>=0;
}
/* Rendering parent: a slotted element is drawn inside the slot it is assigned to (the
   <dialog><slot> in a web component shadow root), not inside its light-DOM parent. */
function __mcpFlatParent(el){if(!el)return null;try{if(el.assignedSlot)return el.assignedSlot;}catch(e){}return __mcpParent(el);}
function __mcpComposedContains(root,node){
  var cur=node,guard=0;while(cur&&guard++<64){if(cur===root)return true;cur=__mcpFlatParent(cur);}return false;
}
function __mcpAssociation(el){
  var out={control:null,label:null,ambiguous:false};if(!el||el.nodeType!==1)return out;
  var tag=String(el.tagName||'').toLowerCase(),doc=el.ownerDocument||document;
  if(tag==='label'){
    out.label=el;
    try{out.control=el.control||null;}catch(e){}
    if(!out.control){
      var fid='';try{fid=String(el.getAttribute('for')||'');}catch(e){}
      if(fid)try{out.control=doc.getElementById(fid)||null;}catch(e){}
    }
    if(!out.control)try{out.control=el.querySelector('input,select,textarea,button,[role="radio"],[role="checkbox"],[role="switch"]');}catch(e){}
    return out;
  }
  var labels=[];
  try{labels=Array.from(el.labels||[]).filter(function(x){return x&&x.isConnected;});}catch(e){}
  if(!labels.length){
    var id='';try{id=String(el.id||'');}catch(e){}
    if(id)try{labels=Array.from(doc.querySelectorAll('label[for]')).filter(function(x){return String(x.htmlFor||x.getAttribute('for')||'')===id;});}catch(e){}
  }
  var visible=labels.filter(function(x){try{return __mcpVisible(x);}catch(e){return false;}});
  if(visible.length===1)out.label=visible[0];
  else if(labels.length===1)out.label=labels[0];
  else if(visible.length>1||labels.length>1)out.ambiguous=true;
  if(out.label)out.control=el;
  return out;
}
function __mcpAssociationText(el){
  var a=__mcpAssociation(el),parts=[];
  if(a.label)try{parts.push(__mcpText(a.label));}catch(e){}
  if(a.control&&a.control!==el)try{parts.push(__mcpText(a.control));}catch(e){}
  return parts.filter(Boolean).join(' ').replace(/\s+/g,' ').trim().slice(0,240);
}
function __mcpAssociationRelated(a,b){
  if(!a||!b)return false;
  var aa=__mcpAssociation(a),bb=__mcpAssociation(b);
  if(aa.ambiguous||bb.ambiguous)return false;
  var ac=aa.control||a,bc=bb.control||b;
  if(ac===bc)return true;
  if(aa.label&&(aa.label===b||__mcpComposedContains(aa.label,b)))return true;
  if(bb.label&&(bb.label===a||__mcpComposedContains(bb.label,a)))return true;
  return false;
}
function __mcpSemanticVisible(el){
  if(!el||el.nodeType!==1)return false;
  var cur=el,guard=0;while(cur&&guard++<20){
    try{
      var cs=__mcpStyle(cur),state=String(cur.getAttribute&&cur.getAttribute('data-state')||'').toLowerCase(),r=String(cur.getAttribute&&cur.getAttribute('role')||'').toLowerCase(),am=String(cur.getAttribute&&cur.getAttribute('aria-modal')||'').toLowerCase();
      if(cs.display==='none'||cs.visibility==='hidden'||String(cur.getAttribute&&cur.getAttribute('aria-hidden')||'').toLowerCase()==='true')return false;
      if(state==='closed'&&(r==='dialog'||am==='true'))return false;
    }catch(e){}
    cur=__mcpFlatParent(cur);
  }
  if(__mcpVisible(el))return true;
  var role=String(__mcpRole(el)||'').toLowerCase(),tag=String(el&&el.tagName||'').toLowerCase(),type=String(el&&el.getAttribute&&el.getAttribute('type')||'').toLowerCase();
  if(['radio','checkbox','switch'].indexOf(role)<0&&!(tag==='input'&&['radio','checkbox'].indexOf(type)>=0))return false;
  var a=__mcpAssociation(el);return !a.ambiguous&&!!a.label&&__mcpVisible(a.label);
}
function __mcpTopBlockingModal(){
  var all=__mcpQueryAll('dialog[open],[aria-modal="true"],[role="dialog"]'),best=null,bestZ=-2147483648,bestOrder=-1;
  for(var i=0;i<all.length;i++){
    var el=all[i],tag=String(el.tagName||'').toLowerCase(),role=String(el.getAttribute&&el.getAttribute('role')||'').toLowerCase(),aria=String(el.getAttribute&&el.getAttribute('aria-modal')||'').toLowerCase(),state=String(el.getAttribute&&el.getAttribute('data-state')||'').toLowerCase(),st=null,rect=null;
    if(state==='closed')continue;
    try{st=__mcpStyle(el);rect=__mcpTopRect(el);}catch(e){}
    var semanticOpen=state==='open'||(tag==='dialog'&&el.hasAttribute('open'))||aria==='true';
    var structurallyVisible=!!(st&&rect&&st.display!=='none'&&st.visibility!=='hidden'&&rect.width>=1&&rect.height>=1&&rect.bottom>0&&rect.right>0&&rect.top<innerHeight&&rect.left<innerWidth);
    if(!__mcpVisible(el)&&!(semanticOpen&&structurallyVisible))continue;
    var blocking=semanticOpen||(role==='dialog'&&st&&String(st.position||'').toLowerCase()==='fixed');
    if(!blocking)continue;
    if(best&&__mcpComposedContains(best,el)){best=el;bestOrder=i;try{bestZ=parseInt(st.zIndex,10)||0;}catch(e){bestZ=0;}continue;}
    var z=0;try{z=parseInt(st.zIndex,10);if(!isFinite(z))z=0;}catch(e){}
    if(!best||z>bestZ||(z===bestZ&&i>bestOrder)){best=el;bestZ=z;bestOrder=i;}
  }
  return best;
}
function __mcpModalScope(el){
  var modal=__mcpTopBlockingModal();if(!modal)return {active:false,inside:true,modal:null};
  return {active:true,inside:el===modal||__mcpComposedContains(modal,el),modal:modal};
}
function __mcpElementReadiness(el,kind,minStableMs){
  var rd=__mcpElementReadinessOnce(el,kind,minStableMs),k=String(kind||'click').toLowerCase();
  if(k==='observe'||!rd||(rd.reason_code!=='ELEMENT_OFFSCREEN'&&rd.reason_code!=='ELEMENT_OCCLUDED'))return rd;
  var t=(k==='click'||k==='double_click'||k==='select')?__mcpActivationTarget(el):el;
  try{t.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});}catch(e){return rd;}
  var again=__mcpElementReadinessOnce(el,kind,minStableMs);again.scrolled_into_view=true;return again;
}
function __mcpElementReadinessOnce(el,kind,minStableMs){
  kind=String(kind||'click').toLowerCase();minStableMs=Math.max(0,Number(minStableMs||0));
  if(!el||el.nodeType!==1||!el.isConnected)return {ready:false,reason_code:'ELEMENT_DETACHED'};
  if((kind==='click'||kind==='double_click'||kind==='select')&&__mcpActivationAmbiguous(el))
    return {ready:false,reason_code:'AMBIGUOUS_ACTIVATION_TARGET',controls:__mcpInnerControls(el).slice(0,6).map(function(c){return __mcpNorm(__mcpText(c)||c.getAttribute('aria-label')||c.getAttribute('title')||'').slice(0,60);})};
  var target=(kind==='click'||kind==='double_click'||kind==='select')?__mcpActivationTarget(el):el;
  if(!target||!target.isConnected)return {ready:false,reason_code:'ELEMENT_DETACHED'};
  var s=__mcpState(),st=null,r=null,topRect=null;
  try{st=__mcpStyle(target);r=target.getBoundingClientRect();topRect=__mcpTopRect(target);}catch(e){return {ready:false,reason_code:'ELEMENT_NOT_READY'};}
  var modalScope=__mcpModalScope(target),assoc=__mcpAssociation(target);
  var base={ready:false,reason_code:null,element_id:__mcpId(target,s),stable_for_ms:Math.max(0,Date.now()-Number(s.lastMutationAt||Date.now())),dom_revision:s.mutationRevision,
    rect:{x:Math.round(topRect.left),y:Math.round(topRect.top),w:Math.round(topRect.width),h:Math.round(topRect.height)},pointer_events:String(st.pointerEvents||''),
    modal_scope:modalScope.active?(modalScope.inside?'inside':'outside'):'none'};
  if(assoc.control&&assoc.control!==target)base.associated_control={element_id:__mcpId(assoc.control,s),tag:String(assoc.control.tagName||'').toLowerCase(),role:__mcpRole(assoc.control)};
  if(assoc.label&&assoc.label!==target)base.associated_label={element_id:__mcpId(assoc.label,s),text:__mcpText(assoc.label)};
  if(assoc.ambiguous)base.association_ambiguous=true;
  if(modalScope.active&&!modalScope.inside){base.reason_code='ELEMENT_OUTSIDE_MODAL_SCOPE';return base;}
  if(st.display==='none'||st.visibility==='hidden'||parseFloat(st.opacity||'1')===0){base.reason_code='ELEMENT_HIDDEN';return base;}
  if(!isFinite(r.width)||!isFinite(r.height)||r.width<=0||r.height<=0){base.reason_code='ELEMENT_ZERO_BOUNDS';return base;}
  if(topRect.bottom<=0||topRect.right<=0||topRect.top>=innerHeight||topRect.left>=innerWidth){base.reason_code='ELEMENT_OFFSCREEN';return base;}
  if(target.disabled===true||target.getAttribute('aria-disabled')==='true'||target.closest&&target.closest('[inert]')){base.reason_code='ELEMENT_DISABLED';return base;}
  if((kind==='type'||kind==='type_text'||kind==='paste')&&(target.readOnly===true||target.getAttribute('readonly')!==null)){base.reason_code='ELEMENT_READONLY';return base;}
  if((kind==='type'||kind==='type_text'||kind==='paste')){
    var tag=String(target.tagName||'').toLowerCase(),role=String(__mcpRole(target)||'').toLowerCase();
    if(!(tag==='input'||tag==='textarea'||target.isContentEditable||role==='textbox'||role==='searchbox'||tag==='select')){base.reason_code='ELEMENT_NOT_EDITABLE';return base;}
  }else if((kind==='click'||kind==='double_click'||kind==='select')&&!__mcpActionable(target)){base.reason_code='ELEMENT_NOT_ACTIONABLE';return base;}
  var p=target,depth=0,pointerBlocked=false;while(p&&depth++<12){try{if(__mcpStyle(p).pointerEvents==='none')pointerBlocked=true;if(p.getAttribute&&p.getAttribute('aria-busy')==='true'){base.reason_code='ELEMENT_BUSY';return base;}}catch(e){}p=__mcpParent(p);}
  if(base.stable_for_ms<minStableMs){base.reason_code='ELEMENT_UNSTABLE';return base;}
  try{
    var doc=target.ownerDocument||document,win=doc.defaultView||window,lr=target.getBoundingClientRect(),cx=lr.left+lr.width/2,cy=lr.top+lr.height/2;
    if(cx<0||cy<0||cx>=win.innerWidth||cy>=win.innerHeight){base.reason_code='ELEMENT_OFFSCREEN';return base;}
    var hit=doc.elementFromPoint(cx,cy);base.hit_tag=hit?String(hit.tagName||'').toLowerCase():null;
    if(hit)base.hit_target={tag:String(hit.tagName||'').toLowerCase(),role:__mcpRole(hit),element_id:__mcpId(hit,s)};
    var containsHit=!!hit&&__mcpComposedContains(target,hit),containedByHit=!!hit&&__mcpComposedContains(hit,target),associationRelated=!!hit&&__mcpAssociationRelated(target,hit),related=containsHit||containedByHit||associationRelated;
    if(pointerBlocked&&!(containsHit||associationRelated)){base.reason_code='ELEMENT_POINTER_EVENTS_NONE';return base;}
    if(!hit||!related){base.reason_code='ELEMENT_OCCLUDED';return base;}
    if(pointerBlocked&&(containsHit||associationRelated))base.pointer_events_association_fallback=true;
  }catch(e){base.reason_code='ELEMENT_HIT_TEST_FAILED';return base;}
  base.ready=true;base.reason_code=null;return base;
}
/* Rich editors (ProseMirror, Tiptap) put their placeholder on the empty first paragraph, not on the editable itself. */
function __mcpPlaceholder(el){try{var v=el.getAttribute('placeholder')||el.getAttribute('aria-placeholder')||el.getAttribute('data-placeholder')||'';if(v||!el.isContentEditable)return String(v);
  var c=el.querySelector('[data-placeholder]');return c&&!String(c.textContent||'').trim()?String(c.getAttribute('data-placeholder')||''):'';}catch(e){return '';}}
function __mcpText(el){var aria=el.getAttribute('aria-label')||'',ph=__mcpPlaceholder(el),title=el.getAttribute('title')||'',txt='';try{txt=(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim();}catch(e){}
  /* Short visible text names a control before its tooltip (a 'Create Topic' button titled 'Or press Cmd Enter'). */
  var name=aria||ph||(txt.length<=80?txt:'')||title||txt;
  /* A field inside a web component is often named only on its host (<faceplate-text-input label="Password">). */
  if(!name){var host=__mcpShadowHost(el);if(host)name=host.getAttribute('aria-label')||host.getAttribute('label')||host.getAttribute('placeholder')||'';}
  return String(name).slice(0,240);}
function __mcpShadowHost(el){try{var root=el.getRootNode&&el.getRootNode();return root&&root.host&&root!==document?root.host:null;}catch(e){return null;}}
/* Closed shadow roots cannot be read from the page. A defined custom element that is drawn
   but has no open root and no children probably hides its controls in one: say so instead of
   letting it look empty. */
function __mcpClosedShadowHosts(all){var tags=[],n=0;
  for(var i=0;i<all.length&&n<50;i++){var el=all[i],tag=String(el.tagName||'').toLowerCase();if(tag.indexOf('-')<0||el.shadowRoot||el.childElementCount)continue;
    try{if(!window.customElements||!customElements.get(tag))continue;var r=el.getBoundingClientRect();if(r.width<24||r.height<16||!__mcpVisible(el))continue;}catch(e){continue;}
    n++;if(tags.indexOf(tag)<0&&tags.length<5)tags.push(tag);}
  return n?{count:n,tags:tags,hint:'These components keep their controls in closed shadow roots, which page scripts cannot read; use visual=element on one of them.'}:undefined;}
function __mcpRole(el){var role=el.getAttribute('role');if(role)return role;var tag=(el.tagName||'').toLowerCase();if(tag==='a')return'link';if(tag==='button')return'button';if(tag==='select')return'combobox';if(tag==='textarea')return'textbox';if(tag==='input'){var t=(el.type||'text').toLowerCase();if(t==='checkbox')return'checkbox';if(t==='radio')return'radio';if(t==='range')return'slider';if(['button','submit','reset'].indexOf(t)>=0)return'button';return'textbox';}if(el.isContentEditable&&!__mcpEditableChild(el))return'textbox';return'';}
function __mcpEditableChild(el){try{var p=__mcpParent(el);return !!p&&!!p.isContentEditable;}catch(e){return false;}}
function __mcpContext(el){
  try{
    var own=__mcpText(el),p=__mcpParent(el),depth=0;
    while(p&&depth++<8){
      var role=String(p.getAttribute&&p.getAttribute('role')||'').toLowerCase();
      var cls=String(p.className||'').toLowerCase();
      var semantic=['grid','listbox','menu','dialog','tooltip','group','radiogroup'].indexOf(role)>=0 || /(?:^|[\s_-])(month|calendar|datepicker|date-picker|listbox|menu|option-group|suggestions?|results?)(?:[\s_-]|$)/i.test(cls);
      if(semantic){
        var labelled='';
        try{
          var labelledBy=p.getAttribute('aria-labelledby');
          if(labelledBy){var doc=p.ownerDocument||document,node=doc.getElementById(labelledBy);if(node)labelled=String(node.innerText||node.textContent||'').replace(/\s+/g,' ').trim();}
        }catch(e){}
        var candidates=[];
        if(labelled)candidates.push(labelled);
        try{
          var heads=Array.from(p.querySelectorAll('.rdp-caption_label,[data-caption],[aria-live="polite"],legend,[role="heading"],h1,h2,h3,h4,h5,h6'));
          for(var i=0;i<heads.length&&i<10;i++){
            var head=heads[i],txt=String(head.innerText||head.textContent||'').replace(/\s+/g,' ').trim();
            if(txt&&txt.length<=120)candidates.push(txt);
          }
        }catch(e){}
        for(var j=0;j<candidates.length;j++){var text=candidates[j];if(text&&text!==own)return text.slice(0,120);}
      }
      p=__mcpParent(p);
    }
  }catch(e){}
  return '';
}
function __mcpRect(el){var r=__mcpTopRect(el),ox=(window.outerWidth-window.innerWidth),oy=(window.outerHeight-window.innerHeight),viewportX=window.screenX+Math.max(0,Math.round(ox/2)),viewportY=window.screenY+Math.max(0,Math.round(oy));return{viewport:{x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)},document:{x:Math.round(r.left+scrollX),y:Math.round(r.top+scrollY),w:Math.round(r.width),h:Math.round(r.height)},screen:{x:Math.round(viewportX+r.left),y:Math.round(viewportY+r.top),w:Math.round(r.width),h:Math.round(r.height),estimated:true}};}
function __mcpDescribe(el,s){
  var tag=(el.tagName||'').toLowerCase(),rect=__mcpRect(el),out={element_id:__mcpId(el,s),tag:tag,role:__mcpRole(el),text:__mcpText(el),viewport_rect:rect.viewport,screen_rect:rect.screen,actionable:__mcpActionable(el)};
  var shadowHost=__mcpShadowHost(el);if(shadowHost)out.shadow_host=String(shadowHost.tagName||'').toLowerCase();
  var modalScope=__mcpModalScope(el);out.modal_scope=modalScope.active?(modalScope.inside?'inside':'outside'):'none';
  var assoc=__mcpAssociation(el),associationText=__mcpAssociationText(el);if(associationText)out.association_text=associationText;
  if(assoc.control&&assoc.control!==el){out.associated_control={element_id:__mcpId(assoc.control,s),tag:String(assoc.control.tagName||'').toLowerCase(),role:__mcpRole(assoc.control)};if(String(assoc.control.tagName||'').toLowerCase()==='input'){if(assoc.control.type)out.associated_control.input_type=String(assoc.control.type);if(__mcpSuggests(assoc.control))out.associated_control.suggests=true;}}
  if(assoc.label&&assoc.label!==el)out.associated_label={element_id:__mcpId(assoc.label,s),text:__mcpText(assoc.label)};
  if(assoc.ambiguous)out.association_ambiguous=true;
  if(out.actionable){var rd=__mcpElementReadiness(el,'observe',0);out.ready=!!rd.ready;if(rd.reason_code)out.readiness_reason=rd.reason_code;if(rd.hit_target)out.hit_target=rd.hit_target;}
  var context=__mcpContext(el);if(context)out.context=context;
  var aria=el.getAttribute('aria-label')||'';if(!aria&&el.getAttribute('aria-labelledby')){var lroot=(el.getRootNode&&el.getRootNode().getElementById)?el.getRootNode():document;aria=String(el.getAttribute('aria-labelledby')).split(/\s+/).map(function(i){var n=i&&lroot.getElementById(i);return n?String(n.textContent||'').replace(/\s+/g,' ').trim():'';}).filter(Boolean).join(' ');}var ph=__mcpPlaceholder(el),name=el.getAttribute('name')||'',title=el.getAttribute('title')||'';if(aria)out.aria_label=aria.slice(0,120);if(ph)out.placeholder=ph.slice(0,100);if(name)out.name=name.slice(0,100);if(title)out.title=title.slice(0,100);if(tag==='a'&&el.href)out.href=String(el.href).slice(0,220);if(el.disabled===true||el.getAttribute('aria-disabled')==='true')out.enabled=false;
  try{if(el.ownerDocument&&el.ownerDocument.activeElement===el)out.focused=true;}catch(e){}if(['input','textarea','select'].indexOf(tag)>=0)out.value=__mcpSafeValue(el).slice(0,160);if(tag==='input'&&el.type)out.input_type=String(el.type);if(tag==='input'&&__mcpSuggests(el))out.suggests=true;if(typeof el.checked==='boolean'&&el.checked)out.checked=true;if(tag==='select')out.options=Array.from(el.options||[]).slice(0,24).map(function(o){return{text:String(o.text||'').slice(0,90),value:String(o.value||'').slice(0,90),selected:!!o.selected};});return out;
}
/* A text box that offers a list while typing (a search box, an autocomplete): marked by ARIA, or by a
   page script that turns off the suggestions of the browser to show its own. No quote marks in comments
   here: the export scan reads them as strings. */
function __mcpSuggests(el){var a=function(n){return String(el.getAttribute(n)||'').toLowerCase();},popup=a('aria-haspopup');return a('role')==='combobox'||a('aria-autocomplete')==='list'||a('aria-autocomplete')==='both'||popup==='listbox'||popup==='true'||!!el.getAttribute('aria-controls')||!!el.getAttribute('aria-owns')||a('autocomplete')==='off';}
function __mcpParent(el){if(!el)return null;if(el.parentElement)return el.parentElement;try{var root=el.getRootNode&&el.getRootNode();return root&&root.host?root.host:null;}catch(e){return null;}}
function __mcpActivationTarget(el){
  if(!el)return el;
  var assoc=__mcpAssociation(el),tag=String(el.tagName||'').toLowerCase(),type=String(el.getAttribute&&el.getAttribute('type')||'').toLowerCase();
  if(assoc.ambiguous)return el;
  if(tag==='input'&&['radio','checkbox'].indexOf(type)>=0&&assoc.label&&__mcpVisible(assoc.label))return assoc.label;
  if(tag==='label'&&assoc.control){
    if(__mcpVisible(el))return el;
    if(__mcpVisible(assoc.control))return assoc.control;
  }
  if(__mcpActionable(el))return el;
  var inner=__mcpInnerControls(el);
  /* A box holding one control stands for it; one holding several (Star and Delete in a row) stands for none (#390). */
  if(inner.length===1)return inner[0];if(inner.length>1)return el;
  var p=__mcpParent(el),n=0;while(p&&n++<4){if(__mcpActionable(p))return p;p=__mcpParent(p);}return el;
}
/* The separate controls shown inside a box that is not a control itself; a control inside another counts once. */
function __mcpInnerControls(el){
  var selector='button,a,input,textarea,select,summary,label,[role="button"],[role="link"],[role="combobox"],[role="option"],[role="menuitem"],[role="tab"],[role="checkbox"],[role="radio"],[role="switch"],[tabindex],[draggable="true"]';
  var found=[];try{found=Array.from(el.querySelectorAll(selector)).filter(function(c){return __mcpVisible(c);});}catch(e){return [];}
  return found.filter(function(c){return !found.some(function(o){return o!==c&&o.contains(c);});});
}
function __mcpActivationAmbiguous(el){
  if(!el||el.nodeType!==1||__mcpActionable(el))return false;
  var tag=String(el.tagName||'').toLowerCase();if(tag==='label'||tag==='input')return false;
  return __mcpInnerControls(el).length>1;
}
function __mcpScrollIntoView(el){if(!el)return;try{el.scrollIntoView({block:'center',inline:'nearest'});}catch(e){}var win=__mcpOwnerWindow(el),guard=0;while(win&&win!==window&&guard++<10){var frame=null;try{frame=win.frameElement;}catch(e){}if(!frame)break;try{frame.scrollIntoView({block:'center',inline:'nearest'});}catch(e){}win=__mcpOwnerWindow(frame);}}
function __mcpMouseEvent(el,type){var win=__mcpOwnerWindow(el),r=el.getBoundingClientRect(),x=Math.max(0,Math.round(r.left+r.width/2)),y=Math.max(0,Math.round(r.top+r.height/2)),common={bubbles:true,cancelable:true,composed:true,view:win,clientX:x,clientY:y,button:0,buttons:(type==='pointerdown'||type==='mousedown')?1:0};try{if(type.indexOf('pointer')===0&&typeof win.PointerEvent==='function')return new win.PointerEvent(type,Object.assign({pointerId:1,pointerType:'mouse',isPrimary:true},common));return new win.MouseEvent(type,common);}catch(e){return null;}}
function __mcpStartActionNetworkProbe(s,trace){
  if(!s||!trace)return;trace.network_count=0;trace.network_paths=[];var win=window,X=win.XMLHttpRequest,gen=Number(s.networkProbeGeneration||0)+1;s.networkProbeGeneration=gen;s.networkProbeTrace=trace;
  function record(raw){var current=s.networkProbeTrace;if(!current)return;try{current.network_count+=1;var value=raw&&raw.url?raw.url:raw,u=new URL(String(value||''),location.href),prefix=u.origin===location.origin?'same-origin:':'cross-origin:';if(current.network_paths.length<4)current.network_paths.push(prefix+String(u.pathname||'/').slice(0,180));}catch(e){current.network_count+=1;}}
  if(!s.networkProbeInstalled){
    s.networkProbeInstalled=true;s.networkProbeOriginalFetch=win.fetch;s.networkProbeOriginalXhrOpen=X&&X.prototype?X.prototype.open:null;s.networkProbeOriginalXhrSend=X&&X.prototype?X.prototype.send:null;
    if(typeof s.networkProbeOriginalFetch==='function'){s.networkProbeFetchWrapper=function(){record(arguments[0]);return s.networkProbeOriginalFetch.apply(this,arguments);};try{win.fetch=s.networkProbeFetchWrapper;}catch(e){}}
    if(X&&X.prototype&&typeof s.networkProbeOriginalXhrOpen==='function'&&typeof s.networkProbeOriginalXhrSend==='function'){
      s.networkProbeOpenWrapper=function(method,url){try{this.__macMcpActionProbeUrl=url;}catch(e){}return s.networkProbeOriginalXhrOpen.apply(this,arguments);};
      s.networkProbeSendWrapper=function(){try{record(this.__macMcpActionProbeUrl||'');}catch(e){}return s.networkProbeOriginalXhrSend.apply(this,arguments);};
      try{X.prototype.open=s.networkProbeOpenWrapper;X.prototype.send=s.networkProbeSendWrapper;}catch(e){}
    }
  }
  setTimeout(function(){if(Number(s.networkProbeGeneration||0)!==gen)return;try{if(s.networkProbeFetchWrapper&&win.fetch===s.networkProbeFetchWrapper)win.fetch=s.networkProbeOriginalFetch;}catch(e){}try{if(X&&X.prototype&&s.networkProbeOpenWrapper&&X.prototype.open===s.networkProbeOpenWrapper)X.prototype.open=s.networkProbeOriginalXhrOpen;if(X&&X.prototype&&s.networkProbeSendWrapper&&X.prototype.send===s.networkProbeSendWrapper)X.prototype.send=s.networkProbeOriginalXhrSend;}catch(e){}s.networkProbeInstalled=false;s.networkProbeTrace=null;s.networkProbeFetchWrapper=null;s.networkProbeOpenWrapper=null;s.networkProbeSendWrapper=null;},250);
}
function __mcpActivate(el){
  if(__mcpActivationAmbiguous(el))throw new Error('ambiguous_activation_target');
  el=__mcpActivationTarget(el);if(!el)throw new Error('element_not_found');if(el.disabled===true||el.getAttribute('aria-disabled')==='true')throw new Error('element_disabled');__mcpScrollIntoView(el);
  var s=__mcpState(),win=__mcpOwnerWindow(el),trace={mode:'synthetic_dom',target_click_seen:false,window_click_seen:false,click_is_trusted:null,click_default_prevented:null,dispatch_canceled:[]};__mcpStartActionNetworkProbe(s,trace);
  var targetRecorder=function(ev){trace.target_click_seen=true;trace.click_is_trusted=!!ev.isTrusted;};
  var windowRecorder=function(ev){try{var path=typeof ev.composedPath==='function'?ev.composedPath():[];if(ev.target===el||path.indexOf(el)>=0||__mcpComposedContains(el,ev.target)){trace.window_click_seen=true;trace.click_is_trusted=!!ev.isTrusted;trace.click_default_prevented=!!ev.defaultPrevented;}}catch(e){}};
  try{el.addEventListener('click',targetRecorder,{capture:true,once:true});win.addEventListener('click',windowRecorder,{capture:false,once:true});}catch(e){}
  var events=['pointerover','mouseover','pointermove','mousemove','pointerdown','mousedown'];for(var i=0;i<events.length;i++){var ev=__mcpMouseEvent(el,events[i]);if(ev)try{if(!el.dispatchEvent(ev))trace.dispatch_canceled.push(events[i]);}catch(e){}}
  try{el.focus({preventScroll:true});}catch(e){try{el.focus();}catch(_){}}events=['pointerup','mouseup'];for(var j=0;j<events.length;j++){var up=__mcpMouseEvent(el,events[j]);if(up)try{if(!el.dispatchEvent(up))trace.dispatch_canceled.push(events[j]);}catch(e){}}
  try{el.click();}finally{try{el.removeEventListener('click',targetRecorder,true);win.removeEventListener('click',windowRecorder,false);}catch(e){}}
  s.lastActivationTrace=trace;return el;
}
function __mcpDoubleActivate(el){el=__mcpActivate(el);__mcpActivate(el);var ev=__mcpMouseEvent(el,'dblclick');if(ev)try{el.dispatchEvent(ev);}catch(e){}return el;}
function __mcpInputEvent(el,type,data,inputType,cancelable){var win=__mcpOwnerWindow(el);try{if(typeof win.InputEvent==='function')return new win.InputEvent(type,{bubbles:true,cancelable:!!cancelable,composed:true,data:data,inputType:inputType});}catch(e){}try{return new win.Event(type,{bubbles:true,cancelable:!!cancelable,composed:true});}catch(e){return null;}}
function __mcpNativeValueSetter(el,value){var win=__mcpOwnerWindow(el),tag=String(el.tagName||'').toLowerCase(),proto=null;if(tag==='input')proto=win.HTMLInputElement&&win.HTMLInputElement.prototype;else if(tag==='textarea')proto=win.HTMLTextAreaElement&&win.HTMLTextAreaElement.prototype;else if(tag==='select')proto=win.HTMLSelectElement&&win.HTMLSelectElement.prototype;if(proto){try{var d=Object.getOwnPropertyDescriptor(proto,'value');if(d&&typeof d.set==='function'){d.set.call(el,value);return true;}}catch(e){}}try{el.value=value;return true;}catch(e){return false;}}
function __mcpRecoverElement(id,s){
  var old=s.elements[id];if(old&&old.isConnected)return old;
  if(!old){/* An id handed out in the other world of the page: find it by its DOM tag. */
    if(!/^e_[a-z0-9]+_[0-9]+$/.test(String(id)))return null;var tagged=null;try{tagged=document.querySelector('[data-mac-mcp-id="'+id+'"]');}catch(e){}
    if(!tagged)return null;s.elements[id]=tagged;try{s.ids.set(tagged,id);}catch(e){}return tagged;}
  var ident={tag:String(old.tagName||'').toLowerCase(),role:__mcpRole(old),text:__mcpText(old),aria:old.getAttribute&&old.getAttribute('aria-label')||'',name:old.getAttribute&&old.getAttribute('name')||'',ph:old.getAttribute&&old.getAttribute('placeholder')||'',title:old.getAttribute&&old.getAttribute('title')||''};
  /* The old node keeps its detached row (list item, table row, card), so a twin control in another row is not it. */
  var row=__mcpRowKey(old),all=__mcpQueryAll('*'),ranked=[];
  for(var i=0;i<all.length;i++){var el=all[i];if(!el.isConnected||!__mcpSemanticVisible(el))continue;var score=0,role=__mcpRole(el),tag=String(el.tagName||'').toLowerCase();if(ident.role&&role===ident.role)score+=2;if(ident.tag&&tag===ident.tag)score+=1;
    var pairs=[['aria','aria-label'],['name','name'],['ph','placeholder'],['title','title']];for(var j=0;j<pairs.length;j++){var want=ident[pairs[j][0]];if(want&&String(el.getAttribute(pairs[j][1])||'')===want)score+=4;}if(ident.text&&__mcpText(el)===ident.text)score+=3;if(score>=5&&(!row||__mcpRowKey(el)===row))ranked.push({el:el,score:score});}
  /* Two equally good matches are ambiguous: never pick one of them. */
  ranked.sort(function(a,b){return b.score-a.score;});if(!ranked.length)return null;if(ranked.length>1&&ranked[0].score===ranked[1].score)return null;
  var recovered=ranked[0].el;s.elements[id]=recovered;try{s.ids.set(recovered,id);}catch(e){}return recovered;
}
function __mcpRowKey(el){try{var r=el&&el.closest&&el.closest('li,tr,[role=row],[role=listitem],article');return r?String(r.textContent||'').replace(/[0-9]+/g,'#').replace(/\s+/g,' ').trim().slice(0,300):'';}catch(e){return '';}}
function __mcpNorm(v){return String(v||'').normalize('NFKD').toLowerCase().replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9çğıöşü]+/g,' ').replace(/\s+/g,' ').trim();}
function __mcpRecoverAction(a,s){
  var id=String(a&&a.element_id||''), byId=id?__mcpRecoverElement(id,s):null;if(byId)return byId;
  var query=__mcpNorm(a&&(a.query||a.target||a.text_match||a.target_text)||''), wantedRole=__mcpNorm(a&&a.role||'');
  if(!query&&!wantedRole)return null;
  var qTokens=query.split(' ').filter(Boolean),all=__mcpQueryAll('*'),ranked=[],topModal=__mcpTopBlockingModal();
  for(var i=0;i<all.length;i++){
    var el=all[i];if(!el.isConnected)continue;
    /* Outside the open modal: skipped before the costly checks; the modal is found once, not per element. */
    if(topModal&&el!==topModal&&!__mcpComposedContains(topModal,el))continue;
    if(!__mcpSemanticVisible(el)||!__mcpActionable(el))continue;
    var role=__mcpNorm(__mcpRole(el));if(wantedRole&&role!==wantedRole)continue;
    var d=__mcpDescribe(el,s),fields=[d.text||'',d.aria_label||'',d.placeholder||'',d.name||'',d.title||'',d.value||'',d.context||'',d.association_text||''],score=wantedRole?2:0;
    for(var j=0;j<fields.length;j++){
      var f=__mcpNorm(fields[j]);if(!f)continue;
      if(query&&f===query)score=Math.max(score,12);
      else if(query&&(f.indexOf(query+' ')===0||f.indexOf(query+'-')===0))score=Math.max(score,9);
      else if(query&&qTokens.length&&qTokens.every(function(t){return f.split(' ').indexOf(t)>=0;}))score=Math.max(score,8);
      else if(query&&query.length>=4&&f.indexOf(query)>=0)score=Math.max(score,6);
    }
    if(score>=6||(!query&&wantedRole))ranked.push({el:el,score:score,text:__mcpText(el)});
  }
  ranked.sort(function(x,y){var d=y.score-x.score;if(d)return d;return String(x.text||'').length-String(y.text||'').length;});
  if(!ranked.length)return null;
  if(ranked.length>1&&ranked[0].score===ranked[1].score&&ranked[0].score<10)return null;
  var recovered=ranked[0].el;if(id){s.elements[id]=recovered;try{s.ids.set(recovered,id);}catch(e){}}return recovered;
}
function __mcpFlushMutations(s){
  var changed=false,obs=s.rootObservers||[];
  for(var i=0;i<obs.length;i++){
    var records=[];try{records=obs[i].takeRecords();}catch(e){}
    for(var j=0;j<records.length;j++){var rec=records[j];if(__mcpInternalMutation(rec))continue;changed=true;break;}
  }
  if(changed){s.mutationRevision+=1;s.lastMutationAt=Date.now();}return changed;
}
function __mcpModalEffectFingerprint(){
  var modal=__mcpTopBlockingModal();if(!modal)return '';
  var rect=null;try{rect=__mcpTopRect(modal);}catch(e){}
  return [String(modal.tagName||'').toLowerCase(),String(__mcpRole(modal)||''),String(modal.getAttribute&&modal.getAttribute('data-state')||''),__mcpText(modal).slice(0,180),rect?Math.round(rect.width):0,rect?Math.round(rect.height):0].join('|');
}
function __mcpEffectState(el){
  var agent=__mcpState(),activationTrace=agent.lastActivationTrace||null,activationNetworkCount=Number(activationTrace&&activationTrace.network_count||0);
  if(!el)return {connected:false,modalFingerprint:__mcpModalEffectFingerprint(),activationNetworkCount:activationNetworkCount};var role=__mcpRole(el),value='',text='';
  try{value=('value' in el)?__mcpSafeValue(el):'';}catch(e){}
  try{if(!value&&(el.isContentEditable||role==='textbox'||role==='searchbox'))text=String(el.textContent||'');}catch(e){}
  return {connected:!!el.isConnected,value:value,text:text,checked:typeof el.checked==='boolean'?!!el.checked:null,expanded:el.getAttribute('aria-expanded'),selected:el.getAttribute('aria-selected'),pressed:el.getAttribute('aria-pressed'),ariaChecked:el.getAttribute('aria-checked'),cls:String(el.className||''),modalFingerprint:__mcpModalEffectFingerprint(),activationNetworkCount:activationNetworkCount};
}
function __mcpEffectChanged(before,after){
  if(!before||!after)return false;if(before.connected&&!after.connected)return true;
  var keys=['value','text','checked','expanded','selected','pressed','ariaChecked','cls','modalFingerprint','activationNetworkCount'];for(var i=0;i<keys.length;i++){if(before[keys[i]]!==after[keys[i]])return true;}return false;
}
function __mcpKeyboardActivate(el){
  if(!el)return '';var role=String(__mcpRole(el)||'').toLowerCase(),key=(role==='combobox'||role==='listbox')?'ArrowDown':((role==='checkbox'||role==='switch'||role==='radio')?' ':'Enter');
  try{el.focus({preventScroll:true});}catch(e){try{el.focus();}catch(_){}}
  var win=__mcpOwnerWindow(el);function fire(type){try{el.dispatchEvent(new win.KeyboardEvent(type,{bubbles:true,cancelable:true,composed:true,key:key,code:key===' '?'Space':key}));}catch(e){}}
  fire('keydown');fire('keypress');fire('keyup');return key;
}
/* A password field never reports its value: observations, effects and extraction say [hidden]. */
function __mcpSafeValue(el){try{if(String(el.type||'').toLowerCase()==='password')return el.value?'[hidden]':'';return String(el.value==null?'':el.value);}catch(e){return '';}}
function __mcpEditableText(el){try{return String(el.innerText!=null?el.innerText:el.textContent||'');}catch(e){return String(el.textContent||'');}}
/* Editors such as Medium, Google Docs and Notion rewrite punctuation as you type (smart quotes,
   -- to a dash, ... to an ellipsis); that is the text arriving, not missing. */
function __mcpTextMatches(actual,value){var n=function(v){return String(v||'').replace(/[\u2018\u2019\u201a\u201b\u2032]/g,"'").replace(/[\u201c\u201d\u201e\u201f\u2033]/g,'"').replace(/\u2026/g,'...').replace(/[\u2013\u2014]|--/g,'-').replace(/[\s\u00a0\u200b]+/g,' ').trim();};return n(actual)===n(value);}
/* Rich-text editors (Draft.js, Lexical, ProseMirror, the Medium composer) keep their own
   model and ignore or mis-read text written straight into the DOM (Draft even crashes
   when the browser edits its blocks underneath it), so a contenteditable gets text the
   way a person pasting it would: the selection is announced (selectionchange, which
   React-based editors read their selection from) and a paste carrying the text is
   offered to the editor. A field that does not handle paste gets execCommand, which
   fires the same trusted beforeinput/input as typing. Assigning textContent is the last
   resort and is reported as such.
   (No apostrophes here: _top_level_function_names tracks quotes, not comments.) */
function __mcpSelectEditable(el,clearFirst,backward){var doc=el.ownerDocument||document,win=__mcpOwnerWindow(el);
  /* A background tab gets no focus events, so React keeps the previous field as active and drops the new selection.
     In a focused page React also drops a selection identical to the last one it saw (the caret the settling pass
     placed); a focusin on the already focused field makes it forget that one. Editors ignore a focus they hold. */
  try{if(!doc.hasFocus()){el.dispatchEvent(new win.FocusEvent('focus',{relatedTarget:null}));el.dispatchEvent(new win.FocusEvent('focusin',{bubbles:true,relatedTarget:null}));}
    else if(doc.activeElement===el){el.dispatchEvent(new win.FocusEvent('focusin',{bubbles:true,relatedTarget:null}));}}catch(e){}
  var sel=win.getSelection(),range=doc.createRange();range.selectNodeContents(el);if(clearFirst===false)range.collapse(false);sel.removeAllRanges();
  if(backward&&clearFirst!==false&&sel.setBaseAndExtent)sel.setBaseAndExtent(range.endContainer,range.endOffset,range.startContainer,range.startOffset);else sel.addRange(range);
  try{doc.dispatchEvent(new win.Event('selectionchange'));}catch(e){}}
function __mcpExecInsert(el,value,clearFirst){
  var doc=el.ownerDocument||document,ok=true;
  try{if(!value)return clearFirst===false?true:doc.execCommand('delete',false,null);var lines=value.split(/\r\n|\r|\n/);
    for(var i=0;i<lines.length&&ok;i++){if(i>0)ok=doc.execCommand('insertParagraph',false,null);if(ok&&lines[i])ok=doc.execCommand('insertText',false,lines[i]);}}catch(e){return false;}
  return !!ok;
}
/* Editors that keep the text in their own model and render the field from it. They take a
   paste into that model without firing input, so seeing the text is enough for them. */
function __mcpModelEditor(el){try{return !!(el.closest&&el.closest('.ProseMirror,[data-lexical-editor],.public-DraftEditor-content,[data-slate-editor],.ql-editor,.ck-editor__editable,.mce-content-body,.cm-content'));}catch(e){return false;}}
/* A page can cancel the paste and write the text into the DOM itself without telling its
   framework (React state, a form model); the text shows but the next rerender drops it and
   submit stays disabled. Such a paste is accepted only when the field is a known editor or
   the page announced the change with beforeinput/input; otherwise s.lastPasteUnconfirmed. */
function __mcpPasteInsert(el,value){
  var win=__mcpOwnerWindow(el),s=__mcpState(),announced=false,note=function(){announced=true;};s.lastPasteUnconfirmed=false;
  try{var dt=new win.DataTransfer();dt.setData('text/plain',value);
    var ev=new win.ClipboardEvent('paste',{bubbles:true,cancelable:true,composed:true,clipboardData:dt});
    el.addEventListener('beforeinput',note,true);el.addEventListener('input',note,true);
    var handled;try{handled=!el.dispatchEvent(ev);}finally{el.removeEventListener('beforeinput',note,true);el.removeEventListener('input',note,true);}
    if(handled&&!announced&&!__mcpModelEditor(el))s.lastPasteUnconfirmed=true;return handled;}catch(e){return false;}
}
function __mcpAppendable(el){var tag=String(el.tagName||'').toLowerCase();if(tag==='textarea')return true;if(tag!=='input')return false;return ['text','search','url','tel','email','password'].indexOf(String(el.type||'text').toLowerCase())>=0;}
function __mcpSetText(el,value,clearFirst,focusSettled){
  if(!el)throw new Error('element_not_found');if(el.disabled===true||el.getAttribute('aria-disabled')==='true')throw new Error('element_disabled');if(el.readOnly===true||el.getAttribute('readonly')!==null)throw new Error('element_readonly');var wasActive=false;try{wasActive=(el.ownerDocument||document).activeElement===el;}catch(e){}__mcpScrollIntoView(el);try{el.focus({preventScroll:true});}catch(e){try{el.focus();}catch(_){}}
  value=String(value==null?'':value);var s=__mcpState(),tag=String(el.tagName||'').toLowerCase(),editable=(tag==='input'||tag==='textarea'||tag==='select');
  var rich=!editable&&(el.isContentEditable||['textbox','searchbox'].indexOf(String(el.getAttribute('role')||'').toLowerCase())>=0);
  if(rich&&el.isContentEditable){
    var expected=clearFirst===false?__mcpEditableText(el)+value:value,start=__mcpEditableText(el);
    /* Once one path is accepted no other runs, so text is never entered twice. Editors that
       apply input to their model first (Lexical) render it a moment later: "_async" asks the
       server to confirm the text before reporting success. */
    var settle=function(method){var now=__mcpEditableText(el);s.lastInputMethod=__mcpTextMatches(now,expected)?method:(now===start?method+'_async':'input_partial');return el;};
    /* Replacing text in a field that was not focused: focusing it makes editors such as Draft update their
       state and ignore selection changes until they re-render, so the text would land at the old caret.
       Focus and select now; the server runs the same action again once the editor has settled. Editors
       that read the selection a task later (Slate) need this early selection. It is made backward because
       React drops a select event identical to the last one it saw, and the busy editor (Draft) ignores
       this one, so the forward select-all of the second run must look new or Draft pastes at the old caret. */
    if(!wasActive&&!focusSettled){try{__mcpSelectEditable(el,clearFirst,true);}catch(e){}s.lastInputMethod='focus_settling';return el;}
    var selected=true;try{__mcpSelectEditable(el,clearFirst);}catch(e){selected=false;}
    if(selected&&value&&__mcpPasteInsert(el,value))return settle('paste_event');
    if(selected&&__mcpEditableText(el)===start&&__mcpExecInsert(el,value,clearFirst))return settle('exec_command');
    if(__mcpEditableText(el)!==start){s.lastInputMethod='input_partial';return el;}
  }
  s.lastInputMethod=rich?'dom_text':'value_setter';
  if(!rich){var before=__mcpInputEvent(el,'beforeinput',value,'insertText',true);if(before)try{el.dispatchEvent(before);}catch(e){}}
  /* clear=false appends, as it does in rich editors. Only text-like fields can hold appended text. */
  if(editable&&clearFirst===false&&!__mcpAppendable(el))throw new Error('append_unsupported: clear=false needs a text field; this '+(tag==='input'?String(el.type||'text').toLowerCase()+' input':tag)+' can only be replaced');
  if(editable){var keep=clearFirst===false?String(el.value==null?'':el.value):'';if(clearFirst!==false)__mcpNativeValueSetter(el,'');__mcpNativeValueSetter(el,keep+value);}else if(rich){try{el.textContent=value;}catch(e){}}else{if(!__mcpNativeValueSetter(el,value))try{el.textContent=value;}catch(e){}}
  var input=__mcpInputEvent(el,'input',value,'insertText',false);if(input)try{el.dispatchEvent(input);}catch(e){}try{el.dispatchEvent(new (__mcpOwnerWindow(el).Event)('change',{bubbles:true,composed:true}));}catch(e){}try{var ku=new (__mcpOwnerWindow(el).KeyboardEvent)('keyup',{bubbles:true,cancelable:true,composed:true,key:value.slice(-1)||'Unidentified'});el.dispatchEvent(ku);}catch(e){}return el;
}