'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../client.html'),'utf8');
const start=html.indexOf('function endpoint('),end=html.indexOf('// --- Theme ---',start);assert(start>=0&&end>start);
function harness(token='synthetic-client',status=200){const requests=[],timers=new Map();let logouts=0,timerId=0;
 const c=vm.createContext({URL,Headers,AbortController,state:{token},window:{location:{origin:'https://fixture.test'}},
  setTimeout:(fn,ms)=>{const id=++timerId;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),
  logout:()=>logouts++,fetch:async(url,init)=>{requests.push({url,init});return {ok:status===200,status,headers:new Headers({'Content-Type':'application/json'}),json:async()=>({ok:true}),text:async()=>''};}});
 vm.runInContext(html.slice(start,end),c);return {c,requests,timers,logouts:()=>logouts};}
test('client JSON API uses its own bearer header and no query token',async()=>{const h=harness();await h.c.api('/client/api/calls',{params:{days:14}});const r=h.requests[0];assert.equal(new URL(r.url).searchParams.has('token'),false);assert.equal(new URL(r.url).searchParams.get('days'),'14');assert.equal(r.init.headers.get('Authorization'),'Bearer synthetic-client');});
test('existing mutation payload/content type survives transport change',async()=>{const h=harness();await h.c.api('/client/api/calls/fixture/note',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"note":"synthetic"}'});assert.equal(h.requests[0].init.body,'{"note":"synthetic"}');assert.equal(h.requests[0].init.headers.get('Content-Type'),'application/json');assert.equal(h.requests[0].init.method,'POST');});
test('explicit auth is preserved; explicit query credential retains conflict detection',async()=>{const h=harness();await h.c.api('/client/api/calls',{headers:{Authorization:'Bearer explicit'}});assert.equal(h.requests[0].init.headers.get('Authorization'),'Bearer explicit');assert.equal(new URL(h.requests[0].url).searchParams.has('token'),false);await h.c.api('/client/api/calls',{params:{token:'explicit-query'},headers:{Authorization:'Bearer explicit'}});assert.equal(new URL(h.requests[1].url).searchParams.get('token'),'explicit-query');});
test('native media and non-header-safe legacy credentials keep query transport',async()=>{const h=harness('synthetic-é');await h.c.api('/client/api/calls');assert.equal(new URL(h.requests[0].url).searchParams.get('token'),'synthetic-é');assert.equal(h.requests[0].init.headers.has('Authorization'),false);const media=new URL(h.c.endpoint('/client/api/voice-preview'));assert.equal(media.searchParams.get('token'),'synthetic-é');});
test('401 logs out and rejects; it does not resolve as an empty data response',async()=>{const h=harness('synthetic',401);await assert.rejects(h.c.api('/client/api/calls'),/Sign-in required/);assert.equal(h.logouts(),1);});

test('ordinary data reads carry a signal and clean up their timeout',async()=>{
 for(const path of ['/client/api/me','/client/api/calls','/client/api/calls/fixture','/client/api/inbox','/client/api/insights','/client/api/settings']){
  const h=harness();await h.c.api(path);assert(h.requests[0].init.signal);assert.equal(h.timers.size,0);
 }
});
test('stalled fetch rejects after the selected read deadline without retry',async()=>{
 const h=harness();let attempts=0;
 h.c.fetch=async(url,init)=>{attempts++;return new Promise((resolve,reject)=>init.signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true}));};
 const result=h.c.api('/client/api/calls');const timer=[...h.timers.values()][0];assert.equal(timer.ms,25000);timer.fn();
 await assert.rejects(result,/Request timed out/);assert.equal(attempts,1);assert.equal(h.timers.size,0);assert.equal(h.logouts(),0);
});
test('deadline stays active while a JSON body is stalled after headers',async()=>{
 const h=harness();let bodyStarted=false;
 h.c.fetch=async(url,init)=>({ok:true,headers:new Headers({'Content-Type':'application/json'}),json:()=>{bodyStarted=true;return new Promise((resolve,reject)=>init.signal.addEventListener('abort',()=>reject(new Error('body aborted')),{once:true}));}});
 const result=h.c.api('/client/api/calls/fixture');for(let n=0;n<4;n++)await Promise.resolve();assert(bodyStarted);
 assert.equal(h.timers.size,1);[...h.timers.values()][0].fn();await assert.rejects(result,/Request timed out/);assert.equal(h.timers.size,0);
});
test('writes and voice previews have no implicit read deadline',async()=>{
 for(const [path,opts] of [['/client/api/calls/fixture/note',{method:'POST',body:'synthetic'}],['/client/api/settings',{method:'PATCH',body:'synthetic'}],['/client/api/voice-preview',{}]]){
  const h=harness();await h.c.api(path,opts);assert.equal(h.requests[0].init.signal,undefined);assert.equal(h.timers.size,0);
 }
});
test('explicit read cancellation remains caller controlled',async()=>{
 const h=harness(),controller=new AbortController();await h.c.api('/client/api/calls',{signal:controller.signal});
 assert.equal(h.requests[0].init.signal,controller.signal);assert.equal(h.timers.size,0);
});
