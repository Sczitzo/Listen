"""Actual status-handler checks: CPU feedback and active-recording Retry guard."""
from pathlib import Path
import shutil
import subprocess

node = shutil.which('node')
assert node, 'Existing Node runtime required for status-handler checks'
script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('ui/app.js','utf8');
const html=fs.readFileSync('ui/index.html','utf8');
assert.match(html, /id="speech-runtime-status"[^>]*role="status"[^>]*aria-live="polite"/);
const controls={};
function element(){let value='',writes=0;const classes=new Set();return {
  disabled:false,className:'',title:'',
  get textContent(){return value},set textContent(next){value=next;writes++},
  get writes(){return writes},classList:{toggle(name,active){if(active)classes.add(name);else classes.delete(name)},contains:name=>classes.has(name)}
};}
const status=element();status.children={'.dot':element(),'.status-text':element()};
function $(selector,parent){return parent?parent.children[selector]:(controls[selector]??=element())}
let now=1000;
const context=vm.createContext({$, $$:selector=>selector==='.status'?[status]:[],settings:{cleanup:true,hotkey:'ctrl_r'},options:{hotkey:{'Right Ctrl':'ctrl_r'}},speechModelSavePending:false,modelSavePending:false,document:{body:element()},Date:{now:()=>now},esc:String});
vm.runInContext(source.slice(source.indexOf('function hotkeyLabel('),source.indexOf('function chip(')),context);
vm.runInContext(source.slice(source.indexOf('// ---------- polling ----------'),source.indexOf('let lastErr =')),context);
function render(runtime,extra={}){context.st={state:'idle',busy:'',locked:false,last_error:'',llm_ok:true,speech_runtime:runtime,speech_retry_ready:false,...extra};vm.runInContext('setStatus(st)',context);}
assert(!html.includes('id="rail-status"'),'The navigation duplicated the Dictate indicator');
for(const [direction,glyph] of [['Left','←'],['Right','→'],['Up','↑'],['Down','↓']]) {
 context.keycap=element();context.label=`Ctrl + ${direction} Arrow`;
 vm.runInContext('renderHotkeyKeycaps(keycap,label)',context);
 assert(context.keycap.innerHTML.includes(`aria-hidden="true">${glyph}</span><span class="sr-only">${direction} Arrow`));
}
const startup={state:'loading'};
render(startup);
assert.equal(status.children['.dot'].className,'dot busy');
assert.match($('#dictate-heading').textContent,/Getting ready/);
now+=15000;
render(startup);
assert.match($('#speech-runtime-status').textContent,/taking longer/);
assert($('#btn-retry').classList.contains('hidden'),'A still-running startup must not offer a second one');
render({state:'switching',reason:'idle_unloaded'});
assert($('#speech-runtime-status').classList.contains('hidden'),'Idle sleep is not loading');
render({state:'ready',device:'cuda',fallback:false});
assert($('#speech-runtime-status').classList.contains('hidden'));
assert($('#btn-retry').classList.contains('hidden')&&$('#btn-retry').disabled);
const cpu={state:'ready',device:'cpu',fallback:true};
render(cpu);
const notice=$('#speech-runtime-status');
assert(!notice.classList.contains('hidden'));
assert.match(notice.textContent,/computer's processor/);
assert.match(notice.textContent,/Your selected speech model is unchanged/);
assert.match(notice.textContent,/may take longer/);
render(cpu,{speech_summary:'faster-whisper small on CPU (int8)'});
assert.equal($('#diag-speech').textContent,'faster-whisper small on CPU (int8)');
const writes=notice.writes;render(cpu);assert.equal(notice.writes,writes,'Polling must not repeat screen-reader announcements');
render({state:'switching',device:'cpu',fallback:true},{busy:'transcribing'});
assert.match(notice.textContent,/Switching speech/);
const pending={state:'switching',device:'cuda',fallback:false,reason:'preview_recovery_pending'};
render(pending,{state:'recording',speech_retry_ready:false});
assert.match(notice.textContent,/Live preview paused/);
assert.match(notice.textContent,/when you finish recording/);
assert(!$('#btn-finish').disabled&&!$('#btn-cancel').disabled);
assert(!$('#btn-retry').classList.contains('hidden')&&$('#btn-retry').disabled);
render(pending,{speech_retry_ready:true});
assert(!$('#btn-retry').classList.contains('hidden')&&!$('#btn-retry').disabled);
const failed={state:'failed',device:'cpu',fallback:true};
render(failed,{state:'recording',last_error:'Synthetic speech error',locked:true});
assert(!$('#btn-finish').disabled&&!$('#btn-cancel').disabled,'Background speech failure must preserve active capture controls');
assert(!$('#btn-retry').classList.contains('hidden')&&$('#btn-retry').disabled);
render(failed,{last_error:'Synthetic speech error',speech_retry_ready:true});
assert(!$('#btn-retry').classList.contains('hidden')&&!$('#btn-retry').disabled);
render(failed,{last_error:'Synthetic speech error',speech_retry_ready:true,speech_summary:'faster-whisper small on CPU (int8) · unavailable'});
assert.match($('#diag-speech').textContent,/unavailable/);
render({state:'ready',device:'cuda',fallback:false});
assert(notice.classList.contains('hidden')&&notice.textContent==='');
console.log('CPU UI: accurate mode, stable polite notice, active Finish/Cancel and guarded Retry passed');
'''
subprocess.run([node, '-e', script], check=True)
