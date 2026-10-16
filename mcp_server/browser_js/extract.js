(function(){
__BOOTSTRAP__
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
__mcpVisual('Reading',null,'',1800);
var specs=__SPECS__, aliases=__ALIASES__, budget=__BUDGET__, data=Object.create(null), counts=Object.create(null), metadata=Object.create(null), truncated=false, used=0;
function readValue(el, attr){
  attr=String(attr||'text');
  if(attr==='text') return visibleText(el).trim();
  if(attr==='html') return String(el.innerHTML||'');
  if(attr==='value') return String(el.type||'').toLowerCase()==='password'?(el.value?'[hidden]':''):String(el.value==null?'':el.value);
  if(attr==='href') return String(el.href||el.getAttribute('href')||'');
  if(attr==='aria_label') return String(el.getAttribute('aria-label')||'');
  return String(el.getAttribute(attr)||'');
}
function clean(v){return String(v==null?'':v).replace(/\s+/g,' ').trim();}
function norm(v){
  return clean(v).toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/[ıİ]/g,'i').replace(/[^a-z0-9₺€$%.,:/+\- ]+/g,' ').replace(/\s+/g,' ').trim();
}
function visible(el){
  if(!el||/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(el.tagName)||el.closest('[hidden],[aria-hidden="true"],#mac-mcp-visual-companion-root')) return false;
  try{for(var p=el,depth=0;p&&depth++<128;p=__mcpFlatParent(p)||((p.ownerDocument.defaultView||{}).frameElement)){
      if(p.hasAttribute('hidden')||p.getAttribute('aria-hidden')==='true')return false;var st=__mcpStyle(p);if(st.display==='none'||st.visibility==='hidden'||st.visibility==='collapse'||Number(st.opacity||1)===0)return false;}
    if(__mcpStyle(el).display==='contents')return true;var r=el.getBoundingClientRect();return r.width>0&&r.height>0;}catch(e){return false;}
}
function visibleText(el){
  // innerText on a hidden node falls back to textContent. Read only rendered text nodes;
  // keep offscreen content, excluding scripts/templates and hidden descendants.
  var walker=el.ownerDocument.createTreeWalker(el,4), chunks=[], node;
  while((node=walker.nextNode()))if(visible(node.parentElement)) chunks.push(node.nodeValue);
  return chunks.join(' ').replace(/\s+/g,' ').trim();
}
function termHit(text, term){
  var t=norm(term); if(!t) return 0;
  if(text===t) return 180;
  if(text.indexOf(t)>=0) return 105+Math.min(35,t.length);
  var words=t.split(' ').filter(Boolean), hits=0;
  for(var i=0;i<words.length;i++) if(words[i].length>1&&text.indexOf(words[i])>=0) hits++;
  return hits&&hits===words.length?70+hits*5:0;
}
function semanticTerms(sp,name){
  var target=String(sp.semantic||name||''), key=norm(target), out=[target];
  Object.keys(aliases).forEach(function(aliasKey){
    var nk=norm(aliasKey);
    if(key===nk||key.indexOf(nk)>=0||nk.indexOf(key)>=0) out=out.concat(aliases[aliasKey]||[]);
  });
  if(Array.isArray(sp.terms)) out=out.concat(sp.terms);
  var seen={}, unique=[];
  out.forEach(function(v){var n=norm(v);if(n&&!seen[n]){seen[n]=true;unique.push(v);}});
  return unique;
}
var semanticCache=null;
function semanticCandidates(){
  if(semanticCache!==null) return semanticCache;
  var nodes=[]; semanticCache=[];
  try{nodes=__mcpQueryAll('h1,h2,h3,h4,h5,h6,p,li,dt,dd,label,button,a,span,strong,b,small,div');}catch(e){return semanticCache;}
  if(nodes.length>6000) nodes=nodes.slice(0,6000);
  for(var i=0;i<nodes.length;i++){
    var el=nodes[i]; if(!visible(el)) continue;
    var aria=clean(el.getAttribute&&el.getAttribute('aria-label')||''), title=clean(el.getAttribute&&el.getAttribute('title')||'');
    var raw=clean(visibleText(el)||aria||title||''); if(!raw||raw.length>520) continue;
    var childText=0, href='', itemId='';
    try{for(var c=0;c<el.children.length;c++) if(clean(visibleText(el.children[c]))) childText++;}catch(e){}
    try{href=String(el.href||el.getAttribute('href')||'');itemId=String(el.getAttribute('data-item-id')||'');}catch(e){}
    semanticCache.push({el:el,raw:raw,text:norm(raw),childText:childText,tag:String(el.tagName||'').toLowerCase(),href:href,aria:aria,title:title,itemId:itemId});
  }
  return semanticCache;
}
function semanticValues(sp,name,maxItems){
  var semantic=norm(sp.semantic||name), terms=semanticTerms(sp,name), candidates=semanticCandidates();
  var ranked=[], seen={};
  for(var i=0;i<candidates.length;i++){
    var candidate=candidates[i], el=candidate.el, raw=candidate.raw, text=candidate.text, score=0;
    for(var j=0;j<terms.length;j++) score=Math.max(score,termHit(text,terms[j]));
    if(semantic.indexOf('price')>=0||semantic.indexOf('fiyat')>=0){
      if(/[₺€$]|\b(?:tl|try|eur|usd)\b/i.test(raw)&&/\d/.test(raw)) score=Math.max(score,150);
    }
    if(semantic.indexOf('rating')>=0||semantic.indexOf('score')>=0||semantic.indexOf('puan')>=0){
      if(/^\s*(?:[0-9](?:[.,][0-9])?|10(?:[.,]0)?)\s*(?:\/\s*(?:5|10))?\s*$/.test(raw)) score=Math.max(score,135);
    }
    if(semantic.indexOf('hours')>=0||semantic.indexOf('opening')>=0||semantic.indexOf('calisma saat')>=0||semantic.indexOf('çalışma saat')>=0){
      if(/(?:open|closed|closes|opens|açık|acik|kapalı|kapali|kapanış saati|kapanis saati|çalışma saatleri|calisma saatleri)/i.test(raw)) score=Math.max(score,175);
      if(/\b(?:[01]?\d|2[0-3])[:.]?[0-5]\d\b/.test(raw)&&/(?:open|closed|açık|acik|kapalı|kapali|kapan|saat)/i.test(raw)) score+=45;
    }
    if(semantic.indexOf('address')>=0||semantic.indexOf('location')>=0||semantic.indexOf('adres')>=0||semantic.indexOf('konum')>=0){
      var addressMeta=[candidate.itemId,candidate.aria,candidate.title].join(' ');
      var structuralAddress=/(?:^|[^a-z])address(?:$|[^a-z])|(?:^|[^a-z])adres(?:$|[^a-z])/i.test(addressMeta);
      if(structuralAddress) score=Math.max(score,260);
      if(/^(?:adres|address)\s*:/i.test(candidate.aria||'')) score=Math.max(score,280);
      if(/(?:\b(?:cad(?:desi)?|cd\.?|sok(?:ak)?|sk\.?|bulv(?:arı|ari)?|blv\.?|mah(?:allesi)?|apt\.?|street|st\.?|road|rd\.?|avenue|ave\.?|boulevard|blvd\.?)\b|\bno[:.]?\s*\d)/i.test(raw)) score=Math.max(score,170);
      if(/street view|sokak görünümü|sokak gorunumu/i.test(raw)) score-=260;
      if(raw.length>180&&!structuralAddress) score-=140;
    }
    if(semantic.indexOf('website')>=0||semantic.indexOf('web site')>=0||semantic.indexOf('homepage')>=0){
      var websiteHint=/website|web sitesi|web site|official website|official site|resmi site|homepage|authority/i.test([raw,candidate.aria,candidate.title,candidate.itemId].join(' '));
      if(websiteHint&&/^https?:\/\//i.test(candidate.href||'')) score=Math.max(score,220);
      else if(websiteHint) score=Math.max(score,165);
    }
    if(semantic.indexOf('price')>=0||semantic.indexOf('fiyat')>=0){
      if(/maxipuan|puan kazan|kampanya|\bindirim\b/i.test(raw)) score-=90;
      if(/^\s*[0-9][0-9., ]*\s*(?:tl|try|₺|eur|€|usd|\$)\s*$/i.test(raw)) score+=85;
    }
    if(semantic.indexOf('cancellation')>=0||semantic.indexOf('iptal')>=0){
      if(/ücretsiz iptal|ucretsiz iptal|free cancellation|iptal edilemez|non[- ]?refundable|iade edilemez/i.test(raw)) score+=110;
      if(/paketi|garantisi|fiyat farkı|fiyat farki/i.test(raw)) score-=80;
    }
    if(semantic.indexOf('payment')>=0||semantic.indexOf('odeme')>=0){
      if(/otele ödeme|otele odeme|otelde ödeme|otelde odeme|tesiste ödeme|tesiste odeme|pay at property|pay later|prepayment|ön ödeme|on odeme/i.test(raw)) score+=120;
    }
    if(semantic.indexOf('parking')>=0||semantic.indexOf('otopark')>=0){
      if(/otoparka sahip değildir|otoparka sahip degildir|otopark yok|otopark var|ücretsiz otopark|ucretsiz otopark|free parking|parking available|no parking/i.test(raw)) score+=100;
      if(/\byorum\b|\bkahvalt/i.test(raw)&&raw.length>180) score-=50;
    }
    if(semantic.indexOf('breakfast')>=0||semantic.indexOf('kahvalti')>=0){
      if(/kahvaltı dahil|kahvalti dahil|breakfast included/i.test(raw)) score+=120;
      if(raw.length>160) score-=70;
    }
    if(semantic.indexOf('rating')>=0||semantic.indexOf('score')>=0||semantic.indexOf('puan')>=0){
      if(/^\s*[0-9]+\s*$/.test(raw)) score-=120;
      if(/^\s*(?:[0-9][.,][0-9]|10[.,]0)\s*$/.test(raw)) score+=120;
    }
    if(score<=0) continue;
    if(candidate.childText===0) score+=18;
    if(raw.length<=80) score+=20; else if(raw.length<=180) score+=10;
    if(/^(p|li|dt|dd|label|span|strong|b|small|h[1-6])$/.test(candidate.tag)) score+=8;
    var snippet=raw;
    if((semantic.indexOf('address')>=0||semantic.indexOf('location')>=0||semantic.indexOf('adres')>=0||semantic.indexOf('konum')>=0)&&/^(?:adres|address)\s*:/i.test(candidate.aria||'')){
      snippet=String(candidate.aria||'').replace(/^(?:adres|address)\s*:\s*/i,'').trim();
    }
    if((semantic.indexOf('website')>=0||semantic.indexOf('web site')>=0||semantic.indexOf('homepage')>=0)&&candidate.href){
      try{
        var websiteUrl=new URL(candidate.href,location.href);
        if(/(^|\.)google\./i.test(websiteUrl.hostname)&&websiteUrl.pathname==='/url'){
          snippet=websiteUrl.searchParams.get('q')||websiteUrl.searchParams.get('url')||websiteUrl.href;
        }else snippet=websiteUrl.href;
      }catch(e){snippet=candidate.href;}
    }
    if(raw.length<=80&&terms.some(function(term){return norm(raw)===norm(term);})){
      var parent=el.parentElement, parentText=parent?clean(visibleText(parent)):'';
      if(parentText&&parentText!==raw&&parentText.length<=240) snippet=parentText;
    }
    var sig=norm(snippet); if(!sig||seen[sig]) continue; seen[sig]=true;
    ranked.push({score:score,text:snippet});
  }
  ranked.sort(function(a,b){return b.score-a.score||a.text.length-b.text.length;});
  var values=[], valueSeen={};
  for(var k=0;k<ranked.length&&values.length<maxItems;k++){
    var sig=norm(ranked[k].text); if(valueSeen[sig]) continue; valueSeen[sig]=true; values.push(ranked[k].text);
  }
  return values;
}
for(var i=0;i<specs.length;i++){
  var sp=specs[i],name=sp&&sp.name;
  if(!sp||typeof sp!=='object'||Array.isArray(sp)||!name||(!sp.selector&&!sp.semantic))
    return __mcpB64({ok:false,type:'extract',error:'invalid_extract_fields'});
  var maxItems=Math.max(1,Math.min(Number(sp.max_items||10),100)), vals=[],matched=0;
  if(sp.semantic){
    vals=semanticValues(sp,name,6000);matched=vals.length;
  }else{
    // Validate before the deep-query helper, which deliberately tolerates inaccessible roots.
    try{document.querySelector(sp.selector);}catch(e){return __mcpB64({ok:false,type:'extract',error:'invalid_selector',field:name});}
    var els=__mcpQueryAll(sp.selector);matched=els.length;
    vals=els.filter(visible).map(function(el){return readValue(el,sp.attr);});
  }
  if(sp.regex){
    try{var re=new RegExp(String(sp.regex),String(sp.flags||''));
      vals=vals.map(function(v){re.lastIndex=0;var m=String(v).match(re);return m?(m[1]!==undefined?m[1]:m[0]):null;}).filter(function(v){return v!==null;});
    }catch(e){return __mcpB64({ok:false,type:'extract',error:'invalid_regex',field:name});}
  }
  counts[name]=matched;
  var offset=Number(sp.offset||0),textOffset=Number(sp.text_offset||0),start=offset,out=[];
  var cap=sp.all?maxItems:1,remaining;
  while(offset<vals.length&&out.length<cap&&(remaining=budget-used)>0){
    var full=String(vals[offset]),tail=full.slice(textOffset),chunk=tail.slice(0,remaining);
    if(/[\uD800-\uDBFF]$/.test(chunk))chunk=chunk.slice(0,-1);
    if(!chunk&&tail)break;
    out.push(chunk);used+=chunk.length;
    if(chunk.length<tail.length){textOffset+=chunk.length;break;}
    offset++;textOffset=0;
  }
  var more=offset<vals.length;
  truncated=truncated||more;
  metadata[name]={matched_count:matched,eligible_count:vals.length,returned_count:out.length,
    offset:start,text_offset:Number(sp.text_offset||0),has_more:more,continuation:more?{offset:offset,text_offset:textOffset}:null};
  data[name]=sp.all?out:(out.length?out[0]:null);
}
return __mcpB64({ok:true,type:'extract',url:location.href,title:document.title,data:data,matched_counts:counts,
  fields:metadata,truncated:truncated,complete:!truncated,chars:used,coverage:'currently_rendered_matches'});
})()
