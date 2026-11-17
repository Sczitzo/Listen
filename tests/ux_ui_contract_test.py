from pathlib import Path
root=Path(__file__).resolve().parents[1]/'ui'
h=(root/'index.html').read_text(encoding='utf-8'); j=(root/'app.js').read_text(encoding='utf-8'); c=(root/'app.css').read_text(encoding='utf-8')
for value in ['id="demo-banner"','for="draft"','for="composer"','id="candidate"','id="recovery-status"','<dialog','id="btn-finish"','id="btn-cancel"']:
    assert value in h, value
for value in ['set_composer','apply_candidate','discard_candidate','save_favorite_checked','undo_favorite_removal','clear_private_data','get_local_models','booted','bridgeLost','restore_draft']:
    assert value in j, value
assert 'setTimeout(() => { if (!window.pywebview) boot(); }, 300)' not in j
assert 'prefers-reduced-motion' in c
assert 'const name=prompt(' not in j, 'Save must work where browser prompt() is unsupported'
assert 'id="favorite-name-dialog"' in h and 'for="favorite-name"' in h
print('UI contract checks passed')
import ast
module=ast.parse((root.parent/'dictate.py').read_text(encoding='utf-8'))
version=next(node.value.value for node in module.body if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='UI_VERSION' for t in node.targets))
assert f'app.css?v={version}' in h and f'app.js?v={version}' in h, 'UI asset versions must match the launched page'
assert 'id="btn-quit"' in h and 'requestDictationQuit' in j
assert 'judge_model' in j and 'claude-haiku-5-5' in j and 'claude-sonnet-5-5' in j

# Execute synthetic endpoint behavior without a DOM, network, provider or clipboard.
import shutil, subprocess
if shutil.which('node'):
    script = r'''
const fs=require('node:fs'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const prefix=source.slice(0,source.indexOf('const demo ='));
const realTimeout=global.setTimeout;global.setTimeout=(fn)=>{fn();return 0;};
const m=eval(prefix+'return mock;})();');
(async()=>{
const original=m._lab.text;await m.improve();assert.equal(m._lab.text,original);assert.ok(m._lab.pending);
let id=m._lab.pending.id;await m.set_text('edited',m._lab.draft_id);assert.equal(await m.apply_candidate(id),false);assert.equal(m._lab.text,'edited');
await m.discard_candidate(id);await m.improve();id=m._lab.pending.id;assert.equal(await m.apply_candidate(id),true);assert.match(m._lab.text,/focused check/);
const previous=m._lab.draft_id;await m.set_composer('Keep my numbers',previous,1);await m.new_draft();assert.equal(await m.set_composer('wrong draft',previous,2),false);assert.equal((await m.list_drafts()).length,1);assert.equal(await m.restore_draft(previous),true);assert.equal(m._lab.composer,'Keep my numbers');
assert.equal((await m.save_favorite_checked('Same','first')).status,'saved');assert.equal((await m.save_favorite_checked('Same','second')).status,'conflict');const token=await m.remove_favorite('Same');await m.save_favorite_checked('Same','newer');assert.equal(await m.undo_favorite_removal(token),false);
const before=JSON.stringify(m._archives);assert.equal((await m.clear_private_data('workspace','CANCEL')).ok,false);assert.equal(JSON.stringify(m._archives),before);
console.log('Synthetic bridge behavior checks passed');global.setTimeout=realTimeout;
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    subprocess.run(['node','-e',script,str(root/'app.js')],check=True)
else:
    print('Node unavailable: synthetic endpoint behavior checks not run')

from html.parser import HTMLParser
class IdParser(HTMLParser):
    def __init__(self): super().__init__(); self.ids=[]; self.labels=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if 'id' in a: self.ids.append(a['id'])
        if tag=='label' and 'for' in a: self.labels.append(a['for'])
parser=IdParser();parser.feed(h)
assert len(parser.ids)==len(set(parser.ids)), 'duplicate element ids'
assert all(value in parser.ids for value in parser.labels), 'label target missing'
assert j.count('setTimeout(poll,')==1, 'poll must own one scheduled continuation'
assert 'if(booted)return;booted=true;' in j
assert 'source_text!==ed.value||editorDirty' in ''.join(j.split())
assert 'finally {sending=false;' in j
print('Markup identity and guarded-operation checks passed')
