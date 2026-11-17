"""Actual decoder child protocol with a fake Parakeet library; no native weights."""
import ast
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import speech_process as speech
from speech_runtime import SpeechUnavailableError

NAME = 'parakeet-unified-en-0.6b'
source = ast.parse((ROOT / 'dictate.py').read_text(encoding='utf-8-sig'))
factory = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'load_speech_process')
calls = []
class FakeRuntime:
    def __init__(self, name, **options): calls.append(('new', name, options))
    def initialize(self, device): calls.append(('initialize', device)); return self
    def close(self): return True
scope = dict(SpeechProcessRuntime=FakeRuntime, _mic_lock=threading.RLock(),
             _quitting=False, _starting_speech=None, SpeechUnavailableError=SpeechUnavailableError)
exec(compile(ast.Module(body=[factory], type_ignores=[]), 'actual-speech-factory', 'exec'), scope)
scope['load_speech_process'](NAME)
assert calls[-1] == ('initialize', 'cpu'), 'Parakeet must not use CUDA admission/retry semantics'
assert calls[-2][2]['preferred_device'] == 'cpu'
calls.clear(); scope['load_speech_process']('small')
assert calls[-1] == ('initialize', 'cuda'), 'Whisper startup policy changed'

FAKE = '''
import os,time
from types import SimpleNamespace
from pathlib import Path
from speech_runtime import SpeechUnavailableError
MODEL_NAME='parakeet-unified-en-0.6b'
def cached_model():return str(Path(__file__).with_name('cached.gguf').resolve())
class ParakeetRuntime:
 def __init__(self,snapshot):
  assert snapshot==cached_model()
  if os.environ.get('PARAKEET_TEST_MODE')=='startup_hang':time.sleep(4)
 def status(self):return dict(model=MODEL_NAME,device='cpu',compute_type='Q8_0',fallback=False,state='ready',reason='')
 def close(self):pass
 def transcribe(self,audio,**options):
  if os.environ.get('PARAKEET_TEST_MODE')=='hang':time.sleep(4)
  if os.environ.get('PARAKEET_TEST_MODE')=='malformed':return [SimpleNamespace(text=17)],None
  return [SimpleNamespace(text='sample '+str(round(float(audio[0]),2)),avg_logprob=0.,no_speech_prob=0.,compression_ratio=0.)],None
 def transcribe_preview(self,audio,**options):
  if os.environ.get('PARAKEET_TEST_MODE')=='preview_hang':time.sleep(4)
  return self.transcribe(audio,**options)
'''

@contextmanager
def fixture(mode='normal'):
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for name in ('speech_process_worker.py', 'speech_runtime.py'):
            shutil.copyfile(ROOT / name, folder / name)
        (folder / 'parakeet_runtime.py').write_text(FAKE.replace("os.environ.get('PARAKEET_TEST_MODE')", repr(mode)), encoding='utf-8')
        (folder / 'cached.gguf').write_bytes(b'fake cached model')
        with patch.object(speech, 'WORKER', folder / 'speech_process_worker.py'):
            yield

def load(timeout=1):
    return speech.load_speech_process(NAME, preferred_device='cpu', startup_timeout=2, inference_timeout=timeout)

with fixture():
    runtime = load()
    session = runtime._session
    try:
        assert runtime.status()['compute_type'] == 'Q8_0' and not runtime.status()['fallback']
        first, _ = runtime.transcribe(np.full(16000, .1, dtype=np.float32), language='en')
        second, _ = runtime.transcribe_preview(np.full(16000, .2, dtype=np.float32), language='en')
        assert [x.text for x in first] == ['sample 0.1']
        assert [x.text for x in second] == ['sample 0.2'], 'A distinct request reused old recognition'
    finally:
        assert runtime.close()
    assert session.process.poll() is not None

for mode in ('startup_hang', 'malformed'):
    with fixture(mode):
        runtime = speech.SpeechProcessRuntime(NAME, preferred_device='cpu', startup_timeout=.2, inference_timeout=.2)
        try:
            try:
                runtime.initialize('cpu')
                runtime.transcribe(np.ones(16000, dtype=np.float32), language='en')
                raise AssertionError('Invalid or stalled child unexpectedly succeeded')
            except SpeechUnavailableError:
                pass
        finally:
            session = runtime._session
            assert runtime.close()
            assert session is None or session.process.poll() is not None

with fixture('preview_hang'):
    runtime = load(.2)
    old = runtime._session
    try:
        try:
            runtime.transcribe_preview(np.ones(16000, dtype=np.float32), language='en')
            raise AssertionError('Preview timeout was ignored')
        except SpeechUnavailableError:
            pass
        assert old.process.poll() is not None
        snapshot = runtime._snapshot
        segments, _ = runtime.transcribe(np.full(16000, .3, dtype=np.float32), language='en')
        assert [s.text for s in segments] == ['sample 0.3'] and runtime._snapshot == snapshot
        assert runtime.status()['device'] == 'cpu'
    finally:
        session = runtime._session
        assert runtime.close()
        assert session is None or session.process.poll() is not None

with fixture('hang'):
    runtime = load(2)
    session = runtime._session
    published = []
    def pending():
        try: published.extend(runtime.transcribe(np.ones(16000, dtype=np.float32), language='en')[0])
        except SpeechUnavailableError: pass
    worker = threading.Thread(target=pending)
    worker.start(); time.sleep(.1)
    assert runtime.close(); worker.join(2)
    assert not worker.is_alive() and not published and session.process.poll() is not None

print('Parakeet process: CPU factory, distinct requests, precision, startup/preview timeout, malformed output, close and no late publication passed')
