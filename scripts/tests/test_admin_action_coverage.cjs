'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../admin.html'),'utf8');
const code=html.slice(html.indexOf('  function renderActionsPanel('),html.indexOf('  function actionContext('));
const c=vm.createContext({esc:String,renderActionRow:()=>'<p>Existing action</p>'});vm.runInContext(code,c);
test('partial empty response does not claim no urgent actions',()=>{const output=c.renderActionsPanel({actions:[],coverage:'partial'});assert.match(output,/Action data is incomplete/);assert.doesNotMatch(output,/No urgent actions/);});
test('available actions remain visible with source failure notice',()=>{const output=c.renderActionsPanel({actions:[{}],coverage:'partial'});assert.match(output,/Existing action/);assert.match(output,/sources are unavailable/);});
test('healthy bounded empty and legacy response retain ordinary empty state',()=>{for(const coverage of ['bounded',undefined])assert.match(c.renderActionsPanel({actions:[],coverage}),/No urgent actions/);});
