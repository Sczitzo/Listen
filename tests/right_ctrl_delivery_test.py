"""Consecutive Right Ctrl dictations must survive their own injected input.

Run through tests/run_tests.py's guarded temporary-copy runner. No hook is
started, no native input is sent, and the microphone/model are test doubles.
The installed pynput conversion and app callbacks remain real.
"""
import ctypes
import os
from pathlib import Path
import queue
import sys
from unittest.mock import patch

os.environ.setdefault('ANTHROPIC_API_KEY', 'disabled-test-value')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d


class ImmediateThread:
    def is_alive(self): return False
    def __init__(self, target, args=(), kwargs=None, **unused):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


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


class Native:
    target = d.text_delivery.Target(101, 202)

    def __init__(self):
        self.sent = []
        self.listener = None

    def capture_target(self):
        return self.target

    def target_ready(self, target):
        return target == self.target

    def dispatch(self, vk, scan, down, flags=0):
        data = self.listener._KBDLLHOOKSTRUCT()
        data.vkCode, data.scanCode, data.flags = vk, scan, flags
        converted = self.listener._convert(0, 0x100 if down else 0x101,
                                           ctypes.addressof(data))
        if converted is not None:
            self.listener._process(*converted)

    def send_units(self, units, target):
        assert target == self.target
        self.sent.append(bytes().join(unit.to_bytes(2, 'little')
                                      for unit in units).decode('utf-16-le'))
        for unit in units:
            self.dispatch(0xE7, unit, True, 0x10)
            self.dispatch(0xE7, unit, False, 0x10)
        return 2 * len(units)


d._state = 'idle'
d._quitting = False
d._hotkey_down = None
d._skip_release = d._locked = False
d._recording.clear()
d._streams.clear()
d._stream = d._stream_gen = None
d._mic_ready = False
d._jobs = queue.Queue()
d._pending_recordings = 0
d._retained = []
d._last_error = d._last_delivery_error = ''
d._busy = ''
d._tray = None
native = Native()
d._direct_native = native
# Construction installs no hook; start() is deliberately never called.
listener = d.pk.Listener(on_press=d.on_press, on_release=d.on_release,
                         win32_event_filter=d._event_filter)
native.listener = listener
d._listener = listener
phrases = {1: 'First check blue lantern', 2: 'Second check green river'}


def recognize(chunks, held):
    return phrases[int(chunks[0][0, 0])]


def process_job():
    chunks, held, released, target, generations, mic_released = d._jobs.get_nowait()
    assert mic_released.is_set(), 'Final recognition must wait for microphone close'
    try:
        d.process(chunks, held, released, target, generations)
    finally:
        d._jobs.task_done()
        d._busy = ''


with patch.dict(d.SETTINGS, {'hotkey': 'ctrl_r', 'cleanup': False,
                             'learn': False, 'live_preview': False}), \
     patch.object(d.threading, 'Thread', ImmediateThread), \
     patch.object(d.sd, 'InputStream', Stream), \
     patch.object(d, 'resolve_microphone', return_value=0), \
     patch.object(d, '_chime', lambda *args: None), \
     patch.object(d, 'transcribe', recognize), \
     patch.object(d.clipboard_delivery, 'pending_status', return_value={'pending': False}):
    d.on_press(d.pk.Key.ctrl_r)
    first = Stream.instances[-1]
    first.callback(d.np.ones((16, 1), dtype='float32'), 16, None, None)
    d._press_t -= 1
    d.on_release(d.pk.Key.ctrl_r)
    assert first.stopped and first.closed and d._jobs.qsize() == 1

    d.on_press(d.pk.Key.ctrl_r)
    second = Stream.instances[-1]
    second.callback(2 * d.np.ones((16, 1), dtype='float32'), 16, None, None)
    process_job()  # A delivers while Right Ctrl is still capturing B.
    assert d._recording.is_set(), 'Delivery A injected Unicode cancelled recording B'
    assert d._hotkey_down == d.pk.Key.ctrl_r and not second.closed

    # Both Windows injection flags must be ignored by recognition; lower-IL
    # input may come from another assistive app and must not discard capture.
    native.dispatch(0xE7, ord('x'), True, 0x02)
    native.dispatch(0xE7, ord('x'), False, 0x02)
    assert d._recording.is_set(), 'Lower-integrity injected input cancelled capture'

    d._press_t -= 1
    d.on_release(d.pk.Key.ctrl_r)
    assert second.stopped and second.closed and not d._recording.is_set()
    process_job()
    assert native.sent == ['First check blue lantern ', 'Second check green river ']
    assert not d._retained and d._jobs.unfinished_tasks == 0
    assert d._pending_recordings == 0 and not d._mic_ready

    # Genuine user shortcuts must retain their established cancellation rule.
    d.on_press(d.pk.Key.ctrl_r)
    third = Stream.instances[-1]
    d.on_press(d.pk.KeyCode.from_char('c'))
    assert not d._recording.is_set() and third.closed and d._jobs.empty()
    d.on_release(d.pk.Key.ctrl_r)

assert not listener.is_alive(), 'This regression must never install a live hook'

# Holding Escape after cancelling must not leak repeats or its paired up into
# the destination while Right Ctrl is still physically held.
class Suppressed(Exception):
    pass


def suppress():
    raise Suppressed()


with patch.object(d, '_listener', type('Listener', (), {'suppress_event': staticmethod(suppress)})()), \
     patch.object(d, '_cancel', side_effect=d._recording.clear):
    d._recording.set()
    for message in (0x100, 0x100, 0x101):
        data = type('Edge', (), {'vkCode': 0x1B, 'flags': 0})()
        swallowed = False
        try:
            d._event_filter(message, data)
        except Suppressed:
            swallowed = True
        assert swallowed, 'Cancel leaked an Escape repeat or unmatched release'
    d._event_filter(0x100, data)  # An unrelated idle Escape is available again.
print('Right Ctrl delivery: injected Unicode cannot cancel a newer capture; '
      'ordered entries, microphone release and genuine shortcut cancellation passed')
