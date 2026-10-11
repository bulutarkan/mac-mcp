(function(){
__BOOTSTRAP__
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
var spec=__SPEC__,s=__mcpState();
if(!s.eventWaiters)s.eventWaiters=Object.create(null);
var token='bw_'+s.pageToken+'_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2,7);
var w={token:token,done:false,result:null,event_count:0,fallback_ticks:0,started_at:Date.now(),last_signal_at:Date.now(),network_active:0,cleanup:[]};
s.eventWaiters[token]=w;
function compact(){
  return {ok:true,type:'wait',for:spec.kind,matched:false,pending:true,wait_token:token,
    url:location.href,title:document.title,dom_revision:s.mutationRevision,
    event_count:w.event_count,fallback_ticks:w.fallback_ticks};
}
function stateResult(matched,settledBy,timedOut){
  return {ok:true,type:'wait',for:spec.kind,matched:!!matched,timed_out:!!timedOut,pending:false,
    wait_token:token,wait_strategy:'event_driven',settled_by:settledBy||null,
    duration_ms:Math.max(0,Date.now()-w.started_at),url:location.href,title:document.title,
    dom_revision:s.mutationRevision,event_count:w.event_count,fallback_ticks:w.fallback_ticks,
    network_active:w.network_active};
}
function cleanup(){
  var rows=w.cleanup.splice(0,w.cleanup.length);
  for(var i=0;i<rows.length;i++){try{rows[i]();}catch(e){}}
}
function finish(matched,settledBy,timedOut){
  if(w.done)return;
  w.done=true;w.result=stateResult(matched,settledBy,timedOut);cleanup();
  if(typeof w.resolve==='function'){try{w.resolve(__mcpB64(w.result));}catch(e){}}
  setTimeout(function(){try{if(s.eventWaiters[token]===w)delete s.eventWaiters[token];}catch(e){}},10000);
}
function norm(v){return String(v||'').normalize('NFKD').toLowerCase().replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9çğıöşü]+/g,' ').replace(/\s+/g,' ').trim();}
function semanticHit(actual,wanted){
  var a=norm(actual),w=norm(wanted);if(!w)return true;if(!a)return false;
  if(a===w||a.indexOf(w+' ')===0||a.indexOf(w)>=0)return true;
  var at=a.split(' '),wt=w.split(' ').filter(Boolean);
  return wt.length>0&&wt.every(function(t){return at.indexOf(t)>=0;});
}
function condition(){
  try{
    if(spec.kind==='selector')return !!__mcpQueryOne(spec.selector);
    if(spec.kind==='text')return !!(document.body&&String(document.body.innerText||'').toLowerCase().indexOf(spec.text)>=0);
    if(spec.kind==='element_removed'){var e=s.elements[spec.element_id];return !e||!e.isConnected;}
    if(spec.kind==='url_change')return location.href!==spec.initial_url;
    if(spec.kind==='semantic'){
      var modal=__mcpTopBlockingModal(),all=__mcpQueryAll('*'),limit=Math.min(all.length,6000);
      for(var si=0;si<limit;si++){
        var el=all[si];if(!__mcpSemanticVisible(el))continue;
        if(modal&&el!==modal&&!__mcpComposedContains(modal,el))continue;
        var d=__mcpDescribe(el,s);
        if(spec.actionable_only&&!d.actionable)continue;
        if(spec.role&&norm(d.role)!==norm(spec.role))continue;
        var fields=[d.text,d.aria_label,d.placeholder,d.name,d.title,d.value,d.context,d.association_text].filter(Boolean);
        var joined=fields.join(' ');
        if(spec.text&&!semanticHit(joined,spec.text))continue;
        if(spec.query&&!semanticHit(joined,spec.query))continue;
        return true;
      }
      return false;
    }
    if(spec.kind==='dom_stable')return (Date.now()-w.last_signal_at)>=spec.stable_ms;
    if(spec.kind==='network_idle'){
      var body='';try{body=String((document.body&&document.body.innerText)||'').trim();}catch(e){}
      return location.href!=='about:blank'&&document.readyState==='complete'&&body.length>0&&
        w.network_active===0&&(Date.now()-w.last_signal_at)>=spec.stable_ms;
    }
  }catch(e){}
  return false;
}
var settleTimer=null;
function scheduleStable(){
  if(spec.kind!=='dom_stable'&&spec.kind!=='network_idle')return;
  if(settleTimer)clearTimeout(settleTimer);
  var delay=Math.max(1,spec.stable_ms-(Date.now()-w.last_signal_at));
  settleTimer=setTimeout(evaluate,delay);
}
function evaluate(source){
  if(w.done)return;
  if(condition()){finish(true,source||'event',false);return;}
  scheduleStable();
}
function signal(source){
  if(w.done)return;
  w.event_count+=1;w.last_signal_at=Date.now();evaluate(source||'event');
}
var roots=__mcpRoots();
for(var ri=0;ri<roots.length;ri++){
  try{
    var ob=new MutationObserver(function(records){
      var meaningful=false;
      for(var j=0;j<records.length;j++){
        var rec=records[j];
        if(__mcpInternalMutation(rec))continue;
        meaningful=true;break;
      }
      if(meaningful){s.mutationRevision+=1;s.lastMutationAt=Date.now();signal('mutation');}
    });
    ob.observe(roots[ri],{subtree:true,childList:true,attributes:true,characterData:true});
    (function(observer){w.cleanup.push(function(){observer.disconnect();});})(ob);
  }catch(e){}
}
['load','popstate','hashchange'].forEach(function(name){
  var fn=function(){signal(name);};try{window.addEventListener(name,fn,true);w.cleanup.push(function(){window.removeEventListener(name,fn,true);});}catch(e){}
});
try{
  var rs=function(){signal('readystatechange');};document.addEventListener('readystatechange',rs,true);
  w.cleanup.push(function(){document.removeEventListener('readystatechange',rs,true);});
}catch(e){}
try{
  var opush=history.pushState,oreplace=history.replaceState;
  var pushWrap=function(){var r=opush.apply(this,arguments);signal('history');return r;};
  var replaceWrap=function(){var r=oreplace.apply(this,arguments);signal('history');return r;};
  history.pushState=pushWrap;history.replaceState=replaceWrap;
  w.cleanup.push(function(){try{if(history.pushState===pushWrap)history.pushState=opush;if(history.replaceState===replaceWrap)history.replaceState=oreplace;}catch(e){}});
}catch(e){}
if(spec.kind==='network_idle'){
  try{
    var ofetch=window.fetch;
    if(typeof ofetch==='function'){
      var fetchWrap=function(){
        w.network_active+=1;w.last_signal_at=Date.now();
        var out;
        try{out=ofetch.apply(this,arguments);}catch(err){w.network_active=Math.max(0,w.network_active-1);signal('network_error');throw err;}
        return Promise.resolve(out).then(function(v){w.network_active=Math.max(0,w.network_active-1);signal('network');return v;},
          function(err){w.network_active=Math.max(0,w.network_active-1);signal('network');throw err;});
      };
      window.fetch=fetchWrap;w.cleanup.push(function(){try{if(window.fetch===fetchWrap)window.fetch=ofetch;}catch(e){}});
    }
  }catch(e){}
  try{
    var X=window.XMLHttpRequest,osend=X&&X.prototype&&X.prototype.send;
    if(typeof osend==='function'){
      var sendWrap=function(){
        w.network_active+=1;w.last_signal_at=Date.now();
        var done=false,finishX=function(){if(done)return;done=true;w.network_active=Math.max(0,w.network_active-1);signal('network');};
        try{this.addEventListener('loadend',finishX,{once:true});}catch(e){}
        try{return osend.apply(this,arguments);}catch(err){finishX();throw err;}
      };
      X.prototype.send=sendWrap;w.cleanup.push(function(){try{if(X.prototype.send===sendWrap)X.prototype.send=osend;}catch(e){}});
    }
  }catch(e){}
}
var fallback=setInterval(function(){w.fallback_ticks+=1;evaluate('bounded_fallback');},Math.max(250,spec.fallback_ms));
w.cleanup.push(function(){clearInterval(fallback);});
var deadline=setTimeout(function(){finish(false,'timeout',true);},spec.timeout_ms);
w.cleanup.push(function(){clearTimeout(deadline);if(settleTimer)clearTimeout(settleTimer);});
evaluate('initial');
if(spec.return_mode==='token')return __mcpB64(compact());
return new Promise(function(resolve){w.resolve=resolve;if(w.done)resolve(__mcpB64(w.result));});
})()