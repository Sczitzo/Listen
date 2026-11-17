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
const context=vm.createContext({$, $$:selector=>selector==='.status'?[status]:[],settings:{cleanup:true,hotkey:'ctrl_r'},options:{hotkey:{'Right Ctrl':'ctrl_r'}},document:{body:element()}});
vm.runInContext(source.slice(source.indexOf('// ---------- polling ----------'),source.indexOf('let lastErr =')),context);
function render(runtime,extra={}){context.st={state:'idle',busy:'',locked:false,last_error:'',llm_ok:true,speech_runtime:runtime,speech_retry_ready:false,...extra};vm.runInContext('setStatus(st)',context);}
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
