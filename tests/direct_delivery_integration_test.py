"""App delivery behavior; run only through run_tests.py's isolated copy."""
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d


class Native:
    def __init__(self):
        self.target = d.text_delivery.Target(100, 200)
        self.current = self.target
        self.calls = []
        self.accepted = None
    def capture_target(self):
        return self.target
    def target_ready(self, target):
        return target == self.current
    def send_units(self, units, target):
        self.calls.append((units, target))
        return len(units) * 2 if self.accepted is None else self.accepted


class DeferredThread:
    pending = []
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args
    def start(self):
        self.pending.append(self)
    def run(self):
        self.target(*self.args)


def run():
    folder = tempfile.mkdtemp()
    d.APP_DIR = folder
    for name in ('_workspace_store', '_cost_ledger'):
        if hasattr(d, name):
            delattr(d, name)
    d._quitting = False
    d._state = 'idle'  # This suite simulates the already-started dictation service.
    d._retained = []
    d._retained_ver = 0
    d._last_error = ''
    d._busy = ''
    d._tray = None
    native = Native()
    d._direct_native = native
    if os.environ.get('DIRECT_CAPTURE_LOCK_BASELINE') == '1':
        original_capture = d.capture_target
        def locked_capture():
            with d._direct_delivery_lock:
                return original_capture()
        d.capture_target = locked_capture
    sounds = []
    d._chime = sounds.append
    with patch.object(d.clipboard_delivery, 'pending_status', return_value={'pending': False}), \
         patch.object(d.clipboard_delivery, 'paste_text', side_effect=AssertionError('Clipboard fallback is forbidden')):
        target = d.capture_target()
        assert target is native.target
        for text in ('First check, blue lantern. ', 'Second check, green river. Café 漢字 😀 '):
            result = d.paste(text, target)
            assert result['status'] == 'sent'
        assert len(native.calls) == 2 and not d._retained and not sounds
        assert d._last_error == ''
        diagnostic = d.Api().diagnostics()['direct text delivery']
        assert 'green river' not in diagnostic and 'Café' not in diagnostic
        assert json.loads(diagnostic)['target_pid'] == target.pid

        # Preserve the original process identity even when Windows reuses HWND.
        native.current = d.text_delivery.Target(target.hwnd, target.pid + 1)
        result = d.paste('exact stale target text', target)
        assert result['status'] == 'retained' and len(native.calls) == 2
        assert d._retained[-1]['text'] == 'exact stale target text'
        native.current = target

        for text in ('paragraph one\nparagraph two', 'a' * (d.text_delivery.MAX_UTF16_UNITS + 1)):
            count = len(native.calls)
            result = d.paste(text, target)
            assert result['status'] == 'retained' and len(native.calls) == count
            assert d._retained[-1]['text'] == text
            assert 'copy' in d._retained[-1]['message'].lower()
        for accepted, status in ((0, 'retained'), (1, 'uncertain')):
            native.accepted = accepted
            count = len(native.calls)
            text = 'full exact text after partial input'
            result = d.paste(text, target)
            assert result['status'] == status and len(native.calls) == count + 1
            assert d._retained[-1]['text'] == text
            if accepted:
                assert 'may already' in result['message'] and 'duplicates' in result['message']
        native.accepted = None
        assert d.paste_text('next independent entry', target)['status'] == 'sent'
        attempted_unknown = []
        def unknown_input(text, target, native, **options):
            attempted_unknown.append((text, target))
            raise OSError('synthetic unknown input outcome')
        with patch.object(d.text_delivery, 'send_text', side_effect=unknown_input):
            result = d.paste('exact text after unexpected input failure', target)
        assert result['status'] == 'uncertain' and result['accepted_count'] is None
        assert attempted_unknown == [('exact text after unexpected input failure', target)]
        assert d._retained[-1]['text'] == 'exact text after unexpected input failure'
        assert 'may already' in result['message']

        # Success clears only the specific transient delivery failure. Historical
        # recovery remains available, and a later unrelated error is preserved.
        assert d._last_error == result['message'] and d._delivery_attention_until > 0
        recovered_count = len(d._retained)
        assert d.paste('successful manual retry', target)['status'] == 'sent'
        assert d._last_error == '' and d._delivery_attention_until == 0
        assert len(d._retained) == recovered_count
        native.accepted = 0
        d.paste('another refused entry', target)
        d._last_error = 'Unrelated microphone error'
        previous_attention = d._delivery_attention_until
        native.accepted = None
        assert d.paste('successful entry after unrelated error', target)['status'] == 'sent'
        assert d._last_error == 'Unrelated microphone error'
        assert d._delivery_attention_until == previous_attention

        with patch.object(d._lab_thread, 'recording_clipboard_sequence', 81, create=True), \
             patch.object(d._multiline.backend, 'copy_unsent', return_value=True) as fallback:
            native.accepted = 0
            sound_count = len(sounds)
            assert d.paste('Ready for Ctrl+V. ', target)['status'] == 'copied'
            assert fallback.call_args.args == ('Ready for Ctrl+V. ', 81)
            assert sounds[sound_count:] == ['error']
            native.accepted = 1
            assert d.paste('Check original field. ', target)['status'] == 'uncertain'
            native.accepted = None
            assert d.paste('Normal delivery. ', target)['status'] == 'sent'
            assert fallback.call_count == 1

        # Recording captures once, then passes the same frozen identity through
        # the FIFO even when the current destination changes during capture.
        d._jobs = queue.Queue()
        d._pending_recordings = 0
        d._recording.clear()
        DeferredThread.pending = []
        with patch.object(d.threading, 'Thread', DeferredThread), \
             patch.object(d, '_close_mic', lambda gen: None):
            d._start()
            assert d._record_target is target
            native.target = d.text_delivery.Target(300, 400)
            d._stop()
            job = d._jobs.get_nowait()
            assert job[3] is target and not job[5].is_set()
            DeferredThread.pending[-1].run()
            assert job[5].is_set() and d._pending_recordings == 0
            d._jobs.task_done()
        native.target = target

        # Opening Whisper preserves an external Target without recapturing its
        # PID later, and opening it again cannot replace that with Whisper itself.
        window_calls = []
        class Window:
            def show(self): window_calls.append('show')
            def restore(self): window_calls.append('restore')
            def hide(self): window_calls.append('hide')
        d._win = Window()
        d.show_window()
        assert d._window_target is target
        native.target = d.text_delivery.Target(700, os.getpid())
        d.show_window()
        assert d._window_target is target
        native.target = target

        # Manual entry uses exactly the same route, advertises only 'sent', and
        # releases quit reservations. No real window or keystroke is involved.
        api = d.Api()
        d._pending_pastes = 0
        DeferredThread.pending = []
        with patch.object(d.threading, 'Thread', DeferredThread), \
             patch.object(d, 'bump_favorite', lambda *args: None), \
             patch.object(api, 'lab_kept', return_value=True), \
             patch.object(d.time, 'sleep', lambda seconds: None):
            assert api.paste_draft('manual exact entry')
            assert d._pending_pastes == 1 and window_calls[-1] == 'hide'
            DeferredThread.pending[-1].run()
            assert d._pending_pastes == 0
            assert api.get_state()['event'] == 'sent'
            assert api.get_state()['event'] == ''

        # A legacy unresolved Ctrl+V is never mixed with new direct input.
        count = len(native.calls)
        with patch.object(d.clipboard_delivery, 'pending_status', return_value={'pending': True}):
            result = d.paste('text while old clipboard is pending', target)
        assert result['status'] == 'retained' and len(native.calls) == count
        assert d._retained[-1]['text'] == 'text while old clipboard is pending'
        with patch.object(d.clipboard_delivery, 'pending_status', side_effect=OSError('synthetic recovery check failure')):
            result = d.paste('exact text after recovery check failure', target)
        assert result['status'] == 'retained' and len(native.calls) == count
        assert d._retained[-1]['text'] == 'exact text after recovery check failure'
        assert d.paste_text('after legacy recovery', target)['status'] == 'sent'

        # The shared route serializes automatic and manual sends. Each payload
        # remains an independent batch and no retry follows a partial outcome.
        entered, release, second_attempted, second_entered = (threading.Event() for _ in range(4))
        calls, results = [], []
        def send_units(units, sent_target):
            calls.append(units)
            if len(calls) == 1:
                entered.set()
                assert release.wait(3), 'Serialized test release timed out'
            else:
                second_entered.set()
            return 2 * len(units)
        def second():
            second_attempted.set()
            results.append(d.paste_text('second', target))
        with patch.object(native, 'send_units', send_units):
            first_thread = threading.Thread(target=lambda: results.append(d.paste_text('first', target)))
            second_thread = threading.Thread(target=second)
            first_thread.start()
            assert entered.wait(3)
            second_thread.start()
            assert second_attempted.wait(3)
            try:
                assert not second_entered.wait(.1), 'Concurrent direct sends interleaved'
            finally:
                release.set()
                first_thread.join(3)
                second_thread.join(3)
            assert not first_thread.is_alive() and not second_thread.is_alive()
        assert len(calls) == 2 and all(result['status'] == 'sent' for result in results)

        # Quit holds workspace/mic locks and shows the window when work is
        # active. Capture/show must never wait for a send whose failure needs
        # that workspace lock to save retained text (opposite lock ordering).
        direct_held, release_direct, observed = (threading.Event() for _ in range(3))
        capture_results, observer_errors = [], []
        def hold_direct():
            with d._direct_delivery_lock:
                direct_held.set()
                release_direct.wait(3)
        def observe_under_lab():
            try:
                with d._lab_lock:
                    capture_results.append(d.capture_target())
                    d.show_window()
                    d._pending_pastes = 1
                    assert not d.quit_app()
                    capture_results.append(d._window_target)
                observed.set()
            except Exception as exc:
                observer_errors.append(exc)
        holder = threading.Thread(target=hold_direct)
        observer = threading.Thread(target=observe_under_lab)
        holder.start()
        assert direct_held.wait(3)
        observer.start()
        try:
            completed_while_send_locked = observed.wait(1)
        finally:
            release_direct.set()
            holder.join(3)
            observer.join(3)
            d._pending_pastes = 0
        assert not holder.is_alive() and not observer.is_alive()
        assert not observer_errors, observer_errors
        assert completed_while_send_locked, 'Workspace-held capture/show/Quit waited on send lock'
        assert capture_results == [target, target]
    print('direct delivery integration: immutable capture, exact retention, sent status, pending recovery and serialized sends passed; no real input')


if __name__ == '__main__':
    run()
