"""Controlled race regressions; run only through the isolated test runner."""
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d

REAL_THREAD = threading.Thread
api = d.Api()
d._chime = lambda *args, **kwargs: None
d._warm_llm = lambda: None
d.capture_target = lambda: 123
d.resolve_microphone = lambda *args: 0
d.SETTINGS['learn'] = False
d.SETTINGS['cleanup'] = False
folder = tempfile.mkdtemp()
for attr, name in [('HISTORY', 'history.jsonl'), ('FAVORITES', 'favorites.json'),
                   ('SETTINGS_FILE', 'settings.json'), ('LEARNED_FILE', 'learned.json')]:
    setattr(d, attr, os.path.join(folder, name))


def reset():
    d._state = 'idle'  # These ownership cases run after successful service startup.
    d._lab.update(revs=[], cur=-1, busy='', error='', fav=None)
    d._recording.clear()
    d._stream = None
    d._stream_gen = None
    d._buf = []
    d._mic_ready = False


def score_result(text):
    return dict(score=7, criteria={k: 4 for k in ('clarity', 'context', 'task', 'constraints', 'success_criteria', 'format')},
                gaps=[], improved=text + ' improved', by='mock', cost=0)


class QueuedThread:
    pending = []
    def is_alive(self): return False  # the app asks whether a microphone opener is still running

    def __init__(self, target, args=(), kwargs=None, **unused):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.pending.append(self)

    def run(self):
        self.target(*self.args, **self.kwargs)


def busy_reservation():
    api.set_text('existing')
    QueuedThread.pending = []
    with patch.object(d.threading, 'Thread', QueuedThread), patch.object(d, 'run_judge', score_result):
        assert api.score()
        assert not api.improve(), 'second operation accepted before first worker starts'
        assert len(QueuedThread.pending) == 1
        QueuedThread.pending.pop().run()
    assert not api.get_lab()['busy']


def delayed_operation(operation, mutate, model_name):
    entered, release = threading.Event(), threading.Event()
    threads = []

    def tracked_thread(*args, **kwargs):
        thread = REAL_THREAD(*args, **kwargs)
        threads.append(thread)
        return thread

    def delayed(*args):
        entered.set()
        assert release.wait(5), 'test did not release model'
        return ('obsolete response', 'mock', 0) if model_name == 'engineer' else score_result(args[0])

    try:
        with patch.object(d.threading, 'Thread', tracked_thread), patch.object(d, model_name, delayed), patch.object(d, 'run_judge', delayed if model_name == 'run_judge' else score_result):
            assert operation()
            assert entered.wait(5), 'worker did not enter model'
            mutate()
            expected = api.get_lab()
            release.set()
            for thread in threads:
                thread.join(5)
                assert not thread.is_alive(), 'owned test worker did not exit'
            current = api.get_lab()
            assert current['text'] == expected['text'], 'stale response replaced current text'
            assert current['revs'] == expected['revs'], 'stale response changed revisions or scores'
            assert current['score'] == expected['score'], 'stale score attached after edit'
    finally:
        release.set()
        for thread in threads:
            thread.join(5)


def engineer_after_new():
    delayed_operation(lambda: api.submit('old request'), lambda: (api.new_draft(), api.set_text('new draft')), 'engineer')


def engineer_after_edit():
    api.set_text('original draft')
    delayed_operation(lambda: api.submit('refine'), lambda: api.set_text('hand edited'), 'engineer')


def engineer_after_select():
    d._lab_add(d._lab_rev('revision one', 'typed'))
    d._lab_add(d._lab_rev('revision two', 'typed'))
    delayed_operation(lambda: api.submit('refine'), lambda: api.select_rev(0), 'engineer')


def judge_after_edit():
    api.set_text('original draft')
    delayed_operation(api.score, lambda: api.set_text('hand edited'), 'run_judge')


class Stream:
    instances = []

    def __init__(self, **kwargs):
        self.callback = kwargs['callback']
        self.stopped = self.closed = False
        self.instances.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def microphone_ownership():
    QueuedThread.pending, Stream.instances = [], []
    with patch.object(d.threading, 'Thread', QueuedThread), patch.object(d.sd, 'InputStream', Stream):
        d._start()
        QueuedThread.pending.pop(0).run()
        old = Stream.instances[-1]
        d._stop()  # stop completion remains queued
        delayed_stop = QueuedThread.pending.pop(0)
        d._start()
        QueuedThread.pending.pop(0).run()
        current = Stream.instances[-1]
        before = len(d._buf)
        old.callback(d.np.ones((4, 1), dtype='float32'), 4, None, None)
        assert len(d._buf) == before, 'old microphone callback polluted new recording'
        current.callback(d.np.ones((4, 1), dtype='float32'), 4, None, None)
        assert len(d._buf) == before + 1
        delayed_stop.run()
        assert d._stream is current and not current.closed and d._mic_ready
        assert d._recording.is_set(), 'old stop cancelled newer recording'
        d._recording.clear()
        d._close_mic()
    # The old detached stream must also be released rather than leaked.
    assert old.closed, 'replaced microphone stream leaked'


def stale_microphone_failure():
    entered, release = threading.Event(), threading.Event()
    d._gen += 1
    old_gen = d._gen
    d._recording.set()

    def failing_stream(**kwargs):
        entered.set()
        assert release.wait(5)
        raise OSError('old device failed')

    with patch.object(d.sd, 'InputStream', failing_stream):
        thread = REAL_THREAD(target=d._open_mic, args=(old_gen,))
        thread.start()
        try:
            assert entered.wait(5)
            d._gen += 1
            d._buf = []
            d._last_error = ''
            release.set()
            thread.join(5)
            assert not thread.is_alive()
            assert d._recording.is_set() and d._last_error == ''
        finally:
            release.set()
            thread.join(5)


def invalid_inputs():
    before = dict(d.SETTINGS)
    assert not api.set_setting('hotkey', 'invalid')
    assert not api.set_setting('cleanup', 'true')
    assert d.SETTINGS == before
    api.set_text('keep me')
    QueuedThread.pending = []
    with patch.object(d.threading, 'Thread', QueuedThread), patch.object(d, 'run_judge', lambda text: {'score': float('nan')}):
        assert api.score()
        QueuedThread.pending.pop().run()
    result = api.get_lab()
    assert result['score'] is None and result['error'], 'invalid provider score accepted'


def stale_editor_writes():
    api.new_draft()
    api.set_text('old draft')
    context = api.get_lab()['draft_id']
    assert api.set_text('latest edit', context, 2)
    assert not api.set_text('older edit', context, 1)
    api.new_draft()
    assert not api.set_text('late old blur', context, 3)
    assert api.get_lab()['text'] == ''
    api.set_text('current work')
    context = api.get_lab()['draft_id']
    d._lab_add(d._lab_rev('new generated revision', 'engineer'))
    assert not api.set_text('late old edit', context, 5)
    assert api.get_lab()['text'] == 'new generated revision'


def stale_open_close_has_no_lock():
    # A real audio close may wait for its callback: simulate that dependency.
    class CallbackStream(Stream):
        def start(self):
            d._gen += 1
        def close(self):
            acquired = threading.Event()
            def callback():
                with d._mic_lock:
                    acquired.set()
            thread = REAL_THREAD(target=callback)
            thread.start()
            try:
                assert acquired.wait(2), 'close held microphone lock while waiting for callback'
            finally:
                thread.join(3)
            assert not thread.is_alive()
            self.closed = True
    d._gen += 1
    d._recording.set()
    with patch.object(d.sd, 'InputStream', CallbackStream):
        d._open_mic(d._gen)
    assert Stream.instances[-1].closed


failures = []
for test in (busy_reservation, engineer_after_new, engineer_after_edit, engineer_after_select,
             judge_after_edit, microphone_ownership, stale_microphone_failure, invalid_inputs, stale_editor_writes, stale_open_close_has_no_lock):
    reset()
    try:
        test()
    except Exception as exc:
        failures.append(test.__name__)
        print(f'{test.__name__}: FAIL: {exc}', flush=True)
    else:
        print(f'{test.__name__}: PASS', flush=True)
    finally:
        d._recording.clear()
        d._close_mic()
assert not failures, failures
print('ownership: all checks passed')
