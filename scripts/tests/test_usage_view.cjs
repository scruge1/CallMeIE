'use strict';
const test=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../admin.html'),'utf8');
const start=html.indexOf('  function renderUsagePanel('), end=html.indexOf('  function sparkline()',start);
const context=vm.createContext({esc:s=>String(s).replaceAll('<','&lt;').replaceAll('>','&gt;'),Date});
vm.runInContext(html.slice(start,end),context);
const render=usage=>context.renderUsagePanel({usage});
function snapshot(changes={}) {return {status:'fresh',observed_at:Math.floor(Date.now()/1000),lines:[{line_id:'line-a',completed_calls:1,completed_provider_minutes:2,observed_active_calls:1,active_provider_minutes_estimate:3,unknown_calls:0}],...changes};}
test('missing data never says zero calls',()=>{assert.match(render(),/unavailable/);assert.match(render({status:'unavailable'}),/does not mean zero/);});
test('scope and unknown billing remain visible',()=>{const output=render(snapshot());assert.match(output,/partial coverage/);assert.match(output,/not confirmed AI/);assert.match(output,/not yet verified/);assert.match(output,/before midnight/);assert.match(output,/3 min estimated/);});
test('stale snapshot hides live estimate',()=>{const output=render(snapshot({observed_at:Math.floor(Date.now()/1000)-100}));assert.match(output,/Stale snapshot/);assert.match(output,/estimate unavailable/);assert.doesNotMatch(output,/3 min estimated/);});
test('cap and conflicts are explicit',()=>{const output=render(snapshot({limit_reached:true,conflicting_calls:2,rejected_records:1}));assert.match(output,/100-call limit/);assert.match(output,/2 conflicting calls/);});
test('line IDs escaped',()=>{const output=render(snapshot({lines:[{line_id:'<script>',completed_calls:0,completed_provider_minutes:0,observed_active_calls:0}]}));assert.doesNotMatch(output,/<script>/);assert.match(output,/&lt;script&gt;/);});
test('existing summary refresh updates mounted panels without another request',()=>{
 const panels=[{},{}];context.state={ops:{usage:{status:'unavailable'}}};context.qsa=()=>panels;
 context.refreshUsagePanels();for(const panel of panels)assert.match(panel.outerHTML,/Usage data unavailable/);
 context.state.ops={usage:snapshot()};context.refreshUsagePanels();for(const panel of panels)assert.match(panel.outerHTML,/Recent snapshot/);
 context.state.ops.usage.observed_at-=100;context.refreshUsagePanels();for(const panel of panels)assert.match(panel.outerHTML,/Stale snapshot/);
});
test('configured tenant labels remain separate from verified ownership',()=>{
 const line={line_id:'line-a',configured_tenant_name:'<Tenant A>',allocation_status:'configured_match',completed_calls:1,completed_provider_minutes:2,observed_active_calls:0};
 const output=render(snapshot({lines:[line]}));assert.match(output,/Configured tenant: &lt;Tenant A&gt;/);assert.match(output,/not verified call ownership/);
});
test('conflicting and unavailable tenant mappings are explicit',()=>{
 for(const [status,label] of [['conflicting','Conflicting tenant configuration'],['unassigned','No configured tenant'],['configuration_unavailable','Tenant configuration unavailable']]){
  assert.match(render(snapshot({lines:[{allocation_status:status}]})),new RegExp(label));
 }
});
