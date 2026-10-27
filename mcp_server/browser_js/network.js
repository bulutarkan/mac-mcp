(function(){
__BOOTSTRAP__
/* mcp-network: a small ring of this page's recent requests (method, URL, status, timing).
   Nothing is recorded until an agent first reads it; that first read starts from the requests the
   browser itself already lists (Resource Timing), and from then on fetch/XHR calls are recorded with
   their method and status. Never request or response bodies or headers. */
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
var opts=__NETWORK_OPTIONS__,s=__mcpState(),MAX=100;
function url(raw){var u=String(raw||'');try{u=new URL(u,location.href).href;}catch(e){}if(u.indexOf('data:')===0)return u.slice(0,u.indexOf(',')>0?Math.min(u.indexOf(','),60):60)+',…';return u.length>500?u.slice(0,500)+'…':u;}
function ms(v){return Math.round(Number(v)||0);}
function push(rec){var n=s.net;rec.seq=++n.seq;n.ring.push(rec);if(n.ring.length>MAX){n.ring.shift();n.dropped++;}return rec;}
function fromEntry(e){var kind=String(e.initiatorType||'other');var rec={kind:kind,method:(kind==='fetch'||kind==='xmlhttprequest')?null:'GET',url:url(e.name),start_ms:ms(e.startTime),duration_ms:ms(e.duration)};
  if(typeof e.responseStatus==='number'&&e.responseStatus>0)rec.status=e.responseStatus;if(e.transferSize)rec.bytes=e.transferSize;return rec;}
function arm(){
  var n=s.net={ring:[],seq:0,dropped:0,armed_at:performance.now(),wrapped:[]};
  try{var past=performance.getEntriesByType('resource')||[];for(var i=Math.max(0,past.length-MAX);i<past.length;i++)push(fromEntry(past[i]));}catch(e){}
  try{n.observer=new PerformanceObserver(function(list){list.getEntries().forEach(function(e){
    /* fetch/XHR started after arming are recorded by the wrappers with their method and status. */
    if((e.initiatorType==='fetch'||e.initiatorType==='xmlhttprequest')&&e.startTime>=n.armed_at&&n.wrapped.length)return;push(fromEntry(e));});});
    n.observer.observe({type:'resource'});}catch(e){}
  try{var origFetch=window.fetch;if(typeof origFetch==='function'){window.fetch=function(input,init){
    var rec=push({kind:'fetch',method:String((init&&init.method)||(input&&input.method)||'GET').toUpperCase(),url:url(input&&input.url?input.url:input),start_ms:ms(performance.now())}),t0=performance.now();
    var p=origFetch.apply(this,arguments);
    try{p.then(function(r){rec.status=r.status;rec.duration_ms=ms(performance.now()-t0);},function(err){rec.error=String(err&&err.name||'error');rec.duration_ms=ms(performance.now()-t0);});}catch(e){}
    return p;};n.wrapped.push('fetch');}}catch(e){}
  try{var X=window.XMLHttpRequest&&window.XMLHttpRequest.prototype,open=X&&X.open,send=X&&X.send;if(open&&send){
    X.open=function(method,u){try{this.__macMcpNet={method:String(method||'GET').toUpperCase(),url:url(u)};}catch(e){}return open.apply(this,arguments);};
    X.send=function(){try{var info=this.__macMcpNet||{},rec=push({kind:'xhr',method:info.method||'GET',url:info.url||'',start_ms:ms(performance.now())}),t0=performance.now(),xhr=this;
      xhr.addEventListener('loadend',function(){rec.status=xhr.status||undefined;if(!xhr.status)rec.error='network';rec.duration_ms=ms(performance.now()-t0);});}catch(e){}return send.apply(this,arguments);};
    n.wrapped.push('xhr');}}catch(e){}
}
var first=!s.net;if(first)arm();
var n=s.net,since=Number(opts.since)||0,filter=String(opts.url_contains||'').toLowerCase(),limit=Math.max(1,Math.min(Number(opts.max_items)||50,MAX));
var rows=n.ring.filter(function(r){return r.seq>since&&(!filter||String(r.url).toLowerCase().indexOf(filter)>=0);});
var more=rows.length>limit;if(more)rows=rows.slice(rows.length-limit);
return __mcpB64({ok:true,type:'network',url:location.href,started_now:first,recording:n.wrapped,requests:rows,count:rows.length,has_more:more,
  next_since:n.seq,dropped:n.dropped,buffer_max:MAX});
})()
