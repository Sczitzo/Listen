"""Actual app lifecycle regressions; isolated runner, no native UI or providers."""
import hashlib
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
assert ROOT.name.startswith('.test-run-'), 'Use tests/run_tests.py'
sys.path.insert(0, str(ROOT))
import dictate as d


def reset():
    assert d._draft.close(timeout=2) and not d._draft.alive
    d._draft = d.DraftCoordinator(d._publish_draft, d._valid_draft)
    d._draft_key = ''
    d._draft_offered = None
    d._preview = d.TranscriptPreview(d.SAMPLE_RATE)
    d._recording.clear()
    d._state, d._busy, d._last_error = 'idle', '', ''
    d._quitting = False
    d._pending_recordings = d._pending_pastes = 0
    d._jobs = queue.Queue()
    d._history_generation = d._recovery_generation = 0
    d._lab['busy'] = False
    d._win = d._tray = None
    d.SETTINGS.update(cleanup=True, live_preview=True, learn=False)


def transcript(generation=7):
    context = (d._history_generation, d._recovery_generation)
    d._preview.begin(generation, context, now=0)
    ticket = d._preview.claim(generation, context, 32000, now=2)
    assert ticket is not None
    assert d._preview.finish(ticket, 'Synthetic recorded request.', now=2.1, elapsed=.1)
    return context


def engineer_finalization():
    for kind in ('empty', 'failed', 'unsafe'):
        reset()
        context = transcript()
        assert d._preview.finish_capture(7, context, now=3)
        raw = 'Hey, prompt engineer this' if kind == 'empty' else 'Hey, prompt engineer this: Keep 12 files.'
        retained = []
        with patch.object(d, 'transcribe', return_value=raw), \
             patch.object(d, '_retain_text', lambda text, message: retained.append(text)), \
             patch.object(d, 'show_window'), \
             patch.object(d, 'paste', side_effect=AssertionError('Unsafe engineer result delivered')), \
             patch.object(d, '_append_history', side_effect=AssertionError('Failed work entered history')), \
             patch.object(d, 'engineer', side_effect=RuntimeError('synthetic') if kind == 'failed' else None,
                          return_value=('Keep 13 files.', 'synthetic', 0)):
            d._lab_thread.preview_generation = 7
            try:
                d.process([], 1, d.time.perf_counter(), generations=context)
            finally:
                del d._lab_thread.preview_generation
        snapshot = d._preview.snapshot()
        assert snapshot['phase'] == 'final', (kind, snapshot)
        assert snapshot['text'] == raw, (kind, snapshot)
        assert snapshot['final_text'] == ('Keep 13 files.' if kind == 'unsafe' else ''), (kind, snapshot)
        assert snapshot['final_outcome'] == ('failed' if kind == 'failed' else 'review'), (kind, snapshot)
        assert snapshot['dismiss_at'] is not None and retained
    # A stale generation must never overwrite a new recording's preview.
    reset()
    context = transcript(8)
    d._lab_thread.preview_generation = 7
    try:
        d._finish_preview('old raw', 'old result', 'review_required')
    finally:
        del d._lab_thread.preview_generation
    assert d._preview.snapshot()['generation'] == 8
    assert d._preview.snapshot()['phase'] == 'recording'
    print('PASS engineer early-return finalization and generation fence')


def aborted_quit():
    for failure in ('speech', 'window'):
        reset()
        old = d._draft
        gate = old.gate
        with patch.object(d, 'persist_workspace', return_value=True), \
             patch.object(d.clipboard_delivery, 'pending_status', return_value={'pending': False}), \
             patch.object(d, 'show_window'), \
             patch.object(d, '_close_speech_runtime', return_value=failure != 'speech'):
            if failure == 'window':
                d._win = SimpleNamespace(destroy=MagicMock(side_effect=RuntimeError('synthetic window')))
            assert d.quit_app() is False
        assert not d._quitting and old.closed and not old.alive
        assert d._draft is not old and d._draft.gate is gate
        assert not d._draft.closed and d._draft_offered is None
        transcript()
        d._recording.set()
        job = d._draft_input()
        assert job is not None and job.key == d._draft_key
        # Exercise the real offer admission without launching its HTTP worker.
        with patch.object(d.threading.Thread, 'start'):
            assert d._draft.offer(job), 'Aborted Quit permanently disabled cleanup drafts'
        d._draft._thread = None  # mocked thread was deliberately never started
        d._recording.clear()
        assert d._draft.close(timeout=2)
    print('PASS aborted Quit restores draft admission with existing local gate')


def settings_race():
    reset()
    transcript()
    old_style = d.SETTINGS['style']
    new_style = next(value for value in d.STYLE_NAMES.values() if value != old_style)
    captured, release, setter_started, setter_done = [threading.Event() for _ in range(4)]
    errors, jobs = [], []
    real_hash = hashlib.sha256
    first = True

    def blocked_hash(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            captured.set()
            assert release.wait(3), 'Settings race release timed out'
        return real_hash(*args, **kwargs)

    def old_snapshot():
        try:
            jobs.append(d._draft_input())
        except BaseException as error:
            errors.append(error)

    def new_settings():
        setter_started.set()
        try:
            assert d.Api().set_setting('style', new_style)
        except BaseException as error:
            errors.append(error)
        finally:
            setter_done.set()

    workers = []
    with patch.object(hashlib, 'sha256', blocked_hash), patch.object(d, 'atomic_write_json'):
        try:
            workers.append(threading.Thread(target=old_snapshot))
            workers[-1].start()
            assert captured.wait(2)
            workers.append(threading.Thread(target=new_settings))
            workers[-1].start()
            assert setter_started.wait(2)
            assert not setter_done.wait(.1), 'New settings overtook older unpublished snapshot'
        finally:
            release.set()
            for worker in workers:
                worker.join(3)
            assert all(not worker.is_alive() for worker in workers), 'Settings race leaked a thread'
    assert not errors, errors
    assert setter_done.is_set() and jobs and jobs[0] is not None
    final_key = d._draft_key
    assert jobs[0].key != final_key
    current = d._draft_input()
    assert current.key == final_key and d.SETTINGS['style'] == new_style
    assert current.system != jobs[0].system
    print('PASS concurrent settings capture/hash/publication preserves newest key')


def frozen_monitor():
    reset()
    captured = []
    area = (-1920, -100, 0, 980)
    target = SimpleNamespace(hwnd=101)

    class SuppressedThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass

    with patch.object(d, '_capture_card_area', side_effect=lambda value: captured.append(value) or area), \
         patch.object(d, '_speech_status', return_value={'state': 'ready'}), \
         patch.object(d.threading, 'Thread', SuppressedThread):
        d._start(target)
    assert captured == [target] and d._preview_work_area == area
    # The target may move or disappear before Tk's next render.
    target.hwnd = 999
    d._record_target = SimpleNamespace(hwnd=888)

    class Rendered(Exception): pass
    renders = []

    class FakeRoot(MagicMock):
        def after(self, delay, callback): self.callback = callback
        def mainloop(self): self.callback()

    root = FakeRoot()
    root.winfo_screenwidth.return_value = 1920
    root.winfo_screenheight.return_value = 1080
    card = MagicMock()
    def render(snapshot, **kwargs):
        renders.append(kwargs)
        raise Rendered()
    card.update.side_effect = render
    with patch.object(d.tk, 'Tk', return_value=root), \
         patch.object(d.tk, 'Toplevel', return_value=MagicMock()), \
         patch.object(d.tk, 'Canvas', return_value=MagicMock()), \
         patch.object(d.ctypes, 'WinDLL', return_value=MagicMock()), \
         patch.object(d, '_configure_pill_no_activate', return_value=True), \
         patch.object(d, 'PreviewCard', return_value=card), \
         patch.object(d, '_capture_card_area', side_effect=AssertionError('Renderer recaptured monitor')):
        try:
            d.pill_thread(SimpleNamespace(title=''))
        except Rendered:
            pass
    assert len(renders) == 1 and renders[0].get('area') == area, renders
    d._recording.clear()
    print('PASS recording captures monitor once and renderer uses frozen work area')


def pending_draft_does_not_block_speech():
    reset()
    d._gen = 19
    d._record_generations = (0, 0)
    d._recording.set()
    d._state = 'recording'
    d._audio_samples = 32000
    d._buf = [d.np.ones((32000, 1), dtype='float32')]
    d._preview.begin(19, (0, 0), now=0)
    calls = []
    def recognize(audio, **options):
        calls.append(len(audio))
        return [SimpleNamespace(text='Speech keeps updating.', avg_logprob=0,
                                no_speech_prob=0, compression_ratio=1)], None
    from draft_preview import LocalModelGate
    pending = SimpleNamespace(busy=True, gate=LocalModelGate())
    with patch.object(d, '_draft', pending), patch.object(d, '_offer_draft', lambda: None), \
         patch.object(d, 'model', SimpleNamespace(transcribe_preview=recognize)), \
         patch.object(d, '_speech_status', return_value={'state': 'ready', 'device': 'cuda'}), \
         patch.object(d, 'vocab_words', return_value=[]):
        d._maybe_preview()
    assert calls == [32000] and d._preview.snapshot()['text'] == 'Speech keeps updating.'
    d._recording.clear()
    print('PASS queued/backing-off draft does not block speech when resource gate is free')


def draft_publication_covers_only_its_source():
    reset()
    context = transcript()
    d._recording.set()
    job = d._draft_input()
    assert job is not None
    ticket = d._preview.claim(7, context, 48000, now=4, interval=0)
    assert ticket is not None
    suffix = ' And keep the second sentence.'
    assert d._preview.finish(ticket, job.text + suffix, now=4.1, elapsed=.1)
    d._publish_draft(job, 'A recorded request.', 'ready', '')
    snapshot = d._preview.snapshot()
    assert snapshot['draft_covered_chars'] == len(job.text)
    assert snapshot['text'][snapshot['draft_covered_chars']:] == suffix
    print('PASS production cleanup publication carries its exact source coverage')


def overlay_controls_and_readiness():
    reset()
    d._gen = 71
    d._state = 'recording'
    d._recording.set()
    d._mic_ready = d._announced = False
    assert d.Api().get_state()['recording_ready'] is False
    calls = []
    def action(name):
        calls.append(name)
        d._recording.clear()
    with patch.object(d, '_stop', lambda: action('finish')), patch.object(d, '_cancel', lambda: action('cancel')):
        assert not d._overlay_recording_action('finish', 71), 'Starting allowed Finish'
        assert not d._overlay_recording_action('cancel', 70), 'Old panel cancelled a new recording'
        assert not d._overlay_recording_action('unknown', 71)
        assert not calls
        assert d._overlay_recording_action('cancel', 71)
        assert not d._overlay_recording_action('cancel', 71), 'Repeated click ran twice'
        d._gen = 72
        d._recording.set()
        d._mic_ready = True
        assert d.Api().get_state()['recording_ready'] is False
        d._announced = True
        assert d.Api().get_state()['recording_ready'] is True
        assert not d._overlay_recording_action('finish', 71)
        d._quitting = True
        assert not d._overlay_recording_action('finish', 72)
        d._quitting = False
        assert d._overlay_recording_action('finish', 72)
        assert not d._overlay_recording_action('finish', 72)
        assert calls == ['cancel', 'finish']
        assert d.Api().get_state()['recording_ready'] is False
    print('PASS truthful recording readiness and generation-guarded overlay Finish/Cancel')


try:
    engineer_finalization()
    aborted_quit()
    settings_race()
    frozen_monitor()
    pending_draft_does_not_block_speech()
    draft_publication_covers_only_its_source()
    overlay_controls_and_readiness()
finally:
    d._recording.clear()
    assert d._draft.close(timeout=2) and not d._draft.alive
