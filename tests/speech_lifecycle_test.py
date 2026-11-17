"""App shutdown/retry owns the isolated speech child, including late startup."""
import os
import inspect
from pathlib import Path
import sys
import threading
from unittest.mock import patch

os.environ.setdefault('ANTHROPIC_API_KEY', 'disabled-test-value')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d
# This fixture qualifies the retained Whisper CUDA-to-CPU path explicitly.
d.MODEL_SIZE = 'small'


class Runtime:
    def __init__(self):
        self.closed = False
    def status(self):
        return {'model': 'small', 'device': 'cuda', 'compute_type': 'float16',
                'fallback': False, 'state': 'failed' if self.closed else 'ready',
                'reason': 'speech_unavailable' if self.closed else ''}
    def close(self):
        self.closed = True
        return True


class Window:
    destroyed = False
    def destroy(self): self.destroyed = True


def reset():
    d._quitting = False
    d._state = 'loading'
    d._last_error = ''
    d._recording.clear()
    d._preview.end()
    d._busy = ''; d._lab['busy'] = ''
    d._pending_recordings = d._pending_pastes = 0
    d._tray = None
    d._win = Window()


assert hasattr(d, 'load_speech_process'), 'App startup still uses the unbounded native model call'
with patch.object(d, 'SpeechProcessRuntime', Runtime), \
     patch.object(d, 'persist_workspace', return_value=True), \
     patch.object(d.clipboard_delivery, 'pending_status', return_value={'pending': False}):
    reset()
    active = d.model = Runtime()
    with patch.object(d, 'persist_workspace', return_value=False), \
         patch.object(d, 'show_window', lambda *args: None):
        assert not d.quit_app() and not active.closed, 'A refused Quit stopped speech'
    assert d.quit_app() and active.closed and d._win.destroyed

    # A constructor returning after Quit must close its own late child instead
    # of publishing it or starting a hook/worker after the window has exited.
    reset(); d.model = None
    entered, release = threading.Event(), threading.Event()
    late = Runtime()
    def loader(name):
        assert name == 'small'
        entered.set(); assert release.wait(2)
        return late
    with patch.object(d, 'load_speech_process', loader):
        boot = threading.Thread(target=d._initialize_runtime)
        boot.start(); assert entered.wait(1)
        assert d.quit_app()
        release.set(); boot.join(2)
        assert not boot.is_alive() and late.closed and d.model is None

    # A still-starting process must be reachable by Quit before initialize
    # returns. The close signal itself releases this synthetic blocked load.
    reset(); d.model = None
    entered = threading.Event()
    pending = []
    class PendingRuntime(Runtime):
        def __init__(self, name, **options):
            super().__init__()
            self.release = threading.Event()
            pending.append(self)
        def initialize(self, device):
            entered.set(); assert self.release.wait(2)
            return self
        def close(self):
            self.closed = True
            self.release.set()
            return True
    with patch.object(d, 'SpeechProcessRuntime', PendingRuntime):
        boot = threading.Thread(target=d._initialize_runtime)
        boot.start(); assert entered.wait(1)
        try:
            assert d._starting_speech is pending[0], 'Quit cannot find the pending decoder'
            assert d.quit_app() and pending[0].closed
        finally:
            pending[0].release.set()
            boot.join(2)
        assert not boot.is_alive() and d.model is None and d._starting_speech is None

    # Interrupt at both startup publication boundaries. Quit either owns the
    # published runtime/services, or the initializer must reject its late work.
    for target, statement in ((d._initialize_runtime, 'model = candidate'),
                              (d._ensure_speech_services, 'if not _worker_started:')):
        reset(); d.model = None; d._worker_started = False; d._listener = None
        candidate = Runtime()
        reached, resume, saved = threading.Event(), threading.Event(), threading.Event()
        failures, quit_result, service_order = [], [], []
        lines, first_line = inspect.getsourcelines(target)
        boundary = next(first_line + index for index, line in enumerate(lines)
                        if line.strip() == statement)
        def trace(frame, event, argument):
            if frame.f_code is target.__code__ and event == 'line' and frame.f_lineno == boundary:
                reached.set()
                assert resume.wait(2)
            return trace
        def initialize():
            sys.settrace(trace)
            try: d._initialize_runtime()
            except BaseException as error: failures.append(error)
            finally: sys.settrace(None)
        class Listener:
            def __init__(self, **options):
                service_order.append('after Quit' if saved.is_set() else 'before Quit')
            def start(self): pass
            def is_alive(self): return True
        class Response:
            def raise_for_status(self): pass
        def save(module):
            saved.set()
            return True
        with patch.object(d, 'load_speech_process', return_value=candidate), \
             patch.object(d.pk, 'Listener', Listener), \
             patch.object(d, '_text_native', return_value=None), \
             patch.object(d, 'worker', lambda: None), \
             patch.object(d.cleanup_http, 'warm_json', return_value={}), \
             patch.object(d, '_chime', lambda *args: None), \
             patch.object(d, 'persist_workspace', save):
            boot = threading.Thread(target=initialize)
            boot.start(); assert reached.wait(1)
            quitting = threading.Thread(target=lambda: quit_result.append(d.quit_app()))
            quitting.start()
            saved.wait(.1)  # Atomic admission may hold Quit until publication.
            resume.set()
            boot.join(2); quitting.join(2)
            assert not boot.is_alive() and not quitting.is_alive() and not failures
            assert quit_result == [True] and candidate.closed, 'Late startup published an unowned decoder'
            assert 'after Quit' not in service_order, 'Startup installed a listener after Quit committed'

    reset(); d.model = None
    allow_close = threading.Event()
    created = []
    class UnconfirmedRuntime(Runtime):
        def __init__(self, name, **options):
            super().__init__(); created.append(self)
        def initialize(self, device):
            raise d.SpeechUnavailableError()
        def close(self):
            self.closed = allow_close.is_set()
            return self.closed
    with patch.object(d, 'SpeechProcessRuntime', UnconfirmedRuntime), \
         patch.object(d, 'show_window', lambda *args: None):
        try: d.load_speech_process('small')
        except d.SpeechUnavailableError: pass
        else: raise AssertionError('Synthetic startup failure was swallowed')
        assert d._starting_speech is created[0], 'Unconfirmed decoder cleanup lost its owner'
        assert not d.quit_app() and not d._win.destroyed
        d._initialize_runtime()
        assert len(created) == 1, 'Retry created another decoder before the old one exited'
        allow_close.set()
        assert d.quit_app() and created[0].closed

d._quitting = False
print('Speech lifecycle: refused Quit preserves runtime; accepted Quit and late '
      'startup release the owned decoder without model/provider calls')
