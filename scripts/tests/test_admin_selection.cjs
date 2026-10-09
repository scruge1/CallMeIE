'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../admin.html'),'utf8');
function part(a,b){const i=html.indexOf(a),j=html.indexOf(b,i+a.length);assert(i>=0&&j>i);return html.slice(i,j);}
const code=part('  async function loadMore(which)', '  function renderMoreHome()')+
 part('  async function renderAssistants(', '  window.selectAssistant = selectAssistant;')+
 part('  async function saveAssistantPart(', '  function numberOrNull(');
function harness({deferList=false}={}){
 const calls=[],pending=[],toasts=[];
 const nodes={'#moreView':{innerHTML:'',classList:{add(){}}},'#assistantEditor':{innerHTML:''},'#result':{textContent:''}};
 const items=['a','b'].map(id=>({dataset:{assistantId:id},active:false,classList:{toggle(_,active){items.find(i=>i.dataset.assistantId===id).active=active;}}}));
 const c=vm.createContext({state:{assistantId:'',assistantEditorId:'',assistantRequest:0,currentTab:'more',currentMore:'assistants'},qs:s=>nodes[s],qsa:s=>s==='.assistant-item'?items:[],
  setLoading:(n,t)=>{n.innerHTML=t;},title:x=>x,esc:x=>String(x),shortId:x=>x,icon:()=>'',renderError:(h,t)=>h+':'+t,
  renderAssistantEditor:a=>'DETAIL:'+a.id,showToast:(...a)=>toasts.push(a),
  api:async(url,options)=>{calls.push({url,method:options?.method||'GET'});if(options?.method==='POST')return{};
   if(url.endsWith('/assistants')&&!deferList)return{assistants:[{id:'a',name:'A'},{id:'b',name:'B'}]};
   return new Promise((resolve,reject)=>pending.push({url,resolve,reject}));}});
 vm.runInContext(code,c);return{c,nodes,calls,pending,items,toasts,run:s=>vm.runInContext(s,c)};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('entry starts detail load and binds matching editor',async()=>{const h=harness();const op=h.run("loadMore('assistants')");await tick();assert.equal(h.pending[0].url,'/admin/api/vapi/assistant/a');h.pending[0].resolve({id:'a'});await op;assert.equal(h.c.state.assistantEditorId,'a');assert.equal(h.nodes['#assistantEditor'].innerHTML,'DETAIL:a');});
test('late detail cannot overwrite newer selection',async()=>{const h=harness();const a=h.run("selectAssistant('a')"),b=h.run("selectAssistant('b')");h.pending[1].resolve({id:'b'});await b;h.pending[0].resolve({id:'a'});await a;assert.equal(h.nodes['#assistantEditor'].innerHTML,'DETAIL:b');assert.equal(h.c.state.assistantEditorId,'b');assert.equal(h.items[1].active,true);assert.equal(h.items[0].active,false);});
test('save refused while loading or mismatched',async()=>{const h=harness();h.c.state.assistantId='b';h.c.state.assistantEditorId='a';await h.run("saveAssistantPart('voice',{},'#result')");assert.equal(h.calls.length,0);assert.equal(h.toasts.length,1);});
test('loaded editor save targets its bound ID',async()=>{const h=harness();h.c.state.assistantId=h.c.state.assistantEditorId='b';await h.run("saveAssistantPart('voice',{},'#result')");assert.deepEqual(h.calls,[{url:'/admin/api/vapi/assistant/b/voice',method:'POST'}]);});
test('wrong response ID leaves editor unsaveable',async()=>{const h=harness();const op=h.run("selectAssistant('a')");h.pending[0].resolve({id:'b'});await op;assert.equal(h.c.state.assistantEditorId,'');assert(h.nodes['#assistantEditor'].innerHTML.includes('does not match'));});
test('stale failure cannot erase newer detail',async()=>{const h=harness();const a=h.run("selectAssistant('a')"),b=h.run("selectAssistant('b')");h.pending[1].resolve({id:'b'});await b;h.pending[0].reject(Error('old error'));await a;assert.equal(h.nodes['#assistantEditor'].innerHTML,'DETAIL:b');});
test('leaving view invalidates pending detail',async()=>{const h=harness();const op=h.run("selectAssistant('a')");h.c.state.currentTab='calls';h.pending[0].resolve({id:'a'});await op;assert.equal(h.c.state.assistantEditorId,'');assert(!h.nodes['#assistantEditor'].innerHTML.includes('DETAIL:a'));});
test('empty list does not start a detail call',async()=>{const h=harness({deferList:true});const op=h.run("loadMore('assistants')");h.pending[0].resolve({assistants:[]});await op;assert.equal(h.calls.length,1);assert.equal(h.c.state.assistantEditorId,'');});
test('stale list does not change current selection',async()=>{const h=harness({deferList:true});const op=h.run("loadMore('assistants')");h.c.state.currentMore='flow';h.c.state.assistantId='b';h.c.state.assistantRequest++;h.pending[0].resolve({assistants:[{id:'a'}]});await op;assert.equal(h.c.state.assistantId,'b');});
