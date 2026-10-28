(function(){
/* fill typed into a text box; a search box (React select, MUI Autocomplete, a site's own) only takes a
   value chosen from the list that typing opened. mode "pick" looks for that list near the box and
   presses the option that names the value; "check" reads whether the choice stayed. */
__BOOTSTRAP__
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
function norm(v){return String(v||'').normalize('NFKD').toLowerCase().replace(/[̀-ͯ]/g,'').replace(/\s+/g,' ').trim();}
var s=__mcpState(),eid=__ELEMENT_ID__,wanted=norm(__TEXT__),mode=__MODE__;
var el=s.elements[eid];
if(!el||!el.isConnected)return __mcpB64({ok:false,error:'stale_element',element_id:eid});
var ROW='[role="option"],li,[class*="option"],[class*="Option"],[class*="suggest"],[class*="result"],[class*="item"]';

function attr(e,name){return String(e.getAttribute(name)||'').toLowerCase();}
function marked(e){
  var popup=attr(e,'aria-haspopup');
  return attr(e,'role')==='combobox'||['list','both'].indexOf(attr(e,'aria-autocomplete'))>=0||
    popup==='listbox'||popup==='true'||!!e.getAttribute('aria-controls')||!!e.getAttribute('aria-owns');
}
function byIds(list){return String(list||'').split(/\s+/).filter(Boolean).map(function(id){
  var root=el.getRootNode&&el.getRootNode();var found=null;try{found=(root&&root.getElementById)?root.getElementById(id):null;}catch(e){}
  return found||document.getElementById(id);}).filter(Boolean);}
/* The list this box says it controls: its own aria-controls/owns, an ARIA 1.1 combobox wrapper's, or
   the list its highlighted option sits in. */
function scopes(){
  var out=byIds(el.getAttribute('aria-controls')).concat(byIds(el.getAttribute('aria-owns')));
  for(var p=__mcpParent(el),g=0;p&&g<3&&!out.length;p=__mcpParent(p),g++){
    if(attr(p,'role')==='combobox')out=byIds(p.getAttribute('aria-owns')).concat(byIds(p.getAttribute('aria-controls')));
  }
  var active=byIds(el.getAttribute('aria-activedescendant'))[0];
  if(active){var list=active.closest('[role="listbox"]');if(list&&out.indexOf(list)<0)out.push(list);}
  return out.filter(function(x){return x!==el&&!x.contains(el);});
}
function label(row){var t='';try{t=row.innerText||row.textContent||'';}catch(e){}return norm(t||row.getAttribute('aria-label')||'');}
function usable(row){return row!==el&&!row.contains(el)&&__mcpVisible(row)&&attr(row,'aria-disabled')!=='true'&&!row.hasAttribute('disabled');}
/* A page link is not a list choice: pressing it would leave the page. */
function navigates(row){var a=row.closest('a[href]');if(!a)return false;var h=String(a.getAttribute('href')||'').trim();return !!h&&h!=='#'&&h.indexOf('javascript:')!==0;}
var box=__mcpTopRect(el);
function near(row){
  var r=__mcpTopRect(row);
  var vertical=r.top>=box.top-520&&r.bottom<=box.bottom+520;
  var horizontal=r.right>box.left-40&&r.left<box.right+40;
  return vertical&&horizontal;
}
/* A list drawn over the page: the row or one of its first ancestors floats (absolute/fixed) and that
   layer does not hold the box itself (a dialog holding the whole form is not a list). */
function floating(row){
  for(var c=row,g=0;c&&c.nodeType===1&&g<10;c=__mcpParent(c),g++){
    var pos='';try{pos=__mcpStyle(c).position;}catch(e){}
    if(pos==='absolute'||pos==='fixed')return !c.contains(el);
  }
  return false;
}
/* The rows of a list: its options, else rows its classes or tags call so, else its own children (a
   list of plain divs), one wrapper deep. */
function rowsIn(list){
  var found=[];try{found=Array.from(list.querySelectorAll('[role="option"]'));}catch(e){}
  if(!found.length)try{found=Array.from(list.querySelectorAll(ROW));}catch(e){}
  if(!found.length){var kids=Array.from(list.children);if(kids.length===1&&kids[0].children.length)kids=Array.from(kids[0].children);found=kids;}
  return found.filter(function(row){return usable(row)&&!!label(row);});
}
/* Layers drawn over the page near the box (absolute/fixed, not holding it): among the siblings of the
   box and of its first ancestors, or at the end of the body, where portals put them. */
function layers(){
  var out=[],seen=new Set();
  function add(c){
    if(!c||c.nodeType!==1||seen.has(c)||c===el||c.contains(el))return;seen.add(c);
    var pos='';try{pos=__mcpStyle(c).position;}catch(e){}
    if((pos==='absolute'||pos==='fixed')&&__mcpVisible(c)&&near(c))out.push(c);
  }
  for(var p=__mcpParent(el),g=0;p&&g<4;p=__mcpParent(p),g++)Array.from(p.children).forEach(add);
  if(document.body)Array.from(document.body.children).forEach(function(c){add(c);if(c.children.length===1)add(c.children[0]);});
  return out;
}
function unnamed(keep){
  var options=__mcpQueryAll('[role="listbox"] [role="option"],[role="option"]').filter(function(row){return usable(row)&&near(row)&&keep(row);});
  if(options.length)return {rows:options,source:'listbox'};
  var floated=[];
  layers().forEach(function(layer){floated=floated.concat(rowsIn(layer));});
  floated=floated.filter(function(row){return near(row)&&!navigates(row)&&keep(row);});
  if(floated.length)return {rows:floated,source:'floating'};
  return {rows:__mcpQueryAll(ROW).filter(function(row){return usable(row)&&near(row)&&floating(row)&&!navigates(row)&&keep(row);}),source:'floating'};
}
/* Rows shown before typing (recorded by mode "before") belong to a list some other field left open. */
var before=s.fillBefore&&s.fillBefore.eid===eid?s.fillBefore.rows:null;
function fresh(row){return !before||!before.has(row);}
if(mode==='before'){
  var shown=[];
  ['listbox','layers','loose'].forEach(function(kind){
    if(kind==='listbox')shown=shown.concat(__mcpQueryAll('[role="option"]').filter(function(row){return usable(row)&&near(row);}));
    else if(kind==='layers')layers().forEach(function(layer){shown=shown.concat(rowsIn(layer));});
    else shown=shown.concat(__mcpQueryAll(ROW).filter(function(row){return usable(row)&&near(row)&&floating(row);}));
  });
  s.fillBefore={eid:eid,rows:new Set(shown)};
  return __mcpB64({ok:true,shown:shown.length});
}
function rows(){
  var found=[];
  scopes().forEach(function(scope){found=found.concat(rowsIn(scope));});
  if(found.length)return {rows:found,source:'controls'};
  /* A list the box does not name is taken as its own only while the box has focus: one another field
     left open belongs to that field. */
  var active=document.activeElement;while(active&&active.shadowRoot&&active.shadowRoot.activeElement)active=active.shadowRoot.activeElement;
  if(active!==el)return {rows:[],source:'none'};
  return unnamed(fresh);
}
/* The outer row of a nested pair (li > span.option) counts once. */
function outermost(list){return list.filter(function(row){return !list.some(function(other){return other!==row&&other.contains(row);});});}
function choose(list){
  list=outermost(list);
  var exact=list.filter(function(row){return label(row)===wanted;});
  if(exact.length)return {row:exact[0],ambiguous:exact.length>1&&!exact.every(function(r){return label(r)===label(exact[0]);})};
  var starts=list.filter(function(row){return label(row).indexOf(wanted)===0;});
  if(starts.length===1)return {row:starts[0]};
  var words=list.filter(function(row){return (' '+label(row).replace(/[^\p{L}\p{N}]+/gu,' ')+' ').indexOf(' '+wanted+' ')>=0;});
  if(words.length===1)return {row:words[0]};
  return {row:null,ambiguous:starts.length>1||words.length>1};
}
/* A person's press: pointer down (whose default focuses what it lands on, unless the page keeps focus in
   the box, as these lists do), up, click. A forced focus would blur the box and close the list. */
function press(row){
  var target=row,down=null;
  ['pointerover','mouseover','pointermove','mousemove','pointerdown'].forEach(function(type){var ev=__mcpMouseEvent(target,type);if(ev)try{target.dispatchEvent(ev);}catch(e){}});
  down=__mcpMouseEvent(target,'mousedown');var kept=false;
  if(down)try{kept=!target.dispatchEvent(down);}catch(e){}
  if(!kept){var focusable=target.closest('[tabindex],a[href],button');if(focusable&&focusable!==el)try{focusable.focus({preventScroll:true});}catch(e){}}
  ['pointerup','mouseup'].forEach(function(type){var ev=__mcpMouseEvent(target,type);if(ev)try{target.dispatchEvent(ev);}catch(e){}});
  try{target.click();}catch(e){}
}
function shownText(exclude){
  /* The box's own widget: the nearest ancestor that holds no other form control. React select shows the
     choice beside an emptied input. */
  var widget=el;
  for(var p=__mcpParent(el),g=0;p&&g<4;p=__mcpParent(p),g++){
    var controls=0;try{controls=p.querySelectorAll('input:not([type="hidden"]),select,textarea').length;}catch(e){}
    if(controls>1)break;widget=p;
  }
  var text='';try{text=widget.innerText||'';}catch(e){}
  text=norm(text);
  /* A list left open inside the widget (Tom Select) is not what the box shows. */
  if(exclude&&widget.contains(exclude)){var listed='';try{listed=norm(exclude.innerText||'');}catch(e){}if(listed)text=text.replace(listed,' ');}
  return text;
}

if(mode==='check'){
  var pick=s.fillPick&&s.fillPick.eid===eid?s.fillPick:null,chosen=pick?pick.text:wanted;
  var open=!!(pick&&pick.row&&pick.row.isConnected&&__mcpVisible(pick.row));
  var value=norm(el.value);
  var row=pick&&pick.row,list=row?(row.closest('[role="listbox"]')||__mcpParent(row)):null;
  /* aria-selected is no proof: many lists set it on the option a key or the pointer merely highlights. */
  var kept=!!chosen&&((!!value&&(value.indexOf(chosen)>=0||chosen.indexOf(value)===0))||shownText(open?list:null).indexOf(chosen)>=0);
  return __mcpB64({ok:true,kept:kept,open:open,value:String(el.value||'').slice(0,200),expanded:attr(el,'aria-expanded')||undefined});
}

if(!wanted)return __mcpB64({ok:true,marked:marked(el),popup:false});
var found=rows(),list=outermost(found.rows);
var popup=list.length>0,isMarked=marked(el);
var busy=attr(el,'aria-busy')==='true'||scopes().some(function(sc){return attr(sc,'aria-busy')==='true';})||
  list.length<=1&&list.some(function(row){return /^(loading|searching|yukleniyor|araniyor)/.test(label(row));});
var out={ok:true,marked:isMarked,popup:popup,source:popup?found.source:undefined,busy:busy||undefined,
  options:list.slice(0,12).map(function(row){var t='';try{t=(row.innerText||row.textContent||'').replace(/\s+/g,' ').trim();}catch(e){}return t.slice(0,80);}).filter(Boolean)};
if(!popup)return __mcpB64(out);
/* "No results": the list itself says it holds no such option. */
var NOTICE=/^(no (results|options|matches|items|data)|nothing found|not found|sonuc (bulunamadi|yok)|eslesme yok|kein|aucun)/;
if(list.every(function(row){return NOTICE.test(label(row));})){out.found=false;out.no_such_option=true;return __mcpB64(out);}
var choice=choose(list);
if(!choice.row){out.found=false;out.ambiguous=!!choice.ambiguous||undefined;return __mcpB64(out);}
var text=label(choice.row);
s.fillPick={eid:eid,row:choice.row,text:text};
press(choice.row);
out.found=true;out.picked=(choice.row.innerText||choice.row.textContent||'').replace(/\s+/g,' ').trim().slice(0,200);
return __mcpB64(out);
})()
