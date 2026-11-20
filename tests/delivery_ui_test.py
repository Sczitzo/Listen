"""Controlled bridge delays: no browser/provider/clipboard or saved-data operations."""
import shutil
import subprocess
from pathlib import Path

node = shutil.which('node')
assert node, 'Node is required for controlled frontend delivery regressions'
script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const src=fs.readFileSync(process.argv[1],'utf8');
async function delayedSend(){
 const controls={},calls=[];let release,entered;
 const started=new Promise(r=>entered=r);
 const bridge={currentDraft:1,saves:0,set_text:async()=>true,
  set_composer:async()=>{if(++bridge.saves===1){entered();return new Promise(r=>release=r)}return true},
  submit:async(text,context)=>{calls.push({text,context,draft:bridge.currentDraft});return true},new_draft:async()=>{bridge.currentDraft++;return true}};
 const c=vm.createContext({composer:{value:'Instruction for A',addEventListener(){}},ed:{value:'Draft A',focus(){}},
  editorContext:1,editVersion:1,composerVersion:1,editorDirty:false,composerDirty:true,editTimer:null,composerTimer:null,
  lab:{busy:false},feedbackRequest:null,feedbackIssues:new Map(),labClearVersion:0,api:()=>bridge,clearTimeout(){},setTimeout(){},recovery(){},toast(){},
  setDisplayedProperty(el,key,value){el[key]=value;},
  $:id=>controls[id]??={dataset:{},classList:{toggle(){}}},
  loadLab:async()=>{c.editorContext=bridge.currentDraft;c.composer.value=''}});
 const saves=src.slice(src.indexOf('async function saveEdit('),src.indexOf('window.requestDictationQuit'));
 const send=src.slice(src.indexOf('let sending=false;'),src.indexOf('$("#btn-send").onclick=send;'));
 const newHandler=src.split('\n').find(x=>x.startsWith('$("#btn-new").onclick='));
 const scoreHelpers=src.slice(src.indexOf("const SCORE_LABELS ="),src.indexOf("function prevIdx("));
 vm.runInContext(scoreHelpers+saves+send+newHandler,c);
 const pending=vm.runInContext('send()',c);await started;
 await vm.runInContext('send()',c); // Duplicate click while saving must be reserved.
 await controls['#btn-new'].onclick();release(true);await pending;
 assert.equal(calls.length,0,'Old instruction entered replacement draft');
}
async function recoveryFailures(){
 let mode='false';const notices=[],copy={dataset:{copyRetained:'1'}},dismiss={dataset:{dismissRetained:'1'}};
 const bridge={get_retained:async()=>[{id:1,text:'retained synthetic text',message:'review'}],
  copy_retained:async()=>{if(mode==='throw')throw Error('synthetic');return false},dismiss_retained:async()=>{throw Error('synthetic')}};
 const c=vm.createContext({api:()=>bridge,esc:x=>x,toast:x=>notices.push(x),
  $:id=>({classList:{toggle(){}},innerHTML:''}),$$:s=>s.includes('copy')?[copy]:[dismiss]});
 vm.runInContext(src.slice(src.indexOf('async function loadRetained()'),src.indexOf('async function loadDeliveryStatus()')),c);
 await vm.runInContext('loadRetained()',c);await copy.onclick();assert.equal(notices.length,1);
 mode='throw';await copy.onclick();assert.equal(notices.length,2);await dismiss.onclick();assert.equal(notices.length,3);
}
async function settingsFocus(){
 // Changing a setting updates controls in place (syncSettings); the Settings page is not rebuilt, so focus cannot be lost.
 let rebuilt=0,synced=0;
 const c=vm.createContext({document:{activeElement:{dataset:{key:'style'}}},
  api:()=>({set_setting:async()=>true,get_settings:async()=>({style:'clean'})}),settings:{},page:'settings',previewSettingVersion:0,
  renderChips(){},renderSettings(){rebuilt++},syncSettings(){synced++},toast(){},askConfirm:async()=>true});
 vm.runInContext(src.slice(src.indexOf('async function setSetting('),src.indexOf('// ---------- settings page')),c);
 await vm.runInContext('setSetting("style","clean")',c);assert.equal(rebuilt,0,'Settings page was rebuilt');assert.equal(synced,1);
}
(async()=>{await delayedSend();await recoveryFailures();await settingsFocus();
 assert.ok(src.includes('voice-triggered prompt engineering text'));
 assert.ok(src.includes('Engineered prompt'));
 assert.ok(src.includes('#lab-action-error'));
 console.log('delivery UI: delayed Send/New, duplicate admission, recovery errors, in-place settings update and provenance passed');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
subprocess.run([node, '-e', script, str(Path(__file__).resolve().parents[1]/'ui/app.js')], check=True)
