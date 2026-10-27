(function(){
__BOOTSTRAP__
/* mcp-page-snapshot: the page as it is shown now, as one HTML document, for a PDF made outside the
   browser (page_pdf.swift). Scripts are left out (the copy never runs page code); styles that
   scripts inserted, images as currently chosen, canvases and form values (never passwords) are kept.
   phase start builds it, chunk hands it over in parts, clear forgets it. */
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
var opts=__SNAPSHOT_OPTIONS__,s=__mcpState();
if(opts.phase==='chunk'){var snap=s.pdfSnapshot;if(!snap||snap.token!==opts.token)return __mcpB64({ok:false,error:'snapshot_gone'});
  return __mcpB64({ok:true,text:snap.html.substr(Number(opts.offset)||0,Number(opts.size)||20000)});}
if(opts.phase==='clear'){s.pdfSnapshot=null;return __mcpB64({ok:true});}
var root=document.documentElement.cloneNode(true);
function pairs(sel,fn){var live=document.querySelectorAll(sel),copy=root.querySelectorAll(sel);for(var i=0;i<live.length&&i<copy.length;i++){try{fn(live[i],copy[i]);}catch(e){}}}
pairs('style',function(a,b){var sh=a.sheet;if(!sh||!sh.cssRules)return;var t=Array.prototype.map.call(sh.cssRules,function(r){return r.cssText;}).join('\n');if(t.length>String(a.textContent||'').length)b.textContent=t;});
pairs('img',function(a,b){if(a.currentSrc)b.setAttribute('src',a.currentSrc);b.removeAttribute('srcset');b.removeAttribute('loading');});
pairs('canvas',function(a,b){var d=a.toDataURL('image/png');if(d.length>2000000)return;var img=document.createElement('img');img.setAttribute('src',d);img.setAttribute('style',a.getAttribute('style')||'');img.width=a.width;img.height=a.height;b.parentNode.replaceChild(img,b);});
pairs('input',function(a,b){var t=String(a.type||'').toLowerCase();if(t==='password'||t==='hidden'||t==='file')return;if(t==='checkbox'||t==='radio'){if(a.checked)b.setAttribute('checked','');else b.removeAttribute('checked');}else b.setAttribute('value',String(a.value||''));});
pairs('textarea',function(a,b){b.textContent=String(a.value||'');});
pairs('select',function(a,b){for(var i=0;i<a.options.length&&i<b.options.length;i++){if(a.options[i].selected)b.options[i].setAttribute('selected','');else b.options[i].removeAttribute('selected');}});
try{var adopted=document.adoptedStyleSheets||[],extra=[];for(var k=0;k<adopted.length;k++){try{extra.push(Array.prototype.map.call(adopted[k].cssRules,function(r){return r.cssText;}).join('\n'));}catch(e){}}
  if(extra.length){var st=document.createElement('style');st.textContent=extra.join('\n');(root.querySelector('head')||root).appendChild(st);}}catch(e){}
var drop=root.querySelectorAll('script,noscript,#mac-mcp-visual-companion-root,link[rel=preload],link[rel=prefetch],link[rel=modulepreload],link[rel=preconnect],link[rel=dns-prefetch]');
for(var d=0;d<drop.length;d++){try{drop[d].parentNode.removeChild(drop[d]);}catch(e){}}
var html=(document.doctype?'<!DOCTYPE html>\n':'')+root.outerHTML;
if(html.length>Number(opts.max_chars||4000000))return __mcpB64({ok:false,error:'page_too_large',length:html.length});
var token=Math.random().toString(36).slice(2,12);s.pdfSnapshot={token:token,html:html};
return __mcpB64({ok:true,token:token,length:html.length,url:location.href,title:document.title,width:innerWidth});
})()
