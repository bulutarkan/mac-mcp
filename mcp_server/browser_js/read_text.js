(function(){
__BOOTSTRAP__
/* mcp-read-text: the page's readable text as the browser lays it out, in one piece or in parts. */
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
try{__mcpVisual('Reading',null,'',1200);}catch(e){}
var opts=__READ_OPTIONS__;
var root=document.body||document.documentElement, raw='';
// innerText follows CSS: no script, style, template or hidden content, and block breaks as lines.
try{raw=String(root.innerText||'');}catch(e){raw=String(root.textContent||'');}
try{var host=document.getElementById('mac-mcp-visual-companion-root');var own=host?String(host.innerText||''):'';if(own.trim())raw=raw.split(own).join('');}catch(e){}
var lines=raw.replace(/\r/g,'').split('\n'),out=[],prev=null,blank=false;
for(var i=0;i<lines.length;i++){
  var t=lines[i].replace(/[ \t ​]+/g,' ').trim();
  if(!t){if(out.length&&!blank){out.push('');blank=true;}continue;}
  if(t===prev)continue;
  out.push(t);prev=t;blank=false;
}
var text=out.join('\n').trim(),total=text.length;
var offset=Math.max(0,Math.min(Number(opts.offset)||0,total)),cap=Math.max(1,Number(opts.max_chars)||40000);
var end=Math.min(total,offset+cap);
if(end<total){
  // End at a line, else a word, so no word is split between two parts.
  var cut=text.lastIndexOf('\n',end);
  if(cut<=offset+cap/2)cut=text.lastIndexOf(' ',end);
  if(cut>offset)end=cut;
  var code=text.charCodeAt(end-1);if(code>=0xD800&&code<=0xDBFF)end--;
}
var headings=[];
try{var hs=document.querySelectorAll('h1,h2,h3');for(var h=0;h<hs.length&&headings.length<40;h++){var ht=String(hs[h].innerText||'').replace(/\s+/g,' ').trim();if(ht)headings.push(ht.slice(0,200));}}catch(e){}
var piece=text.slice(offset,end);
return __mcpB64({ok:true,type:'read',url:location.href,title:document.title,text:piece,offset:offset,
  returned_chars:piece.length,total_chars:total,next_offset:end<total?end:null,complete:end>=total,
  headings:headings,coverage:'rendered_page_text'});
})()
