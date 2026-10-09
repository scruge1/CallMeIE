'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../admin.html'),'utf8');
function part(a,b){return html.slice(html.indexOf(a),html.indexOf(b,html.indexOf(a)+a.length));}
const code=part('  async function loadCalls()', '  function renderSendSetupCard(')+
 part('  async function loadToday()', '  function renderActionsPanel(')+
 part('  function navigate(', '  window.navigate = navigate;');
function harness(){
 const pending=[],renders=[],timers=[];
 const nodes={'#callsView':{innerHTML:'',classList:{add(){}}},'#todayView':{innerHTML:'',classList:{add(){}}},'#moreView':{innerHTML:'',classList:{add(){}}},'#main':{hidden:false}};
 const c=vm.createContext({state:{token:'fixture',currentTab:'calls',callsRequest:0,todayRequest:0,callsLoading:false,events:[],ops:{},health:{},callFilter:'all',assistantRequest:0},window:{},
  qs:s=>nodes[s],qsa:()=>[],location:{hash:'',pathname:'/'},history:{replaceState(){}},clearInterval(){},setInterval:fn=>{timers.push(fn);return 1;},pollIfVisible:fn=>fn(),loadMore(){},closeMoreSheet(){},
  setLoading:(view,text)=>{view.innerHTML=text;},esc:String,icon:()=>'',renderSendSetupCard:()=>'<draft-form>',hydrateLatestLeadPhone(){},
  renderError:(title,message)=>title+':'+message,
  renderFilteredCallGroups:(events,scoring,mode)=>{renders.push(mode);return JSON.stringify(events)+':'+mode;},
  filteredCallGroups:(events,scoring,mode)=>({flatCount:mode==='hot'?0:events.length}),
  api:url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject})),loadRecordingsData:async()=>[],
  renderActionsPanel:()=>'',renderMoneyPanel:()=>'',renderUsagePanel:()=>'',renderSystemPanel:()=>'',renderRecordingsPanel:()=>'',renderSignalPanel:()=>''});
 vm.runInContext(code,c);
 const mounted=()=>{nodes['#callsView'].innerHTML='existing draft and filters';nodes['#callTimeline']={innerHTML:'old'};nodes['#callEventsCount']={textContent:''};nodes['#callsRefreshStatus']={textContent:''};};
 return {c,pending,nodes,timers,renders,mounted,run:s=>vm.runInContext(s,c)};
}
function complete(h,offset,id){h.pending[offset].resolve([{call_id:id}]);h.pending[offset+1].resolve({});}
test('late earlier refresh cannot overwrite newest response',async()=>{
 const h=harness();h.mounted();const a=h.run('loadCalls()'),b=h.run('loadCalls()');complete(h,2,'new');await b;complete(h,0,'old');await a;
 assert.equal(h.c.state.events[0].call_id,'new');assert.match(h.nodes['#callTimeline'].innerHTML,/new/);assert.equal(h.c.state.callsLoading,false);
});
test('refresh preserves form DOM and applies latest filter/count',async()=>{
 const h=harness();h.mounted();const op=h.run('loadCalls()');h.c.state.callFilter='hot';complete(h,0,'new');await op;
 assert.equal(h.nodes['#callsView'].innerHTML,'existing draft and filters');assert.equal(h.renders[0],'hot');assert.equal(h.nodes['#callEventsCount'].textContent,'0 events');
});
test('failed refresh retains old records with explicit stale status',async()=>{
 const h=harness();h.mounted();h.c.state.events=[{call_id:'previous'}];const op=h.run('loadCalls()');h.pending[0].reject(Error('synthetic failure'));h.pending[1].resolve({});await op;
 assert.equal(h.c.state.events[0].call_id,'previous');assert.equal(h.nodes['#callTimeline'].innerHTML,'old');assert.match(h.nodes['#callsRefreshStatus'].textContent,/previously loaded/);assert.equal(h.c.state.callsLoading,false);
});
test('invalid first response is an error, not successful empty data',async()=>{
 const h=harness(),op=h.run('loadCalls()');h.pending[0].resolve({invalid:true});h.pending[1].resolve({});await op;
 assert.match(h.nodes['#callsView'].innerHTML,/Calls could not load/);assert.equal(h.c.state.events.length,0);
});
test('score failure does not erase valid call events',async()=>{
 const h=harness();h.mounted();const op=h.run('loadCalls()');h.pending[0].resolve([{call_id:'new'}]);h.pending[1].reject(Error('scores unavailable'));await op;
 assert.equal(h.c.state.events[0].call_id,'new');assert.match(h.nodes['#callsRefreshStatus'].textContent,/scores unavailable/);
});
test('navigation invalidates old request even after returning to Calls',async()=>{
 const h=harness();h.mounted();const old=h.run('loadCalls()');h.run("navigate('more','settings')");h.run("navigate('calls')");complete(h,2,'new');await new Promise(r=>setImmediate(r));complete(h,0,'old');await old;
 assert.equal(h.c.state.events[0].call_id,'new');
});
test('polling cannot pile up requests during a slow refresh',async()=>{
 const h=harness();h.run("navigate('calls')");assert.equal(h.pending.length,2);h.timers[0]();h.timers[0]();assert.equal(h.pending.length,2);
 complete(h,0,'first');await new Promise(r=>setImmediate(r));h.timers[0]();assert.equal(h.pending.length,4);complete(h,2,'second');await new Promise(r=>setImmediate(r));
});
test('late Today response cannot overwrite visible Calls state',async()=>{
 const h=harness();h.c.state.currentTab='today';const today=h.run('loadToday()');h.c.state.currentTab='calls';const calls=h.run('loadCalls()');complete(h,2,'current-call');await calls;
 h.pending[0].resolve({actions:[]});h.pending[1].resolve([{call_id:'old-today'}]);await today;
 assert.equal(h.c.state.events[0].call_id,'current-call');
});
test('hidden Calls invocation produces no fetch',async()=>{
 const h=harness();h.c.state.currentTab='more';await h.run('loadCalls()');assert.equal(h.pending.length,0);
});
test('rejected Today events retain prior data and explicitly report failure',async()=>{
 const h=harness();h.c.state.currentTab='today';h.c.state.events=[{call_id:'previous'}];h.nodes['#todayView'].innerHTML='existing Today';h.nodes['#todayRefreshStatus']={textContent:''};
 const op=h.run('loadToday()');h.pending[0].resolve({actions:[]});h.pending[1].reject(Error('synthetic unavailable'));await op;
 assert.equal(h.c.state.events[0].call_id,'previous');assert.equal(h.nodes['#todayView'].innerHTML,'existing Today');assert.match(h.nodes['#todayRefreshStatus'].textContent,/previously loaded/);
});
test('failed initial actions do not claim no urgent work',async()=>{
 const h=harness();h.c.state.currentTab='today';const op=h.run('loadToday()');h.pending[0].reject(Error('actions unavailable'));h.pending[1].resolve([]);await op;
 assert.match(h.nodes['#todayView'].innerHTML,/Today could not load/);assert.doesNotMatch(h.nodes['#todayView'].innerHTML,/No urgent actions/);
});
test('malformed call records do not replace retained state',async()=>{
 const h=harness();h.mounted();h.c.state.events=[{call_id:'previous'}];const op=h.run('loadCalls()');h.pending[0].resolve([null]);h.pending[1].resolve({});await op;
 assert.equal(h.c.state.events[0].call_id,'previous');assert.match(h.nodes['#callsRefreshStatus'].textContent,/Refresh failed/);
});
test('call heat reads the actual API events envelope and keeps legacy envelopes',()=>{
 const c=vm.createContext({parseDetail:value=>value||{}});vm.runInContext(part('  function callHeat(', '  function detailSummary('),c);
 for(const field of ['events','calls','scores','data'])assert.equal(c.callHeat([],{[field]:[{call_id:'fixture',heat:'very_interested'}]},'fixture'),'very_interested');
 assert.equal(c.callHeat([{detail:{interest_level:'curious'}}],{},'fixture'),'curious');
});
test('existing inline Calls Refresh button has a callable page binding',()=>{
 const h=harness();assert.equal(typeof h.c.window.loadCalls,'function');assert.equal(h.c.window.loadCalls,h.c.loadCalls);
});
