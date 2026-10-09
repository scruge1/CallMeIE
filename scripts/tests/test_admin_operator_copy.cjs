'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../admin.html'),'utf8');
function extract(start,end){const i=html.indexOf(start),j=html.indexOf(end,i);assert(i>=0&&j>i);return html.slice(i,j);}
test('section navigation never pretends to search or executes arbitrary text',()=>{
 const calls=[],messages=[],c=vm.createContext({navigate:(...args)=>calls.push(args),showToast:text=>messages.push(text)});
 vm.runInContext(extract('  function runCommandSearch(', '  async function loadChrome('),c);
 c.runCommandSearch('calls');c.runCommandSearch(' settings ');c.runCommandSearch('Call Margaret');c.runCommandSearch('delete records');c.runCommandSearch('');
 assert.deepEqual(calls,[['calls'],['more','settings']]);assert(messages.every(m=>!m.includes('Searching')));assert(messages.at(-1).includes('does not search records'));
});
test('initial chrome does not assert healthy services or a zero balance',()=>{
 assert.match(html,/<span id="healthLabel">Health unknown<\/span>/);
 assert.match(html,/<strong id="netToday">—<\/strong>/);
 assert.match(html,/aria-label="Go to a dashboard section"/);
});
test('money renderer states limited costs, preserves values, and has no invented trend',()=>{
 const c=vm.createContext({eur:(n)=>String(Number(n||0)),esc:String});
 vm.runInContext(extract('  function renderMoneyPanel(', '  function renderUsagePanel('),c);
 const rendered=c.renderMoneyPanel({today:{revenue_minor:1000,settled_fees_minor:100,net_minor:900,vapi_cost_minor:200},last_7_days:{},currency:'eur'});
 assert.match(rendered,/not profit/);assert.match(rendered,/Other business costs are not included/);
 assert.match(rendered,/Listed costs<\/span><strong>300/);assert.match(rendered,/After listed costs<\/span><strong>700/);
 assert.doesNotMatch(rendered,/<svg|class="ok-text"|<span>Net<\/span>/);assert.doesNotMatch(html,/function sparkline\(/);
});
test('healthy service probes do not claim entire systems or phone path tested',async()=>{
 const nodes={'#healthChip':{},'#healthLabel':{},'#healthDetail':{classList:{contains:()=>false}}};
 const c=vm.createContext({api:async()=>({overall:'ok'}),state:{},qs:selector=>nodes[selector],normalStatus:()=> 'ok',setFavicon:()=>{},updateThemeColor:()=>{},renderHealthDetail:()=>{}});
 vm.runInContext(extract('  async function loadHealth(', '  function normalStatus('),c);await c.loadHealth();
 assert.equal(nodes['#healthLabel'].textContent,'Service checks OK');assert.match(html,/this is not a call-path test/);
});
