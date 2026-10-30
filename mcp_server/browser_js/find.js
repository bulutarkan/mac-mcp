(function(){
__BOOTSTRAP__
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
function norm(v){return String(v||'').normalize('NFKD').toLowerCase().replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9çğıöşü]+/g,' ').replace(/\s+/g,' ').trim();}
var offscreen=new Set();
function rendered(el){
  if(!el||el.nodeType!==1) return false;
  if(__mcpSemanticVisible(el)) return true;
  var st=__mcpStyle(el); if(st.display==='none'||st.visibility==='hidden'||parseFloat(st.opacity||'1')===0) return false;
  var r=__mcpTopRect(el); if(r.width<1||r.height<1) return false;
  if(r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth) return false;
  var cur=__mcpParent(el),g=0;while(cur&&g++<20){var cs=__mcpStyle(cur);if(cs.display==='none'||cs.visibility==='hidden'||String(cur.getAttribute&&cur.getAttribute('aria-hidden')||'').toLowerCase()==='true')return false;cur=__mcpParent(cur);}
  offscreen.add(el); return true;
}
function level(actual,wanted){
  var a=norm(actual),w=norm(wanted); if(!a||!w)return 0;
  if(a===w)return 5;
  if(a.indexOf(w+' ')===0||a.indexOf(w+'-')===0)return 4;
  var at=a.split(' '),wt=w.split(' '); if(wt.length&&wt.every(function(t){return at.indexOf(t)>=0;}))return 3;
  if(w.length>=4&&a.indexOf(w)>=0)return 2;
  return 0;
}
__mcpSettleHidden();var s=__mcpState(), q=__QUERY__, wantedRole=__ROLE__, wantedText=__TEXT__, actionableOnly=__ACTIONABLE_ONLY__;
__mcpVisual('Finding',null,'',1600);
var out=[],els=[],modal=__mcpTopBlockingModal();
var all=__mcpQueryAll('*');
for(var i=0;i<all.length&&out.length<__COLLECT_LIMIT__;i++){
  var el=all[i]; if(!rendered(el))continue;
  if(modal&&el!==modal&&!__mcpComposedContains(modal,el))continue;
  var actionable=__mcpActionable(el);
  if(actionableOnly && !actionable)continue;
  if(wantedRole&&norm(__mcpRole(el))!==wantedRole)continue;
  /* Matched on its names first; the full describe (layout, readiness) only for a match (#394). */
  var d=__mcpNameFields(el);
  var fields=[d.text||'',d.aria_label||'',d.placeholder||'',d.name||'',d.title||'',d.value||'',d.context||'',d.association_text||''];
  fields.push(fields.filter(Boolean).join(' '));
  if(wantedText){var tl=0;fields.forEach(function(v){tl=Math.max(tl,level(v,wantedText));});if(!tl)continue;}
  if(q){
    var ql=0;fields.forEach(function(v){ql=Math.max(ql,level(v,q));});
    if(d.options){d.options.forEach(function(o){ql=Math.max(ql,level(o.text||'',q),level(o.value||'',q));});}
    if(!ql)continue;
  }
  d=__mcpDescribe(el,s); d.actionable=actionable; if(offscreen.has(el))d.offscreen=true;
  out.push(d);els.push(el);
}
function contains(a,b){try{if(a.contains(b))return true;}catch(e){}return __mcpComposedContains(a,b);}
var withinInfo=null;
__WITHIN_BLOCK__
if(out.length>__MAX_CANDIDATES__){out=out.slice(0,__MAX_CANDIDATES__);els=els.slice(0,__MAX_CANDIDATES__);}
for(var fi=0;fi<els.length;fi++)__mcpRememberTarget(els[fi],out[fi].element_id,s);
for(var ci=0;ci<els.length;ci++){
  var nested=[];
  for(var cj=0;cj<els.length&&nested.length<12;cj++){if(ci!==cj&&els[ci]!==els[cj]&&contains(els[ci],els[cj]))nested.push(out[cj].element_id);}
  if(nested.length)out[ci].nested_match_ids=nested;
}
var obs='bobs_'+s.pageToken+'_'+Date.now().toString(36);s.observations[obs]=s.mutationRevision;
return __mcpB64({ok:true,observation_id:obs,dom_revision:s.mutationRevision,url:location.href,title:document.title,modal_scope:modal?{active:true,element_id:__mcpId(modal,s),role:__mcpRole(modal),text:__mcpText(modal).slice(0,120)}:{active:false},within:withinInfo,elements:out});
})()