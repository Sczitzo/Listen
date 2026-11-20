"""Actual frontend handlers with synthetic DOM/bridge; no devices, saved data or providers."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(os.environ.get('UI_REPAIR_ROOT') or Path(__file__).resolve().parents[1])
node = shutil.which('node')
assert node, 'Node is required for frontend behavior checks'
script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const src=fs.readFileSync(process.argv[1],'utf8');
function cut(a,b){const i=src.indexOf(a),j=src.indexOf(b,i);assert(i>=0&&j>i,'source boundary missing: '+a);return src.slice(i,j);}
const scoreHelpers=cut('const SCORE_LABELS =','function prevIdx(');
function element(){const cls=new Set(),attrs={},events={};let text='',writes=0;return {
 value:'',disabled:false,readOnly:false,dataset:{},style:{},events,attrs,children:[],writes:()=>writes,
 get textContent(){return text},set textContent(v){writes++;text=v},
 classList:{add:x=>cls.add(x),remove:x=>cls.delete(x),contains:x=>cls.has(x),toggle(x,on){on=on??!cls.has(x);on?cls.add(x):cls.delete(x);return on}},
 setAttribute(k,v){attrs[k]=String(v)},getAttribute:k=>attrs[k],removeAttribute:k=>delete attrs[k],
 addEventListener(k,f){events[k]=f},focus(){this.focused=true},close(){},querySelector(){return null}
};}
function registry(){const nodes=new Map();return s=>{if(!nodes.has(s))nodes.set(s,element());return nodes.get(s)}}
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}}
const tick=()=>new Promise(r=>setImmediate(r));
const failures=[];
async function check(name,fn){try{await fn();console.log('PASS '+name)}catch(e){failures.push(name+': '+e.message);console.log('FAIL '+name+': '+e.message)}}
function saveContext(bridge){
 const R=registry(),timers=new Map();let id=0;
 const c=vm.createContext({console,Promise,window:{},document:{},settings:{vocab:'old'},api:()=>bridge,
  $:R,$$:()=>[],ed:R('#draft'),composer:R('#composer'),lab:{},editorContext:'draft-a',
   editVersion:1,composerVersion:1,editorDirty:false,composerDirty:false,editTimer:0,composerTimer:0,publishingDraft:false,
  recovery(){},toast(){},loadSuggestions:async()=>{},setSetting:async()=>false,setDisplayedProperty(e,k,v){if(e[k]!==v)e[k]=v},
  setTimeout(fn,ms){timers.set(++id,{fn,ms});return id},clearTimeout(id){timers.delete(id)}});
 vm.runInContext(cut('const vocab = $("#vocab");','// ---------- command palette ----------'),c);
 vm.runInContext(cut('async function saveEdit(','function positionLabPopover('),c);
 c.ed.value='draft';c.composer.value='instruction';R('#vocab').value='old';
 return {c,R,timers,input(text){R('#vocab').value=text;R('#vocab').oninput()}};
}
(async()=>{
 await check('Ctrl+Enter dispatches Paste only; composition never submits',async()=>{
  const local={},global={},R=registry();let submits=0,pastes=0;
  R('#btn-paste').click=()=>pastes++;
  const c=vm.createContext({composer:{addEventListener:(n,f)=>local[n]=f},document:{addEventListener:(n,f)=>global[n]=f,querySelector:()=>null},
   $:R,send:()=>submits++,page:'lab',closeMore(){},openPalette(){}});
  vm.runInContext(cut('composer.addEventListener("keydown"','$("#btn-copy").onclick='),c);
  vm.runInContext(cut('// ---------- keyboard ----------','// ---------- polling ----------'),c);
  const fire=props=>{const ev={key:'Enter',defaultPrevented:false,preventDefault(){this.defaultPrevented=true},...props};local.keydown(ev);global.keydown(ev)};
  fire({ctrlKey:true});assert.equal(submits,0,'Ctrl+Enter also started Write suggestion');assert.equal(pastes,1);
   for(const props of [{isComposing:true},{keyCode:229},{keyCode:229,ctrlKey:true},{altKey:true},{metaKey:true},{shiftKey:true},{ctrlKey:true,shiftKey:true},{ctrlKey:true,altKey:true},{ctrlKey:true,metaKey:true}])fire(props);
  assert.equal(submits,0,'composition or a modified Enter started a rewrite');assert.equal(pastes,1);
  global.keydown({key:'Enter',ctrlKey:true,preventDefault(){}});assert.equal(pastes,2,'Draft shortcut did not attempt Paste');assert.equal(submits,0);
  fire({});assert.equal(submits,1,'plain Enter did not write a suggestion');
 });
 await check('Quit waits for acknowledged Vocabulary save',async()=>{
  const pending=deferred(),calls=[];
  const h=saveContext({set_setting:async(k,v)=>{calls.push(['save',k,v]);return pending.promise},quit_app:async()=>{calls.push(['quit']);return true}});
  h.input('new spelling');const quit=h.c.window.requestDictationQuit();await tick();
  assert.equal(calls.filter(x=>x[0]==='save').length,1,'Vocabulary was not submitted before Quit');
  assert.equal(calls.filter(x=>x[0]==='quit').length,0,'Quit ran before save acknowledgement');
  for(const id of ['#draft','#composer','#vocab'])assert(h.R(id).readOnly,'editable field stayed writable during Quit');
  pending.resolve(true);await quit;assert.equal(calls.at(-1)[0],'quit');
 });
 await check('Failed Vocabulary save blocks Quit and restores field states',async()=>{
  let quits=0;const h=saveContext({set_setting:async()=>false,quit_app:async()=>{quits++;return true}});
  h.R('#draft').readOnly=true;h.input('keep this spelling');await h.c.window.requestDictationQuit();
  assert.equal(quits,0,'Quit ignored a rejected Vocabulary save');assert.equal(h.R('#vocab').value,'keep this spelling');
  assert.equal(h.R('#draft').readOnly,true);assert.equal(h.R('#composer').readOnly,false);assert.equal(h.R('#vocab').readOnly,false);
 });
 await check('An in-flight Vocabulary save cannot overtake the newer save',async()=>{
  const pending=deferred(),calls=[];let first=true;
  const h=saveContext({set_setting:async(k,v)=>{calls.push(['save',v]);if(first){first=false;return pending.promise}return true},quit_app:async()=>{calls.push(['quit']);return true}});
  h.input('first');const timer=[...h.timers.values()].at(-1);timer.fn();await tick();
  h.input('second');const quit=h.c.window.requestDictationQuit();await tick();
  assert.equal(calls.length,1,'new save or Quit overtook the in-flight save');
  pending.resolve(true);await quit;
  assert.deepEqual(calls,[['save','first'],['save','second'],['quit']]);
 });
 await check('A draft transition during save prevents Quit; repeated Quit is reserved',async()=>{
  const pending=deferred();let saves=0,quits=0;
  const h=saveContext({set_setting:async()=>{saves++;return pending.promise},quit_app:async()=>{quits++;return true}});
  h.input('new');const first=h.c.window.requestDictationQuit();await tick();await h.c.window.requestDictationQuit();
  h.c.editorContext='draft-b';h.c.ed.value='newer draft';pending.resolve(true);await first;
  assert.equal(quits,0,'Quit accepted a changed draft context');assert.equal(saves,1,'duplicate Quit queued another save');
  assert.equal(h.R('#vocab').readOnly,false,'field stayed frozen after refused Quit');
 });
 await check('Backend Quit refusal or exception restores fields and permits retry',async()=>{
  for(const mode of ['refuse','throw','status-throw']){
   let first=true,quits=0;
   const h=saveContext({quit_app:async()=>{quits++;if(first){if(mode==='throw')throw Error('quit failure');return false}return true},get_state:async()=>{if(mode==='status-throw')throw Error('state failure');return {last_error:'Wait for Prompt Lab'}}});
   h.R('#composer').readOnly=true;await h.c.window.requestDictationQuit();
   assert.equal(h.R('#draft').readOnly,false,mode);assert.equal(h.R('#composer').readOnly,true,mode);assert.equal(h.R('#vocab').readOnly,false,mode);
   first=false;await h.c.window.requestDictationQuit();assert.equal(quits,2,'Quit reservation did not reset after '+mode);
  }
 });
 await check('Adding a Vocabulary suggestion preserves typed words and reserves delayed mutation',async()=>{
  const pending=deferred(),calls=[];let saved='old',quits=0;
  const h=saveContext({set_setting:async(k,v)=>{saved=v;calls.push('save');return true},add_vocab:async word=>{calls.push('add');await pending.promise;saved+=', '+word;return true},get_settings:async()=>({vocab:saved}),quit_app:async()=>{quits++;return true}});
  h.input('typed spelling');const adding=vm.runInContext('addVocabSuggestion("Expo")',h.c);await tick();
  assert(h.R('#vocab').readOnly,'typing was allowed during delayed Add');await h.c.window.requestDictationQuit();assert.equal(quits,0,'Quit overtook a Vocabulary mutation');
  pending.resolve();await adding;assert.equal(h.R('#vocab').value,'typed spelling, Expo');assert.equal(saved,'typed spelling, Expo');assert.equal(h.R('#vocab').readOnly,false);assert.deepEqual(calls,['save','add']);
 });
 await check('Vocabulary Add waits behind an already-running save',async()=>{
  const pending=deferred(),calls=[];let first=true,saved='old';
  const h=saveContext({set_setting:async(k,v)=>{calls.push(v);if(first){first=false;await pending.promise}saved=v;return true},add_vocab:async word=>{calls.push('add');saved+=', '+word;return true},get_settings:async()=>({vocab:saved})});
  h.input('first');[...h.timers.values()].at(-1).fn();await tick();h.input('latest');const adding=vm.runInContext('addVocabSuggestion("Expo")',h.c);await tick();assert.deepEqual(calls,['first']);
  pending.resolve();await adding;assert.deepEqual(calls,['first','latest','add']);assert.equal(h.R('#vocab').value,'latest, Expo');
 });
 await check('Clipboard backup wording differs from uncertain paste',async()=>{
  const R=registry();let state={pending:true,manual_id:'manual-1',message:'Previous item held'};
  const c=vm.createContext({$:R,$$:()=>[],manualClipboardReceipt:null,api:()=>({get_delivery_status:async()=>state}),setDisplayedProperty(e,k,v){if(e[k]!==v)e[k]=v}});
  vm.runInContext(cut('async function loadDeliveryStatus()','$("#restore-clipboard").onclick='),c);
  await vm.runInContext('loadDeliveryStatus()',c);
  assert.match(R('#delivery-title').textContent,/previous clipboard/i);
  assert.match(R('#delivery-description').textContent,/dictat.*(?:continue|normal)|(?:continue|normal).*dictat/i);
  state={pending:true,message:'Delivery is uncertain'};await vm.runInContext('loadDeliveryStatus()',c);
  assert.match(R('#delivery-title').textContent,/paste needs confirmation/i);
  assert.match(R('#delivery-description').textContent,/may already be there/i);
 });
 await check('Unchanged delivery polls do not rewrite the live status',async()=>{
  const R=registry();const c=vm.createContext({$:R,$$:()=>[],manualClipboardReceipt:null,api:()=>({get_delivery_status:async()=>({pending:true,message:'same warning'})}),setDisplayedProperty(e,k,v){if(e[k]!==v)e[k]=v}});
  vm.runInContext(cut('async function loadDeliveryStatus()','$("#restore-clipboard").onclick='),c);
  for(let i=0;i<3;i++)await vm.runInContext('loadDeliveryStatus()',c);
  assert.equal(R('#delivery-message').writes(),1,'unchanged live region was repeatedly replaced');
 });
 await check('Empty Instruction explains the available next step',async()=>{
  const R=registry();let submits=0;
  const c=vm.createContext({$:R,lab:{busy:false,pending:null},feedbackRequest:null,feedbackIssues:new Map(),composer:{value:'   ',focus(){this.focused=true}},composerVersion:0,editorContext:'a',setDisplayedProperty(e,k,v){e[k]=v},api:()=>({submit:async()=>submits++}),recovery(){},toast(){},flushAll:async()=>true});
  vm.runInContext(scoreHelpers+cut('let sending=false;','$("#btn-send").onclick=send;'),c);await vm.runInContext('send()',c);
  assert.equal(submits,0);assert(c.composer.focused,'empty input was silently ignored');assert.match(R('#lab-action-error').textContent,/instruction|describe/i);
 });
 await check('A waiting or stale suggestion blocks rewrites but allows Get feedback',async()=>{
  const R=registry(),calls=[];
  const c=vm.createContext({$:R,lab:{busy:false,pending:{id:'keep',stale:false}},composer:{value:'Keep this next instruction'},composerVersion:1,editorContext:'a',
    page:'lab',editVersion:1,ed:{value:'Original draft'},feedbackRequest:null,feedbackIssues:new Map(),labClearVersion:0,
   setDisplayedProperty(e,k,v){e[k]=v},api:()=>Object.fromEntries(['submit','improve','rebuild','feedback'].map(method=>[method,async()=>{calls.push(method);return true}])),
   recovery(){},toast(){},flushAll:async()=>true,loadLab:async()=>{c.feedbackRequest=null;return true;}});
  vm.runInContext(scoreHelpers+cut('let sending=false;','$("#btn-send").onclick=send;')+cut('for(const [id,method] of [["improve"','let sending=false;'),c);
  await vm.runInContext('send()',c);await R('#btn-improve').onclick();await R('#btn-rebuild').onclick();
  assert.deepEqual(calls,[],'a waiting suggestion started another rewrite');
  assert.match(R('#lab-action-error').textContent,/review.*(?:apply|discard)/i);
  c.lab.pending.stale=true;await vm.runInContext('send()',c);assert.deepEqual(calls,[],'a stale suggestion was silently replaced');
  await R('#btn-score').onclick();assert.deepEqual(calls,['feedback'],'Get feedback was blocked');
  assert.equal(c.composer.value,'Keep this next instruction');assert.equal(c.lab.pending.id,'keep');
  c.lab.pending=null;await vm.runInContext('send()',c);assert.deepEqual(calls,['feedback','submit'],'rewrite remained blocked after resolution');
 });
 await check('Unconfirmed feedback stays visible and checking it makes no new request',async()=>{
  const R=registry();let calls=0,reads=0,reject;
  const reply=new Promise((_, fail)=>reject=fail);
  const c=vm.createContext({$:R,lab:{busy:'',cur:0,workspace_id:'work-a'},ed:{value:'Book swap'},
   editorContext:'draft-a',editVersion:1,composerVersion:1,page:'lab',feedbackRequest:null,feedbackIssues:new Map(),labClearVersion:0,
   composer:{value:''},setDisplayedProperty(e,k,v){if(e[k]!==v)e[k]=v},flushAll:async()=>true,
   api:()=>({feedback:()=>{calls++;return reply;}}),loadLab:async()=>{reads++;return true;},toast(){}});
  vm.runInContext(scoreHelpers+cut('function currentFeedbackIntent(','$("#btn-send").onclick=send;'),c);
  const request=R('#btn-score').onclick();
  await tick();
  assert.equal(calls,1);assert.match(R('#lab-busy').textContent,/Getting feedback/);
  assert.equal(R('#btn-score').disabled,false,'Waiting for feedback removed the focused action');
  reject(new Error('Acknowledgement lost'));await request;
  assert.match(R('#lab-busy').textContent,/Book swap.*Check for a result/i);
  await R('#btn-score').onclick();assert.equal(calls,1,'Unconfirmed feedback submitted again before checking');
  await R('#btn-check-feedback').onclick();assert.equal(reads,1);assert.equal(calls,1);
  assert.match(R('#lab-busy').textContent,/No new feedback/i);
 });
 await check('A lost Engineer reply identifies its draft and can only be checked on the right revision',async()=>{
  const R=registry(),pending=deferred();let calls=0;
  const c=vm.createContext({$:R,lab:{busy:'',cur:1,workspace_id:'work-a'},ed:{value:'Book swap'},
   editorContext:1,editVersion:1,composerVersion:1,page:'lab',feedbackRequest:null,feedbackIssues:new Map(),labClearVersion:0,
   composer:{value:''},setDisplayedProperty(e,k,v){e[k]=v},flushAll:async()=>true,loadLab:async()=>true,
   api:()=>({draft_pass:async()=>{if(++calls===1){await pending.promise;throw Error('lost reply')}return false;}})});
  vm.runInContext(scoreHelpers+cut('function currentFeedbackIntent(','$("#btn-send").onclick=send;'),c);
  const engineer=R('#btn-draft-pass').onclick,check=R('#btn-check-feedback').onclick;
  const request=engineer();await tick();assert.equal(calls,1);
  c.lab={busy:'engineering',cur:0,workspace_id:'work-b'};c.editorContext=2;c.ed.value='New draft';pending.resolve();await request;
  assert.match(R('#lab-busy').textContent,/Book swap.*switched drafts/i);
  assert.equal(c.ed.value,'New draft');assert(R('#btn-check-feedback').classList.contains('hidden'));
  c.lab={busy:'',cur:0,workspace_id:'work-a'};c.editorContext=3;c.ed.value='Earlier revision';
  await check();assert.match(R('#lab-busy').textContent,/v2.*Book swap|Book swap.*v2/i);
  await engineer();assert.equal(calls,1,'Checking a different revision unlocked another request');
  c.lab.cur=1;c.editorContext=4;c.ed.value='Book swap';
  await engineer();assert.equal(calls,1,'Returning to the source itself unlocked another request');
  await check();await engineer();assert.equal(calls,2,'A check of the source did not allow an explicit retry');
 });
 await check('A lost Write suggestion reply requires a check before retrying',async()=>{
  const R=registry();let calls=0,reads=0;
  const c=vm.createContext({$:R,lab:{busy:'',cur:0,workspace_id:'work-a'},ed:{value:'Book swap'},
   editorContext:1,editVersion:1,composerVersion:1,page:'lab',feedbackRequest:null,feedbackIssues:new Map(),labClearVersion:0,
   composer:{value:'Preserve the titles'},setDisplayedProperty(e,k,v){e[k]=v},recovery(){},flushAll:async()=>true,
   api:()=>({submit:async()=>{if(++calls===1)throw Error('lost reply');return false;}}),loadLab:async()=>{reads++;return true;}});
  vm.runInContext(scoreHelpers+cut('function currentFeedbackIntent(','$("#btn-send").onclick=send;'),c);
  const send=()=>vm.runInContext('send()',c);
  await send();await send();assert.equal(calls,1,'Another suggestion was requested before checking');
  assert.equal(c.composer.value,'Preserve the titles');
  await R('#btn-check-feedback').onclick();assert.equal(reads,1);assert.equal(calls,1);
  await send();assert.equal(calls,2,'A read-only check did not allow an explicit retry');
 });
 await check('A late save failure leaves the current draft save status intact',async()=>{
  const pending=deferred(),messages=[];
  const save=async text=>{if(text==='old'){await pending.promise;throw Error('lost reply');}return true;};
  const {c}=saveContext({set_text:save,set_composer:save});c.recovery=message=>messages.push(message);
  const draft=vm.runInContext('saveEdit("old",editorContext,editVersion)',c);
  const instruction=vm.runInContext('saveComposer("old",editorContext,composerVersion)',c);
  c.editorContext='draft-b';c.editVersion++;c.composerVersion++;
  await vm.runInContext('saveEdit("new",editorContext,editVersion)',c);assert.equal(messages.at(-1),'Saved locally');
  pending.resolve();assert.equal(await draft,false);assert.equal(await instruction,false);
  assert.equal(messages.at(-1),'Saved locally');
 });
 await check('A suggestion arriving during save also blocks the next request',async()=>{
  const R=registry(),pending=deferred();let calls=0;
  const c=vm.createContext({$:R,lab:{busy:false,pending:null},ed:{value:'Original draft'},feedbackRequest:null,feedbackIssues:new Map(),composer:{value:'Keep this instruction'},composerVersion:1,editorContext:'a',
   setDisplayedProperty(e,k,v){e[k]=v},api:()=>({submit:async()=>{calls++;return true}}),recovery(){},toast(){},flushAll:()=>pending.promise,loadLab:async()=>true});
  vm.runInContext(scoreHelpers+cut('let sending=false;','$("#btn-send").onclick=send;'),c);
  const sending=vm.runInContext('send()',c);await tick();c.lab.pending={id:'arrived'};pending.resolve(true);await sending;
  assert.equal(calls,0,'save completion ignored the newly waiting suggestion');assert.equal(c.composer.value,'Keep this instruction');
 });
 await check('Stale suggestion guidance names the required discard step',async()=>{
  const R=registry(),c=vm.createContext({$:R,reviewDialog:{open:true,toggleAttribute(){}},reviewId:'old',reviewDraftId:1,editorContext:1,
   lab:{busy:false,cur:0,pending:{id:'old',text:'old suggestion',source_text:'earlier draft',stale:true}},
   ed:{value:'newer draft'},editorDirty:false,resolvingCandidate:false,startingReview:false,
   reviewVersion:0,reviewCount:1,candidateError:'',renderReviewFeedback(){},setDisplayedProperty(e,k,v){e[k]=v}});
  vm.runInContext(cut('function renderCandidate()','function renderReviewFeedback('),c);vm.runInContext('renderCandidate()',c);
  assert(R('#btn-apply').disabled,'stale Apply was allowed');assert(!R('#btn-discard').disabled,'stale Discard was blocked');
  assert(R('#btn-review-suggestion').disabled,'stale Review was allowed');
  assert.match(R('#candidate-note').textContent,/discard.*(?:new|another)/i,'stale guidance contradicted the waiting-suggestion gate');
 });
 await check('Page navigation focuses its destination instead of a hidden editor',async()=>{
  const R=registry();const c=vm.createContext({$:R,$$:()=>[],page:'lab',lab:{text:'draft'},feedbackRequest:null,closeMore(){},loadHistory(){},renderSettings(){}});
  vm.runInContext(cut('function show(p','$$(".rail-btn").forEach(b => b.onclick'),c);
  vm.runInContext('show("history")',c);assert(R('.page[data-page="history"] h1').focused,'page heading was not focused');
 });
 await check('Vocabulary action names identify the affected term',async()=>{
  const R=registry(),c=vm.createContext({document:{activeElement:null},$:R,$$:()=>[],esc:x=>String(x),api:()=>({get_vocab_suggestions:async()=>['Expo']})});
  vm.runInContext(cut('async function loadSuggestions()','// ---------- history & stats ----------'),c);await vm.runInContext('loadSuggestions()',c);
  assert.match(R('#vocab-chips').innerHTML,/aria-label="Add Expo to vocabulary"/);
  assert.match(R('#vocab-chips').innerHTML,/aria-label="Stop suggesting Expo"/);
 });
 await check('History exposes uncertain outcomes without claiming arrival',async()=>{
  const c=vm.createContext({esc:x=>String(x)});vm.runInContext(cut('function fmtTs(','// Rows keep their entries'),c);
  c.entry={ts:'2026-11-16T10:00:00',text:'kept text',delivery_status:'uncertain'};
  const row=vm.runInContext('histRow(entry,0)',c);assert.match(row,/check.*(?:app|destination)|delivery.*uncertain/i);
  assert(!/delivered|received/i.test(row),'uncertain result claimed receipt');
 });
 if(failures.length){console.error('\n'+failures.length+' repair regression(s) failed:\n'+failures.join('\n'));process.exitCode=1}
 else console.log('Frontend repair behavior checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
sys.exit(subprocess.run([node, '-e', script, str(root / 'ui/app.js')]).returncode)
