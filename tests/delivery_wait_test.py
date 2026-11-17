"""A finished result waits only for our own overlapping Right Ctrl capture."""
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

os.environ.setdefault('ANTHROPIC_API_KEY', 'disabled-test-value')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d


class Native:
    def __init__(self):
        self.target = d.text_delivery.Target(101, 202, 303)
        self.focused = True
        self.held = True
        self.calls = []
        self.checked = threading.Event()

    def target_focused(self, target):
        self.checked.set()
        return target == self.target and self.focused

    def target_ready(self, target):
        return self.target_focused(target) and not self.held

    def send_units(self, units, target):
        assert self.target_ready(target)
        self.calls.append(''.join(chr(unit) for unit in units))
        return 2 * len(units)


def prepare():
    native = Native()
    d._direct_native = native
    d._quitting = False
    d._state = 'recording'
    d._recording.set()
    d._hotkey_down = d.pk.Key.ctrl_r
    d._record_target = native.target
    d._history_generation = d._recovery_generation = 0
    d._gen = 10
    return native


def start_send(native):
    result = []
    thread = threading.Thread(target=lambda: result.append(
        d.paste_text('exact finished first dictation', native.target)))
    thread.start()
    assert native.checked.wait(1)
    return thread, result


with patch.dict(d.SETTINGS, {'hotkey': 'ctrl_r'}), \
     patch.object(d.clipboard_delivery, 'pending_status', return_value={'pending': False}):
    native = prepare()
    thread, result = start_send(native)
    time.sleep(.08)
    assert thread.is_alive() and not result and not native.calls, \
        'A finished dictation was discarded instead of waiting for our own Right Ctrl release'
    native.held = False
    d._hotkey_down = None
    d._recording.clear()
    thread.join(1)
    assert not thread.is_alive() and result[0]['status'] == 'sent'
    assert native.calls == ['exact finished first dictation']

    for failure in ('focus', 'clear', 'quit', 'cancel', 'capture_replaced'):
        native = prepare()
        thread, result = start_send(native)
        time.sleep(.03)
        if failure == 'focus': native.focused = False
        if failure == 'clear': d._recovery_generation += 1
        if failure == 'quit': d._quitting = True
        if failure == 'cancel': d._recording.clear()
        if failure == 'capture_replaced': d._gen += 1
        thread.join(1)
        still_waiting = thread.is_alive()
        if still_waiting:
            d._recording.clear()
            thread.join(1)
        assert not still_waiting and result[0]['status'] == 'retained', failure
        assert not native.calls, 'Waiting replayed text after ' + failure

    # An arbitrary modifier or different capture must not trigger waiting.
    for state in ('unrelated', 'other_target'):
        native = prepare()
        if state == 'unrelated': d._hotkey_down = None; d._recording.clear()
        else: d._record_target = d.text_delivery.Target(102, 202, 304)
        started = time.monotonic()
        assert d.paste_text('retain exact result', native.target)['status'] == 'retained'
        assert time.monotonic() - started < .5 and not native.calls

    native = prepare()
    with patch.object(d, 'MAX_REC_S', .05):
        started = time.monotonic()
        assert d.paste_text('bounded wait result', native.target)['status'] == 'retained'
        assert time.monotonic() - started < 1 and not native.calls

d._recording.clear(); d._hotkey_down = None; d._quitting = False
print('Delivery wait: own Right Ctrl release, immutable text, focus/clear/quit/cancel '
      'fences and bounded failure passed; no real input')
