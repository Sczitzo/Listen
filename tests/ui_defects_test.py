"""UI defect regressions (focus, contrast tokens, toasts, history cap, polling, confirms). No browser, devices or providers.
Set UI_DEFECTS_ROOT to another checkout to run the same checks against it (used to prove they fail on old code)."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(os.environ.get('UI_DEFECTS_ROOT') or Path(__file__).resolve().parents[1])
ui = root / 'ui'
html = (ui / 'index.html').read_text(encoding='utf-8')
css = (ui / 'app.css').read_text(encoding='utf-8')
js = (ui / 'app.js').read_text(encoding='utf-8')
failures = []


def check(name, ok, detail=''):
    if not ok:
        failures.append(name + (': ' + detail if detail else ''))


# ---- static checks: tokens, markup, no native confirm ----
tokens = {}
for key, value in re.findall(r'--([a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{6})\b', css):
    tokens[key] = value  # last declaration wins, like the cascade for :root


def luminance(value):
    channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4 for c in channels]
    return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2]


def ratio(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + .05) / (lo + .05)


check('faint distinct from muted', tokens.get('faint') != tokens.get('muted'), f"faint={tokens.get('faint')} muted={tokens.get('muted')}")
for surface in ('bg', 'surface', 'surface-2', 'hover'):
    check(f'faint contrast on {surface}', ratio(tokens['faint'], tokens[surface]) >= 4.5, f"{ratio(tokens['faint'], tokens[surface]):.2f}")
check('control border token', 'line-ctl' in tokens and ratio(tokens['line-ctl'], tokens['surface']) >= 3.0)
check('inputs use control border', re.search(r'\.input \{[^}]*border: 1px solid var\(--line-ctl\)', css) is not None)
check('switch has visible outline', re.search(r'\.switch \{[^}]*border: 1px solid var\(--line-ctl\)', css) is not None)
check('no old red literal', 'rgba(229,72,77' not in css)
check('color-scheme dark', re.search(r'color-scheme:\s*dark', css) is not None)
check('select option styled', '.chip select option' in css)
check('single .page flex rule', len(re.findall(r'(?m)^\.page \{', css)) == 1 and '.main > .page' not in css)
check('no dead rm/prompt css', '.list li .rm' not in css and '.prompt.recording' not in css)
check('card titles are h2', re.search(r'<div class="card-title">(?!<h2)', html) is None)
check('quit uses svg not glyph', '⏻' not in html and 'href="#i-power"' in html)
check('history word count is spelled out, never a bare "w"', 'function wordLabel' in js and '.length}w' not in js and '}w${e.cost' not in js)
check('finish/cancel stay visible (disabled) while idle', all(re.search(r'<button\b[^>]*id="'+key+r'"[^>]*\bdisabled', html) for key in ('btn-finish','btn-cancel')))
check('how-it-works disclosure', '<details class="how"><summary>How it works</summary>' in html)
check('draft actions disclosure is a named button', 'id="btn-more" aria-label="More draft actions"' in html
      and 'aria-expanded="false" aria-controls="more-menu"' in html)
check('confirm dialog markup', 'id="confirm-dialog"' in html and 'id="confirm-message"' in html)
check('banner collapse', 'id="banners-toggle"' in html and 'max-height:600px' in css)
check('no native confirm( in app.js', re.search(r'(?<![\w.])confirm\(', js) is None)
check('aria-current on rail', 'aria-current' in js)
check('retained id escaped', 'data-copy-retained="${esc(r.id)}"' in js)
check('renderSettings only on page open', len(re.findall(r'renderSettings\(', js)) == 2, str(len(re.findall(r'renderSettings\(', js))))
check('visibility handling', 'visibilitychange' in js)
check('mock gated by demo', js.count('mock.set_text') == 1 and js.count('get_settings: async') == 0)

node = shutil.which('node')
assert node, 'Node is required'
script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const src=fs.readFileSync(process.argv[1],'utf8');
const cut=(a,b)=>{const starts=[].concat(a).map(x=>src.indexOf(x)).filter(i=>i>=0);assert(starts.length,'marker missing: '+a);const i=Math.min(...starts);const j=src.indexOf(b,i);assert(j>i,'marker missing: '+b);return src.slice(i,j);};
function el(){const cls=new Set(),attrs={};return {cls,attrs,children:[],dataset:{},textContent:'',innerHTML:'',value:'',style:{},focusCount:0,
 classList:{add:x=>cls.add(x),remove:x=>cls.delete(x),contains:x=>cls.has(x),toggle(x,v){v=v===undefined?!cls.has(x):v;v?cls.add(x):cls.delete(x);return v;}},
 setAttribute(k,v){attrs[k]=String(v);},removeAttribute(k){delete attrs[k];},getAttribute:k=>attrs[k],appendChild(c){this.children.push(c);},
 focus(){this.focusCount++;},remove(){},querySelector:()=>null,closest:()=>null};}
const registry=()=>{const m=new Map();return s=>{if(!m.has(s))m.set(s,el());return m.get(s);};};
const failed=[];
async function run(name,fn){try{await fn();}catch(e){failed.push(name+': '+String(e.message).split('\n')[0]);}}
const tick=()=>new Promise(r=>setImmediate(r));

(async()=>{
await run('chip focus kept after toggle re-render',async()=>{
 const R=registry(),active=el();active.dataset.toggle='cleanup';active.closest=s=>s==='.chips'?{id:'chips-dictate'}:null;
 const c=vm.createContext({document:{activeElement:active},settings:{cleanup:true,hotkey:'ctrl_r',style:'tighten'},options:{hotkey:{'Right Ctrl':'ctrl_r'},style:{},target:{},engine:{},judge:{}},esc:x=>String(x),$:R,$$:()=>[],setSetting(){}});
 vm.runInContext(cut('const htmlCache','// ---------- state')+cut('function chip(','async function setSetting('),c);
 vm.runInContext('renderChips()',c);
 assert.equal(R('#chips-dictate [data-toggle="cleanup"]').focusCount,1,'keyboard focus was not restored to the toggle chip');
});
await run('settings page updated in place (On/Off text, aria-checked)',async()=>{
 const sw=el(),label=el();sw.dataset.toggle='cleanup';sw.querySelector=()=>label;
 const sel=el();sel.dataset.key='style';sel.options=[{value:'clean'},{value:'tighten'}];sel.value='tighten';
 const c=vm.createContext({settings:{cleanup:false,style:'clean'},$:()=>null,$$:s=>s==='#settings-grid .switch'?[sw]:s==='#settings-grid select[data-key]'?[sel]:[],esc:x=>String(x),options:{},api:()=>({}),refreshModels(){},renderLearned(){},setSetting(){}});
 vm.runInContext(cut('const SETTING_ROWS =','async function refreshModels()'),c);
 vm.runInContext('syncSettings()',c);
 assert.equal(sw.attrs['aria-checked'],'false');assert.equal(label.textContent,'Off');assert(!sw.cls.has('on'));assert.equal(sel.value,'clean');
 c.settings.cleanup=true;vm.runInContext('syncSettings()',c);assert.equal(label.textContent,'On');assert(sw.cls.has('on'));
});
await run('renderSettings has four processing stages with the shared model under AI cleanup',async()=>{
 const R=registry();
 const c=vm.createContext({settings:{cleanup:true,live_preview:false},options:{},esc:x=>String(x),$:R,$$:()=>[],api:()=>({diagnostics:async()=>({})}),refreshModels(){},renderLearned(){},setSetting(){}});
 vm.runInContext(cut('const SETTING_ROWS =','async function refreshModels()')+'renderSettings()',c);
 const m=R('#settings-grid').innerHTML;
 assert(m.includes('>On</span>')&&m.includes('>Off</span>'),'switches lack visible On/Off text');
 assert(m.indexOf('id="stage-cleanup"')<m.indexOf('id="local-model"')&&m.indexOf('id="local-model"')<m.indexOf('id="stage-writing"'),'model row not under AI cleanup');
 assert.equal((m.match(/id="local-model"/g)||[]).length,1,'Shared local model must have one selection owner');
});
await run('setSetting on Settings page does not rebuild the page',async()=>{
 let rendered=0,synced=0;
 const c=vm.createContext({document:{activeElement:{dataset:{}}},api:()=>({set_setting:async()=>true,get_settings:async()=>({style:'clean'})}),settings:{},page:'settings',previewSettingVersion:0,
  renderChips(){},renderSettings(){rendered++;},syncSettings(){synced++;},toast(){},askConfirm:async()=>true});
 vm.runInContext(cut('async function setSetting(','// ---------- settings page'),c);
 await vm.runInContext('setSetting("style","clean")',c);
 assert.equal(rendered,0,'renderSettings re-ran after a change');assert.equal(synced,1);
});
await run('error toasts stay 8s, pause on hover, role=alert, dismissible',async()=>{
 const timers=[],cleared=[],toasts=el();
 const c=vm.createContext({document:{createElement:()=>el()},$:()=>toasts,setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout:id=>cleared.push(id)});
 vm.runInContext(cut(['const TOAST_MS','function toast('],'// ---------- pages ----------'),c);
 vm.runInContext('toast("boom",true)',c);const t=toasts.children[0];
 assert(timers[0].ms>=8000,'error toast closes after '+timers[0].ms+' ms');
 assert.equal(t.attrs.role,'alert');
 assert.equal(typeof t.onmouseenter,'function','no hover pause');const before=cleared.length;t.onmouseenter();assert(cleared.length>before);t.onmouseleave();assert(timers.at(-1).ms>=8000);
 const dismiss=t.children.find(x=>x.attrs&&x.attrs['aria-label']==='Dismiss');assert(dismiss,'no dismiss button');dismiss.onclick();assert(t.cls.has('out'));
 vm.runInContext('toast("ok")',c);assert.equal(timers.find(x=>x.ms<8000&&x.ms>=2000).ms,2400);
});
await run('Finish is primary while recording; Finish/Cancel stay visible but disabled while idle',async()=>{
 const R=registry(),status=el();status['.dot']=el();status['.status-text']=el();
 const c=vm.createContext({$:(s,r)=>r?r[s]:R(s),$$:s=>s==='.status'?[status]:[],settings:{cleanup:true,hotkey:'ctrl_r'},options:{hotkey:{'Right Ctrl':'ctrl_r'}},document:{body:el()}});
 vm.runInContext(cut('// ---------- polling ----------','let lastErr ='),c);
 const idle={state:'idle',busy:'',last_error:'',llm_ok:true,speech_runtime:{state:'ready'}};
 c.st=idle;vm.runInContext('setStatus(st)',c);
 assert(!R('#btn-finish').cls.has('hidden')&&!R('#btn-cancel').cls.has('hidden'),'Finish/Cancel must stay discoverable while idle');assert(R('#btn-finish').disabled&&R('#btn-cancel').disabled);
 c.st={...idle,state:'recording'};vm.runInContext('setStatus(st)',c);
 assert(!R('#btn-finish').cls.has('hidden')&&R('#btn-finish').cls.has('primary'),'Finish not primary while recording');assert(!R('#btn-cancel').cls.has('primary'));
 c.st=idle;vm.runInContext('setStatus(st)',c);assert(!R('#btn-finish').cls.has('primary'));
});
await run('history caps at 200 rows, Show more, 200 ms debounce, index lookup',async()=>{
 const R=registry(),timers=new Map();let n=0,copied=[];
 const entries=Array.from({length:450},(_,i)=>({ts:'2026-09-12T10:12:00',kind:'dictate',text:'entry '+i,raw:'raw '+i,cost:0}));
 const btn=el();btn.dataset.historyCopy='clean';btn.closest=()=>({dataset:{i:'3'}});
 const c=vm.createContext({console,esc:x=>String(x),$:s=>s==='#hist-more'?(R('#hist-list').innerHTML.includes('id="hist-more"')?R(s):null):s==='#clear-filter'?null:R(s),
  $$:(s)=>s==='[data-history-copy]'?[btn]:[],toast(){},
  api:()=>({get_history:async()=>entries,get_stats:async()=>({today:1,week:1,words_week:1,minutes_week:1,wpm:1,latency:1,spend_month:0}),copy_text:async t=>{copied.push(t);return true;}}),
  setTimeout:(fn,ms)=>{timers.set(++n,{fn,ms});return n;},clearTimeout:id=>timers.delete(id)});
 vm.runInContext(cut('function fmtTs(','async function clearPrivate('),c);
 await vm.runInContext('loadHistory()',c);
 const rows=()=>(R('#hist-list').innerHTML.match(/<li (?!class="empty")/g)||[]).length;
 assert.equal(rows(),200,'history rendered '+rows()+' rows');
 assert(R('#hist-list').innerHTML.includes('Show more'));
 assert(!/data-text=|data-raw=/.test(R('#hist-list').innerHTML),'rows still carry text copies');
 await btn.onclick();assert.equal(copied.at(-1),'entry 3','copy did not look up the entry by index');
 R('#hist-more').onclick();assert.equal(rows(),400);
 for(let i=0;i<3;i++)R('#hist-search').oninput();
 assert.equal(timers.size,1,'filter is not debounced');assert.equal([...timers.values()][0].ms,200);
});
await run('polling slows when hidden, restores on show, delivery not polled while recording',async()=>{
 const timers=[],handlers={};let deliv=0,doc={hidden:false,body:el(),addEventListener:(n,f)=>handlers[n]=f},state={state:'idle',busy:'',lab_ver:0,history_len:0,retained_ver:0,lab_busy:'',levels:[]};
 const c=vm.createContext({console,Promise,Date,document:doc,S:{},api:()=>({get_state:async()=>state}),bridgeLost:false,connection(){},setStatus(){},renderPreview(){},previewUiEpoch:0,
  loadDeliveryStatus:async()=>{deliv++;},retainedVersion:0,lastErr:'',lastLabVer:0,lastBusy:'',lastHistLen:0,lab:{},loadRetained:async()=>{},drawBars(){},loadLab:async()=>{},loadRecent(){},loadSuggestions(){},loadHistory(){},show(){},toast(){},page:'dictate',
  setTimeout:(fn,ms)=>{timers.push(ms);return 1;},clearTimeout(){}});
 vm.runInContext(cut('async function poll()','// ---------- boot ----------'),c);
 await vm.runInContext('poll()',c);assert.equal(timers.at(-1),250,'visible idle rate');
 await vm.runInContext('poll()',c);await vm.runInContext('poll()',c);assert.equal(deliv,1,'delivery status polled '+deliv+' times within one second');
 doc.hidden=true;await vm.runInContext('poll()',c);assert.equal(timers.at(-1),1000,'hidden rate is '+timers.at(-1));
 doc.hidden=false;
 const handler=src.split('\n').find(l=>l.startsWith('document.addEventListener("visibilitychange"'));assert(handler,'no visibilitychange handler');
 vm.runInContext(handler,c);const before=timers.length;handlers.visibilitychange();await tick();assert(timers.length>before&&timers.at(-1)===250,'normal rate not restored on show');
 doc.body.classList.add('recording');state={...state,state:'recording'};deliv=0;await vm.runInContext('poll()',c);await vm.runInContext('poll()',c);
 assert.equal(timers.at(-1),33);assert.equal(deliv,0,'delivery status polled while recording');
});
await run('an aborted lab load (owner typed mid-switch) is retried on the next poll, not marked handled',async()=>{
 const timers=[];let loads=0,results=[false,true,true],state={state:'idle',busy:'',lab_ver:5,history_len:0,retained_ver:0,lab_busy:'',levels:[]};
 const c=vm.createContext({console,Promise,Date,document:{hidden:false,body:el(),addEventListener(){}},S:{},api:()=>({get_state:async()=>state}),bridgeLost:false,connection(){},setStatus(){},renderPreview(){},previewUiEpoch:0,
  loadDeliveryStatus:async()=>{},retainedVersion:0,lastErr:'',lastLabVer:0,lastBusy:'',lastHistLen:0,lab:{},loadRetained:async()=>{},drawBars(){},loadLab:async()=>{loads++;return results.shift();},loadRecent(){},loadSuggestions(){},loadHistory(){},show(){},toast(){},page:'dictate',
  setTimeout:(fn,ms)=>{timers.push(ms);return 1;},clearTimeout(){}});
 vm.runInContext(cut('async function poll()','// ---------- boot ----------'),c);
 await vm.runInContext('poll()',c);assert.equal(loads,1);assert.equal(vm.runInContext('lastLabVer',c),0,'aborted load was marked handled');
 await vm.runInContext('poll()',c);assert.equal(loads,2,'aborted load was not retried');assert.equal(vm.runInContext('lastLabVer',c),5);
 await vm.runInContext('poll()',c);assert.equal(loads,2,'completed load was repeated');
});
await run('More actions menu anchors to its button, Escape returns focus',async()=>{
 const btn=el(),menu=el();menu.cls.add('hidden');menu.offsetWidth=300;btn.getBoundingClientRect=()=>({left:100,top:500});menu.addEventListener=()=>{};
 const c=vm.createContext({$:s=>s==='#btn-more'?btn:menu,innerWidth:1000,innerHeight:800,window:{addEventListener(){}}});
 vm.runInContext(cut('const moreBtn','moreMenu.addEventListener("click"'),c);
 btn.onclick();assert(!menu.cls.has('hidden'));assert.equal(btn.attrs['aria-expanded'],'true');assert.equal(menu.style.left,'100px');assert.equal(menu.style.bottom,'306px');
 assert.equal(vm.runInContext('closeMore(true)',c),true);assert(menu.cls.has('hidden'));assert.equal(btn.attrs['aria-expanded'],'false');assert.equal(btn.focusCount,1);
 assert.equal(vm.runInContext('closeMore(true)',c),false);
});
await run('confirm dialog resolves on Continue only and restores focus',async()=>{
 const dialog=el(),message=el(),opener=el();let closer;dialog.open=false;dialog.addEventListener=(t,f)=>closer=f;dialog.showModal=()=>{dialog.open=true;};
 const c=vm.createContext({$:s=>s==='#confirm-dialog'?dialog:message,document:{activeElement:opener},Promise});
 vm.runInContext(cut('function askConfirm(','// ---------- pages ----------'),c);
 let p=vm.runInContext('askConfirm("Clear history?")',c);assert.equal(message.textContent,'Clear history?');dialog.returnValue='ok';closer();assert.equal(await p,true);assert.equal(opener.focusCount,1);
 p=vm.runInContext('askConfirm("Again?")',c);dialog.returnValue='cancel';closer();assert.equal(await p,false);
});
if(failed.length){console.log('JS FAILURES\n - '+failed.join('\n - '));process.exitCode=1;}
else console.log('JS behaviour checks passed');
})();
'''
result = subprocess.run([node, '-e', script, str(ui / 'app.js')])
check('JS behaviour checks', result.returncode == 0, 'see JS FAILURES above')
if failures:
    print('FAILURES\n - ' + '\n - '.join(failures))
    sys.exit(1)
print('UI defect checks passed (tokens, markup, confirms, focus, toasts, history, polling, menu)')
