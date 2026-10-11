(function(){
__BOOTSTRAP__
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
var s=__mcpState();
__mcpFlushMutations(s);
__mcpStartMutationWatch(s,3000);
var expected=__OBS__;
if(expected && !(expected in s.observations)) return __mcpB64({ok:false,error:'stale_observation',observe_again:true});
var changed=expected ? (s.observations[expected]!==s.mutationRevision) : false;
var actions=__ACTIONS__;
var results=[];
function target(a){return __mcpRecoverAction(a,s);}
function emit(el,type){try{el.dispatchEvent(new (__mcpOwnerWindow(el).Event)(type,{bubbles:true,composed:true}));}catch(e){}}
function pageEffect(beforeRevision,beforeUrl,beforeTitle,beforeState,el){
  __mcpFlushMutations(s);
  var afterState=__mcpEffectState(el);
  return location.href!==beforeUrl || document.title!==beforeTitle || __mcpEffectChanged(beforeState,afterState);
}
for(var i=0;i<actions.length;i++){
  var a=actions[i]||{}, type=String(a.type||'').toLowerCase().replace(/-/g,'_');
  // A remembered target that moved to another route or now means something else
  // must not be acted on; nothing has run for this action yet.
  var fpWas=(a.element_id&&type!=='scroll'&&s.targetFp)?s.targetFp[a.element_id]:null;
  if(fpWas&&fpWas.route!==__mcpRoute()){results.push({index:i,type:type,element_id:a.element_id,ok:false,error:'stale_target',reason_code:'ROUTE_CHANGED',observe_again:true,no_side_effect:true});break;}
  __mcpSettleHidden();
  var el=a.element_id?target(a):null;
  if(a.element_id && !el){results.push({index:i,type:type,element_id:a.element_id,ok:false,error:'stale_element',observe_again:true});break;}
  if(fpWas&&el){var fpNow=__mcpTargetFp(el);if(fpNow.role!==fpWas.role||fpNow.tag!==fpWas.tag||fpNow.name!==fpWas.name){results.push({index:i,type:type,element_id:a.element_id,ok:false,error:'stale_target',reason_code:'TARGET_CHANGED',observe_again:true,no_side_effect:true});break;}}
  var visualLabel=type==='click'||type==='double_click'?'Clicking':(type==='type'||type==='type_text'||type==='paste'?'Typing':(type==='scroll'?'Scrolling':(type==='focus'?'Focusing':(type==='select'?'Selecting':'Working'))));
  var visualDetail=type==='scroll'?(el?'Into view':(Number(a.dy||300)<0?'Up':'Down')):'';
  __mcpVisual(visualLabel,el,(type==='click'||type==='double_click')?'click':'',2200,visualDetail);
  if(type==='click'||type==='double_click')s.lastActivationTrace=null;
  var beforeRevision=s.mutationRevision,beforeUrl=location.href,beforeTitle=document.title,beforeState=el?__mcpEffectState(el):null;
  try{
    if(type==='click'||type==='double_click'){
      if(!el) throw new Error('element_id is required');
      __mcpScrollIntoView(el);
      var tag=(el.tagName||'').toLowerCase(),href=String(el.getAttribute('href')||''),inputType=String(el.getAttribute('type')||'').toLowerCase();
      var mayNavigate=(tag==='a'&&href&&href!=='#'&&!href.endsWith('#'))||((tag==='button'||tag==='input')&&inputType==='submit');
      var shouldDefer=(type==='click'&&i===actions.length-1&&mayNavigate),activated=el,effectObserved=false,verification='no_immediate_effect';
      var beforeQuietMs=Math.max(0,Date.now()-Number(s.lastMutationAt||0));
      if(type==='double_click') activated=__mcpDoubleActivate(el);
      else if(shouldDefer) setTimeout(function(node){return function(){try{__mcpActivate(node);}catch(e){}};}(el),0);
      else activated=__mcpActivate(el);
      if(shouldDefer){verification='deferred_pending';}
      else{
        effectObserved=pageEffect(beforeRevision,beforeUrl,beforeTitle,beforeState,activated||el);
        if(effectObserved) verification='state_changed';
        else if(beforeQuietMs>=__QUIET_MS__&&s.mutationRevision!==beforeRevision){effectObserved=true;verification='dom_mutated';}
      }
      var clickResult={index:i,type:type,element_id:a.element_id,ok:true,deferred:shouldDefer,activation_target:activated?__mcpId(activated,s):a.element_id,effect_observed:effectObserved,verification:verification,activation_trace:s.lastActivationTrace||null,_verify_revision:beforeRevision,_verify_url:beforeUrl,_verify_title:beforeTitle,_verify_state:beforeState,_verify_quiet_ms:beforeQuietMs};
      if(!effectObserved)clickResult.observe_again=true;
      results.push(clickResult);
    } else if(type==='type'||type==='type_text'||type==='paste'){
      if(!el) throw new Error('element_id is required');
      var value=String(a.text==null?'':a.text);
      var priorText=('value' in el)?String(el.value==null?'':el.value):__mcpEditableText(el);
      __mcpSetText(el,value,a.clear!==false,a._focus_settled===true);__mcpFlushMutations(s);
      if(s.lastInputMethod==='focus_settling'){results.push({index:i,type:type,element_id:a.element_id,ok:true,effect_observed:false,verification:'focus_settling',input_method:'focus_settling'});break;}
      var hasValue=('value' in el),actual='';try{actual=hasValue?String(el.value||''):__mcpEditableText(el);}catch(e){}
      var method=s.lastInputMethod||'',expectedText=a.clear===false?priorText+value:value,pending=/_async$/.test(method);
      var applied=pending||(hasValue?actual===expectedText:(method!=='input_partial'&&__mcpTextMatches(actual,expectedText)));
      var unconfirmed=/^paste_event/.test(method)&&s.lastPasteUnconfirmed===true;
      var verification=pending?'editor_pending':(!applied?'input_not_applied':(method==='dom_text'?'dom_text_only':(unconfirmed?'editor_model_unconfirmed':'value_applied')));
      var typed={index:i,type:type,element_id:a.element_id,ok:applied,value:(String(el.type||'').toLowerCase()==='password'?(actual?'[hidden]':''):actual.slice(0,200)),effect_observed:applied&&!pending,verification:verification,input_method:method.replace(/_async$/,''),observe_again:!applied};
      if(pending)typed._verify_text=expectedText;
      if(pending&&unconfirmed)typed._model_unconfirmed=true;
      /* The page drew the pasted text but nothing shows its framework took it (see __mcpPasteInsert). */
      if(verification==='editor_model_unconfirmed'){typed.editor_state_unverified=true;typed.observe_again=true;}
      /* Text written only into the DOM may never reach the model of a rich editor (its submit stays disabled). */
      if(verification==='dom_text_only'){typed.editor_state_unverified=true;typed.observe_again=true;}
      if(!applied)typed.error='input_not_applied';results.push(typed);if(!applied)break;
    } else if(type==='select'){
      if(!el) throw new Error('element_id is required');
      var wanted=String(a.option==null?'':a.option).trim().toLowerCase(),chosen=null;
      if((el.tagName||'').toLowerCase()==='select'){
        var opts=Array.from(el.options||[]);
        chosen=opts.find(function(o){return String(o.value).toLowerCase()===wanted||String(o.text).trim().toLowerCase()===wanted;})||opts.find(function(o){return String(o.text).trim().toLowerCase().indexOf(wanted)>=0;});
        if(!chosen)throw new Error('option_not_found');
        __mcpNativeValueSetter(el,chosen.value);emit(el,'input');emit(el,'change');
      }else{
        __mcpActivate(el);
        var candidates=__mcpQueryAll('[role="option"],option,[role="menuitem"],li,button,a').filter(__mcpVisible);
        chosen=candidates.find(function(o){return __mcpText(o).toLowerCase()===wanted;})||candidates.find(function(o){return __mcpText(o).toLowerCase().indexOf(wanted)>=0;});
        if(!chosen)throw new Error('option_not_found');__mcpActivate(chosen);
      }
      __mcpFlushMutations(s);results.push({index:i,type:type,element_id:a.element_id,ok:true,selected:chosen?__mcpText(chosen):wanted,effect_observed:pageEffect(beforeRevision,beforeUrl,beforeTitle,beforeState,el)});
    } else if(type==='scroll'){
      if(el)__mcpScrollIntoView(el);else window.scrollBy(Number(a.dx||0),Number(a.dy||300));
      results.push({index:i,type:type,element_id:a.element_id||null,ok:true,effect_observed:true,verification:'scroll_applied'});
    } else if(type==='focus'){
      if(!el)throw new Error('element_id is required');el.focus();results.push({index:i,type:type,element_id:a.element_id,ok:true,effect_observed:true,verification:'focus_applied'});
    } else throw new Error('unsupported_batch_action:'+type);
  }catch(e){results.push({index:i,type:type,element_id:a.element_id||null,ok:false,error:String(e&&e.message||e)});break;}
}
__mcpFlushMutations(s);
var active=document.activeElement;
var compact={ok:true,url:location.href,title:document.title,scroll:{x:scrollX,y:scrollY},dom_revision:s.mutationRevision,active_element:active&&active.nodeType===1?__mcpDescribe(active,s):null};
return __mcpB64({ok:results.every(function(r){return r.ok;}),actions:results,dom_changed_since_observe:changed,dom_revision:s.mutationRevision,url:location.href,title:document.title,scroll:{x:scrollX,y:scrollY},state:compact});
})()