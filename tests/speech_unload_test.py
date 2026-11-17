"""Idle speech unload and reload ownership. Fake speech runtimes; run only through the isolated runner."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d

folder = tempfile.mkdtemp()
for attr, name in [('HISTORY', 'history.jsonl'), ('FAVORITES', 'favorites.json'),
                   ('SETTINGS_FILE', 'settings.json'), ('LEARNED_FILE', 'learned.json')]:
    setattr(d, attr, os.path.join(folder, name))
d._chime = lambda *a, **k: None
d._warm_llm = lambda: None
d.capture_target = lambda: 123
d.resolve_microphone = lambda *a: 0
d._open_mic = lambda gen: None  # no microphone: only the model lifecycle is under test
d.SETTINGS.update(cleanup=False, learn=False, live_preview=False)
d.SPEECH_WAKE_WAIT_S = 5


class Runtime(d.SpeechProcessRuntime):  # real type so the app's close path treats it as a speech process
    made = []

    def __init__(self):
        self.closed = False
        Runtime.made.append(self)

    def close(self):
        self.closed = True
        return True

    def status(self):
        return {'model': 'small', 'device': 'cuda', 'compute_type': 'float16', 'fallback': False,
                'state': 'ready', 'reason': ''}

    def transcribe(self, audio, **kwargs):
        seg = type('S', (), {'text': 'reloaded words', 'avg_logprob': -.1, 'no_speech_prob': .01})()
        return [seg], None


class Loader:
    """Gated stand-in for load_speech_process: counts loads and can fail or wait."""
    def __init__(self):
        self.calls, self.gate, self.fail = 0, threading.Event(), False
        self.gate.set()

    def __call__(self, name, **options):
        self.calls += 1
        self.gate.wait(10)
        if self.fail:
            raise d.SpeechUnavailableError()
        return Runtime()


def reset(asleep=True):
    Runtime.made.clear()
    d._recording.clear(); d._state = 'idle'; d._last_error = ''; d._busy = ''
    d._speech_asleep, d._waking, d._sleeping = asleep, False, False
    d.model = None if asleep else Runtime()
    d._open_thread = None


def settled(predicate, what, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('timed out waiting for ' + what)


loader = Loader()
with patch.object(d, 'load_speech_process', loader):
    # 1. Unload happens only when truly idle, and reports an honest status.
    reset(asleep=False)
    first = d.model
    for flag, value in (('_busy', 'cleaning'), ('_state', 'loading')):
        setattr(d, flag, value)
        d._sleep_speech()
        assert d.model is first and not d._speech_asleep, flag
        reset(asleep=False); first = d.model
    d._recording.set(); d._sleep_speech()
    assert d.model is first and not first.closed, 'unloaded during capture'
    d._recording.clear()
    d._sleep_speech()
    assert d.model is None and d._speech_asleep and first.closed
    status = d._speech_status()
    assert status['state'] == 'switching' and status['reason'] == 'idle_unloaded', status
    assert 'unloaded while idle' in d._speech_description(status)

    # 2. First press after idle starts the REAL recording at once; reload runs in parallel; one model only.
    reset(); loader.calls = 0; loader.gate.clear()
    d._start()
    assert d._recording.is_set() and d._record_target == 123, 'recording must not wait for the reload'
    d._begin_wake(); d._begin_wake()  # concurrent triggers must not start a second load
    time.sleep(.2)
    assert d.model is None and loader.calls == 1
    loader.gate.set()
    settled(lambda: d.model is not None and not d._speech_asleep, 'reload to publish')
    assert loader.calls == 1 and len(Runtime.made) == 1 and d._recording.is_set()
    d._cancel()
    assert not d._recording.is_set() and d.model is Runtime.made[0] and not Runtime.made[0].closed

    # 3. Cancel during reload: the finished reload still publishes exactly one model; nothing is recording.
    reset(); loader.calls = 0; loader.gate.clear()
    d._start(); d._cancel()
    loader.gate.set()
    settled(lambda: d.model is not None, 'reload after cancel')
    assert len(Runtime.made) == 1 and not d._recording.is_set() and loader.calls == 1
    d._start(); assert d._recording.is_set() and loader.calls == 1, 'second press must not reload'
    d._cancel()
    d._start(); d._cancel()  # consecutive recordings keep working

    # 4. Final transcription waits for the reload instead of failing or needing a second press.
    reset(); loader.calls = 0; loader.gate.clear()
    d._start()
    chunks = [d.np.full((16000, 1), .05, dtype='float32')]
    out = []
    runner = threading.Thread(target=lambda: out.append(d.transcribe(chunks, 1.0)))
    runner.start(); time.sleep(.3)
    assert runner.is_alive() and not out, 'transcribe must wait for the model'
    loader.gate.set(); runner.join(5)
    assert out == ['reloaded words'] and loader.calls == 1
    d._cancel()

    # 5. Reload failure: the recording stays alive, status is honest, transcription fails fast, next press retries.
    reset(); loader.calls = 0; loader.fail = True
    d._start()
    settled(lambda: not d._waking, 'failed reload to finish')
    assert d._recording.is_set() and d._state == 'recording', 'a failed reload must not break the recording'
    assert d._speech_asleep and 'restart' in d._last_error
    started = time.monotonic()
    try:
        d.transcribe(chunks, 1.0)
    except d.SpeechUnavailableError:
        pass
    else:
        raise AssertionError('transcribe must fail when the model cannot reload')
    assert time.monotonic() - started < 2, 'a failed reload must end the wait at once'
    d._cancel()
    loader.fail = False
    d._start()
    settled(lambda: d.model is not None, 'retry on next press')
    d._cancel()

    # 6. Quit during reload: the late model is closed and never published.
    reset(); loader.calls = 0; loader.gate.clear()
    d._start()
    time.sleep(.1)
    d._quitting = True
    loader.gate.set()
    settled(lambda: not d._waking, 'wake to end after quit')
    assert d.model is None and len(Runtime.made) == 1 and Runtime.made[0].closed
    d._quitting = False; d._cancel()

    # 7. Idle clock: busy resets it; only after the configured idle time does ONE unload start.
    reset(asleep=False)
    d.SPEECH_IDLE_UNLOAD_S = .3
    d._busy = 'transcribing'; time.sleep(.4); d._speech_idle_check()
    assert not d._sleeping and d.model is not None, 'busy must reset the idle clock'
    d._busy = ''; d._speech_idle_check(); time.sleep(.45)
    d._speech_idle_check()
    settled(lambda: d._speech_asleep and d.model is None, 'unload after idle')
    d.SPEECH_IDLE_UNLOAD_S = 600.0
print('speech unload: all checks passed')
