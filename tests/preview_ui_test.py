"""Actual preview handlers with controlled bridge delays; no app or input calls."""
from pathlib import Path
import ast
import shutil
import subprocess

node = shutil.which('node')
assert node, 'Existing Node is required for preview UI checks'
root = Path(__file__).resolve().parents[1]
html = (root / 'ui/index.html').read_text(encoding='utf-8')
assert 'id="live-preview" role="region" aria-labelledby="live-preview-title" aria-live="off"' in html
assert 'Provisional. Final text is generated when you finish.' in html
module=ast.parse((root/'dictate.py').read_text(encoding='utf-8'))
version=next(n.value.value for n in module.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='UI_VERSION' for t in n.targets))
assert f'app.js?v={version}' in html and f'app.css?v={version}' in html
script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const between=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end));
const preview=between('// ---------- live preview ----------','// ---------- toasts ----------');
assert(!preview.includes('.focus('),'Preview must not change focus');
const setting=between('async function setSetting(','// ---------- settings page ----------');
const clear=between('async function clearPrivate(','$("#btn-clear-history").onclick');
const status=between('// ---------- polling ----------','let lastErr =');
const polling=between('async function poll()','// ---------- boot ----------');
const actions=source.split('\n').find(line=>line.startsWith('for(const [id,method] of [["finish"'));
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
function setup(){
 const nodes=new Map(),notices=[],connections=[];let focusCalls=0;
 function element(id){
  let text='',html='';const classes=new Set();
  return {id,writes:0,disabled:false,dataset:{},
   classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),
    toggle(x,v){if(v===undefined)v=!classes.has(x);v?classes.add(x):classes.delete(x);}},
   get textContent(){return text;},set textContent(value){this.writes++;text=value;},
   get innerHTML(){return html;},set innerHTML(value){assert(!id.startsWith('#live-preview'),'Preview rendered HTML');html=value;},
   focus(){focusCalls++;},append(){},getClientRects:()=>[1]};
 }
 const $=(selector,parent)=>{const id=parent?parent.id+' '+selector:selector;if(!nodes.has(id))nodes.set(id,element(id));return nodes.get(id);};
 const $$=selector=>selector==='.status'?[$('#status-dictate'),$('#status-lab')]:[];
 const bridge={get_settings:async()=>({...context.settings}),set_setting:async()=>true,
  get_state:async()=>state(1,'fresh'),finish_recording:async()=>true,cancel_recording:async()=>true,retry_startup:async()=>true,
  clear_private_data:async()=>({ok:true,preview_version:20}),diagnostics:async()=>({})};
 const context=vm.createContext({console,Promise,Array,Number,Math,settings:{live_preview:true,cleanup:false},S:{},
  document:{activeElement:{dataset:{}},body:element('body'),createElement:()=>element('created')},
  api:()=>bridge,$,$$,page:'dictate',toast:(...args)=>notices.push(args),connection:x=>connections.push(x),askConfirm:async()=>true,syncSettings(){},
  options:{},esc:x=>String(x),renderChips(){},renderSettings(){},renderLearned(){},refreshModels(){},
  loadHistory:async()=>{},loadRecent:async()=>{},loadLab:async()=>{},loadSuggestions(){},loadRetained:async()=>{},
  loadDeliveryStatus:async()=>{},drawBars(){},show(){},setTimeout(){},
  bridgeLost:false,lastErr:'',retainedVersion:0,lastLabVer:0,lastBusy:'',lastHistLen:0,lab:{},editorDirty:false,composerDirty:false});
 vm.runInContext(preview+setting+clear+status+polling+actions,context);
 return {c:context,$,$$,bridge,notices,focusCalls:()=>focusCalls};
}
function state(version,text,extra={}){return {state:'recording',busy:'',locked:false,last_error:'',llm_ok:true,
 retained_ver:0,lab_ver:0,lab_busy:'',history_len:0,levels:[],preview_busy:false,pending_dictations:0,
 speech_runtime:{state:'ready',device:'cuda',fallback:false},speech_retry_ready:false,
 preview:{active:true,generation:1,version,state:'listening',text,reason:''},...extra};}
function render(h,value){h.c.next=value;vm.runInContext('renderPreview(next,previewUiEpoch)',h.c);}
async function presentation(){
 const h=setup();const literal='<img src=x onerror="unexpected()"> & literal';
 render(h,state(1,literal));assert.equal(h.$('#live-preview-text').textContent,literal);
 assert(!h.$('#live-preview').classList.contains('hidden'));
 const textWrites=h.$('#live-preview-text').writes,phaseWrites=h.$('#live-preview-phase').writes;
 render(h,state(1,literal));assert.equal(h.$('#live-preview-text').writes,textWrites);
 assert.equal(h.$('#live-preview-phase').writes,phaseWrites,'Repeated polling rewrote preview announcement');
 render(h,state(2,'😀'.repeat(700)));assert.equal(Array.from(h.$('#live-preview-text').textContent).length,700);
 render(h,state(3,'last good',{preview:{active:true,generation:1,version:3,state:'paused',text:'last good',reason:'slow'}}));
 assert.match(h.$('#live-preview-phase').textContent,/paused.*responsive/);
 render(h,state(2,'older'));assert.equal(h.$('#live-preview-text').textContent,'last good');
 render(h,state(2,'',{state:'idle',preview:{active:false,version:2}}));assert.equal(h.$('#live-preview-text').textContent,'last good');
 render(h,state(4,'last good',{preview:{active:true,generation:1,version:4,state:'paused',text:'last good',reason:'unavailable'}}));
 assert.match(h.$('#live-preview-phase').textContent,/unavailable/);
 render(h,state(5,'',{state:'idle',preview:{active:false,version:5}}));
 assert(h.$('#live-preview').classList.contains('hidden'));assert.equal(h.$('#live-preview-text').textContent,'');
 assert.equal(h.$('#live-preview-phase').textContent,'');
 render(h,state(6,'shown'));render(h,{state:'idle'});assert.equal(h.$('#live-preview-text').textContent,'');
 render(h,state(7,'full retained transcript',{state:'idle',preview:{active:false,generation:1,version:7,
  phase:'finishing',state:'finishing',text:'full retained transcript'}}));
 assert.equal(h.$('#live-preview-text').textContent,'full retained transcript');
 assert.match(h.$('#live-preview-phase').textContent,/Finishing/);
 render(h,state(8,'authoritative transcript',{state:'idle',preview:{active:false,generation:1,version:8,
  phase:'final',state:'final',text:'authoritative transcript',final_text:'Clean final text.'}}));
 assert.equal(h.$('#live-preview-text').textContent,'authoritative transcript');
 assert.match(h.$('#live-preview-phase').textContent,/Final recognized/);
 render(h,state(7,'stale provisional'));assert.equal(h.$('#live-preview-text').textContent,'authoritative transcript');
 render(h,state(8,'expired',{state:'idle',preview_now:109,preview:{active:false,version:8,phase:'final',dismiss_at:108,text:'expired'}}));
 assert(h.$('#live-preview').classList.contains('hidden'));
 render(h,state(9,'',{state:'idle',preview:{active:false,version:9,phase:'idle'}}));
 assert(h.$('#live-preview').classList.contains('hidden'));
 assert.equal(h.focusCalls(),0);
}
async function clearFences(){
 for(const scope of ['history','workspace']){
  const h=setup(),reply=deferred();render(h,state(10,'before clear'));
  h.bridge.get_state=()=>reply.promise;
  const poll=vm.runInContext('poll()',h.c);
  await vm.runInContext(`clearPrivate('${scope}')`,h.c);
  assert.equal(h.$('#live-preview-text').textContent,'');
  reply.resolve(state(10,'delayed pre-clear private words'));await poll;
  assert.equal(h.$('#live-preview-text').textContent,'','Pre-clear poll repainted deleted preview');
  render(h,state(19,'below clear barrier'));assert.equal(h.$('#live-preview-text').textContent,'');
  render(h,state(21,'new preview'));assert.equal(h.$('#live-preview-text').textContent,'new preview');
  assert.equal(h.focusCalls(),0);
 }
 const h=setup(),reply=deferred();render(h,state(2,'before legacy clear'));
 h.bridge.get_state=()=>reply.promise;h.bridge.clear_private_data=async()=>({ok:true});
 const poll=vm.runInContext('poll()',h.c);await vm.runInContext("clearPrivate('history')",h.c);
 reply.resolve(state(2,'old without barrier'));await poll;assert.equal(h.$('#live-preview-text').textContent,'');
}
async function stopFences(){
 for(const action of ['finish','cancel']){
  const h=setup(),old=deferred(),ack=deferred();render(h,state(1,'visible'));
  h.bridge.get_state=()=>old.promise;h.bridge[action+'_recording']=()=>ack.promise;
  const oldPoll=vm.runInContext('poll()',h.c);
  const operation=h.$('#btn-'+action).onclick();
  assert(h.$('#live-preview').classList.contains('hidden'));assert.equal(h.$('#live-preview-text').textContent,'');
  render(h,state(2,'fresh response while stop pending'));assert.equal(h.$('#live-preview-text').textContent,'');
  ack.resolve(true);await operation;old.resolve(state(1,'delayed cancelled preview'));await oldPoll;
  assert.equal(h.$('#live-preview-text').textContent,'');
  render(h,state(3,'',{state:'idle',preview_busy:true,preview:{active:false,version:3}}));
  assert(h.$('#live-preview').classList.contains('hidden'));
  render(h,state(4,'new recording',{preview:{active:true,generation:2,version:4,state:'listening',text:'new recording',reason:''}}));
  assert.equal(h.$('#live-preview-text').textContent,'new recording');assert.equal(h.focusCalls(),0);
 }
}
async function settingFences(){
 const h=setup(),reply=deferred();render(h,state(1,'visible'));
 h.bridge.get_state=()=>reply.promise;
 const poll=vm.runInContext('poll()',h.c);
 h.bridge.get_settings=async()=>({...h.c.settings});
 await vm.runInContext('setSetting("live_preview",false)',h.c);
 assert.equal(h.c.settings.live_preview,false);assert.equal(h.$('#live-preview-text').textContent,'');
 render(h,state(2,'off must suppress'));assert.equal(h.$('#live-preview-text').textContent,'');
 await vm.runInContext('setSetting("live_preview",true)',h.c);
 reply.resolve(state(1,'old same-generation text after off/on'));await poll;
 assert.equal(h.$('#live-preview-text').textContent,'');
 render(h,state(3,'current same-generation text'));assert.equal(h.$('#live-preview-text').textContent,'current same-generation text');

 // A late acknowledgement for an earlier toggle cannot overwrite a newer one.
 const oldSettings=deferred();let gets=0;
 h.bridge.get_settings=()=>++gets===1?oldSettings.promise:Promise.resolve({...h.c.settings});
 const off=vm.runInContext('setSetting("live_preview",false)',h.c);
 await Promise.resolve();await Promise.resolve();
 await vm.runInContext('setSetting("live_preview",true)',h.c);
 oldSettings.resolve({live_preview:false,cleanup:false,style:'stale'});await off;
 assert.equal(h.c.settings.live_preview,true);
 assert.notEqual(h.c.settings.style,'stale','Old preview acknowledgement overwrote other settings');
 assert.equal(h.$('#live-preview-text').textContent,'');

 // Other settings still apply their returned fields, while a pre-toggle
 // snapshot cannot silently re-enable preview after the owner turned it off.
 const oldStyle=deferred();gets=0;
 h.bridge.get_settings=()=>++gets===1?oldStyle.promise:Promise.resolve({...h.c.settings});
 const style=vm.runInContext('setSetting("style","clean")',h.c);
 await Promise.resolve();await Promise.resolve();
 await vm.runInContext('setSetting("live_preview",false)',h.c);
 oldStyle.resolve({live_preview:true,cleanup:true,style:'clean'});await style;
 assert.equal(h.c.settings.live_preview,false);assert.equal(h.c.settings.style,'clean');assert.equal(h.c.settings.cleanup,true);
 h.bridge.get_settings=async()=>{throw Error('synthetic refresh failure')};
 await vm.runInContext('setSetting("live_preview",false)',h.c);
 assert.equal(h.c.settings.live_preview,false);assert.match(h.notices.at(-1)[0],/could not be refreshed/);
 assert.equal(vm.runInContext('previewActionPending',h.c),0,'Failed setting refresh leaked preview suppression');
 render(h,state(50,'must stay hidden after failed refresh'));assert.equal(h.$('#live-preview-text').textContent,'');
 assert.equal(h.focusCalls(),0);
}
async function controlsAndGrouping(){
 const h=setup();
 h.c.settings.cleanup=true;
 for(const [extra,expected] of [[{llm_ok:false,cleanup_warming:true},'idle · AI clean-up loading…'],[{llm_ok:false,cleanup_warming:false},'idle · AI clean-up offline'],[{llm_ok:true,cleanup_warming:true},'idle']]){
  h.c.st=state(0,'',{state:'idle',...extra});vm.runInContext('setStatus(st)',h.c);
  assert.equal(h.$('.status-text',h.$('#status-dictate')).textContent,expected);
 }
 h.c.settings.cleanup=false;
 h.c.st=state(1,'',{state:'idle',preview_busy:true,pending_dictations:1});vm.runInContext('setStatus(st)',h.c);
 assert.equal(h.$('.status-text',h.$('#status-dictate')).textContent,'finishing dictation…');
 assert(h.$('#btn-finish').disabled&&h.$('#btn-cancel').disabled);
 h.c.st=state(2,'',{state:'idle',preview_busy:true,pending_dictations:0});vm.runInContext('setStatus(st)',h.c);
 assert.equal(h.$('.status-text',h.$('#status-dictate')).textContent,'stopping preview…');
 h.c.st=state(3,'',{preview_busy:true});vm.runInContext('setStatus(st)',h.c);
 assert(!h.$('#btn-finish').disabled&&!h.$('#btn-cancel').disabled);
 h.c.st=state(4,'',{state:'idle',preview_busy:true,speech_runtime:{state:'ready',device:'cpu',fallback:true}});vm.runInContext('setStatus(st)',h.c);
 assert.match(h.$('#speech-runtime-status').textContent,/Whisper Small is unchanged/);
 vm.runInContext(between('const SETTING_ROWS =','async function refreshModels()'),h.c);
 vm.runInContext('renderSettings()',h.c);await Promise.resolve();
 const markup=h.$('#settings-grid').innerHTML;
 assert(markup.indexOf('<h2>Recording</h2>')<markup.indexOf('data-toggle="live_preview"'));
 assert(markup.indexOf('data-toggle="live_preview"')<markup.indexOf('<h2>Text cleanup</h2>'));
 assert.match(markup,/raw, provisional speech locally/);assert.equal(h.focusCalls(),0);
}
(async()=>{await presentation();await clearFences();await stopFences();await settingFences();await controlsAndGrouping();
 console.log('preview UI: actual delayed handlers, clear/off-on/version fences, full literal text, retained final stages, stable presentation and capture controls passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
result = subprocess.run([node, '-e', script, str(root / 'ui/app.js')])
assert result.returncode == 0, 'Actual preview-handler regression failed'
