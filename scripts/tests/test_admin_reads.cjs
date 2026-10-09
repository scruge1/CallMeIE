'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../admin.html'),'utf8');
const start=html.indexOf('  function pollIfVisible('),end=html.indexOf('  function title(',start);
const code=html.slice(start,end);
function harness(){
 const requests=[],timers=new Map();let next=0,token='synthetic-a';
 const c=vm.createContext({Map,Set,AbortController,document:{hidden:false},
  endpoint:(url,params)=>url+'?token='+token+'&'+JSON.stringify(params||{}),
  setTimeout:fn=>{const id=++next;timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id),
  fetch:(url,options)=>new Promise((resolve,reject)=>{requests.push({url,options,resolve,reject});options.signal?.addEventListener('abort',()=>reject(Object.assign(Error('private-url'),{name:'AbortError'})));})});
 vm.runInContext(code,c);
 return {c,requests,timers,token:value=>{token=value;},run:input=>vm.runInContext(input,c)};
}
function resolve(row,data={ok:true},status=200){row.resolve({ok:status<400,status,text:async()=>JSON.stringify(data)});}
test('simultaneous summary reads share one request, not a persistent cache',async()=>{
 const h=harness(),a=h.run("api('/admin/api/operations-summary')"),b=h.run("api('/admin/api/operations-summary')");
 assert.equal(h.requests.length,1);resolve(h.requests[0]);await Promise.all([a,b]);assert.equal(h.timers.size,0);
 const again=h.run("api('/admin/api/operations-summary')");assert.equal(h.requests.length,2);resolve(h.requests[1]);await again;
});
test('different tokens and params never share',async()=>{
 const h=harness(),a=h.run("api('/admin/api/health-detail',{params:{mode:'a'}})");h.token('synthetic-b');
 const b=h.run("api('/admin/api/health-detail',{params:{mode:'a'}})"),c=h.run("api('/admin/api/health-detail',{params:{mode:'b'}})");
 assert.equal(h.requests.length,3);h.requests.forEach(row=>resolve(row));await Promise.all([a,b,c]);
});
test('effectful GET synthesis and POST are never shared or timed out by helper',async()=>{
 const h=harness();const calls=[h.run("api('/admin/api/tts-preview')"),h.run("api('/admin/api/tts-preview')"),h.run("api('/admin/api/operations-summary',{method:'POST',body:'{}'})"),h.run("api('/admin/api/operations-summary',{method:'POST',body:'{}'})")];
 assert.equal(h.requests.length,4);assert.equal(h.timers.size,0);h.requests.forEach(row=>resolve(row));await Promise.all(calls);
});
test('caller-supplied signal or headers preserve independent request behavior',async()=>{
 const h=harness();const calls=[h.run("api('/admin/api/health-detail',{signal:new AbortController().signal})"),h.run("api('/admin/api/health-detail',{headers:{'X-Test':'fixture'}})")];
 assert.equal(h.requests.length,2);assert.equal(h.timers.size,0);h.requests.forEach(row=>resolve(row));await Promise.all(calls);
});
test('timeout rejects all waiting readers, clears cache and does not retry',async()=>{
 const h=harness(),a=h.run("api('/admin/api/operations-summary')"),b=h.run("api('/admin/api/operations-summary')");
 const checked=Promise.allSettled([a,b]);[...h.timers.values()][0]();const outcomes=await checked;
 assert(outcomes.every(o=>o.status==='rejected'&&o.reason.message.includes('Dashboard read timed out')));assert.equal(h.requests.length,1);assert.equal(h.timers.size,0);
 const retry=h.run("api('/admin/api/operations-summary')");assert.equal(h.requests.length,2);resolve(h.requests[1]);await retry;
});
test('HTTP errors retain existing envelope and do not poison later reads',async()=>{
 const h=harness(),a=h.run("api('/admin/api/health-detail')");const checked=assert.rejects(a,e=>e.status===401&&e.message==='Unauthorized');resolve(h.requests[0],{error:'Unauthorized'},401);await checked;
 const b=h.run("api('/admin/api/health-detail')");resolve(h.requests[1]);await b;
});
test('hidden polling issues no read; visible polling resumes once',()=>{
 const h=harness();h.c.count=0;h.c.document.hidden=true;h.run('pollIfVisible(()=>count++)');assert.equal(h.c.count,0);
 h.c.document.hidden=false;h.run('pollIfVisible(()=>count++)');assert.equal(h.c.count,1);
});
test('reviewed event, score and action reads have a finite timeout',async()=>{
 const h=harness(),calls=['events','call-scoring','today-actions'].map(p=>h.run(`api('/admin/api/${p}')`));
 const done=Promise.allSettled(calls);assert.equal(h.timers.size,3);for(const fn of [...h.timers.values()])fn();
 assert((await done).every(r=>r.status==='rejected'&&r.reason.message.includes('timed out')));assert.equal(h.timers.size,0);
});
test('manual event refreshes are independent rather than sharing an older snapshot',async()=>{
 const h=harness(),a=h.run("api('/admin/api/events',{params:{limit:300}})"),b=h.run("api('/admin/api/events',{params:{limit:300}})");
 assert.equal(h.requests.length,2);resolve(h.requests[1],[{id:'new'}]);await b;resolve(h.requests[0],[{id:'old'}]);await a;assert.equal(h.timers.size,0);
});
