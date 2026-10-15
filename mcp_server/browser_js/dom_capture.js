(function(){
var h2c=window.__RASTERIZER__;
var stateKey=__STATE_KEY__;
if(typeof h2c!=="function") return JSON.stringify({ok:false,error:"rasterizer_unavailable"});
var mode=__MODE__, elementId=__ELEMENT_ID__;
var agent=window.__macMcpBrowserAgent;
var target=document.documentElement;
if(mode==="element"){
  target=agent&&agent.elements&&elementId?agent.elements[elementId]:null;
  if(!target||!target.isConnected) return JSON.stringify({ok:false,error:"element_not_available",element_id:elementId});
}
var de=document.documentElement, body=document.body||de;
var fullW=Math.max(Number(de.scrollWidth||0),Number(de.clientWidth||0),Number(body.scrollWidth||0),Number(body.clientWidth||0),Number(innerWidth||0));
var fullH=Math.max(Number(de.scrollHeight||0),Number(de.clientHeight||0),Number(body.scrollHeight||0),Number(body.clientHeight||0),Number(innerHeight||0));
var rect=mode==="element"?target.getBoundingClientRect():null;
var rawW=mode==="element"?Number(rect.width||0):(mode==="viewport"?Number(innerWidth||0):fullW);
var rawH=mode==="element"?Number(rect.height||0):(mode==="viewport"?Number(innerHeight||0):fullH);
if(!isFinite(rawW)||!isFinite(rawH)||rawW<=0||rawH<=0) return JSON.stringify({ok:false,error:"render_not_ready",reason_code:mode==="element"?"ELEMENT_ZERO_BOUNDS":"ZERO_CONTENT_BOUNDS",retryable:true,raw_width:rawW,raw_height:rawH});
var sourceW=Math.max(1,Math.ceil(rawW));
var actualH=Math.max(1,Math.ceil(rawH));
var sourceH=mode==="full_page"?Math.min(actualH,__MAX_CSS_HEIGHT__):actualH;
var truncated=mode==="full_page"&&actualH>sourceH;
var pixelBudget=7500000;
var maxOutputWidth=mode==="viewport"?1100:1280;
var scale=Math.min(1,maxOutputWidth/sourceW,Math.sqrt(pixelBudget/Math.max(1,sourceW*sourceH)));
scale=Math.max(0.20,scale);
var bg=getComputedStyle(de).backgroundColor;
if(!bg||bg==="rgba(0, 0, 0, 0)"||bg==="transparent") bg=getComputedStyle(body).backgroundColor;
if(!bg||bg==="rgba(0, 0, 0, 0)"||bg==="transparent") bg="#ffffff";
var started=Date.now();
window[stateKey]={status:"running",meta:{mode:mode,capture_method:"dom_rasterizer",background_safe:true,tab_activated:false,disk_write:false,source_width:sourceW,source_height:sourceH,actual_height:actualH,truncated:truncated,scale:scale}};
var opts={
  logging:false,useCORS:true,allowTaint:false,imageTimeout:mode==="full_page"?1500:700,removeContainer:true,
  foreignObjectRendering:false,backgroundColor:bg,scale:scale,
  windowWidth:innerWidth,windowHeight:innerHeight,
  scrollX:mode==="full_page"?0:window.scrollX,
  scrollY:mode==="full_page"?0:window.scrollY,
  ignoreElements:function(el){
    /* The Visual Companion overlay is Mac MCP UI, not page content: its full-screen host
       came out as a gray box over the page and its badge and frame in every capture. */
    if(el.id==="mac-mcp-visual-companion-root") return true;
    if(mode!=="viewport") return false;
    try{
      var r=el.getBoundingClientRect();
      return r.bottom < -120 || r.top > innerHeight+120 || r.right < -120 || r.left > innerWidth+120;
    }catch(e){return false;}
  },
  onclone:function(doc){
    try{
      var st=doc.createElement("style");
      st.textContent="*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important;}";
      (doc.head||doc.documentElement).appendChild(st);
    }catch(e){}
  }
};
if(mode==="viewport"){opts.x=window.scrollX;opts.y=window.scrollY;opts.width=sourceW;opts.height=sourceH;}
if(mode==="full_page"){opts.x=0;opts.y=0;opts.width=sourceW;opts.height=sourceH;}
h2c(target,opts).then(function(canvas){
  try{
    var output=canvas;
    var data=output.toDataURL("image/jpeg",0.58);
    var limit=__MAX_DATA_URL_CHARS__;
    if(data.length>limit&&output.width>320&&output.height>240){
      var factor=Math.max(0.35,Math.min(0.92,Math.sqrt(limit/data.length)*0.90));
      var resized=document.createElement("canvas");
      resized.width=Math.max(1,Math.round(output.width*factor));
      resized.height=Math.max(1,Math.round(output.height*factor));
      var ctx=resized.getContext("2d",{alpha:false});
      ctx.fillStyle=bg;ctx.fillRect(0,0,resized.width,resized.height);
      ctx.drawImage(output,0,0,resized.width,resized.height);
      output=resized;
      data=output.toDataURL("image/jpeg",0.52);
    }
    var meta=window[stateKey].meta;
    meta.output_width=output.width;meta.output_height=output.height;meta.elapsed_ms=Date.now()-started;meta.data_url_chars=data.length;
    window[stateKey]={status:"done",meta:meta,data:data};
  }catch(e){window[stateKey]={status:"error",error:String(e&&e.message||e)};}
}).catch(function(e){window[stateKey]={status:"error",error:String(e&&e.message||e)};});
return JSON.stringify({ok:true,status:"running",mode:mode,source_width:sourceW,source_height:sourceH,scale:scale,truncated:truncated});
})()