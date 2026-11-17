"""Deterministic pre-input races; no real keyboard, model, mic or clipboard."""
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch

os.environ.setdefault('ANTHROPIC_API_KEY', 'disabled-test-value')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d


class Native:
    def __init__(self):
        self.target = d.text_delivery.Target(11, 22, 33)
        self.held = False
        self.calls = []

    def target_focused(self, target):
        return target == self.target

    def target_ready(self, target):
        return self.target_focused(target) and not self.held

    def send_units(self, units, target):
        assert self.target_ready(target)
        self.calls.append(units)
        return 2 * len(units)


def setup():
    native = Native()
    d._direct_native = native
    d._history_generation = d._recovery_generation = 0
    d._quitting = False
    d._hotkey_down = None
    d._recording.clear()
    d._record_target = native.target
    return native


def stale_queued_entry():
    """A privacy clear while waiting for the send lock cancels admission."""
    native = setup()
    entered = threading.Event()
    results, errors = [], []

    def send():
        try:
            d._lab_thread.history_generation = 0
            d._lab_thread.recovery_generation = 0
            entered.set()
            results.append(d.paste_text('earlier immutable dictation', native.target))
        except BaseException as error:
            errors.append(error)

    with d._direct_delivery_lock:
        thread = threading.Thread(target=send)
        thread.start()
        assert entered.wait(1)
        d._recovery_generation = 1
    thread.join(1)
    assert not thread.is_alive() and not errors
    assert results[0]['status'] == 'retained', 'Cleared queued dictation was sent to Windows'
    assert not native.calls, 'Stale recovery ownership reached native input'


def normal_release_between_reads():
    """Normal Stop is not Cancel when release occurs during the loop check."""
    native = setup()
    native.held = True
    d._hotkey_down = d.pk.Key.ctrl_r

    class ReleaseBetweenReads:
        reads = 0

        def is_set(self):
            self.reads += 1
            if self.reads == 1:
                return True  # Admission observes the active overlapping capture.
            # on_release normally clears the hotkey, then Stop clears recording.
            d._hotkey_down = None
            native.held = False
            return False

    with patch.object(d, '_recording', ReleaseBetweenReads()):
        result = d.paste_text('normal released dictation', native.target)
    assert result['status'] == 'sent', 'Normal Right Ctrl release was mistaken for cancellation'
    assert len(native.calls) == 1


failures = []
with patch.dict(d.SETTINGS, {'hotkey': 'ctrl_r'}), \
     patch.object(d.clipboard_delivery, 'pending_status', return_value={'pending': False}):
    for check in (stale_queued_entry, normal_release_between_reads):
        try:
            check()
        except AssertionError as error:
            failures.append(str(error))
        finally:
            d._recording.clear()
            d._hotkey_down = None
assert not failures, '; '.join(failures)
print('Delivery admission: queued privacy clear blocks input and normal release '
      'during readiness polling still sends exactly once')
