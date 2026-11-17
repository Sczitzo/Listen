"""Deterministic startup failures/retry without GPU, devices or listeners."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dictate as d

calls={'worker':0,'listener':0,'model':0,'attempt':0}
cache=Path(tempfile.mkdtemp())
for name in ('model.bin','config.json','tokenizer.json'):
    (cache/name).write_text('synthetic local snapshot',encoding='utf-8')
class Thread:
    def __init__(self,target,**kwargs): self.target=target
    def start(self):
        if self.target is d.worker: calls['worker']+=1
        elif self.target is d._hotkey_worker: pass
        else: self.target()
    def is_alive(self): return True
class Listener:
    def __init__(self,**kwargs): calls['listener']+=1; self.running=False
    def start(self): self.running=True
    def is_alive(self): return self.running
class Response:
    def __init__(self,failed=False): self.failed=failed
    def raise_for_status(self):
        if self.failed: raise d.requests.HTTPError('Synthetic missing model')
def loader(*args,**kwargs):
    calls['model']+=1
    if kwargs['device']=='cuda': calls['attempt']+=1
    if calls['attempt']==1: raise RuntimeError('Synthetic GPU and CPU unavailable')
    class Model:
        def transcribe(self,*args,**kwargs): return iter(()),None
    return Model()

d._state='loading'; d._listener=None; d._worker_started=False
d._llm_ok=True; d._last_error=''
def backend(name):
    return d.load_speech_runtime(name, model_factory=loader,
        cache_resolver=lambda *args, **kwargs: str(cache), compute_types=lambda device: {'int8'})

with patch.object(d,'load_speech_process',backend),patch.object(d.threading,'Thread',Thread), \
     patch.object(d.pk,'Listener',Listener),patch.object(d,'_chime',lambda *a:None), \
     patch.object(d,'_text_native',return_value=None), \
     patch.object(d.cleanup_http,'warm_json',side_effect=d.requests.HTTPError('Synthetic missing model')):
    d._initialize_runtime()
    assert d._state=='loading' and d._last_error and not d._startup_lock.locked()
    assert calls['worker']==calls['listener']==0
    assert calls['model']==2, 'Both devices must fail before explicit startup Retry is needed'
    assert d.Api().retry_startup()
    assert d._state=='idle' and not d._last_error
    assert calls['worker']==calls['listener']==1
    assert not d._llm_ok, 'An HTTP failure must not report cleanup ready'
    assert not d.Api().retry_startup(), 'Ready startup cannot be retried from UI'
    d._cleanup_policy = d.CleanupPolicy()
    warm_calls = []
    def warm_with_recording(payload, timeout):
        warm_calls.append((payload, timeout))
        if len(warm_calls) == 1:
            d._warm_llm()
        return {}
    with patch.object(d.cleanup_http,'warm_json',side_effect=warm_with_recording):
        d._initialize_runtime()
    assert len(warm_calls) == 1, 'Startup and recording did not share warm ownership'
    assert warm_calls[0][0]['options']['num_batch'] == 128
    assert warm_calls[0][1] == 60
    assert d._llm_ok and calls['worker']==calls['listener']==1
    with patch.object(d.cleanup_http, 'warm_json', side_effect=AssertionError('Recent healthy warm must be reused')):
        d._initialize_runtime()
    assert d._llm_ok, 'Speech reinitialization erased healthy cleanup readiness'
    d._startup_lock.acquire()
    try:
        before=calls.copy(); d._initialize_runtime()
        assert calls==before, 'Concurrent initialization must not duplicate services'
    finally: d._startup_lock.release()
print('startup recovery: passed')
