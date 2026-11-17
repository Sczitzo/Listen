"""Preview is ephemeral; final capture, transcription and delivery keep ownership."""
from pathlib import Path
import ctypes
import os
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d


assert callable(getattr(d, '_maybe_preview', None)), 'Recording must provide a bounded provisional-preview tick'
assert 'preview' in d.Api().get_state(), 'The app must expose ephemeral preview state'
REAL_THREAD = threading.Thread
clock = SimpleNamespace(now=1000.0)
backend = {'text': 'Provisional words only.', 'elapsed': .1, 'error': False,
           'gate': None, 'entered': None}
calls, delivered, history, refined, closed, chimes, captures = [], [], [], [], [], [], []
cache = Path(tempfile.mkdtemp())
for name in ('model.bin', 'config.json', 'tokenizer.json'):
    (cache / name).write_text('synthetic cached asset', encoding='utf-8')


class Recognizer:
    def transcribe_preview(self, audio, **options):
        return self._recognize(audio, options, preview=True)

    def transcribe(self, audio, **options):
        return self._recognize(audio, options, preview=False)

    def _recognize(self, audio, options, *, preview):
        calls.append((preview, len(audio), dict(options)))
        text = backend['text'] if preview else 'Final destination text.'
        if preview:
            if backend['entered'] is not None: backend['entered'].set()
            if backend['gate'] is not None: assert backend['gate'].wait(3)
            clock.now += backend['elapsed']
            if backend['error']: raise ValueError('Synthetic preview error: private contents must not escape')
        return iter((SimpleNamespace(text=text, avg_logprob=-.1, no_speech_prob=.01, compression_ratio=1),)), None


class Thread:
    def is_alive(self): return False
    def __init__(self, target, args=(), **kwargs): self.target, self.args = target, args
    def start(self):
        if self.target in (d._open_mic, d._warm_llm): return
        self.target(*self.args)


def reset():
    assert not d._preview.in_flight()
    d._preview = d.TranscriptPreview(d.SAMPLE_RATE)
    d._recording.clear(); d._state = 'idle'; d._busy = ''; d._last_error = ''
    d._quitting = False; d._announced = False; d._pending_recordings = d._pending_pastes = 0
    d._jobs = queue.Queue(); d._history_generation = d._recovery_generation = 0
    d._startup_pending = False
    d.SETTINGS.update(live_preview=True, cleanup=True, learn=False)
    d.model = d.load_speech_runtime('small', model_factory=lambda *args, **kwargs: Recognizer(),
                                   cache_resolver=lambda *args, **kwargs: str(cache),
                                   compute_types=lambda device: {'int8'})
    # Preview is identified at its public method boundary: preview and final
    # now deliberately use the same decoding options without token truncation.
    d.model.transcribe_preview = Recognizer().transcribe_preview
    backend.update(text='Provisional words only.', elapsed=.1, error=False, gate=None, entered=None)
    for rows in (calls, delivered, history, refined, closed, chimes, captures): rows.clear()
    clock.now += 20


def feed(seconds):
    count = int(seconds * d.SAMPLE_RATE)
    d._audio_cb(d.np.full((count, 1), .1, dtype=d.np.float32), count, None, None, gen=d._gen, buf=d._buf)
    clock.now += seconds


def start(seconds=3):
    d._start()
    feed(seconds)


def final():
    job = d._jobs.get_nowait()
    generation, lease, _ = d._preview_final_jobs.pop(job[5])
    d._lab_thread.preview_generation = generation
    try:
        assert job[5].is_set(), 'Owned microphone must close before final processing'
        d.process(*job[:5])
    finally:
        del d._lab_thread.preview_generation
        lease.release()
        d._busy = ''
        d._jobs.task_done()


def begin_delayed():
    entered, release = threading.Event(), threading.Event()
    backend.update(entered=entered, gate=release)
    thread = REAL_THREAD(target=d._maybe_preview)
    thread.start()
    assert entered.wait(2), 'Preview never reached fake inference'
    return thread, release


def finish_delayed(thread, release):
    release.set(); thread.join(3)
    assert not thread.is_alive() and not d._preview.in_flight()
    backend.update(entered=None, gate=None)


def capture():
    target = d.text_delivery.Target(100, 200)
    captures.append(target)
    return target


def deliver(text, target=None):
    delivered.append((text, target))
    return {'status': 'sent'}


with patch.object(d.threading, 'Thread', Thread), \
     patch.object(d.time, 'perf_counter', lambda: clock.now), \
     patch.object(d, 'capture_target', capture), patch.object(d, '_close_mic', closed.append), \
     patch.object(d, '_chime', chimes.append), patch.object(d, 'paste', deliver), \
     patch.object(d, '_append_history', history.append), \
     patch.object(d, 'refine', lambda text: (refined.append(text) or text)), \
     patch.object(d, '_offer_draft', lambda: None), \
     patch.object(d.pyperclip, 'copy', side_effect=AssertionError('Preview changed clipboard')), \
     patch.object(d.pyperclip, 'paste', side_effect=AssertionError('Preview read clipboard')), \
     patch.object(d.requests, 'post', side_effect=AssertionError('Preview called a provider')):
    reset()
    start()
    warm_started, finish_warm = threading.Event(), threading.Event()
    def blocked_warm(*args, **kwargs):
        warm_started.set()
        assert finish_warm.wait(3)
    with patch.object(d.cleanup_http, 'warm_json', blocked_warm), \
         patch.object(d, '_cleanup_policy', d.CleanupPolicy()):
        warmer = REAL_THREAD(target=d._warm_llm)
        warmer.start()
        try:
            assert warm_started.wait(2), 'Cleanup did not start warming'
            d._maybe_preview()
            assert d._preview.snapshot()['text'] == 'Provisional words only.'
            assert d._draft.gate.try_draft(d.cleanup_http.Cancellation()) is None
            assert not delivered and not history
            d._cancel()
            assert not d._preview.snapshot()['text']
        finally:
            finish_warm.set()
            warmer.join(3)
        assert not warmer.is_alive()

    reset()
    with patch.object(d, '_speech_status', return_value={'state': 'ready', 'device': 'cuda'}):
        start(.99)
        d._maybe_preview()
        assert not calls
        feed(.01)
        d._maybe_preview()
        assert d._preview.snapshot()['text'] == 'Provisional words only.'
        assert d._draft_input() is None, 'The short initial preview must not start AI cleanup'
        assert not delivered and not history
        d._stop()
        final()
        assert [call[:2] for call in calls] == [(True, d.SAMPLE_RATE), (False, d.SAMPLE_RATE)]
        assert [text for text, _ in delivered] == ['Final destination text. ']

        reset()
        backend['text'] = ''
        start(1.2)
        d._maybe_preview()
        assert len(calls) == 1 and not d._preview.snapshot()['text']
        backend['text'] = 'Speech after initial silence.'
        feed(.8)
        d._maybe_preview()
        assert d._preview.snapshot()['text'] == backend['text']
        assert d._draft_input() is not None
        d._cancel()

        reset()
        backend.update(text='', elapsed=3.0)
        start(1)
        d._maybe_preview()
        assert len(calls) == 1 and d._preview.snapshot()['reason'] == 'slow'
        backend.update(text='Speech after a slow preview.', elapsed=.1)
        feed(1)
        d._maybe_preview()
        assert len(calls) == 1, 'A slow empty decode must keep the normal retry backoff'
        clock.now += 6
        d._maybe_preview()
        assert d._preview.snapshot()['text'] == backend['text']
        d._cancel()

    reset()
    with patch.object(d, '_speech_status', return_value={'state': 'ready', 'device': 'cpu'}):
        start(1)
        d._maybe_preview()
        assert not calls
        feed(1)
        d._maybe_preview()
        assert d._preview.snapshot()['text'] == 'Provisional words only.'
        d._cancel()

    reset(); start()
    original_target, original_buffer = d._record_target, d._buf
    d._maybe_preview()
    assert d._preview.snapshot()['text'] == 'Provisional words only.'
    assert not delivered and not history and not refined and not d._retained and not chimes
    assert len(captures) == 1 and d._record_target is original_target and d._buf is original_buffer
    assert 'Provisional' not in d.Api().diagnostics()['live preview']
    d._stop()
    assert not d._preview.snapshot()['active']
    assert d._preview.snapshot()['phase'] == 'finishing'
    assert d._preview.snapshot()['text'] == 'Provisional words only.'
    final()
    assert d._preview.snapshot()['phase'] == 'final'
    assert d._preview.snapshot()['final_text'] == 'Final destination text.'
    assert len(delivered) == len(history) == len(refined) == 1
    assert delivered[0] == ('Final destination text. ', original_target)
    assert [preview for preview, _, _ in calls] == [True, False]

    reset(); start()
    d._jobs.put('an earlier stopped dictation')
    d._maybe_preview()
    assert not calls, 'Preview overtook already-reserved final work'
    d._jobs.get_nowait(); d._jobs.task_done(); d._cancel()

    # A final reservation appearing during snapshot preparation wins before
    # any new preview inference. Its original full buffer remains untouched.
    reset(); start(20)
    source_interval = d.audio_interval
    def stop_during_selection(chunks, start, end):
        selected = source_interval(chunks, start, end)
        d._stop()
        return selected
    with patch.object(d, 'audio_interval', stop_during_selection): d._maybe_preview()
    assert not calls and not d._preview.in_flight()
    final(); assert calls[0][0] is False and calls[0][1] == 20 * d.SAMPLE_RATE

    # Catch up old audio, including a replaceable tail, without new capture.
    # Deliberately distinct values prove we did not keep selecting recent audio.
    reset(); start(20)
    d._buf[:] = [d.np.full((8 * d.SAMPLE_RATE, 1), value, dtype=d.np.float32)
                 for value in (.1, .2)] + [d.np.full((4 * d.SAMPLE_RATE, 1), .3, dtype=d.np.float32)]
    intervals, values = [], []
    def track_interval(chunks, left, right):
        intervals.append((left, right))
        pieces = source_interval(chunks, left, right)
        values.append(float(pieces[0][0, 0]))
        return pieces
    with patch.object(d, 'audio_interval', track_interval):
        for text in ('First repeated phrase.', 'First repeated phrase.', 'Tail words.'):
            backend['text'] = text
            d._maybe_preview()
            clock.now += 4
    assert intervals == [(0, 8 * d.SAMPLE_RATE), (8 * d.SAMPLE_RATE, 16 * d.SAMPLE_RATE),
                         (16 * d.SAMPLE_RATE, 20 * d.SAMPLE_RATE)]
    assert all(abs(actual - expected) < 1e-6 for actual, expected in zip(values, (.1, .2, .3)))
    assert d._preview.snapshot()['text'] == 'First repeated phrase. First repeated phrase. Tail words.'
    d._maybe_preview(); assert len(calls) == 3, 'Unchanged partial tail was recognized again'
    added = d.np.full((d.SAMPLE_RATE, 1), .4, dtype=d.np.float32)
    d._audio_cb(added, d.SAMPLE_RATE, None, None, gen=d._gen, buf=d._buf)
    clock.now += 1
    backend['text'] = 'Replacement tail words.'
    with patch.object(d, 'audio_interval', track_interval): d._maybe_preview()
    assert intervals[-1] == (16 * d.SAMPLE_RATE, 21 * d.SAMPLE_RATE)
    assert d._preview.snapshot()['text'] == 'First repeated phrase. First repeated phrase. Replacement tail words.'
    assert all('max_new_tokens' not in options for preview, _, options in calls if preview)
    d._stop(); final()
    assert calls[-1][1] == 21 * d.SAMPLE_RATE and len(delivered) == 1

    # Final A remains deliverable in FIFO order, but cannot overwrite the card
    # for recording B, even if A finishes after B starts.
    reset(); start(); d._maybe_preview(); d._stop()
    first_generation = d._gen
    start(); assert d._gen != first_generation
    final()
    assert len(delivered) == 1 and d._preview.snapshot()['phase'] == 'recording'
    assert d._preview.snapshot()['generation'] == d._gen and d._preview.snapshot()['text'] == ''
    d._cancel()

    # Cancel -> new recording while an old native preview is outstanding.
    reset(); start(); old_generation = d._gen
    thread, release = begin_delayed()
    try:
        state = d.Api().get_state()
        assert state['preview_busy'] and state['state'] == 'recording'
        d._cancel()
        assert not d._preview.snapshot()['active'] and d._preview.snapshot()['text'] == ''
        with patch.object(d, 'show_window', lambda *args: None):
            assert not d.quit_app() and not d._quitting, 'Quit ignored outstanding preview inference'
        d._state = 'loading'; d._last_error = 'Synthetic startup failure'
        assert not d.Api().retry_startup(), 'Retry replaced a runtime while preview inference was outstanding'
        with patch.object(d, 'load_speech_process', side_effect=AssertionError('Unsafe model replacement')):
            d._initialize_runtime()
        d._state = 'idle'; d._last_error = ''
        assert not d._speech_retry_available()
        start(); new_count = d._audio_samples
        d._audio_cb(d.np.ones((8, 1)), 8, None, None, gen=old_generation)
        assert d._audio_samples == new_count
        d._maybe_preview(); assert len(calls) == 1, 'Second model call overlapped old preview'
    finally:
        finish_delayed(thread, release)
    assert d._preview.snapshot()['text'] == '' and not delivered and d._jobs.empty()
    backend['text'] = 'Current recording words.'
    d._maybe_preview(); assert d._preview.snapshot()['text'] == 'Current recording words.'
    d._stop(); final(); assert len(delivered) == 1

    # Disable/re-enable during inference invalidates its old epoch, even when
    # the recording generation stays the same. Bad setting types are rejected.
    reset(); start(); thread, release = begin_delayed()
    try:
        api = d.Api()
        assert not api.set_setting('live_preview', 'yes')
        assert api.set_setting('live_preview', False)
        assert not d._preview.snapshot()['active']
        assert api.set_setting('live_preview', True)
        assert d._preview.snapshot()['active'] and d._preview.in_flight()
    finally:
        finish_delayed(thread, release)
    assert d._preview.snapshot()['text'] == ''
    d._cancel()

    # Preview can stay off even while an old preview request returns. Finish
    # retains the recording identity and publishes a status without words.
    reset(); start(); thread, release = begin_delayed()
    try:
        assert d.Api().set_setting('live_preview', False)
    finally:
        finish_delayed(thread, release)
    assert d._preview.snapshot()['phase'] == 'recording' and not d._preview.snapshot()['active']
    d._stop(); final()
    snapshot = d.Api().get_state()['preview']
    assert snapshot['phase'] == 'final' and snapshot['final_outcome'] == 'sent'
    assert snapshot['text'] == snapshot['draft_text'] == snapshot['final_text'] == ''
    assert len(delivered) == 1

    reset(); d.SETTINGS['live_preview'] = False; start()
    d._maybe_preview(); assert not calls
    d._stop()
    with patch.object(d, 'transcribe', return_value=''):
        final()
    assert d._preview.snapshot()['final_outcome'] == 'no_speech' and not delivered

    reset(); start(); d._maybe_preview(); d._stop()
    assert d._preview.snapshot()['text']
    with patch.dict(d.SETTINGS, live_preview=False):
        assert not d.Api().get_state()['preview']['text'], 'Bridge exposed words before the toggle cleared them'
    assert d.Api().set_setting('live_preview', False)
    assert d._preview.snapshot()['phase'] == 'finishing' and not d._preview.snapshot()['text']
    final()
    assert d._preview.snapshot()['final_outcome'] == 'sent'
    assert d.Api().set_setting('live_preview', True)
    assert not d._preview.snapshot()['text'] and not d._preview.snapshot()['final_text']

    # Privacy clear acknowledges an invalidated publication epoch; its delayed
    # model result cannot restore old text or touch history/clipboard/delivery.
    for scope in ('history', 'workspace'):
        reset(); start(); thread, release = begin_delayed()
        try:
            result = d.Api().clear_private_data(scope, 'DELETE')
            assert result['ok'] and result['preview_version'] == d._preview.snapshot()['version']
            assert d._preview.snapshot()['text'] == '' and not d._preview.snapshot()['active']
        finally:
            finish_delayed(thread, release)
        assert d._preview.snapshot()['text'] == '' and not delivered and not history
        d._cancel()

    # Failure preserves the oldest interval for retry; slow success keeps its
    # transcript and adapts cadence rather than permanently pausing catch-up.
    for failure in (True, False):
        reset(); start(20)
        backend.update(error=failure, elapsed=3 if not failure else .1)
        d._maybe_preview()
        snapshot = d._preview.snapshot()
        assert snapshot['state'] == 'listening'
        assert snapshot['reason'] == ('unavailable' if failure else 'slow')
        assert snapshot['covered_samples'] == (0 if failure else 8 * d.SAMPLE_RATE)
        assert not d._last_error and not delivered and not history and not refined and not chimes
        assert calls[0][1] == 8 * d.SAMPLE_RATE
        d._maybe_preview(); assert len(calls) == 1, 'Adaptive cadence was ignored'
        backend.update(error=False, elapsed=.1)
        clock.now += 10; d._maybe_preview(); assert len(calls) == 2
        assert d._preview.snapshot()['covered_samples'] == (8 if failure else 16) * d.SAMPLE_RATE
        d._stop(); final()
        assert len(delivered) == len(history) == 1 and calls[-1][1] == 20 * d.SAMPLE_RATE

    # Snapshot failure still releases the reservation; Quit/Retry cannot become
    # permanently stuck before model inference even begins.
    reset(); start()
    with patch.object(d, 'audio_interval', side_effect=ValueError('Synthetic snapshot failure')):
        d._maybe_preview()
    assert not d._preview.in_flight() and d._preview.snapshot()['reason'] == 'unavailable'
    d._cancel()

    # A separate capture-end signal must not allow a delayed preview result to
    # publish simply because the ticket's generation is still current.
    reset(); start(); thread, release = begin_delayed()
    try:
        d._recording.clear(); d._state = 'idle'
    finally:
        finish_delayed(thread, release)
    assert not d._preview.snapshot()['active'] and d._preview.snapshot()['text'] == ''

    class WindowApi:
        def __init__(self, writable=True, owner=None):
            self.style = 0; self.writes = 0; self.writable = writable
            self.owner = os.getpid() if owner is None else owner
        def IsWindow(self, hwnd): return hwnd == 77
        def GetWindowThreadProcessId(self, hwnd, output):
            ctypes.cast(output, ctypes.POINTER(ctypes.c_ulong))[0] = self.owner
        def GetWindowLongW(self, hwnd, index): return self.style
        def SetWindowLongW(self, hwnd, index, value):
            self.writes += 1
            if self.writable: self.style = value
    safe = WindowApi()
    assert d._configure_pill_no_activate(safe, 77) and safe.writes == 1
    assert d._configure_pill_no_activate(safe, 77) and safe.writes == 1
    refused = WindowApi(writable=False)
    assert not d._configure_pill_no_activate(refused, 77)
    foreign = WindowApi(owner=os.getpid() + 1)
    assert not d._configure_pill_no_activate(foreign, 77) and foreign.writes == 0
    assert not d._configure_pill_no_activate(safe, 0)
print('Live preview: chronological catch-up, final session ownership/whole-audio delivery, stale cancellation/clear/off-on, adaptive retry and microphone release passed.')
