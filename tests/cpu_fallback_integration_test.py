"""Same-model CPU fallback through the app; run in the isolated unpaid runner."""
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d
# This fixture qualifies the retained Whisper CUDA-to-CPU path explicitly.
d.MODEL_SIZE = 'small'


folder = Path(tempfile.mkdtemp())
for name in ('model.bin', 'config.json', 'tokenizer.json'):
    (folder / name).write_text('synthetic cached fixture', encoding='utf-8')
calls = {'worker': 0, 'listener': 0, 'models': []}
mode = {'gpu_startup_failure': True, 'gpu_decode_failure': False, 'cpu_load_failure': False}


class Thread:
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args
    def start(self):
        if self.target is d.worker:
            calls['worker'] += 1
        elif self.target is d._hotkey_worker:
            pass
        else:
            self.target(*self.args)
    def is_alive(self): return True


class Listener:
    def __init__(self, **kwargs):
        calls['listener'] += 1
        self.running = False
    def start(self): self.running = True
    def is_alive(self): return self.running


class Response:
    def raise_for_status(self): pass


class Model:
    def __init__(self, device): self.device = device
    def transcribe(self, audio, **kwargs):
        def segments():
            if self.device == 'cuda' and mode['gpu_decode_failure']:
                yield SimpleNamespace(text='STALE PARTIAL GPU TEXT', avg_logprob=-0.1, no_speech_prob=0.01, compression_ratio=1)
                raise RuntimeError('Synthetic CUDA inference failure')
            yield SimpleNamespace(text='Blue lantern beside green river.', avg_logprob=-0.1, no_speech_prob=0.01, compression_ratio=1)
        return segments(), None


def loader(path, **kwargs):
    calls['models'].append((str(path), dict(kwargs)))
    if kwargs['device'] == 'cuda' and mode['gpu_startup_failure']:
        raise RuntimeError('Synthetic CUDA unavailable')
    if kwargs['device'] == 'cpu' and mode['cpu_load_failure']:
        raise RuntimeError('Synthetic CPU unavailable')
    return Model(kwargs['device'])


def resolver(name, *, local_files_only):
    assert name == 'small' and local_files_only is True
    return str(folder)


d._state = 'loading'
d._listener = None
d._worker_started = False
d._quitting = False
def backend(name):
    return d.load_speech_runtime(name, model_factory=loader, cache_resolver=resolver,
                                 compute_types=lambda device: {'int8', 'float32'})

with patch.object(d, 'load_speech_process', backend), \
     patch.object(d.threading, 'Thread', Thread), patch.object(d.pk, 'Listener', Listener), \
     patch.object(d, '_text_native', return_value=None), \
     patch.object(d, '_chime', lambda *args: None), patch.object(d.cleanup_http, 'warm_json', return_value={}):
    d._initialize_runtime()
    assert d._state == 'idle', 'A CUDA startup failure must recover with the same cached Small model on CPU'
    assert not d._last_error and calls['worker'] == calls['listener'] == 1
    assert len(calls['models']) == 2
    assert all(path == str(folder) and args['local_files_only'] is True for path, args in calls['models'])
    assert [args['device'] for _, args in calls['models']] == ['cuda', 'cpu']
    status = d.Api().get_state()['speech_runtime']
    assert status['model'] == 'small' and status['device'] == 'cpu' and status['fallback']
    assert status['compute_type'] == 'int8' and d._llm_ok
    assert 'CPU' in d.Api().diagnostics()['speech']

    # Healthy GPU remains preferred, with no CPU construction or duplicate services.
    mode['gpu_startup_failure'] = False
    before = len(calls['models'])
    d._initialize_runtime()
    assert len(calls['models']) == before + 1 and calls['models'][-1][1]['device'] == 'cuda'
    assert d.Api().get_state()['speech_runtime']['device'] == 'cuda'
    assert calls['worker'] == calls['listener'] == 1

    # A partial lazy GPU result must be discarded; processing/history/delivery
    # run once using only the CPU result. Later recordings stay on CPU.
    mode['gpu_decode_failure'] = True
    audio = [d.np.full((16000, 1), 0.1, dtype=d.np.float32)]
    delivered, history = [], []
    def delivery(text, target):
        delivered.append(text)
        return {'status': 'sent'}
    with patch.dict(d.SETTINGS, {'cleanup': False, 'learn': False}), \
         patch.object(d, 'paste', delivery), patch.object(d, '_append_history', history.append):
        before = len(calls['models'])
        d.process(audio, 1, d.time.perf_counter(), None)
        d.process(audio, 1, d.time.perf_counter(), None)
        assert len(calls['models']) == before + 1 and calls['models'][-1][1]['device'] == 'cpu'
        assert len(delivered) == len(history) == 2
        assert all('STALE' not in text and 'Blue lantern' in text for text in delivered)
        assert all(row['delivery_status'] == 'sent' for row in history)
        assert d.Api().get_state()['speech_runtime']['device'] == 'cpu'
        # Application filtering errors occur after inference, outside its retry boundary.
        with patch.object(d, 'filter_segments', side_effect=ValueError('Synthetic filter error')):
            try:
                d.transcribe(audio, 1)
                raise AssertionError('Filter failure hidden')
            except ValueError:
                pass
        assert len(calls['models']) == before + 1 and d._speech_status()['state'] == 'ready'

    # Recording B may overlap A's device failure. Its Finish hotkey must still
    # release its microphone; Retry must wait for its queued work to finish.
    mode.update(gpu_decode_failure=False, cpu_load_failure=False)
    d._busy = ''
    d._initialize_runtime()
    mode.update(gpu_decode_failure=True, cpu_load_failure=True)
    class StopWorker(BaseException): pass
    class OneJobQueue(queue.Queue):
        first = True
        def get(self, *args, **kwargs):
            if not self.first: raise StopWorker()
            self.first = False
            return super().get(*args, **kwargs)
    d._jobs = OneJobQueue()
    ready = threading.Event(); ready.set()
    d._jobs.put((audio, 1, d.time.perf_counter(), None,
                 (d._history_generation, d._recovery_generation), ready))
    d._recording.set(); d._state = 'recording'; d._locked = True
    d._gen = 90; d._buf = list(audio); d._rec_t = d.time.perf_counter()
    d._record_generations = (d._history_generation, d._recovery_generation)
    d._hotkey_down = None; d._skip_release = False
    closed = []
    with patch.dict(d.SETTINGS, {'cleanup': False, 'learn': False}), \
         patch.object(d.time, 'sleep', lambda seconds: None), \
         patch.object(d, '_close_mic', closed.append), \
         patch.object(d, 'paste', side_effect=AssertionError('Failed speech reached delivery')):
        try:
            d.worker()
        except StopWorker:
            pass
        assert d._recording.is_set() and d._state == 'recording'
        assert d._speech_status()['state'] == 'failed' and not d._busy
        assert not d.Api().retry_startup()
        d.on_press(d.hotkey())
        assert not d._recording.is_set() and closed == [90] and d._pending_recordings == 0
        assert d._jobs.unfinished_tasks == 1 and not d.Api().retry_startup()
        generation = d._gen
        d._start()
        assert d._gen == generation and not d._recording.is_set(), 'Unavailable speech admitted a new capture'
        queue.Queue.get(d._jobs, block=False)
        d._jobs.task_done()
        d.on_release(d.hotkey())
        mode.update(gpu_decode_failure=False, cpu_load_failure=False)
        assert d.Api().retry_startup()
        assert d._state == 'idle' and not d._last_error
        assert calls['worker'] == calls['listener'] == 1
    d._jobs = queue.Queue()

    # Reserve Retry before launching its thread so rapid clicks/new capture
    # cannot overlap the replacement model. Thread-start failure is recoverable.
    mode.update(gpu_startup_failure=True, cpu_load_failure=True)
    d._initialize_runtime()
    assert d._state == 'loading' and d._last_error
    pending = []
    class DeferredThread(Thread):
        def start(self): pending.append(self)
    with patch.object(d.threading, 'Thread', DeferredThread):
        assert d.Api().retry_startup() and d._startup_pending
        assert not d.Api().retry_startup() and len(pending) == 1
        generation = d._gen; d._start()
        assert d._gen == generation
        mode.update(gpu_startup_failure=False, cpu_load_failure=False)
        pending[0].target()
        assert not d._startup_pending and d._state == 'idle'
    mode.update(gpu_startup_failure=True, cpu_load_failure=True)
    d._initialize_runtime()
    class BrokenThread(Thread):
        def start(self): raise RuntimeError('Synthetic thread start failure')
    with patch.object(d.threading, 'Thread', BrokenThread):
        assert not d.Api().retry_startup() and not d._startup_pending
        assert d._state == 'loading' and d._speech_retry_available()
    mode.update(gpu_startup_failure=False, cpu_load_failure=False)
    assert d.Api().retry_startup()
    assert d._state == 'idle' and calls['worker'] == calls['listener'] == 1
print('CPU fallback integration: startup, lazy retry, exactly-once delivery, overlapping capture, guarded Retry and actual diagnostics passed (fake models).')
