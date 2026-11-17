"""Actual app route with synthetic field/clipboard adapters; no native access."""
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d
from multiline_delivery import MultilineDelivery, VerifiedPasteBackend


class Fields:
    ready, alive = True, False
    def __init__(self):
        self.same, self.cancel_capture = True, False
        self.expected, self.document, self.released = '', '', []
    def capture(self, target):
        assert not d._mic_lock._is_owned()
        if self.cancel_capture: d._cancel()
        return 'capture'
    def prepare(self, token, text):
        assert not d._mic_lock._is_owned()
        if not self.same: return None
        self.expected = text
        return 'transaction'
    def check(self, token): return self.same
    def verify(self, token, timeout=.2): return self.document == self.expected
    def release(self, token):
        assert not d._mic_lock._is_owned()
        self.released.append(token)
    def close(self, timeout=2): return True


class Clipboard:
    def __init__(self, fields):
        self.fields, self.data, self.seq = fields, [(13, b'original')], 1
        self.sends = self.writes = 0
    def open(self): return True
    def close(self): pass
    def sequence(self): return self.seq
    def snapshot(self): return list(self.data)
    def payload(self, text): return [(13, text.encode('utf-16-le'))]
    def prepare(self, values): return list(values)
    def release(self, values): pass
    def replace(self, values):
        self.writes += 1
        self.seq += 1
        self.data = list(values)
    def send_paste(self, ready=None):
        assert ready and ready()
        self.sends += 1
        self.fields.document = self.data[0][1].decode('utf-16-le')
        self.last_send_count = 4
        return 4
    def shutdown(self): return True


class Threads:
    pending = []
    def is_alive(self): return False
    def __init__(self, target, args=(), **kwargs): self.target, self.args = target, args
    def start(self): self.pending.append(self)
    def run(self): self.target(*self.args)


target = d.text_delivery.Target(100, 200, 300)
fields = Fields()
clipboard = Clipboard(fields)
native = SimpleNamespace(target_ready=lambda value: value == target,
                         target_focused=lambda value: value == target,
                         send_units=lambda *args: (_ for _ in ()).throw(AssertionError('No Unicode fallback')))
backend = VerifiedPasteBackend(clipboard)
route = MultilineDelivery(fields, lambda: native, d._delivery_context_valid, backend)
d._quitting = False
d._state = 'idle'
d._recording.clear()
d.SETTINGS.update(cleanup=False)
original_backend = d.clipboard_delivery._verified_backend
with patch.object(d, '_field_access', fields), patch.object(d, '_multiline', route), \
     patch.object(d, '_direct_native', native), patch.object(d, '_chime', lambda *args: None), \
     patch.object(d, '_capture_card_area', lambda target: (0, 0, 1000, 1000)), \
     patch.object(d, '_close_mic', lambda *args: True), \
     patch.object(d, '_speech_status', lambda: {'state': 'ready'}), \
     patch.object(d.threading, 'Thread', Threads):
    d.clipboard_delivery.configure_verified_backend(backend)
    try:
        d._start(target=target)
        assert d._recording.is_set() and d._record_target.field_token == 'capture'
        d._cancel()
        for thread in list(Threads.pending):
            if thread.target.__name__ == 'finish_cancel': thread.run()
        Threads.pending.clear()
        assert not d._recording.is_set() and 'capture' in fields.released and not clipboard.sends

        fields.cancel_capture = True
        d._start(target=target)
        assert not d._recording.is_set() and fields.released.count('capture') >= 2
        fields.cancel_capture = False
        Threads.pending.clear()

        admitted = d.replace_target(target, field_token='capture')
        for text in ('- apple\n- banana 😀 ', '1. milk\n2. eggs '):
            result = d.paste_text(text, admitted)
            assert result['status'] == 'pasted' and result['accepted_count'] == 4
            assert fields.document == text and clipboard.data == [(13, b'original')]
        assert clipboard.sends == 2 and not backend.pending_status()['pending']
        assert fields.released.count('transaction') == 2

        fields.same = False
        writes, sends = clipboard.writes, clipboard.sends
        result = d.paste_text('- never\n- wrong field', admitted)
        assert result['status'] == 'retained' and clipboard.sends == sends and clipboard.writes == writes
        fields.same = True
        result = d.paste_text('- returned\n- original field', admitted)
        assert result['status'] == 'pasted'
        assert d.paste_text('- no\n- field token', target)['status'] == 'retained'

        # Whole recording -> list layout -> actual app paste route, no copy UI.
        raw = 'I need a shopping list with three items. apples, milk, and eggs.'
        d.SETTINGS['cleanup'] = True
        entries = []
        with patch.object(d, 'transcribe', return_value=raw), \
             patch.object(d, 'refine', side_effect=d.layout_explicit_lists), \
             patch.object(d, '_append_history', side_effect=entries.append):
            d.process([object()], 2, d.time.perf_counter(), admitted,
                      (d._history_generation, d._recovery_generation))
        assert entries[-1]['delivery_status'] == 'pasted'
        assert fields.document == d.layout_explicit_lists(raw) + ' '
        assert clipboard.data == [(13, b'original')] and not backend.pending_status()['pending']
    finally:
        d.clipboard_delivery.configure_verified_backend(original_backend)
assert backend.shutdown()
print('Multiline app integration: capture outside locks, Cancel/late capture, two pastes, clipboard preservation, same-window field refusal/return and final list delivery passed')
