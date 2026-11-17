"""Actual-source display transitions and repeated DOM work, no browser/devices/providers."""
from pathlib import Path
import shutil
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
source=next((Path(arg) for arg in sys.argv[1:] if arg.endswith(('.js','.js.txt'))),root/'ui/app.js')
assert shutil.which('node'), 'Existing Node runtime required; do not silently skip rendering verification'
script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
let writes=0;
class Element {
 constructor(){this.values={className:'',textContent:'',title:'',disabled:false,height:''};this.classes=new Set();this.children=[];
  for(const key of ['className','textContent','title','disabled'])Object.defineProperty(this,key,{get:()=>this.values[key],set:value=>{writes++;this.values[key]=value;}});
  // CSS normalizes floating-point strings; mirror the relevant behavior here.
  this.style={};Object.defineProperty(this.style,'height',{get:()=>this.values.height,set:value=>{writes++;this.values.height=Number.parseFloat(value).toPrecision(6).replace(/\.?0+$/,'')+'px';}});
  this.classList={contains:key=>this.classes.has(key),toggle:(key,on)=>{const value=on??!this.classes.has(key);if(value!==this.classes.has(key)){writes++;value?this.classes.add(key):this.classes.delete(key);}return value;}};
  Object.defineProperty(this,'innerHTML',{set:value=>{writes++;this.children=Array.from({length:(value.match(/<i>/g)||[]).length},()=>new Element());}});
 }
}
const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const statuses=[new Element(),new Element()];for(const el of statuses){el.dot=new Element();el.label=new Element();}
const context=vm.createContext({settings:{cleanup:true,hotkey:'ctrl_r'},options:{hotkey:{'Right Ctrl':'ctrl_r'}},document:{body:get('body')},
 $:(selector,root)=>root?(selector==='.dot'?root.dot:root.label):get(selector),
 $$:(selector,root)=>selector==='i'?root.children:selector==='.status'?statuses:[],console});
const bars=source.slice(source.indexOf('// ---------- bars ----------'),source.indexOf('// ---------- chips ----------'));
const status=source.slice(source.indexOf('// ---------- polling ----------'),source.indexOf('let lastErr ='));
assert(bars.includes('function drawBars(')&&status.includes('function setStatus('));
vm.runInContext(bars+status,context);
function render(state,levels=[]){context.state=state;context.levels=levels;vm.runInContext('setStatus(state);drawBars(levels)',context);}
const idle={state:'idle',busy:'',last_error:'',llm_ok:true,speech_retry_ready:false,speech_runtime:{state:'ready'}};
render(idle);const first=writes;for(let i=0;i<100;i++)render(idle);const idleRepeated=writes-first;
const record={...idle,state:'recording',locked:true};render(record,[.1,.2]);
assert.equal(statuses[0].label.textContent,'recording · hands-free');assert.equal(get('#btn-finish').disabled,false);assert.equal(get('#btn-cancel').disabled,false);
const recordingFirst=writes;for(let i=0;i<100;i++)render(record,[.1,.2]);const recordingRepeated=writes-recordingFirst;
const heights=get('#bars-dictate').children.map(e=>e.style.height);assert.equal(heights.at(-1),'26px');
render(record,[.2,.01]);assert.notEqual(get('#bars-dictate').children.at(-1).style.height,heights.at(-1));
render({...idle,state:'recording',locked:false});assert.equal(statuses[1].label.textContent,'recording');assert(context.document.body.classList.contains('recording'));
render({...idle,busy:'engineering'});assert.equal(statuses[1].label.textContent,'engineering');assert.equal(get('#rail-status').title,'engineering');
render({...idle,state:'loading'});assert.equal(statuses[0].label.textContent,'loading…');
render({...idle,preview_busy:true,pending_dictations:0});assert.equal(statuses[0].label.textContent,'stopping preview…');
render({...idle,preview_busy:true,pending_dictations:1});assert.equal(statuses[0].label.textContent,'finishing dictation…');assert.equal(statuses[0].dot.className,'dot busy');
render({...idle,last_error:'failed'});assert.equal(statuses[0].label.textContent,'error');assert.equal(statuses[0].dot.className,'dot error');
render({...idle,state:'loading',last_error:'failed',speech_runtime:{state:'failed'},speech_retry_ready:true});
// Preserve the existing error precedence; this pass does not change status wording.
assert.equal(statuses[0].label.textContent,'error');assert.equal(get('#btn-retry').disabled,false);assert.equal(get('#btn-retry').classList.contains('hidden'),false);
render({...idle,speech_runtime:{state:'ready',device:'cpu',fallback:true},speech_summary:'Whisper Small CPU'});
assert.match(get('#speech-runtime-status').textContent,/processor/);assert.equal(get('#diag-speech').textContent,'Whisper Small CPU');
render({...idle,llm_ok:false});assert.match(statuses[0].label.textContent,/offline/);
context.settings.cleanup=false;render({...idle,llm_ok:false});assert.equal(statuses[0].label.textContent,'idle');context.settings.cleanup=true;
render(idle);assert.equal(get('#btn-finish').disabled,true);assert.equal(get('#speech-runtime-status').classList.contains('hidden'),true);assert.equal(get('#recording-announcement').textContent,'idle');
assert(get('#bars-dictate').children.every(e=>e.style.height==='4px'));assert.deepEqual(get('#bars-dictate').children.map(e=>e.style.height),get('#bars-lab').children.map(e=>e.style.height));
get('#bars-dictate').children[0].style.height='99px';render(idle);assert.equal(get('#bars-dictate').children[0].style.height,'4px');
get('#bars-lab').children[0]=new Element();render(idle);assert.equal(get('#bars-lab').children[0].style.height,'4px');
render(record,Array(40).fill(.2));assert(get('#bars-lab').children.every(e=>e.style.height==='26px'));render(idle,[]);assert(get('#bars-lab').children.every(e=>e.style.height==='4px'));
console.log(JSON.stringify({idle_100_repeated_assignments:idleRepeated,recording_100_repeated_assignments:recordingRepeated,transitions:'passed'}));
if(process.argv[2]!=='measure'){assert.equal(idleRepeated,0,'Unchanged idle state rewrites DOM properties');assert.equal(recordingRepeated,0,'Unchanged recording state rewrites DOM properties');}
'''
subprocess.run(['node','-e',script,str(source),'measure' if '--measure' in sys.argv else 'verify'],check=True)
