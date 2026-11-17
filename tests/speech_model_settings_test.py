"""Speech choices and lifecycle changes in the guarded disposable app copy."""
import importlib.util
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
assert ROOT.name.startswith('.test-run-'), 'Use tests/run_tests.py --no-devices'
sys.path.insert(0, str(ROOT))
import dictate as d
import speech_runtime as speech
from safe_storage import validated_settings, valid_setting

PARAKEET = speech.DEFAULT_SPEECH_MODEL
CHOICES = {speech.speech_model_label(PARAKEET): PARAKEET, 'Whisper Small': 'small'}


class Runtime(d.SpeechProcessRuntime):
    def __init__(self, name, *, state='ready', closes=True):
        self.name, self.phase, self.closes, self.closed = name, state, closes, 0
    def status(self):
        return {'model': self.name, 'state': self.phase, 'device': 'cpu',
                'compute_type': 'Q8_0' if self.name == PARAKEET else 'int8',
                'fallback': False, 'reason': ''}
    def close(self):
        self.closed += 1
        if self.closes:
            self.phase = 'failed'
        return self.closes


class Listener:
    def is_alive(self): return True


base = dict(d.SETTINGS)
def reset():
    d.SETTINGS = dict(base, speech_model=PARAKEET, local_model='qwen3.5:4b')
    d.MODEL_SIZE = PARAKEET
    d._state, d._busy, d._last_error = 'idle', '', ''
    d._lab['busy'] = ''
    d._recording.clear()
    d._pending_recordings = d._pending_pastes = 0
    d._jobs = queue.Queue()
    d._startup_pending = d._waking = d._sleeping = d._speech_changing = False
    d._speech_asleep = d._quitting = False
    d._starting_speech = None
    d._open_thread = d._stream = None
    d._streams = {}
    d._startup_lock = threading.Lock()
    d._listener, d._worker_started = Listener(), True
    d.model = Runtime(PARAKEET)
    d.atomic_write_json(d.SETTINGS_FILE, d.SETTINGS)
    return d.model


assert d.SETTINGS['speech_model'] == PARAKEET and d.MODEL_SIZE == PARAKEET
assert valid_setting('speech_model', 'small')
for invalid in ('qwen3.5:4b', '../small', '', None, 1, True):
    assert not valid_setting('speech_model', invalid)
assert validated_settings({'speech_model': 'small'}, {'speech_model': PARAKEET})['speech_model'] == 'small'
assert validated_settings({'speech_model': 'qwen2.5:7b'}, {'speech_model': PARAKEET})['speech_model'] == PARAKEET

with patch.object(d, 'cached_speech_models', return_value=CHOICES), \
     patch.object(d.Api, 'get_microphones', return_value={}):
    old = reset()
    new = Runtime('small')
    with patch.object(d, 'load_speech_process', return_value=new) as load:
        assert d.Api().set_setting('speech_model', 'small')
    assert load.call_args.args == ('small',)
    assert d.model is new and old.closed == 1
    assert d.MODEL_SIZE == d.SETTINGS['speech_model'] == 'small'
    assert json.loads(Path(d.SETTINGS_FILE).read_text())['speech_model'] == 'small'
    options = d.Api().get_options()
    assert options['speech_models'] == CHOICES
    assert options['speech_model'] == 'small' and options['speech_model_label'] == 'Whisper Small'
    assert not d.Api().get_state()['speech_model_switching']
print('speech model settings: initial switch and persistence passed')

# A fresh import reads the saved choice without opening the desktop application.
spec = importlib.util.spec_from_file_location('speech_choice_restart_fixture', ROOT / 'dictate.py')
fresh = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fresh
try:
    spec.loader.exec_module(fresh)
    assert fresh.SETTINGS['speech_model'] == fresh.MODEL_SIZE == 'small'
    assert fresh._draft.close(timeout=2) and fresh._field_access.close(timeout=2)
finally:
    sys.modules.pop(spec.name, None)

# Inventory reads small refs/stat metadata, never native libraries or model bytes.
home = Path(tempfile.mkdtemp())
cache = home / 'hub'
revision = 'a' * 40
small = cache / 'models--Systran--faster-whisper-small'
(small / 'refs').mkdir(parents=True)
(small / 'refs/main').write_text(revision)
snapshot = small / 'snapshots' / revision
snapshot.mkdir(parents=True)
for name in ('model.bin', 'config.json', 'tokenizer.json'):
    (snapshot / name).write_bytes(b'x')
import parakeet_runtime
parakeet = home / '.cache/huggingface/hub/models--handy-computer--parakeet-unified-en-0.6b-gguf/snapshots' / parakeet_runtime.REVISION / 'parakeet-unified-en-0.6b-Q8_0.gguf'
parakeet.parent.mkdir(parents=True)
parakeet.write_bytes(b'x')
native = home / 'AppData/Local/Listen/runtimes/transcribe-0.4.2-cpu-vulkan/transcribe.dll'
native.parent.mkdir(parents=True)
native.write_bytes(b'x')
with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, HF_HUB_CACHE=str(cache)), \
     patch.object(parakeet_runtime, 'cached_model', side_effect=AssertionError('Inventory must not hash a model')):
    assert speech.cached_speech_models() == CHOICES
    (snapshot / 'tokenizer.json').write_bytes(b'')
    assert speech.cached_speech_models() == {speech.speech_model_label(PARAKEET): PARAKEET}
    (snapshot / 'tokenizer.json').write_bytes(b'x')
    (small / 'refs/main').write_text('../../outside')
    assert 'small' not in speech.cached_speech_models().values()
    (small / 'refs/main').write_text(revision + ' ' * 1000)
    assert 'small' not in speech.cached_speech_models().values(), 'Oversized cache refs are not cheap metadata'
    native.write_bytes(b'')
    assert speech.cached_speech_models() == {}
print('PASS cached choices, incomplete assets, invalid refs, no model hashing and restart persistence')

with patch.object(d, 'cached_speech_models', return_value=CHOICES):
    old = reset()
    with patch.object(d, '_speech_status', side_effect=RuntimeError('Synthetic status failure')):
        assert not d.Api().set_setting('speech_model', PARAKEET)
    assert d._state == 'idle' and d.model is old and not old.closed and not d._startup_lock.locked()
    for invalid in ('medium', 'qwen3.5:4b', '../small', None, 1):
        with patch.object(d, 'load_speech_process') as load:
            assert not d.Api().set_setting('speech_model', invalid)
            load.assert_not_called()
        assert d.model is old and not old.closed and d.MODEL_SIZE == PARAKEET
    for field, value in (('_pending_recordings', 1), ('_pending_pastes', 1), ('_busy', 'cleaning'),
                         ('_startup_pending', True), ('_waking', True), ('_sleeping', True),
                         ('_speech_changing', True), ('_quitting', True), ('_state', 'recording'),
                         ('_state', 'loading'), ('_starting_speech', Runtime('small')),
                         ('_stream', object()), ('_streams', {1: object()}),
                         ('_open_thread', SimpleNamespace(is_alive=lambda: True))):
        old = reset()
        setattr(d, field, value)
        with patch.object(d, 'load_speech_process') as load:
            assert not d.Api().set_setting('speech_model', 'small'), field
            load.assert_not_called()
        assert d.model is old and not old.closed and d.SETTINGS['speech_model'] == PARAKEET
    for activity in ('recording', 'jobs', 'lab', 'preview', 'draft'):
        old = reset()
        if activity == 'recording': d._recording.set()
        elif activity == 'jobs': d._jobs.put('synthetic unfinished job')
        elif activity == 'lab': d._lab['busy'] = 'scoring'
        with patch.object(d._preview, 'in_flight', return_value=activity == 'preview'), \
             patch.object(type(d._draft), 'busy', new=property(lambda self: activity == 'draft')), \
             patch.object(d, 'load_speech_process') as load:
            assert not d.Api().set_setting('speech_model', 'small'), activity
            load.assert_not_called()
        assert not old.closed and d.MODEL_SIZE == PARAKEET
    old = reset()
    d._startup_lock.acquire()
    try:
        assert not d.Api().set_setting('speech_model', 'small')
    finally:
        d._startup_lock.release()
    assert not old.closed
print('PASS invalid/unavailable selections and every active-work admission guard')

with patch.object(d, 'cached_speech_models', return_value=CHOICES):
    for failure in ('load', 'cache', 'save', 'wrong_model', 'not_ready'):
        old = reset()
        candidate = Runtime('other' if failure == 'wrong_model' else 'small',
                            state='failed' if failure == 'not_ready' else 'ready')
        saved = Path(d.SETTINGS_FILE).read_bytes()
        def loader(name):
            assert d.model is old and not old.closed
            if failure in ('load', 'cache'):
                raise d.SpeechUnavailableError('cache_invalid' if failure == 'cache' else 'speech_unavailable')
            return candidate
        original_save = d.atomic_write_json
        def save(*args):
            if failure == 'save': raise OSError('Synthetic save failure')
            return original_save(*args)
        with patch.object(d, 'load_speech_process', side_effect=loader), \
             patch.object(d, 'atomic_write_json', side_effect=save):
            assert not d.Api().set_setting('speech_model', 'small'), failure
        assert d.model is old and old.closed == 0 and old.status()['state'] == 'ready'
        assert d.MODEL_SIZE == d.SETTINGS['speech_model'] == PARAKEET
        assert Path(d.SETTINGS_FILE).read_bytes() == saved
        assert not d._speech_changing and not d._startup_lock.locked()
        if failure not in ('load', 'cache'): assert candidate.closed == 1
    old = reset()
    old.closes = False
    candidate = Runtime('small')
    with patch.object(d, 'load_speech_process', return_value=candidate):
        assert not d.Api().set_setting('speech_model', 'small')
    assert d.MODEL_SIZE == d.SETTINGS['speech_model'] == 'small'
    assert d.model is candidate and d._starting_speech is old
    assert d._speech_status()['state'] == 'failed' and 'previous speech worker' in d._last_error
    assert d._speech_retry_available()
    old.closes = True
    assert d._close_speech_runtime() and d._starting_speech is None
    old = reset()
    candidate = Runtime('small', closes=False)
    with patch.object(d, 'load_speech_process', return_value=candidate), \
         patch.object(d, 'atomic_write_json', side_effect=OSError('Synthetic save failure')):
        assert not d.Api().set_setting('speech_model', 'small')
    assert d.model is old and not old.closed and d._starting_speech is candidate
    assert d.MODEL_SIZE == d.SETTINGS['speech_model'] == PARAKEET
    assert d._speech_status()['state'] == 'failed' and 'finish recovery' in d._last_error
print('PASS load/cache/save failures preserve old model; cleanup failures remain explicit and owned')

old = reset()
entered, release = threading.Event(), threading.Event()
results = []
def blocked_loader(name):
    entered.set()
    assert release.wait(3)
    return Runtime(name)
with patch.object(d, 'cached_speech_models', return_value=CHOICES), \
     patch.object(d, 'load_speech_process', side_effect=blocked_loader) as load:
    thread = threading.Thread(target=lambda: results.append(d.Api().set_setting('speech_model', 'small')))
    thread.start()
    assert entered.wait(2)
    try:
        assert d.Api().get_state()['speech_model_switching']
        assert d.Api().get_settings()['speech_model'] == d.MODEL_SIZE == PARAKEET
        assert not d.Api().set_setting('speech_model', PARAKEET)
        assert not d.lab_run('scoring', lambda: None)
        assert not d.Api().paste_draft('Synthetic draft')
        assert not d.Api().retry_startup()
        assert 'Speech model' in d._quit_work_message()
        with patch.object(d, 'capture_target', side_effect=AssertionError('No recording during switch')):
            d._start()
        with patch.object(d.Api, 'get_local_models', return_value={'models': ['qwen3.5:4b', 'llama3.1:8b']}):
            assert d.Api().set_setting('local_model', 'llama3.1:8b')
        assert d.Api().set_setting('style', 'clean')
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and results == [True] and load.call_count == 1
assert d.SETTINGS['local_model'] == 'llama3.1:8b' and d.SETTINGS['style'] == 'clean'
assert json.loads(Path(d.SETTINGS_FILE).read_text())['local_model'] == 'llama3.1:8b'
assert old.closed == 1 and d.model.status()['model'] == d.SETTINGS['speech_model'] == d.MODEL_SIZE == 'small'
print('PASS concurrent request refusal, action fencing and rebase onto newest unrelated preferences')

class InlineThread:
    def __init__(self, target, **kwargs): self.target = target
    def start(self):
        if self.target is d.worker: service_calls.append('worker')
        elif self.target is d._warm_llm: pass
        else: self.target()
class NewListener:
    def start(self): service_calls.append('listener')
    def is_alive(self): return True

service_calls = []
reset()
d.model, d._listener, d._worker_started = None, None, False
d._state, d._last_error = 'loading', 'Synthetic failed startup'
with patch.object(d, 'cached_speech_models', return_value=CHOICES), \
     patch.object(d, 'load_speech_process', side_effect=lambda name: Runtime(name)), \
     patch.object(d.threading, 'Thread', InlineThread), \
     patch.object(d, '_create_keyboard_listener', return_value=NewListener()):
    assert d.Api().set_setting('speech_model', 'small')
assert service_calls == ['worker', 'listener'] and d._state == 'idle' and not d._last_error
assert d.MODEL_SIZE == 'small'
d.model, d._speech_asleep, d._waking = None, True, True
with patch.object(d, 'load_speech_process', side_effect=lambda name: Runtime(name)) as load:
    d._wake_speech()
    assert load.call_args.args == ('small',) and d._speech_status()['model'] == 'small'
assert not d._speech_asleep and not d._waking
with patch.object(d, 'load_speech_process', side_effect=lambda name: Runtime(name)) as load, \
     patch.object(d.threading, 'Thread', InlineThread), patch.object(d, '_chime', return_value=None):
    d._initialize_runtime()
    assert load.call_args.args == ('small',) and d._speech_status()['model'] == 'small'
assert d.SETTINGS['speech_model'] == d.MODEL_SIZE == 'small'
print('PASS failed-startup recovery, service ownership, selected-model idle wake and Retry')
