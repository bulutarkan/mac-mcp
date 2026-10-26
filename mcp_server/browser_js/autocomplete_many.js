(function(){
function __mcpB64(value){return btoa(unescape(encodeURIComponent(JSON.stringify(value))));}
__BOOTSTRAP__
var store=__mcpState();if(!store.autocompleteRuns)store.autocompleteRuns=Object.create(null);
var token="am_"+Date.now().toString(36)+"_"+Math.random().toString(36).slice(2,9);
var spec=__SPEC__, started=Date.now(), completed=[], items=[];
var values=spec.values||[], timeout=Math.min(5000,Math.max(100,Number(spec.timeout_ms)||2200));
function norm(v){return String(v||'').normalize('NFKC').trim().replace(/\s+/g,' ').toLocaleLowerCase();}
function matches(sel){try{return Array.from(document.querySelectorAll(sel));}catch(e){return [];}}
function visible(e){return e&&e.isConnected&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';}
function label(e){return String(e.innerText||e.textContent||e.getAttribute('aria-label')||'').trim();}
function selectedLabel(e){var aria=String(e.getAttribute('aria-label')||'').trim();if(/^Remove\s+/i.test(aria))return aria.replace(/^Remove\s+/i,'');return label(e);}
function suggestionLabel(e){return label(e).replace(/\s+\(\d+(?:[.,]\d+)?[KMB]?\)$/i,'');}
function chosen(v){return matches(spec.selected_selector).filter(function(e){return norm(selectedLabel(e))===norm(v);});}
function input(){var a=matches(spec.selector).filter(visible);return a.length===1?a[0]:null;}
function opts(el){
  var ids=String(el.getAttribute('aria-controls')||el.getAttribute('aria-owns')||'').split(/\s+/).filter(Boolean);
  var scopes=ids.map(function(id){return document.getElementById(id);}).filter(Boolean);
  var explicit=String(spec.option_selector||'').trim();
  if(!scopes.length&&!explicit)return {error:'listbox_unscoped',rows:[]};
  var rows=[];
  try{
    if(scopes.length){scopes.forEach(function(scope){rows.push(...Array.from(scope.querySelectorAll(explicit||'[role="option"]')));});}
    else rows=matches(explicit);
  }catch(e){return {error:'invalid_option_selector',rows:[]};}
  return {rows:Array.from(new Set(rows)).filter(visible).filter(function(e){return e.getAttribute('aria-disabled')!=='true';})};
}
function waitFor(fn,ms){return new Promise(function(resolve){
  var done=false,observer=null,timer=null,deadline=Date.now()+ms;
  function finish(v){if(done)return;done=true;if(observer)observer.disconnect();clearTimeout(timer);resolve(v);}
  function check(){if(done)return;var value=fn();if(value)return finish(value);if(Date.now()>=deadline)return finish(null);}
  observer=new MutationObserver(check);observer.observe(document.documentElement,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['aria-expanded','aria-controls','class','style']});
  timer=setTimeout(function(){finish(null);},ms);check();
});}
function emit(el,name){el.dispatchEvent(new Event(name,{bubbles:true,composed:true}));}
function typeInto(el,text){
  if(!('value' in el))throw Error('input_not_editable');
  el.focus();var setter=Object.getOwnPropertyDescriptor(el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value');
  if(!setter||!setter.set)throw Error('input_setter_missing');
  setter.set.call(el,text);emit(el,'input');emit(el,'change');
}
async function run(){
for(var i=0;i<values.length;i++){
  if(record.cancelled){items.push({value:values[i],status:'ambiguous',reason:'cancelled'});break;}
  var value=values[i],el=input();
  if(!el){items.push({value:value,status:'failed',reason:'input_not_unique_or_missing'});break;}
  if(chosen(value).length===1){items.push({value:value,status:'completed',already_selected:true});completed.push(value);continue;}
  if(chosen(value).length>1){items.push({value:value,status:'ambiguous',reason:'duplicate_selected_tags'});break;}
  try{typeInto(el,value);}catch(e){items.push({value:value,status:'failed',reason:String(e.message||e)});break;}
  var state=await waitFor(function(){
    var latest=input();if(!latest)return {error:'input_disappeared'};
    var all=opts(latest);if(all.error)return all;
    var found=all.rows.filter(function(e){return norm(suggestionLabel(e))===norm(value);});
    if(found.length>1)return {error:'ambiguous_option'};
    return found.length===1?{option:found[0]}:null;
  },timeout);
  if(!state||state.error){items.push({value:value,status:state&&state.error==='ambiguous_option'?'ambiguous':'failed',reason:state?state.error:'option_timeout'});break;}
  if(record.cancelled){items.push({value:value,status:'ambiguous',reason:'cancelled'});break;}
  var option=state.option;
  if(!option.isConnected||!visible(option)||norm(suggestionLabel(option))!==norm(value)){
    items.push({value:value,status:'ambiguous',reason:'option_changed_before_click'});break;
  }
  try{option.click();}catch(e){items.push({value:value,status:'ambiguous',reason:'click_exception'});break;}
  var committed=await waitFor(function(){return chosen(value).length===1?true:null;},timeout);
  if(!committed){items.push({value:value,status:'ambiguous',reason:'selection_not_verified'});break;}
  items.push({value:value,status:'completed'});completed.push(value);
}
var failed=items.find(function(e){return e.status!=='completed';});
return {ok:!failed&&completed.length===values.length,verification:'selected_tags_verified',
 completed:completed,items:items,remaining:values.slice(items.length),
 error:failed?(failed.status==='ambiguous'?'autocomplete_ambiguous':'autocomplete_incomplete'):null,
 outcome_unknown:!!failed&&failed.status==='ambiguous',automatic_retry:false,
 duration_ms:Date.now()-started};
}
var record={pending:true,result:null,cancelled:false};store.autocompleteRuns[token]=record;
var cleanupTimer=setTimeout(function(){try{if(store.autocompleteRuns[token]===record)delete store.autocompleteRuns[token];}catch(e){}},90000);
run().then(function(value){record.pending=false;record.result=value;clearTimeout(cleanupTimer);setTimeout(function(){delete store.autocompleteRuns[token];},10000);},function(error){record.pending=false;clearTimeout(cleanupTimer);setTimeout(function(){delete store.autocompleteRuns[token];},10000);record.result={ok:false,error:'autocomplete_exception',reason:String(error&&error.message||error),completed:completed,items:items,remaining:values.slice(items.length),outcome_unknown:true,automatic_retry:false};});
return __mcpB64({ok:true,pending:true,run_token:token});
})()
