"""Judge selection and pricing; isolated runner only, all provider calls faked."""
import json,sys,threading,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
assert ROOT.name.startswith('.test-run-'), 'Use tests/run_tests.py'
sys.path.insert(0,str(ROOT))
import dictate as d
from safe_storage import valid_setting,validated_settings

assert d.SETTINGS['judge_model']=='claude-haiku-5-5'
assert d.JUDGE_MODEL=='claude-haiku-5-5'
api=d.Api();d._chime=lambda *a:None
original_engine=d.ENGINE_MODEL
assert set(api.get_options()['judge_model'].values())=={'claude-haiku-5-5','claude-sonnet-5-5'}
assert not api.set_setting('judge_model','unknown-paid-model')
assert api.set_setting('judge_model','claude-sonnet-5-5')
assert json.loads(Path(d.SETTINGS_FILE).read_text())['judge_model']=='claude-sonnet-5-5'
assert validated_settings({'judge_model':'unknown'}, {'judge_model':d.JUDGE_MODEL})['judge_model']==d.JUDGE_MODEL
assert valid_setting('judge_model','claude-haiku-5-5')
score=dict(score=7,criteria={k:4 for k in d.CRITERION_HINT},gaps=[],improved='Preserved draft')
seen=[]
def fake_claude(model,system,user,schema=None):
    seen.append((model,schema));return json.dumps(score),0.001
with patch.object(d,'_claude',fake_claude):
    result=d._judge_anthropic('Preserved draft')
    assert result['by']=='claude-sonnet-5-5' and seen[-1][1] is d.JUDGE_SCHEMA
    assert api.set_setting('judge_model','claude-haiku-5-5')
    assert d._judge_anthropic('Preserved draft')['by']=='claude-haiku-5-5'
    entered,release=threading.Event(),threading.Event()
    def delayed():
        entered.set();assert release.wait(3);d._judge_anthropic('Preserved draft')
    assert d.lab_run('scoring',delayed);assert entered.wait(3)
    assert api.set_setting('judge_model','claude-sonnet-5-5')
    release.set();end=time.monotonic()+3
    while d._lab['busy'] and time.monotonic()<end:time.sleep(.01)
    assert not d._lab['busy'] and seen[-1][0]=='claude-haiku-5-5'
assert d.ENGINE_MODEL==original_engine
assert 'claude-sonnet-5-5' in api.diagnostics()['prompt lab']
usage=SimpleNamespace(input_tokens=1000,output_tokens=200)
response=SimpleNamespace(usage=usage,stop_reason='end_turn',content=[SimpleNamespace(type='text',text='{}')])
calls=[]
def create(**kwargs):calls.append(kwargs);return response
fake_sdk=SimpleNamespace(Anthropic=lambda **kwargs:SimpleNamespace(messages=SimpleNamespace(create=create)))
with patch.dict(sys.modules,anthropic=fake_sdk):
    d.SETTINGS['cloud_enabled']=False
    try:d._claude('claude-haiku-5-5','system','synthetic',d.JUDGE_SCHEMA)
    except PermissionError:pass
    else:raise AssertionError('Cloud consent bypassed')
    assert not calls
    d.SETTINGS['cloud_enabled']=True
    _,cost=d._claude('claude-haiku-5-5','system','synthetic',d.JUDGE_SCHEMA)
    assert abs(cost-0.0002)<1e-12
    usage.input_tokens=100000
    _,cost=d._claude('claude-haiku-5-5','system','synthetic',d.JUDGE_SCHEMA)
    assert abs(cost-(100000*.1+200*.5)/1e6)<1e-12
    usage.cache_read_input_tokens=1
    _,cost=d._claude('claude-haiku-5-5','system','synthetic',d.JUDGE_SCHEMA)
    assert abs(cost-(100000*.5+200*2.5)/1e6)<1e-12
    usage.cache_read_input_tokens=0
    usage.input_tokens=100001
    _,cost=d._claude('claude-haiku-5-5','system','synthetic',d.JUDGE_SCHEMA)
    assert abs(cost-(100001*.5+200*2.5)/1e6)<1e-12
print('judge model selection, snapshot, consent and pricing: passed')
