(function(){
/* mcp-read-scroll: move through the page so content that loads on scroll appears. */
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
var opts=__READ_OPTIONS__;
var doc=document.scrollingElement||document.documentElement, view=innerHeight||doc.clientHeight||800;
// The page scrolls itself, or (an app shell) its largest scrolling element does.
var box=doc;
if(doc.scrollHeight<=view+2){
  var best=null,bestHeight=0,all=document.querySelectorAll('body *');
  for(var i=0;i<all.length&&i<5000;i++){
    var el=all[i];
    if(el.scrollHeight>el.clientHeight+50&&el.clientHeight>100){
      var oy='';try{oy=getComputedStyle(el).overflowY;}catch(e){}
      if((oy==='auto'||oy==='scroll')&&el.scrollHeight>bestHeight){best=el;bestHeight=el.scrollHeight;}
    }
  }
  if(best){box=best;view=best.clientHeight;}
}
if(opts.phase==='restore'){box.scrollTop=opts.top||0;}
else if(opts.phase==='step'){box.scrollTop=box.scrollTop+Math.round(view*opts.factor);}
var top=box.scrollTop,height=box.scrollHeight;
return __mcpB64({ok:true,top:top,height:height,view:view,at_end:top+view>=height-2});
})()
