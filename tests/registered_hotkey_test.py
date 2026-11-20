"""Exercise Windows registration control without generating keyboard input."""

import sys
import threading
from collections import deque
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import registered_hotkey as hotkey


A = (2 | 4, 0x77)
B = (1 | 2, 0x78)


class FakeWin32:
    def __init__(self):
        self.bindings = {}
        self.attempts = []
        self.conflict = None
        self.unregistration_failure = None
        self.keys_down = set()
        self.messages = deque()
        self.clock = 100

    def register(self, ident, combo):
        self.attempts.append(combo)
        if self.conflict == combo or combo in self.bindings.values():
            return False
        self.bindings[ident] = combo
        return True

    def unregister(self, ident):
        if self.unregistration_failure == ident:
            return False
        return self.bindings.pop(ident, None) is not None

    def hotkey_message(self):
        return self.messages.popleft() if self.messages else None

    def down(self, key):
        return key in self.keys_down

    def ticks(self):
        return self.clock


def check_reservation_and_release():
    native = FakeWin32()
    events = []
    state = hotkey._Registration(native, lambda *signal: events.append(signal))
    assert state.replace(A)
    original = state.current
    native.conflict = B
    assert not state.prepare(B)
    assert state.current == original
    native.conflict = None
    assert state.prepare(B)
    proposal = state.proposed
    assert set(native.bindings.values()) == {A, B}
    state.press((*original, 101), 1.0)
    state.press((*proposal, 101), 1.0)
    assert not events
    native.unregistration_failure = original[0]
    assert not state.finish(True)
    assert state.current == original and state.proposed == proposal
    assert state.finish(False)
    assert native.bindings == {original[0]: A}
    native.unregistration_failure = None

    assert state.prepare(A)
    assert state.finish(True)
    state.press((*original, 100), 1.01)
    assert not events  # A queued press from before saving must not start dictation.
    state.press((*original, 101), 1.02)
    assert events == [(A, True, 1.02)]
    assert not state.prepare(B)
    native.keys_down = {0x77, 0x11, 0x10}
    state.press((*original, 102), 1.03)
    state.poll_release(1.03)
    native.keys_down = {0x11}
    state.poll_release(1.04)
    assert len(events) == 1
    native.keys_down.clear()
    state.poll_release(1.1)
    assert events == [(A, True, 1.02), (A, False, 1.04)]

    native.keys_down = {0x77, 0x11, 0x10}
    state.press((*original, 103), 1.2)
    native.keys_down.discard(0x77)
    state.poll_release(1.3)
    native.keys_down.add(0x77)
    state.poll_release(1.4)
    native.keys_down.discard(0x77)
    state.poll_release(1.5)
    assert events[-1] == (A, True, 1.2)
    native.keys_down.clear()
    state.poll_release(1.6)
    assert events[-1] == (A, False, 1.5)

    assert state.prepare(B)
    proposal = state.proposed
    native.clock = 0xFFFFFFFF
    assert state.finish(True)
    assert list(native.bindings.values()) == [B]
    state.press((*original, 0), 2.0)
    state.press((*proposal, 0xFFFFFFFE), 2.0)
    assert len(events) == 4
    state.press((*proposal, 0), 2.0)
    state.poll_release(2.01)
    assert events[-2:] == [(B, True, 2.0), (B, False, 2.01)]
    assert state.replace(None)
    assert native.bindings == {}
    assert state.close()


def check_worker_and_cancelled_request():
    native = FakeWin32()
    callback_entered = threading.Event()
    callback_continue = threading.Event()
    release = threading.Event()
    events = []

    def receive(*signal):
        events.append(signal)
        if signal[1]:
            callback_entered.set()
            assert callback_continue.wait(1)
        else:
            release.set()

    with patch.object(hotkey, '_Win32', return_value=native):
        backend = hotkey.RegisteredHotkey(receive)
        try:
            assert not backend.prepare((0x4000, 0x77))
            assert backend.replace(A)
            ident = next(iter(native.bindings))
            native.messages.append((ident, A, 101))
            assert callback_entered.wait(1)
            with patch.object(hotkey, '_COMMAND_TIMEOUT', 0.02):
                assert not backend.replace(B)
            callback_continue.set()
            assert release.wait(1)
            assert [signal[:2] for signal in events] == [(A, True), (A, False)]
            assert native.bindings == {ident: A}  # Timed-out replacement did not apply late.
            assert backend.prepare(B)
            assert backend.finish(False)
            assert native.bindings == {ident: A}
        finally:
            callback_continue.set()
            assert backend.close()
        assert native.bindings == {}
        assert not backend.replace(A)


def check_escape_registration():
    native = FakeWin32()
    recording = False
    cancellations = []
    state = hotkey._Registration(native, lambda *signal: None, lambda: recording,
                                   lambda: cancellations.append(True))
    assert state.replace(A)
    state.sync_cancel()
    assert list(native.bindings.values()) == [A]

    recording = True
    native.conflict = hotkey._ESCAPE
    state.sync_cancel()
    native.conflict = None
    state.sync_cancel()
    assert native.attempts.count(hotkey._ESCAPE) == 1
    assert list(native.bindings.values()) == [A]

    recording = False
    state.sync_cancel()
    recording = True
    state.sync_cancel()
    first_cancel = state.cancel_id
    assert first_cancel is not None and native.bindings[first_cancel] == hotkey._ESCAPE
    recording = False
    state.press((first_cancel, hotkey._ESCAPE, 101), 1.1)
    assert not cancellations  # A queued Escape after recording stopped cannot cancel anything.
    state.sync_cancel()
    assert first_cancel not in native.bindings

    recording = True
    state.sync_cancel()
    second_cancel = state.cancel_id
    assert second_cancel not in (None, first_cancel)
    state.press((first_cancel, hotkey._ESCAPE, 102), 1.2)
    state.press((second_cancel, A, 102), 1.2)
    assert not cancellations
    state.press((second_cancel, hotkey._ESCAPE, 103), 1.3)
    state.press((second_cancel, hotkey._ESCAPE, 104), 1.4)
    assert cancellations == [True]
    assert state.close()
    assert native.bindings == {}


def check_native_registration():
    first = hotkey.RegisteredHotkey(lambda *signal: None)
    second = hotkey.RegisteredHotkey(lambda *signal: None)
    test_combo = (1 | 2 | 4, 0x87)  # Ctrl+Alt+Shift+F24, reserved only during this check.
    try:
        assert first.replace(test_combo)
        assert not second.replace(test_combo)
        assert first.replace(None)
        assert second.replace(test_combo)
    finally:
        first_closed, second_closed = first.close(), second.close()
        assert first_closed and second_closed


if __name__ == '__main__':
    check_reservation_and_release()
    check_worker_and_cancelled_request()
    check_escape_registration()
    print('Registered shortcut transactions, held releases, Escape, and worker cleanup passed.')
    if '--native' in sys.argv:
        check_native_registration()
        print('Native reservation, conflict rejection, and cleanup passed.')
