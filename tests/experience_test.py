"""Approved experience behavior, isolated fake providers; no paid or device calls."""
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d
from workspace_state import WorkspaceStore, CostLedger

folder = tempfile.mkdtemp()
d._workspace_store = WorkspaceStore(folder)
d._cost_ledger = CostLedger(folder)
for attr, name in [('HISTORY','history.jsonl'), ('SETTINGS_FILE','settings.json'),
                   ('FAVORITES','favorites.json'), ('LEARNED_FILE','learned.json')]:
    setattr(d, attr, os.path.join(folder, name))
d.SETTINGS['learn'] = False
d._chime = lambda *a: None
api = d.Api()

def wait():
    deadline = time.monotonic() + 3
    while api.get_lab()['busy'] and time.monotonic() < deadline:
        time.sleep(.01)
    assert not api.get_lab()['busy']

def judge(text):
    return dict(score=7, criteria={k:4 for k in d.CRITERION_HINT}, gaps=[],
                improved=text+' better', by='fake', cost=0, note='')

# Provider output stays a proposal. Editing invalidates Apply.
api.new_draft(); api.set_text('Original 17 files')
with patch.object(d, 'engineer', lambda *a: ('Suggestion 17 files','fake',0)), patch.object(d,'run_judge',judge):
    assert api.submit('make concise'); wait()
    lab = api.get_lab()
    assert lab['text'] == 'Original 17 files'
    assert lab['pending']['text'] == 'Suggestion 17 files'
    candidate = lab['pending']['id']
    api.set_text('Edited 17 files')
    assert not api.apply_candidate(candidate)
    assert api.get_lab()['text'] == 'Edited 17 files'
    assert api.discard_candidate(candidate)
    assert api.submit('try again'); wait()
    assert api.apply_candidate(api.get_lab()['pending']['id'])
    assert api.get_lab()['text'] == 'Suggestion 17 files'
    assert len(api.get_lab()['revs']) == 2

# Recovery preserves composer and drafts, without inference on restore.
lab = api.get_lab()
assert api.set_composer('Unsent instruction', lab['draft_id'], 2)
assert not api.set_composer('Older instruction', lab['draft_id'], 1)
with patch.object(d._workspace_store,'save',side_effect=OSError('disk full')):
    assert not api.set_composer('Retry without retyping',lab['draft_id'],3)
assert api.set_composer('Retry without retyping',lab['draft_id'],3)
assert not api.set_composer('Changed same version',lab['draft_id'],3)
assert api.set_composer('Unsent instruction',lab['draft_id'],4)
workspace = lab['workspace_id']
assert api.new_draft()
assert any(row['id'] == workspace for row in api.list_drafts())
assert api.restore_draft(workspace)
assert api.get_lab()['composer'] == 'Unsent instruction'
saved = d._workspace_store.load()
assert saved['active']['composer'] == 'Unsent instruction'
assert not api.clear_private_data('workspace', 'cancel')['ok']
assert api.get_lab()['text'] == 'Suggestion 17 files'

# Cloud denied means zero remote calls, even when a key/provider is configured.
d.SETTINGS.update(engine='anthropic', judge='anthropic', cloud_enabled=False)
with patch.object(d, '_claude', side_effect=AssertionError('Paid route reached')), \
     patch.object(d, '_chat', lambda *a:'local result'):
    assert d.engineer('description')[0] == 'local result'

# Installed model selection validates only inventory and never triggers inference.
response = type('Response', (), {'raise_for_status':lambda self:None,
                               'json':lambda self:{'models':[{'name':'qwen2.5:7b'},{'name':'qwen3.5:4b'}]}})()
with patch.object(d.requests,'get',return_value=response):
    assert api.set_setting('local_model','qwen3.5:4b')
    assert not api.set_setting('local_model','missing:9b')
    d.SETTINGS['local_model']='removed:4b'
    assert api.get_local_models()['error']
    assert api.set_setting('local_model','qwen2.5:7b'), 'A removed selected model must not block recovery selection'
redirect = type('Response', (), {'status_code':302,'raise_for_status':lambda self:None})()
with patch.object(d.requests,'get',return_value=redirect) as get:
    assert api.get_local_models()['error']
    assert get.call_args.kwargs['allow_redirects'] is False

# Dismissal is durable; a failed durable retention does not claim success.
assert api.retain_editor('Retained exactly')
item = api.get_retained()[-1]['id']
assert api.dismiss_retained(item)
assert not any(x['text']=='Retained exactly' for x in d._workspace_store.load()['retained'])
with patch.object(d._workspace_store,'save',side_effect=OSError('disk full')):
    assert not api.retain_editor('Still visible on failed recovery')
    retained_id = api.get_retained()[-1]['id']
    assert not api.dismiss_retained(retained_id)
    assert any(x['id']==retained_id for x in api.get_retained())

# Data cleared while a recording is pending must not return from its completion.
generation=(d._history_generation,d._recovery_generation)
assert api.clear_private_data('history','DELETE')['ok']
with patch.object(d,'transcribe',lambda *a:'old private speech'),patch.object(d,'refine',lambda t:t), \
     patch.object(d,'paste') as paste:
    d.process([],1,d.time.perf_counter(),None,generation)
    assert not paste.called and api.get_history()==[]

# Favorite conflict/Undo never silently replaces a newer favorite.
assert api.save_favorite_checked('A','first')['status'] == 'saved'
assert api.save_favorite_checked('A','second')['status'] == 'conflict'
token = api.remove_favorite('A')
assert token and api.save_favorite_checked('A','new')['status'] == 'saved'
assert not api.undo_favorite_removal(token)
assert api.get_favorites()[0]['text'] == 'new'
assert api.clear_private_data('workspace','DELETE')['ok']
assert api.get_lab()['text'] == '' and api.get_lab()['pending'] is None
assert not api.restore_draft(workspace)
# A delayed manual Paste must not restore cleared draft text into recovery.
queued = []
class DelayedThread:
    def __init__(self,target,**kwargs): self.target=target
    def start(self): queued.append(self.target)
d._window_target = 123
d._win = type('Window',(),{'hide':lambda self:None})()
with patch.object(d.threading,'Thread',DelayedThread),patch.object(d.time,'sleep',lambda *a:None), \
     patch.object(d,'show_window',lambda *a:None), \
     patch.object(d,'paste_text',return_value={'status':'retained','message':'destination changed'}):
    assert api.paste_draft('deleted manual draft')
    assert api.clear_private_data('workspace','DELETE')['ok']
    queued.pop()()
    assert api.get_retained()==[] and d._workspace_store.load()['retained']==[]
# Paragraphs survive the exact production cleanup response parser.
response = type('Response', (), {'raise_for_status':lambda self:None,
    'json':lambda self:{'message':{'content':'First paragraph.\n\nSecond paragraph.'},'done_reason':'stop'}})()
with patch.object(d.requests,'post',return_value=response):
    assert '\n\n' in d._chat('system','raw')
print('experience_test: passed')
