"""Model-menu contracts using actual source, fake transport and a synthetic DOM."""
import ast
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace
from speech_runtime import speech_model_label, speech_model_note

ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / 'dictate.py').read_text(encoding='utf-8-sig'))
api = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Api')
methods = [n for n in api.body if isinstance(n, ast.FunctionDef) and n.name in ('get_options', 'get_local_models')]
inventory = {'models': [{'name': 'qwen3.5:4b'}, {'name': 'qwen2.5:7b'}, {'name': 'qwen3.5:4b'}]}
def get(*args, **kwargs):
    assert kwargs['allow_redirects'] is False and kwargs['timeout'] <= 4
    return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: inventory)
scope = dict(SETTINGS={'local_model': 'qwen2.5:7b'}, OLLAMA_MODEL='qwen2.5:7b',
             MODEL_SIZE='small', requests=SimpleNamespace(get=get), _NO_PROXY={},
             _data_lock=threading.RLock(), cached_speech_models=lambda: {'Whisper Small': 'small'},
             speech_model_label=speech_model_label, speech_model_note=speech_model_note,
             PROMPT_PROVIDERS={'openai': ('OpenAI', 'Example model', ('OPENAI_API_KEY',))},
             _prompt_api_key=lambda _: 'synthetic-key',
             **{k: {} for k in ('HOTKEY_NAMES', 'STYLE_NAMES', 'TARGET_NAMES', 'JUDGE_NAMES', 'JUDGE_MODEL_NAMES', 'ENGINE_NAMES')})
for method in methods:
    exec(compile(ast.Module(body=[method], type_ignores=[]), 'actual-model-settings', 'exec'), scope)
receiver = SimpleNamespace(get_microphones=lambda: {'Windows default': None})
result = scope['get_local_models'](receiver)
assert result.get('available') is True, 'successful inventory must explicitly confirm availability'
assert result['models'] == ['qwen2.5:7b', 'qwen3.5:4b']
assert scope['get_options'](receiver)['speech_model'] == 'small', 'speech model must come from its actual configuration'
assert scope['get_options'](receiver)['prompt_providers'] == {
    'openai': {'name': 'OpenAI', 'configured': True, 'model': 'Example model', 'key_env': 'OPENAI_API_KEY'},
}
scope['MODEL_SIZE'] = 'parakeet-unified-en-0.6b'
parakeet = scope['get_options'](receiver)
assert 'Parakeet' in parakeet['speech_model_label'] and 'Q8_0' in parakeet['speech_model_label']
assert 'English only' in parakeet['speech_model_note'] and 'cleanup' in parakeet['speech_model_note']
scope['MODEL_SIZE'] = 'small'
inventory = {'models': []}
result = scope['get_local_models'](receiver)
assert result['available'] is True and result['models'] == [] and result['error']
scope['requests'].get = lambda *a, **kw: (_ for _ in ()).throw(TimeoutError())
result = scope['get_local_models'](receiver)
assert result.get('available') is False and result['models'] == []
assert result['selected'] == 'qwen2.5:7b' and result['error']
print('PASS actual backend: complete inventory, explicit failure and separate speech configuration')

node = shutil.which('node')
assert node, 'Node required for frontend behavior checks'
script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('let modelsRefreshVersion'),end=source.indexOf('async function renderLearned()',start);
const settingStart=source.indexOf('async function setSetting('),settingEnd=source.indexOf('// ---------- settings page ----------',settingStart);
assert(start>=0&&end>start);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}}
function setup(){
 const nodes={},messages=[];let stored='qwen2.5:7b',reads=0,writes=[];
 function reopen(){
  nodes['#local-model']={options:[],value:'',disabled:false,replaceChildren(...a){this.options=a;this.value=a.find(o=>o.selected)?.value||a[0]?.value||''}};
  nodes['#model-status']={textContent:''};nodes['#refresh-models']={disabled:false};
 }
 reopen();
 const backend={get_local_models:async()=>{reads++;return {models:['qwen2.5:7b','qwen3.5:4b','qwen3.5:9b'],selected:stored,available:true,error:''}},
  set_setting:async(k,v)=>{writes.push(v);stored=v;return true},get_settings:async()=>({local_model:stored})};
 const c=vm.createContext({settings:{local_model:stored},previewSettingVersion:0,page:'dictate',$:s=>nodes[s],
  Option:function(text,value,defaultSelected,selected){Object.assign(this,{text,value,defaultSelected,selected,disabled:false})},
  api:()=>backend,toast:message=>messages.push(message),renderChips:()=>{},syncSettings:()=>{}});
 vm.runInContext(source.slice(source.indexOf('let promptModelNames ='),source.indexOf('function chip('))
   +source.slice(settingStart,settingEnd)+source.slice(start,end),c);
 return {c,backend,messages,reopen,get select(){return nodes['#local-model']},get status(){return nodes['#model-status']},get button(){return nodes['#refresh-models']},
  get stored(){return stored},get reads(){return reads},get writes(){return writes},refresh:()=>vm.runInContext('refreshModels()',c)};
}
const failures=[];
async function check(name,run){try{await run();console.log('PASS '+name)}catch(error){failures.push(name+': '+error.message)}}
(async()=>{
 await check('offline inventory and legacy fixture compatibility',async()=>{
 let t=setup();t.backend.get_local_models=async()=>({models:[],selected:'qwen2.5:7b',available:false,error:'Ollama unavailable'});await t.refresh();
 assert(t.select.disabled,'offline inventory must not offer a verified model choice');assert.match(t.select.options[0].text,/saved|unverified|unavailable|not checked/i);assert.match(t.status.textContent,/unavailable/i);
 t.backend.get_local_models=async()=>({models:['qwen2.5:7b'],selected:'qwen2.5:7b',error:''});await t.refresh();assert(!t.select.disabled);
 });
 await check('complete inventory and missing saved selection',async()=>{
 const t=setup(),names=['llama3.1:8b','llama3.2:latest','qwen2.5:7b','qwen3.5:2b','qwen3.5:4b','qwen3.5:9b'];
 t.backend.get_local_models=async()=>({models:names,selected:'qwen2.5:7b',available:true,error:''});await t.refresh();
 assert.deepEqual(t.select.options.map(o=>o.value),names);assert(!t.select.disabled);assert.equal(t.select.value,'qwen2.5:7b');
 t.backend.get_local_models=async()=>({models:names,selected:'missing:4b',available:true,error:'Saved model not installed'});await t.refresh();assert.equal(t.select.value,'missing:4b');assert(t.select.options.find(o=>o.value==='missing:4b').disabled,'missing selection must not look installed');
 });
 await check('successful empty inventory is checked, not unknown',async()=>{
 const t=setup();t.backend.get_local_models=async()=>({models:[],selected:t.stored,available:true,error:'Saved model not installed'});await t.refresh();
 assert(t.select.disabled);assert.match(t.select.options[0].text,/not installed/);assert.doesNotMatch(t.select.options[0].text,/not checked/);
 });
 await check('late inventory and detached controls cannot overwrite current state',async()=>{
 const t=setup(),old=deferred();t.backend.get_local_models=()=>old.promise;const first=t.refresh();
 assert(t.select.disabled&&t.button.disabled,'refresh must show pending state');
 t.reopen();t.backend.get_local_models=async()=>({models:['qwen3.5:4b'],selected:'qwen3.5:4b',available:true,error:''});await t.refresh();
 old.resolve({models:['old:1b'],selected:'old:1b',available:true,error:''});await first;
 assert.equal(t.select.value,'qwen3.5:4b','late inventory replaced the latest choice');assert(!t.button.disabled);
 const stale=t.select;await t.refresh();t.reopen();await t.refresh();stale.value='old:1b';await stale.onchange();assert.equal(t.writes.length,0,'detached select saved a stale choice');
 });
 await check('confirmed save survives failed settings and inventory reads',async()=>{
 const t=setup();await t.refresh();t.backend.get_settings=async()=>{throw Error('settings read failed')};t.select.value='qwen3.5:4b';await t.select.onchange();
 assert.equal(t.stored,'qwen3.5:4b');assert.equal(t.select.value,t.stored);assert.equal(t.c.settings.local_model,t.stored);
 t.backend.get_local_models=async()=>{throw Error('inventory read failed')};t.select.value='qwen3.5:9b';await t.select.onchange();
 assert.equal(t.select.value,t.stored);assert.match(t.select.options[0].text,/saved/);assert(t.select.disabled&&!t.button.disabled);
 });
 await check('refused save restores the confirmed selection',async()=>{
 const t=setup();await t.refresh();t.backend.set_setting=async()=>false;t.select.value='qwen3.5:4b';await t.select.onchange();
 assert.equal(t.select.value,t.stored);assert(t.messages.some(m=>/could not be saved/.test(m)));
 });
 await check('save reservation survives Refresh and Settings reopening',async()=>{
 const t=setup();await t.refresh();const saved=t.backend.set_setting,gate=deferred(),stale=t.select;
 t.backend.set_setting=async(k,v)=>{await gate.promise;return saved(k,v)};stale.value='qwen3.5:4b';const pending=stale.onchange(),reads=t.reads;
 assert(t.select.disabled&&t.button.disabled);await t.refresh();assert.equal(t.reads,reads);assert(t.select.disabled&&t.button.disabled);
 stale.value='qwen3.5:9b';await stale.onchange();t.reopen();await t.refresh();assert(t.select.disabled&&t.button.disabled);assert.equal(t.reads,reads);
 gate.resolve();await pending;assert.deepEqual(t.writes,['qwen3.5:4b']);assert.equal(t.select.value,'qwen3.5:4b');assert(!t.select.disabled&&!t.button.disabled);
 t.select.value='qwen3.5:9b';await t.select.onchange();assert.equal(t.stored,'qwen3.5:9b');assert.equal(t.select.value,t.stored);
 });
 await check('unconfirmed save is not presented as a preserved saved choice',async()=>{
 const t=setup();await t.refresh();const saved=t.backend.set_setting;
 t.backend.set_setting=async(k,v)=>{await saved(k,v);throw Error('save acknowledgement lost')};t.backend.get_settings=async()=>{throw Error('settings unavailable')};t.backend.get_local_models=async()=>{throw Error('bridge unavailable')};
 t.select.value='qwen3.5:4b';await t.select.onchange();assert(t.select.disabled);assert.match(t.select.options[0].text,/last known|unconfirmed|not confirmed/i);assert.match(t.status.textContent,/save.*not.*confirmed/i);
 assert(!t.messages.some(m=>/previous choice is preserved/.test(m)));
 t.backend.get_local_models=async()=>({models:['qwen3.5:4b'],selected:t.stored,available:true,error:''});await t.refresh();assert.equal(t.select.value,t.stored);
 });
 assert.match(source,/Speech recognition model/);assert.match(source,/AI text model \(Ollama\)/);
 if(failures.length)throw Error(failures.join('\n'));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
raise SystemExit(subprocess.run([node, '-e', script, str(ROOT / 'ui/app.js')]).returncode)
